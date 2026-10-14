"""Authority-history quotas refuse new admissions without forgetting other tenants."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from adcp.decisioning.registry import BuyerAgent
from adcp.decisioning.trust_grants import TrustGrantClaim, TrustGrantGate
from adcp.decisioning.types import AdcpError
from adcp.signing.crypto import b64url_encode
from adcp.signing.revocation_fetcher import (
    AsyncCachingRevocationChecker,
    FetchResult,
    RevocationListFreshnessError,
)
from adcp.signing.signer_keys import ResolvedSignerKey
from adcp.signing.trust import (
    GrantAuthorization,
    TrustGrantEvaluator,
    TrustRecordEvaluator,
    TrustRecordNotFoundError,
    TrustRecordUnavailableError,
    TrustResponse,
)
from adcp.signing.wba import (
    DiscoveredSignerKeys,
    OriginRevocationChecker,
    WbaUnavailableError,
    public_directory_key,
    sign_key_directory,
)
from tests.test_wba_authorization_errors import (
    changing_grant_gate,
    require_existing_claim,
)


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.5])
@pytest.mark.parametrize(
    "factory,parameter",
    [
        (TrustRecordEvaluator, "max_history_records"),
        (TrustRecordEvaluator, "max_records"),
        (TrustRecordEvaluator, "max_fetches_per_host"),
        (DiscoveredSignerKeys, "max_origins"),
        (DiscoveredSignerKeys, "max_origins_per_operator"),
        (OriginRevocationChecker, "max_facts_per_origin"),
        (OriginRevocationChecker, "max_origins"),
        (OriginRevocationChecker, "max_origins_per_operator"),
        (OriginRevocationChecker, "max_facts_per_operator"),
        (OriginRevocationChecker, "max_history_facts"),
        (TrustGrantGate, "max_denials"),
        (TrustGrantGate, "max_claims_per_operator"),
    ],
)
def test_history_limits_require_positive_integers(factory, parameter, bad):
    args = []
    if factory is OriginRevocationChecker:
        args = [lambda keyid, **kwargs: None]
    if factory is TrustGrantGate:
        args = [None, None]
    with pytest.raises(ValueError):
        factory(*args, **{parameter: bad})


async def test_private_tenant_flood_cannot_evict_trust_publication_or_removal():
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
    assert len(records._seen_200) == len(records._last_policy) == 3
    assert victim in records._last_policy
    modes[victim], now[0] = "removed", 1061
    with pytest.raises(TrustRecordNotFoundError):
        await records.evaluate(victim)
    modes[victim], now[0] = "offline", 1122
    with pytest.raises(TrustRecordNotFoundError):
        await records.evaluate(victim, allow_stale_on_outage=True)
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
    assert "unknown.vercel.app" not in records._last_policy
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


async def test_private_tenant_directory_flood_preserves_victims_signed_snapshot():
    private = Ed25519PrivateKey.generate()
    kid = public_directory_key(private)["kid"]
    now, offline, calls = [1000.0], set(), []

    async def fetch(url, **kwargs):
        origin = "https://" + urlsplit(url).hostname
        calls.append(origin)
        if origin in offline:
            raise OSError("offline")
        return sign_key_directory(agent_origin=origin, private_keys=[private], created=int(now[0]))

    keys = DiscoveredSignerKeys(fetcher=fetch, max_origins=3, clock=lambda: now[0])
    victim = "https://victim.vercel.app"
    for origin in (victim, "https://attacker-a.vercel.app", "https://attacker-b.vercel.app"):
        assert await keys(kid, signer_origin=origin) is not None
    old_snapshot = keys._cache[victim].response
    for index in range(10):
        origin = f"https://overflow-{index}.vercel.app"
        with pytest.raises(WbaUnavailableError, match="capacity"):
            await keys(kid, signer_origin=origin)
        assert origin not in calls
        assert origin not in keys._failed_discoveries
    assert await keys(kid, signer_origin=victim) is not None
    offline.add(victim)
    now[0] = 1301
    with pytest.raises(WbaUnavailableError):
        await keys(kid, signer_origin=victim)
    assert keys._cache[victim].response is old_snapshot
    assert len(keys._cache) == 3


async def test_directory_operator_pending_quota_does_not_block_other_operators():
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
        fetcher=fetch, max_origins=2, max_origins_per_operator=1, clock=lambda: 1000
    )
    first = asyncio.create_task(keys(kid, signer_origin=attacker))
    try:
        assert await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(WbaUnavailableError, match="operator"):
            await keys(kid, signer_origin="https://two.attacker.vercel.app")
        assert await keys(kid, signer_origin="https://victim.vercel.app") is not None
    finally:
        release.set()
        with pytest.raises(WbaUnavailableError):
            await asyncio.wait_for(first, timeout=5)
    assert await keys(kid, signer_origin="https://two.attacker.vercel.app") is not None


@pytest.mark.parametrize("global_overflow", [False, True])
async def test_claim_overflow_refuses_only_new_claim_and_preserves_victim_continuity(
    global_overflow,
):
    original, mode, now = changing_grant_gate(initial="allowed")
    gate = TrustGrantGate(
        original.evaluator,
        original.extract_claim,
        max_denials=3 if global_overflow else 8,
        max_claims_per_operator=2,
    )
    await require_existing_claim(gate)
    for index in range(2):
        attacker = BuyerAgent(
            agent_url=(
                f"https://attacker-{index}.vercel.app"
                if global_overflow
                else "https://buyer.attacker.com"
            ),
            display_name="Attacker",
            status="active",
        )
        with pytest.raises(AdcpError, match="PERMISSION_DENIED"):
            await require_existing_claim(
                gate, TrustGrantClaim("brand.com", "platform.com", brand_id=str(index)), attacker
            )
    attacker = BuyerAgent(
        agent_url="https://new.vercel.app" if global_overflow else "https://buyer.attacker.com",
        display_name="Attacker",
        status="active",
    )
    with pytest.raises(AdcpError, match="SERVICE_UNAVAILABLE"):
        await require_existing_claim(
            gate,
            TrustGrantClaim("other-brand.com", "other-account.com", brand_id="overflow"),
            attacker,
        )
    assert len(gate._history.claims) == 3
    mode[0], now[0] = "offline", 1061
    await require_existing_claim(gate)
    assert len(gate._history.positives) == 1
    assert len(gate._history.denials) == 2


async def test_shared_claim_cancellation_cannot_release_another_waiters_slot():
    entered, release = asyncio.Event(), asyncio.Event()

    class Evaluator:
        def current_time(self):
            return 1000

        async def check(self, **kwargs):
            entered.set()
            assert await asyncio.wait_for(release.wait(), timeout=5)
            return GrantAuthorization(True, expires_at=1060, outage_expires_at=1300)

    gate = TrustGrantGate(
        Evaluator(), lambda account, agent: account.metadata["claim"], max_denials=1
    )
    first = asyncio.create_task(require_existing_claim(gate))
    second = None
    try:
        assert await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(require_existing_claim(gate))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=5)
        with pytest.raises(AdcpError, match="SERVICE_UNAVAILABLE"):
            await require_existing_claim(
                gate, TrustGrantClaim("brand.com", "platform.com", brand_id="other")
            )
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(second, timeout=5)
        assert not gate._history.claims
        assert not gate._history.operator_counts
        release.set()
        await require_existing_claim(
            gate, TrustGrantClaim("brand.com", "platform.com", brand_id="other")
        )
    finally:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(
                *[task for task in (first, second) if task is not None], return_exceptions=True
            ),
            timeout=5,
        )


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
