"""Draft WBA vectors and the security boundaries of the experimental profile."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.requests import Request
from starlette.responses import JSONResponse

from adcp.decisioning.types import AdcpError
from adcp.server.signed_requests import (
    RequestSignatureVerification,
    SignedRequestVerificationMiddleware,
    apply_verified_signer,
    check_wba_authorization,
)
from adcp.server.translate import build_mcp_error_result
from adcp.signing import (
    DirectoryResponse,
    DiscoveredSignerKeys,
    InMemoryReplayStore,
    OriginBindings,
    SigningConfig,
    StaticSignerKeys,
    WbaVerificationError,
    canonical_origin,
    public_directory_key,
    sign_key_directory,
    sign_request,
    sign_wba_request,
    verify_directory_response,
    verify_wba_request,
)
from adcp.signing.crypto import private_key_from_jwk
from adcp.signing.signer_keys import ResolvedSignerKey

ROOT = Path(__file__).parents[1] / "vectors" / "wba-profile"
KEYS = {key["kid"]: key for key in json.loads((ROOT / "keys.json").read_text())["keys"]}
VECTORS = sorted((ROOT / "positive").glob("*.json")) + sorted((ROOT / "negative").glob("*.json"))


async def _not_revoked(origin, keyid):
    return False


def _mcp_adcp_error(response):
    payload = response.json()
    # The bare ASGI fixture serializes the installed MCP model directly;
    # supported MCP versions expose this field in camel or snake case.
    content = (
        payload["structuredContent"]
        if "structuredContent" in payload
        else payload["structured_content"]
    )
    return content["adcp_error"]


async def test_standalone_verification_needs_only_directory_and_replay_store():
    """Verification is usable on its own; revocation is the authorization layer."""
    with pytest.raises(ValueError, match="replay store"):
        await verify_wba_request(
            method="POST",
            url="https://seller.com/mcp",
            headers={},
            body=b"",
            signer_keys=StaticSignerKeys({}),
            replay_store=None,
        )
    origin = "https://buyer.example.com"
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    headers = {"Content-Type": "application/json"}
    headers.update(
        sign_wba_request(
            method="POST",
            url="https://seller.example.com/mcp",
            headers=headers,
            body=b"{}",
            private_key=private,
            agent_origin=origin,
            created=1000,
        )
    )
    (signer,) = await verify_wba_request(
        method="POST",
        url="https://seller.example.com/mcp",
        headers=headers,
        body=b"{}",
        signer_keys=StaticSignerKeys({origin: {"keys": [jwk]}}),
        replay_store=InMemoryReplayStore(),
        now=1001,
    )
    assert signer.agent_url == origin
    assert signer.revocation is None


def _trust_records(origin):
    from adcp.signing import TrustRecordEvaluator, TrustResponse

    async def fetch(url, *, allow_private=False):
        return TrustResponse(
            json.dumps({"agents": [{"url": origin, "roles": ["adcp:buying"]}]}).encode()
        )

    return TrustRecordEvaluator(fetcher=fetch)


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: f"{p.parent.name}/{p.name}")
async def test_draft_vectors(path):
    vector = json.loads(path.read_text())
    request = vector["request"]
    if "response" in vector:
        response = vector["response"]
        result = verify_directory_response(
            request["url"],
            DirectoryResponse(response["body"].encode(), response["headers"]),
            now=vector["reference_now"],
        )
        assert set(result) <= set(KEYS)
        return

    async def resolve(keyid, *, signer_origin=None):
        ids = vector["directories"].get(signer_origin, {}).get("keys_ref", [])
        public = {k: v for k, v in KEYS[keyid].items() if not k.startswith("_")}
        return ResolvedSignerKey(signer_origin, public) if keyid in ids else None

    replay = InMemoryReplayStore()
    for entry in vector.get("test_harness_state", {}).get("replay_cache_entries", []):
        replay.claim(entry["identity"], entry["nonce"], entry["ttl_seconds"])

    async def verify():
        return await verify_wba_request(
            method=request["method"],
            url=request["url"],
            headers=request["headers"],
            body=request["body"].encode(),
            signer_keys=resolve,
            replay_store=replay,
            revocation_checker=_not_revoked,
            now=vector["reference_now"],
            # The spec answers a reused nonce 429; #7894's older replay vector
            # predates that and expects 403, reproduced by the compat switch.
            replay_status=403 if "replayed-nonce" in path.name else 429,
        )

    expected = vector["expected_outcome"]
    if expected["success"]:
        signers = await verify()
        assert {s.label: s.agent_url for s in signers} == expected["identities"]
    else:
        with pytest.raises(WbaVerificationError) as caught:
            await verify()
        assert caught.value.status == expected["status"]
        if expected["status"] == 403:
            assert "Accept-Signature" in caught.value.headers


def test_signer_reproduces_independent_vector():
    vector = json.loads((ROOT / "positive/001-single-signer-request.json").read_text())
    request = vector["request"]
    key = next(iter(KEYS.values()))
    result = sign_wba_request(
        method=request["method"],
        url=request["url"],
        headers={"Content-Type": "application/json"},
        body=request["body"].encode(),
        private_key=private_key_from_jwk(key, d_field="_private_d_for_test_only"),
        agent_origin=key["identity"],
        created=1790841600,
        nonce="Mn-ZM8SnGESx0-rmyYMuTkJFerfbwEii2C0qNkDP8ugpcuv0pcrt6Da2ifr-6MTmfVeZhnFymLeAofeXJbFaxA",
    )
    assert all(request["headers"][name] == value for name, value in result.items())


@pytest.mark.parametrize(
    "url,expected",
    [
        ("HTTPS://Buyer.Example.COM.:443/mcp/?q=1", "https://buyer.example.com"),
        ("https://bücher.de:8443/a", "https://xn--bcher-kva.de:8443"),
        ("https://[2001:db8::1]:443/mcp", "https://[2001:db8::1]"),
    ],
)
def test_origin_normalization(url, expected):
    assert canonical_origin(url) == expected


@pytest.mark.parametrize(
    "url", ["/mcp", "https://", "ftp://example.com", "https://[fe80::1%eth0]/"]
)
def test_invalid_origin(url):
    with pytest.raises(ValueError):
        canonical_origin(url)


async def test_directory_discovery_binds_origin_and_thumbprint_and_caches():
    key = ed25519.Ed25519PrivateKey.generate()
    now = [1000.0]
    calls = []
    responses = {
        origin: sign_key_directory(agent_origin=origin, private_keys=[key], created=1000)
        for origin in ("https://buyer.example.com", "https://other.example.com")
    }

    async def fetch(url, *, allow_private=False):
        calls.append(url)
        return responses[canonical_origin(url)]

    resolver = DiscoveredSignerKeys(fetcher=fetch, clock=lambda: now[0])
    kid = public_directory_key(key)["kid"]
    first = await resolver(kid, signer_origin="https://buyer.example.com")
    second = await resolver(kid, signer_origin="https://other.example.com")
    assert first.agent_url != second.agent_url
    assert await resolver("unknown", signer_origin=first.agent_url) is None
    assert await resolver(kid) is None
    assert len(calls) == 2
    assert await resolver(kid, signer_origin=first.agent_url) == first
    responses[first.agent_url] = sign_key_directory(
        agent_origin=first.agent_url,
        private_keys=[ed25519.Ed25519PrivateKey.generate()],
        created=1301,
    )
    now[0] = 1301
    assert await resolver(kid, signer_origin=first.agent_url) is None
    assert len(calls) == 3


async def test_directory_outage_cools_down():
    calls = []

    async def fetch(url, *, allow_private=False):
        calls.append(url)
        raise OSError("offline")

    resolver = DiscoveredSignerKeys(fetcher=fetch, clock=lambda: 1000)
    for _ in range(2):
        with pytest.raises(WbaVerificationError):
            await resolver("kid", signer_origin="https://buyer.example.com")
    assert len(calls) == 1


@pytest.mark.parametrize("tamper", ["body", "authority", "future", "unsigned_key", "kid"])
def test_signed_directory_rejects_tampering(tamper):
    origin = "https://buyer.example.com"
    response = sign_key_directory(
        agent_origin=origin, private_keys=[ed25519.Ed25519PrivateKey.generate()], created=1000
    )
    now = 1001
    if tamper == "body":
        response = replace(response, body=response.body + b" ")
    elif tamper == "authority":
        origin = "https://attacker.example.com"
    elif tamper == "future":
        # Directory signatures tolerate 60 s of publisher clock skew only.
        now = 939
    else:
        data = json.loads(response.body)
        if tamper == "kid":
            data["keys"][0]["kid"] = "x" * 43
        else:
            data["keys"].append(public_directory_key(ed25519.Ed25519PrivateKey.generate()))
        response = replace(response, body=json.dumps(data).encode())
    with pytest.raises(WbaVerificationError):
        verify_directory_response(origin, response, now=now)


async def test_replay_is_per_origin_across_key_rotation():
    origin = "https://buyer.example.com"
    keys = [ed25519.Ed25519PrivateKey.generate() for _ in range(2)]
    jwks = {public_directory_key(key)["kid"]: public_directory_key(key) for key in keys}

    async def resolve(keyid, *, signer_origin=None):
        return ResolvedSignerKey(origin, jwks[keyid])

    replay = InMemoryReplayStore()
    for index, key in enumerate(keys):
        headers = {"Content-Type": "application/json"}
        headers.update(
            sign_wba_request(
                method="POST",
                url="https://seller.example.com/mcp",
                headers=headers,
                body=b"{}",
                private_key=key,
                agent_origin=origin,
                created=1000,
                nonce="A" * 86,
            )
        )
        if index == 0:
            assert await verify_wba_request(
                method="POST",
                url="https://seller.example.com/mcp",
                headers=headers,
                body=b"{}",
                signer_keys=resolve,
                replay_store=replay,
                revocation_checker=_not_revoked,
                now=1001,
            )
        else:
            with pytest.raises(WbaVerificationError, match="replayed") as replayed:
                await verify_wba_request(
                    method="POST",
                    url="https://seller.example.com/mcp",
                    headers=headers,
                    body=b"{}",
                    signer_keys=resolve,
                    replay_store=replay,
                    revocation_checker=_not_revoked,
                    now=1001,
                )
            # A reused nonce is 429 with no re-sign challenge.
            assert replayed.value.status == 429
            assert "Accept-Signature" not in replayed.value.headers


async def test_framework_requires_explicit_binding_before_dispatch():
    origin = "https://buyer.example.com"
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    legacy_jwk = {**jwk, "adcp_use": "request-signing", "key_ops": ["verify"]}
    bindings = OriginBindings()
    dispatched = []

    async def app(scope, receive, send):
        context = apply_verified_signer(None, Request(scope))
        try:
            check_wba_authorization(context)
        except AdcpError as exc:
            await JSONResponse(build_mcp_error_result(exc).model_dump(mode="json", by_alias=True))(
                scope, receive, send
            )
            return
        dispatched.append(
            scope["adcp.verified_signer"] if "adcp.verified_signer" in scope else scope
        )
        await JSONResponse({"ok": True})(scope, receive, send)

    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({origin: {"keys": [legacy_jwk]}}),
        enable_web_bot_auth=True,
        origin_bindings=bindings,
        wba_revocation_checker=_not_revoked,
        trust_records=_trust_records(origin),
        signing_profile_version="3.2",
        required_for=frozenset({"sync_accounts"}),
    )
    app = SignedRequestVerificationMiddleware(
        app, config, transport="mcp", defer_authorization_errors=True
    )
    body = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"sync_accounts","arguments":{}}}'
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://seller.example.com"
    ) as client:

        async def post():
            headers = {"Content-Type": "application/json"}
            headers.update(
                sign_wba_request(
                    method="POST",
                    url="https://seller.example.com/mcp",
                    headers=headers,
                    body=body,
                    private_key=private,
                    agent_origin=origin,
                )
            )
            return await client.post("/mcp", content=body, headers=headers)

        denied = await post()
        assert denied.status_code == 200, denied.text
        assert _mcp_adcp_error(denied)["code"] == "PERMISSION_DENIED"
        assert "Accept-Signature" not in denied.headers
        assert not dispatched
        bindings.bind(origin, "principal-1")
        assert (await post()).status_code == 200
        assert len(dispatched) == 1
        legacy = sign_request(
            method="POST",
            url="https://seller.example.com/mcp",
            headers={"Content-Type": "application/json"},
            body=body,
            private_key=private,
            key_id=jwk["kid"],
            alg="ed25519",
            signing_profile_version="3.2",
        )
        response = await client.post(
            "/mcp", content=body, headers={"Content-Type": "application/json", **legacy.as_dict()}
        )
        assert response.status_code == 200, response.text
        assert _mcp_adcp_error(response)["code"] == "PERMISSION_DENIED"
        assert "Accept-Signature" not in response.headers
        assert len(dispatched) == 1


def test_binding_migration_requires_approval_and_never_rebinds():
    bindings = OriginBindings()
    with pytest.raises(ValueError, match="approval"):
        bindings.bind("https://buyer.example.com", "principal-1", existing_rfc9421_principal=True)
    bindings.bind(
        "https://buyer.example.com",
        "principal-1",
        existing_rfc9421_principal=True,
        migration_approved=True,
    )
    with pytest.raises(ValueError, match="rebound"):
        bindings.bind("https://buyer.example.com", "principal-2")


def test_signing_config_requires_thumbprint_and_emits_opt_in_header():
    private = ed25519.Ed25519PrivateKey.generate()
    with pytest.raises(ValueError, match="thumbprint"):
        SigningConfig(
            private_key=private, key_id="legacy-kid", agent_origin="https://buyer.example.com"
        )
    config = SigningConfig(
        private_key=private,
        key_id=public_directory_key(private)["kid"],
        agent_origin="https://Buyer.example.com/mcp",
    )
    result = sign_request(
        method="POST",
        url="https://seller.example.com/mcp",
        headers={"Content-Type": "application/json"},
        body=b"{}",
        private_key=private,
        key_id=config.key_id,
        alg=config.alg,
        signing_profile_version="3.1",
        agent_origin=config.agent_origin,
    )
    assert result.signature_agent == 'sig1="https://buyer.example.com"'
    assert 'tag="web-bot-auth"' in result.signature_input


@pytest.mark.parametrize(
    "field",
    [
        'sig1=("@method");created=1;created=2',
        'sig1=("signature-agent";key="sig1";key="other");created=1',
        'sig1="https://buyer.com";type="directory";type="other"',
    ],
)
def test_duplicate_parameters_are_rejected_before_parsing(field):
    from adcp.signing.wba import _dictionary

    with pytest.raises(WbaVerificationError, match="duplicate"):
        _dictionary(field)


async def test_signer_overwrites_stale_digest_case_insensitively():
    key = ed25519.Ed25519PrivateKey.generate()
    headers = {"content-type": "application/json", "content-digest": "stale"}
    signed = sign_wba_request(
        method="POST",
        url="https://seller.com/mcp",
        headers=headers,
        body=b"{}",
        private_key=key,
        agent_origin="https://buyer.com",
        created=1000,
    )
    merged = {**headers, **{k.lower(): v for k, v in signed.items()}}
    jwk = public_directory_key(key)
    signer = await verify_wba_request(
        method="POST",
        url="https://seller.com/mcp",
        headers=merged,
        body=b"{}",
        signer_keys=StaticSignerKeys({"https://buyer.com": {"keys": [jwk]}}),
        replay_store=InMemoryReplayStore(),
        revocation_checker=_not_revoked,
        now=1001,
    )
    assert signer[0].body_authenticated


async def test_discovery_coalesces_per_origin_without_blocking_other_signers():
    import asyncio

    key = ed25519.Ed25519PrivateKey.generate()
    kid = public_directory_key(key)["kid"]
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def fetch(url, *, allow_private=False):
        origin = canonical_origin(url)
        calls.append(origin)
        if origin == "https://slow.com":
            started.set()
            await release.wait()
        return sign_key_directory(agent_origin=origin, private_keys=[key], created=1000)

    resolver = DiscoveredSignerKeys(fetcher=fetch, clock=lambda: 1000)
    first = asyncio.create_task(resolver(kid, signer_origin="https://slow.com"))
    await started.wait()
    second = asyncio.create_task(resolver(kid, signer_origin="https://slow.com"))
    try:
        fast = await asyncio.wait_for(resolver(kid, signer_origin="https://fast.com"), timeout=1)
        assert fast.agent_url == "https://fast.com"
    finally:
        release.set()
    assert await first == await second
    assert calls.count("https://slow.com") == 1


async def test_wba_discovery_never_falls_back_to_legacy_keys():
    key = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(key)
    legacy = StaticSignerKeys({"https://legacy.com/mcp": {"keys": [jwk]}})

    async def fetch(url, *, allow_private=False):
        raise OSError("offline")

    resolver = DiscoveredSignerKeys(fetcher=fetch, legacy_resolver=legacy)
    assert (await resolver(jwk["kid"])).agent_url == "https://legacy.com/mcp"
    with pytest.raises(WbaVerificationError):
        await resolver(jwk["kid"], signer_origin="https://legacy.com")


async def test_p256_wba_derives_optional_algorithm_and_signs_multikey_directory():
    from cryptography.hazmat.primitives.asymmetric import ec

    from adcp.signing.crypto import format_signature_header, sign_signature_base
    from adcp.signing.wba import _base, _dictionary

    ed_key, ec_key = ed25519.Ed25519PrivateKey.generate(), ec.generate_private_key(ec.SECP256R1())
    response = sign_key_directory(
        agent_origin="https://buyer.com", private_keys=[ed_key, ec_key], created=1000
    )
    keys = verify_directory_response("https://buyer.com", response, now=1001)
    assert len(keys) == 2
    headers = {"Content-Type": "application/json"}
    headers.update(
        sign_wba_request(
            method="POST",
            url="https://seller.com/mcp",
            headers=headers,
            body=b"{}",
            private_key=ec_key,
            agent_origin="https://buyer.com",
            created=1000,
        )
    )
    inputs = _dictionary(headers["Signature-Input"])
    member = inputs["sig1"]
    del member.params["alg"]
    headers["Signature-Input"] = str(inputs)
    signature = sign_signature_base(
        alg="ecdsa-p256-sha256",
        private_key=ec_key,
        signature_base=_base("POST", "https://seller.com/mcp", headers, member),
    )
    headers["Signature"] = format_signature_header(
        signature, label="sig1", use_legacy_base64url=False
    )
    result = await verify_wba_request(
        method="POST",
        url="https://seller.com/mcp",
        headers=headers,
        body=b"{}",
        signer_keys=StaticSignerKeys({"https://buyer.com": {"keys": list(keys.values())}}),
        replay_store=InMemoryReplayStore(),
        revocation_checker=_not_revoked,
        now=1001,
    )
    assert result[0].alg == "ecdsa-p256-sha256"


@pytest.mark.parametrize(
    "headers",
    [
        {"Signature-Agent": 'sig1="https://buyer.com"'},
        {"Signature-Input": 'sig1=("@method");tag="web-bot-auth"'},
    ],
)
def test_legacy_verifier_never_attributes_or_emits_legacy_codes_for_wba(headers):
    from unittest.mock import Mock

    from adcp.signing import VerifierCapability, VerifyOptions, verify_request_signature

    resolver = Mock()
    with pytest.raises(WbaVerificationError) as caught:
        verify_request_signature(
            method="POST",
            url="https://seller.com/mcp",
            headers=headers,
            body=b"",
            options=VerifyOptions(
                now=1000,
                capability=VerifierCapability(),
                operation="get_products",
                jwks_resolver=resolver,
            ),
        )
    assert caught.value.status == 403
    assert "Accept-Signature" in caught.value.headers
    resolver.assert_not_called()


@pytest.mark.parametrize("cover_extra", [False, True])
async def test_relay_covers_every_component_of_earlier_signature(cover_extra):
    from http_sfv.item import Item

    from adcp.signing.crypto import sign_signature_base
    from adcp.signing.wba import _base, _dictionary

    vector = json.loads((ROOT / "positive/002-relay-request-two-signatures.json").read_text())
    request = vector["request"]
    headers = request["headers"]
    headers["X-Buyer-Reference"] = "campaign-123"
    inputs = _dictionary(headers["Signature-Input"])
    signatures = _dictionary(headers["Signature"])
    labels = list(inputs)
    inputs[labels[0]].append(Item("x-buyer-reference"))
    if cover_extra:
        inputs[labels[1]].append(Item("x-buyer-reference"))
    headers["Signature-Input"] = str(inputs)
    for label, member in inputs.items():
        key = KEYS[member.params["keyid"]]
        signatures[label] = Item(
            sign_signature_base(
                alg="ed25519",
                private_key=private_key_from_jwk(key, d_field="_private_d_for_test_only"),
                signature_base=_base(request["method"], request["url"], headers, member),
            )
        )
        headers["Signature"] = str(signatures)

    async def resolve(keyid, *, signer_origin=None):
        key = KEYS[keyid]
        return ResolvedSignerKey(signer_origin, key) if key["identity"] == signer_origin else None

    async def verify():
        return await verify_wba_request(
            method=request["method"],
            url=request["url"],
            headers=headers,
            body=request["body"].encode(),
            signer_keys=resolve,
            replay_store=InMemoryReplayStore(),
            revocation_checker=_not_revoked,
            now=vector["reference_now"],
        )

    if cover_extra:
        assert len(await verify()) == 2
    else:
        with pytest.raises(WbaVerificationError, match="covered component"):
            await verify()


@pytest.mark.parametrize(
    ("revocation_state", "operation"),
    [
        ("clear", "sync_accounts"),
        ("revoked", "sync_accounts"),
        ("unavailable", "create_media_buy"),
        ("unavailable", "get_media_buys"),
    ],
)
async def test_framework_checks_origin_revocations_by_default(
    monkeypatch, revocation_state, operation
):
    from datetime import datetime, timedelta, timezone

    from adcp.signing import OriginRevocationChecker
    from adcp.signing.crypto import b64url_encode
    from adcp.signing.revocation_fetcher import FetchResult

    origin = "https://buyer.example.com"
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    now = datetime.now(timezone.utc)
    payload = {
        "version": 1,
        "issuer": origin,
        "updated": now.isoformat(),
        "next_update": (now + timedelta(minutes=5)).isoformat(),
        "revoked_kids": [jwk["kid"]] if revocation_state == "revoked" else [],
        "revoked_jtis": [],
    }
    protected = b64url_encode(
        json.dumps({"alg": "EdDSA", "kid": jwk["kid"], "typ": "adcp-gov-revocation+jws"}).encode()
    )
    encoded = b64url_encode(json.dumps(payload).encode())
    signature = b64url_encode(private.sign(f"{protected}.{encoded}".encode()))
    calls, dispatched = [], []

    async def fetch(uri, **kwargs):
        calls.append(uri)
        if revocation_state == "unavailable":
            raise OSError("offline")
        return FetchResult(f"{protected}.{encoded}.{signature}", etag=None)

    monkeypatch.setattr(
        "adcp.signing.revocation_fetcher.async_default_revocation_list_fetcher", fetch
    )
    bindings = OriginBindings()
    bindings.bind(origin, "principal-1")
    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({origin: {"keys": [jwk]}}),
        enable_web_bot_auth=True,
        origin_bindings=bindings,
        trust_records=_trust_records(origin),
    )
    assert isinstance(config.wba_revocation_checker, OriginRevocationChecker)

    async def dispatch(scope, receive, send):
        context = apply_verified_signer(None, Request(scope))
        try:
            check_wba_authorization(context)
        except AdcpError as exc:
            await JSONResponse(build_mcp_error_result(exc).model_dump(mode="json", by_alias=True))(
                scope, receive, send
            )
            return
        dispatched.append(True)
        await JSONResponse({"ok": True})(scope, receive, send)

    app = SignedRequestVerificationMiddleware(
        dispatch, config, transport="mcp", defer_authorization_errors=True
    )
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": operation, "arguments": {}},
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(
        sign_wba_request(
            method="POST",
            url="https://seller.example.com/mcp",
            headers=headers,
            body=body,
            private_key=private,
            agent_origin=origin,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://seller.example.com"
    ) as client:
        response = await client.post("/mcp", content=body, headers=headers)
    assert calls == [origin + "/.well-known/governance-revocations.json"]
    if revocation_state == "revoked":
        # A revoked key: 403, the generic authentication error, and no
        # Accept-Signature, since signing again with the same key is wrong.
        assert response.status_code == 403, response.text
        assert response.json()["errors"][0]["code"] == "AUTH_INVALID"
        assert "Accept-Signature" not in response.headers
        assert not dispatched
    elif revocation_state == "unavailable" and operation == "create_media_buy":
        # A stale list fails spend closed in the authorization layer.
        assert response.status_code == 200, response.text
        error = _mcp_adcp_error(response)
        assert error["code"] == "SERVICE_UNAVAILABLE"
        assert error["recovery"] == "transient"
        assert "Accept-Signature" not in response.headers
        assert not dispatched
    else:
        # A current list, or a stale one for a non-spend call, proceeds.
        assert response.status_code == 200, response.text
        assert dispatched == [True]
