"""Consumer-scoped incremental reconciliation reads, independent of Core cursors."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, NewType, Protocol, get_args, runtime_checkable

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import aware_utc, reporting_identifier
from adcp.reporting.ledger._delivery_state import change_id, fail, principal
from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingDestinationBinding,
    ReportingReconciliationRecordKind,
)
from adcp.reporting.ledger.store import LedgerConflictError, decode_cursor, encode_cursor

_FEED = "reporting-reconciliation-v1"
ReportingReconciliationCursor = NewType("ReportingReconciliationCursor", str)
ReportingReconciliationCheckpoint = NewType("ReportingReconciliationCheckpoint", str)


def _is_record_kind(value: object) -> bool:
    return type(value) is str and value in get_args(ReportingReconciliationRecordKind)


@dataclass(frozen=True, slots=True)
class ReportingReconciliationFilter:
    record_kinds: tuple[ReportingReconciliationRecordKind, ...] = ()
    reporting_obligation_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.record_kinds) is not tuple or any(
            not _is_record_kind(kind) for kind in self.record_kinds
        ):
            raise ValueError("invalid reconciliation record filter")
        object.__setattr__(self, "record_kinds", tuple(sorted(set(self.record_kinds))))
        if self.reporting_obligation_id is not None:
            reporting_identifier(self.reporting_obligation_id)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json_utf8_v1(asdict(self))).hexdigest()

    def matches(self, record: ReportingDeliveryRecord) -> bool:
        return (not self.record_kinds or record.kind in self.record_kinds) and (
            self.reporting_obligation_id is None
            or (
                not isinstance(record, ReportingDestinationBinding)
                and record.scope.reporting_obligation_id == self.reporting_obligation_id
            )
        )


@dataclass(frozen=True, slots=True)
class ReportingReconciliationSnapshotToken:
    """A frozen boundary in one principal's feed, never a Core ledger sequence."""

    caller: ReportingDeliveryPrincipal
    snapshot_id: str
    ledger_as_of: datetime
    min_sequence: int
    max_sequence: int
    total_count: int
    filters: ReportingReconciliationFilter

    @property
    def account_id(self) -> str:
        return self.caller.account_id

    @property
    def consumer_id(self) -> str:
        return self.caller.consumer_id


@dataclass(frozen=True, slots=True)
class ReportingReconciliationChange:
    sequence: int
    record: ReportingDeliveryRecord


@dataclass(frozen=True, slots=True)
class ReportingReconciliationPage:
    caller: ReportingDeliveryPrincipal
    boundary: ReportingReconciliationSnapshotToken
    changes: tuple[ReportingReconciliationChange, ...]
    has_more: bool
    cursor: ReportingReconciliationCursor | None
    changes_checkpoint: ReportingReconciliationCheckpoint | None

    @property
    def total_count(self) -> int:
        return self.boundary.total_count


@runtime_checkable
class ReportingReconciliationFeedStore(Protocol):
    """An optional seam; existing Core and reconciliation protocols are unchanged.

    Persist partial pages with their cursor. Only the final page issues a
    checkpoint, which opens a new walk via ``changes_after``. Tokens bind caller,
    version, filters, bounds and the last emitted key, and survive reader restarts.
    Neither token is an authorization grant or a Core status checkpoint.
    """

    async def read_reconciliation_changes(
        self,
        *,
        caller: ReportingDeliveryPrincipal,
        changes_after: ReportingReconciliationCheckpoint | None = None,
        cursor: ReportingReconciliationCursor | None = None,
        limit: int = 100,
        filters: ReportingReconciliationFilter = ReportingReconciliationFilter(),
    ) -> ReportingReconciliationPage: ...


# Retain the early storage-slice spelling for callers already evaluating the PR.
ReportingReconciliationChangeStore = ReportingReconciliationFeedStore


def _token_scope(
    caller: ReportingDeliveryPrincipal, filters: ReportingReconciliationFilter
) -> dict[str, str | int]:
    return {
        "feed": _FEED,
        "version": 1,
        "account": caller.account_id,
        "consumer": caller.consumer_id,
        "filter": filters.fingerprint,
    }


def _decode_position(
    value: str, caller: ReportingDeliveryPrincipal, filters: ReportingReconciliationFilter
) -> dict[str, Any]:
    decoded: dict[str, Any] | None = None
    try:
        if type(value) is str and 0 < len(value) <= 4096:
            decoded = decode_cursor(value)
    except (LedgerConflictError, TypeError, ValueError):
        # Reject below without retaining the decoder's exception context.
        pass
    if (
        decoded is None
        or type(decoded.get("version")) is not int
        or any(decoded.get(k) != v for k, v in _token_scope(caller, filters).items())
    ):
        raise LedgerConflictError("INVALID_CHECKPOINT", "reconciliation position is unavailable")
    return decoded


def change_boundary(
    caller: ReportingDeliveryPrincipal,
    maximum: int,
    as_of: datetime,
    *,
    after: int = 0,
    total_count: int | None = None,
    filters: ReportingReconciliationFilter = ReportingReconciliationFilter(),
) -> ReportingReconciliationSnapshotToken:
    count = maximum - after if total_count is None else total_count
    if (
        any(type(value) is not int for value in (after, maximum, count))
        or not 0 <= after <= maximum
        or not 0 <= count <= maximum - after
    ):
        raise LedgerConflictError("INVALID_CHECKPOINT", "invalid reconciliation boundary")
    identity = hashlib.sha256(
        canonical_json_utf8_v1([_token_scope(caller, filters), after, maximum, count])
    ).hexdigest()
    return ReportingReconciliationSnapshotToken(
        caller, "rprc_" + identity[:32], aware_utc(as_of), after, maximum, count, filters
    )


def validate_boundary(
    caller: ReportingDeliveryPrincipal,
    boundary: ReportingReconciliationSnapshotToken,
    maximum: int,
) -> None:
    valid = False
    try:
        if (
            type(boundary) is ReportingReconciliationSnapshotToken
            and boundary.caller == caller
            and type(boundary.filters) is ReportingReconciliationFilter
        ):
            expected = change_boundary(
                caller,
                boundary.max_sequence,
                boundary.ledger_as_of,
                after=boundary.min_sequence,
                total_count=boundary.total_count,
                filters=boundary.filters,
            )
            valid = boundary == expected and boundary.max_sequence <= maximum
    except (ValueError, TypeError, LedgerConflictError):
        # Keep the boundary invalid and classify outside the exception handler.
        pass
    if not valid:
        raise LedgerConflictError("INVALID_CHECKPOINT", "reconciliation boundary is unavailable")


def read_position(
    caller: ReportingDeliveryPrincipal,
    changes_after: ReportingReconciliationCheckpoint | None,
    cursor: ReportingReconciliationCursor | None,
    limit: int,
    filters: ReportingReconciliationFilter,
) -> tuple[int, ReportingReconciliationSnapshotToken | None, str | None]:
    if (
        type(limit) is not int
        or not 1 <= limit <= 1000
        or (changes_after is not None and cursor is not None)
        or type(caller) is not ReportingDeliveryPrincipal
        or type(filters) is not ReportingReconciliationFilter
    ):
        raise LedgerConflictError("INVALID_CHECKPOINT", "invalid reconciliation page request")
    if cursor is None and changes_after is None:
        return 0, None, None
    position = _decode_position(
        cursor if cursor is not None else changes_after or "", caller, filters
    )
    sequence = position.get("seq")
    expected = {*_token_scope(caller, filters), "type", "seq"}
    if cursor is not None:
        expected.update(("after", "through", "as_of", "count", "snapshot", "key"))
    if (
        set(position) != expected
        or type(sequence) is not int
        or sequence < 0
        or position["type"] != ("cursor" if cursor is not None else "checkpoint")
    ):
        raise LedgerConflictError("INVALID_CHECKPOINT", "invalid reconciliation position")
    if cursor is None:
        return sequence, None, None
    through, after, as_of = position["through"], position["after"], position["as_of"]
    boundary = None
    if (
        type(through) is int
        and type(after) is int
        and 0 <= after < sequence < through
        and type(as_of) is str
        and type(position["count"]) is int
        and type(position["key"]) is str
        and len(position["key"]) == 64
    ):
        try:
            boundary = change_boundary(
                caller,
                through,
                datetime.fromisoformat(as_of),
                after=after,
                total_count=position["count"],
                filters=filters,
            )
        except (ValueError, LedgerConflictError):
            # Reject below without exposing the parse or validation exception.
            pass
    if boundary is None or position["snapshot"] != boundary.snapshot_id:
        raise LedgerConflictError("INVALID_CHECKPOINT", "invalid reconciliation boundary")
    return sequence, boundary, position["key"]


def change_page(
    caller: ReportingDeliveryPrincipal,
    boundary: ReportingReconciliationSnapshotToken,
    after: int,
    changes: tuple[ReportingReconciliationChange, ...],
    limit: int,
) -> ReportingReconciliationPage:
    validate_boundary(caller, boundary, boundary.max_sequence)
    if not boundary.min_sequence <= after <= boundary.max_sequence:
        raise LedgerConflictError("INVALID_CHECKPOINT", "reconciliation position is unavailable")
    previous = after
    for change in changes:
        if (
            type(change.sequence) is not int
            or not previous < change.sequence <= boundary.max_sequence
            or principal(change.record) != caller
            or not boundary.filters.matches(change.record)
        ):
            fail("REPORTING_HISTORY_CORRUPT")
        previous = change.sequence
    window, more = changes[:limit], len(changes) > limit
    position = _token_scope(caller, boundary.filters)
    return ReportingReconciliationPage(
        caller,
        boundary,
        window,
        more,
        (
            ReportingReconciliationCursor(
                encode_cursor(
                    {
                        **position,
                        "type": "cursor",
                        "seq": window[-1].sequence,
                        "key": change_id(window[-1].record),
                        "after": boundary.min_sequence,
                        "through": boundary.max_sequence,
                        "as_of": boundary.ledger_as_of.isoformat(),
                        "count": boundary.total_count,
                        "snapshot": boundary.snapshot_id,
                    }
                )
            )
            if more
            else None
        ),
        (
            None
            if more
            else ReportingReconciliationCheckpoint(
                encode_cursor({**position, "type": "checkpoint", "seq": boundary.max_sequence})
            )
        ),
    )
