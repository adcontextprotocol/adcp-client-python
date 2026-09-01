"""Reporting webhooks stay separate from MCP envelope correlation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from adcp.types import McpWebhookPayload, ReportingWebhook
from adcp.types.v32 import AcceptProposalRequest
from adcp.validation.version import resolve_bundle_key

_AUTHENTICATION = {
    "schemes": ["Bearer"],
    "credentials": "buyer-reporting-token-1234567890",
}

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_reporting_webhook_does_not_define_operation_id() -> None:
    webhook = ReportingWebhook(
        url="https://buyer.example/reporting",
        authentication=_AUTHENTICATION,
        reporting_frequency="daily",
    )

    assert "operation_id" not in ReportingWebhook.model_fields
    assert "operation_id" not in webhook.model_dump()


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
