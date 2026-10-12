"""Period retirement: retire a whole period once its retention window has elapsed.

Shared SDK persistence spec, adcontextprotocol/adcp#7996, sections 4.2 and 4.3, and the
period-aligned anchor of adcontextprotocol/adcp#8100. A period is retired as a unit by one
ordered, resumable sequence, so the records that locate row bytes are never deleted while
the bytes remain stored:

1. **Tombstone.** Under the account lock, write a ``retiring`` tombstone (final revision,
   its digests, revision and adjustment counts) and mark the period's row sets
   ``pruning``. Exact reads now answer as expired.
2. **External bytes.** Delete the period's recorded object versions. A failure leaves the
   period ``retiring`` with every locator intact; the next pass resumes ``retiring``
   periods before new candidates.
3. **Records.** Under the account lock, delete the manifests and bodies, then the period's
   ledger records, mark the tombstone ``retired`` and append a ``retirement`` change.

**Eligibility.** A period qualifies once the later of its period end, its newest
revision's ``created_at`` and its newest adjustment's ``created_at`` is older than
``max(status_retention_days, ledger_record_retention_days)``. It is held, and simply not a
candidate this pass, while any materializer work or reconciliation record (a live
materialization or a receipt) references it, or while a live registered feed consumer
(updated within ``max_feed_hold_days``) has not passed its change rows in the consumer's
own feed order. A consumer silent past ``max_feed_hold_days`` stops holding, so retention
is never blocked forever.

**What Python can delete today.** Several Python extension tables (provisional
observations, reconciliation records, notification events) are append-only by trigger and
foreign key, and their guards are frozen catalog objects. A period that still has such
records finishes phases 1 and 2, then stays ``retiring`` with cause ``RECORDS_PROTECTED``:
its row bytes are gone and reads answer as expired, while its records wait for the shared
Core layout (persistence Stage 3), which carries retirement exemptions. Nothing is
half-deleted: phase 3 either removes every record of the period or none.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger.row_retention import NoLongerEligibleError
from adcp.reporting.ledger.row_storage import ReportingRowStoreError

if TYPE_CHECKING:
    from adcp.reporting.ledger.pg import PgReportingLedgerStore

__all__ = ["ReportingPeriodRetirementV1", "retire_expired_periods"]


@dataclass(frozen=True, slots=True)
class ReportingPeriodRetirementV1:
    #: Periods whose records are now deleted and whose tombstone is ``retired``.
    retired: int
    #: Periods whose row bytes are gone but whose records remain, cause ``RECORDS_PROTECTED``.
    protected: tuple[str, ...]
    #: Periods that failed, with a secret-free cause, and resume on the next pass.
    failed: tuple[str, ...]
    causes: tuple[tuple[str, str], ...]


async def _table_exists(connection: Any, name: str) -> bool:
    row = await (
        await connection.execute(
            "SELECT to_regclass(quote_ident(current_schema()) || '.' || %s)", (name,)
        )
    ).fetchone()
    return row is not None and row[0] is not None


async def retire_expired_periods(
    store: PgReportingLedgerStore,
    *,
    limit: int = 20,
    account_id: str | None = None,
    ledger_record_retention_days: int | None = None,
    max_feed_hold_days: int = 7,
) -> ReportingPeriodRetirementV1:
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    if type(max_feed_hold_days) is not int or not 1 <= max_feed_hold_days <= 36_500:
        raise ValueError("max_feed_hold_days must be between 1 and 36500")
    if ledger_record_retention_days is not None and (
        type(ledger_record_retention_days) is not int or ledger_record_retention_days < 1
    ):
        raise ValueError("ledger_record_retention_days must be a positive integer")
    async with store._pool.connection() as connection:
        if not await _table_exists(connection, "adcp_reporting_period_tombstones"):
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE",
                "period retirement requires row_storage or change_feed on the store",
            )
        holds: list[str] = []
        if await _table_exists(connection, "reporting_materializer_work"):
            holds.append(
                " AND NOT EXISTS (SELECT 1 FROM reporting_materializer_work w"
                " WHERE w.account_id = o.account_id"
                " AND w.reporting_obligation_id = o.reporting_obligation_id)"
            )
        if await _table_exists(connection, "reporting_reconciliation_records"):
            holds.append(
                " AND NOT EXISTS (SELECT 1 FROM reporting_reconciliation_records x"
                " WHERE x.account_id = o.account_id"
                " AND x.reporting_obligation_id = o.reporting_obligation_id)"
            )
        if await _table_exists(connection, "adcp_reporting_changes") and await _table_exists(
            connection, "adcp_reporting_feed_consumers"
        ):
            holds.append(
                " AND NOT EXISTS (SELECT 1 FROM adcp_reporting_changes change"
                " JOIN adcp_reporting_feed_consumers consumer"
                "   ON consumer.updated_at > clock_timestamp()"
                "      - (%(hold_days)s::integer * INTERVAL '1 day')"
                " WHERE change.obligation_id = o.reporting_obligation_id"
                " AND ((consumer.account_id IS NULL AND (change.xid, change.seq) >"
                "        (consumer.cursor_xid, consumer.cursor_seq))"
                "   OR (consumer.account_id = change.account_id"
                "       AND change.seq > consumer.cursor_seq)))"
            )
        query = _candidate_query("".join(holds), only=False)
        candidates = await (
            await connection.execute(
                query,
                {
                    "account": account_id,
                    "only": None,
                    "record_days": ledger_record_retention_days,
                    "hold_days": max_feed_hold_days,
                    "limit": limit,
                },
            )
        ).fetchall()
        has_rows = await _table_exists(connection, "adcp_reporting_row_sets")
    retired = 0
    protected: list[str] = []
    failed: list[str] = []
    causes: list[tuple[str, str]] = []
    options = (ledger_record_retention_days, max_feed_hold_days, "".join(holds))
    for obligation_id, owner in candidates:
        try:
            outcome = await _retire_period(store, obligation_id, owner, has_rows, options)
        except NoLongerEligibleError:
            continue
        except Exception as error:
            failed.append(obligation_id)
            causes.append(
                (
                    obligation_id,
                    (
                        error.code
                        if isinstance(error, ReportingRowStoreError)
                        else type(error).__name__
                    ),
                )
            )
            continue
        if outcome == "retired":
            retired += 1
        elif outcome == "protected":
            protected.append(obligation_id)
    return ReportingPeriodRetirementV1(retired, tuple(protected), tuple(failed), tuple(causes))


def _candidate_query(holds: str, *, only: bool) -> str:
    """Resumable ``retiring`` periods first, then expired periods that nothing holds.

    ``only=True`` is the recheck under the account lock: it asks whether this one
    obligation still qualifies, so a late revision cannot be retired with its period.
    """
    resumable = (
        ""
        if only
        else "".join(
            (
                " SELECT t.reporting_obligation_id, t.account_id, 0 AS resume, t.period_end",
                "  FROM adcp_reporting_period_tombstones t WHERE t.state = 'retiring'",
                "   AND (%(account)s::text IS NULL OR t.account_id = %(account)s)",
                " UNION ALL",
            )
        )
    )
    return "".join(
        (
            "SELECT reporting_obligation_id, account_id FROM (",
            resumable,
            " SELECT o.reporting_obligation_id, o.account_id, 1 AS resume, o.period_end",
            "  FROM reporting_obligations o JOIN reporting_configurations c",
            "    ON c.account_id = o.account_id AND c.consumer_id = o.consumer_id",
            "   AND c.delivery_config_id = o.delivery_config_id",
            "   AND c.delivery_config_version = o.delivery_config_version",
            "  WHERE (%(account)s::text IS NULL OR o.account_id = %(account)s)",
            "   AND o.reporting_obligation_id = %(only)s" if only else "",
            "   AND NOT EXISTS (SELECT 1 FROM adcp_reporting_period_tombstones t",
            "        WHERE t.reporting_obligation_id = o.reporting_obligation_id)",
            "   AND GREATEST(o.period_end,",
            "        COALESCE((SELECT MAX(r.created_at) FROM reporting_revisions r",
            "          WHERE r.account_id = o.account_id",
            "            AND r.reporting_obligation_id = o.reporting_obligation_id),",
            "          o.period_end),",
            "        COALESCE((SELECT MAX(a.created_at) FROM reporting_adjustments a",
            "          JOIN reporting_revisions ar ON ar.reporting_revision_id =",
            "               a.adjusts_reporting_revision_id",
            "          WHERE ar.account_id = o.account_id",
            "            AND ar.reporting_obligation_id = o.reporting_obligation_id),",
            "          o.period_end))",
            "      + (GREATEST(c.status_retention_days, COALESCE(%(record_days)s::integer,",
            "           c.status_retention_days)) * INTERVAL '1 day') <= clock_timestamp()",
            holds,
            ") candidate ORDER BY resume, period_end, reporting_obligation_id LIMIT %(limit)s",
        )
    )


def _tombstone_insert(has_rows: bool) -> str:
    """The phase 1 tombstone insert. ``adcp_reporting_row_sets`` exists only with row storage,
    so a change-feed-only host records no row manifest instead of failing on the missing table."""
    manifest = (
        "(SELECT s.row_manifest_sha256 FROM adcp_reporting_row_sets s"
        " WHERE s.row_set_id = latest.reporting_revision_id)"
        if has_rows
        else "NULL::text"
    )
    return "".join(
        (
            "INSERT INTO adcp_reporting_period_tombstones",
            " (reporting_obligation_id, account_id, consumer_id, delivery_config_id,",
            "  delivery_config_version, period_start, period_end, state, final_revision_id,",
            "  final_content_sha256, final_row_manifest_sha256, revision_count,",
            "  adjustment_count)",
            " SELECT o.reporting_obligation_id, o.account_id, o.consumer_id,",
            "        o.delivery_config_id, o.delivery_config_version, o.period_start,",
            "        o.period_end, 'retiring', latest.reporting_revision_id,",
            "        latest.revision_content_sha256, ",
            manifest,
            ", (SELECT count(*) FROM reporting_revisions r",
            "    WHERE r.account_id = o.account_id",
            "      AND r.reporting_obligation_id = o.reporting_obligation_id),",
            "  (SELECT count(*) FROM reporting_adjustments a",
            "    JOIN reporting_revisions ar",
            "      ON ar.reporting_revision_id = a.adjusts_reporting_revision_id",
            "   WHERE ar.account_id = o.account_id",
            "     AND ar.reporting_obligation_id = o.reporting_obligation_id)",
            " FROM reporting_obligations o LEFT JOIN LATERAL (",
            "   SELECT r.reporting_revision_id, r.revision_content_sha256",
            "     FROM reporting_revisions r",
            "    WHERE r.account_id = o.account_id",
            "      AND r.reporting_obligation_id = o.reporting_obligation_id",
            "    ORDER BY (r.finality = 'official') DESC, r.created_at DESC,",
            "             r.reporting_revision_id DESC LIMIT 1) latest ON TRUE",
            " WHERE o.reporting_obligation_id = %s AND o.account_id = %s",
            " ON CONFLICT (reporting_obligation_id) DO NOTHING",
        )
    )


async def _retire_period(
    store: PgReportingLedgerStore,
    obligation_id: str,
    account_id: str,
    has_rows: bool,
    options: tuple[int | None, int, str],
) -> str:
    from psycopg import errors

    record_days, hold_days, holds = options
    row_set_ids: list[str] = []
    # Phase 1: tombstone first, so expired rows read as expired from here on. A period
    # that is not yet tombstoned is re-checked under the account lock: a late revision or
    # adjustment may have committed since the candidate list was read.
    async with store._pool.connection() as connection, connection.transaction():
        await store._lock_account(connection, account_id)
        resuming = await (
            await connection.execute(
                "SELECT 1 FROM adcp_reporting_period_tombstones"
                " WHERE reporting_obligation_id = %s AND state = 'retiring'",
                (obligation_id,),
            )
        ).fetchone()
        if resuming is None:
            still = await (
                await connection.execute(
                    _candidate_query(holds, only=True),
                    {
                        "account": account_id,
                        "only": obligation_id,
                        "record_days": record_days,
                        "hold_days": hold_days,
                        "limit": 1,
                    },
                )
            ).fetchone()
            if still is None:
                raise NoLongerEligibleError
        await connection.execute(_tombstone_insert(has_rows), (obligation_id, account_id))
        if has_rows:
            from adcp.reporting.ledger.row_retention import mark_period_pruning

            row_set_ids = await mark_period_pruning(connection, obligation_id, account_id)
    # Phase 2: external bytes, at their recorded versions.
    if has_rows and store._row_storage is not None:
        from adcp.reporting.ledger.row_retention import delete_period_objects

        await delete_period_objects(store, obligation_id, row_set_ids)
    # Phase 3: the records, together.
    async with store._pool.connection() as connection, connection.transaction():
        await store._lock_account(connection, account_id)
        if has_rows:
            from adcp.reporting.ledger.row_retention import _finish_period

            await _finish_period(connection, obligation_id, account_id, row_set_ids)
        try:
            async with connection.transaction():  # a savepoint: all of the records or none
                await _delete_records(connection, obligation_id, account_id)
        except (errors.RaiseException, errors.ForeignKeyViolation, errors.CheckViolation):
            return "protected"
        await connection.execute(
            "UPDATE adcp_reporting_period_tombstones SET state = 'retired',"
            " retired_at = clock_timestamp()"
            " WHERE reporting_obligation_id = %s AND state = 'retiring'",
            (obligation_id,),
        )
        if store._change_feed:
            await connection.execute(
                "INSERT INTO adcp_reporting_changes"
                " (account_id, record_kind, record_id, obligation_id)"
                " VALUES (%s, 'retirement', %s, %s)",
                (account_id, obligation_id, obligation_id),
            )
    return "retired"


async def _delete_records(connection: Any, obligation_id: str, account_id: str) -> None:
    revisions = (
        "SELECT reporting_revision_id FROM reporting_revisions"
        " WHERE account_id = %(a)s AND reporting_obligation_id = %(o)s"
    )
    params = {"a": account_id, "o": obligation_id}
    if await _table_exists(connection, "adcp_reporting_row_sets"):
        await connection.execute(
            "DELETE FROM adcp_reporting_row_sets WHERE account_id = %(a)s"
            " AND obligation_id = %(o)s AND rows_state IN ('pruned', 'pruning')",
            params,
        )
    await connection.execute(
        "DELETE FROM reporting_consumer_statuses WHERE account_id = %(a)s"
        f" AND (reporting_obligation_id = %(o)s OR reporting_revision_id IN ({revisions}))",  # noqa: S608  # nosec B608
        params,
    )
    await connection.execute(
        "DELETE FROM reporting_adjustments WHERE account_id = %(a)s"
        f" AND adjusts_reporting_revision_id IN ({revisions})",  # noqa: S608  # nosec B608
        params,
    )
    if await _table_exists(connection, "reporting_revision_rows"):
        await connection.execute(
            f"DELETE FROM reporting_revision_rows WHERE reporting_revision_id IN ({revisions})",  # noqa: S608  # nosec B608
            params,
        )
    if await _table_exists(connection, "adcp_reporting_legacy_revision_rows_v0"):
        await connection.execute(
            "DELETE FROM adcp_reporting_legacy_revision_rows_v0"
            f" WHERE reporting_revision_id IN ({revisions})",  # noqa: S608  # nosec B608
            params,
        )
    await connection.execute(
        "DELETE FROM reporting_restatement_checkpoints WHERE account_id = %(a)s"
        " AND reporting_obligation_id = %(o)s",
        params,
    )
    for table in ("reporting_provisional_observations", "reporting_provisional_acquisitions"):
        if await _table_exists(connection, table):
            await connection.execute(
                f"DELETE FROM {table} WHERE account_id = %(a)s"  # noqa: S608  # nosec B608
                " AND reporting_obligation_id = %(o)s",
                params,
            )
    await connection.execute(
        "DELETE FROM reporting_revisions WHERE account_id = %(a)s"
        " AND reporting_obligation_id = %(o)s",
        params,
    )
    await connection.execute(
        "DELETE FROM reporting_obligations WHERE account_id = %(a)s"
        " AND reporting_obligation_id = %(o)s",
        params,
    )
