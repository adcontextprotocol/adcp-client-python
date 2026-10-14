"""Structured-field ambiguity fails before discovery, replay, or dispatch."""

from __future__ import annotations

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from adcp.server import RequestSignatureVerification
from adcp.server.signed_requests import SignedRequestVerificationMiddleware
from adcp.signing import (
    REQUEST_SIGNATURE_HEADER_MALFORMED,
    InMemoryReplayStore,
    SignatureVerificationError,
    VerifierCapability,
    VerifyOptions,
    sign_request,
    sign_wba_request,
    verify_request_signature,
    verify_wba_request,
)
from adcp.signing._structured_fields import reject_dictionary_duplicates
from adcp.signing.canonical import parse_signature_input_header
from adcp.signing.crypto import extract_signature_bytes
from adcp.signing.wba import WbaVerificationError, _dictionary, uses_web_bot_auth


@pytest.mark.parametrize(
    "field",
    [
        'sig1=("@method");created=1;created=2',
        'sig1=("a";key="one";key="two")',
        "sig1=(1;key=one;key=two 2)",
        "sig1=(:YWJj:;p=1;p=2)",
        'sig1=("a");flag;flag=?0',
        'sig1=:YWJj:;p="one";p="two"',
        'sig1="https://buyer.com";type=directory;type=directory',
        'sig1="a", sig1="b"',
        "sig1;p=1, sig1;p=2",
        'sig1=("a");nonce="comma, ; created=1";nonce="second"',
        'sig1=("a";p="escaped \\" quote";p="second")',
        'sig1=("a");p=:YWJj:;p=:YWJj:',
        'sig1=("a");p=%"comma, (;)";p=%"second"',
    ],
)
def test_duplicate_dictionary_or_parameter_evidence_is_preserved(field):
    with pytest.raises(ValueError, match="duplicate"):
        reject_dictionary_duplicates(field)
    with pytest.raises(WbaVerificationError, match="duplicate") as rejected:
        _dictionary(field)
    assert rejected.value.status == 400
    assert not rejected.value.headers


@pytest.mark.parametrize(
    "field",
    [
        "sig1=(1;p=one 2;p=two);p=outer",
        "sig1=(:YWJj:;p=1 :YWJj:;p=2);p=3",
        'sig1=("a";key="one" "b";key="two");key="outer"',
        'sig1=("a";p=1 "a";p=1)',
        'sig1=("a");nonce="comma, ; created=1";created=2',
        'sig1=("a");nonce="escaped \\" quote, (;)";created=2',
        'sig1=("a");nonce="trailing \\\\";created=2',
        "sig1;flag=1,\t sig2;flag=2",
        'sig1=("a";p=1),\t sig2=("a";p=2)',
        'sig1=(%"comma, (;)";p=1 %"a%22b";p=2);p=outer',
        "sig1=(@123;p=?1 @456;p=?0);p=outer",
    ],
)
def test_parameter_scopes_and_quoted_delimiters_remain_legal(field):
    reject_dictionary_duplicates(field)
    assert _dictionary(field)


def test_existing_public_readers_reject_duplicate_labels_and_parameters():
    for field in (
        'sig1=("@method");created=1;created=2',
        'sig1=("@method"), sig1=("@path")',
    ):
        with pytest.raises(ValueError, match="duplicate"):
            parse_signature_input_header(field)
    with pytest.raises(ValueError, match="duplicate"):
        extract_signature_bytes("sig1=:YWJj:, sig1=:YWJj:")
    # Keep explicitly enabled legacy Base64URL parsing intact.
    assert extract_signature_bytes("sig1=:-_8:") == b"\xfb\xff"


@pytest.mark.parametrize(
    "nonce",
    ["web-bot-auth", 'prefix;tag=\\"web-bot-auth\\"', "prefix, web-bot-auth"],
)
def test_legacy_duplicate_with_quoted_marker_text_keeps_legacy_malformed_code(nonce):
    field = f'sig1=("@method");created=1;created=2;nonce="{nonce}";tag="adcp/request-signing/v1"'
    headers = {"Signature-Input": field, "Signature": "sig1=:YWJj:"}
    assert not uses_web_bot_auth(headers)
    with pytest.raises(SignatureVerificationError) as rejected:
        verify_request_signature(
            method="POST",
            url="https://seller.com/mcp",
            headers=headers,
            body=b"{}",
            options=VerifyOptions(
                now=1000,
                jwks_resolver=lambda key: pytest.fail("No discovery"),
                capability=VerifierCapability(covers_content_digest="required"),
                operation="create_media_buy",
            ),
        )
    assert rejected.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED


@pytest.mark.parametrize(
    "field",
    [
        'sig1=("@method");tag="web-bot-auth";tag="adcp/request-signing/v1"',
        'sig1=("@method");created=1;created=2;tag="web-bot-auth"',
        'sig1=("@method");tag="web-bot-auth", sig1=("@path");tag="legacy"',
        'sig1=("@method");tag="web-bot-auth";nonce="unterminated',
    ],
)
def test_explicit_wba_marker_cannot_be_overwritten_or_downgraded(field):
    assert uses_web_bot_auth({"Signature-Input": field})
    with pytest.raises(WbaVerificationError) as rejected:
        _dictionary(field)
    assert rejected.value.status == 400


def test_component_tag_parameter_is_not_a_signature_profile_marker():
    assert not uses_web_bot_auth(
        {"Signature-Input": 'sig1=("x";tag="web-bot-auth");tag="adcp/request-signing/v1"'}
    )


class CountingReplayStore(InMemoryReplayStore):
    def __init__(self):
        super().__init__()
        self.claims = []

    def claim(self, keyid, nonce, ttl_seconds):
        self.claims.append((keyid, nonce))
        return super().claim(keyid, nonce, ttl_seconds)


ATTACKS = [
    ("Signature-Input", "label"),
    ("Signature-Input", "parameter"),
    ("Signature-Input", "unused_member"),
    ("Signature", "label"),
    ("Signature", "parameter"),
    ("Content-Digest", "parameter"),
]


def attack(headers, field, kind):
    value = headers[field]
    if kind == "label":
        return {**headers, field: value + ", " + value}
    if kind == "unused_member":
        return {**headers, field: value + ', ignored=("a");created=1;created=2'}
    suffix = {
        "Signature-Input": ";created=1000",
        "Signature-Agent": ";type=directory;type=directory",
    }.get(field, ";p=1;p=2")
    return {**headers, field: value + suffix}


@pytest.mark.parametrize("field,kind", ATTACKS)
def test_legacy_ambiguity_is_malformed_before_resolver_or_nonce(field, kind):
    calls, replay = [], CountingReplayStore()
    key = Ed25519PrivateKey.generate()
    headers = {"Content-Type": "application/json"}
    headers.update(
        sign_request(
            method="POST",
            url="https://seller.com/mcp",
            headers=headers,
            body=b"{}",
            private_key=key,
            key_id="kid",
            alg="ed25519",
            created=1000,
            signing_profile_version="3.2",
        ).as_dict()
    )

    def resolve(keyid):
        calls.append(keyid)
        pytest.fail("Malformed fields must not reach discovery")

    with pytest.raises(SignatureVerificationError) as rejected:
        verify_request_signature(
            method="POST",
            url="https://seller.com/mcp",
            headers=attack(headers, field, kind),
            body=b"{}",
            options=VerifyOptions(
                now=1000,
                jwks_resolver=resolve,
                capability=VerifierCapability(covers_content_digest="required"),
                operation="create_media_buy",
                replay_store=replay,
                signing_profile_version="3.2",
            ),
        )
    assert rejected.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert not calls and not replay.claims


@pytest.mark.parametrize(
    "field,kind", ATTACKS + [("Signature-Agent", "label"), ("Signature-Agent", "parameter")]
)
async def test_wba_ambiguity_is_400_before_discovery_revocation_or_nonce(field, kind):
    calls, replay = [], CountingReplayStore()
    key = Ed25519PrivateKey.generate()
    headers = {"Content-Type": "application/json"}
    headers.update(
        sign_wba_request(
            method="POST",
            url="https://seller.com/mcp",
            headers=headers,
            body=b"{}",
            private_key=key,
            agent_origin="https://buyer.com",
            created=1000,
        )
    )

    async def resolve(keyid, *, signer_origin):
        calls.append((keyid, signer_origin))
        pytest.fail("Malformed fields must not reach discovery")

    async def revoked(origin, keyid):
        calls.append((origin, keyid))
        pytest.fail("Malformed fields must not reach revocation")

    with pytest.raises(WbaVerificationError) as rejected:
        await verify_wba_request(
            method="POST",
            url="https://seller.com/mcp",
            headers=attack(headers, field, kind),
            body=b"{}",
            signer_keys=resolve,
            replay_store=replay,
            revocation_checker=revoked,
            now=1000,
        )
    assert rejected.value.status == 400
    assert not calls and not replay.claims


@pytest.mark.parametrize("wba", [False, True])
async def test_framework_rejects_ambiguity_before_business_dispatch(wba):
    calls, dispatched, replay = [], [], CountingReplayStore()
    key = Ed25519PrivateKey.generate()
    headers = {"Content-Type": "application/json"}
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"get_principal","arguments":{}}}'
    )
    if wba:
        headers.update(
            sign_wba_request(
                method="POST",
                url="https://seller.com/mcp",
                headers=headers,
                body=body,
                private_key=key,
                agent_origin="https://buyer.com",
                created=1000,
            )
        )
    else:
        headers.update(
            sign_request(
                method="POST",
                url="https://seller.com/mcp",
                headers=headers,
                body=body,
                private_key=key,
                key_id="kid",
                alg="ed25519",
                created=1000,
                signing_profile_version="3.2",
            ).as_dict()
        )

    async def resolve(keyid, **kwargs):
        calls.append(keyid)
        pytest.fail("Malformed headers must not discover a key")

    async def revoked(origin, keyid):
        calls.append(origin)
        pytest.fail("Malformed headers must not check revocation")

    async def dispatch(scope, receive, send):
        dispatched.append(True)
        pytest.fail("Malformed headers must not dispatch business logic")

    app = SignedRequestVerificationMiddleware(
        dispatch,
        RequestSignatureVerification(
            signer_keys=resolve,
            enable_web_bot_auth=True,
            replay_store=replay,
            wba_revocation_checker=revoked,
            signing_profile_version="3.2",
        ),
        transport="mcp",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://seller.com"
    ) as client:
        response = await client.post(
            "/mcp", content=body, headers=attack(headers, "Signature-Input", "parameter")
        )
    assert response.status_code == (400 if wba else 401)
    if not wba:
        assert response.json()["error"] == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert not calls and not dispatched and not replay.claims
