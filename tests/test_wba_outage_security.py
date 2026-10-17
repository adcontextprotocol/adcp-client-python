"""Signer evidence outages are discovery failures, never a 503 on the signature path."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from adcp.signing.crypto import b64url_encode
from adcp.signing.revocation_fetcher import (
    AsyncCachingRevocationChecker,
    CachingRevocationChecker,
    FetchResult,
    RevocationListFetchError,
    RevocationListFreshnessError,
)
from adcp.signing.signer_keys import ResolvedSignerKey
from adcp.signing.wba import (
    DirectoryResponse,
    DiscoveredSignerKeys,
    OriginRevocationChecker,
    WbaDiscoveryError,
    WbaVerificationError,
    fetch_key_directory,
    public_directory_key,
    sign_key_directory,
)

ORIGIN = "https://buyer.platform.com"


@pytest.mark.parametrize("status", [302, 401, 500, 404, 410])
async def test_directory_fetch_preserves_error_status_without_reading_body(monkeypatch, status):
    reads = []

    class UnexpectedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            yield b"unexpected directory error body"

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status, headers={"Content-Encoding": "gzip"}, stream=UnexpectedBody()
        )
    )
    monkeypatch.setattr(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        lambda *args, **kwargs: transport,
    )
    response = await fetch_key_directory(ORIGIN + "/.well-known/http-message-signatures-directory")
    assert response.status == status
    assert response.body == b""
    assert not reads


@pytest.mark.parametrize(
    "outcome",
    [
        "timeout",
        "offline",
        204,
        301,
        302,
        304,
        307,
        308,
        400,
        401,
        403,
        408,
        429,
        500,
        503,
        404,
        410,
        200,
    ],
)
async def test_directory_failure_classification_survives_cooldown(outcome):
    calls = []

    async def fetch(url, *, allow_private=False):
        calls.append(url)
        if outcome == "timeout":
            raise TimeoutError("secret transport detail")
        if outcome == "offline":
            raise OSError("secret transport detail")
        return DirectoryResponse(b"invalid", {}, status=outcome)

    resolver = DiscoveredSignerKeys(fetcher=fetch, clock=lambda: 1000)
    exceptions = []
    for _ in range(5):
        with pytest.raises(WbaVerificationError) as caught:
            await resolver("missing", signer_origin=ORIGIN)
        # WBA §6.10: every outcome is a discovery failure, the request is
        # unverified, and the signature path never answers 503.
        assert isinstance(caught.value, WbaDiscoveryError)
        assert caught.value.discovery_failure
        assert caught.value.status == 403
        assert caught.value.headers.get("Accept-Signature")
        exceptions.append(caught.value)
    assert len(calls) == 1
    assert len({id(error) for error in exceptions}) == len(exceptions)
    # The cooldown keeps only a deadline, never a raised exception object.
    assert all(isinstance(value, float | int) for value in resolver._failures.values())


async def test_fetched_directory_without_requested_key_is_absence_not_outage():
    private = Ed25519PrivateKey.generate()
    response = sign_key_directory(agent_origin=ORIGIN, private_keys=[private], created=1000)

    async def fetch(url, *, allow_private=False):
        return response

    resolver = DiscoveredSignerKeys(fetcher=fetch, clock=lambda: 1000)
    assert await resolver("missing", signer_origin=ORIGIN) is None
    assert await resolver(public_directory_key(private)["kid"], signer_origin=ORIGIN) is not None


def revocation_fixture(*, private=None, start=None, revoked=False):
    private = private or Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = {
        "version": 1,
        "issuer": ORIGIN,
        "updated": start.isoformat(),
        "next_update": (start + timedelta(seconds=60)).isoformat(),
        "revoked_kids": [jwk["kid"]] if revoked else [],
        "revoked_jtis": [],
    }
    protected = b64url_encode(
        json.dumps({"alg": "EdDSA", "kid": jwk["kid"], "typ": "adcp-gov-revocation+jws"}).encode()
    )
    encoded = b64url_encode(json.dumps(payload).encode())
    signature = b64url_encode(private.sign(f"{protected}.{encoded}".encode()))
    return jwk, start, FetchResult(f"{protected}.{encoded}.{signature}", etag=None)


@pytest.mark.parametrize("refresh_result", ["failed", "signed", "not_modified"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_revocation_refresh_rechecks_grace_after_wait(refresh_result, asynchronous):
    jwk, start, document = revocation_fixture()
    now, calls = [0.0], []

    def fetch(uri, **kwargs):
        calls.append(uri)
        if len(calls) == 1:
            return document
        # A request begins inside grace, but fetch completes at its boundary.
        now[0] = 300.0
        if refresh_result == "failed":
            raise RevocationListFetchError("offline")
        if refresh_result == "not_modified":
            return FetchResult("", etag=None, not_modified=True)
        return document

    async def afetch(uri, **kwargs):
        await asyncio.sleep(0)
        return fetch(uri, **kwargs)

    async def resolve(keyid):
        return jwk

    options = {
        "issuer": ORIGIN,
        "revocation_uri": ORIGIN + "/.well-known/governance-revocations.json",
        "clock": lambda: now[0],
        "wall_clock": lambda: start + timedelta(seconds=now[0]),
        "grace_multiplier": 4,
    }
    if asynchronous:
        checker = AsyncCachingRevocationChecker(fetcher=afetch, jwks_resolver=resolve, **options)
        assert not await checker(jwk["kid"])
        now[0] = 299.0
        with pytest.raises(RevocationListFreshnessError):
            await checker(jwk["kid"])
    else:
        checker = CachingRevocationChecker(
            fetcher=fetch, jwks_resolver=lambda keyid: jwk, **options
        )
        assert not checker(jwk["kid"])
        now[0] = 299.0
        with pytest.raises(RevocationListFreshnessError):
            checker(jwk["kid"])
    assert len(calls) == 2


async def test_stale_authenticated_revocations_survive_failed_initial_origin_discovery():
    private = Ed25519PrivateKey.generate()
    jwk, start, stale = revocation_fixture(private=private, revoked=True)
    now, calls = [300.0], []

    async def resolve(keyid, *, signer_origin=None):
        return ResolvedSignerKey(signer_origin, jwk)

    async def fetch(uri, **kwargs):
        calls.append(uri)
        if len(calls) == 1:
            return stale
        _, _, fresh = revocation_fixture(
            private=private, start=start + timedelta(seconds=now[0]), revoked=False
        )
        return fresh

    checker = OriginRevocationChecker(
        resolve,
        fetcher=fetch,
        clock=lambda: now[0],
        wall_clock=lambda: start + timedelta(seconds=now[0]),
    )
    # A stale first list is refused as current, but still revokes what it revoked.
    assert await checker.status(ORIGIN, jwk["kid"]) == "revoked"
    assert await checker(ORIGIN, jwk["kid"])
    assert ORIGIN in checker._checkers
    assert checker._checkers[ORIGIN].has_verified_list
    now[0] = 361
    # A later list that omits the revocation cannot withdraw it.
    assert await checker.status(ORIGIN, "unrevoked-kid-" + "x" * 29) == "current"
    assert len(calls) == 2
    assert await checker(ORIGIN, jwk["kid"])
