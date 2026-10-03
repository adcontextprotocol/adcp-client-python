"""Explicit PostgreSQL reservations with business/replay transaction ownership."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from adcp.exceptions import IdempotencyConflictError

if TYPE_CHECKING:
    from psycopg import AsyncConnection


class _CachedEntry(Protocol):
    @property
    def payload_hash(self) -> str: ...

    @property
    def response(self) -> dict[str, Any]: ...


class _ReservationBackend(Protocol):
    _pool: Any
    _table: str
    _sql_now: str
    _sql_put_if_absent: str
    _active_connection: ContextVar[tuple[Any, asyncio.Task[Any] | None] | None]

    def hold(self, scope_key: str, key: str) -> AbstractAsyncContextManager[None]: ...

    async def _get_on_connection(
        self, conn: Any, scope_key: str, key: str
    ) -> _CachedEntry | None: ...


class IdempotencyReservationError(RuntimeError):
    """The reservation lifecycle cannot safely commit a replayable operation."""


class PgReservation:
    """A task-owned reservation yielded by ``PgBackend.reserve``.

    On a miss, execute business SQL on :attr:`connection` and call :meth:`record`
    exactly once before leaving the context. Successful context exit commits
    both writes before unlocking. On a hit, :attr:`response` is a deep copied
    replay envelope and business SQL is forbidden. Do not commit, roll back, or
    issue transaction-control SQL yourself; the reservation owns the transaction.
    """

    def __init__(
        self,
        backend: _ReservationBackend,
        connection: AsyncConnection[Any],
        scope_key: str,
        key: str,
        payload_hash: str,
        ttl_seconds: int,
        response: dict[str, Any] | None,
        replay_table_oid: int,
    ) -> None:
        self._backend = backend
        self._connection = connection
        self._scope_key = scope_key
        self._key = key
        self._payload_hash = payload_hash
        self._ttl_seconds = ttl_seconds
        self._replay_table_oid = replay_table_oid
        self._response = copy.deepcopy(response)
        self._replayed = response is not None
        self._recorded = False
        self._failed = False
        self._active = True
        self._owner = asyncio.current_task()

    @property
    def replayed(self) -> bool:
        """Whether this reservation found a committed matching replay."""
        return self._replayed

    @property
    def recorded(self) -> bool:
        """Whether ``record`` wrote a response (commit occurs on context exit)."""
        return self._recorded

    @property
    def response(self) -> dict[str, Any] | None:
        """Return an independent response copy, adding ``replayed`` on a hit."""
        response = copy.deepcopy(self._response)
        if response is not None and self._replayed:
            response["replayed"] = True
        return response

    def _check_active(self) -> None:
        if not self._active or asyncio.current_task() is not self._owner:
            raise IdempotencyReservationError("Reservation must be used by its active owning task")

    @property
    def connection(self) -> AsyncConnection[Any]:
        """The business connection; available only on an active cache miss."""
        self._check_active()
        if self._replayed:
            raise IdempotencyReservationError("A replay reservation cannot execute business writes")
        return self._connection

    async def record(self, response: dict[str, Any]) -> None:
        """Write a JSON response in the open business transaction, without committing.

        Errors propagate and poison the reservation even if caught by the handler.
        The response must remain present through transaction exit; rolling it back
        in a nested savepoint makes the entire reservation fail closed.
        """
        self._check_active()
        if self._replayed or self._recorded or self._failed:
            raise IdempotencyReservationError("Only a fresh reservation can record once")
        try:
            await self._check_replay_table()
            response_copy = copy.deepcopy(response)
            encoded = json.dumps(response_copy, allow_nan=False)
            response_copy = json.loads(encoded)
            cursor = await self._connection.execute(self._backend._sql_now)
            row = await cursor.fetchone()
            assert row is not None
            expires_at = datetime.fromtimestamp(float(row[0]) + self._ttl_seconds, timezone.utc)
            cursor = await self._connection.execute(
                self._backend._sql_put_if_absent,
                (self._scope_key, self._key, self._payload_hash, encoded, expires_at),
            )
            if await cursor.fetchone() is None:
                raise IdempotencyReservationError("Replay slot changed during reservation")
            self._response = response_copy
            self._recorded = True
        except BaseException:
            self._failed = True
            raise

    async def _check_replay_table(self) -> None:
        cursor = await self._connection.execute(
            "SELECT to_regclass(%s)::oid", (self._backend._table,)
        )
        row = await cursor.fetchone()
        if row is None or row[0] != self._replay_table_oid:
            raise IdempotencyReservationError("Reservation replay table changed during transaction")

    async def _verify_record(self) -> None:
        if self._failed or not self._recorded:
            raise IdempotencyReservationError("Reservation exited without a successful record")
        await self._check_replay_table()
        cached = await self._backend._get_on_connection(
            self._connection, self._scope_key, self._key
        )
        if (
            cached is None
            or cached.payload_hash != self._payload_hash
            or cached.response != self._response
        ):
            raise IdempotencyReservationError("Recorded response was rolled back or changed")


@asynccontextmanager
async def reserve_transaction(
    backend: _ReservationBackend,
    scope_key: str,
    key: str,
    payload_hash: str,
    *,
    ttl_seconds: int,
    connection: AsyncConnection[Any] | None,
    operation: str,
) -> AsyncIterator[PgReservation]:
    # Optional dependency stays optional for users of MemoryBackend.
    from psycopg import AsyncConnection, Rollback
    from psycopg.pq import TransactionStatus

    if not scope_key or not key or not payload_hash:
        raise ValueError("Reservation requires nonempty scope, key, and payload hash")
    if not 3600 <= ttl_seconds <= 604800:
        raise ValueError("ttl_seconds must be in [3600, 604800]")
    active = backend._active_connection.get()
    if active is not None and active[1] is asyncio.current_task():
        raise IdempotencyReservationError("reserve cannot nest inside hold or @store.wrap")
    if connection is not None:
        if not isinstance(connection, AsyncConnection):
            raise TypeError("connection must be a psycopg.AsyncConnection")
        if connection.info.transaction_status != TransactionStatus.IDLE:
            raise IdempotencyReservationError("An existing outer transaction is not supported")

    @asynccontextmanager
    async def business_connection() -> AsyncIterator[AsyncConnection[Any]]:
        if connection is not None:
            yield connection
        else:
            async with backend._pool.connection() as conn:
                yield conn

    @asynccontextmanager
    async def business_transaction(conn: AsyncConnection[Any]) -> AsyncIterator[None]:
        try:
            async with conn.transaction():
                yield
        except BaseException:
            # Transaction __aexit__ can itself fail/cancel during COMMIT. Resolve
            # or close the business session before releasing the execution lock,
            # including for a caller-owned connection not returned to our pool.
            if not conn.closed:
                try:
                    await conn.rollback()
                except BaseException:
                    await conn.close()
            raise

    # Lexical nesting proves commit-before-unlock: hold encloses the *whole*
    # business transaction, including its __aexit__ and any failed commit.
    async with backend.hold(scope_key, key):
        async with business_connection() as conn:
            if not isinstance(conn, AsyncConnection):
                raise TypeError("Business pool must yield a psycopg.AsyncConnection")
            if conn.info.transaction_status != TransactionStatus.IDLE:
                raise IdempotencyReservationError("An existing outer transaction is not supported")
            async with business_transaction(conn):
                # Take a fresh snapshot after obtaining the execution lock even
                # when the caller configured a different connection isolation.
                await conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                fingerprint_sql = (
                    "SELECT current_database(), pg_postmaster_start_time(), to_regclass(%s)::oid"
                )
                lock_connection = backend._active_connection.get()
                assert lock_connection is not None
                lock_cursor = await lock_connection[0].execute(fingerprint_sql, (backend._table,))
                business_cursor = await conn.execute(fingerprint_sql, (backend._table,))
                lock_identity = await lock_cursor.fetchone()
                business_identity = await business_cursor.fetchone()
                if (
                    lock_identity is None
                    or lock_identity[2] is None
                    or lock_identity != business_identity
                ):
                    raise IdempotencyReservationError(
                        "Business and lock connections must use the same "
                        "PostgreSQL database and replay table"
                    )
                cached = await backend._get_on_connection(conn, scope_key, key)
                if cached is not None and cached.payload_hash != payload_hash:
                    raise IdempotencyConflictError(
                        operation=operation,
                        errors=[
                            {
                                "code": "IDEMPOTENCY_CONFLICT",
                                "message": "idempotency_key reused with a different payload",
                            }
                        ],
                    )
                if cached is not None:
                    await conn.execute("SET TRANSACTION READ ONLY")
                slot = PgReservation(
                    backend,
                    conn,
                    scope_key,
                    key,
                    payload_hash,
                    ttl_seconds,
                    cached.response if cached is not None else None,
                    lock_identity[2],
                )
                try:
                    yield slot
                    if not slot.replayed:
                        await slot._verify_record()
                except Rollback as exc:
                    raise IdempotencyReservationError(
                        "Reservation was explicitly rolled back"
                    ) from exc
                finally:
                    slot._active = False
