"""AdCP 3.3 media-buy and webhook rules (#1644).

1. Flight start activates committed buys only: a provisional buy
   (``confirmed_at: null``) stays ``pending_start``.
2. ``revision`` advances on every committed change from any origin, and
   ``history`` entries for changes outside an AdCP task carry ``origin.kind``.
3. Reporting / artifact webhooks may omit ``authentication`` on requests served
   at 3.3; the seller then signs under the registration's ``signing_profile``.
   Below 3.3 it stays required.
4. Receivers ignore ``aggregated_totals`` in delivery webhooks.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft7Validator, FormatChecker

from adcp._version import resolve_adcp_version
from adcp.decisioning import (
    MEDIA_BUY_TRANSITIONS,
    OBSERVED_MEDIA_BUY_TRANSITIONS,
    PROVISIONAL_MEDIA_BUY_BLOCKED_STATUSES,
    AdcpError,
    assert_media_buy_transition,
    is_observable_media_buy_transition,
    media_buy_status_at_flight_start,
    record_media_buy_change,
)
from adcp.server import ADCPHandler, ToolContext
from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.server.mcp_tools import MCPToolSet
from adcp.signing import StaticJwksResolver, private_key_from_jwk
from adcp.types import ReportingWebhook
from adcp.types.domains.media_buy.create_media_buy_request import ArtifactWebhook
from adcp.validation.client_hooks import ValidationHookConfig
from adcp.webhooks import (
    WEBHOOK_SIGNING_PROFILE_ADCP,
    WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH,
    PreparedWebhook,
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookRegistrationError,
    WebhookSender,
    WebhookSigningProfileUnavailableError,
    WebhookVerifyOptions,
    create_mcp_webhook_payload,
    sign_webhook,
    to_wire_dict,
    webhook_signing_profile_for,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = resolve_adcp_version(None)
SCHEMAS = ROOT / "schemas" / "cache" / "3.3.0-beta.3"

_KEYS = json.loads(
    (
        Path(__file__).parent / "conformance" / "vectors" / "request-signing" / "keys.json"
    ).read_text()
)["keys"]
WEBHOOK_KEY = {
    **copy.deepcopy(next(k for k in _KEYS if k["kid"] == "test-ed25519-2026")),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}
URL = "https://buyer.example/reporting"
LEGACY_AUTH = {"schemes": ["Bearer"], "credentials": "buyer-reporting-token-1234567890"}
DELIVERY_REPORT: dict[str, Any] = {
    "notification_type": "scheduled",
    "reporting_period": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z"},
    "currency": "USD",
    "media_buy_deliveries": [
        {
            "media_buy_id": "mb_001",
            "status": "active",
            "totals": {"impressions": 1000, "spend": 45.0},
            "by_package": [],
        }
    ],
}


# ---------------------------------------------------------------------------
# Rule 1: flight start activates committed buys only
# ---------------------------------------------------------------------------


def test_media_buy_status_enum_says_provisional_buys_are_never_active() -> None:
    enum = json.loads((SCHEMAS / "enums" / "media-buy-status.json").read_text())
    assert "never `active`" in enum["description"]
    assert "MUST NOT be `active`" in enum["enumDescriptions"]["active"]


@pytest.mark.parametrize(
    ("status", "confirmed_at", "paused", "expected"),
    [
        ("pending_start", None, False, "pending_start"),
        ("pending_start", None, True, "pending_start"),
        ("pending_start", "2026-10-01T00:00:00Z", False, "active"),
        ("pending_start", datetime(2026, 10, 1, tzinfo=timezone.utc), True, "paused"),
        ("pending_creatives", "2026-10-01T00:00:00Z", False, "pending_creatives"),
        ("pending_creatives", None, False, "pending_creatives"),
        ("active", "2026-10-01T00:00:00Z", False, "active"),
        ("canceled", None, False, "canceled"),
    ],
)
def test_flight_start_moves_only_committed_pending_start_buys(
    status: str, confirmed_at: object, paused: bool, expected: str
) -> None:
    assert (
        media_buy_status_at_flight_start(
            status,
            confirmed_at=confirmed_at,  # type: ignore[arg-type]
            paused=paused,
        )
        == expected
    )


def test_provisional_buy_cannot_be_activated_on_request() -> None:
    with pytest.raises(AdcpError) as caught:
        assert_media_buy_transition(
            "pending_start", "active", media_buy_id="mb_1", provisional=True
        )
    assert caught.value.code == "INVALID_STATE"
    assert "provisional" in str(caught.value)
    assert caught.value.details == {
        "from_state": "pending_start",
        "to_state": "active",
        "media_buy_id": "mb_1",
    }
    # Committed: the same move is legal.
    assert_media_buy_transition("pending_start", "active", media_buy_id="mb_1")


@pytest.mark.parametrize("to_state", ["canceled", "rejected"])
def test_provisional_buy_can_still_be_canceled_or_rejected(to_state: str) -> None:
    assert_media_buy_transition("pending_start", to_state, provisional=True)
    assert_media_buy_transition("pending_creatives", to_state, provisional=True)
    assert is_observable_media_buy_transition("pending_start", to_state, provisional=True)


def test_provisional_buy_may_reach_pending_start_when_creatives_arrive() -> None:
    assert_media_buy_transition("pending_creatives", "pending_start", provisional=True)
    assert is_observable_media_buy_transition(
        "pending_creatives", "pending_start", provisional=True
    )


def test_provisional_buy_still_gets_the_graph_error_for_illegal_moves() -> None:
    with pytest.raises(AdcpError, match="terminal"):
        assert_media_buy_transition("completed", "active", provisional=True)


def test_observed_graph_closes_every_start_edge_for_provisional_buys() -> None:
    assert PROVISIONAL_MEDIA_BUY_BLOCKED_STATUSES == {"active", "paused"}
    for from_state, legal in OBSERVED_MEDIA_BUY_TRANSITIONS.items():
        for to_state in legal:
            committed = is_observable_media_buy_transition(from_state, to_state)
            provisional = is_observable_media_buy_transition(from_state, to_state, provisional=True)
            assert committed
            assert provisional is (to_state not in PROVISIONAL_MEDIA_BUY_BLOCKED_STATUSES)
    for from_state, legal in MEDIA_BUY_TRANSITIONS.items():
        for to_state in legal & PROVISIONAL_MEDIA_BUY_BLOCKED_STATUSES:
            with pytest.raises(AdcpError, match="provisional"):
                assert_media_buy_transition(from_state, to_state, provisional=True)


# ---------------------------------------------------------------------------
# Rule 2: revisions from any origin, history origin.kind
# ---------------------------------------------------------------------------


def _history_item_validator() -> Draft7Validator:
    bundle = json.loads(
        (SCHEMAS / "bundled" / "media-buy" / "get-media-buys-response.json").read_text()
    )
    item = bundle["properties"]["media_buys"]["items"]["properties"]["history"]["items"]
    return Draft7Validator(item, format_checker=FormatChecker())


@pytest.mark.parametrize(
    ("origin", "action", "extra"),
    [
        ("seller_operator", "updated_budget", {"actor": "ops team", "summary": "Budget +10%"}),
        ("seller_system", "paused", {"package_id": "pkg_1"}),
        ("connected_platform", "updated_dates", {"connection_id": "gam-network-42"}),
        ("adcp", "creative_assigned", {"package_id": "pkg_1", "ext": {"seller": {"n": 1}}}),
        ("unknown", "updated_packages", {}),
    ],
)
def test_recorded_change_advances_revision_and_carries_origin(
    origin: Any, action: str, extra: dict[str, Any]
) -> None:
    at = datetime(2026, 10, 5, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
    change = record_media_buy_change(7, action=action, origin=origin, timestamp=at, **extra)
    assert change.revision == 8
    entry = change.history_entry
    assert entry["revision"] == 8
    assert entry["action"] == action
    assert entry["origin"]["kind"] == origin
    assert entry["timestamp"] == "2026-10-05T16:00:00Z"
    if "connection_id" in extra:
        assert entry["origin"]["connection_id"] == extra["connection_id"]
    errors = list(_history_item_validator().iter_errors(entry))
    assert errors == []


def test_created_entry_starts_at_revision_one() -> None:
    change = record_media_buy_change(0, action="created", origin="adcp")
    assert change.revision == 1
    assert change.history_entry["timestamp"].endswith("Z")


def test_successive_external_changes_stay_monotonic() -> None:
    revision = 1
    for origin in ("seller_operator", "seller_system", "connected_platform", "adcp"):
        change = record_media_buy_change(revision, action="updated_budget", origin=origin)  # type: ignore[arg-type]
        assert change.revision == revision + 1
        revision = change.revision
    assert revision == 5


@pytest.mark.parametrize(
    "kwargs",
    [
        {"origin": "buyer"},
        {"origin": "seller_operator", "connection_id": "c1"},
        {"origin": "connected_platform", "connection_id": "x" * 256},
        {"origin": "seller_operator", "summary": "x" * 501},
        {"origin": "seller_operator", "action": ""},
        {"origin": "seller_operator", "timestamp": datetime(2026, 10, 5)},
    ],
)
def test_record_change_rejects_invalid_input(kwargs: dict[str, Any]) -> None:
    kwargs.setdefault("action", "updated_budget")
    with pytest.raises(ValueError):
        record_media_buy_change(1, **kwargs)


@pytest.mark.parametrize("current", [-1, True, 1.0])
def test_record_change_rejects_bad_current_revision(current: Any) -> None:
    with pytest.raises(ValueError, match="current_revision"):
        record_media_buy_change(current, action="paused", origin="seller_system")


# ---------------------------------------------------------------------------
# Rule 3: optional webhook authentication at 3.3, signing_profile
# ---------------------------------------------------------------------------


def test_reporting_webhook_schema_makes_authentication_optional() -> None:
    schema = json.loads((SCHEMAS / "core" / "reporting-webhook.json").read_text())
    assert schema["required"] == ["url", "reporting_frequency"]
    assert schema["not"] == {"required": ["authentication", "signing_profile"]}


@pytest.mark.parametrize(
    ("registration", "expected"),
    [
        ({"url": URL, "reporting_frequency": "daily"}, WEBHOOK_SIGNING_PROFILE_ADCP),
        (
            {"url": URL, "reporting_frequency": "daily", "signing_profile": "web-bot-auth"},
            WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH,
        ),
        ({"url": URL, "reporting_frequency": "daily", "authentication": LEGACY_AUTH}, None),
        (
            ReportingWebhook(
                url=URL, reporting_frequency="daily", signing_profile="adcp/webhook-signing/v1"
            ),
            WEBHOOK_SIGNING_PROFILE_ADCP,
        ),
        (
            ArtifactWebhook(url=URL, delivery_mode="realtime", signing_profile="web-bot-auth"),
            WEBHOOK_SIGNING_PROFILE_WEB_BOT_AUTH,
        ),
    ],
)
def test_signing_profile_for_registration(registration: Any, expected: str | None) -> None:
    assert webhook_signing_profile_for(registration) == expected
    assert webhook_signing_profile_for(registration, adcp_version=PACKAGED) == expected


@pytest.mark.parametrize("version", ["3.2", "3.1", "3.0", "3.2.1"])
def test_authentication_stays_required_below_3_3(version: str) -> None:
    with pytest.raises(WebhookRegistrationError) as caught:
        webhook_signing_profile_for(
            {"url": URL, "reporting_frequency": "daily"}, adcp_version=version
        )
    assert caught.value.field == "authentication"
    assert caught.value.code == "INVALID_REQUEST"
    legacy = {"url": URL, "reporting_frequency": "daily", "authentication": LEGACY_AUTH}
    assert webhook_signing_profile_for(legacy, adcp_version=version) is None


@pytest.mark.parametrize("version", ["3.3", "3.3-beta.3", "3.3.0-beta.1", "4.0"])
def test_authentication_is_optional_at_3_3(version: str) -> None:
    registration = {"url": URL, "reporting_frequency": "daily"}
    assert webhook_signing_profile_for(registration, adcp_version=version) == (
        WEBHOOK_SIGNING_PROFILE_ADCP
    )


def test_authentication_and_signing_profile_are_exclusive() -> None:
    registration = {
        "url": URL,
        "reporting_frequency": "daily",
        "authentication": LEGACY_AUTH,
        "signing_profile": "adcp/webhook-signing/v1",
    }
    with pytest.raises(WebhookRegistrationError) as caught:
        webhook_signing_profile_for(registration)
    assert caught.value.field == "signing_profile"


def test_unknown_signing_profile_is_refused() -> None:
    with pytest.raises(WebhookRegistrationError, match="unknown signing_profile"):
        webhook_signing_profile_for(
            {"url": URL, "reporting_frequency": "daily", "signing_profile": "custom"}
        )


def _jwk_sender(client: httpx.AsyncClient) -> WebhookSender:
    return WebhookSender.from_jwk(WEBHOOK_KEY, d_field="_private_d_for_test_only", client=client)


def _recording_client(received: list[httpx.Request]) -> httpx.AsyncClient:
    def receive(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    return httpx.AsyncClient(transport=httpx.MockTransport(receive))


async def test_registration_without_authentication_is_signed_rfc9421() -> None:
    received: list[httpx.Request] = []
    registration = {"url": URL, "reporting_frequency": "daily", "operation_id": "op_1"}
    async with _recording_client(received) as client, _jwk_sender(client) as sender:
        assert sender.signing_profile == WEBHOOK_SIGNING_PROFILE_ADCP
        assert sender.check_registration(registration) == WEBHOOK_SIGNING_PROFILE_ADCP
        result = await sender.send_media_buy_delivery(
            registration=registration, task_id="delivery_report_1", result=DELIVERY_REPORT
        )
    assert result.ok
    [request] = received
    assert 'tag="adcp/webhook-signing/v1"' in request.headers["signature-input"]
    assert "authorization" not in request.headers


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_unsigned_sender_refuses_registration_without_authentication() -> None:
    """A bearer/HMAC sender would send credentials the buyer never registered."""
    received: list[httpx.Request] = []
    registration = {"url": URL, "reporting_frequency": "daily", "operation_id": "op_1"}
    async with _recording_client(received) as client:
        senders = [
            WebhookSender.from_bearer_token(LEGACY_AUTH["credentials"], client=client),
            WebhookSender.from_adcp_legacy_hmac(
                secret=b"s" * 32, key_id="legacy-hmac", client=client
            ),
        ]
        for sender in senders:
            assert sender.signing_profile is None
            with pytest.raises(WebhookSigningProfileUnavailableError) as caught:
                await sender.send_media_buy_delivery(
                    registration=registration, task_id="delivery_report_1", result=DELIVERY_REPORT
                )
            assert caught.value.required_profile == WEBHOOK_SIGNING_PROFILE_ADCP
            assert caught.value.sender_profile is None
            with pytest.raises(WebhookSigningProfileUnavailableError):
                sender.check_registration(registration)
    assert received == []


async def test_web_bot_auth_profile_fails_clearly_without_network_io() -> None:
    received: list[httpx.Request] = []
    registration = {
        "url": URL,
        "reporting_frequency": "daily",
        "operation_id": "op_1",
        "signing_profile": "web-bot-auth",
    }
    async with _recording_client(received) as client, _jwk_sender(client) as sender:
        with pytest.raises(WebhookSigningProfileUnavailableError, match="web-bot-auth"):
            await sender.send_media_buy_delivery(
                registration=registration, task_id="delivery_report_1", result=DELIVERY_REPORT
            )
    assert received == []


async def test_prepared_delivery_carries_the_required_profile_to_send_prepared() -> None:
    received: list[httpx.Request] = []
    prepared = WebhookSender.prepare_media_buy_delivery(
        registration={"url": URL, "reporting_frequency": "daily", "operation_id": "op_1"},
        task_id="delivery_report_1",
        result=DELIVERY_REPORT,
    )
    assert prepared.required_signing_profile == WEBHOOK_SIGNING_PROFILE_ADCP
    legacy = WebhookSender.prepare_media_buy_delivery(
        registration={
            "url": URL,
            "reporting_frequency": "daily",
            "operation_id": "op_1",
            "authentication": LEGACY_AUTH,
        },
        task_id="delivery_report_1",
        result=DELIVERY_REPORT,
    )
    assert legacy.required_signing_profile is None
    async with _recording_client(received) as client:
        async with WebhookSender.from_bearer_token(
            LEGACY_AUTH["credentials"], client=client
        ) as bearer:
            with pytest.raises(WebhookSigningProfileUnavailableError):
                await bearer.send_prepared(prepared)
            assert (await bearer.send_prepared(legacy)).ok
        async with _jwk_sender(client) as signer:
            assert (await signer.send_prepared(prepared)).ok
    assert len(received) == 2


def test_prepared_webhook_positional_construction_is_unchanged() -> None:
    prepared = PreparedWebhook(URL, "whk_1234567890123456", b"{}")
    assert prepared.required_signing_profile is None


async def test_artifact_webhook_registration_without_authentication() -> None:
    received: list[httpx.Request] = []
    registration = ArtifactWebhook(url=URL, delivery_mode="realtime")
    artifact = {
        "url": URL,
        "media_buy_id": "mb_1",
        "batch_id": "batch_1",
        "timestamp": "2026-10-01T00:00:00Z",
        "artifacts": [],
    }
    prepared = WebhookSender.prepare_artifact_webhook(**artifact, registration=registration)
    assert prepared.required_signing_profile == WEBHOOK_SIGNING_PROFILE_ADCP
    assert WebhookSender.prepare_artifact_webhook(**artifact).required_signing_profile is None
    async with _recording_client(received) as client:
        async with WebhookSender.from_bearer_token(
            LEGACY_AUTH["credentials"], client=client
        ) as bearer:
            with pytest.raises(WebhookSigningProfileUnavailableError):
                await bearer.send_artifact_webhook(**artifact, registration=registration)
        async with _jwk_sender(client) as signer:
            assert (await signer.send_artifact_webhook(**artifact, registration=registration)).ok
    assert len(received) == 1


class _CreateSeller(ADCPHandler[ToolContext]):
    adcp_capabilities = {"media_buy": {"features": {"canonical_creatives": True}}}

    def __init__(self, pin: str | None) -> None:
        self.pin = pin
        self.requests: list[dict[str, Any]] = []

    def get_adcp_version(self) -> str | None:
        return self.pin

    async def create_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        self.requests.append(params)
        return {"errors": [{"code": "INVALID_REQUEST", "message": "test stop"}]}


def _create_without_authentication(version: str) -> dict[str, Any]:
    return {
        "adcp_version": version,
        "idempotency_key": "issue1644-reporting-webhook-0001",
        "account": {"account_id": "acc_1"},
        "brand": {"domain": "acme.example"},
        "start_time": "asap",
        "end_time": "2027-01-31T23:59:59Z",
        "reporting_webhook": {"url": URL, "reporting_frequency": "daily"},
        "packages": [{"product_id": "p1", "pricing_option_id": "po1", "budget": 1000}],
    }


@pytest.mark.parametrize(
    ("pin", "buyer", "accepted"),
    [
        (None, PACKAGED, True),  # served at 3.3: optional
        (None, "3.2", False),  # a 3.2 buyer must send it
        ("3.2", "3.2", False),
        ("3.2", PACKAGED, False),  # the 3.3 -> 3.2 downgrade cannot invent one
    ],
)
async def test_server_acceptance_follows_the_served_version(
    pin: str | None, buyer: str, accepted: bool
) -> None:
    seller = _CreateSeller(pin)
    tools = MCPToolSet(
        seller,
        validation=ValidationHookConfig(requests="strict", responses="strict"),
        advertise_all=True,
    )
    request = _create_without_authentication(buyer)
    if accepted:
        await tools.call_tool("create_media_buy", request)
        [seen] = seller.requests
        assert "authentication" not in seen["reporting_webhook"]
        assert webhook_signing_profile_for(seen["reporting_webhook"], adcp_version=buyer) == (
            WEBHOOK_SIGNING_PROFILE_ADCP
        )
    else:
        with pytest.raises(Exception, match="authentication"):
            await tools.call_tool("create_media_buy", request)
        assert seller.requests == []


# ---------------------------------------------------------------------------
# Rule 4: receivers ignore aggregated_totals in delivery webhooks
# ---------------------------------------------------------------------------


def _receiver() -> WebhookReceiver:
    return WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_KEY]}),
            ),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="test-receiver",
            publisher_scope_for=lambda _signer: "test-publisher",
        ),
    )


def _signed_delivery(aggregated_totals: Any, key: str) -> tuple[dict[str, str], bytes]:
    payload = create_mcp_webhook_payload(
        task_id="delivery_report_1",
        task_type="media_buy_delivery",
        operation_id="op_1",
        status="completed",  # type: ignore[arg-type]
        result=DELIVERY_REPORT,
        idempotency_key=key,
    )
    wire = to_wire_dict(payload)
    # A non-conformant sender adds the API-only cross-buy aggregate.
    wire["result"]["aggregated_totals"] = aggregated_totals
    body = json.dumps(wire, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    signed = sign_webhook(
        method="POST",
        url=URL,
        headers=headers,
        body=body,
        private_key=private_key_from_jwk(WEBHOOK_KEY, d_field="_private_d_for_test_only"),
        key_id=WEBHOOK_KEY["kid"],
        alg="ed25519",
    )
    return {**headers, **signed.as_dict()}, body


@pytest.mark.parametrize(
    ("aggregated_totals", "key"),
    [
        ({"impressions": 1000, "spend": 45.0, "media_buy_count": 1}, "whk_aggregated_ok_aaaaaaaaa"),
        ({"impressions": "not-a-number"}, "whk_aggregated_bad_aaaaaaaa"),
        (7, "whk_aggregated_int_aaaaaaaa"),
    ],
)
async def test_receiver_ignores_aggregated_totals(aggregated_totals: Any, key: str) -> None:
    headers, body = _signed_delivery(aggregated_totals, key)
    receiver = _receiver()
    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False, outcome.rejection_reason
    result = outcome.payload.result  # type: ignore[union-attr]
    dumped = getattr(result, "root", result).model_dump(mode="json", exclude_none=True)
    assert "aggregated_totals" not in dumped
    assert dumped["media_buy_deliveries"][0]["media_buy_id"] == "mb_001"
    await receiver.acknowledge(outcome)


async def test_receiver_keeps_aggregated_totals_out_of_other_tasks_untouched() -> None:
    """Only media_buy_delivery results are filtered."""
    payload = create_mcp_webhook_payload(
        task_id="task_1",
        task_type="create_media_buy",
        operation_id="op_1",
        status="working",  # type: ignore[arg-type]
        idempotency_key="whk_other_task_aaaaaaaaaaaaa",
    )
    wire = to_wire_dict(payload)
    body = json.dumps(wire, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    signed = sign_webhook(
        method="POST",
        url=URL,
        headers=headers,
        body=body,
        private_key=private_key_from_jwk(WEBHOOK_KEY, d_field="_private_d_for_test_only"),
        key_id=WEBHOOK_KEY["kid"],
        alg="ed25519",
    )
    outcome = await _receiver().receive(
        method="POST", url=URL, headers={**headers, **signed.as_dict()}, body=body
    )
    assert outcome.rejected is False, outcome.rejection_reason
