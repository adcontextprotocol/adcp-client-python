"""Adopter pattern: create_mcp_webhook_payload usage with cast() for field access.

create_mcp_webhook_payload returns dict[str, Any]. The zero-ignore pattern
for extracting typed values is cast() — explicit, visible, and does not
require # type: ignore.
"""
from __future__ import annotations

import json
from typing import Any, cast

from adcp.types import GeneratedTaskStatus
from adcp.webhooks import create_mcp_webhook_payload


def build_completed_payload(task_id: str, products: list[dict[str, Any]]) -> dict[str, Any]:
    return create_mcp_webhook_payload(
        task_id=task_id,
        task_type="get_products",
        status=GeneratedTaskStatus.completed,
        result={"products": products},
        message=f"Found {len(products)} products",
    )


def extract_task_id(payload: dict[str, Any]) -> str:
    return cast(str, payload["task_id"])


def extract_status(payload: dict[str, Any]) -> str:
    return cast(str, payload["status"])


payload = build_completed_payload("task_123", [{"product_id": "p1"}])

serialized = json.dumps(payload)
assert isinstance(serialized, str)

task_id = extract_task_id(payload)
status = extract_status(payload)
assert task_id == "task_123"
assert status == "completed"
