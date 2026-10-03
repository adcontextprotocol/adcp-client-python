"""Shared application-task wire projection and reconciliation queries."""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from adcp.decisioning.pagination import _decode_cursor, _encode_cursor, _query_hash
from adcp.decisioning.types import AdcpError
from adcp.validation.schema_loader import get_named_schema_document


@lru_cache(maxsize=1)
def _task_protocols() -> dict[str, str]:
    manifest = get_named_schema_document("manifest.json") or {}
    # Manifest namespaces include administrative surfaces; task categorization
    # uses the coarser adcp-protocol enum.
    aliases = {
        "account": "media-buy",
        "content-standards": "governance",
        "property": "governance",
        "collection": "governance",
        "compliance": "governance",
    }
    return {
        name: aliases.get(tool["protocol"], tool["protocol"])
        for name, tool in manifest.get("tools", {}).items()
    }


def task_protocol(record: dict[str, Any]) -> str:
    protocol = record.get("protocol") or _task_protocols().get(record["task_type"])
    if not isinstance(protocol, str):
        raise AdcpError("INTERNAL_ERROR", message="Task has no protocol metadata")
    return protocol


def task_is_expired(record: dict[str, Any]) -> bool:
    """Custom registries may expose an epoch expiry; SDK stores retain until discard."""
    expires = record.get("expires_at")
    return expires is not None and float(expires) <= time.time()


def project_task(record: dict[str, Any], *, include_result: bool = False) -> dict[str, Any]:
    response: dict[str, Any] = {
        "task_id": record["task_id"],
        "task_type": record["task_type"],
        "protocol": task_protocol(record),
        "status": record["state"],
        "created_at": datetime.fromtimestamp(record["created_at"], timezone.utc).isoformat(),
        "updated_at": datetime.fromtimestamp(record["updated_at"], timezone.utc).isoformat(),
    }
    for key in ("progress", "error", "context", "has_webhook"):
        if record.get(key) is not None:
            response[key] = record[key]
    if record["state"] in {"completed", "failed", "canceled", "rejected"}:
        response["completed_at"] = response["updated_at"]
        if include_result:
            if record.get("result") is not None:
                response["result"] = record["result"]
            elif record["state"] == "failed" and record.get("error") is not None:
                response["result"] = {
                    "status": "failed",
                    "adcp_error": record["error"],
                    "errors": [record["error"]],
                }
    return response


def list_task_records(
    records: list[dict[str, Any]],
    *,
    account_id: str,
    filters: dict[str, Any] | None = None,
    sort: dict[str, Any] | None = None,
    pagination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Query an account-scoped snapshot. Cursors bind account, filters and sort.

    The legacy list schema admits media-buy, creative and signals domains only.
    Other domains remain pollable through get_task_status. SDK registries do not
    persist conversation history. Unknown filter extensions are rejected rather
    than silently returning a broader result set.
    """
    filters, sort, pagination = filters or {}, sort or {}, pagination or {}
    supported = {
        "protocol",
        "protocols",
        "status",
        "statuses",
        "task_type",
        "task_types",
        "created_after",
        "created_before",
        "updated_after",
        "updated_before",
        "task_ids",
        "context_contains",
        "has_webhook",
    }
    if filters.keys() - supported:
        raise AdcpError("INVALID_REQUEST", message="Unsupported task filter", field="filters")
    field, direction = sort.get("field", "created_at"), sort.get("direction", "desc")
    if field not in {
        "created_at",
        "updated_at",
        "status",
        "task_type",
        "protocol",
    } or direction not in {"asc", "desc"}:
        raise AdcpError("INVALID_REQUEST", message="Invalid task sort", field="sort")
    max_results = pagination.get("max_results", 50)
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or not 1 <= max_results <= 100
    ):
        raise AdcpError(
            "INVALID_REQUEST", message="max_results must be 1–100", field="pagination.max_results"
        )
    query_hash = _query_hash(
        {
            "account_id": account_id,
            "filters": filters,
            "sort": {"field": field, "direction": direction},
        }
    )
    cursor = pagination.get("cursor")
    offset = _decode_cursor(cursor, query_hash) if cursor else 0
    if offset < 0:
        raise AdcpError("INVALID_REQUEST", message="Invalid task cursor", field="pagination.cursor")
    tasks = []
    for record in records:
        if record["account_id"] != account_id or task_is_expired(record):
            continue
        task = project_task(record)
        if task["protocol"] not in {"media-buy", "creative", "signals"}:
            continue
        matches = True
        for singular, plural in (
            ("protocol", "protocols"),
            ("status", "statuses"),
            ("task_type", "task_types"),
        ):
            if singular in filters and task[singular] != filters[singular]:
                matches = False
            if plural in filters and task[singular] not in filters[plural]:
                matches = False
        for stamp in ("created", "updated"):
            for bound, op in (("after", "after"), ("before", "before")):
                value = filters.get(f"{stamp}_{bound}")
                if value is not None:
                    try:
                        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                        if date.tzinfo is None:
                            raise ValueError("missing timezone")
                        epoch = date.timestamp()
                    except (ValueError, TypeError):
                        raise AdcpError(
                            "INVALID_REQUEST", message="Invalid task date filter", field="filters"
                        ) from None
                    if (op == "after" and record[f"{stamp}_at"] <= epoch) or (
                        op == "before" and record[f"{stamp}_at"] >= epoch
                    ):
                        matches = False
        if "task_ids" in filters and task["task_id"] not in filters["task_ids"]:
            matches = False
        if "context_contains" in filters and filters["context_contains"] not in json.dumps(
            record.get("context") or {}, sort_keys=True
        ):
            matches = False
        if (
            "has_webhook" in filters
            and bool(record.get("has_webhook", False)) != filters["has_webhook"]
        ):
            matches = False
        if matches:
            tasks.append(task)
    tasks.sort(key=lambda task: (task[field], task["task_id"]), reverse=direction == "desc")
    total = len(tasks)
    page = tasks[offset : offset + max_results]
    metadata: dict[str, Any] = {"has_more": offset + len(page) < total, "total_count": total}
    if metadata["has_more"]:
        metadata["cursor"] = _encode_cursor(offset + len(page), query_hash)
    summary = {
        "total_matching": total,
        "returned": len(page),
        "domain_breakdown": dict(Counter(task["protocol"] for task in tasks)),
        "status_breakdown": dict(Counter(task["status"] for task in tasks)),
        "filters_applied": list(filters),
        "sort_applied": {"field": field, "direction": direction},
    }
    for task in page:
        task["domain"] = task.pop("protocol")
    return {"status": "completed", "query_summary": summary, "tasks": page, "pagination": metadata}
