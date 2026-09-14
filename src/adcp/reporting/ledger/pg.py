"""PostgreSQL-backed :class:`~adcp.reporting.ledger.store.ReportingLedgerStore`.

Durable counterpart to
:class:`~adcp.reporting.ledger.store.InMemoryReportingLedgerStore`, following
the same shape as :class:`adcp.decisioning.PgTaskRegistry`: the caller supplies
an :class:`psycopg_pool.AsyncConnectionPool`, we never open, own, or close it,
and each operation takes a short-lived connection.

Quickstart
----------

::

    from psycopg_pool import AsyncConnectionPool
    from adcp.reporting.ledger import PgReportingLedgerStore, ReportingProducer

    async with AsyncConnectionPool(dsn, min_size=2, max_size=10) as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()          # idempotent; safe on every boot
        producer = ReportingProducer(source=..., offerings=..., store=store)
        await producer.run_worker()

Schema bootstrap
----------------

:meth:`create_schema` is idempotent.  The equivalent raw DDL ships at
:file:`src/adcp/reporting/ledger/reporting_ledger.sql` for adopters using
Alembic, Flyway, or psql.

Where the invariants actually live
----------------------------------

In the schema, not in Python.  A ledger that enforces "one official revision
per obligation" in application code is one deploy away from two workers racing
past it.  The partial unique indexes in the DDL are the enforcement:

* one obligation per ``(account, config generation, period)``;
* one official revision per obligation;
* one successor per superseded revision;
* one unsuperseded consumer-status leaf per logical chain.

Python turns the resulting integrity errors into typed
:class:`~adcp.reporting.ledger.store.LedgerConflictError` values so a handler can
map them to wire errors without matching on driver prose.

A note on the ``# noqa: S608  # nosec B608`` comments
----------------------------------------------------

Several queries f-string a column list or a filter clause into the SQL.  Every
interpolated value is a module-level constant defined in this file -- column
lists and literal clause fragments -- and never a caller-supplied value.
Caller data is always bound as a ``%s`` parameter.  The alternative, repeating
twenty column names inline at each of nine call sites, is how column lists and
row mappers drift apart.

Change-feed ordering
--------------------

Every immutable write appends to ``reporting_ledger_changes`` in the *same*
transaction, under a transaction-scoped advisory lock on the account.  That
lock is what makes sequence order equal commit order per account: without it a
transaction could take a low sequence, commit late, and be invisible to a
consumer that had already checkpointed past it.  A silently lost record is the
one failure mode a reporting ledger must not have.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.health import issue_id_for_occurrence
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    LedgerRecordKind,
    LedgerSnapshot,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingDefinitionBinding,
    ReportingIssueLifecycle,
    ReportingObligationRecord,
    ReportingPeriodBoundary,
    ReportingRevisionRecord,
    ReportingScheduleSpec,
)
from adcp.reporting.ledger.store import (
    LeasedConfiguration,
    LedgerConflictError,
    LedgerPage,
    ReportingRowPage,
    check_issue_state_transition,
    decode_cursor,
    encode_cursor,
    issue_is_retirable,
    reject_reserved_authoritative_party,
)

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

try:
    from psycopg_pool import AsyncConnectionPool as _AsyncConnectionPool  # noqa: F401

    PG_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the install-hint path
    PG_AVAILABLE = False

_INSTALL_HINT = (
    "PgReportingLedgerStore requires psycopg3 and psycopg-pool. "
    "Install the 'pg' extra: `pip install 'adcp[pg]'`."
)

_DDL_PATH = Path(__file__).parent / "reporting_ledger.sql"

__all__ = ["PG_AVAILABLE", "PgReportingLedgerStore"]


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(value)).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


class PgReportingLedgerStore:
    """Durable reporting ledger over a caller-supplied connection pool."""

    is_durable: ClassVar[bool] = True

    def __init__(
        self,
        *,
        pool: AsyncConnectionPool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        self._pool = pool
        # The snapshot observation boundary. Defaults to the *database* clock,
        # which is what makes two readers of one snapshot agree even across
        # application hosts with drifting clocks -- do not override it in
        # production for that reason. Overriding is for replay, backfill, and
        # tests that need to stand at a specific instant relative to seeded
        # evidence rather than wherever wall-clock time happens to fall.
        self._clock = clock

    async def create_schema(self) -> None:
        """Create every ledger table and index. Idempotent; safe on every boot."""
        async with self._pool.connection() as connection:
            await connection.execute(_DDL_PATH.read_text())

    # -- change feed ------------------------------------------------------

    async def _append_change(
        self, connection: Any, account_id: str, kind: LedgerRecordKind, record_id: str
    ) -> None:
        await connection.execute(
            "INSERT INTO reporting_ledger_changes (account_id, record_kind, record_id)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (account_id, record_kind, record_id) DO NOTHING",
            (account_id, kind, record_id),
        )

    @staticmethod
    async def _lock_account(connection: Any, account_id: str) -> None:
        """Serialize this account's feed appends so seq order == commit order."""
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"adcp.reporting:{account_id}",)
        )

    # -- configurations ---------------------------------------------------

    async def put_configuration(self, configuration: ReportingConfiguration) -> None:
        reject_reserved_authoritative_party(configuration)
        payload = _configuration_payload(configuration)
        digest = _fingerprint(payload)
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT content_sha256 FROM reporting_configurations"
                    " WHERE delivery_config_id = %s AND delivery_config_version = %s",
                    (configuration.delivery_config_id, configuration.delivery_config_version),
                )
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise LedgerConflictError(
                        "CONFIGURATION_GENERATION_IMMUTABLE",
                        f"configuration {configuration.delivery_config_id}"
                        f"@{configuration.delivery_config_version} already exists with "
                        "different content; publish a new version instead of editing a "
                        "retained generation",
                    )
                return
            await connection.execute(
                "INSERT INTO reporting_configurations"
                " (delivery_config_id, delivery_config_version, account_id,"
                "  report_definition_id, reporting_profile, feed_purpose, required_finality,"
                "  account_timezone, schedule, media_buy_ids, activated_at, deactivated_at,"
                "  automated_recovery_seconds, status_retention_days, definition,"
                "  authoritative_party, content_sha256)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s,"
                "         %s::jsonb, %s, %s)"
                " ON CONFLICT (delivery_config_id, delivery_config_version) DO NOTHING",
                (
                    configuration.delivery_config_id,
                    configuration.delivery_config_version,
                    configuration.account_id,
                    configuration.report_definition_id,
                    configuration.reporting_profile,
                    configuration.feed_purpose,
                    configuration.required_finality,
                    configuration.account_timezone,
                    _json(payload["schedule"]),
                    _json(sorted(configuration.media_buy_ids)),
                    configuration.activated_at,
                    configuration.deactivated_at,
                    configuration.automated_recovery_window.total_seconds(),
                    configuration.status_retention_days,
                    _json(payload["definition"]) if payload["definition"] else None,
                    configuration.authoritative_party,
                    digest,
                ),
            )

    async def list_configurations(
        self, *, account_id: str, delivery_config_ids: Sequence[str] | None = None
    ) -> tuple[ReportingConfiguration, ...]:
        clause = " AND delivery_config_id = ANY(%s)" if delivery_config_ids else ""
        params: list[Any] = [account_id]
        if delivery_config_ids:
            params.append(list(delivery_config_ids))
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT delivery_config_id, delivery_config_version, account_id,"  # noqa: S608  # nosec B608
                    " report_definition_id, reporting_profile, feed_purpose, required_finality,"
                    " account_timezone, schedule, media_buy_ids, activated_at, deactivated_at,"
                    " automated_recovery_seconds, status_retention_days, definition,"
                    " authoritative_party"
                    " FROM reporting_configurations"
                    f" WHERE account_id = %s{clause}"  # noqa: S608 — clause is a literal
                    " ORDER BY delivery_config_id, delivery_config_version",
                    tuple(params),
                )
            ).fetchall()
        return tuple(_configuration_from_row(row) for row in rows)

    # -- obligations ------------------------------------------------------

    async def commit_obligation(
        self, obligation: ReportingObligationRecord
    ) -> ReportingObligationRecord:
        async with self._pool.connection() as connection:
            await self._lock_account(connection, obligation.account_id)
            inserted = await (
                await connection.execute(
                    "INSERT INTO reporting_obligations"
                    " (reporting_obligation_id, account_id, delivery_config_id,"
                    "  delivery_config_version, report_definition_id, reporting_profile,"
                    "  feed_purpose, period_key, period_start, period_end, source_timezone,"
                    "  expected_at, scope_resolved_at, automated_recovery_deadline_at,"
                    "  required_finality, coverage_status, media_buy_ids, package_ids,"
                    "  schedule, definition, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    "         %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s)"
                    " ON CONFLICT DO NOTHING"
                    " RETURNING reporting_obligation_id",
                    (
                        obligation.reporting_obligation_id,
                        obligation.account_id,
                        obligation.delivery_config_id,
                        obligation.delivery_config_version,
                        obligation.report_definition_id,
                        obligation.reporting_profile,
                        obligation.feed_purpose,
                        obligation.period.period_key,
                        obligation.period.start,
                        obligation.period.end,
                        obligation.period.source_timezone,
                        obligation.period.expected_at,
                        obligation.scope_resolved_at,
                        obligation.automated_recovery_deadline_at,
                        obligation.required_finality,
                        obligation.coverage_status,
                        _json(sorted(obligation.media_buy_ids)),
                        _json(sorted(obligation.package_ids)),
                        _json(_schedule_payload(obligation.schedule)),
                        (
                            _json(_definition_payload(obligation.definition))
                            if obligation.definition
                            else None
                        ),
                        obligation.created_at,
                    ),
                )
            ).fetchone()
            if inserted is not None:
                await self._append_change(
                    connection,
                    obligation.account_id,
                    "obligation",
                    obligation.reporting_obligation_id,
                )
                return obligation
        # Another worker won the period close; converge on its obligation.
        existing = await self.find_obligation(
            account_id=obligation.account_id,
            delivery_config_id=obligation.delivery_config_id,
            delivery_config_version=obligation.delivery_config_version,
            period_start=obligation.period.start,
            period_end=obligation.period.end,
        )
        if existing is None:  # pragma: no cover - only under concurrent deletion
            raise LedgerConflictError(
                "OBLIGATION_LOST", "the obligation vanished between insert and read"
            )
        return existing

    async def get_obligation(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> ReportingObligationRecord | None:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    f"SELECT {_OBLIGATION_COLUMNS} FROM reporting_obligations"  # noqa: S608  # nosec B608
                    " WHERE account_id = %s AND reporting_obligation_id = %s",
                    (account_id, reporting_obligation_id),
                )
            ).fetchone()
        return _obligation_from_row(row) if row else None

    async def find_obligation(
        self,
        *,
        account_id: str,
        delivery_config_id: str,
        delivery_config_version: int,
        period_start: datetime,
        period_end: datetime,
    ) -> ReportingObligationRecord | None:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    f"SELECT {_OBLIGATION_COLUMNS} FROM reporting_obligations"  # noqa: S608  # nosec B608
                    " WHERE account_id = %s AND delivery_config_id = %s"
                    " AND delivery_config_version = %s AND period_start = %s AND period_end = %s",
                    (
                        account_id,
                        delivery_config_id,
                        delivery_config_version,
                        period_start,
                        period_end,
                    ),
                )
            ).fetchone()
        return _obligation_from_row(row) if row else None

    # -- revisions --------------------------------------------------------

    async def commit_revision(
        self, revision: ReportingRevisionRecord, rows: Sequence[dict[str, Any]]
    ) -> ReportingRevisionRecord:
        if revision.row_count != len(rows):
            raise LedgerConflictError(
                "ROW_COUNT_MISMATCH",
                f"revision declares {revision.row_count} rows but {len(rows)} were supplied",
            )
        digest = _fingerprint(_revision_payload(revision))
        async with self._pool.connection() as connection:
            existing = await (
                await connection.execute(
                    "SELECT content_sha256 FROM reporting_revisions"
                    " WHERE reporting_revision_id = %s AND account_id = %s",
                    (revision.reporting_revision_id, revision.account_id),
                )
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise LedgerConflictError(
                        "REVISION_IMMUTABLE",
                        f"revision {revision.reporting_revision_id} already exists with "
                        "different content; a restatement is a new revision",
                    )
                stored = await self.get_revision(
                    account_id=revision.account_id,
                    reporting_revision_id=revision.reporting_revision_id,
                )
                assert stored is not None
                return stored

            await self._lock_account(connection, revision.account_id)
            obligation = await (
                await connection.execute(
                    "SELECT 1 FROM reporting_obligations"
                    " WHERE reporting_obligation_id = %s AND account_id = %s",
                    (revision.reporting_obligation_id, revision.account_id),
                )
            ).fetchone()
            if obligation is None:
                raise LedgerConflictError(
                    "OBLIGATION_NOT_FOUND",
                    "a revision must attach to an obligation committed at the period close",
                )
            if revision.supersedes_reporting_revision_id:
                await self._require_current_leaf(connection, revision)
            try:
                await connection.execute(
                    "INSERT INTO reporting_revisions"
                    " (reporting_revision_id, account_id, reporting_obligation_id, finality,"
                    "  revision_content_sha256, row_count, control_totals, observed_at,"
                    "  data_through, created_at, supersedes_reporting_revision_id,"
                    "  finality_basis, finality_policy_id, finalized_at, readable,"
                    "  readable_at_commit, source_publication_id, source_manifest_sha256,"
                    "  content_sha256)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s,"
                    "         %s, %s, %s, %s, %s)",
                    (
                        revision.reporting_revision_id,
                        revision.account_id,
                        revision.reporting_obligation_id,
                        revision.finality,
                        revision.revision_content_sha256,
                        revision.row_count,
                        _json([[name, value] for name, value in revision.control_totals]),
                        revision.observed_at,
                        revision.data_through,
                        revision.created_at,
                        revision.supersedes_reporting_revision_id,
                        revision.finality_basis,
                        revision.finality_policy_id,
                        revision.finalized_at,
                        revision.readable,
                        revision.readable_at_commit,
                        revision.source_publication_id,
                        revision.source_manifest_sha256,
                        digest,
                    ),
                )
            except Exception as error:  # psycopg raises UniqueViolation subclasses
                raise _translate_integrity_error(error) from error
            if rows:
                await connection.cursor().executemany(
                    "INSERT INTO reporting_revision_rows"
                    " (reporting_revision_id, ordinal, row_payload)"
                    " VALUES (%s, %s, %s::jsonb)",
                    [
                        (revision.reporting_revision_id, ordinal, _json(row))
                        for ordinal, row in enumerate(rows)
                    ],
                )
            await self._append_change(
                connection, revision.account_id, "revision", revision.reporting_revision_id
            )
        return revision

    @staticmethod
    async def _require_current_leaf(connection: Any, revision: ReportingRevisionRecord) -> None:
        target = revision.supersedes_reporting_revision_id
        row = await (
            await connection.execute(
                "SELECT r.reporting_revision_id,"
                " EXISTS (SELECT 1 FROM reporting_revisions s"
                "         WHERE s.supersedes_reporting_revision_id = r.reporting_revision_id)"
                " FROM reporting_revisions r"
                " WHERE r.reporting_revision_id = %s AND r.account_id = %s"
                "   AND r.reporting_obligation_id = %s",
                (target, revision.account_id, revision.reporting_obligation_id),
            )
        ).fetchone()
        if row is None:
            raise LedgerConflictError(
                "SUPERSEDES_UNKNOWN", f"revision {target} is not part of this obligation's chain"
            )
        if row[1]:
            raise LedgerConflictError(
                "SUPERSEDES_STALE",
                f"revision {target} has already been superseded; a stale pointer would fork "
                "the chain and let a successful retry erase a recorded restatement",
            )

    async def list_revisions(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> tuple[ReportingRevisionRecord, ...]:
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    f"SELECT {_REVISION_COLUMNS} FROM reporting_revisions"  # noqa: S608  # nosec B608
                    " WHERE account_id = %s AND reporting_obligation_id = %s"
                    " ORDER BY created_at, reporting_revision_id",
                    (account_id, reporting_obligation_id),
                )
            ).fetchall()
        return tuple(_revision_from_row(row) for row in rows)

    async def get_revision(
        self, *, account_id: str, reporting_revision_id: str
    ) -> ReportingRevisionRecord | None:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    f"SELECT {_REVISION_COLUMNS} FROM reporting_revisions"  # noqa: S608  # nosec B608
                    " WHERE account_id = %s AND reporting_revision_id = %s",
                    (account_id, reporting_revision_id),
                )
            ).fetchone()
        return _revision_from_row(row) if row else None

    async def read_revision_rows(
        self,
        *,
        account_id: str,
        reporting_revision_id: str,
        cursor: str | None = None,
        limit: int = 500,
    ) -> ReportingRowPage:
        offset = int(decode_cursor(cursor).get("offset", 0)) if cursor else 0
        async with self._pool.connection() as connection:
            owned = await (
                await connection.execute(
                    "SELECT row_count FROM reporting_revisions"
                    " WHERE account_id = %s AND reporting_revision_id = %s",
                    (account_id, reporting_revision_id),
                )
            ).fetchone()
            if owned is None:
                raise LedgerConflictError("REVISION_NOT_FOUND", "no such revision for this account")
            rows = await (
                await connection.execute(
                    "SELECT row_payload FROM reporting_revision_rows"
                    " WHERE reporting_revision_id = %s"
                    " ORDER BY ordinal OFFSET %s LIMIT %s",
                    (reporting_revision_id, offset, limit),
                )
            ).fetchall()
        total = int(owned[0])
        has_more = offset + limit < total
        return ReportingRowPage(
            rows=tuple(row[0] for row in rows),
            total_count=total,
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
        async with self._pool.connection() as connection:
            updated = await (
                await connection.execute(
                    "UPDATE reporting_revisions SET readable = %s"
                    " WHERE account_id = %s AND reporting_revision_id = %s"
                    " RETURNING reporting_revision_id",
                    (readable, account_id, reporting_revision_id),
                )
            ).fetchone()
        if updated is None:
            raise LedgerConflictError("REVISION_NOT_FOUND", "no such revision for this account")

    # -- adjustments ------------------------------------------------------

    async def commit_adjustment(
        self, adjustment: ReportingAdjustmentRecord
    ) -> ReportingAdjustmentRecord:
        async with self._pool.connection() as connection:
            await self._lock_account(connection, adjustment.account_id)
            revision = await (
                await connection.execute(
                    "SELECT finality FROM reporting_revisions"
                    " WHERE reporting_revision_id = %s AND account_id = %s",
                    (adjustment.adjusts_reporting_revision_id, adjustment.account_id),
                )
            ).fetchone()
            if revision is None:
                raise LedgerConflictError(
                    "REVISION_NOT_FOUND", "an adjustment must name a committed revision"
                )
            if revision[0] != "official":
                raise LedgerConflictError(
                    "ADJUSTMENT_REQUIRES_OFFICIAL",
                    "adjustments correct an official revision; restate a snapshot with a "
                    "superseding snapshot revision instead",
                )
            inserted = await (
                await connection.execute(
                    "INSERT INTO reporting_adjustments"
                    " (reporting_adjustment_id, account_id, adjusts_reporting_revision_id,"
                    "  reason_code, reason_detail, accounting_period_start,"
                    "  accounting_period_end, control_total_deltas, correction_observed_at,"
                    "  created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)"
                    " ON CONFLICT (reporting_adjustment_id) DO NOTHING"
                    " RETURNING reporting_adjustment_id",
                    (
                        adjustment.reporting_adjustment_id,
                        adjustment.account_id,
                        adjustment.adjusts_reporting_revision_id,
                        adjustment.reason_code,
                        adjustment.reason_detail,
                        adjustment.accounting_period_start,
                        adjustment.accounting_period_end,
                        _json([[name, value] for name, value in adjustment.control_total_deltas]),
                        adjustment.correction_observed_at,
                        adjustment.created_at,
                    ),
                )
            ).fetchone()
            if inserted is not None:
                await self._append_change(
                    connection,
                    adjustment.account_id,
                    "adjustment",
                    adjustment.reporting_adjustment_id,
                )
        return adjustment

    async def list_adjustments(
        self, *, account_id: str, reporting_revision_ids: Sequence[str]
    ) -> tuple[ReportingAdjustmentRecord, ...]:
        if not reporting_revision_ids:
            return ()
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    f"SELECT {_ADJUSTMENT_COLUMNS} FROM reporting_adjustments"  # noqa: S608  # nosec B608
                    " WHERE account_id = %s AND adjusts_reporting_revision_id = ANY(%s)"
                    " ORDER BY created_at, reporting_adjustment_id",
                    (account_id, list(reporting_revision_ids)),
                )
            ).fetchall()
        return tuple(_adjustment_from_row(row) for row in rows)

    # -- consumer status (preview) ----------------------------------------

    async def record_consumer_status(
        self, status: ConsumerStatusRecord
    ) -> tuple[ConsumerStatusRecord, bool]:
        digest = _fingerprint(_consumer_status_payload(status))
        async with self._pool.connection() as connection:
            replay = await self._replay(connection, status, digest)
            if replay is not None:
                return replay, False

            await self._lock_account(connection, status.account_id)
            leaf = await (
                await connection.execute(
                    "SELECT reporting_status_id FROM reporting_consumer_statuses"
                    " WHERE account_id = %s AND consumer_id = %s AND delivery_config_id = %s"
                    "   AND delivery_config_version = %s AND report_definition_id = %s"
                    "   AND period_start = %s AND period_end = %s AND superseded = FALSE",
                    (
                        status.account_id,
                        status.consumer_id,
                        status.delivery_config_id,
                        status.delivery_config_version,
                        status.report_definition_id,
                        status.period_start,
                        status.period_end,
                    ),
                )
            ).fetchone()
            if status.supersedes_reporting_status_id:
                if leaf is None or leaf[0] != status.supersedes_reporting_status_id:
                    raise LedgerConflictError(
                        "STATUS_SUPERSEDES_STALE",
                        "supersedes_reporting_status_id must name this chain's current leaf; "
                        "a stale pointer would let a successful retry erase a recorded outage",
                    )
                await connection.execute(
                    "UPDATE reporting_consumer_statuses SET superseded = TRUE"
                    " WHERE reporting_status_id = %s",
                    (status.supersedes_reporting_status_id,),
                )
            elif leaf is not None:
                raise LedgerConflictError(
                    "STATUS_SUPERSEDES_REQUIRED",
                    "this chain already has a current statement; a new statement must "
                    "explicitly supersede it",
                )
            try:
                await connection.execute(
                    "INSERT INTO reporting_consumer_statuses"
                    " (reporting_status_id, account_id, consumer_id, delivery_config_id,"
                    "  delivery_config_version, report_definition_id, period_start, period_end,"
                    "  period_source_timezone, consumer_status, status_as_of, recorded_at,"
                    "  supersedes_reporting_status_id, reporting_obligation_id,"
                    "  reporting_revision_id, observed_revision_content_sha256, failure_code,"
                    "  mismatch_code, consumer_commit_ref, seller_ledger_snapshot_id,"
                    "  seller_ledger_as_of, superseded, content_sha256)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    "         %s, %s, %s, %s, %s, FALSE, %s)",
                    (
                        status.reporting_status_id,
                        status.account_id,
                        status.consumer_id,
                        status.delivery_config_id,
                        status.delivery_config_version,
                        status.report_definition_id,
                        status.period_start,
                        status.period_end,
                        status.period_source_timezone,
                        status.consumer_status,
                        status.status_as_of,
                        status.recorded_at,
                        status.supersedes_reporting_status_id,
                        status.reporting_obligation_id,
                        status.reporting_revision_id,
                        status.observed_revision_content_sha256,
                        status.failure_code,
                        status.mismatch_code,
                        status.consumer_commit_ref,
                        status.seller_ledger_snapshot_id,
                        status.seller_ledger_as_of,
                        digest,
                    ),
                )
            except Exception as error:
                raise _translate_integrity_error(error) from error
            await self._append_change(
                connection, status.account_id, "consumer_status", status.reporting_status_id
            )
        return status, True

    @staticmethod
    async def _get_consumer_status(
        connection: Any, status: ConsumerStatusRecord
    ) -> ConsumerStatusRecord | None:
        row = await (
            await connection.execute(
                # Bare columns: this query has no `s` alias to qualify against.
                f"SELECT {_STATUS_COLUMNS_BARE} FROM reporting_consumer_statuses"  # noqa: S608  # nosec B608
                " WHERE reporting_status_id = %s AND account_id = %s AND consumer_id = %s",
                (status.reporting_status_id, status.account_id, status.consumer_id),
            )
        ).fetchone()
        return _status_from_row(row) if row else None

    async def resolve_consumer_status_replay(
        self, status: ConsumerStatusRecord
    ) -> ConsumerStatusRecord | None:
        async with self._pool.connection() as connection:
            return await self._replay(
                connection, status, _fingerprint(_consumer_status_payload(status))
            )

    async def _replay(
        self, connection: Any, status: ConsumerStatusRecord, digest: str
    ) -> ConsumerStatusRecord | None:
        existing = await (
            await connection.execute(
                "SELECT content_sha256 FROM reporting_consumer_statuses"
                " WHERE reporting_status_id = %s AND account_id = %s AND consumer_id = %s",
                (status.reporting_status_id, status.account_id, status.consumer_id),
            )
        ).fetchone()
        if existing is None:
            return None
        if existing[0] != digest:
            raise LedgerConflictError(
                "STATUS_IDENTITY_CONFLICT",
                f"reporting_status_id {status.reporting_status_id} was already "
                "recorded with different content",
            )
        stored = await self._get_consumer_status(connection, status)
        assert stored is not None
        return stored

    async def list_consumer_statuses(
        self,
        *,
        account_id: str,
        consumer_id: str,
        reporting_obligation_ids: Sequence[str] | None = None,
    ) -> tuple[ConsumerStatusRecord, ...]:
        # Attaches by obligation id *or* by the exact logical period key, so a
        # chain filed before the obligation existed is not lost, forked, or
        # reset when the seller later repairs the missing obligation.
        clause = ""
        params: list[Any] = [account_id, consumer_id]
        if reporting_obligation_ids is not None:
            clause = (
                " AND (s.reporting_obligation_id = ANY(%s) OR EXISTS ("
                "   SELECT 1 FROM reporting_obligations o"
                "   WHERE o.reporting_obligation_id = ANY(%s)"
                "     AND o.account_id = s.account_id"
                "     AND o.delivery_config_id = s.delivery_config_id"
                "     AND o.delivery_config_version = s.delivery_config_version"
                "     AND o.report_definition_id = s.report_definition_id"
                "     AND o.period_start = s.period_start"
                "     AND o.period_end = s.period_end))"
            )
            params.extend([list(reporting_obligation_ids), list(reporting_obligation_ids)])
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    f"SELECT {_STATUS_COLUMNS} FROM reporting_consumer_statuses s"  # noqa: S608  # nosec B608
                    f" WHERE s.account_id = %s AND s.consumer_id = %s{clause}"
                    " ORDER BY s.recorded_at, s.reporting_status_id",
                    tuple(params),
                )
            ).fetchall()
        return tuple(_status_from_row(row) for row in rows)

    # -- issue lifecycle --------------------------------------------------

    async def ensure_issue_opened(
        self,
        *,
        issue_key: str,
        account_id: str,
        consumer_id: str | None,
        observed_at: datetime,
    ) -> ReportingIssueLifecycle:
        async with self._pool.connection() as connection:
            # Serialize per account so two concurrent readers of one condition
            # converge on one occurrence instead of both computing
            # generation = N + 1 and racing the partial unique index.
            await self._lock_account(connection, account_id)
            live = await self._live_issue(connection, issue_key, account_id)
            if live is not None:
                return live
            row = await (
                await connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) FROM reporting_issue_lifecycle"
                    " WHERE account_id = %s AND issue_key = %s",
                    (account_id, issue_key),
                )
            ).fetchone()
            generation = int(row[0] if row else 0) + 1
            issue_id = issue_id_for_occurrence(issue_key, generation)
            await connection.execute(
                "INSERT INTO reporting_issue_lifecycle"
                " (issue_key, account_id, generation, issue_id, consumer_id, opened_at,"
                "  issue_state)"
                " VALUES (%s, %s, %s, %s, %s, %s, 'open')",
                (issue_key, account_id, generation, issue_id, consumer_id, _utc(observed_at)),
            )
            return ReportingIssueLifecycle(
                issue_key=issue_key,
                issue_id=issue_id,
                account_id=account_id,
                consumer_id=consumer_id,
                opened_at=_utc(observed_at),
                issue_state="open",
                generation=generation,
            )

    async def set_issue_state(
        self,
        *,
        issue_key: str,
        account_id: str,
        state: Literal["acknowledged", "waived"],
        at: datetime,
        external_ref: str | None = None,
    ) -> ReportingIssueLifecycle:
        if state not in {"acknowledged", "waived"}:
            raise LedgerConflictError(
                "ISSUE_STATE_NOT_OPERATOR_SETTABLE",
                f"issue_state {state!r} is not settable by an operator. 'resolved' is "
                "reachable only when the condition actually clears -- the projection "
                "retires it -- because a seller must not retire a mismatch out of a "
                "degraded projection while the statement that caused it is still the "
                "consumer's current leaf",
            )
        async with self._pool.connection() as connection:
            await self._lock_account(connection, account_id)
            live = await self._live_issue(connection, issue_key, account_id)
            if live is None:
                raise LedgerConflictError(
                    "ISSUE_NOT_OPEN",
                    f"no open issue {issue_key!r} for this account; a retired issue cannot be "
                    "reopened, and a recurrence gets a new occurrence",
                )
            check_issue_state_transition(live.issue_state, state)
            await connection.execute(
                "UPDATE reporting_issue_lifecycle"
                " SET issue_state = %s,"
                "     external_ref = COALESCE(%s, external_ref),"
                "     retired_at = CASE WHEN %s = 'waived' THEN %s ELSE retired_at END"
                " WHERE account_id = %s AND issue_key = %s AND generation = %s",
                (state, external_ref, state, _utc(at), account_id, issue_key, live.generation),
            )
            refreshed = await self._issue_row(connection, issue_key, account_id, live.generation)
            assert refreshed is not None
            return refreshed

    async def retire_issue(
        self, *, issue_key: str, account_id: str, at: datetime
    ) -> ReportingIssueLifecycle | None:
        async with self._pool.connection() as connection:
            await self._lock_account(connection, account_id)
            live = await self._live_issue(connection, issue_key, account_id)
            if live is None or not issue_is_retirable(live.issue_state):
                # Convergent, and a waived issue is left alone: it is already
                # retired from the projection by agreement, and overwriting
                # that readable act with `resolved` is an edge the forward-only
                # lifecycle forbids.
                return None
            check_issue_state_transition(live.issue_state, "resolved")
            await connection.execute(
                "UPDATE reporting_issue_lifecycle"
                " SET issue_state = 'resolved', retired_at = %s"
                " WHERE account_id = %s AND issue_key = %s AND generation = %s",
                (_utc(at), account_id, issue_key, live.generation),
            )
            return await self._issue_row(connection, issue_key, account_id, live.generation)

    async def get_issue(self, *, issue_key: str, account_id: str) -> ReportingIssueLifecycle | None:
        async with self._pool.connection() as connection:
            return await self._live_issue(connection, issue_key, account_id)

    @staticmethod
    async def _live_issue(
        connection: Any, issue_key: str, account_id: str
    ) -> ReportingIssueLifecycle | None:
        row = await (
            await connection.execute(
                f"SELECT {_ISSUE_COLUMNS} FROM reporting_issue_lifecycle"  # noqa: S608  # nosec B608
                " WHERE account_id = %s AND issue_key = %s"
                "   AND issue_state IN ('open', 'acknowledged', 'waived')",
                (account_id, issue_key),
            )
        ).fetchone()
        return _issue_from_row(row) if row else None

    @staticmethod
    async def _issue_row(
        connection: Any, issue_key: str, account_id: str, generation: int
    ) -> ReportingIssueLifecycle | None:
        row = await (
            await connection.execute(
                f"SELECT {_ISSUE_COLUMNS} FROM reporting_issue_lifecycle"  # noqa: S608  # nosec B608
                " WHERE account_id = %s AND issue_key = %s AND generation = %s",
                (account_id, issue_key, generation),
            )
        ).fetchone()
        return _issue_from_row(row) if row else None

    # -- snapshots --------------------------------------------------------

    async def open_snapshot(self, *, account_id: str, filters_fingerprint: str) -> LedgerSnapshot:
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT COALESCE(MAX(seq), 0), now() FROM reporting_ledger_changes"
                    " WHERE account_id = %s",
                    (account_id,),
                )
            ).fetchone()
        assert row is not None
        max_sequence = int(row[0])
        as_of = _utc(self._clock()) if self._clock is not None else _utc(row[1])
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
        async with self._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT seq, record_kind, record_id FROM reporting_ledger_changes"
                    " WHERE account_id = %s AND seq > %s AND seq <= %s"
                    " ORDER BY seq",
                    (snapshot.account_id, lower, snapshot.max_sequence),
                )
            ).fetchall()

            selected: list[tuple[int, LedgerRecordKind, Any]] = []
            for _seq, kind, record_id in rows:
                record = await self._resolve(connection, snapshot.account_id, kind, record_id)
                if record is None:
                    continue
                if not await self._in_scope(
                    connection, kind, record, delivery_config_ids, media_buy_ids, consumer_id
                ):
                    continue
                selected.append((_seq, kind, record))

        window = selected[offset : offset + limit]
        has_more = offset + limit < len(selected)
        return LedgerPage(
            obligations=tuple(item[2] for item in window if item[1] == "obligation"),
            revisions=tuple(item[2] for item in window if item[1] == "revision"),
            adjustments=tuple(item[2] for item in window if item[1] == "adjustment"),
            consumer_statuses=tuple(item[2] for item in window if item[1] == "consumer_status"),
            total_count=len(selected),
            has_more=has_more,
            cursor=(
                encode_cursor({"snapshot": snapshot.snapshot_id, "offset": offset + limit})
                if has_more
                else None
            ),
        )

    async def _resolve(
        self, connection: Any, account_id: str, kind: str, record_id: str
    ) -> Any | None:
        table, columns, key, builder = _RESOLVERS[kind]
        row = await (
            await connection.execute(
                f"SELECT {columns} FROM {table} WHERE account_id = %s AND {key} = %s",  # noqa: S608  # nosec B608
                (account_id, record_id),
            )
        ).fetchone()
        return builder(row) if row else None

    async def _in_scope(
        self,
        connection: Any,
        kind: str,
        record: Any,
        delivery_config_ids: Sequence[str] | None,
        media_buy_ids: Sequence[str] | None,
        consumer_id: str | None,
    ) -> bool:
        if kind == "consumer_status":
            # A caller sees only its own statements; another consumer's
            # operational status is never disclosed.
            return consumer_id is not None and record.consumer_id == consumer_id
        obligation = await self._obligation_for(connection, kind, record)
        if obligation is None:
            return False
        if delivery_config_ids and obligation.delivery_config_id not in set(delivery_config_ids):
            return False
        if media_buy_ids and not set(media_buy_ids).intersection(obligation.media_buy_ids):
            return False
        return True

    async def _obligation_for(
        self, connection: Any, kind: str, record: Any
    ) -> ReportingObligationRecord | None:
        if kind == "obligation":
            found: ReportingObligationRecord = record
            return found
        if kind == "revision":
            return await self.get_obligation(
                account_id=record.account_id,
                reporting_obligation_id=record.reporting_obligation_id,
            )
        revision = await self.get_revision(
            account_id=record.account_id,
            reporting_revision_id=record.adjusts_reporting_revision_id,
        )
        if revision is None:
            return None
        return await self.get_obligation(
            account_id=revision.account_id,
            reporting_obligation_id=revision.reporting_obligation_id,
        )

    # -- leasing ----------------------------------------------------------

    async def lease_period_close(
        self, *, worker_id: str, now: datetime, lease_seconds: float
    ) -> LeasedConfiguration | None:
        expires = _utc(now) + timedelta(seconds=lease_seconds)
        async with self._pool.connection() as connection:
            row = await (
                await connection.execute(
                    "UPDATE reporting_configurations SET lease_worker_id = %s,"
                    " lease_expires_at = %s"
                    " WHERE (delivery_config_id, delivery_config_version) = ("
                    "   SELECT delivery_config_id, delivery_config_version"
                    "   FROM reporting_configurations"
                    "   WHERE lease_expires_at IS NULL OR lease_expires_at <= %s"
                    "   ORDER BY lease_expires_at NULLS FIRST"
                    "   FOR UPDATE SKIP LOCKED"
                    "   LIMIT 1)"
                    " RETURNING account_id, delivery_config_id, delivery_config_version",
                    (worker_id, expires, _utc(now)),
                )
            ).fetchone()
        if row is None:
            return None
        return LeasedConfiguration(
            account_id=row[0],
            delivery_config_id=row[1],
            delivery_config_version=row[2],
            lease_expires_at=expires,
        )

    async def release_period_close(self, lease: LeasedConfiguration, *, worker_id: str) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                "UPDATE reporting_configurations SET lease_worker_id = NULL,"
                " lease_expires_at = NULL"
                " WHERE delivery_config_id = %s AND delivery_config_version = %s"
                "   AND lease_worker_id = %s",
                (lease.delivery_config_id, lease.delivery_config_version, worker_id),
            )


# --------------------------------------------------------------------------
# Row mapping
# --------------------------------------------------------------------------


def _translate_integrity_error(error: Exception) -> LedgerConflictError:
    """Map a constraint name onto the invariant it protects.

    Matching the index name rather than the driver's prose: the message text
    varies by Postgres version and locale, the index name does not.
    """
    text = str(error)
    if "reporting_revisions_one_official" in text:
        return LedgerConflictError(
            "OFFICIAL_REVISION_TERMINAL",
            "an official revision already exists for this obligation; publish a later source "
            "correction as an adjustment",
        )
    if "reporting_revisions_one_successor" in text:
        return LedgerConflictError(
            "SUPERSEDES_STALE",
            "that revision has already been superseded; a stale pointer would fork the chain",
        )
    if "reporting_consumer_statuses_one_leaf" in text:
        return LedgerConflictError(
            "STATUS_SUPERSEDES_STALE",
            "this chain already has a current statement; supersede it explicitly",
        )
    if "reporting_consumer_statuses_one_successor" in text:
        return LedgerConflictError(
            "STATUS_SUPERSEDES_STALE",
            "that statement has already been superseded",
        )
    if "reporting_obligations_period_key" in text:
        return LedgerConflictError(
            "OBLIGATION_EXISTS", "an obligation already exists for this logical period"
        )
    return LedgerConflictError("LEDGER_WRITE_FAILED", "the ledger write violated an integrity rule")


_OBLIGATION_COLUMNS = (
    "reporting_obligation_id, account_id, delivery_config_id, delivery_config_version,"
    " report_definition_id, reporting_profile, feed_purpose, period_key, period_start,"
    " period_end, source_timezone, expected_at, scope_resolved_at,"
    " automated_recovery_deadline_at, required_finality, coverage_status, media_buy_ids,"
    " package_ids, schedule, definition, created_at"
)

_REVISION_COLUMNS = (
    "reporting_revision_id, account_id, reporting_obligation_id, finality,"
    " revision_content_sha256, row_count, control_totals, observed_at, data_through,"
    " created_at, supersedes_reporting_revision_id, finality_basis, finality_policy_id,"
    " finalized_at, readable, readable_at_commit, source_publication_id, source_manifest_sha256"
)

_ADJUSTMENT_COLUMNS = (
    "reporting_adjustment_id, account_id, adjusts_reporting_revision_id, reason_code,"
    " reason_detail, accounting_period_start, accounting_period_end, control_total_deltas,"
    " correction_observed_at, created_at"
)

_STATUS_COLUMNS = (
    "s.reporting_status_id, s.account_id, s.consumer_id, s.delivery_config_id,"
    " s.delivery_config_version, s.report_definition_id, s.period_start, s.period_end,"
    " s.period_source_timezone, s.consumer_status, s.status_as_of, s.recorded_at,"
    " s.supersedes_reporting_status_id, s.reporting_obligation_id, s.reporting_revision_id,"
    " s.observed_revision_content_sha256, s.failure_code, s.mismatch_code,"
    " s.consumer_commit_ref, s.seller_ledger_snapshot_id, s.seller_ledger_as_of, s.superseded"
)

_STATUS_COLUMNS_BARE = _STATUS_COLUMNS.replace("s.", "")

_ISSUE_COLUMNS = (
    "issue_key, account_id, generation, issue_id, consumer_id, opened_at, issue_state,"
    " external_ref, retired_at"
)


def _issue_from_row(row: Sequence[Any]) -> ReportingIssueLifecycle:
    return ReportingIssueLifecycle(
        issue_key=row[0],
        account_id=row[1],
        generation=row[2],
        issue_id=row[3],
        consumer_id=row[4],
        opened_at=_utc(row[5]),
        issue_state=row[6],
        external_ref=row[7],
        retired_at=_utc(row[8]) if row[8] else None,
    )


def _schedule_payload(schedule: ReportingScheduleSpec) -> dict[str, Any]:
    return {
        "period_duration": schedule.period_duration,
        "delivery_sla": schedule.delivery_sla,
        "alignment": schedule.alignment,
        "period_timezone": schedule.period_timezone,
        "period_anchor": (
            _utc(schedule.period_anchor).isoformat() if schedule.period_anchor else None
        ),
    }


def _definition_payload(
    definition: ReportingDefinitionBinding | None,
) -> dict[str, Any] | None:
    return definition.to_wire() if definition is not None else None


def _definition_from_payload(payload: dict[str, Any] | None) -> ReportingDefinitionBinding | None:
    return ReportingDefinitionBinding(**payload) if payload else None


def _schedule_from_payload(payload: dict[str, Any]) -> ReportingScheduleSpec:
    anchor = payload.get("period_anchor")
    return ReportingScheduleSpec(
        period_duration=payload["period_duration"],
        delivery_sla=payload["delivery_sla"],
        alignment=payload.get("alignment", "utc"),
        period_timezone=payload.get("period_timezone"),
        period_anchor=datetime.fromisoformat(anchor) if anchor else None,
    )


def _configuration_payload(configuration: ReportingConfiguration) -> dict[str, Any]:
    return {
        "account_id": configuration.account_id,
        "report_definition_id": configuration.report_definition_id,
        "reporting_profile": configuration.reporting_profile,
        "feed_purpose": configuration.feed_purpose,
        "required_finality": configuration.required_finality,
        "account_timezone": configuration.account_timezone,
        "media_buy_ids": sorted(configuration.media_buy_ids),
        "schedule": _schedule_payload(configuration.schedule),
        "definition": _definition_payload(configuration.definition),
    }


def _configuration_from_row(row: Sequence[Any]) -> ReportingConfiguration:
    return ReportingConfiguration(
        delivery_config_id=row[0],
        delivery_config_version=row[1],
        account_id=row[2],
        report_definition_id=row[3],
        reporting_profile=row[4],
        feed_purpose=row[5],
        required_finality=row[6],
        account_timezone=row[7],
        schedule=_schedule_from_payload(row[8]),
        media_buy_ids=tuple(row[9] or ()),
        activated_at=_utc(row[10]) if row[10] else None,
        deactivated_at=_utc(row[11]) if row[11] else None,
        automated_recovery_window=timedelta(seconds=float(row[12])),
        status_retention_days=row[13],
        definition=_definition_from_payload(row[14]),
        authoritative_party=row[15],
    )


def _obligation_from_row(row: Sequence[Any]) -> ReportingObligationRecord:
    return ReportingObligationRecord(
        reporting_obligation_id=row[0],
        account_id=row[1],
        delivery_config_id=row[2],
        delivery_config_version=row[3],
        report_definition_id=row[4],
        reporting_profile=row[5],
        feed_purpose=row[6],
        period=ReportingPeriodBoundary(
            period_key=row[7],
            start=_utc(row[8]),
            end=_utc(row[9]),
            source_timezone=row[10],
            expected_at=_utc(row[11]),
        ),
        scope_resolved_at=_utc(row[12]),
        automated_recovery_deadline_at=_utc(row[13]),
        required_finality=row[14],
        coverage_status=row[15],
        media_buy_ids=tuple(row[16] or ()),
        package_ids=tuple(row[17] or ()),
        schedule=_schedule_from_payload(row[18]),
        definition=_definition_from_payload(row[19]),
        created_at=_utc(row[20]),
    )


def _revision_from_row(row: Sequence[Any]) -> ReportingRevisionRecord:
    return ReportingRevisionRecord(
        reporting_revision_id=row[0],
        account_id=row[1],
        reporting_obligation_id=row[2],
        finality=row[3],
        revision_content_sha256=row[4],
        row_count=int(row[5]),
        control_totals=tuple((name, value) for name, value in (row[6] or [])),
        observed_at=_utc(row[7]),
        data_through=_utc(row[8]) if row[8] else None,
        created_at=_utc(row[9]),
        supersedes_reporting_revision_id=row[10],
        finality_basis=row[11],
        finality_policy_id=row[12],
        finalized_at=_utc(row[13]) if row[13] else None,
        readable=row[14],
        readable_at_commit=row[15],
        source_publication_id=row[16],
        source_manifest_sha256=row[17],
    )


def _adjustment_from_row(row: Sequence[Any]) -> ReportingAdjustmentRecord:
    return ReportingAdjustmentRecord(
        reporting_adjustment_id=row[0],
        account_id=row[1],
        adjusts_reporting_revision_id=row[2],
        reason_code=row[3],
        reason_detail=row[4],
        accounting_period_start=_utc(row[5]),
        accounting_period_end=_utc(row[6]),
        control_total_deltas=tuple((name, value) for name, value in (row[7] or [])),
        correction_observed_at=_utc(row[8]),
        created_at=_utc(row[9]),
    )


def _status_from_row(row: Sequence[Any]) -> ConsumerStatusRecord:
    return ConsumerStatusRecord(
        reporting_status_id=row[0],
        account_id=row[1],
        consumer_id=row[2],
        delivery_config_id=row[3],
        delivery_config_version=row[4],
        report_definition_id=row[5],
        period_start=_utc(row[6]),
        period_end=_utc(row[7]),
        period_source_timezone=row[8],
        consumer_status=row[9],
        status_as_of=_utc(row[10]),
        recorded_at=_utc(row[11]),
        supersedes_reporting_status_id=row[12],
        reporting_obligation_id=row[13],
        reporting_revision_id=row[14],
        observed_revision_content_sha256=row[15],
        failure_code=row[16],
        mismatch_code=row[17],
        consumer_commit_ref=row[18],
        seller_ledger_snapshot_id=row[19],
        seller_ledger_as_of=_utc(row[20]) if row[20] else None,
        superseded=row[21],
    )


def _consumer_status_payload(status: ConsumerStatusRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chain": list(status.chain_key),
        "consumer_status": status.consumer_status,
        "status_as_of": _utc(status.status_as_of).isoformat(),
        "supersedes": status.supersedes_reporting_status_id,
        "obligation": status.reporting_obligation_id,
        "revision": status.reporting_revision_id,
        "digest": status.observed_revision_content_sha256,
        "failure_code": status.failure_code,
    }
    # Conditional on purpose. ``canonical_json_utf8_v1`` encodes ``None`` as
    # ``null``, so including the key unconditionally would change the digest of
    # every statement that has no mismatch_code -- i.e. every row an rc.2 SDK
    # wrote. After an in-place upgrade a buyer's exact retry would then fail
    # with STATUS_IDENTITY_CONFLICT instead of replaying as ``unchanged``,
    # which is the one thing an idempotent append-only surface must never do.
    # Omitting the key when absent keeps rc.2 digests byte-identical while a
    # code-only change is still a conflict: "the metric is missing" and "the
    # currency is wrong" are different claims and must not share one immutable
    # identity.
    if status.mismatch_code is not None:
        payload["mismatch_code"] = status.mismatch_code
    return payload


def _revision_payload(revision: ReportingRevisionRecord) -> dict[str, Any]:
    return {
        "finality": revision.finality,
        "revision_content_sha256": revision.revision_content_sha256,
        "row_count": revision.row_count,
        "control_totals": [list(item) for item in revision.control_totals],
        "obligation": revision.reporting_obligation_id,
        "supersedes": revision.supersedes_reporting_revision_id,
    }


_RESOLVERS: dict[str, tuple[str, str, str, Any]] = {
    "obligation": (
        "reporting_obligations",
        _OBLIGATION_COLUMNS,
        "reporting_obligation_id",
        _obligation_from_row,
    ),
    "revision": (
        "reporting_revisions",
        _REVISION_COLUMNS,
        "reporting_revision_id",
        _revision_from_row,
    ),
    "adjustment": (
        "reporting_adjustments",
        _ADJUSTMENT_COLUMNS,
        "reporting_adjustment_id",
        _adjustment_from_row,
    ),
    "consumer_status": (
        "reporting_consumer_statuses",
        _STATUS_COLUMNS_BARE,
        "reporting_status_id",
        _status_from_row,
    ),
}
