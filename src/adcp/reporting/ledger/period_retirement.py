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
foreign key, and their guards are frozen catalog objects. A reconciliation record *holds* its
period, so retirement never picks it up; a period whose protected records are provisional
observations or notification events finishes phases 1 and 2, then stays ``retiring`` with
cause ``RECORDS_PROTECTED``. Either way its row bytes are freed (the production worker also
schedules ``prune_expired_row_sets``), reads answer as expired, and the records stay. Whether
to relax those guards is an audit-model decision for the maintainers, not made here. Nothing
is half-deleted: phase 3 either removes every record of the period or none.

**Fairness.** A period that cannot finish records ``attempts``, ``next_attempt_at`` and
``last_failure`` and backs off (1 minute, doubling, capped at 6 hours), so it does not occupy a
candidate slot on every pass. Ready retries take at most half of a pass, rounded up, and fresh
periods fill the rest. The same names and numbers as the JavaScript SDK.
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
    #: Periods that failed, with a secret-free cause, and resume once their backoff elapses.
    failed: tuple[str, ...]
    causes: tuple[tuple[str, str], ...]
    #: Expired periods this pass left alone because a hold applies (materializer work, a
    #: reconciliation record, or an unread registered feed consumer). Capped at 10,000.
    held: int = 0
    #: ``retiring`` periods backing off after a failure (protected records or a failing
    #: provider); they are not attempted this pass and occupy no candidate slot.
    deferred: int = 0
    #: Periods still ``retiring`` after five or more failed attempts, worst first, at most 20:
    #: ``(reporting_obligation_id, attempts, cause)``. They need operator attention, for
    #: example a removed binding or a permanent provider error.
    stuck: tuple[tuple[str, int, str], ...] = ()


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
        parameters = {
            "account": account_id,
            "only": None,
            "record_days": ledger_record_retention_days,
            "hold_days": max_feed_hold_days,
            "limit": limit,
        }
        resumable = await (await connection.execute(_resumable_query(), parameters)).fetchall()
        fresh = await (
            await connection.execute(_fresh_query("".join(holds), only=False), parameters)
        ).fetchall()
        # Ready retries (interrupted, failed or protected periods whose backoff has elapsed) are
        # guaranteed at most half the pass, rounded up, ahead of fresh periods; a retry beyond
        # that only fills the slots fresh periods leave empty. The same split as the JavaScript
        # SDK, so a few periods that cannot finish never take every slot.
        first = (limit + 1) // 2
        candidates = [*resumable[:first], *fresh, *resumable[first:]][:limit]
        held = await _held_count(connection, "".join(holds), parameters)
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
            await _back_off(
                store,
                obligation_id,
                error.code if isinstance(error, ReportingRowStoreError) else type(error).__name__,
            )
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
    # What is left after this pass, so a host can see work no pass is reaching.
    async with store._pool.connection() as connection:
        deferred = await _deferred_count(connection, account_id)
        stuck = await _stuck(connection, account_id)
    return ReportingPeriodRetirementV1(
        retired,
        tuple(protected),
        tuple(failed),
        tuple(causes),
        held=held,
        deferred=deferred,
        stuck=stuck,
    )


def _resumable_query() -> str:
    """``retiring`` periods whose backoff has elapsed: fewest failed attempts, then oldest."""
    return (
        "SELECT t.reporting_obligation_id, t.account_id"
        " FROM adcp_reporting_period_tombstones t WHERE t.state = 'retiring'"
        " AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= clock_timestamp())"
        " AND (%(account)s::text IS NULL OR t.account_id = %(account)s)"
        " ORDER BY t.attempts, t.period_end, t.reporting_obligation_id LIMIT %(limit)s"
    )


def _eligible_query(holds: str, *, only: bool, select: str, tail: str) -> str:
    return "".join(
        (
            "SELECT ",
            select,
            " FROM reporting_obligations o JOIN reporting_configurations c",
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
            tail,
        )
    )


def _fresh_query(holds: str, *, only: bool) -> str:
    """Expired periods nothing holds and no tombstone names.

    ``only=True`` is the recheck under the account lock: it asks whether this one
    obligation still qualifies, so a late revision cannot be retired with its period.
    """
    return _eligible_query(
        holds,
        only=only,
        select="o.reporting_obligation_id, o.account_id",
        tail=" ORDER BY o.period_end, o.reporting_obligation_id LIMIT %(limit)s",
    )


async def _held_count(connection: Any, holds: str, parameters: dict[str, Any]) -> int:
    """Expired periods the holds kept out of this pass (a bounded count, never the rows)."""
    if not holds:
        return 0
    everything = _eligible_query("", only=False, select="1", tail=" LIMIT 10001")
    unheld = _eligible_query(holds, only=False, select="1", tail=" LIMIT 10001")
    row = await (
        await connection.execute(
            f"SELECT (SELECT count(*) FROM ({everything}) a)"  # noqa: S608  # nosec B608
            f" - (SELECT count(*) FROM ({unheld}) b)",
            parameters,
        )
    ).fetchone()
    return max(0, int(row[0])) if row else 0


async def _deferred_count(connection: Any, account_id: str | None) -> int:
    row = await (
        await connection.execute(
            "SELECT count(*) FROM adcp_reporting_period_tombstones WHERE state = 'retiring'"
            " AND next_attempt_at > clock_timestamp()"
            " AND (%(account)s::text IS NULL OR account_id = %(account)s)",
            {"account": account_id},
        )
    ).fetchone()
    return int(row[0]) if row else 0


#: Failed attempts after which a period is reported as stuck (the JavaScript SDK's threshold).
PERSISTENT_FAILURE_ATTEMPTS = 5


async def _stuck(connection: Any, account_id: str | None) -> tuple[tuple[str, int, str], ...]:
    rows = await (
        await connection.execute(
            "SELECT reporting_obligation_id, attempts, last_failure"
            " FROM adcp_reporting_period_tombstones"
            " WHERE state = 'retiring' AND attempts >= %(threshold)s"
            " AND (%(account)s::text IS NULL OR account_id = %(account)s)"
            " ORDER BY attempts DESC, retiring_at, reporting_obligation_id LIMIT 20",
            {"account": account_id, "threshold": PERSISTENT_FAILURE_ATTEMPTS},
        )
    ).fetchall()
    return tuple((row[0], int(row[1]), row[2] or "Error") for row in rows)


_BACKOFF = (
    "attempts = attempts + 1, last_failure = %s,"
    " next_attempt_at = clock_timestamp()"
    " + LEAST(21600, 60 * power(2, LEAST(attempts, 12))) * INTERVAL '1 second'"
)


async def _back_off(store: PgReportingLedgerStore, obligation_id: str, outcome: str) -> None:
    """Park a ``retiring`` period that could not finish; best effort, never raises."""
    try:
        async with store._pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_period_tombstones SET "  # noqa: S608  # nosec B608
                + _BACKOFF
                + " WHERE reporting_obligation_id = %s AND state = 'retiring'",
                (outcome, obligation_id),
            )
    except Exception:  # noqa: S110  # nosec B110
        pass


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
                    _fresh_query(holds, only=True),
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
            await connection.execute(
                "UPDATE adcp_reporting_period_tombstones SET "  # noqa: S608  # nosec B608
                + _BACKOFF
                + " WHERE reporting_obligation_id = %s AND state = 'retiring'",
                ("RECORDS_PROTECTED", obligation_id),
            )
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
