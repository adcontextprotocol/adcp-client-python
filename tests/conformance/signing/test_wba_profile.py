"""WBA profile vectors (adcp#7894) and the security boundaries of the experimental profile."""

from __future__ import annotations

import hashlib
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
GOVERNANCE_VECTORS = sorted((ROOT / "governance").glob("*.json"))


def _public(kid):
    return {k: v for k, v in KEYS[kid].items() if not k.startswith("_")}


def _vector_resolver(vector):
    async def resolve(keyid, *, signer_origin=None):
        ids = vector["directories"].get(signer_origin, {}).get("keys_ref", [])
        return ResolvedSignerKey(signer_origin, _public(keyid)) if keyid in ids else None

    return resolve


def test_vendored_vectors_match_recorded_provenance():
    upstream = json.loads((ROOT / "UPSTREAM.json").read_text())
    assert upstream["commit"] == "e24eeb090c4365707641099cbbb45f9945c6d6e0"
    vendored = {
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.is_file() and path.name not in {"README.md", "UPSTREAM.json"}
    }
    assert vendored == set(upstream["sha256"])
    for name, digest in upstream["sha256"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


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
async def test_profile_vectors(path):
    vector = json.loads(path.read_text())
    request = vector["request"]
    if "response" in vector:
        response = vector["response"]
        origin = request["url"].split("/.well-known/")[0]
        result = verify_directory_response(
            origin,
            DirectoryResponse(response["body"].encode(), response["headers"]),
            now=vector["reference_now"],
        )
        assert set(result) <= set(KEYS)
        assert set(vector["expected_outcome"]["identities"].values()) == {origin}
        return

    replay = InMemoryReplayStore()
    for entry in vector.get("test_harness_state", {}).get("replay_cache_entries", []):
        replay.claim(entry["identity"], entry["nonce"], entry["ttl_seconds"])

    async def verify():
        return await verify_wba_request(
            method=request["method"],
            url=request["url"],
            headers=request["headers"],
            body=request["body"].encode(),
            signer_keys=_vector_resolver(vector),
            replay_store=replay,
            revocation_checker=_not_revoked,
            now=vector["reference_now"],
        )

    expected = vector["expected_outcome"]
    if expected["success"]:
        signers = await verify()
        assert {s.label: s.agent_url for s in signers} == expected["identities"]
        return
    with pytest.raises(WbaVerificationError) as caught:
        await verify()
    assert caught.value.status == expected["status"]
    challenge = expected.get("accept_signature")
    if challenge is None:
        assert "Accept-Signature" not in caught.value.headers
        return
    # The Accept-Signature member names the label, components and parameters.
    from adcp.signing.wba import _dictionary

    member = _dictionary(caught.value.headers["Accept-Signature"])[challenge["label"]]
    components, params = member
    assert "(" + " ".join(_identifier(item) for item in components) + ")" == challenge["components"]
    assert list(params) == challenge["parameters"]
    assert params["tag"] == challenge["tag"]
    if challenge.get("nonce") == "verifier-chosen":
        assert isinstance(params["nonce"], str) and len(params["nonce"]) == 86
    else:
        assert params["nonce"] is True


def _identifier(item):
    from adcp.signing._rfc9421 import component_identifier

    return component_identifier(item)


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_profile_vector_signature_bases(path):
    """Our signature base matches every vector's, built by http-message-signatures."""
    from adcp.signing.wba import _signature_base

    vector = json.loads(path.read_text())
    message = vector.get("response", vector["request"])
    for label, expected in vector.get("expected_signature_base", {}).items():
        base = _signature_base(
            label,
            vector["request"]["method"],
            vector["request"]["url"],
            message["headers"],
            response="response" in vector,
        )
        assert base.decode() == expected


@pytest.mark.parametrize("path", GOVERNANCE_VECTORS, ids=lambda p: p.name)
async def test_governance_vectors(path):
    from adcp.signing.wba import WbaGovernanceError, verify_wba_governance_issuer

    vector = json.loads(path.read_text())

    async def verify():
        return await verify_wba_governance_issuer(
            vector["jws"],
            signer_keys=_vector_resolver(vector),
            governance_agents=vector["role_listing"]["adcp:governance"],
            now=vector["reference_now"],
        )

    expected = vector["expected_outcome"]
    if expected["success"]:
        assert await verify() == expected["issuer"]
        return
    with pytest.raises(WbaGovernanceError) as caught:
        await verify()
    assert caught.value.code == expected["error_code"]


def test_signer_reproduces_independent_vector():
    """Signing the committed base with the committed seed gives the committed signature."""
    from adcp.signing._rfc9421 import parse_dictionary
    from adcp.signing.crypto import sign_signature_base

    vector = json.loads((ROOT / "positive/001-single-signer-request.json").read_text())
    request = vector["request"]
    key = KEYS["6WsKMCObba49V3uN0vIENnk1BKAk7c9zjBJcWKyQ9_I"]
    private = private_key_from_jwk(key, d_field="_private_d_for_test_only")
    signature = sign_signature_base(
        alg="ed25519",
        private_key=private,
        signature_base=vector["expected_signature_base"]["sig1"].encode(),
    )
    assert parse_dictionary(request["headers"]["Signature"])["sig1"][0] == signature
    # sign_wba_request produces the same covered components, Signature-Agent
    # and Content-Digest; the library orders the parameters its own way, so
    # its base differs from the vector's only in @signature-params.
    result = sign_wba_request(
        method=request["method"],
        url=request["url"],
        headers={"Content-Type": "application/json"},
        body=request["body"].encode(),
        private_key=private,
        agent_origin=key["identity"],
        created=1790841600,
        nonce="Mn-ZM8SnGESx0-rmyYMuTkJFerfbwEii2C0qNkDP8ugpcuv0pcrt6Da2ifr-6MTmfVeZhnFymLeAofeXJbFaxA",
    )
    for name in ("Signature-Agent", "Content-Digest"):
        assert result[name] == request["headers"][name]
    ours = parse_dictionary(result["Signature-Input"])["sig1"]
    theirs = parse_dictionary(request["headers"]["Signature-Input"])["sig1"]
    assert ours[0] == theirs[0] and ours[1] == theirs[1]


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
            # A reused nonce is 429 with Accept-Signature carrying a fresh
            # nonce (wba-profile.mdx, Error responses).
            assert replayed.value.status == 429
            assert 'nonce="' in replayed.value.headers["Accept-Signature"]


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


async def test_es256_is_not_a_web_bot_auth_algorithm():
    """The profile allows ed25519 and rsa-pss-sha512; ES256 stays on RFC 9421."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from adcp.signing.crypto import b64url_encode

    ed_key, ec_key = ed25519.Ed25519PrivateKey.generate(), ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ValueError, match="Ed25519 and RSA"):
        sign_key_directory(agent_origin="https://buyer.com", private_keys=[ec_key], created=1000)
    with pytest.raises(ValueError, match="Ed25519 and RSA"):
        sign_wba_request(
            method="POST",
            url="https://seller.com/mcp",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            private_key=ec_key,
            agent_origin="https://buyer.com",
            created=1000,
        )
    # A directory may list a P-256 key for another use; it is skipped, and the
    # signed Ed25519 key is the only one returned.
    response = sign_key_directory(
        agent_origin="https://buyer.com", private_keys=[ed_key], created=1000
    )
    numbers = ec_key.public_key().public_numbers()
    p256 = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url_encode(numbers.x.to_bytes(32, "big")),
        "y": b64url_encode(numbers.y.to_bytes(32, "big")),
    }
    with pytest.raises(WbaVerificationError, match="unsupported directory key"):
        from adcp.signing.wba import _algorithm

        _algorithm(p256)
    keys = verify_directory_response("https://buyer.com", response, now=1001)
    assert list(keys) == [public_directory_key(ed_key)["kid"]]


async def test_unsigned_directory_is_refused():
    """Signed directory responses are required (WBA Appendix B.1)."""
    key = ed25519.Ed25519PrivateKey.generate()
    signed = sign_key_directory(agent_origin="https://buyer.com", private_keys=[key], created=1000)
    unsigned = DirectoryResponse(
        signed.body,
        {k: v for k, v in signed.headers.items() if k not in {"Signature", "Signature-Input"}},
    )
    with pytest.raises(WbaVerificationError, match="not signed"):
        verify_directory_response("https://buyer.com", unsigned, now=1001)

    async def fetch(url, *, allow_private=False):
        return unsigned

    with pytest.raises(WbaVerificationError):
        await DiscoveredSignerKeys(fetcher=fetch, clock=lambda: 1001)(
            public_directory_key(key)["kid"], signer_origin="https://buyer.com"
        )
    with pytest.raises(TypeError):
        DiscoveredSignerKeys(require_signed_directories=True)  # type: ignore[call-arg]


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


@pytest.mark.parametrize("change", ["second_agent_member", "short_nonce", "padded_nonce"])
async def test_one_signer_and_64_byte_nonce(change):
    """One Signature-Agent member per request, and a nonce of exactly 64 bytes."""
    import base64

    vector = json.loads((ROOT / "positive/001-single-signer-request.json").read_text())
    request = vector["request"]
    key = KEYS["6WsKMCObba49V3uN0vIENnk1BKAk7c9zjBJcWKyQ9_I"]
    private = private_key_from_jwk(key, d_field="_private_d_for_test_only")
    if change == "short_nonce":
        with pytest.raises(ValueError, match="64 bytes"):
            sign_wba_request(
                method="POST",
                url=request["url"],
                headers={"Content-Type": "application/json"},
                body=request["body"].encode(),
                private_key=private,
                agent_origin=key["identity"],
                created=1790841600,
                nonce="A" * 22,
            )
        return
    nonce = (
        base64.b64encode(bytes(range(64))).decode()
        if change == "padded_nonce"
        else base64.urlsafe_b64encode(b"\x07" * 64).decode().rstrip("=")
    )
    headers = {"Content-Type": "application/json"}
    headers.update(
        sign_wba_request(
            method="POST",
            url=request["url"],
            headers=headers,
            body=request["body"].encode(),
            private_key=private,
            agent_origin=key["identity"],
            created=1790841600,
            nonce=nonce,
        )
    )
    if change == "second_agent_member":
        headers["Signature-Agent"] += ', sig2="https://agent.brand-7k3q.com"'

    async def verify():
        return await verify_wba_request(
            method="POST",
            url=request["url"],
            headers=headers,
            body=request["body"].encode(),
            signer_keys=_vector_resolver(vector),
            replay_store=InMemoryReplayStore(),
            now=vector["reference_now"],
        )

    if change == "padded_nonce":
        # Cloudflare's signer sends padded standard base64 of 64 bytes.
        (signer,) = await verify()
        assert signer.agent_url == key["identity"]
        return
    with pytest.raises(WbaVerificationError, match="one web-bot-auth signature") as caught:
        await verify()
    assert caught.value.status == 403
    assert "Accept-Signature" in caught.value.headers


async def test_short_received_nonce_is_a_challenge():
    """A nonce shorter than 64 bytes fails the profile: 403 with Accept-Signature."""
    from adcp.signing import _rfc9421
    from adcp.signing.wba import _WbaComponentResolver

    key = KEYS["6WsKMCObba49V3uN0vIENnk1BKAk7c9zjBJcWKyQ9_I"]
    private = private_key_from_jwk(key, d_field="_private_d_for_test_only")
    vector = json.loads((ROOT / "positive/001-single-signer-request.json").read_text())
    request = vector["request"]
    headers = {k: v for k, v in request["headers"].items() if not k.startswith("Signature-I")}
    headers = {k: v for k, v in headers.items() if k != "Signature"}
    signature_input, signature = _rfc9421.sign(
        _rfc9421.Request(request["method"], request["url"], dict(headers)),
        alg="ed25519",
        private_key=private,
        key_id=key["kid"],
        label="sig1",
        created=1790841600,
        expires=1790841900,
        nonce="A" * 22,
        tag="web-bot-auth",
        covered_component_ids=[
            '"@method"',
            '"@target-uri"',
            '"@authority"',
            '"content-type"',
            '"content-digest"',
            '"signature-agent";key="sig1"',
        ],
        resolver=_WbaComponentResolver,
    )
    headers.update({"Signature-Input": signature_input, "Signature": signature})
    with pytest.raises(WbaVerificationError, match="nonce") as caught:
        await verify_wba_request(
            method=request["method"],
            url=request["url"],
            headers=headers,
            body=request["body"].encode(),
            signer_keys=_vector_resolver(vector),
            replay_store=InMemoryReplayStore(),
            now=vector["reference_now"],
        )
    assert caught.value.status == 403
    assert "Accept-Signature" in caught.value.headers


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


def _governance_token(key, header_extra=None, claims_extra=None, *, iss=None):
    from joserfc import jws as jose_jws
    from joserfc.jwk import OKPKey

    from adcp.signing.wba import _governance_registry

    identity = iss or KEYS["1XlJ3EIte7bPOEqKQtKrKNTNy5hywTyrwFXWrMbXKqE"]["identity"]
    header = {
        "alg": "EdDSA",
        "typ": "adcp-gov+jws",
        "jku": identity + "/.well-known/http-message-signatures-directory",
        "kid": "1XlJ3EIte7bPOEqKQtKrKNTNy5hywTyrwFXWrMbXKqE",
        **(header_extra or {}),
    }
    claims = {"iss": identity, "aud": "https://seller-7k3q.com/mcp/", "iat": 1000, "exp": 1600}
    claims.update(claims_extra or {})
    private = private_key_from_jwk(
        KEYS["1XlJ3EIte7bPOEqKQtKrKNTNy5hywTyrwFXWrMbXKqE"], d_field="_private_d_for_test_only"
    )
    return jose_jws.serialize_compact(
        header,
        json.dumps(claims).encode(),
        OKPKey.import_key(private),
        registry=_governance_registry("EdDSA"),
    )


@pytest.mark.parametrize("markers", ["matched", "claim_without_marker", "task_without_hash"])
async def test_governance_action_binding_markers(markers):
    """Action-authorizing tokens carry crit markers (security.mdx checklist step 4)."""
    from adcp.signing.wba import WbaGovernanceError, verify_wba_governance_issuer

    vector = json.loads((ROOT / "governance/001-governance-token.json").read_text())
    binding = {"authorized_task": "create_media_buy", "authorized_payload_hash": "h"}
    header = {"crit": list(binding), **{name: True for name in binding}}
    if markers == "claim_without_marker":
        header = {}
    claims = dict(binding)
    if markers == "task_without_hash":
        header = {"crit": ["authorized_task"], "authorized_task": True}
        claims = {"authorized_task": "create_media_buy"}
    token = _governance_token(None, header, claims)

    async def verify():
        return await verify_wba_governance_issuer(
            token,
            signer_keys=_vector_resolver(vector),
            governance_agents=vector["role_listing"]["adcp:governance"],
            now=1100,
        )

    if markers == "matched":
        assert await verify() == "https://governance-7k3q.com"
        return
    with pytest.raises(WbaGovernanceError) as caught:
        await verify()
    assert caught.value.code == "governance_token_invalid"


async def test_governance_issuer_is_checked_before_key_discovery():
    from adcp.signing.wba import WbaGovernanceError, verify_wba_governance_issuer

    token = _governance_token(None, iss="https://buyer-7k3q.com")

    async def resolve(keyid, *, signer_origin=None):
        pytest.fail("an unlisted issuer must not trigger key discovery")

    with pytest.raises(WbaGovernanceError) as caught:
        await verify_wba_governance_issuer(
            token,
            signer_keys=resolve,
            governance_agents=["https://governance-7k3q.com"],
            now=1100,
        )
    assert caught.value.code == "governance_issuer_not_authorized"
