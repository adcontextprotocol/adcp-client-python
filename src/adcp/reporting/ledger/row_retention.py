"""Row retention: intent sweeps and expired-period row pruning.

Shared SDK persistence spec, adcontextprotocol/adcp#7996, sections 4.2 and 4.3.

**Intent sweeps** resolve abandoned object uploads. An expired ``open`` write intent
whose ledger commit never happened moves to ``sweeping`` under the account lock, so no
writer can commit against it. Then only the objects that upload *created* are deleted,
at their recorded native versions. Adopted objects are never recorded as created, and
any object a committed chunk references is skipped. Interrupted sweeps resume.

**Row pruning** retires a whole period's row bytes in an ordered, resumable sequence
(row bytes first, never the records that locate them):

1. under the account lock, mark the period's live row sets ``pruning``. Exact reads
   now answer as expired, like an unknown revision;
2. delete the period's recorded object versions outside any transaction. A failure
   leaves the period ``pruning`` with every locator intact, and the next pass resumes;
3. under the account lock, delete the period's chunk manifests and PostgreSQL bodies
   and mark its row sets ``pruned``.

A period qualifies when every record in it is older than
``max(status_retention_days, ledger_record_retention_days)``, anchored at the later of
the period end and its newest revision or adjustment, and no materializer work is
pending for it. Python retains the ledger headers and their records until the shared
Core layout (persistence Stage 3), so a pruned row set stays as a small marker that
makes reads answer ``REVISION_NOT_FOUND``; the records stay outside the protocol
projection because the status horizon already excludes periods past retention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger.row_storage import ReportingRowStoreError

if TYPE_CHECKING:
    from adcp.reporting.ledger.pg import PgReportingLedgerStore

__all__ = [
    "ReportingRowIntentSweepV1",
    "ReportingRowPruneV1",
    "prune_expired_row_sets",
    "sweep_row_write_intents",
]


@dataclass(frozen=True, slots=True)
class ReportingRowIntentSweepV1:
    swept: int
    objects_deleted: int
    #: Intents whose sweep failed this pass. Each backs off before its next attempt.
    failed: int
    #: Stable, secret-free cause per failed intent: ``(row_set_id, code)``.
    causes: tuple[tuple[str, str], ...] = ()
    #: Expired intents with objects to delete through a binding this host does not configure.
    #: They are left untouched for a host that configures it; they are not this host's failures.
    skipped_unconfigured: int = 0
    #: Expired intents backing off after a failed sweep, not attempted this pass.
    deferred: int = 0
    #: Intents expired for longer than ``overdue_after_seconds`` (default 24 hours) and still not
    #: swept, including ones backing off. Their objects are leaking: check that a host configures
    #: their binding and that provider deletes succeed.
    overdue: int = 0


@dataclass(frozen=True, slots=True)
class ReportingRowPruneV1:
    #: Obligations whose row bytes were fully retired in this pass.
    retired: int
    #: Obligations left ``pruning`` with locators intact, to resume on the next pass.
    failed: tuple[str, ...]
    #: Stable, secret-free cause per failed obligation.
    causes: tuple[tuple[str, str], ...]


def _positive(value: int, name: str) -> None:
    if type(value) is not int or value < 1 or value > 10_000:
        raise ValueError(f"{name} must be between 1 and 10000")


async def sweep_row_write_intents(
    store: PgReportingLedgerStore,
    *,
    limit: int = 100,
    account_id: str | None = None,
    overdue_after_seconds: int = 24 * 3600,
) -> ReportingRowIntentSweepV1:
    """Resolve abandoned object uploads. Safe to run on a schedule from any worker."""
    _positive(limit, "limit")
    if type(overdue_after_seconds) is not int or overdue_after_seconds < 1:
        raise ValueError("overdue_after_seconds must be a positive integer")
    storage = store._row_storage
    if storage is None:
        raise ReportingRowStoreError(
            "INVALID_INPUT", "sweep_row_write_intents requires row storage"
        )
    await store.ready_row_storage()
    parameters = {
        "account": account_id,
        "bindings": list(storage._bindings),
        "limit": limit,
        "overdue": overdue_after_seconds,
    }
    due = (
        "(%(account)s::text IS NULL OR account_id = %(account)s)"
        " AND (state = 'sweeping' OR expires_at <= clock_timestamp())"
    )
    async with store._pool.connection() as connection:
        # An intent with no recorded objects needs no binding to sweep; one with objects is
        # skipped unless this host configures its binding. Intents that failed before back off,
        # and never-failed ones come first, so a failing intent cannot hold the head of every
        # pass. (The same selection as the JavaScript SDK.)
        expired = await (
            await connection.execute(
                "SELECT row_binding_id, account_id, row_set_id, content_sha256"
                " FROM adcp_reporting_row_write_intents"
                f" WHERE {due}"  # noqa: S608  # nosec B608
                " AND (next_sweep_at IS NULL OR next_sweep_at <= clock_timestamp())"
                " AND (row_binding_id = ANY(%(bindings)s::text[])"
                "      OR created_objects = '[]'::jsonb)"
                " ORDER BY sweep_attempts, expires_at LIMIT %(limit)s",
                parameters,
            )
        ).fetchall()
    causes: list[tuple[str, str]] = []
    swept = deleted = failed = 0
    for binding_id, owner, row_set_id, content_sha256 in expired:
        key = (binding_id, row_set_id, content_sha256)
        try:
            async with store._pool.connection() as connection, connection.transaction():
                await store._lock_account(connection, owner)
                claimed = await (
                    await connection.execute(
                        "UPDATE adcp_reporting_row_write_intents SET state = 'sweeping'"
                        " WHERE row_binding_id = %s AND row_set_id = %s AND content_sha256 = %s"
                        " AND (state = 'sweeping' OR expires_at <= clock_timestamp())"
                        " RETURNING created_objects",
                        key,
                    )
                ).fetchone()
            if claimed is None:
                continue
            # Referenced objects are kept; the check and the delete are one atomic step
            # against a concurrent commit adopting the object.
            orphaned = [(str(item["objectKey"]), str(item["nativeVersion"])) for item in claimed[0]]
            removed = await storage.delete_unreferenced(store._pool, binding_id, orphaned)
            async with store._pool.connection() as connection:
                await connection.execute(
                    "DELETE FROM adcp_reporting_row_write_intents"
                    " WHERE row_binding_id = %s AND row_set_id = %s AND content_sha256 = %s"
                    " AND state = 'sweeping'",
                    key,
                )
            swept += 1
            deleted += removed
        except Exception as error:
            failed += 1
            # Cancellation is shutdown, not a fault: it is a BaseException, never recorded here.
            await _back_off_intent(store, key)
            causes.append(
                (
                    row_set_id,
                    (
                        error.code
                        if isinstance(error, ReportingRowStoreError)
                        else type(error).__name__
                    ),
                )
            )
    async with store._pool.connection() as connection:
        # What is still waiting after this pass, so a host can see work no pass is reaching.
        backlog = await (
            await connection.execute(
                "SELECT"
                " count(*) FILTER (WHERE row_binding_id <> ALL(%(bindings)s::text[])"
                "                  AND created_objects <> '[]'::jsonb) AS skipped,"
                " count(*) FILTER (WHERE (row_binding_id = ANY(%(bindings)s::text[])"
                "                         OR created_objects = '[]'::jsonb)"
                "                  AND next_sweep_at > clock_timestamp()) AS deferred,"
                " count(*) FILTER (WHERE expires_at <= clock_timestamp()"
                "                  - (%(overdue)s::integer * INTERVAL '1 second')) AS overdue"
                " FROM adcp_reporting_row_write_intents"
                f" WHERE {due}",  # noqa: S608  # nosec B608
                parameters,
            )
        ).fetchone()
    return ReportingRowIntentSweepV1(
        swept,
        deleted,
        failed,
        tuple(causes),
        skipped_unconfigured=int(backlog[0]) if backlog else 0,
        deferred=int(backlog[1]) if backlog else 0,
        overdue=int(backlog[2]) if backlog else 0,
    )


async def _back_off_intent(store: PgReportingLedgerStore, key: tuple[str, str, str]) -> None:
    """Record a failed sweep of one intent: 1 minute, doubling, capped at 6 hours.

    Best effort, never raises: a failure to record only means a retry on the next pass.
    """
    try:
        async with store._pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_row_write_intents"
                " SET sweep_attempts = sweep_attempts + 1,"
                "     next_sweep_at = clock_timestamp()"
                "       + LEAST(21600, 60 * power(2, LEAST(sweep_attempts, 12)))"
                "         * INTERVAL '1 second'"
                " WHERE row_binding_id = %s AND row_set_id = %s AND content_sha256 = %s",
                key,
            )
    except Exception:  # noqa: S110  # nosec B110
        pass


def prune_candidate_query(materializer_hold: str, *, only: bool) -> str:
    """Obligations whose row bytes are past their window.

    Shared by candidate selection and the per-period recheck under the account lock
    (``only=True``), so a period that stopped qualifying between the two is skipped.
    The anchor is the latest of the period end, the newest revision, the newest
    adjustment and the newest row set recorded, the last on the database clock.
    """
    return "".join(
        (
            "SELECT o.reporting_obligation_id, o.account_id FROM reporting_obligations o"
            " JOIN reporting_configurations c ON c.account_id = o.account_id"
            "  AND c.consumer_id = o.consumer_id"
            "  AND c.delivery_config_id = o.delivery_config_id"
            "  AND c.delivery_config_version = o.delivery_config_version"
            " WHERE (%(account)s::text IS NULL OR o.account_id = %(account)s)",
            " AND o.reporting_obligation_id = %(only)s" if only else "",
            " AND EXISTS (SELECT 1 FROM adcp_reporting_row_sets s"
            "              WHERE s.account_id = o.account_id"
            "                AND s.obligation_id = o.reporting_obligation_id"
            "                AND s.rows_state IN ('live', 'unavailable', 'pruning'))"
            " AND GREATEST(o.period_end, COALESCE((SELECT MAX(r.created_at)"
            "      FROM reporting_revisions r WHERE r.account_id = o.account_id"
            "       AND r.reporting_obligation_id = o.reporting_obligation_id), o.period_end),"
            "      COALESCE((SELECT MAX(a.created_at) FROM reporting_adjustments a"
            "       JOIN reporting_revisions ar ON ar.reporting_revision_id ="
            "            a.adjusts_reporting_revision_id"
            "       WHERE ar.reporting_obligation_id = o.reporting_obligation_id"
            "         AND ar.account_id = o.account_id), o.period_end),"
            "      COALESCE((SELECT MAX(rs.recorded_at) FROM adcp_reporting_row_sets rs"
            "       WHERE rs.account_id = o.account_id"
            "         AND rs.obligation_id = o.reporting_obligation_id), o.period_end))"
            "    + (GREATEST(c.status_retention_days,"
            "        COALESCE(%(record_days)s::integer, c.status_retention_days))"
            "       * INTERVAL '1 day') <= clock_timestamp()",
            materializer_hold,
            " ORDER BY EXISTS (SELECT 1 FROM adcp_reporting_row_sets p"
            "      WHERE p.obligation_id = o.reporting_obligation_id"
            "        AND p.rows_state = 'pruning') DESC, o.period_end,"
            "      o.reporting_obligation_id LIMIT %(limit)s",
        )
    )


async def prune_expired_row_sets(
    store: PgReportingLedgerStore,
    *,
    limit: int = 20,
    account_id: str | None = None,
    ledger_record_retention_days: int | None = None,
) -> ReportingRowPruneV1:
    """Retire the row bytes of periods past their retention window.

    ``ledger_record_retention_days`` may only lengthen retention: a window shorter
    than a generation's advertised ``status_retention_days`` is non-conformant, so
    each period uses the longer of the two.
    """
    _positive(limit, "limit")
    if ledger_record_retention_days is not None and (
        type(ledger_record_retention_days) is not int or ledger_record_retention_days < 1
    ):
        raise ValueError("ledger_record_retention_days must be a positive integer")
    storage = store._row_storage
    if storage is None:
        raise ReportingRowStoreError("INVALID_INPUT", "prune_expired_row_sets requires row storage")
    await store.ready_row_storage()
    async with store._pool.connection() as connection:
        hold = await (
            await connection.execute(
                "SELECT to_regclass(quote_ident(current_schema())"
                " || '.reporting_materializer_work') IS NOT NULL"
            )
        ).fetchone()
        materializer_hold = (
            " AND NOT EXISTS (SELECT 1 FROM reporting_materializer_work w"
            "   WHERE w.account_id = o.account_id"
            "     AND w.reporting_obligation_id = o.reporting_obligation_id"
            "     AND w.state = 'pending')"
            if hold is not None and hold[0]
            else ""
        )
        candidates = await (
            await connection.execute(
                prune_candidate_query(materializer_hold, only=False),
                {
                    "account": account_id,
                    "only": None,
                    "record_days": ledger_record_retention_days,
                    "limit": limit,
                },
            )
        ).fetchall()
    retired = 0
    failed: list[str] = []
    causes: list[tuple[str, str]] = []
    for obligation_id, owner in candidates:
        try:
            await _retire_period(
                store, obligation_id, owner, ledger_record_retention_days, materializer_hold
            )
            retired += 1
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
    return ReportingRowPruneV1(retired, tuple(failed), tuple(causes))


class NoLongerEligibleError(Exception):
    """The period stopped qualifying between candidate selection and the account lock."""


async def mark_period_pruning(connection: Any, obligation_id: str, account_id: str) -> list[str]:
    """Stop serving a period's live row sets and return every set now ``pruning``.

    Call under the account lock. The returned IDs are the only sets later phases touch, so
    a revision committed after this point can never lose its rows to this pass.
    """
    await connection.execute(
        "UPDATE adcp_reporting_row_sets SET rows_state = 'pruning',"
        " rows_state_changed_at = clock_timestamp()"
        " WHERE account_id = %s AND obligation_id = %s AND rows_state IN ('live', 'unavailable')",
        (account_id, obligation_id),
    )
    rows = await (
        await connection.execute(
            "SELECT row_set_id FROM adcp_reporting_row_sets"
            " WHERE account_id = %s AND obligation_id = %s AND rows_state = 'pruning'",
            (account_id, obligation_id),
        )
    ).fetchall()
    return [row[0] for row in rows]


async def delete_period_objects(
    store: PgReportingLedgerStore, obligation_id: str, row_set_ids: list[str]
) -> None:
    """Delete the recorded object versions of ``row_set_ids``, at those versions.

    An object that a live chunk of another obligation still references is kept: the default
    key template cannot collide across obligations, but a custom template can, and a
    delete must never take bytes another revision still needs. See
    ``ReportingRowStorageV1.delete_unreferenced`` for the locking contract.
    """
    storage = store._row_storage
    assert storage is not None
    async with store._pool.connection() as connection:
        objects = await (
            await connection.execute(
                "SELECT DISTINCT s.row_binding_id, c.object_key, c.native_version"
                " FROM adcp_reporting_row_chunks c JOIN adcp_reporting_row_sets s"
                "   ON s.row_set_id = c.row_set_id"
                " WHERE c.row_set_id = ANY(%(ids)s::text[]) AND c.object_key IS NOT NULL",
                {"ids": row_set_ids},
            )
        ).fetchall()
    by_binding: dict[str, list[tuple[str, str]]] = {}
    for binding_id, object_key, native_version in objects:
        by_binding.setdefault(binding_id, []).append((object_key, native_version))
    for binding_id, pairs in by_binding.items():
        # Objects a chunk of any other row set still references are kept, checked
        # atomically with the delete so a concurrent commit cannot adopt a doomed object.
        await storage.delete_unreferenced(
            store._pool, binding_id, pairs, obligation_id=obligation_id
        )


async def _retire_period(
    store: PgReportingLedgerStore,
    obligation_id: str,
    account_id: str,
    ledger_record_retention_days: int | None,
    materializer_hold: str,
) -> None:
    # Phase 1: stop serving the rows. Exact reads answer as expired from here on. The
    # period is re-checked under the account lock: a late revision or restatement may have
    # committed since the candidate list was read.
    async with store._pool.connection() as connection, connection.transaction():
        await store._lock_account(connection, account_id)
        resuming = await (
            await connection.execute(
                "SELECT 1 FROM adcp_reporting_row_sets WHERE account_id = %s AND obligation_id = %s"
                " AND rows_state = 'pruning' LIMIT 1",
                (account_id, obligation_id),
            )
        ).fetchone()
        if resuming is None:
            still = await (
                await connection.execute(
                    prune_candidate_query(materializer_hold, only=True),
                    {
                        "account": account_id,
                        "only": obligation_id,
                        "record_days": ledger_record_retention_days,
                        "limit": 1,
                    },
                )
            ).fetchone()
            if still is None:
                raise NoLongerEligibleError
        row_set_ids = await mark_period_pruning(connection, obligation_id, account_id)
    # Phase 2: external bytes, at their recorded versions.
    await delete_period_objects(store, obligation_id, row_set_ids)
    # Phase 3: the manifests and bodies, together, then the marker state.
    async with store._pool.connection() as connection, connection.transaction():
        await store._lock_account(connection, account_id)
        await _finish_period(connection, obligation_id, account_id, row_set_ids)


async def _finish_period(
    connection: Any, obligation_id: str, account_id: str, row_set_ids: list[str]
) -> None:
    await connection.execute(
        "DELETE FROM adcp_reporting_row_chunks c USING adcp_reporting_row_sets s"
        " WHERE s.row_set_id = c.row_set_id AND s.rows_state = 'pruning'"
        " AND s.row_set_id = ANY(%s::text[])",
        (row_set_ids,),
    )
    # Bodies are content-addressed within one obligation, so only bodies no remaining
    # chunk of this obligation still names may go.
    await connection.execute(
        "DELETE FROM adcp_reporting_chunk_bodies b WHERE b.account_id = %s AND b.obligation_id = %s"
        " AND NOT EXISTS (SELECT 1 FROM adcp_reporting_row_chunks c"
        "                  WHERE c.account_id = b.account_id AND c.obligation_id = b.obligation_id"
        "                    AND c.sha256 = b.sha256)",
        (account_id, obligation_id),
    )
    await connection.execute(
        "UPDATE adcp_reporting_row_sets SET rows_state = 'pruned',"
        " rows_state_changed_at = clock_timestamp()"
        " WHERE row_set_id = ANY(%s::text[]) AND rows_state = 'pruning'",
        (row_set_ids,),
    )
