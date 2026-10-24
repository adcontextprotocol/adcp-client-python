"""Connection-bound status snapshot reads and lifecycle persistence over PostgreSQL.

The pure projection helpers (memory snapshots, evidence validation, decoding a
stored snapshot) are :mod:`adcp.reporting.ledger.status_snapshot`; these
functions run on a caller-owned connection inside the account transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusLifecycleIntent,
    apply_intents_to_snapshot,
    lifecycle_intents,
)
from adcp.reporting.ledger.status_snapshot import snapshot_from_storage

if TYPE_CHECKING:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

__all__ = [
    "apply_intents_on",
    "persist_replay_lifecycles_on",
    "read_snapshot_on",
    "settle_snapshot_on",
]


_TABLES = {
    "configurations": "reporting_configurations",
    "obligations": "reporting_obligations",
    "revisions": "reporting_revisions",
    "statuses": "reporting_consumer_statuses",
    "lifecycles": "reporting_issue_lifecycle",
    "waivers": "reporting_issue_waiver_bindings",
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
    from adcp_reporting_storage.ledger.pg import _waiver_storage_on
    from adcp_reporting_storage.outbox.pg import database_now

    at = as_of or await database_now(connection, clock)
    raw: dict[str, Any] = {"account_id": account_id, "as_of": at.isoformat()}
    for key, table in _TABLES.items():
        if key == "issue_scopes" and not include_issue_scopes:
            continue
        if key == "waivers" and not await _waiver_storage_on(connection):
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
    from adcp_reporting_storage.ledger.pg import _save_waiver_binding

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
        await _save_waiver_binding(connection, issue)
        if issue.issue_state != "open":
            await connection.execute(
                "UPDATE reporting_issue_lifecycle SET issue_state=%s, external_ref=%s,"
                " retired_at=%s"
                " WHERE account_id=%s AND issue_key=%s AND generation=%s"
                " AND issue_state IN ('open','acknowledged')",
                (
                    issue.issue_state,
                    issue.external_ref,
                    issue.retired_at,
                    issue.account_id,
                    issue.issue_key,
                    issue.generation,
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
    from adcp_reporting_storage.outbox.pg import database_now

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
