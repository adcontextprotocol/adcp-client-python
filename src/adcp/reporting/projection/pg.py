"""Connection-bound v2 capture over the reviewed feed and status participants."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib.resources import files
from typing import Any

from adcp.reporting.feed.pg import PgReportingFeedStore, _CapturedFeed, _PreparedFeed
from adcp.reporting.feed.snapshot import StoredFeedSnapshot
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.models import ReportingDeliveryEscalation
from adcp.reporting.ledger.notification_models import (
    ReportingDomainEvent,
    ReportingNotificationError,
)
from adcp.reporting.ledger.status_projection import with_replay_lifecycles
from adcp.reporting.ledger.status_snapshot import persist_replay_lifecycles_on
from adcp.reporting.outbox.pg import database_now
from adcp.reporting.outbox.status import (
    StatusDueLease,
    StatusTurn,
    escalation_identity,
    settled_replay,
)
from adcp.reporting.outbox.status_pg import _CHECKPOINT as _LEGACY_CHECKPOINT
from adcp.reporting.outbox.status_pg import PgStatusNotificationStore, _enqueue_on
from adcp.reporting.outbox.status_pg import _checkpoint as _legacy_checkpoint
from adcp.reporting.projection.capture import (
    ReportingProjectionInput,
    decode_projection_input,
    with_projection_core,
)
from adcp.reporting.projection.history import (
    checkpoint_document,
    checkpoint_key,
    decode_boundary,
    decode_checkpoint,
    project_boundary,
)
from adcp.reporting.projection.notifications import (
    PgReportingProjectionOutbox,
    _ProjectionQueueConnection,
)
from adcp.reporting.projection.schema import validate_projection_schema


class _ProjectionFeedConnection:
    """Closed SDK table selection; no adopter-supplied SQL identifiers."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def execute(self, query: str, params: Any = None) -> Any:
        return await self.connection.execute(
            query.replace("reporting_feed_snapshots", "reporting_projection_feed_snapshots"), params
        )


class PgReportingProjectionStore(PgReportingFeedStore):
    """Optional v2 store. Installation alone neither activates tiers nor sends readiness."""

    _projection_read_policy: dict[str, Any]
    _projection_read_escalation: ReportingDeliveryEscalation | None

    async def create_schema(self) -> None:
        async with self._connection() as connection, connection.transaction():
            await self._create_schema_on(connection)
            root = files("adcp.reporting.ledger")
            for name in (
                "reporting_materializer.sql",
                "reporting_receipt_ingestion.sql",
                "reporting_feed.sql",
                "reporting_status_notifications.sql",
                "reporting_status_selector_version.sql",
                "reporting_projection.sql",
                "reporting_projection_notifications.sql",
                "reporting_projection_feed.sql",
            ):
                await connection.execute(root.joinpath(name).read_text())

    async def _feed_snapshot_on(
        self, connection: Any, snapshot_id: str, caller: ReportingDeliveryPrincipal
    ) -> StoredFeedSnapshot | None:
        legacy = await super()._feed_snapshot_on(connection, snapshot_id, caller)
        if legacy is not None:
            return legacy
        return await super()._feed_snapshot_on(
            _ProjectionFeedConnection(connection), snapshot_id, caller
        )

    async def _save_feed_snapshot_on(self, connection: Any, stored: _PreparedFeed) -> None:
        if stored.snapshot.representation_version == 2:
            connection = _ProjectionFeedConnection(connection)
        await super()._save_feed_snapshot_on(connection, stored)

    async def _capture_feed_on(
        self, connection: Any, caller: ReportingDeliveryPrincipal
    ) -> _CapturedFeed:
        captured = await super()._capture_feed_on(connection, caller)
        row = await (
            await connection.execute(
                "SELECT ownership_enabled,consumer_status_enabled"
                " FROM reporting_projection_accounts WHERE account_id=%s"
                " AND current_input IS NOT NULL",
                (caller.account_id,),
            )
        ).fetchone()
        if row is None:
            return captured
        await validate_projection_schema(connection, notifications=self._notifications_enabled)
        return replace(
            captured,
            representation_version=2,
            revision_ownership=row[0],
            activated_consumer_status_enabled=row[1],
        )

    async def read_projection_input(
        self, *, caller: ReportingDeliveryPrincipal
    ) -> ReportingProjectionInput:
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, caller.account_id)
            await validate_projection_schema(connection, notifications=self._notifications_enabled)
            row = await (
                await connection.execute(
                    "SELECT input,content_sha256=reporting_receipt_ingestion_sha256(input)"
                    " FROM reporting_projection_inputs WHERE account_id=%s"
                    " AND EXISTS (SELECT 1 FROM reporting_projection_accounts a"
                    " WHERE a.account_id=reporting_projection_inputs.account_id"
                    " AND a.current_input IS NOT NULL)"
                    " ORDER BY sequence DESC LIMIT 1",
                    (caller.account_id,),
                )
            ).fetchone()
            if row is None:
                raise ReportingNotificationError("status_projection_activation_required")
            if not row[1]:
                raise ReportingNotificationError("status_projection_history_corrupt")
            value = decode_projection_input(row[0])
            return value.at(await database_now(connection, self._clock))

    async def read_tier_status(
        self,
        request: dict[str, Any],
        *,
        caller: ReportingDeliveryPrincipal,
        consumer_status_enabled: bool = False,
    ) -> dict[str, Any]:
        from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
        from adcp.reporting.projection.wire import render_tier_status

        async with self._connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT policy FROM reporting_projection_accounts WHERE account_id=%s"
                    " AND current_input IS NOT NULL",
                    (caller.account_id,),
                )
            ).fetchone()
        if row is None:
            return await ReportingStatusHandler(
                self, consumer_status_enabled=consumer_status_enabled
            ).handle(request, caller=ReportingStatusCaller(caller.account_id, caller.consumer_id))
        value = await self.read_projection_input(caller=caller)
        return await asyncio.to_thread(
            render_tier_status, self, request, value, caller, row[0], consumer_status_enabled
        )


class PgReportingStatusProjection(PgStatusNotificationStore):
    """v2 input/cursor lifecycle reusing C's pure projector, checkpoint and event transaction."""

    _projection_version = 2
    ledger: PgReportingProjectionStore

    def __init__(
        self,
        ledger: PgReportingProjectionStore,
        *,
        consumer_status_enabled: bool = False,
        revision_ownership: bool = False,
        escalation: ReportingDeliveryEscalation | None = None,
    ) -> None:
        if not isinstance(ledger, PgReportingProjectionStore):
            raise ReportingNotificationError("status_projection_component_unready")
        if type(consumer_status_enabled) is not bool or type(revision_ownership) is not bool:
            raise ValueError("projection feature selections must be booleans")
        self.ledger, self.escalation = ledger, escalation
        self.consumer_status_enabled, self.revision_ownership = (
            consumer_status_enabled,
            revision_ownership,
        )
        self.outbox = PgReportingProjectionOutbox(pool=ledger._pool, clock=ledger._clock)
        ledger._projection_read_policy = self.policy
        ledger._projection_read_escalation = escalation

    async def _enqueue_status_on(self, connection: Any, event: ReportingDomainEvent) -> None:
        await _enqueue_on(_ProjectionQueueConnection(connection), event)

    @property
    def policy(self) -> dict[str, Any]:
        return {
            "version": 2,
            "escalation": escalation_identity(self.escalation),
            "consumer_status_enabled": self.consumer_status_enabled,
            "ownership_enabled": self.revision_ownership,
            "notifications_enabled": self.ledger._notifications_enabled,
        }

    async def create_schema(self) -> None:
        await self.ledger.create_schema()

    @asynccontextmanager
    async def _transaction(self, account_id: str) -> AsyncIterator[Any]:
        async with self.ledger._connection() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('adcp.reporting.projection_version','2',true)"
            )
            await connection.execute(
                "SELECT set_config('adcp.reporting.selector_semantics_version','2',true)"
            )
            await connection.execute(
                "SELECT set_config('adcp.reporting.projection_internal','on',true)"
            )
            await self.ledger._lock_account(connection, account_id)
            if self.ledger._clock is not None:
                await connection.execute(
                    "SELECT set_config('adcp.reporting.projection_clock',%s,true)",
                    (self.ledger._clock().isoformat(),),
                )
            yield connection

    async def _state_on(self, connection: Any, account_id: str) -> tuple[Any, ...]:
        row = await (
            await connection.execute(
                "SELECT policy,cursor,max_sequence,checkpoint_floor,current_input,current_as_of"
                " FROM reporting_projection_accounts WHERE account_id=%s FOR UPDATE",
                (account_id,),
            )
        ).fetchone()
        if row is None:
            raise ReportingNotificationError("status_projection_activation_required")
        if row[0] != self.policy:
            raise ReportingNotificationError("status_projection_policy_conflict")
        return tuple(row)

    async def _account_on(self, connection: Any, account_id: str) -> int:
        await self._state_on(connection, account_id)
        return await super()._account_on(connection, account_id)

    async def baseline_ready(self, *, account_id: str) -> bool:
        async with self._transaction(account_id) as connection:
            await validate_projection_schema(
                connection, notifications=self.ledger._notifications_enabled
            )
            row = await (
                await connection.execute(
                    "SELECT policy,current_input IS NOT NULL FROM reporting_projection_accounts"
                    " WHERE account_id=%s",
                    (account_id,),
                )
            ).fetchone()
            if row is None:
                return False
            if row[0] != self.policy:
                raise ReportingNotificationError("status_projection_policy_conflict")
            return bool(row[1])

    async def activate(self, *, account_id: str) -> bool:
        """Fence, replay captured private history, then activate the frozen baseline.

        Each retained input advances in its own transaction. Cancellation or a
        process crash resumes at that exact cursor. The derived historical
        events stay in an immutable epoch-zero journal, never a delivery queue.
        Old C projectors/sweepers must first be stopped and drained.
        """
        started = await self._begin_activation(account_id=account_id)
        while True:
            async with self._transaction(account_id) as connection:
                state = await self._state_on(connection, account_id)
                if state[4] is not None:
                    return started
                await self._activation_step_on(connection, account_id)

    async def _begin_activation(self, *, account_id: str) -> bool:
        async with self._transaction(account_id) as connection:
            await validate_projection_schema(
                connection, notifications=self.ledger._notifications_enabled
            )
            existing = await (
                await connection.execute(
                    "SELECT policy FROM reporting_projection_accounts WHERE account_id=%s",
                    (account_id,),
                )
            ).fetchone()
            if existing is not None:
                await self._state_on(connection, account_id)
                return False
            now = await database_now(connection, self.ledger._clock)
            old = await (
                await connection.execute(
                    "SELECT dirty_sequence,baseline_complete,policy FROM reporting_status_accounts"
                    " WHERE account_id=%s FOR UPDATE",
                    (account_id,),
                )
            ).fetchone()
            dirty = await (
                await connection.execute(
                    "SELECT coalesce(max_sequence,0) FROM reporting_status_dirty_heads"
                    " WHERE account_id=%s",
                    (account_id,),
                )
            ).fetchone()
            through = int(dirty[0]) if dirty else 0
            if old is not None and old[1]:
                if old[2] != escalation_identity(self.escalation):
                    raise ReportingNotificationError("status_policy_conflict")
                if int(old[0]) != through or await self._needs_rebuild_on(connection, account_id):
                    raise ReportingNotificationError("status_projection_legacy_drain_required")
            rows = await (
                await connection.execute(
                    "SELECT source_sequence,lease_expires_at,next_due_at FROM"
                    " reporting_status_scope_checkpoints"
                    " WHERE account_id=%s ORDER BY"
                    " consumer_namespace,delivery_config_id,version,scope_kind,"
                    " obligation_namespace FOR UPDATE",
                    (account_id,),
                )
            ).fetchall()
            if any(
                (r[1] is not None and r[1] > now) or (r[2] is not None and r[2] <= now)
                for r in rows
            ):
                raise ReportingNotificationError("status_projection_legacy_drain_required")
            floor = max((int(r[0]) for r in rows), default=0)
            # The inherited SDK column list is fixed; the account is bound.
            old_checkpoints = [
                _legacy_checkpoint(r)
                for r in await (
                    await connection.execute(
                        f"SELECT {_LEGACY_CHECKPOINT} FROM reporting_status_scope_checkpoints"  # nosec B608
                        " WHERE account_id=%s",
                        (account_id,),
                    )
                ).fetchall()
            ]
            generation_floor = max((c.generation for c in old_checkpoints if c), default=0)
            legacy_head = await (
                await connection.execute(
                    "SELECT captured_sequence FROM reporting_materializer_accounts"
                    " WHERE account_id=%s",
                    (account_id,),
                )
            ).fetchone()
            legacy_through = int(legacy_head[0]) if legacy_head else 0
            await connection.execute(
                "INSERT INTO"
                " reporting_status_accounts(account_id,policy,baseline_complete,dirty_sequence,"
                "baseline_highwater,baseline_at,selector_target_version,selector_transition)"
                " VALUES(%s,%s::jsonb,TRUE,%s,%s,%s,2,'complete')"
                " ON CONFLICT(account_id) DO NOTHING",
                (
                    account_id,
                    json.dumps(escalation_identity(self.escalation)),
                    through,
                    through,
                    now,
                ),
            )
            await connection.execute(
                "INSERT INTO"
                " reporting_projection_accounts(account_id,activated_at,notifications_enabled,"
                "consumer_status_enabled,ownership_enabled,policy,legacy_through,checkpoint_floor,"
                "legacy_capture_through,legacy_generation_floor)"
                " VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)",
                (
                    account_id,
                    now,
                    self.ledger._notifications_enabled,
                    self.consumer_status_enabled,
                    self.revision_ownership,
                    json.dumps(self.policy),
                    through,
                    floor + legacy_through,
                    legacy_through,
                    generation_floor,
                ),
            )
            for checkpoint in old_checkpoints:
                if checkpoint is None:
                    continue
                document = json.dumps(checkpoint_document(checkpoint))
                await connection.execute(
                    "INSERT INTO reporting_projection_legacy_baselines VALUES"
                    " (%s,%s,%s::jsonb,reporting_receipt_ingestion_sha256(%s::jsonb))",
                    (account_id, checkpoint_key(checkpoint), document, document),
                )
            await connection.execute(
                "UPDATE reporting_status_scope_checkpoints SET projection_writer_floor=2,"
                "lease_token=NULL,lease_expires_at=NULL WHERE account_id=%s",
                (account_id,),
            )
            await connection.execute("SELECT reporting_projection_capture(%s)", (account_id,))
            return True

    async def _activation_step_on(self, connection: Any, account_id: str) -> StatusTurn:
        metadata = await (
            await connection.execute(
                "SELECT legacy_capture_through,legacy_capture_cursor,"
                "checkpoint_floor FROM reporting_projection_accounts WHERE account_id=%s",
                (account_id,),
            )
        ).fetchone()
        through, cursor, floor = map(int, metadata)
        if cursor < through:
            row = await (
                await connection.execute(
                    "SELECT kind,input,content_sha256=reporting_receipt_ingestion_sha256(input),"
                    " count(*) OVER (PARTITION BY account_sequence)"
                    " FROM (SELECT 'materializer' AS kind,account_sequence,input,content_sha256"
                    " FROM reporting_materializer_status_boundaries WHERE account_id=%s"
                    " AND account_sequence>%s AND account_sequence<=%s UNION ALL"
                    " SELECT 'receipt',account_sequence,input,content_sha256"
                    " FROM reporting_receipt_ingestion_boundaries WHERE account_id=%s"
                    " AND account_sequence>%s AND account_sequence<=%s) b"
                    " ORDER BY account_sequence LIMIT 1",
                    (account_id, cursor, through, account_id, cursor, through),
                )
            ).fetchone()
            if row is None or not row[2] or row[3] != 1:
                raise ReportingNotificationError("status_projection_history_corrupt")
            boundary = decode_boundary(row[0], row[1])
            if boundary.account_sequence != cursor + 1 or boundary.caller.account_id != account_id:
                raise ReportingNotificationError("status_projection_history_corrupt")
            previous_rows = await (
                await connection.execute(
                    "SELECT scope_key,checkpoint FROM reporting_projection_legacy_checkpoints"
                    " WHERE account_id=%s AND consumer_id=%s ORDER BY scope_key",
                    (account_id, boundary.caller.consumer_id),
                )
            ).fetchall()
            previous = {r[0]: decode_checkpoint(r[1]) for r in previous_rows}
            baseline_rows = await (
                await connection.execute(
                    "SELECT scope_key,checkpoint FROM reporting_projection_legacy_baselines"
                    " WHERE account_id=%s ORDER BY scope_key",
                    (account_id,),
                )
            ).fetchall()
            baselines = {r[0]: decode_checkpoint(r[1]) for r in baseline_rows}
            steps = project_boundary(
                boundary,
                previous,
                baselines=baselines,
                source_sequence=floor - through + boundary.account_sequence,
                escalation=self.escalation,
                consumer_status_enabled=self.consumer_status_enabled,
            )
            raw = json.dumps(row[1])
            await connection.execute(
                "INSERT INTO reporting_projection_legacy_inputs VALUES"
                " (%s,%s,%s,%s,%s::jsonb,reporting_receipt_ingestion_sha256(%s::jsonb))",
                (
                    account_id,
                    boundary.account_sequence,
                    boundary.caller.consumer_id,
                    row[0],
                    raw,
                    raw,
                ),
            )
            await self._lock_scopes_on(connection, boundary.core)
            for step in steps:
                key = checkpoint_key(step.checkpoint)
                document = json.dumps(step.document())
                await connection.execute(
                    "INSERT INTO reporting_projection_legacy_steps VALUES"
                    " (%s,%s,%s,%s::jsonb,reporting_receipt_ingestion_sha256(%s::jsonb))",
                    (account_id, boundary.account_sequence, key, document, document),
                )
                checkpoint = json.dumps(checkpoint_document(step.checkpoint))
                await connection.execute(
                    "INSERT INTO reporting_projection_legacy_checkpoints VALUES"
                    " (%s,%s,%s,%s::jsonb,reporting_receipt_ingestion_sha256(%s::jsonb))"
                    " ON CONFLICT(account_id,scope_key) DO UPDATE SET"
                    " checkpoint=EXCLUDED.checkpoint,content_sha256=EXCLUDED.content_sha256",
                    (account_id, boundary.caller.consumer_id, key, checkpoint, checkpoint),
                )
                # Advance the original guarded checkpoint once per retained
                # boundary. Only the immutable epoch-zero journal receives the
                # event; activation never releases it into an active queue.
                await self._write_on(connection, step.checkpoint)
            await connection.execute(
                "UPDATE reporting_projection_accounts SET legacy_capture_cursor=%s"
                " WHERE account_id=%s",
                (boundary.account_sequence, account_id),
            )
            return StatusTurn(True)
        row = await (
            await connection.execute(
                "SELECT input FROM reporting_projection_inputs WHERE account_id=%s AND sequence=1",
                (account_id,),
            )
        ).fetchone()
        value = decode_projection_input(row[0])
        await self._lock_scopes_on(connection, value.core)
        await self._apply_value_on(connection, value, through=floor + 1, silent=True)
        await connection.execute(
            "UPDATE reporting_projection_accounts SET"
            " cursor=1,current_input=%s::jsonb,current_as_of=%s WHERE account_id=%s",
            (value.document.decode(), value.core.as_of, account_id),
        )
        return StatusTurn(True)

    async def baseline(self, *, account_id: str) -> bool:
        return await self.activate(account_id=account_id)

    async def _apply_value_on(
        self,
        connection: Any,
        value: ReportingProjectionInput,
        *,
        through: int,
        silent: bool = False,
    ) -> int:
        return await self._apply_on(
            connection,
            settled_replay(value.core),
            through=through,
            reconciliation=value.reconciliation,
            consumer_status_enabled=self.consumer_status_enabled,
            enqueue=self.ledger._notifications_enabled and not silent,
        )

    async def _project_version_on(self, connection: Any, account_id: str) -> StatusTurn:
        state = await self._state_on(connection, account_id)
        if state[4] is None:
            return await self._activation_step_on(connection, account_id)
        cursor, floor = int(state[1]), int(state[3])
        row = await (
            await connection.execute(
                "SELECT sequence,input,content_sha256=reporting_receipt_ingestion_sha256(input)"
                " FROM reporting_projection_inputs WHERE account_id=%s AND sequence>%s"
                " ORDER BY sequence LIMIT 1",
                (account_id, cursor),
            )
        ).fetchone()
        next_value = None
        if row is not None:
            if row[0] != cursor + 1 or not row[2]:
                raise ReportingNotificationError("status_projection_history_corrupt")
            next_value = decode_projection_input(row[1])
        now = await database_now(connection, self.ledger._clock)
        deadline = await (
            await connection.execute(
                "SELECT min(next_due_at) FROM reporting_status_scope_checkpoints"
                " WHERE account_id=%s",
                (account_id,),
            )
        ).fetchone()
        due = deadline[0] if deadline else None
        if due is not None and due <= now and (next_value is None or due < next_value.core.as_of):
            value = decode_projection_input(state[4]).at(due)
            if due < state[5]:
                raise ReportingNotificationError("status_projection_clock_regressed")
        elif next_value is not None:
            value, cursor = next_value, int(row[0])
            if value.core.as_of < state[5]:
                raise ReportingNotificationError("status_projection_clock_regressed")
        else:
            return StatusTurn(False)
        prior = decode_projection_input(state[4])
        value = with_projection_core(
            value, settled_replay(with_replay_lifecycles(value.core, prior.core))
        )
        await persist_replay_lifecycles_on(self.ledger, connection, value.core)
        count = await self._apply_value_on(connection, value, through=floor + cursor)
        await connection.execute(
            "UPDATE reporting_projection_accounts SET"
            " cursor=%s,current_input=%s::jsonb,current_as_of=%s"
            " WHERE account_id=%s",
            (cursor, value.document.decode(), value.core.as_of, account_id),
        )
        return StatusTurn(True, count)

    async def project_one(self, *, account_id: str) -> StatusTurn:
        async with self._transaction(account_id) as connection:
            return await self._project_version_on(connection, account_id)

    async def rebuild_one(self) -> StatusTurn:
        async with self.ledger._connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT account_id FROM reporting_projection_accounts WHERE cursor<max_sequence"
                    " AND policy=%s::jsonb ORDER BY account_id LIMIT 1",
                    (json.dumps(self.policy),),
                )
            ).fetchone()
        return await self.project_one(account_id=row[0]) if row else StatusTurn(False)

    async def complete_due(self, lease: StatusDueLease) -> StatusTurn:
        async with self._transaction(lease.scope.account_id) as connection:
            await self._state_on(connection, lease.scope.account_id)
            if not await self._held_on(connection, lease):
                return StatusTurn(False)
            result = await self._project_version_on(connection, lease.scope.account_id)
            if not await self._ack_on(connection, lease):
                raise ReportingNotificationError("status_lease_lost")
            return result

    async def sweep_one(self) -> StatusTurn:
        async with self.ledger._connection() as connection:
            at = await database_now(connection, self.ledger._clock)
            row = await (
                await connection.execute(
                    "SELECT c.account_id FROM reporting_status_scope_checkpoints c"
                    " JOIN reporting_projection_accounts a ON a.account_id=c.account_id"
                    " WHERE c.next_due_at<=%s AND a.policy=%s::jsonb"
                    " AND (c.lease_expires_at IS NULL OR c.lease_expires_at<=%s)"
                    " ORDER BY c.next_due_at,c.account_id LIMIT 1",
                    (at, json.dumps(self.policy), at),
                )
            ).fetchone()
        if row is None:
            return StatusTurn(False)
        lease = await self.claim_due(account_id=row[0])
        return await self.complete_due(lease) if lease is not None else StatusTurn(False)
