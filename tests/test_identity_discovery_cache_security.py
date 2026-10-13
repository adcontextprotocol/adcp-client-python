"""Discovery isolates tenants and bounds unauthenticated fetch pressure."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from adcp.signing.crypto import b64url_encode
from adcp.signing.revocation_fetcher import FetchResult, RevocationListFetchError
from adcp.signing.signer_keys import ResolvedSignerKey
from adcp.signing.trust import (
    TrustRecordAbsentError,
    TrustRecordError,
    TrustRecordEvaluator,
    TrustResponse,
)
from adcp.signing.wba import (
    OriginRevocationChecker,
    WbaVerificationError,
    public_directory_key,
)


def response(origin: str) -> TrustResponse:
    return TrustResponse(
        json.dumps({"agents": [{"url": origin, "roles": ["adcp:buying"]}]}).encode()
    )


async def test_slow_trust_fetch_does_not_block_another_operators_cached_record():
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        if "slow.com" in url:
            started.set()
            await release.wait()
        return response("https://buyer.slow.com" if "slow.com" in url else "https://buyer.fast.com")

    records = TrustRecordEvaluator(fetcher=fetch)
    await records.evaluate("https://buyer.fast.com", role="adcp:buying")
    slow = asyncio.create_task(records.evaluate("https://buyer.slow.com"))
    try:
        await started.wait()
        fast = await asyncio.wait_for(records.evaluate("https://buyer.fast.com"), timeout=1)
        assert fast.operator_domain == "fast.com"
    finally:
        release.set()
        await slow


async def test_same_operator_trust_fetch_is_shared_and_survives_waiter_cancellation():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return response("https://buyer.agency.com")

    records = TrustRecordEvaluator(fetcher=fetch)
    first = asyncio.create_task(records.evaluate("https://buyer.agency.com"))
    await started.wait()
    second = asyncio.create_task(records.evaluate("https://other.agency.com"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second).operator_domain == "agency.com"
    assert calls == 1


@pytest.mark.parametrize("status", [404, 500])
async def test_negative_trust_cache_expires_within_sixty_seconds(status: int):
    now, calls = [1000.0], 0

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        nonlocal calls
        calls += 1
        return TrustResponse(b"", status)

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    error = TrustRecordAbsentError if status == 404 else TrustRecordError
    for _ in range(3):
        with pytest.raises(error):
            await records.evaluate("https://buyer.agency.com")
    assert calls == 1
    now[0] += 60
    with pytest.raises(error):
        await records.evaluate("https://buyer.agency.com")
    assert calls == 2


async def test_trust_discovery_bounds_pending_and_negative_records():
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        started.set()
        await release.wait()
        return TrustResponse(b"", 404)

    records = TrustRecordEvaluator(fetcher=fetch, max_records=1)
    first = asyncio.create_task(records.evaluate("https://one.com"))
    await started.wait()
    with pytest.raises(TrustRecordError, match="capacity"):
        await records.evaluate("https://two.com")
    release.set()
    with pytest.raises(TrustRecordAbsentError):
        await first
    with pytest.raises(TrustRecordAbsentError):
        await records.evaluate("https://two.com")
    assert len(records._failures) == 1
    assert not records._pending


async def test_negative_cache_and_eviction_do_not_forget_successful_trust_discovery():
    now = [1000.0]
    absent = False

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        origin = "https://buyer.agency.com" if "agency.com" in url else "https://other.com"
        return TrustResponse(b"", 404) if absent else response(origin)

    records = TrustRecordEvaluator(fetcher=fetch, max_records=1, clock=lambda: now[0])
    await records.evaluate("https://buyer.agency.com")
    await records.evaluate("https://other.com")
    absent = True
    now[0] += 60
    for _ in range(2):
        with pytest.raises(TrustRecordError) as exc:
            await records.evaluate("https://buyer.agency.com")
        assert not isinstance(exc.value, TrustRecordAbsentError)


async def test_shared_hop_fetch_coalesces_without_sharing_operator_authority():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0
    target = "https://hosting.com/shared.json"

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        nonlocal calls
        if url == target:
            calls += 1
            started.set()
            await release.wait()
            return response("https://buyer.one.com")
        return TrustResponse(json.dumps({"authoritative_location": target}).encode())

    records = TrustRecordEvaluator(fetcher=fetch, max_fetches_per_host=1)
    first = asyncio.create_task(records.evaluate("https://buyer.one.com"))
    await started.wait()
    second = asyncio.create_task(records.evaluate("https://buyer.two.com"))
    await asyncio.sleep(0)
    release.set()
    assert (await first).operator_domain == "one.com"
    with pytest.raises(TrustRecordError, match="owner"):
        await second
    assert calls == 1
    assert not records._hosts_inflight


async def test_shared_hop_host_limits_other_paths_without_blocking_distinct_host():
    started, release = asyncio.Event(), asyncio.Event()
    host_calls = []

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        if "hosting.com" in url:
            host_calls.append(url)
            started.set()
            await release.wait()
            return TrustResponse(b'{"agents":[]}')
        if "fast.com" in url:
            return response("https://buyer.fast.com")
        path = "one" if "one.com" in url else "two"
        return TrustResponse(
            json.dumps({"authoritative_location": f"https://hosting.com/{path}.json"}).encode()
        )

    records = TrustRecordEvaluator(fetcher=fetch, max_fetches_per_host=1)
    first = asyncio.create_task(records.evaluate("https://buyer.one.com"))
    try:
        await started.wait()
        with pytest.raises(TrustRecordError):
            await records.evaluate("https://buyer.two.com")
        fast = await asyncio.wait_for(records.evaluate("https://buyer.fast.com"), timeout=1)
        assert fast.operator_domain == "fast.com"
        assert len(host_calls) == 1
    finally:
        release.set()
        await first


async def test_hop_transport_cache_cannot_extend_old_authority_freshness():
    now = [1000.0]
    calls = 0
    target = "https://hosting.com/shared.json"

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        nonlocal calls
        if url == target:
            calls += 1
            return TrustResponse(b'{"agents":[]}')
        return TrustResponse(json.dumps({"authoritative_location": target}).encode())

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    assert (await records.evaluate("https://one.com")).expires_at == 1060
    now[0] = 1050
    assert (await records.evaluate("https://two.com")).expires_at == 1060
    assert calls == 1
    now[0] = 1060
    assert (await records.evaluate("https://two.com")).expires_at == 1110
    assert calls == 2


async def test_hop_host_failure_cooldown_cannot_be_mistaken_for_owner_absence():
    now, calls = [1000.0], 0

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        nonlocal calls
        if "hosting.com" in url:
            calls += 1
            if calls == 1:
                raise OSError("offline")
            return TrustResponse(b'{"agents":[]}')
        path = "one" if "one.com" in url else "two"
        return TrustResponse(
            json.dumps({"authoritative_location": f"https://hosting.com/{path}.json"}).encode()
        )

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    for origin in ("https://one.com", "https://two.com"):
        with pytest.raises(TrustRecordError) as exc:
            await records.evaluate(origin)
        assert not isinstance(exc.value, TrustRecordAbsentError)
    assert calls == 1
    now[0] += 60
    assert (await records.evaluate("https://two.com")).operator_domain == "two.com"
    assert calls == 2


def signed_revocations(private, origin: str, *, revoked: bool = False) -> FetchResult:
    jwk = public_directory_key(private)
    now = datetime.now(timezone.utc)
    payload = {
        "version": 1,
        "issuer": origin,
        "updated": (now - timedelta(seconds=1)).isoformat(),
        "next_update": (now + timedelta(minutes=5)).isoformat(),
        "revoked_kids": [jwk["kid"]] if revoked else [],
        "revoked_jtis": [],
    }
    protected = b64url_encode(
        json.dumps({"alg": "EdDSA", "kid": jwk["kid"], "typ": "adcp-gov-revocation+jws"}).encode()
    )
    encoded = b64url_encode(json.dumps(payload).encode())
    signature = b64url_encode(private.sign(f"{protected}.{encoded}".encode()))
    return FetchResult(f"{protected}.{encoded}.{signature}", etag=None)


async def test_failed_initial_revocations_do_not_consume_permanent_admission():
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)

    async def resolve(keyid, *, signer_origin=None):
        return ResolvedSignerKey(signer_origin, jwk) if keyid == jwk["kid"] else None

    async def fetch(uri, **kwargs):
        if "failed" in uri:
            raise RevocationListFetchError("not published")
        return signed_revocations(private, "https://buyer.legitimate.com")

    checker = OriginRevocationChecker(resolve, fetcher=fetch, max_origins=1)
    for index in range(5):
        with pytest.raises(WbaVerificationError, match="unavailable"):
            await checker(f"https://failed-{index}.agency.com", jwk["kid"])
    assert not checker._checkers
    assert len(checker._failures) == 1
    assert not await checker("https://buyer.legitimate.com", jwk["kid"])
    assert len(checker._checkers) == 1


async def test_revocation_discovery_negative_cache_expires_and_preserves_revoked_history():
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    now, calls = [1000.0], 0
    origin = "https://buyer.agency.com"

    async def resolve(keyid, *, signer_origin=None):
        return ResolvedSignerKey(signer_origin, jwk)

    async def fetch(uri, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RevocationListFetchError("offline")
        return signed_revocations(private, origin, revoked=True)

    checker = OriginRevocationChecker(resolve, fetcher=fetch, clock=lambda: now[0])
    with pytest.raises(WbaVerificationError, match="unavailable"):
        await checker(origin, jwk["kid"])
    with pytest.raises(WbaVerificationError, match="cooling"):
        await checker(origin, jwk["kid"])
    assert calls == 1
    now[0] += 60
    assert await checker(origin, jwk["kid"])
    assert await checker(origin, jwk["kid"])
    assert calls == 2
    assert origin in checker._checkers
    assert origin not in checker._failures


async def test_operator_revocation_admission_quota_leaves_room_for_other_operators():
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)

    async def resolve(keyid, *, signer_origin=None):
        return ResolvedSignerKey(signer_origin, jwk)

    async def fetch(uri, **kwargs):
        return signed_revocations(private, uri.partition("/.well-known")[0])

    checker = OriginRevocationChecker(
        resolve, fetcher=fetch, max_origins=4, max_origins_per_operator=2
    )
    for index in range(2):
        assert not await checker(f"https://buyer-{index}.agency.com", jwk["kid"])
    with pytest.raises(WbaVerificationError, match="operator") as exc:
        await checker("https://buyer-3.agency.com", jwk["kid"])
    assert exc.value.status == 429
    assert not await checker("https://buyer.other.com", jwk["kid"])
    assert len(checker._checkers) == 3


async def test_revocation_first_discovery_is_shared_and_not_cancelled_by_one_waiter():
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0
    origin = "https://buyer.agency.com"

    async def resolve(keyid, *, signer_origin=None):
        return ResolvedSignerKey(signer_origin, jwk)

    async def fetch(uri, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return signed_revocations(private, origin)

    checker = OriginRevocationChecker(resolve, fetcher=fetch)
    first = asyncio.create_task(checker(origin, jwk["kid"]))
    await started.wait()
    second = asyncio.create_task(checker(origin, jwk["kid"]))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert not await second
    assert calls == 1
    assert not checker._pending
