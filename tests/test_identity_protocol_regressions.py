"""Identity migration preserves legacy authority without allowing WBA downgrade."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from adcp.decisioning import (
    ApiKeyCredential,
    AuthInfo,
    BuyerAgent,
    DecisioningCapabilities,
    DecisioningPlatform,
    HttpSigCredential,
    InMemoryTaskRegistry,
    SingletonAccounts,
    bearer_only_registry,
    signing_only_registry,
)
from adcp.decisioning.handler import PlatformHandler, _resolve_buyer_agent
from adcp.decisioning.trust_grants import TrustGrantClaim, TrustGrantGate
from adcp.decisioning.types import Account, AdcpError
from adcp.server import ToolContext
from adcp.signing import (
    TrustGrantEvaluator,
    TrustRecordEvaluator,
    TrustRecordMatchError,
    TrustRecordUnavailableError,
    TrustResponse,
    trust_record_url,
)
from adcp.signing.trust import fetch_trust_record

ORIGIN = "https://buyer.platform.com"
LEGACY_PROFILE = "adcp/request-signing/v1"


def make_evaluator(documents, legacy_calls, fetch_calls):
    async def fetch(url, *, allow_private=False):
        fetch_calls.append(url)
        document = documents.get(url)
        if isinstance(document, Exception):
            raise document
        return (
            TrustResponse(json.dumps(document).encode())
            if document is not None
            else TrustResponse(b"", 404)
        )

    async def legacy(request):
        legacy_calls.append(request)
        return True

    records = TrustRecordEvaluator(fetcher=fetch)
    return TrustGrantEvaluator(records, legacy_authorization=legacy)


async def test_legacy_buyer_needs_no_new_operator_record_before_brand_publication():
    legacy_calls, fetch_calls = [], []
    evaluator = make_evaluator({}, legacy_calls, fetch_calls)
    result = await evaluator.check(
        agent_origin=ORIGIN,
        brand_domain="brand.com",
        account_operator="platform.com",
        signing_profile=LEGACY_PROFILE,
    )
    assert result.authorized and result.legacy_fallback_allowed
    assert len(legacy_calls) == 1
    assert fetch_calls == [trust_record_url("brand.com")]


async def test_wba_missing_operator_record_never_reaches_legacy_authority():
    legacy_calls, fetch_calls = [], []
    evaluator = make_evaluator({}, legacy_calls, fetch_calls)
    result = await evaluator.check(
        agent_origin=ORIGIN, brand_domain="brand.com", account_operator="platform.com"
    )
    assert not result.authorized
    assert result.reason == "signer_trust_record_unavailable"
    assert not legacy_calls
    assert fetch_calls == [trust_record_url(ORIGIN)]


@pytest.mark.parametrize("document", [{"agents": []}, RuntimeError("network unavailable")])
async def test_legacy_brand_publication_or_fetch_failure_does_not_fall_back(document):
    legacy_calls, fetch_calls = [], []
    evaluator = make_evaluator({trust_record_url("brand.com"): document}, legacy_calls, fetch_calls)
    result = await evaluator.check(
        agent_origin=ORIGIN,
        brand_domain="brand.com",
        account_operator="platform.com",
        signing_profile=LEGACY_PROFILE,
    )
    assert not result.authorized
    assert not legacy_calls


@pytest.mark.parametrize("trusted_profile", [LEGACY_PROFILE, "web-bot-auth"])
async def test_dispatch_uses_trusted_profile_before_sync_accounts_upsert(trusted_profile):
    touched, legacy_calls, fetch_calls = [], [], []

    class Accounts(SingletonAccounts):
        def upsert_request(self, request, ctx=None):
            touched.append("upsert")
            return {"accounts": []}

    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = Accounts(account_id="existing")

    buyer = BuyerAgent(agent_url=ORIGIN, display_name="Buyer", status="active")

    async def lookup(agent_url):
        return buyer

    gate = TrustGrantGate(
        make_evaluator({}, legacy_calls, fetch_calls),
        lambda account, agent: TrustGrantClaim("brand.com", "platform.com"),
    )
    auth = AuthInfo(
        kind="http_sig",
        credential=HttpSigCredential(
            kind="http_sig", keyid="kid", agent_url=ORIGIN, verified_at=1000
        ),
        extra={"signing_profile": trusted_profile, "principal_id": "approved-principal"},
    )
    # Caller-supplied fields must never select the compatibility path.
    declaration = {
        "brand": {"domain": "brand.com"},
        "operator": "platform.com",
        "signing_profile": LEGACY_PROFILE if trusted_profile == "web-bot-auth" else "web-bot-auth",
    }
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            Platform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            buyer_agent_registry=signing_only_registry(lookup),
            trust_grant_gate=gate,
        )
        context = ToolContext(metadata={"adcp.auth_info": auth})
        if trusted_profile == "web-bot-auth":
            with pytest.raises(AdcpError) as error:
                await handler.sync_accounts(SimpleNamespace(accounts=[declaration]), context)
            assert error.value.code == "PERMISSION_DENIED"
            assert not error.value.details
            assert not touched and not legacy_calls
        else:
            await handler.sync_accounts(SimpleNamespace(accounts=[declaration]), context)
            assert touched == ["upsert"]
            assert len(legacy_calls) == 1


@pytest.mark.parametrize("granted_scope", ["adcp:media_buying", "adcp:creative_generation"])
async def test_operation_extractor_uses_creative_authority_not_media_buying(granted_scope):
    legacy_calls, fetch_calls, operations = [], [], []
    evaluator = make_evaluator(
        {
            trust_record_url(ORIGIN): {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]},
            trust_record_url("brand.com"): {
                "grants": [{"grantee": "platform.com", "scopes": [granted_scope]}]
            },
        },
        legacy_calls,
        fetch_calls,
    )

    async def operation_claim(account, agent, operation):
        operations.append(operation)
        return TrustGrantClaim("brand.com", "platform.com", scope="adcp:creative_generation")

    gate = TrustGrantGate(
        evaluator,
        lambda account, agent: TrustGrantClaim("brand.com", "platform.com"),
        extract_operation_claim=operation_claim,
    )
    account = Account(id="existing")
    buyer = BuyerAgent(agent_url=ORIGIN, display_name="Buyer", status="active")
    if granted_scope == "adcp:creative_generation":
        await gate.require_account(account, buyer, operation="build_creative")
    else:
        with pytest.raises(AdcpError) as error:
            await gate.require_account(account, buyer, operation="build_creative")
        assert error.value.details["reason"] == "grant_absent"
    assert operations == ["build_creative"]
    assert not legacy_calls


async def test_non_buying_operation_cannot_inherit_default_media_buying_scope():
    legacy_calls, fetch_calls = [], []
    gate = TrustGrantGate(
        make_evaluator({}, legacy_calls, fetch_calls),
        lambda account, agent: TrustGrantClaim("brand.com", "platform.com"),
    )
    buyer = BuyerAgent(agent_url=ORIGIN, display_name="Buyer", status="active")
    with pytest.raises(AdcpError, match="explicit grant scope"):
        await gate.require_account(Account(id="existing"), buyer, operation="build_creative")
    assert not fetch_calls and not legacy_calls


@pytest.mark.parametrize("principal", [None, "approved-principal"])
async def test_unbound_wba_cannot_inherit_legacy_registry_origin(principal):
    looked_up = []
    buyer = BuyerAgent(agent_url=ORIGIN, display_name="Existing legacy buyer", status="active")

    async def lookup(agent_url):
        looked_up.append(agent_url)
        return buyer

    auth = AuthInfo(
        kind="http_sig",
        credential=HttpSigCredential(
            kind="http_sig", keyid="new-directory-key", agent_url=ORIGIN, verified_at=1000
        ),
        extra={"signing_profile": "web-bot-auth", "principal_id": principal},
    )
    registry = signing_only_registry(lookup)
    if principal is None:
        with pytest.raises(AdcpError) as error:
            await _resolve_buyer_agent(registry, auth)
        assert error.value.code == "PERMISSION_DENIED"
        assert not error.value.details
        assert not looked_up
    else:
        assert await _resolve_buyer_agent(registry, auth) is buyer
        assert looked_up == [ORIGIN]


async def test_explicit_bearer_identity_keeps_its_independent_registry_authority():
    buyer = BuyerAgent(agent_url=ORIGIN, display_name="Approved bearer buyer", status="active")
    credential = ApiKeyCredential(kind="api_key", key_id="approved-api-key")
    looked_up = []

    async def lookup(verified_credential):
        looked_up.append(verified_credential)
        return buyer

    auth = AuthInfo(
        kind="api_key",
        credential=credential,
        extra={"signing_profile": "web-bot-auth", "principal_id": None},
    )
    assert await _resolve_buyer_agent(bearer_only_registry(lookup), auth) is buyer
    assert looked_up == [credential]


@pytest.mark.parametrize("policy_delay", [0, 49, 50, 51])
async def test_discretionary_authority_cannot_outlive_required_operator_membership(policy_delay):
    now = [1000.0]

    async def fetch(url, *, allow_private=False):
        if url == trust_record_url(ORIGIN):
            now[0] += 10
            document = {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}
        else:
            # The brand's record expires later than the required signer record.
            document = {"grants": []}
        return TrustResponse(json.dumps(document).encode())

    async def seller(request):
        now[0] += policy_delay
        return True

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    result = await TrustGrantEvaluator(records, seller_authorization=seller).check(
        agent_origin=ORIGIN, brand_domain="brand.com", account_operator="platform.com"
    )
    assert (await records.evaluate("brand.com")).expires_at == 1070
    if policy_delay < 50:
        assert result.authorized
        assert result.expires_at == 1060
    else:
        assert not result.authorized
        assert result.reason == "trust_record_unavailable"
        assert result.error_code == "SERVICE_UNAVAILABLE"


@pytest.mark.parametrize("signing_profile", ["web-bot-auth", LEGACY_PROFILE])
@pytest.mark.parametrize("policy_delay", [0, 60, 61])
async def test_legacy_fallback_retains_required_wba_membership_expiry(
    signing_profile, policy_delay
):
    now = [1000.0]
    fetched = []

    async def fetch(url, *, allow_private=False):
        fetched.append(url)
        if url == trust_record_url(ORIGIN):
            return TrustResponse(
                json.dumps({"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}).encode()
            )
        return TrustResponse(b"", 404)

    async def legacy(request):
        now[0] += policy_delay
        return True

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    result = await TrustGrantEvaluator(records, legacy_authorization=legacy).check(
        agent_origin=ORIGIN,
        brand_domain="brand.com",
        account_operator="platform.com",
        signing_profile=signing_profile,
    )
    if signing_profile == LEGACY_PROFILE:
        assert result.authorized
        assert result.expires_at is None
        assert fetched == [trust_record_url("brand.com")]
    elif policy_delay < 60:
        assert result.authorized
        assert result.expires_at == 1060
    else:
        assert not result.authorized
        assert result.reason == "signer_trust_record_unavailable"
        assert result.error_code == "SERVICE_UNAVAILABLE"


@pytest.mark.parametrize(
    "claim_overrides,grant_overrides,authorized",
    [
        ({}, {}, True),
        ({"account_operator": "platform.com"}, {}, True),
        ({"account_operator": "agency.com"}, {}, False),
        ({"brand_id": "other"}, {}, False),
        ({"countries": ["CA"]}, {}, False),
        ({"scope": "adcp:creative_generation"}, {}, False),
        ({}, {"agents": [{"url": "https://different.platform.com"}]}, False),
        ({}, {"valid_until": "1970-01-01T00:16:40Z"}, False),
        ({}, {"valid_from": "1970-01-01T00:18:20Z"}, False),
    ],
)
async def test_self_operated_brand_can_grant_hosted_signer_directly(
    claim_overrides, grant_overrides, authorized
):
    fetched = []
    grant = {
        "grantee": "platform.com",
        "scopes": ["adcp:media_buying"],
        "agents": [{"url": ORIGIN + "/mcp"}],
        "countries": ["US"],
        "profiles": {"adcp": {"brands": ["brand_1"]}},
        "valid_until": "1970-01-01T00:18:20Z",
        **grant_overrides,
    }
    documents = {
        trust_record_url(ORIGIN): {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]},
        trust_record_url("brand.com"): {"grants": [grant], "exclusive_grants": True},
    }

    async def fetch(url, *, allow_private=False):
        fetched.append(url)
        document = documents.get(url)
        return (
            TrustResponse(json.dumps(document).encode())
            if document is not None
            else TrustResponse(b"", 404)
        )

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: 1000)
    result = await TrustGrantEvaluator(records).check(
        **{
            "agent_origin": ORIGIN,
            "brand_domain": "brand.com",
            "account_operator": "brand.com",
            "brand_id": "brand_1",
            "countries": ["US"],
            **claim_overrides,
        }
    )
    assert result.authorized is authorized
    if authorized:
        assert result.expires_at == 1060
        assert fetched == [trust_record_url(ORIGIN), trust_record_url("brand.com")]
    else:
        assert result.error_code == "PERMISSION_DENIED"
        assert result.include_grant_absent_details


@pytest.mark.parametrize(
    "record_url,document,expected_code,expected_details",
    [
        (trust_record_url(ORIGIN), {"agents": []}, "PERMISSION_DENIED", {}),
        (trust_record_url(ORIGIN), None, "PERMISSION_DENIED", {}),
        (trust_record_url(ORIGIN), RuntimeError("offline"), "SERVICE_UNAVAILABLE", {}),
        (trust_record_url(ORIGIN), TrustResponse(b"", 503), "SERVICE_UNAVAILABLE", {}),
        (trust_record_url("brand.com"), RuntimeError("offline"), "SERVICE_UNAVAILABLE", {}),
        (trust_record_url("brand.com"), {"unexpected": True}, "PERMISSION_DENIED", {}),
        (
            trust_record_url("brand.com"),
            {"grants": []},
            "PERMISSION_DENIED",
            {"scope": "agent", "reason": "grant_absent"},
        ),
        (trust_record_url("agency.com"), RuntimeError("offline"), "SERVICE_UNAVAILABLE", {}),
    ],
)
async def test_grant_failures_distinguish_trust_outages_and_authorization(
    record_url, document, expected_code, expected_details
):
    documents = {
        trust_record_url(ORIGIN): {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]},
        trust_record_url("brand.com"): {
            "grants": [{"grantee": "agency.com", "scopes": ["adcp:media_buying"]}]
        },
        trust_record_url("agency.com"): {
            "grants": [{"grantee": "platform.com", "scopes": ["adcp:media_buying"]}]
        },
        record_url: document,
    }

    async def fetch(url, *, allow_private=False):
        value = documents[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, TrustResponse):
            return value
        return (
            TrustResponse(json.dumps(value).encode())
            if value is not None
            else TrustResponse(b"", 404)
        )

    result = await TrustGrantEvaluator(TrustRecordEvaluator(fetcher=fetch)).check(
        agent_origin=ORIGIN, brand_domain="brand.com", account_operator="agency.com"
    )
    assert not result.authorized
    with pytest.raises(AdcpError) as error:
        result.require()
    assert error.value.code == expected_code
    assert error.value.details == expected_details
    assert error.value.recovery == (
        "transient" if expected_code == "SERVICE_UNAVAILABLE" else "correctable"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent_origin": "https://127.0.0.1"},
        {"agent_origin": "https://co.uk"},
        {"brand_domain": "co.uk"},
        {"account_operator": "127.0.0.1"},
    ],
)
async def test_undefined_operator_derivation_is_not_an_uncovered_grant(overrides):
    async def fetch(url, *, allow_private=False):
        pytest.fail("An undefined registrable domain must not trigger trust discovery")

    result = await TrustGrantEvaluator(TrustRecordEvaluator(fetcher=fetch)).check(
        **{
            "agent_origin": ORIGIN,
            "brand_domain": "brand.com",
            "account_operator": "platform.com",
            **overrides,
        }
    )
    assert not result.authorized
    assert result.error_code == "PERMISSION_DENIED"
    with pytest.raises(AdcpError) as error:
        result.require()
    assert error.value.code == "PERMISSION_DENIED"
    assert not error.value.details


@pytest.mark.parametrize("when,allowed", [(1061, True), (1299.999, True), (1300, False)])
async def test_public_record_outage_grace_is_bounded_and_never_fresh(when, allowed):
    now, offline = [1000.0], [False]

    async def fetch(url, *, allow_private=False):
        if offline[0]:
            raise OSError("offline")
        return TrustResponse(
            json.dumps({"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}).encode()
        )

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    initial = await records.evaluate(ORIGIN, role="adcp:buying")
    assert records.polling_interval_seconds == 60
    assert not initial.stale_on_outage
    offline[0], now[0] = True, when
    with pytest.raises(TrustRecordUnavailableError):
        await records.evaluate(ORIGIN, role="adcp:buying")
    if allowed:
        cached = await records.evaluate(ORIGIN, role="adcp:buying", allow_stale_on_outage=True)
        assert cached.stale_on_outage
        assert cached.expires_at == 1060
        assert cached.revision == initial.revision
        await records.validate_snapshots((cached,), origin_or_domain=ORIGIN, role="adcp:buying")
    else:
        with pytest.raises(TrustRecordUnavailableError, match="grace"):
            await records.evaluate(ORIGIN, role="adcp:buying", allow_stale_on_outage=True)


async def test_initial_outage_cannot_create_record_authority():
    async def fetch(url, *, allow_private=False):
        raise OSError("offline before first read")

    records = TrustRecordEvaluator(fetcher=fetch)
    with pytest.raises(TrustRecordUnavailableError, match="no prior"):
        await records.evaluate(ORIGIN, role="adcp:buying", allow_stale_on_outage=True)


async def test_other_origin_negative_read_survives_global_capacity_and_age():
    now, mode = [1000.0], ["listed"]
    started, release = asyncio.Event(), asyncio.Event()
    other_origin = "https://other.platform.com"

    async def fetch(url, *, allow_private=False):
        if url == trust_record_url("busy.com"):
            started.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return TrustResponse(json.dumps({"agents": []}).encode())
        agents = [other_origin, ORIGIN] if mode[0] == "listed" else [other_origin]
        return TrustResponse(
            json.dumps(
                {"agents": [{"url": origin, "roles": ["adcp:buying"]} for origin in agents]}
            ).encode()
        )

    records = TrustRecordEvaluator(fetcher=fetch, max_records=1, clock=lambda: now[0])
    await records.evaluate(ORIGIN, role="adcp:buying")
    mode[0], now[0] = "delisted", 1061
    await records.evaluate(other_origin, role="adcp:buying")
    now[0] = 2000
    pending = asyncio.create_task(records.evaluate("busy.com"))
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        for allow_stale in (False, True):
            with pytest.raises(TrustRecordMatchError):
                await records.evaluate(
                    ORIGIN, role="adcp:buying", allow_stale_on_outage=allow_stale
                )
    finally:
        release.set()
        await asyncio.wait_for(pending, timeout=5)


@pytest.mark.parametrize("grantor", ["brand.com", "agency.com"])
@pytest.mark.parametrize("status", [404, 410])
async def test_removed_grantor_stays_grant_absent_during_later_outage(grantor, status):
    now, mode, legacy_calls = [1000.0], ["published"], []

    async def fetch(url, *, allow_private=False):
        if url == trust_record_url(grantor) and mode[0] != "published":
            if mode[0] == "offline":
                raise OSError("later outage")
            return TrustResponse(b"", status)
        document = (
            {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}
            if url == trust_record_url(ORIGIN)
            else {
                "grants": [
                    {
                        "grantee": (
                            "agency.com" if url == trust_record_url("brand.com") else "platform.com"
                        ),
                        "scopes": ["adcp:media_buying"],
                    }
                ]
            }
        )
        return TrustResponse(json.dumps(document).encode())

    async def legacy(request):
        legacy_calls.append(request)
        return True

    evaluator = TrustGrantEvaluator(
        TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0]), legacy_authorization=legacy
    )
    claim = {"agent_origin": ORIGIN, "brand_domain": "brand.com", "account_operator": "agency.com"}
    assert (await evaluator.check(**claim)).authorized
    for phase, when in (("removed", 1061), ("offline", 1122)):
        mode[0], now[0] = phase, when
        result = await evaluator.check(**claim, allow_stale_on_outage=True)
        assert not result.authorized
        assert result.error_code == "PERMISSION_DENIED"
        assert result.include_grant_absent_details
        assert result.reason == "grant_absent"
    assert not legacy_calls


@pytest.mark.parametrize("grant_expiry", [None, 1200])
async def test_grant_grace_checks_all_chain_records_and_hard_validity(grant_expiry):
    now, offline = [1000.0], [False]

    async def fetch(url, *, allow_private=False):
        if offline[0]:
            raise OSError("offline")
        now[0] += 10
        if url == trust_record_url(ORIGIN):
            document = {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}
        else:
            grant = {
                "grantee": "agency.com" if url == trust_record_url("brand.com") else "platform.com",
                "scopes": ["adcp:media_buying"],
            }
            if grant_expiry is not None:
                grant["valid_until"] = "1970-01-01T00:20:00Z"
            document = {"grants": [grant]}
        return TrustResponse(json.dumps(document).encode())

    evaluator = TrustGrantEvaluator(TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0]))
    claim = {"agent_origin": ORIGIN, "brand_domain": "brand.com", "account_operator": "agency.com"}
    initial = await evaluator.check(**claim)
    assert initial.authorized
    assert initial.expires_at == 1060
    assert initial.outage_expires_at == (grant_expiry or 1300)
    offline[0], now[0] = True, (grant_expiry or 1300) - 0.001
    grace = await evaluator.check(**claim, allow_stale_on_outage=True)
    assert grace.authorized and grace.stale_on_outage
    assert grace.outage_expires_at == initial.outage_expires_at
    now[0] = grant_expiry or 1300
    expired = await evaluator.check(**claim, allow_stale_on_outage=True)
    assert not expired.authorized
    assert expired.error_code == ("PERMISSION_DENIED" if grant_expiry else "SERVICE_UNAVAILABLE")


async def test_shared_grantor_latest_withdrawal_overrides_older_claim_snapshot():
    now, mode = [1000.0], ["allowed"]

    async def fetch(url, *, allow_private=False):
        if mode[0] == "offline":
            raise OSError("offline")
        document = (
            {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}
            if url == trust_record_url(ORIGIN)
            else {
                "exclusive_grants": True,
                "grants": [
                    {
                        "grantee": "platform.com" if mode[0] == "allowed" else "other.com",
                        "scopes": ["adcp:media_buying"],
                    }
                ],
            }
        )
        return TrustResponse(json.dumps(document).encode())

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    evaluator = TrustGrantEvaluator(records)
    claim = {
        "agent_origin": ORIGIN,
        "brand_domain": "brand.com",
        "account_operator": "platform.com",
    }
    assert (await evaluator.check(**claim)).authorized
    mode[0], now[0] = "withdrawn", 1061
    await records.evaluate("brand.com")  # Another account refreshes this grantor.
    mode[0], now[0] = "offline", 1122
    result = await evaluator.check(**claim, allow_stale_on_outage=True)
    assert not result.authorized
    assert result.error_code == "PERMISSION_DENIED"
    assert result.reason == "grant_absent"


async def test_captured_listing_cannot_approve_after_other_origin_refresh_delists_it():
    now, delisted = [1000.0], [False]
    started, release = asyncio.Event(), asyncio.Event()
    other_origin = "https://other.platform.com"

    async def fetch(url, *, allow_private=False):
        if url == trust_record_url("brand.com"):
            started.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            document = {"grants": [{"grantee": "platform.com", "scopes": ["adcp:media_buying"]}]}
        elif url == trust_record_url(ORIGIN):
            origins = [other_origin] if delisted[0] else [ORIGIN, other_origin]
            document = {"agents": [{"url": origin, "roles": ["adcp:buying"]} for origin in origins]}
        else:
            document = {"agents": []}
        return TrustResponse(json.dumps(document).encode())

    records = TrustRecordEvaluator(fetcher=fetch, max_records=2, clock=lambda: now[0])
    captured = await records.evaluate(ORIGIN, role="adcp:buying")
    claim = {
        "agent_origin": ORIGIN,
        "brand_domain": "brand.com",
        "account_operator": "platform.com",
    }
    pending = asyncio.create_task(TrustGrantEvaluator(records).check(**claim))
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        # Evict transport/cache entries while the original snapshot remains
        # inside its TTL, allowing an independent origin to read newer policy.
        await records.evaluate("other.com")
        await records.evaluate("third.com")
        delisted[0], now[0] = True, 1001
        newer = await records.evaluate(other_origin, role="adcp:buying")
        assert newer.revision > captured.revision
        assert not await records.snapshots_current((captured,))
        release.set()
        result = await asyncio.wait_for(pending, timeout=5)
        assert not result.authorized
        assert result.error_code == "PERMISSION_DENIED"
        assert not result.include_grant_absent_details
        with pytest.raises(TrustRecordMatchError):
            await records.validate_snapshots(
                (captured,), origin_or_domain=ORIGIN, role="adcp:buying"
            )
    finally:
        release.set()
        if not pending.done():
            await asyncio.wait_for(pending, timeout=5)


@pytest.mark.parametrize("signing_profile", ["web-bot-auth", LEGACY_PROFILE])
async def test_brand_publication_during_legacy_callback_supersedes_old_fallback(signing_profile):
    now, published = [1000.0], [False]
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(url, *, allow_private=False):
        if url == trust_record_url(ORIGIN):
            document = {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}
        elif not published[0]:
            return TrustResponse(b"", 404)
        else:
            document = {"grants": []}
        return TrustResponse(json.dumps(document).encode())

    async def legacy(request):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return True

    records = TrustRecordEvaluator(
        fetcher=fetch, polling_interval_seconds=1800, clock=lambda: now[0]
    )
    evaluator = TrustGrantEvaluator(records, legacy_authorization=legacy)
    pending = asyncio.create_task(
        evaluator.check(
            agent_origin=ORIGIN,
            brand_domain="brand.com",
            account_operator="platform.com",
            signing_profile=signing_profile,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        # The negative retry cache expires while the operator remains fresh.
        published[0], now[0] = True, 1061
        await records.evaluate("brand.com")
        release.set()
        result = await asyncio.wait_for(pending, timeout=5)
        assert not result.authorized
        assert result.error_code == "SERVICE_UNAVAILABLE"
        assert result.reason == "trust_record_changed_during_evaluation"
        assert not result.legacy_fallback_allowed
    finally:
        release.set()
        if not pending.done():
            await asyncio.wait_for(pending, timeout=5)


@pytest.mark.parametrize("status", [404, 410, 500, 503, 429, 401, 403, 302])
async def test_production_trust_fetch_preserves_error_status_without_consuming_body(
    monkeypatch, status
):
    consumed = []

    class UnreadableErrorPage(httpx.AsyncByteStream):
        async def __aiter__(self):
            consumed.append(True)
            raise AssertionError("Error response bodies must not be consumed")
            yield b""

    def respond(request):
        return httpx.Response(
            status,
            headers={"content-encoding": "gzip", "content-length": str(3 * 1024 * 1024)},
            stream=UnreadableErrorPage(),
        )

    monkeypatch.setattr(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        lambda url, *, allow_private: httpx.MockTransport(respond),
    )
    response = await fetch_trust_record(trust_record_url("brand.com"))
    assert response.status == status
    assert response.body == b""
    assert not consumed


@pytest.mark.parametrize("status", [404, 410, 500, 503, 429, 401, 403, 302])
async def test_custom_fetcher_error_page_preserves_removal_or_outage_grace(status):
    now, fail = [1000.0], [False]

    async def fetch(url, *, allow_private=False):
        if url == trust_record_url(ORIGIN):
            document = {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}
        elif fail[0]:
            return TrustResponse(b"x" * (3 * 1024 * 1024), status)
        else:
            document = {"grants": [{"grantee": "platform.com", "scopes": ["adcp:media_buying"]}]}
        return TrustResponse(json.dumps(document).encode())

    evaluator = TrustGrantEvaluator(TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0]))
    claim = {
        "agent_origin": ORIGIN,
        "brand_domain": "brand.com",
        "account_operator": "platform.com",
    }
    assert (await evaluator.check(**claim)).authorized
    fail[0], now[0] = True, 1061
    fresh = await evaluator.check(**claim)
    assert not fresh.authorized
    grace = await evaluator.check(**claim, allow_stale_on_outage=True)
    if status in {404, 410}:
        assert fresh.error_code == grace.error_code == "PERMISSION_DENIED"
        assert fresh.reason == grace.reason == "grant_absent"
        assert fresh.include_grant_absent_details and grace.include_grant_absent_details
    else:
        assert fresh.error_code == "SERVICE_UNAVAILABLE"
        assert grace.authorized and grace.stale_on_outage
        assert grace.outage_expires_at == 1300


@pytest.mark.parametrize(
    "guard_headers,read_timeout,total_deadline",
    [
        ({"content-encoding": "gzip"}, False, False),
        ({"content-length": str(3 * 1024 * 1024)}, False, False),
        ({}, True, False),
        ({}, False, True),
    ],
)
async def test_rejected_production_200_still_supersedes_legacy_fallback(
    monkeypatch, guard_headers, read_timeout, total_deadline
):
    now, removed, legacy_calls = [1000.0], [False], []
    cancelled = []

    if total_deadline:
        original_wait_for = asyncio.wait_for

        async def shortened_deadline(awaitable, timeout):
            return await original_wait_for(awaitable, timeout=1 if timeout == 10 else timeout)

        monkeypatch.setattr("adcp.signing.trust.asyncio.wait_for", shortened_deadline)

    class GuardedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            if read_timeout:
                raise httpx.ReadTimeout("body stalled after HTTP 200")
            if total_deadline:
                try:
                    # Independent bound: removing the production deadline must
                    # fail this fixture promptly instead of hanging CI.
                    await original_wait_for(asyncio.Event().wait(), timeout=5)
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
            pytest.fail("The 200 body guard must reject before consuming bytes")
            yield b""

    def respond(request):
        return httpx.Response(
            404 if removed[0] else 200, headers=guard_headers, stream=GuardedBody()
        )

    monkeypatch.setattr(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        lambda url, *, allow_private: httpx.MockTransport(respond),
    )

    async def legacy(request):
        legacy_calls.append(request)
        return True

    evaluator = TrustGrantEvaluator(
        TrustRecordEvaluator(clock=lambda: now[0]), legacy_authorization=legacy
    )
    claim = {
        "agent_origin": ORIGIN,
        "brand_domain": "brand.com",
        "account_operator": "platform.com",
        "signing_profile": LEGACY_PROFILE,
    }
    rejected = await evaluator.check(**claim)
    assert not rejected.authorized
    assert rejected.error_code == (
        "SERVICE_UNAVAILABLE" if read_timeout or total_deadline else "PERMISSION_DENIED"
    )
    assert bool(cancelled) == total_deadline
    assert not rejected.include_grant_absent_details
    removed[0], now[0] = True, 1061
    removal = await evaluator.check(**claim)
    assert not removal.authorized
    assert removal.reason == "grant_absent"
    assert not removal.legacy_fallback_allowed
    assert not legacy_calls
