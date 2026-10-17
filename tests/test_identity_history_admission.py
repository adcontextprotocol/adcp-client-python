"""Authority-history quotas refuse new admissions without forgetting other tenants."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from adcp.signing.crypto import b64url_encode
from adcp.signing.revocation_fetcher import (
    AsyncCachingRevocationChecker,
    FetchResult,
    RevocationListFreshnessError,
)
from adcp.signing.signer_keys import ResolvedSignerKey
from adcp.signing.trust import (
    TrustGrantEvaluator,
    TrustRecordEvaluator,
    TrustRecordUnavailableError,
    TrustResponse,
)
from adcp.signing.wba import (
    DiscoveredSignerKeys,
    OriginRevocationChecker,
    WbaDiscoveryError,
    WbaUnavailableError,
    WbaVerificationError,
    public_directory_key,
    sign_key_directory,
)


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.5])
@pytest.mark.parametrize(
    "factory,parameter",
    [
        (TrustRecordEvaluator, "max_history_records"),
        (TrustRecordEvaluator, "max_history_records_per_operator"),
        (TrustRecordEvaluator, "max_records"),
        (TrustRecordEvaluator, "max_fetches_per_host"),
        (TrustRecordEvaluator, "max_foreign_fetches_per_host"),
        (DiscoveredSignerKeys, "max_origins"),
        (DiscoveredSignerKeys, "max_origins_per_operator"),
        (OriginRevocationChecker, "max_facts_per_origin"),
        (OriginRevocationChecker, "max_origins"),
        (OriginRevocationChecker, "max_origins_per_operator"),
        (OriginRevocationChecker, "max_facts_per_operator"),
        (OriginRevocationChecker, "max_history_facts"),
    ],
)
def test_history_limits_require_positive_integers(factory, parameter, bad):
    args = []
    if factory is OriginRevocationChecker:
        args = [lambda keyid, **kwargs: None]
    with pytest.raises(ValueError):
        factory(*args, **{parameter: bad})


async def test_private_tenant_flood_cannot_evict_trust_publication_or_last_record():
    now, modes, calls = [1000.0], {}, []

    async def fetch(url, **kwargs):
        owner = urlsplit(url).hostname
        calls.append(owner)
        if modes.get(owner) == "removed":
            return TrustResponse(b"", 404)
        if modes.get(owner) == "offline":
            raise OSError("offline")
        return TrustResponse(b'{"agents":[]}')

    records = TrustRecordEvaluator(
        fetcher=fetch, clock=lambda: now[0], max_records=1, max_history_records=3
    )
    victim = "victim.vercel.app"
    await records.evaluate(victim)
    await records.evaluate("attacker-a.vercel.app")
    await records.evaluate("attacker-b.vercel.app")
    for index in range(10):
        owner = f"overflow-{index}.vercel.app"
        with pytest.raises(TrustRecordUnavailableError, match="history admission"):
            await records.evaluate(owner)
        assert owner not in records._failures
        assert owner not in calls
    assert len(records._seen_200) == len(records._last_record) == 3
    assert victim in records._last_record
    # A 404 after a read is a discovery failure, never a removal.
    modes[victim], now[0] = "removed", 1061
    with pytest.raises(TrustRecordUnavailableError):
        await records.evaluate(victim)
    modes[victim], now[0] = "offline", 1122
    stale = await records.evaluate(victim, allow_stale_on_outage=True)
    assert stale.stale_on_outage
    assert victim in records._seen_200
    assert len(records._history_owners) == 3


async def test_failed_and_cancelled_pending_trust_reads_release_history_reservations():
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(url, **kwargs):
        if urlsplit(url).hostname == "attacker.vercel.app":
            entered.set()
            assert await asyncio.wait_for(release.wait(), timeout=5)
            raise OSError("no authoritative response")
        return TrustResponse(b'{"agents":[]}')

    records = TrustRecordEvaluator(fetcher=fetch, max_history_records=1)
    first = asyncio.create_task(records.evaluate("attacker.vercel.app"))
    try:
        assert await asyncio.wait_for(entered.wait(), timeout=5)
        shared = records._pending["attacker.vercel.app"]
        with pytest.raises(TrustRecordUnavailableError, match="history admission"):
            await records.evaluate("victim.vercel.app")
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=5)
        assert len(records._history_owners) == 1
        release.set()
        with pytest.raises(TrustRecordUnavailableError):
            await asyncio.wait_for(asyncio.shield(shared), timeout=5)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), timeout=5)
    assert not records._history_owners
    assert (await records.evaluate("victim.vercel.app")).operator_domain == "victim.vercel.app"


async def test_previous_absent_owner_keeps_admission_without_authorizing_stale_absence():
    calls = []
    now, offline = [1000.0], [False]

    async def fetch(url, **kwargs):
        owner = urlsplit(url).hostname
        calls.append(owner)
        if offline[0] and owner == "unknown.vercel.app":
            raise OSError("unavailable")
        return (
            TrustResponse(b"", 404)
            if owner == "unknown.vercel.app"
            else TrustResponse(b'{"agents":[]}')
        )

    records = TrustRecordEvaluator(fetcher=fetch, max_history_records=2, clock=lambda: now[0])
    from adcp.signing.trust import TrustRecordAbsentError

    with pytest.raises(TrustRecordAbsentError):
        await records.evaluate("unknown.vercel.app")
    assert records._history_owners == {"unknown.vercel.app"}
    await records.evaluate("victim.vercel.app")
    with pytest.raises(TrustRecordUnavailableError, match="history admission"):
        await records.evaluate("new.vercel.app")
    now[0] = 1061
    with pytest.raises(TrustRecordAbsentError):
        await records.evaluate("unknown.vercel.app")
    offline[0], now[0] = True, 1122
    with pytest.raises(TrustRecordUnavailableError):
        await records.evaluate("unknown.vercel.app", allow_stale_on_outage=True)
    assert "unknown.vercel.app" not in records._seen_200
    assert "unknown.vercel.app" not in records._last_record
    assert len(records._history_owners) == 2
    assert calls == [
        "unknown.vercel.app",
        "victim.vercel.app",
        "unknown.vercel.app",
        "unknown.vercel.app",
    ]


async def test_previously_used_legacy_brand_can_refresh_after_history_fills():
    now, offline, legacy_calls = [1000.0], [False], []
    origin = "https://buyer.platform.com"

    async def fetch(url, **kwargs):
        if urlsplit(url).hostname == "platform.com":
            return TrustResponse(
                json.dumps({"agents": [{"url": origin, "roles": ["adcp:buying"]}]}).encode()
            )
        if offline[0]:
            raise OSError("brand record unavailable")
        return TrustResponse(b"", 404)

    async def legacy(request):
        legacy_calls.append(request.brand_domain)
        return True

    records = TrustRecordEvaluator(fetcher=fetch, max_history_records=2, clock=lambda: now[0])
    evaluator = TrustGrantEvaluator(records, legacy_authorization=legacy)
    request = {
        "agent_origin": origin,
        "brand_domain": "brand.com",
        "account_operator": "platform.com",
    }
    assert (await evaluator.check(**request)).authorized
    assert records._history_owners == {"platform.com", "brand.com"}
    with pytest.raises(TrustRecordUnavailableError, match="history admission"):
        await records.evaluate("attacker.vercel.app")
    now[0] = 1061
    assert (await evaluator.check(**request)).authorized
    offline[0], now[0] = True, 1122
    result = await evaluator.check(**request, allow_stale_on_outage=True)
    assert not result.authorized
    assert result.error_code == "SERVICE_UNAVAILABLE"
    assert legacy_calls == ["brand.com", "brand.com"]


async def test_private_tenant_history_quota_refuses_the_provider_group_only():
    """All tenants of a PSL PRIVATE suffix share one authority-history quota."""

    async def fetch(url, **kwargs):
        return TrustResponse(b'{"agents":[]}')

    records = TrustRecordEvaluator(
        fetcher=fetch, max_history_records=10, max_history_records_per_operator=2
    )
    await records.evaluate("victim.example.com")
    await records.evaluate("tenant-a.vercel.app")
    await records.evaluate("tenant-b.vercel.app")
    with pytest.raises(TrustRecordUnavailableError, match="history admission"):
        await records.evaluate("tenant-c.vercel.app")
    # The refusal is local backpressure: never cached as the owner's failure.
    assert "tenant-c.vercel.app" not in records._failures
    # Another operator still fits, and no admitted history was evicted.
    assert (await records.evaluate("other.example.net")).operator_domain == "example.net"
    assert {"example.com", "tenant-a.vercel.app", "tenant-b.vercel.app"} <= set(
        records._last_record
    )


async def test_directory_operator_pending_quota_groups_private_tenants():
    private = Ed25519PrivateKey.generate()
    kid = public_directory_key(private)["kid"]
    entered, release = asyncio.Event(), asyncio.Event()
    attacker = "https://one.attacker.vercel.app"

    async def fetch(url, **kwargs):
        origin = "https://" + urlsplit(url).hostname
        if origin == attacker:
            entered.set()
            assert await asyncio.wait_for(release.wait(), timeout=5)
            raise OSError("offline")
        return sign_key_directory(agent_origin=origin, private_keys=[private], created=1000)

    keys = DiscoveredSignerKeys(
        fetcher=fetch, max_origins=4, max_origins_per_operator=1, clock=lambda: 1000
    )
    first = asyncio.create_task(keys(kid, signer_origin=attacker))
    try:
        assert await asyncio.wait_for(entered.wait(), timeout=5)
        # Another free tenant of the same hosting suffix shares the quota: a
        # local refusal is load shedding (429), not a re-sign challenge.
        with pytest.raises(WbaVerificationError, match="operator") as refused:
            await keys(kid, signer_origin="https://two.vercel.app")
        assert refused.value.status == 429
        assert "Accept-Signature" not in refused.value.headers
        assert await keys(kid, signer_origin="https://buyer.victim.com") is not None
    finally:
        release.set()
        with pytest.raises(WbaDiscoveryError):
            await asyncio.wait_for(first, timeout=5)
    assert await keys(kid, signer_origin="https://two.vercel.app") is not None


async def test_directory_cache_eviction_only_forces_a_refetch():
    private = Ed25519PrivateKey.generate()
    kid = public_directory_key(private)["kid"]
    calls = []

    async def fetch(url, **kwargs):
        origin = "https://" + urlsplit(url).hostname
        calls.append(origin)
        return sign_key_directory(agent_origin=origin, private_keys=[private], created=1000)

    keys = DiscoveredSignerKeys(fetcher=fetch, max_origins=2, clock=lambda: 1000)
    victim = "https://buyer.victim.com"
    assert await keys(kid, signer_origin=victim) is not None
    for index in range(5):
        assert await keys(kid, signer_origin=f"https://buyer.other-{index}.com") is not None
    assert len(keys._cache) == 2
    assert victim not in keys._cache
    assert await keys(kid, signer_origin=victim) is not None
    assert calls.count(victim) == 2


def signed_list(private, origin, now, revoked):
    kid = public_directory_key(private)["kid"]
    payload = {
        "version": 1,
        "issuer": origin,
        "updated": now.isoformat(),
        "next_update": (now + timedelta(seconds=60)).isoformat(),
        "revoked_kids": revoked,
        "revoked_jtis": [],
    }
    protected = b64url_encode(
        json.dumps({"alg": "EdDSA", "kid": kid, "typ": "adcp-gov-revocation+jws"}).encode()
    )
    body = b64url_encode(json.dumps(payload).encode())
    signature = b64url_encode(private.sign(f"{protected}.{body}".encode()))
    return FetchResult(f"{protected}.{body}.{signature}", etag=None)


@pytest.mark.parametrize(
    "limit", ["max_facts_per_origin", "max_facts_per_operator", "max_history_facts"]
)
@pytest.mark.parametrize("initial_overflow", [False, True])
async def test_authenticated_revocation_overflow_permanently_closes_only_offending_origin(
    limit, initial_overflow
):
    private = Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    now, lists, calls = [1000.0], {}, []
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    victim, attacker = "https://victim.com", "https://buyer.attacker.com"
    lists[attacker] = ["old", "new"] if initial_overflow else ["old"]

    async def resolve(keyid, *, signer_origin=None):
        return ResolvedSignerKey(signer_origin, jwk)

    async def fetch(uri, **kwargs):
        origin = uri.partition("/.well-known/")[0]
        calls.append(origin)
        return signed_list(
            private, origin, start + timedelta(seconds=now[0]), lists.get(origin, [])
        )

    checker = OriginRevocationChecker(
        resolve,
        fetcher=fetch,
        clock=lambda: now[0],
        wall_clock=lambda: start + timedelta(seconds=now[0]),
        **{limit: 1},
    )
    assert not await checker(victim, jwk["kid"])
    if not initial_overflow:
        assert await checker(attacker, "old")
        lists[attacker], now[0] = ["new"], 1061
    with pytest.raises(WbaUnavailableError):
        await checker(attacker, "new")
    assert checker._checkers[attacker]._history_overflowed
    lists[attacker], now[0] = [], 1122
    old_calls = len(calls)
    with pytest.raises(WbaUnavailableError):
        await checker(attacker, "new")
    assert len(calls) == old_calls
    assert not await checker(victim, jwk["kid"])
    assert checker._history_fact_count <= 1
    if not initial_overflow:
        assert checker._checkers[attacker]._current_list.is_revoked("old")


@pytest.mark.parametrize("initial_list", [False, True])
@pytest.mark.parametrize("hook_raises", [False, True])
async def test_queued_revocation_calls_cannot_reuse_grace_after_history_refusal(
    initial_list, hook_raises
):
    private = Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    start, now = datetime(2026, 1, 1, tzinfo=timezone.utc), [0.0]
    entered, release = asyncio.Event(), asyncio.Event()
    calls, admissions = [], []

    async def resolve(keyid):
        return jwk

    async def fetch(uri, **kwargs):
        calls.append(uri)
        if len(calls) > (1 if initial_list else 0):
            entered.set()
            assert await asyncio.wait_for(release.wait(), timeout=5)
        return signed_list(
            private, "https://attacker.com", start + timedelta(seconds=now[0]), ["new"]
        )

    def admit(record):
        admissions.append(record)
        if initial_list and len(admissions) == 1:
            return True
        if hook_raises:
            raise RuntimeError("persistence unavailable")
        return False

    checker = AsyncCachingRevocationChecker.from_issuer_origin(
        "https://attacker.com",
        jwks_resolver=resolve,
        fetcher=fetch,
        clock=lambda: now[0],
        wall_clock=lambda: start + timedelta(seconds=now[0]),
        history_admission=admit,
    )
    if initial_list:
        assert not await checker(jwk["kid"])
        now[0] = 61
    first = asyncio.create_task(checker(jwk["kid"]))
    second = None
    try:
        assert await asyncio.wait_for(entered.wait(), timeout=5)
        if initial_list:
            # A second request inside grace joins the refresh lock once its
            # own cooldown elapsed, rather than taking pre-observation grace.
            now[0] = 122
        second = asyncio.create_task(checker(jwk["kid"]))
        await asyncio.sleep(0)
        release.set()
        for task in (first, second):
            with pytest.raises(RevocationListFreshnessError):
                await asyncio.wait_for(task, timeout=5)
        assert len(calls) == (2 if initial_list else 1)
        assert checker.has_verified_list
    finally:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(
                *[task for task in (first, second) if task is not None], return_exceptions=True
            ),
            timeout=5,
        )
