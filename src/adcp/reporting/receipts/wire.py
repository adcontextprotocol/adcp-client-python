"""Lossless raw batch admission and isolated transport-schema overlays.

Never edit cached upstream schemas or generated models. The portable canonical
schema retains conditionals omitted by model generation and compact MCP profiles.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, cast

from jsonschema import Draft7Validator, FormatChecker

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.receipts.errors import ReportingReceiptError
from adcp.validation.schema_loader import get_portable_schema

TASK = "sync_reporting_receipts"
ReceiptKind = Literal["revision_receipt", "adjustment_receipt"]
_IDENTITY_FIELDS = frozenset(
    {
        "consumer",
        "consumer_id",
        "principal",
        "principal_id",
        "buyer",
        "buyer_agent",
        "buyer_agent_id",
        "governance_agent",
        "governance_principal",
        "caller_identity",
        "tenant_id",
        "auth_info",
        "auth_principal",
        "agent_url",
    }
)


def receipt_schema(
    direction: Literal["request", "sync"], *, version: str | None = None
) -> dict[str, Any]:
    """Independent mutable schema for pinned, unpinned and model-fallback mounts."""
    schema = get_portable_schema(TASK, direction, version=version)
    if schema is None:
        raise ReportingReceiptError("RECEIPT_SCHEMA_UNREADY")
    if direction == "request":
        schema["anyOf"] = [{"required": ["receipts"]}, {"required": ["adjustment_receipts"]}]
        rules = schema.setdefault("allOf", [])
        rules.append(
            {"not": {"anyOf": [{"required": [name]} for name in sorted(_IDENTITY_FIELDS)]}}
        )
        for name in ("receipts", "adjustment_receipts"):
            array = schema["properties"][name]
            array.update(minItems=1, maxItems=100)
            array["items"] = {"allOf": [array["items"], {"not": {"required": ["received_at"]}}]}
        # JSON Schema has no cross-array sum operator. These bounded implications
        # express the exact combined limit without a proprietary validator keyword.
        rules.extend(
            {
                "if": {"required": ["receipts"], "properties": {"receipts": {"minItems": n}}},
                "then": {"properties": {"adjustment_receipts": {"maxItems": 100 - n}}},
            }
            for n in range(1, 101)
        )
    return schema


@lru_cache(maxsize=2)
def _validator(direction: Literal["request", "sync"]) -> Any:
    return Draft7Validator(receipt_schema(direction), format_checker=FormatChecker())


def validate_receipt_request(request: object) -> None:
    """Preflight the entire supplied shape before auth-dependent lookup or writes."""
    valid = False
    try:
        if type(request) is dict:
            supplied = [
                request[name] for name in ("receipts", "adjustment_receipts") if name in request
            ]
            if supplied and all(type(items) is list and items for items in supplied):
                items = [item for array in supplied for item in array]
                ids = [item.get("reporting_receipt_id") for item in items if type(item) is dict]
                valid = (
                    1 <= len(items) <= 100
                    and len(ids) == len(items)
                    and all(isinstance(i, str) for i in ids)
                    and len(set(ids)) == len(ids)
                    and not _IDENTITY_FIELDS.intersection(request)
                    and _validator("request").is_valid(request)
                )
                if valid:
                    # Also reject non-JSON values and non-finite/lossy JCS numbers.
                    canonical_json_utf8_v1(request)
                    # JSONB requires Unicode scalar values without U+0000.
                    # Reject unsupported keys/values before a header on both
                    # stores, including strings inside arbitrary context/ext.
                    pending: list[Any] = [request]
                    while pending:
                        value = pending.pop()
                        if isinstance(value, str) and any(
                            c == "\x00" or 0xD800 <= ord(c) <= 0xDFFF for c in value
                        ):
                            raise ValueError("unsupported JSON string")
                        if type(value) is dict:
                            pending.extend(value)
                            pending.extend(value.values())
                        elif type(value) is list:
                            pending.extend(value)
    except (ValueError, TypeError, OverflowError, RecursionError):
        valid = False
    if not valid:
        raise ReportingReceiptError("INVALID_REQUEST")


def validate_receipt_results(
    results: object, batch: ReceiptBatch, *, complete: bool = False
) -> None:
    """Validate every durable prefix before resuming any domain writes."""
    valid = False
    try:
        if type(results) is list and len(results) <= len(batch.items):
            if not results and not complete:
                return
            valid = _validator("sync").is_valid({"status": "completed", "results": results})
            ids = [
                (
                    result["reporting_receipt_id"]
                    if result["result"] == "failed"
                    else result.get("receipt", result.get("adjustment_receipt"))[
                        "reporting_receipt_id"
                    ]
                )
                for result in results
            ]
            expected = [item["reporting_receipt_id"] for _, item in batch.items]
            valid = valid and ids == (expected if complete else expected[: len(results)])
            for (kind, _), result in zip(batch.items, results):
                if result["result"] != "failed":
                    key = "receipt" if kind == "revision_receipt" else "adjustment_receipt"
                    valid = valid and key in result
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")


def validate_receipt_response(response: object, batch: ReceiptBatch) -> None:
    """Validate exact ID coverage/order as well as the final wire schema."""
    if type(response) is not dict or not _validator("sync").is_valid(response):
        raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
    validate_receipt_results(response["results"], batch, complete=True)


@dataclass(frozen=True)
class ReceiptBatch:
    """Private immutable whole-request identity, including context and extensions."""

    canonical_request: bytes = field(repr=False)

    @classmethod
    def parse(cls, request: dict[str, Any]) -> ReceiptBatch:
        validate_receipt_request(request)
        return cls(canonical_json_utf8_v1(request))

    @property
    def request(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.canonical_request))

    @property
    def key(self) -> str:
        return str(self.request["idempotency_key"])

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_request).hexdigest()

    @property
    def items(self) -> tuple[tuple[ReceiptKind, dict[str, Any]], ...]:
        request = self.request
        arrays: tuple[tuple[ReceiptKind, str], ...] = (
            ("revision_receipt", "receipts"),
            ("adjustment_receipt", "adjustment_receipts"),
        )
        return tuple((kind, item) for kind, name in arrays for item in request.get(name, []))
