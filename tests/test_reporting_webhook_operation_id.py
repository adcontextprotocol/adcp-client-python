"""Reporting webhooks carry buyer-supplied MCP envelope correlation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from adcp.types import McpWebhookPayload, ReportingWebhook
from adcp.types.v32 import AcceptProposalRequest

_AUTHENTICATION = {
    "schemes": ["Bearer"],
    "credentials": "buyer-reporting-token-1234567890",
}

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_reporting_webhook_requires_operation_id() -> None:
    with pytest.raises(ValidationError, match="operation_id"):
        ReportingWebhook(
            url="https://buyer.example/reporting",
            authentication=_AUTHENTICATION,
            reporting_frequency="daily",
        )


def test_reporting_webhook_accepts_buyer_supplied_operation_id() -> None:
    webhook = ReportingWebhook(
        url="https://buyer.example/reporting",
        operation_id="reporting.mb_123.v1",
        authentication=_AUTHENTICATION,
        reporting_frequency="daily",
    )

    assert webhook.operation_id == "reporting.mb_123.v1"


def test_versioned_request_schema_requires_reporting_operation_id() -> None:
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


def test_webhook_payload_schema_documents_both_operation_id_sources() -> None:
    schema_path = _REPOSITORY_ROOT / "schemas/cache/3.2.0-beta.6/core/mcp-webhook-payload.json"
    schema = json.loads(schema_path.read_text())
    description = schema["properties"]["operation_id"]["description"]

    assert "push_notification_config.operation_id" in description
    assert "reporting_webhook.operation_id" in description
    assert McpWebhookPayload.model_fields["operation_id"].description == description
