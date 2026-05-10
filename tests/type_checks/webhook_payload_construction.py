"""Adopter pattern: create_mcp_webhook_payload usage with typed attribute access.

create_mcp_webhook_payload returns a typed McpWebhookPayload Pydantic model.
The zero-ignore adopter pattern is direct attribute access for typed reads
and to_wire_dict() for HTTP serialization — no cast() needed.
"""

from __future__ import annotations

import json
from typing import Any

from adcp.types import GeneratedTaskStatus, McpWebhookPayload, TaskType
from adcp.webhooks import create_mcp_webhook_payload, to_wire_dict


def build_completed_payload(task_id: str, media_buy_id: str) -> McpWebhookPayload:
    return create_mcp_webhook_payload(
        task_id=task_id,
        task_type=TaskType.create_media_buy,
        status=GeneratedTaskStatus.completed,
        result={"media_buy_id": media_buy_id, "status": "active"},
        message=f"Media buy {media_buy_id} activated",
    )


def extract_task_id(payload: McpWebhookPayload) -> str:
    return payload.task_id


def extract_status(payload: McpWebhookPayload) -> GeneratedTaskStatus:
    return payload.status


def serialize_for_http(payload: McpWebhookPayload) -> dict[str, Any]:
    return to_wire_dict(payload)


payload = build_completed_payload("task_123", "mb_abc")

serialized = json.dumps(serialize_for_http(payload))
assert isinstance(serialized, str)

task_id = extract_task_id(payload)
status = extract_status(payload)
assert task_id == "task_123"
assert status == GeneratedTaskStatus.completed
