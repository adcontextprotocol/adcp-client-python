"""End-to-end smoke tests for CachingRevocationChecker.

Spins up a minimal Starlette app that serves a signed revocation-list JWS
at ``/.well-known/governance-revocations.json``. Wires the checker at it
through a custom fetcher that uses ``httpx.ASGITransport`` — no real
network, no monkey-patching. Proves the checker → HTTP → JWS verify →
decide pipeline works on real bytes the server-side verifier would emit,
and that the checker plugs into the existing
:class:`VerifyOptions.revocation_checker` kwarg.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from adcp.signing import (
    REVOCATION_LIST_TYP,
    CachingRevocationChecker,
    FetchResult,
    SignatureVerificationError,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    default_revocation_list_fetcher,  # noqa: F401 — imported for coverage of __init__
    sign_request,
    verify_request_signature,
)
from adcp.signing.crypto import ALG_ED25519, b64url_encode, sign_signature_base

ISSUER = "https://gov.example.com"
REVOCATION_URI = f"{ISSUER}/.well-known/governance-revocations.json"


# -- helpers ------------------------------------------------------------


def _make_operator_key() -> tuple[ed25519.Ed25519PrivateKey, dict[str, Any]]:
    """Generate the operator's key pair used to sign the revocation list."""
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "key_ops": ["verify"],
        "kid": "operator-2026",
        "x": b64url_encode(private.public_key().public_bytes_raw()),
    }
    return private, jwk


def _make_signer_key() -> tuple[ed25519.Ed25519PrivateKey, dict[str, Any]]:
    """Generate a request-signing key used to sign outgoing AdCP requests."""
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "key_ops": ["verify"],
        "adcp_use": "request-signing",
        "kid": "buyer-key-1",
        "x": b64url_encode(private.public_key().public_bytes_raw()),
    }
    return private, jwk


def _sign_revocation_list(
    *,
    operator_key: ed25519.Ed25519PrivateKey,
    operator_kid: str,
    payload: dict[str, Any],
) -> str:
    header = {"alg": "EdDSA", "kid": operator_kid, "typ": REVOCATION_LIST_TYP}
    b64_header = b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    b64_payload = b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = (b64_header + "." + b64_payload).encode("ascii")
    signature = sign_signature_base(
        alg=ALG_ED25519, private_key=operator_key, signature_base=signing_input
    )
    return b64_header + "." + b64_payload + "." + b64url_encode(signature)


def _build_revocation_app(*, body: str, etag: str) -> Starlette:
    """Starlette app that serves the list with ETag + If-None-Match support."""

    async def handler(request: Any) -> Response:
        if_none_match = request.headers.get("if-none-match")
        if if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return PlainTextResponse(
            content=body,
            media_type="application/jose",
            headers={"ETag": etag},
        )

    return Starlette(
        routes=[Route("/.well-known/governance-revocations.json", handler, methods=["GET"])]
    )


def _asgi_fetcher(app: Starlette) -> Any:
    """Wrap the default fetcher pattern over httpx.ASGITransport.

    ``ASGITransport`` only supports ``httpx.AsyncClient``, but
    :class:`RevocationListFetcher` is sync — so the fetcher bridges by
    running the async HTTP call inside ``asyncio.run``.
    """
    import asyncio

    transport = httpx.ASGITransport(app=app)

    def fetch(
        uri: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        headers = {"Accept": "application/jose"}
        if if_none_match is not None:
            headers["If-None-Match"] = if_none_match
        if if_modified_since is not None:
            headers["If-Modified-Since"] = if_modified_since

        async def _do_fetch() -> httpx.Response:
            async with httpx.AsyncClient(transport=transport, base_url=ISSUER) as client:
                return await client.get("/.well-known/governance-revocations.json", headers=headers)

        response = asyncio.run(_do_fetch())

        if response.status_code == 304:
            return FetchResult(
                body="",
                etag=if_none_match,
                last_modified=if_modified_since,
                not_modified=True,
            )
        response.raise_for_status()
        return FetchResult(
            body=response.text,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            not_modified=False,
        )

    return fetch


def _jwks_resolver_for(jwk: dict[str, Any]) -> Any:
    def resolve(keyid: str) -> dict[str, Any] | None:
        return jwk if keyid == jwk["kid"] else None

    return resolve


# -- tests --------------------------------------------------------------


def test_checker_fetches_via_asgi_and_verifies_jws() -> None:
    operator_priv, operator_jwk = _make_operator_key()
    payload = {
        "version": 1,
        "issuer": ISSUER,
        "updated": "2026-04-18T14:00:00Z",
        "next_update": "2026-04-18T14:15:00Z",
        "revoked_kids": ["compromised-buyer-key"],
        "revoked_jtis": [],
    }
    compact_jws = _sign_revocation_list(
        operator_key=operator_priv,
        operator_kid=operator_jwk["kid"],
        payload=payload,
    )
    app = _build_revocation_app(body=compact_jws, etag='"rev-1"')

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=_jwks_resolver_for(operator_jwk),
        fetcher=_asgi_fetcher(app),
        wall_clock=lambda: datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc),
    )

    assert checker("compromised-buyer-key") is True
    assert checker("innocent-buyer-key") is False


def test_checker_honors_server_etag_via_asgi() -> None:
    operator_priv, operator_jwk = _make_operator_key()
    payload = {
        "version": 1,
        "issuer": ISSUER,
        "updated": "2026-04-18T14:00:00Z",
        "next_update": "2026-04-18T14:15:00Z",
        "revoked_kids": [],
        "revoked_jtis": [],
    }
    compact_jws = _sign_revocation_list(
        operator_key=operator_priv,
        operator_kid=operator_jwk["kid"],
        payload=payload,
    )
    app = _build_revocation_app(body=compact_jws, etag='"rev-42"')

    now = [datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)]
    mono = [0.0]

    def wall_clock() -> datetime:
        return now[0]

    def mono_clock() -> float:
        return mono[0]

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=_jwks_resolver_for(operator_jwk),
        fetcher=_asgi_fetcher(app),
        wall_clock=wall_clock,
        clock=mono_clock,
    )

    assert checker("any-key") is False  # 1st fetch: 200
    # Jump past next_update so the checker issues a conditional refresh,
    # and the server responds 304 because our ETag matches.
    now[0] = datetime(2026, 4, 18, 14, 16, tzinfo=timezone.utc)
    mono[0] = 900.0  # satisfy the 60s refresh cooldown
    assert checker("any-key") is False  # still cached via 304 path


def test_checker_plugs_into_verify_request_signature_pipeline() -> None:
    """Full pipeline: sign a request, then verify using the live checker.

    The key signing the outgoing request is also listed in the live
    revocation list → verifier must reject at step 9 (revocation check)
    before crypto verify.
    """
    operator_priv, operator_jwk = _make_operator_key()
    buyer_priv, buyer_jwk = _make_signer_key()

    payload = {
        "version": 1,
        "issuer": ISSUER,
        "updated": "2026-04-18T14:00:00Z",
        "next_update": "2026-04-18T14:15:00Z",
        "revoked_kids": [buyer_jwk["kid"]],
        "revoked_jtis": [],
    }
    compact_jws = _sign_revocation_list(
        operator_key=operator_priv,
        operator_kid=operator_jwk["kid"],
        payload=payload,
    )
    app = _build_revocation_app(body=compact_jws, etag='"rev-revoked"')

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=_jwks_resolver_for(operator_jwk),
        fetcher=_asgi_fetcher(app),
        wall_clock=lambda: datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc),
    )

    # Sign a request with the revoked buyer key.
    body = b'{"plan_id":"p1"}'
    url = "https://seller.example.com/adcp/create_media_buy"
    signed = sign_request(
        method="POST",
        url=url,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=buyer_priv,
        key_id=buyer_jwk["kid"],
        alg=ALG_ED25519,
        signing_profile_version="3.2",
    )
    request_headers = {"Content-Type": "application/json", **signed.as_dict()}

    # Verify. Checker fetches the live list, sees buyer_jwk["kid"] in
    # revoked_kids, and the verifier raises request_signature_key_revoked.
    options = VerifyOptions(
        now=float(int(time.time())),
        capability=VerifierCapability(
            covers_content_digest="either",
            required_for=frozenset({"create_media_buy"}),
        ),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [buyer_jwk]}),
        revocation_checker=checker,
    )
    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_request_signature(
            method="POST",
            url=url,
            headers=request_headers,
            body=body,
            options=options,
        )
    assert exc_info.value.code == "request_signature_key_revoked"


def test_checker_verifier_accepts_when_kid_not_in_revocation_list() -> None:
    """Inverse of the above: clean buyer key → revocation check passes, crypto verifies."""
    operator_priv, operator_jwk = _make_operator_key()
    buyer_priv, buyer_jwk = _make_signer_key()

    payload = {
        "version": 1,
        "issuer": ISSUER,
        "updated": "2026-04-18T14:00:00Z",
        "next_update": "2026-04-18T14:15:00Z",
        "revoked_kids": ["some-other-key"],
        "revoked_jtis": [],
    }
    compact_jws = _sign_revocation_list(
        operator_key=operator_priv,
        operator_kid=operator_jwk["kid"],
        payload=payload,
    )
    app = _build_revocation_app(body=compact_jws, etag='"rev-clean"')

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=_jwks_resolver_for(operator_jwk),
        fetcher=_asgi_fetcher(app),
        wall_clock=lambda: datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc),
    )

    body = b'{"plan_id":"p1"}'
    url = "https://seller.example.com/adcp/create_media_buy"
    signed = sign_request(
        method="POST",
        url=url,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=buyer_priv,
        key_id=buyer_jwk["kid"],
        alg=ALG_ED25519,
        signing_profile_version="3.2",
    )
    request_headers = {"Content-Type": "application/json", **signed.as_dict()}

    options = VerifyOptions(
        now=float(int(time.time())),
        capability=VerifierCapability(
            covers_content_digest="either",
            required_for=frozenset({"create_media_buy"}),
        ),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [buyer_jwk]}),
        revocation_checker=checker,
    )
    verified = verify_request_signature(
        method="POST",
        url=url,
        headers=request_headers,
        body=body,
        options=options,
    )
    assert verified.key_id == buyer_jwk["kid"]


def test_stale_list_past_grace_surfaces_revocation_stale() -> None:
    """Once the cached list is past next_update + grace and refresh fails,
    the checker raises RevocationListFreshnessError — and when wired into
    the verifier, the caller sees it at step 9 of the pipeline."""
    from adcp.signing.revocation_fetcher import RevocationListFreshnessError

    operator_priv, operator_jwk = _make_operator_key()
    payload = {
        "version": 1,
        "issuer": ISSUER,
        "updated": "2026-04-18T14:00:00Z",
        "next_update": "2026-04-18T14:15:00Z",
        "revoked_kids": [],
        "revoked_jtis": [],
    }
    compact_jws = _sign_revocation_list(
        operator_key=operator_priv,
        operator_kid=operator_jwk["kid"],
        payload=payload,
    )

    # First fetch succeeds; subsequent fetches fail (simulate operator outage).
    call_count = [0]

    def failing_fetcher(
        uri: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        call_count[0] += 1
        if call_count[0] == 1:
            return FetchResult(body=compact_jws, etag='"initial"', not_modified=False)
        from adcp.signing import RevocationListFetchError

        raise RevocationListFetchError("operator unreachable")

    now = [datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)]
    mono = [0.0]

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=_jwks_resolver_for(operator_jwk),
        fetcher=failing_fetcher,
        wall_clock=lambda: now[0],
        clock=lambda: mono[0],
    )

    # First call: initial fetch succeeds.
    assert checker("any") is False

    # Past next_update + grace (interval 15min × 2 = 30min grace → 45min past updated).
    now[0] = datetime(2026, 4, 18, 14, 46, tzinfo=timezone.utc)
    mono[0] = 2700.0  # past the 60s cooldown

    with pytest.raises(RevocationListFreshnessError):
        checker("any")
