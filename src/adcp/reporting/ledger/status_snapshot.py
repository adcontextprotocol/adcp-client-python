"""Optional status participants and connection-bound snapshot/lifecycle helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from adcp.reporting.ledger.models import ConsumerStatusRecord
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusLifecycleIntent,
    apply_intents_to_snapshot,
    lifecycle_intents,
    status_matches_obligation,
)
from adcp.reporting.revision_selection import select_reporting_revision

if TYPE_CHECKING:
    from adcp.reporting.ledger.pg import PgReportingLedgerStore
    from adcp.reporting.ledger.store import InMemoryReportingLedgerStore


@runtime_checkable
class ReportingStatusParticipant(Protocol):
    """Optional upgrade; existing ReportingLedgerStore implementations stay valid."""

    async def read_status_snapshot(self, *, account_id: str) -> ReportingStatusSnapshot: ...

    async def record_consumer_status_with_lifecycle(
        self, status: ConsumerStatusRecord
    ) -> tuple[ConsumerStatusRecord, bool]: ...


def validate_status_evidence(
    status: ConsumerStatusRecord, snapshot: ReportingStatusSnapshot
) -> None:
    """Repeat ingest validation under the source transaction's account lock.

    Transport validation cannot fence a concurrent restatement. These readers
    consume only already captured evidence and never acquire a connection.
    """
    from adcp.reporting.ledger.consumer_status import validate_consumer_status_timing
    from adcp.reporting.ledger.store import LedgerConflictError

    generation = next(
        (c for c in snapshot.configurations if c.generation_key == status.generation_key), None
    )
    if generation is None:
        raise LedgerConflictError(
            "UNKNOWN_CONFIGURATION_GENERATION", "status generation is unavailable"
        )
    if status.report_definition_id != generation.report_definition_id:
        raise LedgerConflictError("REPORT_DEFINITION_MISMATCH", "status definition differs")
    if (status.seller_ledger_snapshot_id is None) != (status.seller_ledger_as_of is None):
        raise LedgerConflictError(
            "SELLER_SNAPSHOT_EVIDENCE_INCOMPLETE", "seller snapshot evidence is incomplete"
        )
    obligation = next(
        (o for o in snapshot.obligations if status_matches_obligation(status, o)),
        None,
    )
    if status.reporting_obligation_id is not None:
        # Trusted legacy writers retained a claimed ID on obligation_missing
        # before an account-local obligation existed. It is never an ownership
        # coordinate: attachment uses the validated generation and period only.
        # Public ingest still resolves every supplied ID before this participant.
        if obligation is None and status.consumer_status != "obligation_missing":
            raise LedgerConflictError("LOOKUP_UNAVAILABLE", "status evidence is unavailable")
        if obligation is not None and (
            obligation.generation_key != status.generation_key
            or obligation.report_definition_id != status.report_definition_id
            or obligation.period.start != status.period_start
            or obligation.period.end != status.period_end
        ):
            raise LedgerConflictError("OBLIGATION_IDENTITY_MISMATCH", "status evidence differs")
    validate_consumer_status_timing(status, generation, as_of=snapshot.as_of)
    required = None
    if obligation is not None:
        revisions = tuple(
            r
            for r in snapshot.revisions
            if r.reporting_obligation_id == obligation.reporting_obligation_id
        )
        selection = select_reporting_revision(
            revisions,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            required_finality=obligation.required_finality,
        )
        if selection.kind == "corrupt":
            raise LedgerConflictError("HISTORY_UNAVAILABLE", "the revision history requires repair")
        if selection.kind == "selected":
            required = selection.revision
    if status.reporting_revision_id is None:
        return
    revision = next(
        (r for r in snapshot.revisions if r.reporting_revision_id == status.reporting_revision_id),
        None,
    )
    if revision is None or (
        obligation is not None
        and revision.reporting_obligation_id != obligation.reporting_obligation_id
    ):
        raise LedgerConflictError("LOOKUP_UNAVAILABLE", "status evidence is unavailable")
    if status.consumer_status == "content_mismatch" and obligation is not None:
        if required is None or required.reporting_revision_id != status.reporting_revision_id:
            raise LedgerConflictError(
                "REVISION_NOT_CURRENTLY_REQUIRED", "status revision is no longer required"
            )


def memory_snapshot(
    store: InMemoryReportingLedgerStore, account_id: str
) -> ReportingStatusSnapshot:
    """Caller holds the memory transaction. No public method or lock acquisition."""
    statuses = tuple(s for s in store._statuses.values() if s.account_id == account_id)
    issues = tuple(i for i in store._issues.values() if i.account_id == account_id)
    consumer_ids = {s.consumer_id for s in statuses} | {
        i.consumer_id for i in issues if i.consumer_id is not None
    }
    # Reconciliation subclasses retain independently scoped destination/receipt evidence.
    for _, who, _ in getattr(store, "_delivery_records", []):
        if who.account_id == account_id:
            consumer_ids.add(who.consumer_id)
    return ReportingStatusSnapshot(
        account_id=account_id,
        as_of=store._clock(),
        configurations=tuple(
            c for c in store._configurations.values() if c.account_id == account_id
        ),
        obligations=tuple(o for o in store._obligations.values() if o.account_id == account_id),
        revisions=tuple(r for r in store._revisions.values() if r.account_id == account_id),
        statuses=statuses,
        lifecycles=issues,
        issue_scopes=tuple(
            (i, s) for (a, i), s in store._issue_status_scopes.items() if a == account_id
        ),
        consumer_ids=tuple(sorted(consumer_ids)),
        adjustments=tuple(a for a in store._adjustments.values() if a.account_id == account_id),
        changes=tuple(
            (seq, kind, record_id, "")
            for seq, a, kind, record_id, _ in store._changes
            if a == account_id
        ),
    )


def apply_memory_intents(
    store: InMemoryReportingLedgerStore, intents: tuple[StatusLifecycleIntent, ...]
) -> None:
    for intent in intents:
        issue = intent.lifecycle
        key = (issue.account_id, issue.issue_key)
        store._issues[key] = issue
        store._issue_generations[key] = max(store._issue_generations.get(key, 0), issue.generation)
        store._dirty_issue(issue, intent.scope, enqueue=False)


def settle_memory_snapshot(
    store: InMemoryReportingLedgerStore, account_id: str
) -> ReportingStatusSnapshot:
    for _ in range(3):
        snapshot = memory_snapshot(store, account_id)
        intents = lifecycle_intents(snapshot)
        if not intents:
            return snapshot
        apply_memory_intents(store, intents)
    raise ReportingNotificationError("status_lifecycle_did_not_converge")


_TABLES = {
    "configurations": "reporting_configurations",
    "obligations": "reporting_obligations",
    "revisions": "reporting_revisions",
    "statuses": "reporting_consumer_statuses",
    "lifecycles": "reporting_issue_lifecycle",
    "issue_scopes": "reporting_issue_status_scopes",
    "adjustments": "reporting_adjustments",
    "changes": "reporting_ledger_changes",
    "consumers": "reporting_reconciliation_heads",
}


async def read_snapshot_on(
    connection: Any,
    *,
    account_id: str,
    clock: Callable[[], datetime] | None = None,
    as_of: datetime | None = None,
    include_issue_scopes: bool = True,
) -> ReportingStatusSnapshot:
    from adcp.reporting.outbox.pg import database_now

    at = as_of or await database_now(connection, clock)
    raw: dict[str, Any] = {"account_id": account_id, "as_of": at.isoformat()}
    for key, table in _TABLES.items():
        if key == "issue_scopes" and not include_issue_scopes:
            continue
        # Table names are the closed SDK constants above. Parameters are bound.
        rows = await (
            await connection.execute(
                f"SELECT to_jsonb(r) FROM {table} r WHERE account_id = %s",  # nosec B608
                (account_id,),
            )
        ).fetchall()
        raw[key] = [row[0] for row in rows]
    return snapshot_from_storage(raw)


def snapshot_from_storage(raw: dict[str, Any]) -> ReportingStatusSnapshot:
    """Decode the exact SQL boundary input through the ledger's existing mappers."""
    from adcp.reporting.ledger.notification_models import decode_status_scope
    from adcp.reporting.ledger.pg import (
        _ADJUSTMENT_COLUMNS,
        _ISSUE_COLUMNS,
        _OBLIGATION_COLUMNS,
        _REVISION_COLUMNS,
        _STATUS_COLUMNS_BARE,
        _adjustment_from_row,
        _configuration_from_row,
        _issue_from_row,
        _obligation_from_row,
        _revision_from_row,
        _status_from_row,
    )

    dates = {
        "activated_at",
        "deactivated_at",
        "period_start",
        "period_end",
        "expected_at",
        "scope_resolved_at",
        "automated_recovery_deadline_at",
        "created_at",
        "observed_at",
        "data_through",
        "finalized_at",
        "status_as_of",
        "recorded_at",
        "seller_ledger_as_of",
        "opened_at",
        "retired_at",
        "accounting_period_start",
        "accounting_period_end",
        "correction_observed_at",
    }

    def decode(key: str, columns: str, builder: Callable[[Any], Any]) -> tuple[Any, ...]:
        names = [name.strip() for name in columns.split(",")]
        result = []
        for row in raw.get(key, []):
            values = [
                (
                    datetime.fromisoformat(row[n])
                    if n in dates and row.get(n) is not None
                    else row.get(n)
                )
                for n in names
            ]
            result.append(builder(values))
        return tuple(result)

    configurations = decode(
        "configurations",
        (
            "delivery_config_id, delivery_config_version, account_id, report_definition_id,"
            " reporting_profile, feed_purpose, required_finality, account_timezone, schedule,"
            " media_buy_ids, activated_at, deactivated_at, automated_recovery_seconds,"
            " status_retention_days, definition, authoritative_party"
        ),
        _configuration_from_row,
    )
    statuses = decode("statuses", _STATUS_COLUMNS_BARE, _status_from_row)
    lifecycles = decode("lifecycles", _ISSUE_COLUMNS, _issue_from_row)
    consumers = {row["consumer_id"] for row in raw.get("consumers", [])}
    consumers.update(s.consumer_id for s in statuses)
    consumers.update(i.consumer_id for i in lifecycles if i.consumer_id is not None)
    return ReportingStatusSnapshot(
        account_id=raw["account_id"],
        as_of=datetime.fromisoformat(raw["as_of"]),
        configurations=configurations,
        obligations=decode("obligations", _OBLIGATION_COLUMNS, _obligation_from_row),
        revisions=decode("revisions", _REVISION_COLUMNS, _revision_from_row),
        statuses=statuses,
        lifecycles=lifecycles,
        issue_scopes=tuple(
            (row["issue_id"], decode_status_scope(row["scope"]))
            for row in raw.get("issue_scopes", [])
        ),
        consumer_ids=tuple(sorted(consumers)),
        adjustments=decode("adjustments", _ADJUSTMENT_COLUMNS, _adjustment_from_row),
        changes=tuple(
            (row["seq"], row["record_kind"], row["record_id"], "") for row in raw.get("changes", [])
        ),
    )


async def apply_intents_on(
    store: PgReportingLedgerStore, connection: Any, intents: tuple[StatusLifecycleIntent, ...]
) -> None:
    """No pooled calls. The source/projector owns account and checkpoint locks."""
    for intent in intents:
        issue = intent.lifecycle
        if intent.action == "ensure_mismatch":
            await store._ensure_issue_opened_on(
                connection,
                issue_key=issue.issue_key,
                account_id=issue.account_id,
                consumer_id=issue.consumer_id,
                observed_at=issue.opened_at,
                status_scope=intent.scope,
                enqueue=False,
            )
        elif intent.action == "retire_mismatch":
            assert issue.retired_at is not None
            await store._retire_issue_on(
                connection,
                issue_key=issue.issue_key,
                account_id=issue.account_id,
                at=issue.retired_at,
                status_scope=intent.scope,
                enqueue=False,
                agreeing=True,
            )
        else:
            await store._dirty_issue(connection, issue, intent.scope, enqueue=False)


async def persist_replay_lifecycles_on(
    store: PgReportingLedgerStore, connection: Any, snapshot: ReportingStatusSnapshot
) -> None:
    """Persist derived historical occurrences without regressing later evidence.

    Captured source rows remain immutable. These are the same public lifecycle
    rows used by polling, including repairs for an old writer's interrupted
    status/issue pair. The enclosing account lock fences concurrent ingestion.
    """
    scopes = dict(snapshot.issue_scopes)
    for issue in sorted(snapshot.lifecycles, key=lambda i: (i.issue_key, i.generation)):
        await connection.execute(
            "INSERT INTO reporting_issue_lifecycle"
            " (account_id, issue_key, generation, issue_id, consumer_id, opened_at,"
            " issue_state, external_ref, retired_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (account_id, issue_key, generation) DO NOTHING",
            (
                issue.account_id,
                issue.issue_key,
                issue.generation,
                issue.issue_id,
                issue.consumer_id,
                issue.opened_at,
                issue.issue_state,
                issue.external_ref,
                issue.retired_at,
            ),
        )
        if issue.issue_state != "open":
            await connection.execute(
                "UPDATE reporting_issue_lifecycle SET issue_state=%s, external_ref=%s,"
                " retired_at=%s WHERE account_id=%s AND issue_key=%s AND generation=%s"
                " AND (issue_state IN ('open','acknowledged')"
                " OR (issue_state='waived' AND %s='resolved'))",
                (
                    issue.issue_state,
                    issue.external_ref,
                    issue.retired_at,
                    issue.account_id,
                    issue.issue_key,
                    issue.generation,
                    issue.issue_state,
                ),
            )
        scope = scopes.get(issue.issue_id)
        if scope is not None:
            # Existing scope may already be more precise because a later source
            # transaction attached this occurrence. Historical replay retains it.
            row = await (
                await connection.execute(
                    "SELECT scope FROM reporting_issue_status_scopes"
                    " WHERE account_id=%s AND issue_id=%s",
                    (issue.account_id, issue.issue_id),
                )
            ).fetchone()
            if row is not None:
                from adcp.reporting.ledger.notification_models import (
                    decode_status_scope,
                    validate_scope_refinement,
                )

                old = decode_status_scope(row[0])
                try:
                    validate_scope_refinement(old, scope)
                except ReportingNotificationError:
                    validate_scope_refinement(scope, old)
                    scope = old
            await store._dirty_issue(connection, issue, scope, enqueue=False)


async def settle_snapshot_on(
    store: PgReportingLedgerStore,
    connection: Any,
    *,
    account_id: str,
    as_of: datetime | None = None,
) -> ReportingStatusSnapshot:
    from adcp.reporting.outbox.pg import database_now

    at = as_of or await database_now(connection, store._clock)
    has_scope_storage = await store._issue_scope_storage_on(connection)
    for _ in range(3):
        snapshot = await read_snapshot_on(
            connection, account_id=account_id, as_of=at, include_issue_scopes=has_scope_storage
        )
        if not has_scope_storage:
            # Pre-outbox Core schemas retain lifecycle but have no scope table.
            # Derive scope from authenticated status evidence for this read;
            # notification-enabled participants require durable scope storage.
            snapshot = apply_intents_to_snapshot(
                snapshot,
                tuple(i for i in lifecycle_intents(snapshot) if i.action == "refine_scope"),
            )
        intents = lifecycle_intents(snapshot)
        if not intents:
            return snapshot
        await apply_intents_on(store, connection, intents)
    raise ReportingNotificationError("status_lifecycle_did_not_converge")
