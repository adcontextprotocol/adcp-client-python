"""Identity, optional account decoration, truthful capability, and URL boundaries."""

from __future__ import annotations

import re
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from pydantic import AnyUrl, BaseModel

from adcp.decisioning import DecisioningPlatform
from adcp.decisioning.accounts import ResolveContext
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.handler import PlatformHandler, _build_list_accounts_filter
from adcp.decisioning.registry import (
    ApiKeyCredential,
    BuyerAgent,
    HttpSigCredential,
    OAuthCredential,
)
from adcp.decisioning.task_registry import InMemoryTaskRegistry
from adcp.reporting.outbox import (
    ReportingActivityProjector,
    ReportingActivitySupport,
    ReportingNotificationError,
    ReportingNotificationSubscription,
    resolve_reporting_consumer,
    sanitize_activity_url,
)
from adcp.reporting.outbox.support import validate_activity_claims
from adcp.server import ToolContext
from adcp.types import ListAccountsRequest

from ._reliable_support import notification_subscription
from .test_reporting_webhook_activity import prepare

PRINCIPAL = "https://buyer.example/agent"
CANONICAL_BOUND_HOST = "https://receiver.example.test/"
ACTIVITY_URL_BOUND = 8192


def agent(value=PRINCIPAL):
    return BuyerAgent(agent_url=value, display_name="Buyer", status="active")


@pytest.mark.parametrize("source", ["auth", "registry", "api_key", "oauth", "signed", "equal_all"])
def test_identity_accepts_either_trusted_source_and_checks_all_signed_coordinates(source):
    info = None
    buyer = None
    if source == "auth":
        info = AuthInfo(kind="bearer", principal=PRINCIPAL)
    elif source == "registry":
        buyer = agent()
    elif source in {"api_key", "oauth"}:
        credential = (
            ApiKeyCredential(kind="api_key", key_id="key-1")
            if source == "api_key"
            else OAuthCredential(kind="oauth", client_id="client-1")
        )
        info = AuthInfo(kind="bearer", principal=None, credential=credential)
        buyer = agent()
    else:
        info = AuthInfo(
            kind="http_sig",
            credential=HttpSigCredential(
                kind="http_sig",
                keyid="key-1",
                agent_url=PRINCIPAL,
                verified_at=1,
            ),
        )
        if source == "equal_all":
            info.principal = info.agent_url = PRINCIPAL
            buyer = agent()
    assert resolve_reporting_consumer(auth_info=info, agent=buyer) == PRINCIPAL


@pytest.mark.parametrize("source", ["principal", "agent_url", "credential", "registry"])
def test_every_contradictory_trusted_identity_is_rejected_without_echo(source):
    info = AuthInfo(kind="bearer", principal=PRINCIPAL)
    info.agent_url = PRINCIPAL
    info.credential = HttpSigCredential(
        kind="http_sig", keyid="key-1", agent_url=PRINCIPAL, verified_at=1
    )
    buyer = agent()
    if source == "registry":
        buyer = agent("FOREIGN_SECRET")
    elif source == "credential":
        info.credential = replace(info.credential, agent_url="FOREIGN_SECRET")
    else:
        setattr(info, source, "FOREIGN_SECRET")
    with pytest.raises(ReportingNotificationError, match="activity_identity_conflict") as caught:
        resolve_reporting_consumer(auth_info=info, agent=buyer)
    assert "FOREIGN_SECRET" not in str(caught.value)


@pytest.mark.parametrize(
    "value", [None, "", " ", "anonymous", "ANONYMOUS", "Anon", "NULL", "none", "unauthenticated"]
)
def test_absent_and_anonymous_identity_fails_closed(value):
    with pytest.raises(ReportingNotificationError, match="activity_identity_required"):
        resolve_reporting_consumer(auth_info=AuthInfo(kind="bearer", principal=value))


@pytest.mark.parametrize("source", ["principal", "agent_url", "credential", "registry"])
def test_present_anonymous_sentinel_is_not_hidden_by_another_valid_identity(source):
    info = AuthInfo(kind="bearer", principal=PRINCIPAL)
    buyer = agent()
    if source == "registry":
        buyer = agent("ANONYMOUS")
    elif source == "credential":
        info.credential = HttpSigCredential(
            kind="http_sig", keyid="k", agent_url="ANONYMOUS", verified_at=1
        )
    else:
        setattr(info, source, "ANONYMOUS")
    with pytest.raises(ReportingNotificationError, match="activity_identity_required"):
        resolve_reporting_consumer(auth_info=info, agent=buyer)


@pytest.mark.parametrize(
    "segment",
    [
        "c5b1b9b3-3e99-4da8-b930-6a9e67245553",
        "a" * 48,
        pytest.param(None, id="synthetic-jwt"),
        "aGVsbG9TZWNyZXRUYXJnZXRUb2tlbkFiQ0QxMjM0NTY=",
        "api_key_Live123MixedSecret",
        "%55%52%4c%5f%53%45%43%52%45%54",
        "%252fSECRET%252f",
        "shortpassword",
        "secret;param=PRIVATE",
        "a%2Fb%3Fc",
        "秘密令牌",
        "12345678901234567890",
    ],
)
def test_sanitizer_redacts_tokens_and_encoded_segments_with_constant(segment):
    if segment is None:
        # Build an invalid JWT-shaped segment without embedding a credential.
        segment = ".".join(
            urlsafe_b64encode(part).decode("ascii").rstrip("=")
            for part in (b'{"alg":"ES256"}', b'{"sub":"sanitizer-fixture"}', bytes(8))
        )
        assert re.fullmatch(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", segment)
    raw = f"https://user:USERINFO_SECRET@example.test:443/api/v1/webhooks/{segment}/reporting?QUERY_SECRET=x#FRAGMENT_SECRET"
    value = sanitize_activity_url(raw)
    assert value == "https://example.test/api/v1/webhooks/redacted/reporting"
    assert str(AnyUrl(value)) == value
    assert sanitize_activity_url(value) == value
    assert all(
        secret not in value
        for secret in (segment, "USERINFO_SECRET", "QUERY_SECRET", "FRAGMENT_SECRET")
    )


@pytest.mark.parametrize(
    "url",
    ["INVALID_URL_SECRET", "https://host:INVALID_PORT_SECRET/x", "https://host/" + "x" * 8192],
)
def test_invalid_url_and_subscription_errors_are_bounded_and_secret_free(url):
    with pytest.raises(ReportingNotificationError) as caught:
        sanitize_activity_url(url)
    assert str(caught.value) == "invalid_activity_url"
    assert caught.value.__context__ is None
    with pytest.raises(ReportingNotificationError) as caught:
        notification_subscription(url=url)
    assert str(caught.value) == "invalid_configuration"
    assert caught.value.__context__ is None


def test_registration_rejects_a_url_only_the_canonical_form_exceeds():
    # Percent-encoding expands the stored form. A configuration accepted here
    # would otherwise quarantine every expansion instead of failing closed at
    # registration, so the bound must hold on the canonical value.
    url = CANONICAL_BOUND_HOST + "PATH_SECRET" + " " * 8000
    assert len(url) <= ACTIVITY_URL_BOUND < len(str(httpx.URL(url)))
    with pytest.raises(ReportingNotificationError) as caught:
        notification_subscription(url=url)
    assert str(caught.value) == "invalid_configuration"
    assert caught.value.__context__ is None
    assert "PATH_SECRET" not in repr(caught.value)


def test_registration_keeps_a_canonical_url_exactly_on_the_bound():
    url = CANONICAL_BOUND_HOST + "a" * (ACTIVITY_URL_BOUND - len(CANONICAL_BOUND_HOST))
    subscription = notification_subscription(url=url)
    assert subscription.url == url and len(subscription.url) == ACTIVITY_URL_BOUND
    # Expansion and envelope reopening re-run this validator on the stored
    # value, so an accepted registration stays accepted and sanitizes.
    ReportingNotificationSubscription.__post_init__(subscription)
    assert subscription.url == url
    assert sanitize_activity_url(subscription.url) == CANONICAL_BOUND_HOST + "redacted"


class ReadSpy:
    def __init__(self):
        self.calls = []

    async def list_activity(self, **kwargs):
        self.calls.append(kwargs)
        return ()


class Envelope(BaseModel):
    accounts: list[dict]
    pagination: dict
    context: dict


@pytest.mark.parametrize("shape", ["list", "dict", "pydantic"])
@pytest.mark.parametrize(
    "mounted,requested", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_legacy_filters_shapes_nulls_and_three_state_projection(shape, mounted, requested):
    source = [
        {
            "account_id": "acct_a",
            "name": "Visible",
            "status": "active",
            "webhook_activity": [{"url": "ADOPTER_SECRET"}],
            "billing_entity": {"legal_name": "Buyer", "bank": {"iban": "BANK_SECRET"}},
        }
    ]
    envelope = {
        "accounts": source,
        "pagination": {"has_more": True, "cursor": "next"},
        "context": {"roundtrip": "same"},
    }
    seen = []

    class LegacyStore:
        # Deliberately legacy: no ctx parameter and no new required methods.
        def list(self, filter=None):
            seen.append(deepcopy(filter))
            if shape == "list":
                return source
            return envelope if shape == "dict" else Envelope(**envelope)

    class Platform(DecisioningPlatform):
        accounts = LegacyStore()

    spy = ReadSpy()
    projector = ReportingActivityProjector(spy) if mounted else None
    params = ListAccountsRequest(
        account={"account_id": "acct_a"},
        status="active",
        sandbox=False,
        pagination={"max_results": 7},
        include_webhook_activity=requested,
        webhook_activity_limit=2,
    )
    # Unsupported/unrequested legacy calls must not require an identity.
    context = (
        ToolContext(metadata={"adcp.auth_info": AuthInfo(kind="bearer", principal=PRINCIPAL)})
        if mounted and requested
        else ToolContext()
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            Platform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            account_activity=projector,
        )
        result = await handler.list_accounts(params, context)
    assert seen == [
        {
            "account": {"account_id": "acct_a"},
            "status": "active",
            "sandbox": False,
            "pagination": {"max_results": 7},
        }
    ]
    assert "ADOPTER_SECRET" not in str(result) and "BANK_SECRET" not in str(result)
    if mounted and requested:
        assert result["accounts"][0]["webhook_activity"] == []
        assert spy.calls == [{"account_id": "acct_a", "consumer_id": PRINCIPAL, "limit": 2}]
    else:
        assert "webhook_activity" not in result["accounts"][0] and spy.calls == []
    if shape != "list":
        assert (
            result["pagination"] == envelope["pagination"]
            and result["context"] == envelope["context"]
        )
    assert source[0]["webhook_activity"] == [{"url": "ADOPTER_SECRET"}]


async def test_exact_account_filter_controls_visible_accounts_before_activity_reads():
    class Store:
        def list(self, filter=None, ctx=None):
            visible = [{"account_id": "acct_a"}, {"account_id": "acct_b"}]
            return [row for row in visible if row["account_id"] == filter["account"]["account_id"]]

    class Platform(DecisioningPlatform):
        accounts = Store()

    spy = ReadSpy()
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            Platform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            account_activity=ReportingActivityProjector(spy),
        )
        for selected, expected in [("acct_b", ["acct_b"]), ("invisible", [])]:
            result = await handler.list_accounts(
                ListAccountsRequest(
                    account={"account_id": selected}, include_webhook_activity=True
                ),
                ToolContext(
                    metadata={"adcp.auth_info": AuthInfo(kind="bearer", principal=PRINCIPAL)}
                ),
            )
            assert [row["account_id"] for row in result["accounts"]] == expected
    assert [call["account_id"] for call in spy.calls] == ["acct_b"]


def test_complete_natural_key_filter_is_preserved():
    account = {
        "brand": {"domain": "brand.example"},
        "operator": "operator.example",
        "operator_unit": {"id": "seat-1"},
        "currency": "USD",
        "timezone": "UTC",
        "sandbox": True,
    }
    request = ListAccountsRequest(
        account=account, include_webhook_activity=True, webhook_activity_limit=4
    )
    assert _build_list_accounts_filter(request) == {"account": account}


@pytest.mark.parametrize(
    "credential",
    [
        ApiKeyCredential(kind="api_key", key_id="key-1"),
        OAuthCredential(kind="oauth", client_id="client-1"),
    ],
)
@pytest.mark.parametrize("disagree", [False, True])
async def test_handler_registry_migration_and_identity_disagreement(credential, disagree):
    class Registry:
        async def resolve_by_credential(self, value):
            return agent()

        async def resolve_by_agent_url(self, value):
            return agent()

    class Store:
        def list(self, filter=None, ctx=None):
            assert ctx.agent.agent_url == PRINCIPAL
            return [{"account_id": "acct_a"}]

    class Platform(DecisioningPlatform):
        accounts = Store()

    info = AuthInfo(kind="bearer", credential=credential)
    if disagree:
        info.principal = "different-principal"
    spy = ReadSpy()
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            Platform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            buyer_agent_registry=Registry(),
            account_activity=ReportingActivityProjector(spy),
        )
        request = ListAccountsRequest(include_webhook_activity=True)
        context = ToolContext(metadata={"adcp.auth_info": info})
        if disagree:
            with pytest.raises(ReportingNotificationError, match="activity_identity_conflict"):
                await handler.list_accounts(request, context)
            assert spy.calls == []
        else:
            assert (await handler.list_accounts(request, context))["accounts"][0][
                "webhook_activity"
            ] == []
            assert spy.calls[0]["consumer_id"] == PRINCIPAL


@pytest.mark.parametrize(
    "result", [{"errors": [{"code": "DENIED"}], "context": {"a": 1}}, {"accounts": []}, None]
)
async def test_optional_decorator_preserves_error_and_empty_envelopes(result):
    spy = ReadSpy()
    projector = ReportingActivityProjector(spy)
    actual = await projector.enrich(
        result,
        context=ResolveContext(auth_info=AuthInfo(kind="bearer", principal=PRINCIPAL)),
        include=True,
    )
    assert actual == result and spy.calls == []


@pytest.mark.parametrize(
    "mode", ["none", "outbox_only", "missing_projection", "missing_account", "full"]
)
async def test_independent_capability_matrix_and_contradictory_overrides(
    notification_harness, mode
):
    h = notification_harness
    outbox, worker = await prepare(h)
    projector = ReportingActivityProjector(outbox)
    if mode == "outbox_only":
        worker.activity = None
    support = (
        None
        if mode == "none"
        else ReportingActivitySupport(
            worker, h.reliable.store, None if mode == "missing_projection" else projector
        )
    )
    account_mount = projector if mode == "full" else None
    durable = h.reliable.blobs.pool is not None and mode in {"missing_account", "full"}
    expected = {"reporting": durable, "account_notifications": durable and mode == "full"}
    if support is not None:
        assert await support.capability_flags(account_activity=account_mount) == expected
    for reporting, account in [(False, False), (True, False), (False, True), (True, True)]:
        response = {
            "media_buy": {
                "reporting_delivery": {"supports_webhook_activity": reporting},
                "relationship_notifications": {
                    "supported": True,
                    "supports_webhook_activity": True,
                },
            },
            "account": {"notifications": {"supports_webhook_activity": account}},
        }
        before = deepcopy(response)
        if (
            reporting
            and not expected["reporting"]
            or account
            and not expected["account_notifications"]
        ):
            with pytest.raises(ReportingNotificationError, match="activity_capability_requires"):
                await validate_activity_claims(
                    response, support=support, account_activity=account_mount, account_listing=True
                )
        else:
            await validate_activity_claims(
                response, support=support, account_activity=account_mount, account_listing=True
            )
        assert response == before


@pytest.mark.parametrize("source", ["static", "request"])
@pytest.mark.parametrize("claim", ["reporting", "account"])
@pytest.mark.parametrize("mounted", [False, True])
async def test_handler_boot_and_request_capability_gates_follow_projection_without_mutation(
    notification_harness, source, claim, mounted
):
    from adcp.decisioning import validate_capabilities_response_shape_async
    from adcp.decisioning.capabilities import Account, MediaBuy, WebhookSigning
    from adcp.types import ReportingDeliveryCapabilities
    from tests.test_decisioning_capabilities_projection import _SalesPlatform
    from tests.test_reporting_ledger import _OFFERING

    h = notification_harness
    outbox, worker = await prepare(h)
    projector = ReportingActivityProjector(outbox)
    support = ReportingActivitySupport(worker, h.reliable.store, projector)
    reporting = ReportingDeliveryCapabilities.model_validate(
        {
            "supported": True,
            # Required polling task names are explicit declarations. Generated
            # defaults must no longer silently advertise optional reporting work.
            "configuration_task": "sync_accounts",
            "status_task": "get_reporting_status",
            "offerings": [_OFFERING],
            "automated_recovery_window_seconds": 3600,
            "status_retention_days": 30,
            "supports_webhook_activity": claim == "reporting",
        }
    ).model_copy(update={"readiness_notification": None, "status_notification": None})
    capabilities = replace(
        _SalesPlatform.capabilities,
        experimental_features=["media_buy.reporting_delivery"],
        media_buy=MediaBuy(supported_pricing_models=["cpm"], reporting_delivery=reporting),
        account=Account.model_validate(
            {
                "supported_billing": ["operator"],
                "notifications": {
                    "supported": True,
                    "registration_task": "sync_accounts",
                    "read_task": "list_accounts",
                    "event_types": ["account.status_changed"],
                    "supports_webhook_activity": claim == "account",
                },
            }
        ),
        webhook_signing=WebhookSigning(
            supported=True,
            profile="adcp/webhook-signing/v1",
            algorithms=["ed25519"],
            delivery_retry_horizon_seconds=86400,
        ),
        webhook_signing_managed_externally=True,
    )

    class Listing:
        def list(self, filter=None):
            return []

    class Platform(_SalesPlatform):
        accounts = Listing()

        def get_adcp_capabilities_for_request(self, params=None, context=None):
            return capabilities if source == "request" else None

    platform = Platform()
    platform.capabilities = capabilities if source == "static" else _SalesPlatform.capabilities
    before = deepcopy(platform.capabilities)
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            platform,
            executor=executor,
            registry=InMemoryTaskRegistry(),
            reporting_activity=support if mounted else None,
            account_activity=projector if mounted else None,
            auto_emit_task_webhooks=False,
        )
        if mounted and h.reliable.blobs.pool is not None:
            await validate_capabilities_response_shape_async(handler)
            response = await handler.get_adcp_capabilities()
            block = (
                response["media_buy"]["reporting_delivery"]
                if claim == "reporting"
                else response["account"]["notifications"]
            )
            assert block["supports_webhook_activity"] is True
        else:
            with pytest.raises(ReportingNotificationError, match="activity_capability_requires"):
                await validate_capabilities_response_shape_async(handler)
    assert platform.capabilities == before


async def test_account_activity_claim_requires_account_listing_even_with_full_stack(
    notification_harness,
):
    h = notification_harness
    outbox, worker = await prepare(h)
    projector = ReportingActivityProjector(outbox)
    with pytest.raises(
        ReportingNotificationError, match="activity_capability_requires_account_projection"
    ):
        await validate_activity_claims(
            {"account": {"notifications": {"supports_webhook_activity": True}}},
            support=ReportingActivitySupport(worker, h.reliable.store, projector),
            account_activity=projector,
            account_listing=False,
        )


@pytest.mark.parametrize("all_accounts", [None, False, True])
def test_ledger_changed_requires_all_authorized_accounts(all_accounts):
    from adcp.server.principal import _normalize_notification_config
    from adcp.types import AgentNotificationConfig

    config = AgentNotificationConfig(
        subscriber_id="buyer",
        url="https://8.8.8.8/webhooks",
        event_types=["reporting.ledger_changed"],
        all_authorized_accounts=all_accounts,
    )
    if all_accounts is True:
        assert _normalize_notification_config(config, 0).all_authorized_accounts is True
    else:
        with pytest.raises(ValueError, match="all_authorized_accounts"):
            _normalize_notification_config(config, 0)
