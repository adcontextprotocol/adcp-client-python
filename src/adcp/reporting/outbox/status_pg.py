"""C-only queues and connection-bound PostgreSQL status transactions.

A/B workers cannot claim any of these rows. The generic outbox state machine
uses a closed table adapter; no SQL or table name comes from caller input.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from importlib.resources import files
from secrets import token_hex
from typing import Any

from pydantic import TypeAdapter

from adcp.reporting.ledger.models import ReportingDeliveryEscalation, ReportingIssueLifecycle
from adcp.reporting.ledger.notification_models import (
    ReportingDomainEvent,
    ReportingNotificationError,
    StatusChanged,
    decode_status_scope,
    event_storage,
)
from adcp.reporting.ledger.pg import PG_AVAILABLE, PgReportingLedgerStore
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusProjectionInput,
    project_status_scope,
    projection_scopes,
    with_replay_lifecycles,
)
from adcp.reporting.ledger.status_snapshot import (
    persist_replay_lifecycles_on,
    settle_snapshot_on,
    snapshot_from_storage,
)
from adcp.reporting.outbox.models import DeliveryBinding
from adcp.reporting.outbox.pg import PgReportingOutbox, database_now
from adcp.reporting.outbox.status import (
    StatusCheckpoint,
    StatusDueLease,
    StatusTurn,
    advance_checkpoint,
    escalation_identity,
    settled_replay,
)

_KEY = (
    "account_id, consumer_namespace, delivery_config_id, version, scope_kind, obligation_namespace"
)
_WHERE = (
    "account_id=%s AND consumer_namespace=%s AND delivery_config_id=%s AND version=%s"
    " AND scope_kind=%s AND obligation_namespace=%s"
)
_CHECKPOINT = (
    "scope, fingerprint, generation, snapshot, next_due_at, source_sequence, baseline, publishable,"
    " lease_token, lease_expires_at, initialized"
)

_LIFECYCLE = TypeAdapter(ReportingIssueLifecycle)


def _replay_storage(snapshot: ReportingStatusSnapshot) -> str:
    scopes = dict(snapshot.issue_scopes)
    return json.dumps(
        [
            {
                "lifecycle": _LIFECYCLE.dump_python(i, mode="json"),
                "scope": asdict(scopes[i.issue_id]) if i.issue_id in scopes else None,
            }
            for i in snapshot.lifecycles
        ]
    )


def _with_replay(
    snapshot: ReportingStatusSnapshot, rows: list[dict[str, Any]]
) -> ReportingStatusSnapshot:
    prior = replace(
        snapshot,
        lifecycles=tuple(_LIFECYCLE.validate_python(r["lifecycle"]) for r in rows),
        issue_scopes=tuple(
            (r["lifecycle"]["issue_id"], decode_status_scope(r["scope"]))
            for r in rows
            if r["scope"] is not None
        ),
    )
    return settled_replay(with_replay_lifecycles(snapshot, prior))


class _StatusQueueConnection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def transaction(self) -> Any:
        return self.connection.transaction()

    async def execute(self, query: str, params: Any = None) -> Any:
        for old, new in (
            ("reporting_notification_", "reporting_status_notification_"),
            ("reporting_webhook_", "reporting_status_webhook_"),
        ):
            query = query.replace(old, new)
        return await self.connection.execute(query, params)


class PgReportingStatusOutbox(PgReportingOutbox):
    """Separate C events, expansion, delivery and activity, with generic HTTP logic."""

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as connection:
            yield _StatusQueueConnection(connection)

    async def create_schema(self) -> None:
        await PgStatusNotificationStore(
            PgReportingLedgerStore(pool=self._pool, clock=self._clock, notifications=True)
        ).create_schema()

    async def _next_attempt_on(self, conn: Any, binding: DeliveryBinding) -> int:
        b = binding
        row = await (
            await conn.execute(
                "INSERT INTO reporting_webhook_attempt_heads (account_id, consumer_namespace,"
                " principal_id, subscriber_id, idempotency_key, last_attempt)"
                " VALUES (%s,%s,%s,%s,%s,1) ON CONFLICT"
                " (account_id, consumer_namespace, principal_id, subscriber_id, idempotency_key)"
                " DO UPDATE SET last_attempt=reporting_webhook_attempt_heads.last_attempt+1"
                " WHERE reporting_webhook_attempt_heads.account_id=%s"
                " AND reporting_webhook_attempt_heads.consumer_namespace=%s"
                " AND reporting_webhook_attempt_heads.principal_id=%s RETURNING last_attempt",
                (
                    b.account_id,
                    b.consumer_namespace,
                    b.principal_id,
                    b.subscriber_id,
                    b.idempotency_key,
                    b.account_id,
                    b.consumer_namespace,
                    b.principal_id,
                ),
            )
        ).fetchone()
        assert row is not None
        return int(row[0])


async def _enqueue_on(connection: Any, event: ReportingDomainEvent) -> None:
    cause = event.cause
    if not isinstance(cause, StatusChanged):
        raise ReportingNotificationError("invalid_status_projection")
    key = cause.scope.checkpoint_key
    await connection.execute(
        "INSERT INTO reporting_status_notification_events"
        " (account_id, consumer_namespace, delivery_config_id, version, scope_kind,"
        " obligation_namespace, notification_id, notification_type, cause_kind, cause_id,"
        " cause_generation, fingerprint, fired_at, snapshot)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
        (
            *key,
            event.notification_id,
            event.notification_type,
            cause.kind,
            event.cause_id,
            event.cause_generation,
            cause.fingerprint,
            event.fired_at,
            json.dumps(event_storage(event)),
        ),
    )
    await connection.execute(
        "INSERT INTO reporting_status_notification_expansions"
        " (account_id, consumer_namespace, notification_id, emission_generation, due_at)"
        " VALUES (%s,%s,%s,1,%s)",
        (event.account_id, event.consumer_namespace, event.notification_id, event.fired_at),
    )


def _checkpoint(row: Any) -> StatusCheckpoint | None:
    if row is None or not row[-1]:
        return None
    return StatusCheckpoint(decode_status_scope(row[0]), *row[1:-1])


class PgStatusNotificationStore:
    """Optional C transaction participant over an existing notification-enabled ledger."""

    def __init__(
        self,
        ledger: PgReportingLedgerStore,
        *,
        escalation: ReportingDeliveryEscalation | None = None,
    ) -> None:
        if not PG_AVAILABLE:
            raise ImportError("PgStatusNotificationStore requires PostgreSQL; install adcp[pg]")
        if not isinstance(ledger, PgReportingLedgerStore) or not ledger._notifications_enabled:
            raise ReportingNotificationError("status_chain_unready")
        self.ledger, self.escalation = ledger, escalation
        self.outbox = PgReportingStatusOutbox(pool=ledger._pool, clock=ledger._clock)

    async def create_schema(self) -> None:
        from adcp.reporting.outbox.status_schema import validate_status_schema

        async with self.ledger._pool.connection() as connection, connection.transaction():
            await self.ledger._create_schema_on(connection)
            await connection.execute(
                files("adcp.reporting.ledger")
                .joinpath("reporting_status_notifications.sql")
                .read_text()
            )
            await validate_status_schema(connection)

    @asynccontextmanager
    async def _transaction(self, account_id: str) -> AsyncIterator[Any]:
        async with self.ledger._pool.connection() as connection, connection.transaction():
            await self.ledger._lock_account(connection, account_id)
            yield connection

    async def _account_on(self, connection: Any, account_id: str) -> int:
        row = await (
            await connection.execute(
                "SELECT dirty_sequence, policy, baseline_complete FROM reporting_status_accounts"
                " WHERE account_id=%s FOR UPDATE",
                (account_id,),
            )
        ).fetchone()
        if row is None or not row[2]:
            raise ReportingNotificationError("status_baseline_required")
        if row[1] != escalation_identity(self.escalation):
            raise ReportingNotificationError("status_policy_conflict")
        return int(row[0])

    async def _lock_scopes_on(self, connection: Any, snapshot: ReportingStatusSnapshot) -> None:
        # Insert placeholders before allocating random IDs. New and existing
        # typed scopes have exactly the same row-lock/notification boundary.
        for scope in projection_scopes(snapshot):
            await connection.execute(
                f"INSERT INTO reporting_status_scope_checkpoints ({_KEY}, scope, fingerprint,"  # nosec B608
                " snapshot, source_sequence, baseline, publishable)"
                ' VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,\'{"health":"waiting"}\',0,FALSE,FALSE)'
                f" ON CONFLICT ({_KEY}) DO NOTHING",  # nosec B608
                (*scope.checkpoint_key, json.dumps(asdict(scope)), "0" * 64),
            )
        await (
            await connection.execute(
                "SELECT 1 FROM reporting_status_scope_checkpoints WHERE account_id=%s"
                f" ORDER BY {_KEY}"  # nosec B608
                " FOR UPDATE",
                (snapshot.account_id,),
            )
        ).fetchall()

    async def _write_on(self, connection: Any, checkpoint: StatusCheckpoint) -> None:
        await connection.execute(
            "UPDATE reporting_status_scope_checkpoints SET scope=%s::jsonb, fingerprint=%s,"
            " generation=%s, snapshot=%s::jsonb, next_due_at=%s, source_sequence=%s,"
            " baseline=%s, publishable=%s, initialized=TRUE"
            f" WHERE {_WHERE}",  # nosec B608
            (
                json.dumps(asdict(checkpoint.scope)),
                checkpoint.fingerprint,
                checkpoint.generation,
                json.dumps(checkpoint.snapshot),
                checkpoint.next_due_at,
                checkpoint.source_sequence,
                checkpoint.baseline,
                checkpoint.publishable,
                *checkpoint.scope.checkpoint_key,
            ),
        )

    async def _apply_on(
        self,
        connection: Any,
        snapshot: ReportingStatusSnapshot,
        *,
        through: int,
        baseline: bool = False,
    ) -> int:
        await self._lock_scopes_on(connection, snapshot)
        snapshot = settled_replay(snapshot)
        count = 0
        for scope in projection_scopes(snapshot):
            row = await (
                await connection.execute(
                    f"SELECT {_CHECKPOINT} FROM reporting_status_scope_checkpoints WHERE {_WHERE}"  # nosec B608
                    " FOR UPDATE",
                    scope.checkpoint_key,
                )
            ).fetchone()
            result = project_status_scope(StatusProjectionInput(snapshot, scope, self.escalation))
            checkpoint, event = advance_checkpoint(
                _checkpoint(row),
                result,
                fired_at=await database_now(connection, self.ledger._clock),
                source_sequence=through,
                baseline=baseline,
            )
            await self._write_on(connection, checkpoint)
            if event is not None:
                await _enqueue_on(connection, event)
                count += 1
        return count

    async def baseline(self, *, account_id: str) -> bool:
        from adcp.reporting.outbox.status_schema import validate_status_schema

        async with self._transaction(account_id) as connection:
            await validate_status_schema(connection)
            row = await (
                await connection.execute(
                    "SELECT baseline_complete FROM reporting_status_accounts WHERE account_id=%s",
                    (account_id,),
                )
            ).fetchone()
            if row is not None and row[0]:
                await self._account_on(connection, account_id)
                return False
            await connection.execute(
                "INSERT INTO reporting_status_accounts (account_id, policy) VALUES (%s,%s::jsonb)"
                " ON CONFLICT (account_id) DO NOTHING",
                (account_id, json.dumps(escalation_identity(self.escalation))),
            )
            snapshot = await settle_snapshot_on(self.ledger, connection, account_id=account_id)
            row = await (
                await connection.execute(
                    "SELECT max_sequence FROM reporting_status_dirty_heads WHERE account_id=%s",
                    (account_id,),
                )
            ).fetchone()
            through = int(row[0]) if row is not None else 0
            await self._apply_on(connection, snapshot, through=through, baseline=True)
            await connection.execute(
                "UPDATE reporting_status_accounts SET baseline_complete=TRUE,"
                " baseline_highwater=%s,"
                " dirty_sequence=%s, baseline_at=%s, replay_lifecycles=%s::jsonb"
                " WHERE account_id=%s",
                (through, through, snapshot.as_of, _replay_storage(snapshot), account_id),
            )
            return True

    async def baseline_ready(self, *, account_id: str) -> bool:
        from adcp.reporting.outbox.status_schema import validate_status_schema

        async with self._transaction(account_id) as connection:
            await validate_status_schema(connection)
            row = await (
                await connection.execute(
                    "SELECT baseline_complete, policy FROM reporting_status_accounts"
                    " WHERE account_id=%s",
                    (account_id,),
                )
            ).fetchone()
            if row is None or not row[0]:
                return False
            if row[1] != escalation_identity(self.escalation):
                raise ReportingNotificationError("status_policy_conflict")
            # Readiness describes the durable lifecycle, independently of the
            # account's current business health or waived issue occurrences.
            return True

    async def _project_on(self, connection: Any, account_id: str) -> StatusTurn:
        through = await self._account_on(connection, account_id)
        row = await (
            await connection.execute(
                "SELECT through, input FROM reporting_status_boundaries WHERE account_id=%s"
                " AND through > %s ORDER BY through LIMIT 1",
                (account_id, through),
            )
        ).fetchone()
        if row is None:
            return StatusTurn(False)
        snapshot = snapshot_from_storage(row[1])
        await self._lock_scopes_on(connection, snapshot)
        replay = await (
            await connection.execute(
                "SELECT replay_lifecycles FROM reporting_status_accounts WHERE account_id=%s",
                (account_id,),
            )
        ).fetchone()
        snapshot = _with_replay(snapshot, replay[0])
        await persist_replay_lifecycles_on(self.ledger, connection, snapshot)
        count = await self._apply_on(connection, snapshot, through=row[0])
        await connection.execute(
            "UPDATE reporting_status_accounts SET dirty_sequence=%s, replay_lifecycles=%s::jsonb"
            " WHERE account_id=%s",
            (row[0], _replay_storage(snapshot), account_id),
        )
        return StatusTurn(True, count)

    async def project_one(self, *, account_id: str) -> StatusTurn:
        async with self._transaction(account_id) as connection:
            return await self._project_on(connection, account_id)

    async def claim_due(
        self, *, account_id: str, lease_seconds: float = 30
    ) -> StatusDueLease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self.ledger._pool.connection() as connection, connection.transaction():
            at = await database_now(connection, self.ledger._clock)
            row = await (
                await connection.execute(
                    "SELECT scope, next_due_at FROM reporting_status_scope_checkpoints"
                    " WHERE account_id=%s AND next_due_at <= %s"
                    " AND (lease_expires_at IS NULL OR lease_expires_at <= %s)"
                    f" ORDER BY next_due_at, {_KEY} FOR UPDATE SKIP LOCKED LIMIT 1",  # nosec B608
                    (account_id, at, at),
                )
            ).fetchone()
            if row is None:
                return None
            # The claim clock is read after the nonblocking row lock as well.
            at = await database_now(connection, self.ledger._clock)
            lease = StatusDueLease(
                decode_status_scope(row[0]),
                token_hex(32),
                at + timedelta(seconds=lease_seconds),
                row[1],
            )
            await connection.execute(
                "UPDATE reporting_status_scope_checkpoints SET lease_token=%s, lease_expires_at=%s"
                f" WHERE {_WHERE}",  # nosec B608
                (lease.token, lease.expires_at, *lease.scope.checkpoint_key),
            )
            return lease

    async def _held_on(self, connection: Any, lease: StatusDueLease) -> bool:
        at = await database_now(connection, self.ledger._clock)
        row = await (
            await connection.execute(
                f"SELECT 1 FROM reporting_status_scope_checkpoints WHERE {_WHERE}"  # nosec B608
                " AND lease_token=%s AND lease_expires_at=%s AND lease_expires_at > %s FOR UPDATE",
                (*lease.scope.checkpoint_key, lease.token, lease.expires_at, at),
            )
        ).fetchone()
        return row is not None

    async def _ack_on(self, connection: Any, lease: StatusDueLease) -> bool:
        at = await database_now(connection, self.ledger._clock)
        cursor = await connection.execute(
            "UPDATE reporting_status_scope_checkpoints SET lease_token=NULL, lease_expires_at=NULL"
            f" WHERE {_WHERE} AND lease_token=%s AND lease_expires_at > %s",  # nosec B608
            (*lease.scope.checkpoint_key, lease.token, at),
        )
        return bool(cursor.rowcount)

    async def complete_due(self, lease: StatusDueLease) -> StatusTurn:
        try:
            async with self._transaction(lease.scope.account_id) as connection:
                await self._account_on(connection, lease.scope.account_id)
                if not await self._held_on(connection, lease):
                    return StatusTurn(False)
                count = 0
                repaired = False
                while (turn := await self._project_on(connection, lease.scope.account_id)).did_work:
                    count += turn.events
                    repaired = True
                snapshot = await settle_snapshot_on(
                    self.ledger, connection, account_id=lease.scope.account_id
                )
                through = await self._account_on(connection, lease.scope.account_id)
                if repaired:
                    count += await self._apply_on(connection, snapshot, through=through)
                else:
                    for _ in range(1000):
                        row = await (
                            await connection.execute(
                                "SELECT min(next_due_at) FROM reporting_status_scope_checkpoints"
                                " WHERE account_id=%s AND next_due_at <= %s",
                                (lease.scope.account_id, snapshot.as_of),
                            )
                        ).fetchone()
                        if row is None or row[0] is None:
                            break
                        count += await self._apply_on(
                            connection, replace(snapshot, as_of=row[0]), through=through
                        )
                    else:
                        raise ReportingNotificationError("status_deadline_limit")
                if not await self._ack_on(connection, lease):
                    raise _ExpiredStatusLeaseError
                return StatusTurn(True, count)
        except _ExpiredStatusLeaseError:
            return StatusTurn(False)

    async def release_due(self, lease: StatusDueLease) -> bool:
        async with self._transaction(lease.scope.account_id) as connection:
            return await self._ack_on(connection, lease)

    async def checkpoints(self, *, account_id: str) -> tuple[StatusCheckpoint, ...]:
        async with self.ledger._pool.connection() as connection:
            rows = await (
                await connection.execute(
                    f"SELECT {_CHECKPOINT} FROM reporting_status_scope_checkpoints"  # nosec B608
                    f" WHERE account_id=%s ORDER BY {_KEY}",
                    (account_id,),  # nosec B608
                )
            ).fetchall()
        return tuple(c for row in rows if (c := _checkpoint(row)) is not None)


class _ExpiredStatusLeaseError(Exception):
    pass
