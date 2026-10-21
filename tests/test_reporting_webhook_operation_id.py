"""Reporting-webhook operation_id: registration, storage, and scheduled delivery."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft7Validator, FormatChecker
from pydantic import ValidationError

from adcp.types import (
    AcceptProposalRequest as CurrentAcceptProposalRequest,
)
from adcp.types import (
    BuyProductsRequest,
    ControlMediaBuyRequest,
    CreateMediaBuyRequest,
    McpWebhookPayload,
    MediaBuyDeliveryWebhookResult,
    PushNotificationConfig,
    ReportingWebhook,
    UpdateMediaBuyRequest,
)
from adcp.types.v32 import AcceptProposalRequest
from adcp.validation.schema_loader import get_named_validator, get_portable_schema
from adcp.validation.version import resolve_bundle_key
from adcp.webhooks import (
    REPORTING_WEBHOOK_OPERATION_ID_MISSING,
    ReportingWebhookOperationIdMissingError,
    WebhookSender,
    create_mcp_webhook_payload,
    create_media_buy_delivery_webhook_payload,
    deliver,
)

_AUTHENTICATION = {
    "schemes": ["Bearer"],
    "credentials": "buyer-reporting-token-1234567890",
}

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_reporting_webhook_operation_id_is_optional() -> None:
    webhook = ReportingWebhook(
        url="https://buyer.example/reporting",
        authentication=_AUTHENTICATION,
        reporting_frequency="daily",
    )

    assert "operation_id" in ReportingWebhook.model_fields
    assert not ReportingWebhook.model_fields["operation_id"].is_required()
    assert webhook.operation_id is None
    assert "operation_id" not in webhook.model_dump()


@pytest.mark.parametrize("operation_id", ["op_reporting_001", "a" * 255, "buyer.stream:1-2"])
def test_reporting_operation_id_round_trips(operation_id: str) -> None:
    webhook = ReportingWebhook(
        url="https://buyer.example/reporting?route=opaque",
        authentication=_AUTHENTICATION,
        reporting_frequency="daily",
        operation_id=operation_id,
    )
    assert webhook.operation_id == operation_id
    assert webhook.model_dump(mode="json")["operation_id"] == operation_id
    restored = ReportingWebhook.model_validate_json(webhook.model_dump_json())
    assert restored.operation_id == operation_id


@pytest.mark.parametrize("operation_id", ["", "a" * 256, "has spaces", "a/b", "a\n", 123])
def test_reporting_operation_id_rejects_invalid_supplied_values(operation_id: object) -> None:
    with pytest.raises(ValidationError, match="operation_id"):
        ReportingWebhook(
            url="https://buyer.example/reporting",
            authentication=_AUTHENTICATION,
            reporting_frequency="daily",
            operation_id=operation_id,
        )


def test_reporting_operation_id_none_is_omitted_on_the_wire() -> None:
    webhook = ReportingWebhook(
        url="https://buyer.example/reporting",
        authentication=_AUTHENTICATION,
        reporting_frequency="daily",
        operation_id=None,
    )
    assert "operation_id" not in json.loads(webhook.model_dump_json())


_ACCOUNT = {"brand": {"domain": "example.com"}, "operator": "agency.example"}
_REQUEST_CASES = [
    (
        CurrentAcceptProposalRequest,
        "accept_proposal",
        {"proposal_id": "proposal-1", "proposal_terms_digest": "sha256:" + "x" * 43},
    ),
    (
        BuyProductsRequest,
        "buy_products",
        {
            "feed_version": "feed-1",
            "purchases": [{"product_id": "product-1", "pricing_option_id": "price-1"}],
            "start_time": "2026-10-01T00:00:00Z",
            "end_time": "2026-10-31T23:59:59Z",
        },
    ),
    (ControlMediaBuyRequest, "control_media_buy", {"media_buy_id": "buy-1", "revision": 1}),
    (
        CreateMediaBuyRequest,
        "create_media_buy",
        {
            "brand": {"domain": "example.com"},
            "start_time": "2026-10-01T00:00:00Z",
            "end_time": "2026-10-31T23:59:59Z",
            "packages": [
                {"product_id": "product-1", "pricing_option_id": "price-1", "budget": 100.0}
            ],
        },
    ),
    (UpdateMediaBuyRequest, "update_media_buy", {"media_buy_id": "buy-1"}),
]


@pytest.mark.parametrize("model,tool,params", _REQUEST_CASES)
@pytest.mark.parametrize("operation_id", [None, "op_reporting_001"])
def test_all_registration_requests_preserve_optional_reporting_id(
    model: Any, tool: str, params: dict[str, Any], operation_id: str | None
) -> None:
    registration = {
        "url": "https://buyer.example/reporting",
        "authentication": _AUTHENTICATION,
        "reporting_frequency": "daily",
    }
    if operation_id is not None:
        registration["operation_id"] = operation_id
    request = model(
        **params,
        adcp_version="3.2",
        account=_ACCOUNT,
        idempotency_key="reporting_request_001",
        reporting_webhook=registration,
    )
    assert request.reporting_webhook.operation_id == operation_id
    dumped = request.model_dump(mode="json")
    assert dumped["reporting_webhook"].get("operation_id") == operation_id
    schema = get_portable_schema(tool, "request", version="3.2")
    assert schema is not None
    Draft7Validator(schema, format_checker=FormatChecker()).validate(dumped)
    restored = model.model_validate_json(request.model_dump_json())
    assert restored.reporting_webhook.operation_id == operation_id


@pytest.mark.parametrize("model,tool,params", _REQUEST_CASES)
def test_nested_registration_validation_rejects_bad_reporting_id(
    model: Any, tool: str, params: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match="reporting_webhook.operation_id"):
        model(
            **params,
            adcp_version="3.2",
            account=_ACCOUNT,
            idempotency_key="reporting_request_001",
            reporting_webhook={
                "url": "https://buyer.example/reporting",
                "authentication": _AUTHENTICATION,
                "reporting_frequency": "daily",
                "operation_id": "invalid value",
            },
        )


_DELIVERY_REPORT = {
    "notification_type": "scheduled",
    "sequence_number": 3,
    "reporting_period": {
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-10-02T00:00:00Z",
    },
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


def _validate_wire_delivery_webhook(body: bytes) -> dict[str, Any]:
    """Validate the posted bytes against the pinned bundle, envelope and result."""
    wire = json.loads(body)
    for path, value in (
        ("core/mcp-webhook-payload.json", wire),
        ("media-buy/media-buy-delivery-webhook-result.json", wire["result"]),
    ):
        validator = get_named_validator(path, version="3.2")
        assert validator is not None
        validator.validate(value)
    return wire


def test_reporting_operation_id_constraints_match_push_notification_config() -> None:
    """AdCP 3.3 (adcp#7895) adopts push_notification_config.operation_id's constraints.

    Pinning parity keeps this 3.2 extension and the SDK 10 generated field in
    agreement, so the forward-compat patch can retire without a wire change.
    """
    reporting = ReportingWebhook.model_fields["operation_id"]
    push = PushNotificationConfig.model_fields["operation_id"]
    assert not reporting.is_required()
    assert reporting.default is None
    assert reporting.annotation == push.annotation
    assert [repr(item) for item in reporting.metadata] == [repr(item) for item in push.metadata]
    schema = ReportingWebhook.model_json_schema()["properties"]["operation_id"]
    assert schema["anyOf"][0] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 255,
        "pattern": "^[A-Za-z0-9_.:-]{1,255}$",
    }
    assert "MUST NOT emit `media_buy_delivery`" in schema["description"]


@pytest.mark.asyncio
async def test_stored_registration_correlates_delivery_and_retry(tmp_path: Path) -> None:
    registration = ReportingWebhook(
        url="https://buyer.example/reporting?operation_id=do-not-parse-this",
        authentication=_AUTHENTICATION,
        reporting_frequency="daily",
        operation_id="op_reporting_001",
        token="buyer-reporting-echo-token",
    )
    stored = tmp_path / "registration.json"
    stored.write_text(registration.model_dump_json())
    restored = json.loads(stored.read_text())
    received: list[bytes] = []

    def receive(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == str(registration.url)
        assert request.headers["authorization"] == "Bearer buyer-reporting-token-1234567890"
        payload = McpWebhookPayload.model_validate_json(request.content)
        assert payload.operation_id == "op_reporting_001"
        assert payload.task_type.value == "media_buy_delivery"
        assert payload.status.value == "completed"
        received.append(request.content)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        async with WebhookSender.from_bearer_token(
            _AUTHENTICATION["credentials"], client=client
        ) as sender:
            prepared = sender.prepare_media_buy_delivery(
                registration=restored,
                task_id="delivery_report_mb_001_000003",
                result=_DELIVERY_REPORT,
            )
            assert (await sender.send_prepared(prepared)).ok
            assert (await sender.send_prepared(prepared)).ok
    assert received[0] == received[1]
    wire = _validate_wire_delivery_webhook(received[0])
    assert wire["operation_id"] == "op_reporting_001"
    assert wire["token"] == "buyer-reporting-echo-token"
    assert wire["result"] == _DELIVERY_REPORT
    assert wire["idempotency_key"] != wire["operation_id"]
    # Point-in-time delivery events carry no notification_id.
    assert "notification_id" not in wire


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_legacy_deliver_echoes_registered_operation_id() -> None:
    registration = ReportingWebhook(
        url="https://buyer.example/reporting",
        authentication=_AUTHENTICATION,
        reporting_frequency="daily",
        operation_id="op_reporting_legacy",
    )
    received: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        response = await deliver(
            registration,
            create_media_buy_delivery_webhook_payload(
                registration, task_id="delivery_report_000001", result=_DELIVERY_REPORT
            ),
            client=client,
        )
    assert response.status_code == 204
    wire = _validate_wire_delivery_webhook(received[0].content)
    assert wire["operation_id"] == "op_reporting_legacy"
    assert received[0].headers["authorization"] == f"Bearer {_AUTHENTICATION['credentials']}"


@pytest.mark.asyncio
async def test_scheduled_sends_skip_and_flag_registrations_without_operation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A legacy registration is suppressed and flagged, never given a minted ID."""
    legacy = {
        "url": "https://buyer.example/reporting/mb_legacy?op=not-a-source",
        "authentication": _AUTHENTICATION,
        "reporting_frequency": "daily",
    }
    current = {**legacy, "url": "https://buyer.example/reporting", "operation_id": "op_current"}
    received: list[dict[str, Any]] = []
    skipped: list[ReportingWebhookOperationIdMissingError] = []

    def receive(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200)

    caplog.set_level(logging.WARNING, logger="adcp.webhooks")
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        async with WebhookSender.from_bearer_token(
            _AUTHENTICATION["credentials"], client=client
        ) as sender:
            # A seller's scheduler loop over stored registrations.
            for media_buy_id, stored in (("mb_legacy", legacy), ("mb_current", current)):
                report = {
                    **_DELIVERY_REPORT,
                    "media_buy_deliveries": [
                        {
                            **_DELIVERY_REPORT["media_buy_deliveries"][0],
                            "media_buy_id": media_buy_id,
                        }
                    ],
                }
                try:
                    result = await sender.send_media_buy_delivery(
                        registration=stored,
                        task_id=f"delivery_report_{media_buy_id}",
                        result=report,
                    )
                except ReportingWebhookOperationIdMissingError as exc:
                    skipped.append(exc)
                    continue
                assert result.ok

    assert [payload["operation_id"] for payload in received] == ["op_current"]
    assert len(skipped) == 1
    assert skipped[0].code == REPORTING_WEBHOOK_OPERATION_ID_MISSING
    assert skipped[0].media_buy_ids == ("mb_legacy",)
    [record] = [r for r in caplog.records if r.name == "adcp.webhooks"]
    assert record.levelno == logging.WARNING
    assert record.adcp_event == REPORTING_WEBHOOK_OPERATION_ID_MISSING  # type: ignore[attr-defined]
    assert record.task_type == "media_buy_delivery"  # type: ignore[attr-defined]
    assert record.notification_type == "scheduled"  # type: ignore[attr-defined]
    assert record.media_buy_ids == ("mb_legacy",)  # type: ignore[attr-defined]
    rendered = record.getMessage() + str(skipped[0])
    assert "buyer.example" not in rendered
    assert _AUTHENTICATION["credentials"] not in rendered


def test_typed_registration_without_operation_id_is_refused() -> None:
    registration = ReportingWebhook(
        url="https://buyer.example/reporting",
        authentication=_AUTHENTICATION,
        reporting_frequency="daily",
    )
    with pytest.raises(ReportingWebhookOperationIdMissingError):
        create_media_buy_delivery_webhook_payload(
            registration,
            task_id="delivery_report_000001",
            result=MediaBuyDeliveryWebhookResult.model_validate(_DELIVERY_REPORT),
        )


def test_tampered_stored_operation_id_fails_validation() -> None:
    with pytest.raises(ValidationError, match="operation_id"):
        WebhookSender.prepare_media_buy_delivery(
            registration={
                "url": "https://buyer.example/reporting",
                "authentication": _AUTHENTICATION,
                "reporting_frequency": "daily",
                "operation_id": "not valid",
            },
            task_id="delivery_report_000001",
            result=_DELIVERY_REPORT,
        )


def test_generic_mcp_builder_omits_terminal_id_only_for_delivery_reports() -> None:
    delivery = create_mcp_webhook_payload(
        task_id="delivery_report_000001",
        status="completed",
        task_type="media_buy_delivery",
        operation_id="op_reporting_001",
        result=_DELIVERY_REPORT,
    )
    task = create_mcp_webhook_payload(
        task_id="task_001",
        status="completed",
        task_type="create_media_buy",
        operation_id="op_task_001",
    )
    assert delivery.notification_id is None
    assert task.notification_id == "task_001.terminal"


def test_beta6_versioned_request_schema_preserves_reporting_operation_id() -> None:
    """Historical versioned types retain the beta.6 wire contract."""
    with pytest.raises(ValidationError, match="operation_id.*required property"):
        AcceptProposalRequest(
            adcp_version="3.2-beta.6",
            idempotency_key="accept-request-1234",
            account={
                "brand": {"domain": "example.com"},
                "operator": "agency.example",
            },
            proposal_id="proposal-1",
            proposal_terms_digest="sha256:" + "x" * 43,
            reporting_webhook={
                "url": "https://buyer.example/reporting",
                "authentication": _AUTHENTICATION,
                "reporting_frequency": "daily",
            },
        )


def test_webhook_payload_schema_documents_push_notification_operation_id_source() -> None:
    pinned_version = (_REPOSITORY_ROOT / "src/adcp/ADCP_VERSION").read_text().strip()
    schema_path = (
        _REPOSITORY_ROOT
        / "schemas/cache"
        / resolve_bundle_key(pinned_version)
        / "core/mcp-webhook-payload.json"
    )
    schema = json.loads(schema_path.read_text())
    description = schema["properties"]["operation_id"]["description"]

    assert "push_notification_config.operation_id" in description
    assert "reporting_webhook.operation_id" not in description
    assert McpWebhookPayload.model_fields["operation_id"].description == description
