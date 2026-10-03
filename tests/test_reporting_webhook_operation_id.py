"""Opt-in 3.2 reporting correlation survives validation and serialization."""

from __future__ import annotations

import json
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
    ReportingWebhook,
    UpdateMediaBuyRequest,
)
from adcp.types.v32 import AcceptProposalRequest
from adcp.validation.schema_loader import get_portable_schema
from adcp.validation.version import resolve_bundle_key
from adcp.webhook_sender import WebhookSender

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


@pytest.mark.asyncio
async def test_stored_registration_correlates_delivery_and_retry(tmp_path: Path) -> None:
    registration = ReportingWebhook(
        url="https://buyer.example/reporting?operation_id=do-not-parse-this",
        authentication=_AUTHENTICATION,
        reporting_frequency="daily",
        operation_id="op_reporting_001",
    )
    stored = tmp_path / "registration.json"
    stored.write_text(registration.model_dump_json())
    restored = ReportingWebhook.model_validate_json(stored.read_text())
    assert restored.operation_id is not None
    received: list[bytes] = []

    def receive(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer buyer-reporting-token-1234567890"
        payload = McpWebhookPayload.model_validate_json(request.content)
        assert payload.operation_id == restored.operation_id
        assert payload.task_type.value == "media_buy_delivery"
        received.append(request.content)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
        async with WebhookSender.from_bearer_token(
            _AUTHENTICATION["credentials"], client=client
        ) as sender:
            prepared = sender.prepare_mcp(
                url=str(restored.url),
                operation_id=restored.operation_id,
                task_id="reporting-task-001",
                task_type="media_buy_delivery",
                status="completed",
                result={
                    "notification_type": "scheduled",
                    "reporting_period": {
                        "start": "2026-10-01T00:00:00Z",
                        "end": "2026-10-02T00:00:00Z",
                    },
                    "media_buy_deliveries": [],
                },
            )
            assert (await sender.send_prepared(prepared)).ok
            assert (await sender.send_prepared(prepared)).ok
    assert received[0] == received[1]
    assert json.loads(received[0])["idempotency_key"] != restored.operation_id


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
