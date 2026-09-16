"""Account-scoped PostgreSQL outbox; HTTP never holds a database transaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, astuple, fields
from datetime import datetime, timedelta
from secrets import token_hex
from typing import TYPE_CHECKING, Any

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import aware_utc
from adcp.reporting.ledger.notification_models import (
    DirtyReason,
    ReportingDomainEvent,
    ReportingNotificationError,
    ReportingStatusDirty,
    ReportingStatusEvidence,
    ReportingStatusScope,
    decode_dirty,
    decode_event,
    dirty_storage,
    event_storage,
)
from adcp.reporting.outbox.memory import validate_finish
from adcp.reporting.outbox.models import (
    DeliveryBinding,
    DeliveryLease,
    DeliveryStatus,
    ErrorCode,
    ExpansionLease,
    StoredDelivery,
    WorkState,
)

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_BINDING_NAMES = tuple(item.name for item in fields(DeliveryBinding))
_BINDING_COLUMNS = ", ".join(_BINDING_NAMES)
_DELIVERY_COLUMNS = _BINDING_COLUMNS + ", envelope"
_BINDING_SIZE = len(_BINDING_NAMES)


async def database_now(connection: Any, clock: Callable[[], datetime] | None) -> datetime:
    """DB time in production; a deliberately injected clock for conformance."""
    if clock is not None:
        return aware_utc(clock())
    row = await (await connection.execute("SELECT clock_timestamp()")).fetchone()
    assert row is not None
    at: datetime = row[0]
    return at


async def enqueue_event(connection: Any, event: ReportingDomainEvent) -> None:
    """Private transaction participant. Never acquire another connection here."""
    consumer = event.consumer_namespace
    inserted = await (
        await connection.execute(
            "INSERT INTO reporting_notification_events"
            " (account_id, notification_id, notification_type, cause_kind, cause_id,"
            " cause_generation, consumer_namespace, fired_at, snapshot)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
            " ON CONFLICT (account_id, consumer_namespace, notification_type,"
            " cause_kind, cause_id, cause_generation) DO NOTHING RETURNING notification_id",
            (
                event.account_id,
                event.notification_id,
                event.notification_type,
                event.cause.kind,
                event.cause_id,
                event.cause_generation,
                consumer,
                event.fired_at,
                json.dumps(event_storage(event)),
            ),
        )
    ).fetchone()
    if inserted is None:
        row = await (
            await connection.execute(
                "SELECT snapshot FROM reporting_notification_events WHERE account_id = %s"
                " AND consumer_namespace = %s AND notification_type = %s AND cause_kind = %s"
                " AND cause_id = %s AND cause_generation = %s",
                (
                    event.account_id,
                    consumer,
                    event.notification_type,
                    event.cause.kind,
                    event.cause_id,
                    event.cause_generation,
                ),
            )
        ).fetchone()
        if row is None or decode_event(row[0]).cause != event.cause:
            raise ReportingNotificationError("event_identity_conflict")
        return
    await connection.execute(
        "INSERT INTO reporting_notification_expansions"
        " (account_id, consumer_namespace, notification_id, emission_generation, due_at)"
        " VALUES (%s,%s,%s,1,%s)",
        (event.account_id, consumer, event.notification_id, event.fired_at),
    )


async def mark_dirty(
    connection: Any,
    scope: ReportingStatusScope,
    reason: DirtyReason,
    at: datetime,
    before: ReportingStatusEvidence | None = None,
    after: ReportingStatusEvidence | None = None,
) -> None:
    from adcp.reporting.ledger.pg import PgReportingLedgerStore

    await PgReportingLedgerStore._lock_account(connection, scope.account_id)
    scope_hash = hashlib.sha256(canonical_json_utf8_v1(asdict(scope))).hexdigest()
    evidence = after or before
    cause_id = evidence.record_id if evidence is not None else scope_hash
    consumer = scope.consumer_id or ""
    row = await (
        await connection.execute(
            "SELECT COALESCE(max(cause_generation), 0) + 1 FROM reporting_status_dirty"
            " WHERE account_id = %s AND consumer_namespace = %s AND scope_sha256 = %s"
            " AND reason = %s AND cause_id = %s",
            (scope.account_id, consumer, scope_hash, reason, cause_id),
        )
    ).fetchone()
    assert row is not None
    generation = row[0]
    row = await (
        await connection.execute(
            "INSERT INTO reporting_status_dirty_heads (account_id, max_sequence) VALUES (%s,1)"
            " ON CONFLICT (account_id) DO UPDATE SET max_sequence ="
            " reporting_status_dirty_heads.max_sequence + 1 RETURNING max_sequence",
            (scope.account_id,),
        )
    ).fetchone()
    assert row is not None
    record = ReportingStatusDirty(row[0], scope, reason, at, cause_id, generation, before, after)
    await connection.execute(
        "INSERT INTO reporting_status_dirty (account_id, sequence, consumer_namespace,"
        " scope_sha256, reason, cause_id, cause_generation, snapshot)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
        (
            scope.account_id,
            record.sequence,
            consumer,
            scope_hash,
            reason,
            cause_id,
            generation,
            json.dumps(dirty_storage(record)),
        ),
    )


def _delivery(row: Any) -> StoredDelivery:
    return StoredDelivery(DeliveryBinding(*row[:_BINDING_SIZE]), bytes(row[_BINDING_SIZE]))


class _LostLeaseError(Exception):
    pass


class PgReportingOutbox:
    """Caller-owned pool, database time, expiring random tokens, fenced writes.

    ``now`` arguments implement the shared memory protocol. In PostgreSQL they
    never override the database clock; only the explicit constructor ``clock``
    seam does, for deterministic tests. Retry *durations* are applied to DB time.
    Claims are counted for diagnostics, never as an HTTP retry limit. Evidence
    and prepared bindings are retained indefinitely; this slice has no purge.
    """

    def __init__(
        self, *, pool: AsyncConnectionPool, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._pool, self._clock = pool, clock

    async def create_schema(self) -> None:
        from adcp.reporting.ledger.pg import PgReportingLedgerStore

        await PgReportingLedgerStore(pool=self._pool, notifications=True).create_schema()

    async def list_events(self, *, account_id: str) -> tuple[ReportingDomainEvent, ...]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT snapshot FROM reporting_notification_events WHERE account_id = %s"
                    " ORDER BY fired_at, notification_id",
                    (account_id,),
                )
            ).fetchall()
        events = tuple(decode_event(row[0]) for row in rows)
        if any(event.account_id != account_id for event in events):
            raise ReportingNotificationError("invalid_event")
        return events

    async def claim_expansion(
        self, *, account_id: str, now: datetime, lease_seconds: float
    ) -> ExpansionLease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self._pool.connection() as conn, conn.transaction():
            at = await database_now(conn, self._clock)
            row = await (
                await conn.execute(
                    "SELECT x.notification_id, x.emission_generation, x.claim_count, e.snapshot,"
                    " x.consumer_namespace"
                    " FROM reporting_notification_expansions x JOIN reporting_notification_events e"
                    " ON e.account_id = x.account_id AND e.notification_id = x.notification_id"
                    " AND e.consumer_namespace = x.consumer_namespace"
                    " WHERE x.account_id = %s AND x.due_at <= %s AND (x.state = 'pending' OR"
                    " (x.state = 'leased' AND x.lease_expires_at <= %s))"
                    " ORDER BY x.due_at, x.notification_id, x.emission_generation"
                    " FOR UPDATE OF x SKIP LOCKED LIMIT 1",
                    (account_id, at, at),
                )
            ).fetchone()
            if row is None:
                return None
            token, expires = token_hex(32), at + timedelta(seconds=lease_seconds)
            await conn.execute(
                "UPDATE reporting_notification_expansions SET state = 'leased', lease_token = %s,"
                " lease_expires_at = %s, claim_count = claim_count + 1"
                " WHERE account_id = %s AND consumer_namespace = %s"
                " AND notification_id = %s AND emission_generation = %s",
                (token, expires, account_id, row[4], row[0], row[1]),
            )
            return ExpansionLease(
                account_id, row[0], row[1], token, expires, row[2] + 1, row[3], row[4]
            )

    async def complete_expansion(
        self, lease: ExpansionLease, deliveries: tuple[StoredDelivery, ...], *, now: datetime
    ) -> bool:
        try:
            async with self._pool.connection() as conn, conn.transaction():
                at = await database_now(conn, self._clock)
                row = await (
                    await conn.execute(
                        "SELECT 1 FROM reporting_notification_expansions WHERE account_id = %s"
                        " AND consumer_namespace = %s"
                        " AND notification_id = %s AND emission_generation = %s"
                        " AND state = 'leased'"
                        " AND lease_token = %s AND lease_expires_at > %s FOR UPDATE",
                        (
                            lease.account_id,
                            lease.consumer_namespace,
                            lease.notification_id,
                            lease.emission_generation,
                            lease.token,
                            at,
                        ),
                    )
                ).fetchone()
                if row is None:
                    return False
                for delivery in deliveries:
                    binding = delivery.binding
                    if (
                        binding.account_id != lease.account_id
                        or binding.notification_id != lease.notification_id
                        or binding.emission_generation != lease.emission_generation
                        or binding.consumer_namespace != lease.consumer_namespace
                    ):
                        raise ReportingNotificationError("invalid_configuration")
                    await self._insert_delivery(conn, delivery, at)
                if not await self._finish_expansion(
                    conn, lease, state="complete", error_code=None, delay=0
                ):
                    # Do not commit N subscriber rows if the final fence failed.
                    raise _LostLeaseError
            return True
        except _LostLeaseError:
            return False

    async def _insert_delivery(self, conn: Any, delivery: StoredDelivery, at: datetime) -> None:
        placeholders = ",".join(["%s"] * (_BINDING_SIZE + 2))
        await conn.execute(
            f"INSERT INTO reporting_notification_deliveries ({_DELIVERY_COLUMNS}, due_at)"  # nosec B608
            f" VALUES ({placeholders}) ON CONFLICT"  # nosec B608
            " (account_id, consumer_namespace, notification_id, emission_generation, subscriber_id)"
            " DO NOTHING",
            (*astuple(delivery.binding), delivery.envelope, at),
        )

    async def _finish_expansion(
        self,
        conn: Any,
        lease: ExpansionLease,
        *,
        state: WorkState,
        error_code: ErrorCode | None,
        delay: float,
    ) -> bool:
        at = await database_now(conn, self._clock)
        cursor = await conn.execute(
            "UPDATE reporting_notification_expansions SET state = %s, error_code = %s,"
            " due_at = %s, lease_token = NULL, lease_expires_at = NULL"
            " WHERE account_id = %s AND notification_id = %s AND emission_generation = %s"
            " AND consumer_namespace = %s"
            " AND state = 'leased' AND lease_token = %s AND lease_expires_at > %s",
            (
                state,
                error_code,
                at + timedelta(seconds=delay),
                lease.account_id,
                lease.notification_id,
                lease.emission_generation,
                lease.consumer_namespace,
                lease.token,
                at,
            ),
        )
        return bool(cursor.rowcount)

    async def finish_expansion(
        self,
        lease: ExpansionLease,
        *,
        now: datetime,
        state: WorkState,
        error_code: ErrorCode | None = None,
        retry_at: datetime | None = None,
    ) -> bool:
        validate_finish(state, error_code)
        delay = max(0.0, (retry_at - now).total_seconds()) if retry_at is not None else 0.0
        async with self._pool.connection() as conn, conn.transaction():
            return await self._finish_expansion(
                conn, lease, state=state, error_code=error_code, delay=delay
            )

    async def reemit(self, *, account_id: str, notification_id: str, now: datetime) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT consumer_namespace FROM reporting_notification_events"
                    " WHERE account_id = %s"
                    " AND notification_id = %s FOR UPDATE",
                    (account_id, notification_id),
                )
            ).fetchone()
            if row is None:
                raise ReportingNotificationError("event_unavailable")
            consumer = row[0]
            row = await (
                await conn.execute(
                    "SELECT max(emission_generation) + 1 FROM reporting_notification_expansions"
                    " WHERE account_id = %s AND notification_id = %s",
                    (account_id, notification_id),
                )
            ).fetchone()
            assert row is not None
            generation = int(row[0])
            await conn.execute(
                "INSERT INTO reporting_notification_expansions"
                " (account_id, consumer_namespace, notification_id, emission_generation, due_at)"
                " VALUES (%s,%s,%s,%s,%s)",
                (
                    account_id,
                    consumer,
                    notification_id,
                    generation,
                    await database_now(conn, self._clock),
                ),
            )
            return generation

    async def claim_delivery(
        self, *, account_id: str, now: datetime, lease_seconds: float
    ) -> DeliveryLease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self._pool.connection() as conn, conn.transaction():
            at = await database_now(conn, self._clock)
            row = await (
                await conn.execute(
                    f"SELECT {_DELIVERY_COLUMNS}, claim_count"  # nosec B608
                    " FROM reporting_notification_deliveries"
                    " WHERE account_id = %s AND due_at <= %s AND (state = 'pending' OR"
                    " (state = 'leased' AND lease_expires_at <= %s)) ORDER BY due_at, delivery_id"
                    " FOR UPDATE SKIP LOCKED LIMIT 1",
                    (account_id, at, at),
                )
            ).fetchone()
            if row is None:
                return None
            token, expires = token_hex(32), at + timedelta(seconds=lease_seconds)
            await conn.execute(
                "UPDATE reporting_notification_deliveries SET state = 'leased', lease_token = %s,"
                " lease_expires_at = %s, claim_count = claim_count + 1"
                " WHERE account_id = %s AND consumer_namespace = %s AND delivery_id = %s",
                (
                    token,
                    expires,
                    account_id,
                    row[_BINDING_NAMES.index("consumer_namespace")],
                    row[1],
                ),
            )
            return DeliveryLease(_delivery(row), token, expires, row[-1] + 1)

    async def delivery_lease_current(self, lease: DeliveryLease, *, now: datetime) -> bool:
        binding = lease.delivery.binding
        async with self._pool.connection() as conn:
            at = await database_now(conn, self._clock)
            row = await (
                await conn.execute(
                    "SELECT 1 FROM reporting_notification_deliveries WHERE account_id = %s"
                    " AND consumer_namespace = %s"
                    " AND delivery_id = %s AND state = 'leased' AND lease_token = %s"
                    " AND lease_expires_at > %s",
                    (
                        binding.account_id,
                        binding.consumer_namespace,
                        binding.delivery_id,
                        lease.token,
                        at,
                    ),
                )
            ).fetchone()
        return row is not None

    async def finish_delivery(
        self,
        lease: DeliveryLease,
        *,
        now: datetime,
        state: WorkState,
        error_code: ErrorCode | None = None,
        retry_at: datetime | None = None,
    ) -> bool:
        validate_finish(state, error_code)
        delay = max(0.0, (retry_at - now).total_seconds()) if retry_at is not None else 0.0
        binding = lease.delivery.binding
        async with self._pool.connection() as conn, conn.transaction():
            at = await database_now(conn, self._clock)
            cursor = await conn.execute(
                "UPDATE reporting_notification_deliveries SET state = %s, error_code = %s,"
                " due_at = %s, lease_token = NULL, lease_expires_at = NULL"
                " WHERE account_id = %s AND delivery_id = %s AND state = 'leased'"
                " AND consumer_namespace = %s"
                " AND lease_token = %s AND lease_expires_at > %s",
                (
                    state,
                    error_code,
                    at + timedelta(seconds=delay),
                    binding.account_id,
                    binding.delivery_id,
                    binding.consumer_namespace,
                    lease.token,
                    at,
                ),
            )
            return bool(cursor.rowcount)

    async def list_deliveries(self, *, account_id: str) -> tuple[DeliveryStatus, ...]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    f"SELECT {_DELIVERY_COLUMNS}, state, claim_count, due_at, error_code"  # nosec B608
                    " FROM reporting_notification_deliveries WHERE account_id = %s"
                    " ORDER BY due_at, delivery_id",
                    (account_id,),
                )
            ).fetchall()
        return tuple(DeliveryStatus(_delivery(row), *row[-4:]) for row in rows)

    async def mark_status_dirty(
        self, scope: ReportingStatusScope, *, reason: DirtyReason, now: datetime
    ) -> None:
        """Additive clock-sweep handoff, not a scheduler or a status projector."""
        async with self._pool.connection() as conn, conn.transaction():
            await mark_dirty(conn, scope, reason, await database_now(conn, self._clock))

    async def read_status_dirty(
        self, *, account_id: str, after: int = 0, limit: int = 100
    ) -> tuple[ReportingStatusDirty, ...]:
        if not 1 <= limit <= 1000 or after < 0:
            raise ValueError("invalid dirty checkpoint window")
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT snapshot FROM reporting_status_dirty WHERE account_id = %s"
                    " AND sequence > %s ORDER BY sequence LIMIT %s",
                    (account_id, after, limit),
                )
            ).fetchall()
        records = tuple(decode_dirty(row[0]) for row in rows)
        if any(record.scope.account_id != account_id for record in records):
            raise ReportingNotificationError("invalid_status_evidence")
        return records

    async def status_checkpoint(self, *, account_id: str, projector_id: str) -> int:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT sequence FROM reporting_status_checkpoints WHERE account_id = %s"
                    " AND projector_id = %s",
                    (account_id, projector_id),
                )
            ).fetchone()
        return int(row[0]) if row is not None else 0

    async def advance_status_checkpoint(
        self, *, account_id: str, projector_id: str, expected: int, through: int
    ) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT max_sequence FROM reporting_status_dirty_heads WHERE account_id = %s",
                    (account_id,),
                )
            ).fetchone()
            maximum = int(row[0]) if row is not None else 0
            if not 0 <= expected <= through <= maximum:
                raise ValueError("invalid status checkpoint")
            await conn.execute(
                "INSERT INTO reporting_status_checkpoints (account_id, projector_id, sequence)"
                " VALUES (%s,%s,0) ON CONFLICT (account_id, projector_id) DO NOTHING",
                (account_id, projector_id),
            )
            cursor = await conn.execute(
                "UPDATE reporting_status_checkpoints SET sequence = %s WHERE account_id = %s"
                " AND projector_id = %s AND sequence = %s",
                (through, account_id, projector_id, expected),
            )
            return bool(cursor.rowcount)
