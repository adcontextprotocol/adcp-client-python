"""Verified migration of legacy inline rows to chunked row storage.

``reporting_revision_rows`` held one JSONB value per row, which does not preserve
the canonical bytes a revision's digest was computed over. Migration moves each
revision's rows into verified canonical chunks (adcontextprotocol/adcp#7996,
section 7, Stage 2) and never rewrites evidence it cannot prove:

* a revision's inline rows are re-canonicalized and must reproduce its committed
  ``revision_content_sha256`` before any chunk is written. Any mismatch,
  non-portable value or broken ordinal sequence **quarantines** the revision as
  ``ROWS_INTEGRITY_FAILED``. Its inline rows stay exactly as found, and reads refuse
  to serve it;
* each revision is migrated in its own transaction under the account lock, so a
  batch never holds the lock longer than one revision;
* inline rows are not deleted. Once every revision is migrated or quarantined,
  :func:`retire_legacy_inline_rows` renames the table (never truncates it), so a
  binary that predates chunked rows fails closed instead of writing rows nobody
  reads.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from adcp.reporting.ledger.row_storage import REPORTING_ROW_STORAGE_TABLES, ReportingRowStoreError
from adcp.reporting.ledger.store import LedgerConflictError

if TYPE_CHECKING:
    from adcp.reporting.ledger.pg import PgReportingLedgerStore

__all__ = [
    "LEGACY_ROW_ARCHIVE_TABLE",
    "ReportingRowMigrationBatchV1",
    "ReportingRowQuarantineReason",
    "migrate_inline_rows",
    "retire_legacy_inline_rows",
]

LEGACY_ROW_ARCHIVE_TABLE = "adcp_reporting_legacy_revision_rows_v0"

ReportingRowQuarantineReason = Literal[
    "BINDING_MISMATCH", "ROWS_NOT_PORTABLE", "ROW_SEQUENCE_INVALID"
]


@dataclass(frozen=True, slots=True)
class ReportingRowMigrationBatchV1:
    """One bounded migration pass."""

    #: Revisions examined in this batch, including quarantined ones.
    examined: int
    migrated: int
    #: Revisions whose inline rows did not reproduce their binding. Never rewritten.
    quarantined: tuple[str, ...]
    #: Pass back to resume after the last examined revision; ``None`` once none remain.
    cursor: str | None


def _encode_cursor(revision_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"id": revision_id}).encode()).decode()


def _decode_cursor(cursor: str) -> str:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        revision_id = value["id"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("migrate_inline_rows cursor is invalid") from None
    if not isinstance(revision_id, str):
        raise ValueError("migrate_inline_rows cursor is invalid")
    return revision_id


async def migrate_inline_rows(
    store: PgReportingLedgerStore,
    *,
    limit: int = 100,
    account_id: str | None = None,
    cursor: str | None = None,
    binding_id: str | None = None,
) -> ReportingRowMigrationBatchV1:
    """Move up to ``limit`` revisions' inline rows into verified chunks.

    Resumable and idempotent. Revisions that already have a row set or a
    quarantine record are skipped. ``binding_id`` selects a ``postgres`` binding
    for the migrated rows.
    """
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("migrate_inline_rows limit must be between 1 and 10000")
    after = _decode_cursor(cursor) if cursor is not None else None
    if store._row_storage is None:
        raise ReportingRowStoreError("INVALID_INPUT", "migrate_inline_rows requires row storage")
    await store.ready_row_storage()
    async with store._pool.connection() as connection:
        candidates = await (
            await connection.execute(
                "SELECT r.reporting_revision_id, r.account_id FROM reporting_revisions r"
                " WHERE (%(account)s::text IS NULL OR r.account_id = %(account)s)"
                " AND (%(after)s::text IS NULL OR r.reporting_revision_id > %(after)s)"
                " AND NOT EXISTS (SELECT 1 FROM adcp_reporting_row_sets s"
                "                  WHERE s.row_set_id = r.reporting_revision_id)"
                " AND NOT EXISTS (SELECT 1 FROM adcp_reporting_row_quarantine q"
                "                  WHERE q.reporting_revision_id = r.reporting_revision_id)"
                " ORDER BY r.reporting_revision_id LIMIT %(limit)s",
                {"account": account_id, "after": after, "limit": limit + 1},
            )
        ).fetchall()
    migrated = 0
    quarantined: list[str] = []
    for revision_id, owner in candidates[:limit]:
        outcome = await _migrate_one(store, revision_id, owner, binding_id)
        if outcome == "migrated":
            migrated += 1
        elif outcome != "skipped":
            quarantined.append(revision_id)
    return ReportingRowMigrationBatchV1(
        examined=min(len(candidates), limit),
        migrated=migrated,
        quarantined=tuple(quarantined),
        cursor=_encode_cursor(candidates[limit - 1][0]) if len(candidates) > limit else None,
    )


async def _migrate_one(
    store: PgReportingLedgerStore, revision_id: str, account_id: str, binding_id: str | None
) -> Literal["migrated", "skipped"] | ReportingRowQuarantineReason:
    from adcp.reporting.ledger import pg as pg_module

    async with store._pool.connection() as connection, connection.transaction():
        await store._lock_account(connection, account_id)
        taken = await (
            await connection.execute(
                "SELECT EXISTS (SELECT 1 FROM adcp_reporting_row_sets WHERE row_set_id = %s)"
                " OR EXISTS (SELECT 1 FROM adcp_reporting_row_quarantine"
                "             WHERE reporting_revision_id = %s)",
                (revision_id, revision_id),
            )
        ).fetchone()
        if taken is None or taken[0]:
            return "skipped"
        row = await (
            await connection.execute(
                f"SELECT {pg_module._REVISION_COLUMNS} FROM reporting_revisions"  # noqa: S608  # nosec B608
                " WHERE reporting_revision_id = %s AND account_id = %s",
                (revision_id, account_id),
            )
        ).fetchone()
        if row is None:
            return "skipped"
        revision = pg_module._revision_from_row(row)
        reason = await _prove_and_write(store, connection, revision, binding_id)
        if reason is None:
            return "migrated"
        await connection.execute(
            "INSERT INTO adcp_reporting_row_quarantine (reporting_revision_id, account_id, reason)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (revision_id, account_id, reason),
        )
        return reason


async def _prove_and_write(
    store: PgReportingLedgerStore, connection: Any, revision: Any, binding_id: str | None
) -> ReportingRowQuarantineReason | None:
    """Write the revision's chunks, or name why its inline rows cannot be proven."""
    legacy_table = await (
        await connection.execute(
            "SELECT to_regclass(quote_ident(current_schema()) || '.reporting_revision_rows')"
        )
    ).fetchone()
    stored: list[Any] = []
    if legacy_table is not None and legacy_table[0] is not None:
        stored = list(
            await (
                await connection.execute(
                    "SELECT ordinal, row_payload FROM reporting_revision_rows"
                    " WHERE reporting_revision_id = %s ORDER BY ordinal",
                    (revision.reporting_revision_id,),
                )
            ).fetchall()
        )
    ordinals = [item[0] for item in stored]
    if ordinals != list(range(len(stored))) or len(stored) != revision.row_count:
        return "ROW_SEQUENCE_INVALID"
    rows = [item[1] for item in stored]
    if any(not isinstance(item, dict) for item in rows):
        return "ROWS_NOT_PORTABLE"
    try:
        async with connection.transaction():
            await store._write_revision_row_set(connection, revision, rows, binding_id=binding_id)
    except LedgerConflictError as error:
        if error.code == "REVISION_CONTENT_MISMATCH":
            return "BINDING_MISMATCH"
        if error.code == "ROWS_NOT_PORTABLE":
            return "ROWS_NOT_PORTABLE"
        raise
    return None


async def retire_legacy_inline_rows(store: PgReportingLedgerStore) -> bool:
    """Rename ``reporting_revision_rows`` once nothing needs it. Returns whether it renamed.

    Refuses with ``ROWS_MIGRATION_INCOMPLETE`` while any revision has neither
    chunks nor a quarantine record. The table is renamed to
    ``adcp_reporting_legacy_revision_rows_v0``, never truncated or dropped:
    inline rows (including those of quarantined revisions) stay available for
    forensics, and drop the archive yourself once you no longer need it.
    Idempotent: a retired table returns ``False``.

    Stop every writer that predates chunked rows first. Their inserts fail once
    the table is renamed, which is the point, but a revision they committed
    between the last migration pass and the rename would block retirement.

    The locks retirement takes are bounded twice: ``lock_timeout`` (15s) for acquiring each
    and ``statement_timeout`` (30s) for every statement run under them, so a slow count or
    rename aborts the transaction and releases them instead of stalling readers and
    commits. The unmigrated count is capped at 1000. A timeout surfaces as the driver's
    ``LockNotAvailable`` / ``QueryCanceled``; retry in a quieter window.
    """
    async with store._pool.connection() as connection, connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('adcp.reporting.schema'),"
            " hashtext(current_schema()))"
        )
        # Bound both the wait for each lock and how long the locks are held: the count and
        # the rename run under ACCESS EXCLUSIVE, which blocks inline reads and commits.
        await connection.execute("SET LOCAL lock_timeout = '15s'")
        await connection.execute("SET LOCAL statement_timeout = '30s'")
        legacy = await (
            await connection.execute(
                "SELECT to_regclass(quote_ident(current_schema()) || '.reporting_revision_rows'),"
                " to_regclass(quote_ident(current_schema()) || '.' || %s)",
                (LEGACY_ROW_ARCHIVE_TABLE,),
            )
        ).fetchone()
        if legacy is None or legacy[0] is None:
            return False
        missing = await (
            await connection.execute(
                "SELECT count(*) FROM information_schema.tables"
                " WHERE table_schema = current_schema() AND table_name = ANY(%s::text[])",
                (list(REPORTING_ROW_STORAGE_TABLES),),
            )
        ).fetchone()
        if missing is None or missing[0] != len(REPORTING_ROW_STORAGE_TABLES):
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE", "row storage must be installed before retiring inline rows"
            )
        await connection.execute("LOCK TABLE reporting_revisions IN SHARE ROW EXCLUSIVE MODE")
        await connection.execute("LOCK TABLE reporting_revision_rows IN ACCESS EXCLUSIVE MODE")
        remaining = await (
            await connection.execute(
                "SELECT count(*) FROM (SELECT 1 FROM reporting_revisions r"
                " WHERE NOT EXISTS (SELECT 1 FROM adcp_reporting_row_sets s"
                "                    WHERE s.row_set_id = r.reporting_revision_id)"
                " AND NOT EXISTS (SELECT 1 FROM adcp_reporting_row_quarantine q"
                "                  WHERE q.reporting_revision_id = r.reporting_revision_id)"
                " LIMIT 1000) unmigrated"
            )
        ).fetchone()
        if remaining is None or remaining[0]:
            raise LedgerConflictError(
                "ROWS_MIGRATION_INCOMPLETE",
                f"{remaining[0] if remaining else 'some'} revisions have neither verified chunks"
                " nor a quarantine record; run migrate_inline_rows until it returns no cursor",
            )
        await connection.execute(
            f"ALTER TABLE reporting_revision_rows RENAME TO {LEGACY_ROW_ARCHIVE_TABLE}"  # noqa: S608  # nosec B608
        )
        return True
