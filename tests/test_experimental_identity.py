"""Trust records, grant chains, revocation retention and experimental types."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from adcp.decisioning.trust_grants import TrustGrantClaim, TrustGrantGate
from adcp.decisioning.types import AdcpError
from adcp.signing import (
    RevocationDetails,
    TrustGrantEvaluator,
    TrustRecordAbsentError,
    TrustRecordError,
    TrustRecordEvaluator,
    TrustResponse,
    trust_record_url,
    validate_trust_record,
)
from adcp.signing.revocation import RevocationList
from adcp.types.experimental_identity import (
    AccountCapabilities,
    BuyerOnboardingPosture,
    GetPrincipalResponse,
    GrantAbsentDetails,
    PrincipalChangedWebhook,
    TrustRecord,
)

ORIGIN = "https://buyer.platform.com"
SIGNER_RECORD = {"agents": [{"url": ORIGIN + "/mcp", "roles": ["adcp:buying"]}]}


def records(documents, clock=lambda: 1000):
    calls = []

    async def fetch(url, *, allow_private=False):
        calls.append(url)
        document = documents.get(url)
        if isinstance(document, Exception):
            raise document
        if isinstance(document, TrustResponse):
            return document
        return (
            TrustResponse(json.dumps(document).encode())
            if document is not None
            else TrustResponse(b"", 404)
        )

    return TrustRecordEvaluator(fetcher=fetch, clock=clock), calls


async def test_trust_record_expiring_during_fetch_never_grants_implicit_authority():
    now = [1000]

    async def fetch(url, *, allow_private=False):
        now[0] += 61
        return TrustResponse(json.dumps(SIGNER_RECORD).encode())

    evaluator = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    with pytest.raises(TrustRecordError, match="expired"):
        await evaluator.evaluate(ORIGIN, role="adcp:buying")
    result = await TrustGrantEvaluator(evaluator).check(
        agent_origin=ORIGIN,
        brand_domain="platform.com",
        account_operator="platform.com",
    )
    assert not result.authorized


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://buyer.agency.co.uk/mcp", "https://agency.co.uk/.well-known/trust.json"),
        ("https://buyer.team.github.io/mcp", "https://team.github.io/.well-known/trust.json"),
        ("https://xn--bcher-kva.de/mcp", "https://xn--bcher-kva.de/.well-known/trust.json"),
    ],
)
def test_pinned_psl(url, expected):
    assert trust_record_url(url) == expected


@pytest.mark.parametrize("origin", ["https://localhost/mcp", "https://127.0.0.1", "https://co.uk"])
def test_no_operator_domain(origin):
    with pytest.raises(TrustRecordError):
        trust_record_url(origin)


@pytest.mark.parametrize(
    "document",
    [
        {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"], "jwks_uri": ORIGIN + "/keys"}]},
        {
            "agents": [
                {"url": ORIGIN, "roles": ["adcp:buying"]},
                {"url": ORIGIN + "/mcp", "roles": ["adcp:sales"]},
            ]
        },
        {"agents": [{"url": "https://attacker.com", "roles": ["adcp:buying"]}]},
        {"agents": [{"url": "https://user@platform.com", "roles": ["adcp:buying"]}]},
        {"grants": [{"grantee": "seat.agency.com", "scopes": ["adcp:media_buying"]}]},
    ],
)
def test_invalid_records(document):
    with pytest.raises(TrustRecordError):
        validate_trust_record(document, operator_domain="platform.com")


async def test_hosted_record_preserves_owner_and_requires_role():
    evaluator, calls = records(
        {
            trust_record_url(ORIGIN): {"authoritative_location": "https://hosting.com/trust.json"},
            "https://hosting.com/trust.json": SIGNER_RECORD,
        }
    )
    record = await evaluator.evaluate(ORIGIN, role="adcp:buying")
    assert record.operator_domain == "platform.com"
    assert len(calls) == 2
    record.document["agents"].clear()
    assert (await evaluator.evaluate(ORIGIN, role="adcp:buying")).agent(ORIGIN, role="adcp:buying")
    with pytest.raises(TrustRecordError, match="role"):
        await evaluator.evaluate(ORIGIN, role="adcp:governance")


async def test_no_second_hop_or_http_redirect():
    evaluator, _ = records(
        {
            trust_record_url(ORIGIN): {"authoritative_location": "https://hosting.com/trust.json"},
            "https://hosting.com/trust.json": {
                "authoritative_location": "https://third.com/trust.json"
            },
        }
    )
    with pytest.raises(TrustRecordError, match="second"):
        await evaluator.evaluate(ORIGIN)
    evaluator, _ = records({trust_record_url(ORIGIN): TrustResponse(b"", 302)})
    with pytest.raises(TrustRecordError):
        await evaluator.evaluate(ORIGIN)


@pytest.mark.parametrize(
    "body", [b'{"agents":[],"agents":[]}', b'{"agents":NaN}', b" " * (2 * 1024 * 1024 + 1)]
)
async def test_strict_json_and_size_cap(body):
    evaluator, _ = records({trust_record_url(ORIGIN): TrustResponse(body)})
    with pytest.raises(TrustRecordError):
        await evaluator.evaluate(ORIGIN)


async def test_expired_record_fails_closed_and_cannot_downgrade():
    now = [1000]
    documents = {trust_record_url(ORIGIN): SIGNER_RECORD}
    evaluator, calls = records(documents, lambda: now[0])
    await evaluator.evaluate(ORIGIN)
    await evaluator.evaluate(ORIGIN)
    assert len(calls) == 1
    now[0] = 1061
    documents[trust_record_url(ORIGIN)] = OSError("offline")
    with pytest.raises(TrustRecordError):
        await evaluator.evaluate(ORIGIN)
    documents[trust_record_url(ORIGIN)] = None
    with pytest.raises(TrustRecordError) as exc:
        await evaluator.evaluate(ORIGIN)
    assert not isinstance(exc.value, TrustRecordAbsentError)


def grant(grantee, **changes):
    return {"grantee": grantee, "scopes": ["adcp:media_buying"], **changes}


async def check(documents, operator="agency.com", **changes):
    evaluator, _ = records({trust_record_url(ORIGIN): SIGNER_RECORD, **documents})
    return await TrustGrantEvaluator(evaluator, clock=lambda: 1000).check(
        agent_origin=ORIGIN, brand_domain="brand.com", account_operator=operator, **changes
    )


async def test_implicit_and_direct_grants_need_operator_on_chain():
    evaluator, _ = records({trust_record_url(ORIGIN): SIGNER_RECORD})
    assert (
        await TrustGrantEvaluator(evaluator).check(
            agent_origin=ORIGIN, brand_domain="platform.com", account_operator="platform.com"
        )
    ).authorized
    docs = {trust_record_url("brand.com"): {"grants": [grant("platform.com")]}}
    assert (await check(docs, operator="platform.com")).authorized
    assert not (await check(docs)).authorized


async def test_two_link_intersection_and_expiration():
    docs = {
        trust_record_url("brand.com"): {
            "grants": [
                grant(
                    "agency.com",
                    countries=["US", "CA"],
                    profiles={"adcp": {"brands": ["brand_1"]}},
                    valid_until="1970-01-01T00:30:00Z",
                )
            ]
        },
        trust_record_url("agency.com"): {
            "grants": [
                grant(
                    "platform.com",
                    countries=["US"],
                    agents=[{"url": ORIGIN}],
                    valid_until="1970-01-01T00:20:00Z",
                )
            ]
        },
    }
    accepted = await check(docs, brand_id="brand_1", countries=["US"])
    assert accepted.authorized and accepted.expires_at <= 1200
    assert not (await check(docs, brand_id="other", countries=["US"])).authorized
    assert not (await check(docs, brand_id="brand_1", countries=["CA"])).authorized
    assert not (await check(docs, brand_id="brand_1", countries=[])).authorized
    docs[trust_record_url("agency.com")]["grants"][0]["valid_until"] = "1970-01-01T00:16:40Z"
    assert not (await check(docs, brand_id="brand_1", countries=["US"])).authorized


async def test_never_follow_three_links_or_operator_record_as_brand_authority():
    docs = {
        trust_record_url("brand.com"): {"grants": [grant("agency.com")]},
        trust_record_url("agency.com"): {"grants": [grant("middle.com")]},
        trust_record_url("middle.com"): {"grants": [grant("platform.com")]},
    }
    assert not (await check(docs)).authorized
    docs[trust_record_url("brand.com")] = {"agents": []}
    docs[trust_record_url("agency.com")] = {"grants": [grant("platform.com")]}
    denied = await check(docs)
    assert not denied.authorized and not denied.legacy_fallback_allowed
    with pytest.raises(AdcpError) as exc:
        denied.require()
    assert exc.value.details == {"scope": "agent", "reason": "grant_absent"}


async def test_exclusive_scopes_and_no_legacy_after_200():
    docs = {
        trust_record_url(ORIGIN): SIGNER_RECORD,
        trust_record_url("brand.com"): {"grants": [grant("other.com")], "exclusive_grants": True},
    }
    called = []

    async def authorize(request):
        called.append(request)
        return True

    evaluator, _ = records(docs)
    authz = TrustGrantEvaluator(
        evaluator,
        clock=lambda: 1000,
        seller_authorization=authorize,
        legacy_authorization=authorize,
    )
    args = dict(agent_origin=ORIGIN, brand_domain="brand.com", account_operator="agency.com")
    assert not (await authz.check(**args)).authorized and not called
    docs[trust_record_url("brand.com")]["exclusive_grants"] = False
    evaluator, _ = records(docs)
    authz = TrustGrantEvaluator(
        evaluator,
        clock=lambda: 1000,
        seller_authorization=authorize,
        legacy_authorization=authorize,
    )
    assert (await authz.check(**args)).authorized and len(called) == 1


async def test_unestablished_grant_gate_omits_oracle_details():
    evaluator, _ = records({})
    gate = TrustGrantGate(
        TrustGrantEvaluator(evaluator),
        lambda account, agent: TrustGrantClaim("brand.com", "agency.com"),
    )
    with pytest.raises(AdcpError) as exc:
        await gate.require_claim(TrustGrantClaim("brand.com", "agency.com"), None)
    assert exc.value.details == {}


def test_preview_round_trips():
    from adcp.types.experimental_identity import AgentOnboardingMode, PrincipalRelationshipStatus

    response = GetPrincipalResponse.model_validate(
        {
            "result": {
                "kind": "unconfigured",
                "relationship": {"status": "pending_approval", "setup": {"message": "Apply"}},
            }
        }
    )
    assert response.result.relationship.status is PrincipalRelationshipStatus.pending_approval
    assert (
        response.model_dump(mode="json", exclude_none=True)["result"]["relationship"]["setup"][
            "message"
        ]
        == "Apply"
    )
    account = AccountCapabilities(
        supported_billing=["operator"], sandbox=True, agent_onboarding={"sandbox": {"mode": "open"}}
    )
    assert account.agent_onboarding.sandbox.mode == "open"
    assert BuyerOnboardingPosture(mode="open").mode is AgentOnboardingMode.open
    assert GrantAbsentDetails().model_dump() == {"scope": "agent", "reason": "grant_absent"}
    validate_trust_record(
        TrustRecord.model_validate(SIGNER_RECORD).model_dump(mode="json", exclude_none=True),
        operator_domain="platform.com",
    )
    validate_trust_record(
        {
            "agents": [
                {
                    "url": ORIGIN,
                    "roles": ["adcp:buying"],
                    "profiles": {"adcp": {"onboarding": {"mode": "application"}}},
                }
            ],
            "profiles": {"adcp": {"onboarding": {"mode": "invite_only"}}},
        },
        operator_domain="platform.com",
    )


@pytest.mark.parametrize("reason", ["relationship_changed", "relationship_expiring"])
def test_principal_webhook_reasons(reason):
    payload = {
        "idempotency_key": "0123456789abcdef",
        "notification_id": "n1",
        "fired_at": "2026-10-13T00:00:00Z",
        "subscriber_id": "s1",
        "agent_url": "https://seller.com",
        "changed_at": "2026-10-13T00:00:00Z",
        "reason": reason,
    }
    assert (
        PrincipalChangedWebhook.model_validate(payload).model_dump(mode="json")["reason"] == reason
    )


def test_revocation_retains_compromise_and_earliest_date():
    base = {
        "issuer": ORIGIN,
        "updated": "2026-10-13T00:00:00Z",
        "next_update": "2026-10-13T00:01:00Z",
    }
    first = RevocationList.from_dict(
        {
            **base,
            "revoked_kids": ["kid"],
            "revocation_details": {
                "kid": {"reason": "keyCompromise", "invalidity_date": "2026-10-11T00:00:00Z"}
            },
        }
    )
    newer = RevocationList.from_dict(
        {
            **base,
            "revoked_kids": ["kid"],
            "revocation_details": {
                "kid": {"reason": "superseded", "invalidity_date": "2026-10-12T00:00:00Z"}
            },
        }
    )
    newer.retain(first)
    assert newer.revocation_details["kid"].reason == "keyCompromise"
    assert newer.revocation_details["kid"].invalidity_date.day == 11
    withdrawn = RevocationList.from_dict(base)
    withdrawn.retain(newer)
    assert withdrawn.is_revoked("kid")
    assert withdrawn.revocation_details["kid"].exposes(datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not withdrawn.revocation_details["kid"].exposes(
        datetime(2026, 10, 10, tzinfo=timezone.utc)
    )
    assert RevocationDetails(reason="keyCompromise").exposes(datetime.now(timezone.utc))


async def test_grants_checked_before_account_upsert_and_existing_account_calls():
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from adcp.decisioning import (
        AuthInfo,
        BuyerAgent,
        DecisioningCapabilities,
        DecisioningPlatform,
        HttpSigCredential,
        InMemoryTaskRegistry,
        SingletonAccounts,
        signing_only_registry,
    )
    from adcp.decisioning.handler import PlatformHandler
    from adcp.server import ToolContext
    from adcp.types import GetProductsRequest

    touched = []

    class Accounts(SingletonAccounts):
        def upsert_request(self, request, ctx=None):
            touched.append("upsert")
            return {"accounts": []}

    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = Accounts(account_id="existing")

        async def get_products(self, req, ctx):
            touched.append("products")
            return {"products": []}

    buyer = BuyerAgent(agent_url=ORIGIN, display_name="Buyer", status="active")

    async def lookup(agent_url):
        return buyer

    evaluator, _ = records(
        {trust_record_url(ORIGIN): SIGNER_RECORD, trust_record_url("brand.com"): {"agents": []}}
    )
    gate = TrustGrantGate(
        TrustGrantEvaluator(evaluator, clock=lambda: 1000),
        lambda account, agent: TrustGrantClaim("brand.com", "platform.com"),
    )
    auth = AuthInfo(
        kind="http_sig",
        credential=HttpSigCredential(
            kind="http_sig", keyid="kid", agent_url=ORIGIN, verified_at=1000
        ),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            Platform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            buyer_agent_registry=signing_only_registry(lookup),
            trust_grant_gate=gate,
        )
        context = ToolContext(metadata={"adcp.auth_info": auth})
        with pytest.raises(AdcpError) as exc:
            await handler.sync_accounts(
                SimpleNamespace(
                    accounts=[{"brand": {"domain": "brand.com"}, "operator": "platform.com"}]
                ),
                context,
            )
        assert exc.value.details["reason"] == "grant_absent"
        assert not touched
        with pytest.raises(AdcpError):
            await handler.get_products(
                GetProductsRequest(buying_mode="brief", brief="any"), context
            )
        assert not touched


async def test_client_opt_in_preserves_relationship_response(monkeypatch):
    from unittest.mock import AsyncMock

    from adcp import ADCPClient, AgentConfig
    from adcp.types import GetPrincipalRequest
    from adcp.types.core import TaskResult, TaskStatus
    from adcp.types.experimental_identity import PrincipalRelationshipStatus

    payload = {"result": {"kind": "unconfigured", "relationship": {"status": "pending_approval"}}}
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.com/mcp", protocol="mcp"),
        experimental_agent_identity=True,
    )
    monkeypatch.setattr(
        client.adapter,
        "get_principal",
        AsyncMock(return_value=TaskResult(status=TaskStatus.COMPLETED, success=True, data=payload)),
    )
    result = await client.get_principal(GetPrincipalRequest())
    assert result.success
    assert isinstance(result.data, GetPrincipalResponse)
    assert result.data.result.relationship.status is PrincipalRelationshipStatus.pending_approval


async def test_client_preserves_unknown_onboarding_requirements_and_signing_profiles(monkeypatch):
    from unittest.mock import AsyncMock

    from adcp import ADCPClient, AgentConfig
    from adcp.signing.trust import validate_identity_value
    from adcp.types import GetAdcpCapabilitiesRequest
    from adcp.types.core import TaskResult, TaskStatus
    from adcp.types.experimental_identity import AgentOnboardingRequirementKind

    posture = {
        "message": "Contact our onboarding team for this requirement",
        "url": "https://seller.com/apply",
        "sandbox": {
            "mode": "application",
            "requirements": [{"kind": "grant"}, {"kind": "future_identity_badge"}],
        },
    }
    payload = {
        "adcp": {"major_versions": [3], "idempotency": {"supported": False}},
        "supported_protocols": ["media_buy"],
        "account": {
            "supported_billing": ["operator"],
            "sandbox": True,
            "agent_onboarding": posture,
        },
        "request_signing": {
            "supported": True,
            "covers_content_digest": "required",
            "profiles": ["adcp/request-signing/v1", "web-bot-auth"],
        },
    }
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.com/mcp", protocol="mcp"),
        experimental_agent_identity=True,
    )
    monkeypatch.setattr(
        client.adapter,
        "get_adcp_capabilities",
        AsyncMock(return_value=TaskResult(status=TaskStatus.COMPLETED, success=True, data=payload)),
    )
    result = await client.get_adcp_capabilities(GetAdcpCapabilitiesRequest())
    assert result.success
    onboarding = result.data.account.agent_onboarding
    assert onboarding.sandbox.requirements[0].kind is AgentOnboardingRequirementKind.grant
    assert onboarding.sandbox.requirements[1].kind == "future_identity_badge"
    assert onboarding.message == posture["message"]
    assert str(onboarding.url) == posture["url"]
    assert result.data.request_signing.profiles == payload["request_signing"]["profiles"]
    # Publishing validation stays strict even when readers preserve future kinds.
    with pytest.raises(TrustRecordError):
        validate_identity_value(posture, "core/agent-onboarding.json")


@pytest.mark.parametrize("record_level", [False, True])
async def test_trust_reader_retains_unknown_onboarding_requirements(record_level):
    posture = {
        "mode": "application",
        "requirements": [{"kind": "future_identity_badge", "future_property": "opaque"}],
        "message": "Ask a human to review this application",
        "url": "https://platform.com/apply",
    }
    document = {"agents": [{"url": ORIGIN, "roles": ["adcp:buying"]}]}
    profiles = {"adcp": {"onboarding": posture}}
    if record_level:
        document["profiles"] = profiles
    else:
        document["agents"][0]["profiles"] = profiles
    evaluator, _ = records({trust_record_url(ORIGIN): document})
    record = await evaluator.evaluate(ORIGIN, role="adcp:buying")
    parsed_profiles = (
        record.document["profiles"] if record_level else record.document["agents"][0]["profiles"]
    )
    assert parsed_profiles["adcp"]["onboarding"] == posture
    with pytest.raises(TrustRecordError):
        validate_trust_record(document, operator_domain="platform.com")
    # Forward-compatible posture reading still rejects invalid agent authority.
    document["agents"][0]["roles"] = ["unknown:authority"]
    bad_evaluator, _ = records({trust_record_url(ORIGIN): document})
    with pytest.raises(TrustRecordError):
        await bad_evaluator.evaluate(ORIGIN, role="adcp:buying")


async def test_relationship_hook_does_not_create_principal():
    from adcp.server.base import ToolContext
    from adcp.server.principal import (
        InMemoryPrincipalRecordStore,
        PrincipalIdentity,
        PrincipalService,
    )

    calls = []

    async def relationship(identity):
        calls.append(identity.subject)
        return {"status": "pending_approval", "setup": {"message": "Apply"}}

    store = InMemoryPrincipalRecordStore()
    service = PrincipalService(store, relationship_resolver=relationship)
    response = await service.get_principal(
        PrincipalIdentity("https://buyer.platform.com", "buyer_agent"),
        context=ToolContext(resolved_adcp_version="3.3"),
    )
    assert response.result.kind == "unconfigured"
    assert (
        response.model_dump(mode="json")["result"]["relationship"]["status"] == "pending_approval"
    )
    assert calls == [ORIGIN]
    assert await store.get(ORIGIN) is None


@pytest.mark.parametrize(
    "target", ["https://hosting.com:99999/trust.json", "https://user@hosting.com/trust.json"]
)
async def test_untrusted_pointer_invalid_authority_is_a_closed_trust_failure(target):
    evaluator, calls = records({trust_record_url(ORIGIN): {"authoritative_location": target}})
    with pytest.raises(TrustRecordError):
        await evaluator.evaluate(ORIGIN)
    assert len(calls) == 1


async def test_trust_grants_replace_legacy_brand_authorization_gate():
    from concurrent.futures import ThreadPoolExecutor

    from adcp.decisioning import (
        AuthInfo,
        BuyerAgent,
        DecisioningCapabilities,
        DecisioningPlatform,
        HttpSigCredential,
        InMemoryTaskRegistry,
        SingletonAccounts,
        signing_only_registry,
    )
    from adcp.decisioning.brand_authz_gate import BrandAuthorizationGate, BrandIdentity
    from adcp.decisioning.handler import PlatformHandler
    from adcp.server import ToolContext

    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="existing")

    class Legacy:
        async def is_authorized(self, **kwargs):
            pytest.fail("brand.json must not authorize after a successful trust.json fetch")

    buyer = BuyerAgent(agent_url=ORIGIN, display_name="Buyer", status="active")

    async def lookup(agent_url):
        return buyer

    evaluator, _ = records(
        {
            trust_record_url(ORIGIN): SIGNER_RECORD,
            trust_record_url("brand.com"): {"grants": [grant("platform.com")]},
        }
    )
    gate = TrustGrantGate(
        TrustGrantEvaluator(evaluator, clock=lambda: 1000),
        lambda account, agent: TrustGrantClaim("brand.com", "platform.com"),
    )
    context = ToolContext(
        metadata={
            "adcp.auth_info": AuthInfo(
                kind="http_sig",
                credential=HttpSigCredential(
                    kind="http_sig", keyid="kid", agent_url=ORIGIN, verified_at=1000
                ),
            )
        }
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            Platform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            buyer_agent_registry=signing_only_registry(lookup),
            trust_grant_gate=gate,
            brand_authorization_gate=BrandAuthorizationGate(
                resolver=Legacy(),
                extract_identity=lambda account, agent: BrandIdentity("brand.com"),
            ),
        )
        account = await handler._resolve_account(None, context, tool_name="get_products")
        assert account.id.startswith("existing")


async def test_grant_expiring_during_intermediary_fetch_is_not_authorized():
    now = [1000]
    docs = {
        trust_record_url(ORIGIN): SIGNER_RECORD,
        trust_record_url("brand.com"): {
            "grants": [grant("agency.com", valid_until="1970-01-01T00:16:50Z")]
        },
        trust_record_url("agency.com"): {"grants": [grant("platform.com")]},
    }

    async def fetch(url, *, allow_private=False):
        if url == trust_record_url("agency.com"):
            now[0] = 1011  # The first link expired while discovering the second.
        return TrustResponse(json.dumps(docs[url]).encode())

    evaluator = TrustGrantEvaluator(TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0]))
    result = await evaluator.check(
        agent_origin=ORIGIN, brand_domain="brand.com", account_operator="agency.com"
    )
    assert not result.authorized
