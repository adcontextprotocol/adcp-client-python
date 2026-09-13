"""The durable seam under the reporting producer and the status handler.

:class:`ReportingLedgerStore` is deliberately small.  Everything interesting --
health, coverage roll-up, issue identity, period derivation -- is computed from
what the store returns, not stored by it.  A store's whole job is: keep
immutable records, keep them ordered, hand back a consistent snapshot, and
refuse to break the invariants that make the records evidence.

The invariants a conforming store MUST enforce, because they cannot be checked
after the fact:

* **Obligation identity is unique per (config generation, period).**  Two
  obligations for one logical period would let a seller quietly publish twice
  and pick a winner.
* **Revisions are immutable.**  Re-committing an existing
  ``reporting_revision_id`` with different content is an idempotency conflict,
  not an update.
* **At most one official revision per obligation.**  It is terminal; a later
  restatement is an adjustment.
* **Supersession is atomic and exact.**  A snapshot restatement names the
  current leaf; naming a stale leaf must fail rather than fork the chain.
* **The change feed is gapless per account.**  Every immutable write appends in
  the same transaction as the record it describes.

:class:`InMemoryReportingLedgerStore` is the reference implementation and the
conformance target.  It is genuinely usable in tests and single-process pilots,
and it is not durable -- Reliable Reporting's whole premise is retained
evidence, so production wants
:class:`~adcp.reporting.ledger.pg.PgReportingLedgerStore` or your own.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    LedgerRecordKind,
    LedgerSnapshot,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)

__all__ = [
    "InMemoryReportingLedgerStore",
    "LeasedConfiguration",
    "LedgerConflictError",
    "LedgerPage",
    "ReportingLedgerStore",
    "ReportingRowPage",
    "decode_cursor",
    "encode_cursor",
]


class LedgerConflictError(RuntimeError):
    """A write would violate an invariant that makes the ledger evidence.

    Carries a stable ``code`` so a handler can map it to a wire error without
    matching on prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LeasedConfiguration:
    """One configuration generation leased for period-close work.

    Carries its account so the producer never has to invert the lease back into
    a tenant -- an inversion that is easy to get subtly wrong when two accounts
    reuse a ``delivery_config_id``.
    """

    account_id: str
    delivery_config_id: str
    delivery_config_version: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class LedgerPage:
    """One page of the flat union of ledger records.

    Pagination is over the *flat union* of obligations, revisions, adjustments
    and consumer statuses rather than nesting history under each obligation.
    Nesting looks friendlier and is unbounded: one obligation with ten thousand
    snapshot restatements would make a single "page" unpageable.
    """

    obligations: tuple[ReportingObligationRecord, ...]
    revisions: tuple[ReportingRevisionRecord, ...]
    adjustments: tuple[ReportingAdjustmentRecord, ...]
    consumer_statuses: tuple[ConsumerStatusRecord, ...]
    total_count: int
    has_more: bool
    cursor: str | None


@dataclass(frozen=True)
class ReportingRowPage:
    """One page of an exact revision read.

    Every page repeats identical revision metadata and binding; a consumer
    hashes the concatenated rows in cursor order only after exhausting the
    frozen walk.  Do not substitute a fresh date-range pull for this read -- it
    may have changed since the immutable revision was published.
    """

    rows: tuple[dict[str, Any], ...]
    total_count: int
    has_more: bool
    cursor: str | None


def encode_cursor(payload: dict[str, Any]) -> str:
    """Opaque, self-describing cursor.

    Base64url over canonical JSON: opaque to the consumer, cheap for the seller
    to validate, and stable enough that the same logical position encodes
    identically on every page.  It is *not* signed -- the store re-validates
    caller, account, and snapshot on use, which is the check that actually
    matters.
    """
    return base64.urlsafe_b64encode(canonical_json_utf8_v1(payload)).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as error:
        raise LedgerConflictError(
            "INVALID_CURSOR", "the pagination cursor is not readable"
        ) from error
    if not isinstance(payload, dict):
        raise LedgerConflictError("INVALID_CURSOR", "the pagination cursor is not readable")
    return payload


@runtime_checkable
class ReportingLedgerStore(Protocol):
    """Durable home for obligations, revisions, adjustments, and statuses."""

    async def create_schema(self) -> None:
        """Idempotently create whatever this store needs. Safe on every boot."""
        ...

    # -- configurations --------------------------------------------------

    async def put_configuration(self, configuration: ReportingConfiguration) -> None:
        """Record an accepted configuration generation.

        A generation is immutable: re-putting one with changed content is a
        conflict, because a buyer that retained it derives expectations from it.
        """
        ...

    async def list_configurations(
        self, *, account_id: str, delivery_config_ids: Sequence[str] | None = None
    ) -> tuple[ReportingConfiguration, ...]: ...

    # -- obligations -----------------------------------------------------

    async def commit_obligation(
        self, obligation: ReportingObligationRecord
    ) -> ReportingObligationRecord:
        """Commit an obligation, or return the existing one for its period.

        Idempotent by ``(account, config generation, period)``, not by id: two
        workers racing a period close must converge on one obligation.
        """
        ...

    async def get_obligation(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> ReportingObligationRecord | None: ...

    async def find_obligation(
        self,
        *,
        account_id: str,
        delivery_config_id: str,
        delivery_config_version: int,
        period_start: datetime,
        period_end: datetime,
    ) -> ReportingObligationRecord | None: ...

    # -- revisions -------------------------------------------------------

    async def commit_revision(
        self,
        revision: ReportingRevisionRecord,
        rows: Sequence[dict[str, Any]],
    ) -> ReportingRevisionRecord:
        """Commit an immutable revision and its frozen rows.

        Enforces terminal officials, exact supersession, and idempotent replay
        of an identical ``reporting_revision_id``.
        """
        ...

    async def list_revisions(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> tuple[ReportingRevisionRecord, ...]: ...

    async def get_revision(
        self, *, account_id: str, reporting_revision_id: str
    ) -> ReportingRevisionRecord | None: ...

    async def read_revision_rows(
        self,
        *,
        account_id: str,
        reporting_revision_id: str,
        cursor: str | None = None,
        limit: int = 500,
    ) -> ReportingRowPage:
        """Walk one revision's frozen rows in stable order."""
        ...

    async def set_revision_readable(
        self, *, account_id: str, reporting_revision_id: str, readable: bool
    ) -> None:
        """Record that a revision's content is (no longer) readable.

        Retention expiry and storage loss are real; representing them is how an
        obligation becomes honestly ``action_required`` instead of staying
        ``complete`` over evidence nobody can read.
        """
        ...

    # -- adjustments -----------------------------------------------------

    async def commit_adjustment(
        self, adjustment: ReportingAdjustmentRecord
    ) -> ReportingAdjustmentRecord: ...

    async def list_adjustments(
        self, *, account_id: str, reporting_revision_ids: Sequence[str]
    ) -> tuple[ReportingAdjustmentRecord, ...]: ...

    # -- consumer status (preview) ---------------------------------------

    async def record_consumer_status(
        self, status: ConsumerStatusRecord
    ) -> tuple[ConsumerStatusRecord, bool]:
        """Append a consumer status statement, superseding the chain's leaf.

        Returns ``(record, recorded)`` where ``recorded`` is ``False`` for an
        exact idempotent replay.  Supersession is atomic: naming a stale or
        missing leaf must raise rather than fork the chain, so a successful
        retry cannot erase a recorded outage.
        """
        ...

    async def list_consumer_statuses(
        self,
        *,
        account_id: str,
        consumer_id: str,
        reporting_obligation_ids: Sequence[str] | None = None,
    ) -> tuple[ConsumerStatusRecord, ...]: ...

    # -- snapshots and pagination ----------------------------------------

    async def open_snapshot(self, *, account_id: str, filters_fingerprint: str) -> LedgerSnapshot:
        """Take a consistent read boundary for one account and filter set."""
        ...

    async def read_page(
        self,
        *,
        snapshot: LedgerSnapshot,
        consumer_id: str | None,
        delivery_config_ids: Sequence[str] | None,
        media_buy_ids: Sequence[str] | None,
        offset: int,
        limit: int,
        changes_after_sequence: int | None,
    ) -> LedgerPage: ...

    # -- worker leasing --------------------------------------------------

    async def lease_period_close(
        self, *, worker_id: str, now: datetime, lease_seconds: float
    ) -> LeasedConfiguration | None:
        """Lease one configuration generation for period-close work.

        Leasing rather than locking so a worker that dies mid-close releases its
        work by expiry instead of wedging the period forever, and so two workers
        cannot both close the same period.  Implementations should prefer the
        generation whose work is most overdue.
        """
        ...

    async def release_period_close(self, lease: LeasedConfiguration, *, worker_id: str) -> None: ...


# --------------------------------------------------------------------------
# Reference implementation
# --------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(value)).hexdigest()


def _revision_identity(revision: ReportingRevisionRecord) -> str:
    """What makes two commits of one revision id "the same revision".

    Content plus binding, excluding readability -- which is mutable state about
    storage, not about what was published.
    """
    return _fingerprint(
        {
            "finality": revision.finality,
            "revision_content_sha256": revision.revision_content_sha256,
            "row_count": revision.row_count,
            "control_totals": [list(item) for item in revision.control_totals],
            "obligation": revision.reporting_obligation_id,
            "supersedes": revision.supersedes_reporting_revision_id,
        }
    )


def _consumer_status_identity(status: ConsumerStatusRecord) -> str:
    return _fingerprint(
        {
            "chain": list(status.chain_key),
            "consumer_status": status.consumer_status,
            "status_as_of": _utc(status.status_as_of).isoformat(),
            "supersedes": status.supersedes_reporting_status_id,
            "obligation": status.reporting_obligation_id,
            "revision": status.reporting_revision_id,
            "digest": status.observed_revision_content_sha256,
            "failure_code": status.failure_code,
        }
    )


class InMemoryReportingLedgerStore:
    """Process-local reference store. Correct, ordered, and not durable.

    ``clock`` supplies the snapshot observation boundary, standing in for the
    database clock a durable store reads.  Override it to place a test's ledger
    boundary deliberately rather than wherever wall-clock time happens to fall.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._configurations: dict[tuple[str, int], ReportingConfiguration] = {}
        self._obligations: dict[str, ReportingObligationRecord] = {}
        self._obligation_by_period: dict[tuple[str, str, int, str, str], str] = {}
        self._revisions: dict[str, ReportingRevisionRecord] = {}
        self._revision_identity: dict[str, str] = {}
        self._rows: dict[str, tuple[dict[str, Any], ...]] = {}
        self._adjustments: dict[str, ReportingAdjustmentRecord] = {}
        self._statuses: dict[str, ConsumerStatusRecord] = {}
        self._status_identity: dict[str, str] = {}
        self._changes: list[tuple[int, str, LedgerRecordKind, str, datetime]] = []
        self._sequence = 0
        self._leases: dict[tuple[str, int], tuple[str, datetime]] = {}

    async def create_schema(self) -> None:
        return None

    def _append(self, account_id: str, kind: LedgerRecordKind, record_id: str) -> None:
        self._sequence += 1
        self._changes.append(
            (self._sequence, account_id, kind, record_id, datetime.now(timezone.utc))
        )

    # -- configurations --------------------------------------------------

    async def put_configuration(self, configuration: ReportingConfiguration) -> None:
        async with self._lock:
            key = configuration.generation_key
            existing = self._configurations.get(key)
            if existing is not None and _fingerprint(_config_payload(existing)) != _fingerprint(
                _config_payload(configuration)
            ):
                raise LedgerConflictError(
                    "CONFIGURATION_GENERATION_IMMUTABLE",
                    f"configuration {key[0]}@{key[1]} already exists with different content; "
                    "publish a new version instead of editing a retained generation",
                )
            self._configurations[key] = configuration

    async def list_configurations(
        self, *, account_id: str, delivery_config_ids: Sequence[str] | None = None
    ) -> tuple[ReportingConfiguration, ...]:
        wanted = set(delivery_config_ids) if delivery_config_ids else None
        return tuple(
            configuration
            for configuration in self._configurations.values()
            if configuration.account_id == account_id
            and (wanted is None or configuration.delivery_config_id in wanted)
        )

    # -- obligations -----------------------------------------------------

    async def commit_obligation(
        self, obligation: ReportingObligationRecord
    ) -> ReportingObligationRecord:
        async with self._lock:
            key = (
                obligation.account_id,
                obligation.delivery_config_id,
                obligation.delivery_config_version,
                _utc(obligation.period.start).isoformat(),
                _utc(obligation.period.end).isoformat(),
            )
            existing_id = self._obligation_by_period.get(key)
            if existing_id is not None:
                return self._obligations[existing_id]
            self._obligations[obligation.reporting_obligation_id] = obligation
            self._obligation_by_period[key] = obligation.reporting_obligation_id
            self._append(obligation.account_id, "obligation", obligation.reporting_obligation_id)
            return obligation

    async def get_obligation(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> ReportingObligationRecord | None:
        found = self._obligations.get(reporting_obligation_id)
        return found if found and found.account_id == account_id else None

    async def find_obligation(
        self,
        *,
        account_id: str,
        delivery_config_id: str,
        delivery_config_version: int,
        period_start: datetime,
        period_end: datetime,
    ) -> ReportingObligationRecord | None:
        key = (
            account_id,
            delivery_config_id,
            delivery_config_version,
            _utc(period_start).isoformat(),
            _utc(period_end).isoformat(),
        )
        found = self._obligation_by_period.get(key)
        return self._obligations.get(found) if found else None

    # -- revisions -------------------------------------------------------

    async def commit_revision(
        self, revision: ReportingRevisionRecord, rows: Sequence[dict[str, Any]]
    ) -> ReportingRevisionRecord:
        async with self._lock:
            identity = _revision_identity(revision)
            existing = self._revisions.get(revision.reporting_revision_id)
            if existing is not None:
                if self._revision_identity[revision.reporting_revision_id] != identity:
                    raise LedgerConflictError(
                        "REVISION_IMMUTABLE",
                        f"revision {revision.reporting_revision_id} already exists with "
                        "different content; a restatement is a new revision",
                    )
                return existing
            obligation = self._obligations.get(revision.reporting_obligation_id)
            if obligation is None or obligation.account_id != revision.account_id:
                raise LedgerConflictError(
                    "OBLIGATION_NOT_FOUND",
                    "a revision must attach to an obligation committed at the period close",
                )
            siblings = [
                item
                for item in self._revisions.values()
                if item.reporting_obligation_id == revision.reporting_obligation_id
            ]
            if revision.finality == "official" and any(
                item.finality == "official" for item in siblings
            ):
                raise LedgerConflictError(
                    "OFFICIAL_REVISION_TERMINAL",
                    "an official revision already exists for this obligation; publish a later "
                    "source correction as an adjustment",
                )
            if revision.supersedes_reporting_revision_id:
                self._require_current_leaf(revision, siblings)
            if revision.row_count != len(rows):
                raise LedgerConflictError(
                    "ROW_COUNT_MISMATCH",
                    f"revision declares {revision.row_count} rows but {len(rows)} were supplied",
                )
            self._revisions[revision.reporting_revision_id] = revision
            self._revision_identity[revision.reporting_revision_id] = identity
            self._rows[revision.reporting_revision_id] = tuple(dict(row) for row in rows)
            self._append(revision.account_id, "revision", revision.reporting_revision_id)
            return revision

    def _require_current_leaf(
        self, revision: ReportingRevisionRecord, siblings: Sequence[ReportingRevisionRecord]
    ) -> None:
        target = revision.supersedes_reporting_revision_id
        known = {item.reporting_revision_id for item in siblings}
        if target not in known:
            raise LedgerConflictError(
                "SUPERSEDES_UNKNOWN",
                f"revision {target} is not part of this obligation's chain",
            )
        already = {
            item.supersedes_reporting_revision_id
            for item in siblings
            if item.supersedes_reporting_revision_id
        }
        if target in already:
            raise LedgerConflictError(
                "SUPERSEDES_STALE",
                f"revision {target} has already been superseded; a stale pointer would fork "
                "the chain and let a successful retry erase a recorded restatement",
            )

    async def list_revisions(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> tuple[ReportingRevisionRecord, ...]:
        return tuple(
            item
            for item in self._revisions.values()
            if item.account_id == account_id
            and item.reporting_obligation_id == reporting_obligation_id
        )

    async def get_revision(
        self, *, account_id: str, reporting_revision_id: str
    ) -> ReportingRevisionRecord | None:
        found = self._revisions.get(reporting_revision_id)
        return found if found and found.account_id == account_id else None

    async def read_revision_rows(
        self,
        *,
        account_id: str,
        reporting_revision_id: str,
        cursor: str | None = None,
        limit: int = 500,
    ) -> ReportingRowPage:
        revision = await self.get_revision(
            account_id=account_id, reporting_revision_id=reporting_revision_id
        )
        if revision is None:
            raise LedgerConflictError("REVISION_NOT_FOUND", "no such revision for this account")
        rows = self._rows.get(reporting_revision_id, ())
        offset = int(decode_cursor(cursor).get("offset", 0)) if cursor else 0
        window = rows[offset : offset + limit]
        has_more = offset + limit < len(rows)
        return ReportingRowPage(
            rows=tuple(dict(row) for row in window),
            total_count=len(rows),
            has_more=has_more,
            cursor=(
                encode_cursor({"revision": reporting_revision_id, "offset": offset + limit})
                if has_more
                else None
            ),
        )

    async def set_revision_readable(
        self, *, account_id: str, reporting_revision_id: str, readable: bool
    ) -> None:
        async with self._lock:
            existing = self._revisions.get(reporting_revision_id)
            if existing is None or existing.account_id != account_id:
                raise LedgerConflictError("REVISION_NOT_FOUND", "no such revision for this account")
            self._revisions[reporting_revision_id] = replace(existing, readable=readable)

    # -- adjustments -----------------------------------------------------

    async def commit_adjustment(
        self, adjustment: ReportingAdjustmentRecord
    ) -> ReportingAdjustmentRecord:
        async with self._lock:
            existing = self._adjustments.get(adjustment.reporting_adjustment_id)
            if existing is not None:
                return existing
            revision = self._revisions.get(adjustment.adjusts_reporting_revision_id)
            if revision is None or revision.account_id != adjustment.account_id:
                raise LedgerConflictError(
                    "REVISION_NOT_FOUND", "an adjustment must name a committed revision"
                )
            if revision.finality != "official":
                raise LedgerConflictError(
                    "ADJUSTMENT_REQUIRES_OFFICIAL",
                    "adjustments correct an official revision; restate a snapshot with a "
                    "superseding snapshot revision instead",
                )
            self._adjustments[adjustment.reporting_adjustment_id] = adjustment
            self._append(adjustment.account_id, "adjustment", adjustment.reporting_adjustment_id)
            return adjustment

    async def list_adjustments(
        self, *, account_id: str, reporting_revision_ids: Sequence[str]
    ) -> tuple[ReportingAdjustmentRecord, ...]:
        wanted = set(reporting_revision_ids)
        return tuple(
            item
            for item in self._adjustments.values()
            if item.account_id == account_id and item.adjusts_reporting_revision_id in wanted
        )

    # -- consumer status -------------------------------------------------

    async def record_consumer_status(
        self, status: ConsumerStatusRecord
    ) -> tuple[ConsumerStatusRecord, bool]:
        async with self._lock:
            identity = _consumer_status_identity(status)
            existing = self._statuses.get(status.reporting_status_id)
            if existing is not None:
                if self._status_identity[status.reporting_status_id] != identity:
                    raise LedgerConflictError(
                        "STATUS_IDENTITY_CONFLICT",
                        f"reporting_status_id {status.reporting_status_id} was already "
                        "recorded with different content",
                    )
                return existing, False
            leaf = self._current_status_leaf(status.chain_key)
            if status.supersedes_reporting_status_id:
                if (
                    leaf is None
                    or leaf.reporting_status_id != status.supersedes_reporting_status_id
                ):
                    raise LedgerConflictError(
                        "STATUS_SUPERSEDES_STALE",
                        "supersedes_reporting_status_id must name this chain's current leaf; "
                        "a stale pointer would let a successful retry erase a recorded outage",
                    )
                self._statuses[leaf.reporting_status_id] = replace(leaf, superseded=True)
            elif leaf is not None:
                raise LedgerConflictError(
                    "STATUS_SUPERSEDES_REQUIRED",
                    "this chain already has a current statement; a new statement must "
                    "explicitly supersede it",
                )
            self._statuses[status.reporting_status_id] = status
            self._status_identity[status.reporting_status_id] = identity
            self._append(status.account_id, "consumer_status", status.reporting_status_id)
            return status, True

    def _current_status_leaf(self, chain_key: tuple[Any, ...]) -> ConsumerStatusRecord | None:
        for item in self._statuses.values():
            if item.chain_key == chain_key and not item.superseded:
                return item
        return None

    async def list_consumer_statuses(
        self,
        *,
        account_id: str,
        consumer_id: str,
        reporting_obligation_ids: Sequence[str] | None = None,
    ) -> tuple[ConsumerStatusRecord, ...]:
        wanted = set(reporting_obligation_ids) if reporting_obligation_ids is not None else None
        result = []
        for item in self._statuses.values():
            if item.account_id != account_id or item.consumer_id != consumer_id:
                continue
            if wanted is not None and not self._matches_obligations(item, wanted):
                continue
            result.append(item)
        return tuple(result)

    def _matches_obligations(self, status: ConsumerStatusRecord, wanted: set[str]) -> bool:
        """Attach a statement to an obligation by id *or* by logical period key.

        The logical-key path is what makes the loop repairable: a statement
        filed as ``obligation_missing`` predates the obligation it is about, and
        when the seller later creates that obligation the existing chain must
        attach to it rather than being lost, forked, or reset.
        """
        if status.reporting_obligation_id in wanted:
            return True
        for obligation_id in wanted:
            obligation = self._obligations.get(obligation_id)
            if obligation is None:
                continue
            if (
                obligation.delivery_config_id == status.delivery_config_id
                and obligation.delivery_config_version == status.delivery_config_version
                and obligation.report_definition_id == status.report_definition_id
                and _utc(obligation.period.start) == _utc(status.period_start)
                and _utc(obligation.period.end) == _utc(status.period_end)
            ):
                return True
        return False

    # -- snapshots -------------------------------------------------------

    async def open_snapshot(self, *, account_id: str, filters_fingerprint: str) -> LedgerSnapshot:
        async with self._lock:
            as_of = _utc(self._clock())
            max_sequence = max(
                (item[0] for item in self._changes if item[1] == account_id), default=0
            )
            return LedgerSnapshot(
                snapshot_id="rpls_"
                + _fingerprint([account_id, filters_fingerprint, max_sequence])[:32],
                account_id=account_id,
                ledger_as_of=as_of,
                max_sequence=max_sequence,
            )

    async def read_page(
        self,
        *,
        snapshot: LedgerSnapshot,
        consumer_id: str | None,
        delivery_config_ids: Sequence[str] | None,
        media_buy_ids: Sequence[str] | None,
        offset: int,
        limit: int,
        changes_after_sequence: int | None,
    ) -> LedgerPage:
        lower = changes_after_sequence or 0
        selected = [
            item
            for item in self._changes
            if item[1] == snapshot.account_id and lower < item[0] <= snapshot.max_sequence
        ]
        config_filter = set(delivery_config_ids) if delivery_config_ids else None
        media_buy_filter = set(media_buy_ids) if media_buy_ids else None

        records: list[tuple[int, LedgerRecordKind, Any]] = []
        for sequence, _account, kind, record_id, _committed in sorted(selected):
            record = self._resolve(kind, record_id)
            if record is None:
                continue
            if not self._in_scope(kind, record, config_filter, media_buy_filter, consumer_id):
                continue
            records.append((sequence, kind, record))

        window = records[offset : offset + limit]
        has_more = offset + limit < len(records)
        return LedgerPage(
            obligations=tuple(item[2] for item in window if item[1] == "obligation"),
            revisions=tuple(item[2] for item in window if item[1] == "revision"),
            adjustments=tuple(item[2] for item in window if item[1] == "adjustment"),
            consumer_statuses=tuple(item[2] for item in window if item[1] == "consumer_status"),
            total_count=len(records),
            has_more=has_more,
            cursor=(
                encode_cursor({"snapshot": snapshot.snapshot_id, "offset": offset + limit})
                if has_more
                else None
            ),
        )

    def _resolve(self, kind: LedgerRecordKind, record_id: str) -> Any:
        if kind == "obligation":
            return self._obligations.get(record_id)
        if kind == "revision":
            return self._revisions.get(record_id)
        if kind == "adjustment":
            return self._adjustments.get(record_id)
        return self._statuses.get(record_id)

    def _in_scope(
        self,
        kind: LedgerRecordKind,
        record: Any,
        config_filter: set[str] | None,
        media_buy_filter: set[str] | None,
        consumer_id: str | None,
    ) -> bool:
        if kind == "consumer_status":
            # A caller sees only its own statements; another consumer's
            # operational status is not disclosed.
            return consumer_id is not None and record.consumer_id == consumer_id
        obligation = self._obligation_for(kind, record)
        if obligation is None:
            return False
        if config_filter is not None and obligation.delivery_config_id not in config_filter:
            return False
        if media_buy_filter is not None and not media_buy_filter.intersection(
            obligation.media_buy_ids
        ):
            return False
        return True

    def _obligation_for(
        self, kind: LedgerRecordKind, record: Any
    ) -> ReportingObligationRecord | None:
        if kind == "obligation":
            found: ReportingObligationRecord = record
            return found
        if kind == "revision":
            return self._obligations.get(record.reporting_obligation_id)
        revision = self._revisions.get(record.adjusts_reporting_revision_id)
        return self._obligations.get(revision.reporting_obligation_id) if revision else None

    # -- leasing ---------------------------------------------------------

    async def lease_period_close(
        self, *, worker_id: str, now: datetime, lease_seconds: float
    ) -> LeasedConfiguration | None:
        from datetime import timedelta

        async with self._lock:
            for key, configuration in self._configurations.items():
                held = self._leases.get(key)
                if held is not None and _utc(held[1]) > _utc(now):
                    continue
                expires = _utc(now) + timedelta(seconds=lease_seconds)
                self._leases[key] = (worker_id, expires)
                return LeasedConfiguration(
                    account_id=configuration.account_id,
                    delivery_config_id=configuration.delivery_config_id,
                    delivery_config_version=configuration.delivery_config_version,
                    lease_expires_at=expires,
                )
            return None

    async def release_period_close(self, lease: LeasedConfiguration, *, worker_id: str) -> None:
        async with self._lock:
            key = (lease.delivery_config_id, lease.delivery_config_version)
            held = self._leases.get(key)
            if held is not None and held[0] == worker_id:
                del self._leases[key]


def _config_payload(configuration: ReportingConfiguration) -> dict[str, Any]:
    schedule = configuration.schedule
    return {
        "account_id": configuration.account_id,
        "report_definition_id": configuration.report_definition_id,
        "reporting_profile": configuration.reporting_profile,
        "feed_purpose": configuration.feed_purpose,
        "required_finality": configuration.required_finality,
        "account_timezone": configuration.account_timezone,
        "media_buy_ids": sorted(configuration.media_buy_ids),
        "schedule": {
            "period_duration": schedule.period_duration,
            "delivery_sla": schedule.delivery_sla,
            "alignment": schedule.alignment,
            "period_timezone": schedule.period_timezone,
            "period_anchor": (
                _utc(schedule.period_anchor).isoformat() if schedule.period_anchor else None
            ),
        },
    }
