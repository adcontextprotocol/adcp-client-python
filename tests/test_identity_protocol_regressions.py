"""Identity migration preserves legacy authority without allowing WBA downgrade."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

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
from adcp.signing import TrustGrantEvaluator, TrustRecordEvaluator, TrustResponse, trust_record_url

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
            assert error.value.details["reason"] == "grant_absent"
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
