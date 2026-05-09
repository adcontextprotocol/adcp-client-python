"""Tests for ``adcp.webhooks.to_wire_dict``.

The seam exists so adopters wrapping ``create_a2a_webhook_payload`` and
``create_mcp_webhook_payload`` can serialize either return shape with one
call. The load-bearing properties:

* a2a protobuf round-trips to camelCase keys (``id``, ``contextId``,
  ``artifactId``) so external A2A receivers see the on-wire shape.
* MCP dicts pass through with the snake_case keys the MCP webhook
  schema specifies (``task_id``, ``task_type``).
* Pydantic models dump to JSON-mode dicts so sub-models serialize too.
* Unsupported types raise ``TypeError`` at the seam — silent fallthrough
  to ``str(payload)`` or similar would mask integration bugs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adcp.types import GeneratedTaskStatus
from adcp.types.generated_poc.core.mcp_webhook_payload import McpWebhookPayload
from adcp.webhooks import (
    create_a2a_webhook_payload,
    create_mcp_webhook_payload,
    to_wire_dict,
)


def test_a2a_task_round_trips_to_camelcase_wire_keys() -> None:
    """Terminated A2A status returns a Task → camelCase wire keys."""
    payload = create_a2a_webhook_payload(
        task_id="task_123",
        status=GeneratedTaskStatus.completed,
        context_id="ctx_456",
        result={"media_buy_id": "mb_1"},
        timestamp=datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc),
    )

    wire = to_wire_dict(payload)

    assert wire["id"] == "task_123"
    assert wire["contextId"] == "ctx_456"
    assert wire["status"]["state"] == "completed"
    assert wire["artifacts"][0]["artifactId"] == "task_123_result"
    # Inner DataPart preserves the AdCP response payload verbatim.
    assert wire["artifacts"][0]["parts"][0]["data"] == {"media_buy_id": "mb_1"}


def test_a2a_status_update_event_round_trips_to_camelcase_wire_keys() -> None:
    """Intermediate A2A status returns a TaskStatusUpdateEvent."""
    payload = create_a2a_webhook_payload(
        task_id="task_789",
        status=GeneratedTaskStatus.working,
        context_id="ctx_789",
        result={"current_step": "processing", "percentage": 50},
    )

    wire = to_wire_dict(payload)

    assert wire["taskId"] == "task_789"
    assert wire["contextId"] == "ctx_789"
    assert wire["status"]["state"] == "working"
    assert wire["status"]["message"]["role"] == "agent"
    assert wire["status"]["message"]["parts"][0]["data"] == {
        "current_step": "processing",
        "percentage": 50,
    }


def test_mcp_dict_passes_through_with_snake_case_keys() -> None:
    """MCP wire shape is snake_case per mcp-webhook-payload.json."""
    payload = create_mcp_webhook_payload(
        task_id="task_123",
        task_type="create_media_buy",
        status="completed",
        result={"media_buy_id": "mb_1"},
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
    )

    wire = to_wire_dict(payload)

    assert wire["task_id"] == "task_123"
    assert wire["task_type"] == "create_media_buy"
    assert wire["status"] == "completed"
    assert wire["result"] == {"media_buy_id": "mb_1"}
    assert wire["idempotency_key"] == "whk_01HW9D2T3VXQ5M7K9N1P3R5S7U"


def test_mcp_pydantic_model_dumps_to_snake_case_wire_keys() -> None:
    """Adopters that construct ``McpWebhookPayload`` directly get the
    same wire shape as the dict path — single seam, no per-shape branch.
    """
    model = McpWebhookPayload(
        idempotency_key="whk_01HW9D2T3VXQ5M7K9N1P3R5S7U",
        task_id="task_456",
        task_type="create_media_buy",
        status="completed",
        timestamp=datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc),
    )

    wire = to_wire_dict(model)

    assert wire["task_id"] == "task_456"
    assert wire["task_type"] == "create_media_buy"
    assert wire["status"] == "completed"
    # ``mode="json", exclude_none=True`` is load-bearing — None fields
    # would otherwise pollute the wire body.
    assert "operation_id" not in wire
    assert "context_id" not in wire


def test_plain_dict_passes_through_unchanged() -> None:
    """Hand-built dicts (legacy adopter passthrough) round-trip verbatim."""
    raw = {"task_id": "t1", "status": "working", "extra": {"nested": True}}

    wire = to_wire_dict(raw)

    assert wire == raw
    # Defensive copy — caller mutating the returned dict must not
    # mutate the input.
    assert wire is not raw


def test_unsupported_type_raises_type_error() -> None:
    """Silent fallthrough would mask integration bugs — fail loud."""
    with pytest.raises(TypeError, match="Unsupported webhook payload type"):
        to_wire_dict("not a payload")  # type: ignore[arg-type]
