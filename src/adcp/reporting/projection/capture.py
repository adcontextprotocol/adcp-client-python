"""Closed immutable v2 inputs preserve legacy C/B2.1/B2.2 document shapes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError
from pydantic_core import TzInfo

from adcp.reporting._timestamp import aware_timestamp
from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import aware_utc
from adcp.reporting.ledger._delivery_state import decode_record, payload, principal, record_identity
from adcp.reporting.ledger.delivery_models import ReportingDeliveryRecord
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.status_projection import ReportingStatusSnapshot
from adcp.reporting.ledger.status_snapshot import snapshot_from_storage

_CORE = TypeAdapter(ReportingStatusSnapshot)
_COLLECTIONS = (
    "configurations",
    "obligations",
    "revisions",
    "statuses",
    "lifecycles",
    "issue_scopes",
    "adjustments",
    "changes",
    "reconciliation",
)


def _require_frozen(value: Any) -> None:
    if value is None or type(value) in {str, bytes, int, bool, float, timedelta}:
        return
    if type(value) is datetime and type(value.tzinfo) in {timezone, ZoneInfo, TzInfo}:
        # Datetime itself is immutable; a user-defined tzinfo need not be.
        # These closed decoder timezone types cannot mutate shared history.
        return
    if type(value) is tuple:
        for item in value:
            _require_frozen(item)
        return
    if is_dataclass(value) and getattr(type(value), "__dataclass_params__").frozen:
        for item in fields(value):
            _require_frozen(getattr(value, item.name))
        return
    raise ReportingNotificationError("status_projection_history_corrupt")


@dataclass(frozen=True)
class ReportingProjectionInput:
    core: ReportingStatusSnapshot = field(repr=False)
    reconciliation: tuple[ReportingDeliveryRecord, ...] = field(repr=False)
    document: bytes = field(repr=False)

    def __post_init__(self) -> None:
        # Structural validation also protects direct internal construction:
        # frozen outer dataclasses alone do not make nested lists/dicts safe.
        _require_frozen(self.core)
        _require_frozen(self.reconciliation)
        if type(self.document) is not bytes:
            raise ReportingNotificationError("status_projection_history_corrupt")

    def __deepcopy__(self, memo: dict[int, Any]) -> ReportingProjectionInput:
        # Decoding closes every collection into tuples of frozen records. A
        # rollback copies the mutable account cursor, queues and input list;
        # the immutable historical values can safely remain shared. Copying
        # every earlier full-account snapshot on each new source mutation made
        # bounded production catch-up grow cubically in retained history.
        memo[id(self)] = self
        return self

    def at(self, at: datetime) -> ReportingProjectionInput:
        """Evaluate a deadline on the same captured history, never today's records."""
        return replace(self, core=replace(self.core, as_of=aware_utc(at)))


def capture_memory_input(
    core: ReportingStatusSnapshot,
    changes: tuple[tuple[int, Any, ReportingDeliveryRecord], ...],
) -> ReportingProjectionInput:
    raw = _CORE.dump_python(core, mode="json")
    entries = [
        {"consumer_id": who.consumer_id, "sequence": seq, "record": payload(record)}
        for seq, who, record in changes
        if who.account_id == core.account_id
    ]
    document = {
        "version": 2,
        "account_id": core.account_id,
        "as_of": core.as_of.isoformat(),
        "core_format": "typed-v1",
        "core": raw,
        "reconciliation": entries,
        "counts": {k: len(entries) if k == "reconciliation" else len(raw[k]) for k in _COLLECTIONS},
    }
    return decode_projection_input(document)


def decode_projection_input(value: Any) -> ReportingProjectionInput:
    try:
        if (
            type(value) is not dict
            or set(value)
            != {"version", "account_id", "as_of", "core_format", "core", "reconciliation", "counts"}
            or type(value["version"]) is not int
            or value["version"] != 2
            or value["core_format"] not in {"typed-v1", "sql-v1"}
            or type(value["core"]) is not dict
            or type(value["reconciliation"]) is not list
            or type(value["counts"]) is not dict
            or set(value["counts"]) != set(_COLLECTIONS)
        ):
            raise ValueError
        # Closure counts are checked before interpreting any financial evidence.
        for name in _COLLECTIONS:
            rows = value["reconciliation"] if name == "reconciliation" else value["core"][name]
            count = value["counts"][name]
            if type(rows) is not list or type(count) is not int or count != len(rows):
                raise ValueError
        core = (
            _CORE.validate_python(value["core"])
            if value["core_format"] == "typed-v1"
            else snapshot_from_storage(value["core"])
        )
        if core.account_id != value["account_id"] or core.as_of != aware_timestamp(value["as_of"]):
            raise ValueError
        positions: dict[str, int] = {}
        identities: set[tuple[str, tuple[str, str]]] = set()
        records = []
        for row in value["reconciliation"]:
            if type(row) is not dict or set(row) != {"consumer_id", "sequence", "record"}:
                raise ValueError
            record = decode_record(row["record"])
            who = principal(record)
            if who.account_id != core.account_id or who.consumer_id != row["consumer_id"]:
                raise ValueError
            sequence = row["sequence"]
            if type(sequence) is not int or sequence != positions.get(who.consumer_id, 0) + 1:
                raise ValueError
            positions[who.consumer_id] = sequence
            identity = who.consumer_id, record_identity(record)
            if identity in identities:
                raise ValueError
            identities.add(identity)
            records.append(record)
        if not set(positions).issubset(core.consumer_ids):
            raise ValueError
        for name in (
            "configurations",
            "obligations",
            "revisions",
            "statuses",
            "lifecycles",
            "adjustments",
        ):
            if any(row.account_id != core.account_id for row in getattr(core, name)):
                raise ValueError
        return ReportingProjectionInput(core, tuple(records), canonical_json_utf8_v1(value))
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
        raise ReportingNotificationError("status_projection_history_corrupt") from None


def source_identity(value: ReportingProjectionInput) -> bytes:
    """Memory transaction change detection excludes only the observation clock."""
    raw = json.loads(value.document)
    raw.pop("as_of")
    raw["core"].pop("as_of")
    return canonical_json_utf8_v1(raw)


def with_projection_core(
    value: ReportingProjectionInput, core: ReportingStatusSnapshot
) -> ReportingProjectionInput:
    """Runtime replay state is separate from each immutable source boundary."""
    raw = json.loads(value.document)
    raw.update(
        core_format="typed-v1",
        core=_CORE.dump_python(core, mode="json"),
        as_of=core.as_of.isoformat(),
    )
    raw["counts"] = {
        k: len(raw["reconciliation"]) if k == "reconciliation" else len(raw["core"][k])
        for k in _COLLECTIONS
    }
    return decode_projection_input(raw)
