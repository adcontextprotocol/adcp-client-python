"""Normalize the complete semantic request, independently of transport models."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from jsonschema import Draft7Validator, FormatChecker

from adcp._version import (
    is_adcp_version_at_least,
    normalize_to_release_precision,
    resolve_adcp_version,
)
from adcp.reporting.feed.errors import ReportingFeedError
from adcp.reporting.receipts.wire import _IDENTITY_FIELDS
from adcp.validation.schema_loader import get_portable_schema

TASK = "get_reporting_status"
TOKEN_LIMIT = 2048


def _semantic_numbers(value: Any) -> Any:
    """Bind equal JSON numbers identically across MCP and protobuf Struct.

    Struct represents every number as a double, including integral values.
    Keep exact integers (and booleans) intact: rounding an integer through a
    float here could silently authorize a genuinely different vendor filter.
    The caller validates finite ordinary JSON before applying this transform.
    """
    if type(value) is float and value.is_integer():
        return int(value)
    if type(value) is dict:
        return {key: _semantic_numbers(item) for key, item in value.items()}
    if type(value) is list:
        return [_semantic_numbers(item) for item in value]
    return value


def transport_parameters(params: dict[str, Any]) -> dict[str, Any]:
    """Normalize received JSON numbers without changing their semantic value.

    The shared transport decoder retains decimal lexemes for financial receipt
    admission. Feed context/vendor JSON permits ordinary finite fractions, but
    conversion must not alias two different received lexemes. Integral Decimal
    values become exact integers; fractional values must survive the ordinary
    JSON float round trip. This cannot recover precision a client lost before
    transmission (for example in a protobuf Struct).
    """

    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("feed parameters require finite JSON numbers")
            if value == value.to_integral_value():
                # The finite-float check above also bounds exponent expansion.
                # Do not pass exact large integers through a binary float.
                return int(value)
            if Decimal(repr(number)) != value:
                raise ValueError(
                    "feed fractional parameters require an exact JSON number round trip"
                )
            return number
        if type(value) is dict:
            return {key: convert(item) for key, item in value.items()}
        if type(value) is list:
            return [convert(item) for item in value]
        return value

    result = dict(convert(params))
    pagination = params.get("pagination")
    if isinstance(pagination, dict) and isinstance(pagination.get("max_results"), Decimal):
        limit = pagination["max_results"]
        if not limit.is_finite() or not 1 <= limit <= 100 or limit != limit.to_integral_value():
            raise ValueError("feed page size requires an exact bounded integer")
        result["pagination"]["max_results"] = int(limit)
    return result


def feed_schema(
    direction: Literal["request", "sync"], *, version: str | None = None
) -> dict[str, Any]:
    """Copy the bundled schema; never change cached/generated Core schemas."""
    schema = get_portable_schema(TASK, direction, version=version)
    if schema is None:
        raise ReportingFeedError("REPORTING_FEED_SCHEMA_UNREADY")
    properties = schema["properties"]
    if direction == "request":
        properties["changes_after"].update(minLength=1, maxLength=TOKEN_LIMIT)
    else:
        properties["changes_checkpoint"].update(minLength=1, maxLength=TOKEN_LIMIT)
    # A portable pagination schema can be a local $ref. Draft 7 ignores
    # siblings of $ref, so impose the bound in a separate allOf member.
    pagination = properties.get("pagination")
    if pagination is not None:
        properties["pagination"] = {
            "allOf": [
                pagination,
                {
                    "properties": {
                        "cursor": {"type": "string", "minLength": 1, "maxLength": TOKEN_LIMIT}
                    }
                },
            ]
        }
    return schema


@lru_cache(maxsize=1)
def _validator() -> Any:
    return Draft7Validator(feed_schema("request"), format_checker=FormatChecker())


@dataclass(frozen=True)
class FeedRequest:
    filters_json: bytes
    cursor: str | None
    changes_after: str | None
    limit: int

    @property
    def filters(self) -> dict[str, Any]:
        return dict(json.loads(self.filters_json))

    @classmethod
    def parse(cls, request: dict[str, Any]) -> FeedRequest:
        try:
            if (
                type(request) is not dict
                or request.get("view") != "periods"
                or _IDENTITY_FIELDS.intersection(request)
                or "idempotency_key" in request
                or request.get("reporting_revision_id") is not None
                or not _validator().is_valid(request)
            ):
                raise ValueError
            pagination = request.get("pagination") or {}
            cursor, checkpoint = pagination.get("cursor"), request.get("changes_after")
            for token in (cursor, checkpoint):
                if token is not None and (
                    type(token) is not str or not 1 <= len(token) <= TOKEN_LIMIT
                ):
                    raise ValueError
            limit = pagination.get("max_results", 50)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError
            # All request extensions/unknown fields are conservatively semantic.
            # Account aliases resolve to the canonical caller outside this hash.
            filters = {
                k: v
                for k, v in request.items()
                if k
                not in {
                    "account",
                    "pagination",
                    "context",
                    "changes_after",
                    "adcp_version",
                    "adcp_major_version",
                    "reporting_revision_id",
                }
            }
            version = normalize_to_release_precision(
                request.get("adcp_version") or resolve_adcp_version(None)
            )
            if is_adcp_version_at_least(version, "3.2-rc.6"):
                # New snapshots bind their rendering contract. Integrated
                # parents' unversioned v1 walks retain their captured bytes;
                # StoredFeedSnapshot checks that narrow compatibility case.
                filters["adcp_version"] = version
            for name in (
                "delivery_config_ids",
                "media_buy_ids",
                "feed_purposes",
                "health",
                "finality",
            ):
                filters[name] = sorted(set(request.get(name) or []))
            period = request.get("period")
            if period is not None:
                dates = [
                    datetime.fromisoformat(period[k].replace("Z", "+00:00"))
                    for k in ("start", "end")
                ]
                if dates[0] >= dates[1]:
                    raise ValueError
                filters["period"] = {
                    k: v.astimezone(timezone.utc).isoformat()
                    for k, v in zip(("start", "end"), dates)
                }
            else:
                filters.pop("period", None)
            if not filters.get("ext"):
                filters.pop("ext", None)
            # Context and vendor filters accept ordinary JSON, including finite
            # fractional numbers. The restricted financial evidence encoder is
            # not their wire contract. Store deterministic JSON *bytes* as part
            # of the snapshot binding, without normalizing the caller's context.
            # Integral number spellings are transport-equivalent; fractional
            # values and nonnumeric JSON types retain their distinct meanings.
            if json.loads(json.dumps(request, allow_nan=False)) != request:
                raise ValueError
            encoded = json.dumps(
                _semantic_numbers(filters), allow_nan=False, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
            return cls(encoded, cursor, checkpoint, limit)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            pass
        raise ReportingFeedError("INVALID_REQUEST")
