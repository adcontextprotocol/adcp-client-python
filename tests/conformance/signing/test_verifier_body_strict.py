"""Checklist step 14: strict body well-formedness (adcp-client-python#1264).

A valid signature authenticates bytes, not their interpretation. The verifier
must reject a signed body that different parsers could read differently --
duplicate object keys at any depth, invalid UTF-8, or not JSON at all -- with
``request_body_malformed``, and only after step 13 has burned the nonce.

No AdCP conformance vector covers step 14 on the request profile yet (the
vendored request-signing tree has none), so these are direct behavior tests.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from adcp.signing import (
    REQUEST_BODY_MALFORMED,
    REQUEST_SIGNATURE_REPLAYED,
    InMemoryReplayStore,
    RequestBodyMalformedError,
    SignatureVerificationError,
    StaticJwksResolver,
    VerifiedSigner,
    VerifierCapability,
    VerifyOptions,
    sign_request,
    verify_flask_request,
    verify_request_signature,
    verify_starlette_request,
)
from adcp.signing._strict_json import StrictJsonError, parse_strict_json
from adcp.signing.crypto import private_key_from_jwk
from adcp.signing.errors import WEBHOOK_BODY_MALFORMED
from adcp.signing.verifier import CoversDigestPolicy
from adcp.webhooks import (
    LegacyHmacFallback,
    LegacyWebhookHmacOptions,
    VerifiedWebhookSender,
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookVerifyOptions,
    get_adcp_signed_headers_for_webhook,
    sign_webhook,
    verify_webhook_signature,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
ED25519_KEY = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")
WEBHOOK_ED25519 = {
    **copy.deepcopy(ED25519_KEY),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}

URL = "https://seller.example.com/mcp"
NOW = 1776520800
NONCE = "step14-nonce-0000000001"

# The issue's motivating body: a lenient parser (json.loads keeps the last
# duplicate) classifies this as get_products, a first-wins parser runs
# create_media_buy.
OPERATION_SMUGGLE = (
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
    b'"params":{"name":"create_media_buy","name":"get_products","arguments":{}}}'
)


def _signed(body: bytes, *, method: str = "POST", nonce: str = NONCE) -> dict[str, str]:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    headers = {"Content-Type": "application/json"} if body else {}
    signed = sign_request(
        method=method,
        url=URL,
        headers=headers,
        body=body,
        private_key=private_key,
        key_id=ED25519_KEY["kid"],
        alg="ed25519",
        created=NOW,
        nonce=nonce,
        signing_profile_version="3.2",
    )
    return {**headers, **signed.as_dict()}


def _options(
    replay_store: InMemoryReplayStore | None = None,
    *,
    covers_content_digest: CoversDigestPolicy = "required",
) -> VerifyOptions:
    return VerifyOptions(
        now=float(NOW),
        capability=VerifierCapability(covers_content_digest=covers_content_digest),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [ED25519_KEY]}),
        replay_store=replay_store if replay_store is not None else InMemoryReplayStore(),
        signing_profile_version="3.2",
    )


def _verify(body: bytes, options: VerifyOptions | None = None) -> VerifiedSigner:
    return verify_request_signature(
        method="POST",
        url=URL,
        headers=_signed(body),
        body=body,
        options=options or _options(),
    )


def _assert_malformed(body: bytes, reason: str) -> SignatureVerificationError:
    with pytest.raises(SignatureVerificationError) as exc:
        _verify(body)
    assert exc.value.code == REQUEST_BODY_MALFORMED
    assert exc.value.step == 14
    assert exc.value.detail == {"reason": reason}
    # The signature verified, so the rejection is attributable to the signer.
    assert isinstance(exc.value, RequestBodyMalformedError)
    assert exc.value.signer.key_id == ED25519_KEY["kid"]
    assert exc.value.signer.parsed_body is None
    return exc.value


# ---- accepted bodies expose the strict parse ----


def test_valid_body_is_exposed_as_parsed_body() -> None:
    body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_products","a":[{"x":1}]}}'
    signer = _verify(body)
    assert signer.parsed_body == json.loads(body)


def test_parsed_body_does_not_affect_equality_hash_or_repr() -> None:
    body = b'{"secret_ish":"value-that-must-not-reach-logs"}'
    signer = _verify(body)
    bare = VerifiedSigner(
        key_id=signer.key_id,
        alg=signer.alg,
        label=signer.label,
        verified_at=signer.verified_at,
        agent_url=signer.agent_url,
    )
    assert signer == bare
    assert hash(signer) == hash(bare)
    assert "value-that-must-not-reach-logs" not in repr(signer)


def test_bodyless_request_has_no_parsed_body() -> None:
    signer = verify_request_signature(
        method="GET",
        url=URL,
        headers=_signed(b"", method="GET"),
        body=b"",
        options=_options(covers_content_digest="either"),
    )
    assert signer.parsed_body is None


def test_same_key_in_sibling_objects_is_not_a_duplicate() -> None:
    body = b'{"a":{"id":1},"b":{"id":2},"c":[{"id":3},{"id":4}]}'
    assert _verify(body).parsed_body["c"][1] == {"id": 4}


# ---- duplicate keys at any depth ----


def test_operation_smuggling_body_is_rejected() -> None:
    _assert_malformed(OPERATION_SMUGGLE, "duplicate_key")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"idempotency_key":"a","idempotency_key":"b"}', id="top-level"),
        pytest.param(b'{"params":{"arguments":{"budget":1,"budget":1000000}}}', id="nested"),
        pytest.param(b'{"packages":[{"id":"p1"},{"id":"p2","id":"p3"}]}', id="object-in-array"),
        pytest.param(b'{"a":{"b":{"c":{"d":1,"d":1}}}}', id="deep-identical-values"),
        pytest.param(b'{"name":1,"\\u006eame":2}', id="escape-equivalent"),
    ],
)
def test_duplicate_keys_are_rejected(body: bytes) -> None:
    _assert_malformed(body, "duplicate_key")


def test_rejection_does_not_echo_key_names_or_body_bytes() -> None:
    body = b'{"\\u202eevil\\u0007":1,"\\u202eevil\\u0007":2,"payload":"attacker-bytes"}'
    exc = _assert_malformed(body, "duplicate_key")
    rendered = f"{exc} {exc.detail!r}"
    assert "evil" not in rendered
    assert "attacker-bytes" not in rendered


# ---- invalid UTF-8 and non-JSON ----


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"name":"\xff"}', id="invalid-byte"),
        pytest.param(b'{"name":"\xed\xa0\x80"}', id="encoded-surrogate"),
        pytest.param(b'{"name":"\xc3"}', id="truncated-sequence"),
        # json.loads(bytes) would sniff and accept UTF-16; the wire is UTF-8.
        pytest.param('{"name":"x"}'.encode("utf-16"), id="utf-16"),
    ],
)
def test_invalid_utf8_is_rejected(body: bytes) -> None:
    _assert_malformed(body, "invalid_utf8")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not-json", id="text"),
        pytest.param(b'{"a":1} trailing', id="trailing-garbage"),
        pytest.param(b'{"a":1,}', id="trailing-comma"),
        pytest.param(b'{"a":NaN}', id="nan"),
        pytest.param(b'{"a":-Infinity}', id="neg-infinity"),
        pytest.param(b'\xef\xbb\xbf{"a":1}', id="utf8-bom"),
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="pathological-nesting"),
        pytest.param(b'{"n":' + b"9" * 10_000 + b"}", id="int-digit-limit"),
        pytest.param(b'{"n":1e999}', id="float-overflow-inf"),
        pytest.param(b'{"n":-1.5e400}', id="float-overflow-neg-inf"),
        pytest.param(b'{"name":"\\ud800"}', id="lone-high-surrogate-value"),
        pytest.param(b'{"\\udc00":1}', id="lone-low-surrogate-key"),
        pytest.param(b'["\\ud83dx"]', id="high-surrogate-without-low"),
        pytest.param(b'{"a":[{"b":"ok\\udfff"}]}', id="nested-lone-surrogate"),
    ],
)
def test_non_json_is_rejected(body: bytes) -> None:
    _assert_malformed(body, "invalid_json")


# ---- ordering: step 13 burns the nonce before step 14 rejects ----


def test_malformed_body_burns_the_nonce() -> None:
    store = InMemoryReplayStore()
    options = _options(store)
    headers = _signed(OPERATION_SMUGGLE)

    with pytest.raises(SignatureVerificationError) as first:
        verify_request_signature(
            method="POST", url=URL, headers=headers, body=OPERATION_SMUGGLE, options=options
        )
    assert first.value.code == REQUEST_BODY_MALFORMED
    assert store.seen(ED25519_KEY["kid"], NONCE)

    # Replaying the captured frame is now a cheap replay rejection, not a
    # second crypto verify followed by another body parse.
    with pytest.raises(SignatureVerificationError) as replay:
        verify_request_signature(
            method="POST", url=URL, headers=headers, body=OPERATION_SMUGGLE, options=options
        )
    assert replay.value.code == REQUEST_SIGNATURE_REPLAYED


def test_step_14_runs_without_a_replay_store() -> None:
    options = VerifyOptions(
        now=float(NOW),
        capability=VerifierCapability(covers_content_digest="required"),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [ED25519_KEY]}),
        replay_store=None,
        signing_profile_version="3.2",
    )
    with pytest.raises(SignatureVerificationError) as exc:
        _verify(OPERATION_SMUGGLE, options)
    assert exc.value.code == REQUEST_BODY_MALFORMED


# ---- framework wrappers and the webhook profile ----


class _Headers(dict[str, str]):
    raw: list[tuple[bytes, bytes]]


class _FakeStarletteRequest:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self.method = "POST"
        self.url = URL
        self.headers = _Headers(headers)
        self.headers.raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeFlaskRequest:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self.method = "POST"
        self.url = URL
        self.headers = headers
        self._body = body

    def get_data(self) -> bytes:
        return self._body


def test_starlette_wrapper_exposes_parsed_body_and_rejects_duplicates() -> None:
    good = b'{"method":"tools/call","params":{"name":"get_products"}}'
    signer = asyncio.run(
        verify_starlette_request(_FakeStarletteRequest(good, _signed(good)), options=_options())
    )
    assert signer.parsed_body == {"method": "tools/call", "params": {"name": "get_products"}}

    request = _FakeStarletteRequest(OPERATION_SMUGGLE, _signed(OPERATION_SMUGGLE))
    with pytest.raises(SignatureVerificationError) as exc:
        asyncio.run(verify_starlette_request(request, options=_options()))
    assert exc.value.code == REQUEST_BODY_MALFORMED


def test_flask_wrapper_exposes_parsed_body_and_rejects_duplicates() -> None:
    good = b'{"method":"tools/call","params":{"name":"get_products"}}'
    signer = verify_flask_request(_FakeFlaskRequest(good, _signed(good)), options=_options())
    assert signer.parsed_body["params"]["name"] == "get_products"

    request = _FakeFlaskRequest(OPERATION_SMUGGLE, _signed(OPERATION_SMUGGLE))
    with pytest.raises(SignatureVerificationError) as exc:
        verify_flask_request(request, options=_options())
    assert exc.value.code == REQUEST_BODY_MALFORMED


def test_webhook_profile_retags_to_webhook_body_malformed() -> None:
    url = "https://buyer.example.com/webhooks/adcp"
    body = b'{"idempotency_key":"whk_aaaaaaaaaaaaaaaa","status":"working","status":"failed"}'
    private_key = private_key_from_jwk(WEBHOOK_ED25519, d_field="_private_d_for_test_only")
    signed = sign_webhook(
        method="POST",
        url=url,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id=WEBHOOK_ED25519["kid"],
        alg="ed25519",
    )
    with pytest.raises(SignatureVerificationError) as exc:
        verify_webhook_signature(
            method="POST",
            url=url,
            headers={"Content-Type": "application/json", **signed.as_dict()},
            body=body,
            options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_ED25519]}),
            ),
        )
    assert exc.value.code == WEBHOOK_BODY_MALFORMED
    assert exc.value.step == 14


# ---- parser unit ----


def test_parser_preserves_ordinary_json_values() -> None:
    body = b'{"s":"\\u00e9\\ud83d\\ude00","n":-1.5e3,"b":[true,false,null],"o":{}}'
    assert parse_strict_json(body) == json.loads(body)


def test_parser_error_is_a_value_error_with_reason() -> None:
    with pytest.raises(ValueError) as exc:
        parse_strict_json(b'{"a":1,"a":1}')
    assert isinstance(exc.value, StrictJsonError)
    assert exc.value.reason == "duplicate_key"


# ---- body authenticity: well-formed is not the same as signed ----


def _signed_without_digest(body: bytes) -> dict[str, str]:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    headers = {"Content-Type": "application/json"}
    signed = sign_request(
        method="POST",
        url=URL,
        headers=headers,
        body=body,
        private_key=private_key,
        key_id=ED25519_KEY["kid"],
        alg="ed25519",
        created=NOW,
        nonce=NONCE,
        cover_content_digest=False,
        signing_profile_version="3.1",
    )
    return {**headers, **signed.as_dict()}


def _legacy_either_options() -> VerifyOptions:
    return VerifyOptions(
        now=float(NOW),
        capability=VerifierCapability(covers_content_digest="either"),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [ED25519_KEY]}),
        signing_profile_version="3.1",
    )


def test_digest_covered_body_is_authenticated() -> None:
    assert _verify(b'{"a":1}').body_authenticated is True


def test_body_without_digest_coverage_is_parsed_but_not_authenticated() -> None:
    body = b'{"params":{"name":"get_products"}}'
    signer = verify_request_signature(
        method="POST",
        url=URL,
        headers=_signed_without_digest(body),
        body=body,
        options=_legacy_either_options(),
    )
    assert signer.parsed_body == {"params": {"name": "get_products"}}
    assert signer.body_authenticated is False


def test_unauthenticated_body_is_still_strictly_parsed() -> None:
    with pytest.raises(RequestBodyMalformedError) as exc:
        verify_request_signature(
            method="POST",
            url=URL,
            headers=_signed_without_digest(OPERATION_SMUGGLE),
            body=OPERATION_SMUGGLE,
            options=_legacy_either_options(),
        )
    assert exc.value.code == REQUEST_BODY_MALFORMED
    assert exc.value.signer.body_authenticated is False


# ---- body type coercion happens before any check reads the body ----


@pytest.mark.parametrize("wrap", [bytearray, memoryview, lambda b: b.decode("utf-8")])
def test_non_bytes_body_types_are_verified_not_crashed(wrap: object) -> None:
    body = b'{"params":{"name":"get_products"}}'
    signer = verify_request_signature(
        method="POST",
        url=URL,
        headers=_signed(body),
        body=wrap(body),  # type: ignore[operator, arg-type]
        options=_options(),
    )
    assert signer.parsed_body == {"params": {"name": "get_products"}}


def test_str_body_with_lone_surrogate_is_rejected_as_invalid_utf8() -> None:
    text = '{"name":"' + chr(0xD800) + '"}'  # an actual lone surrogate code point
    wire = text.encode("utf-8", "surrogatepass")
    with pytest.raises(RequestBodyMalformedError) as exc:
        verify_request_signature(
            method="POST", url=URL, headers=_signed(wire), body=text, options=_options()  # type: ignore[arg-type]
        )
    assert exc.value.detail == {"reason": "invalid_utf8"}


def test_none_body_is_bodyless() -> None:
    signer = verify_request_signature(
        method="GET",
        url=URL,
        headers=_signed(b"", method="GET"),
        body=None,  # type: ignore[arg-type]
        options=_options(covers_content_digest="either"),
    )
    assert signer.parsed_body is None


def test_unsupported_body_type_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="body must be bytes"):
        verify_request_signature(
            method="POST", url=URL, headers=_signed(b"{}"), body=123, options=_options()  # type: ignore[arg-type]
        )


# ---- webhook receiver: attribution kept, HMAC never consulted ----

WEBHOOK_URL = "https://buyer.example.com/webhooks/adcp"
WEBHOOK_DUPLICATE_BODY = (
    b'{"idempotency_key":"whk_duplicate_json_aaaaaaaa",'
    b'"operation_id":"op-1","task_id":"task-1",'
    b'"task_type":"create_media_buy","status":"working","status":"failed",'
    b'"timestamp":"2026-04-19T00:00:00Z"}'
)


def _signed_webhook(body: bytes) -> dict[str, str]:
    private_key = private_key_from_jwk(WEBHOOK_ED25519, d_field="_private_d_for_test_only")
    signed = sign_webhook(
        method="POST",
        url=WEBHOOK_URL,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id=WEBHOOK_ED25519["kid"],
        alg="ed25519",
    )
    return {"Content-Type": "application/json", **signed.as_dict()}


def _receiver(legacy_hmac: LegacyHmacFallback | None = None) -> WebhookReceiver:
    from adcp.server.idempotency import MemoryBackend, WebhookDedupStore

    return WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_ED25519]}),
                sender_url="https://seller.example.com",
            ),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="test-receiver",
            publisher_scope_for=lambda _signer: "test-publisher",
            legacy_hmac=legacy_hmac,
        ),
    )


def test_webhook_body_rejection_keeps_verified_sender() -> None:
    with pytest.raises(RequestBodyMalformedError) as exc:
        verify_webhook_signature(
            method="POST",
            url=WEBHOOK_URL,
            headers=_signed_webhook(WEBHOOK_DUPLICATE_BODY),
            body=WEBHOOK_DUPLICATE_BODY,
            options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_ED25519]}),
            ),
        )
    assert exc.value.code == WEBHOOK_BODY_MALFORMED
    assert isinstance(exc.value.signer, VerifiedWebhookSender)
    assert exc.value.signer.key_id == WEBHOOK_ED25519["kid"]

    outcome = asyncio.run(
        _receiver().receive(
            method="POST",
            url=WEBHOOK_URL,
            headers=_signed_webhook(WEBHOOK_DUPLICATE_BODY),
            body=WEBHOOK_DUPLICATE_BODY,
        )
    )
    assert outcome.rejected is True
    assert outcome.rejection_reason == "body_invalid_json"
    assert outcome.sender_identity == f"https://seller.example.com|{WEBHOOK_ED25519['kid']}"


def test_malformed_9421_body_never_falls_back_to_hmac() -> None:
    # Fallback deliberately permissive (only_when_9421_absent=False) and the
    # request carries valid HMAC headers too: a verified-but-malformed 9421
    # frame must still not consult HMAC at all.
    consulted: list[object] = []
    ts = str(NOW)
    headers = _signed_webhook(WEBHOOK_DUPLICATE_BODY)
    get_adcp_signed_headers_for_webhook(
        headers=headers,
        secret="s" * 32,
        timestamp=ts,
        payload=json.loads(WEBHOOK_DUPLICATE_BODY),
    )

    def options_for(hdrs: object) -> LegacyWebhookHmacOptions:
        consulted.append(hdrs)
        return LegacyWebhookHmacOptions(secret=b"s" * 32, sender_identity="legacy", now=float(NOW))

    fallback = LegacyHmacFallback(options_for=options_for, only_when_9421_absent=False)
    outcome = asyncio.run(
        _receiver(fallback).receive(
            method="POST", url=WEBHOOK_URL, headers=headers, body=WEBHOOK_DUPLICATE_BODY
        )
    )
    assert outcome.rejected is True
    assert outcome.rejection_reason == "body_invalid_json"
    assert consulted == []
