"""PostgreSQL-backed :class:`~adcp.decisioning.TaskRegistry` implementation.

Durable counterpart to :class:`~adcp.decisioning.InMemoryTaskRegistry`:
task state survives process restarts and is safe for multi-worker deployments
sharing a single Postgres database.

The caller supplies an :class:`psycopg_pool.AsyncConnectionPool`. We don't
open, own, or close the pool — adopters typically share an existing pool with
their main application database.

Quickstart
----------

::

    import asyncio
    from psycopg_pool import AsyncConnectionPool
    from adcp.decisioning import PgTaskRegistry, serve
    from myapp import MyPlatform

    async def main():
        async with AsyncConnectionPool(
            "postgresql://user:pass@localhost/mydb",
            min_size=2,
            max_size=10,
        ) as pool:
            registry = PgTaskRegistry(pool=pool)
            await registry.create_schema()  # idempotent; safe on every boot
            serve(MyPlatform(), registry=registry)

    asyncio.run(main())

Schema bootstrap
----------------

Call :meth:`create_schema` once per deployment (or every boot — it is
idempotent via ``CREATE TABLE IF NOT EXISTS``). The equivalent raw DDL
ships at :file:`src/adcp/decisioning/pg/decisioning_tasks.sql` for adopters
using a migration tool (Alembic, Flyway, psql).

Cross-tenant safety
-------------------

:meth:`get` enforces account isolation at the SQL level —
``WHERE account_id = %s`` is part of the query predicate, not a Python-level
filter. A mis-matched ``expected_account_id`` returns ``None`` without
materializing the row.

Multi-worker concurrency
------------------------

Terminal-state transitions (:meth:`complete`, :meth:`fail`) use an atomic
``UPDATE ... WHERE state NOT IN ('completed', 'failed') RETURNING task_id``
pattern. If the UPDATE lands zero rows, a follow-up SELECT determines whether
the task is unknown or already terminal, enabling correct idempotency
behavior across workers without optimistic-lock retries.

:meth:`update_progress` similarly uses a conditional UPDATE that silently
no-ops on terminal rows, so a straggler progress write can never resurrect a
completed task.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, ClassVar

from adcp.decisioning.account_projection import strip_credentials_from_wire_result

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox

try:
    from psycopg_pool import AsyncConnectionPool as _AsyncConnectionPool  # noqa: F401

    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

_INSTALL_HINT = (
    "PgTaskRegistry requires psycopg3 and psycopg-pool. "
    "Install the 'pg' extra: `pip install 'adcp[pg]'` "
    "(Poetry: `poetry add 'adcp[pg]'`)."
)

_DEFAULT_TABLE = "decisioning_tasks"

# ASCII-only identifier guard — same pattern as PgReplayStore._is_safe_identifier.
# The table name is static-formatted into SQL at construction so this guard is
# the only protection against SQL injection or Unicode homoglyph substitution.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PgTaskRegistry:
    """PostgreSQL-backed :class:`~adcp.decisioning.TaskRegistry` — v6.1.

    Durable counterpart to :class:`~adcp.decisioning.InMemoryTaskRegistry`.
    Set ``is_durable = True`` so the production-mode gate in
    :func:`adcp.decisioning.serve.create_adcp_server_from_platform` accepts it
    without requiring ``ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1``.

    Parameters
    ----------
    pool:
        An :class:`psycopg_pool.AsyncConnectionPool` owned by the caller.
        Each registry operation acquires a short-lived connection from the
        pool and returns it immediately after the query. No long-lived
        transactions, no cross-operation state.
    task_webhook_outbox:
        Optional :class:`PgTaskWebhookOutbox` sharing this pool. When a task
        carries callback registration, ``complete`` / ``fail`` enqueue its
        immutable terminal webhook on the same transaction connection.

    Notes
    -----
    Unlike :class:`~adcp.signing.PgReplayStore`, this class uses a fixed
    ``decisioning_tasks`` table name. Multi-tenant table-name isolation is not
    supported in this release — callers requiring strict schema separation
    should use separate databases or schemas.
    """

    is_durable: ClassVar[bool] = True

    def __init__(
        self,
        *,
        pool: AsyncConnectionPool,
        task_webhook_outbox: PgTaskWebhookOutbox | None = None,
        _table: str = _DEFAULT_TABLE,
    ) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        if not _SAFE_IDENTIFIER_RE.fullmatch(_table):
            raise ValueError(f"_table must match [a-z_][a-z0-9_]* (ASCII only), got {_table!r}")
        if task_webhook_outbox is not None and task_webhook_outbox._pool is not pool:
            raise ValueError(
                "PgTaskRegistry and PgTaskWebhookOutbox must use the same connection pool"
            )
        self._pool = pool
        self._table = _table
        self.task_webhook_outbox = task_webhook_outbox
        self.atomic_task_webhook_outbox = task_webhook_outbox is not None

        # Pre-format queries at construction so the hot path avoids f-strings per call.
        # _table is whitelisted by _SAFE_IDENTIFIER_RE above.
        self._sql_insert = (  # noqa: S608 — table name is whitelisted
            f"INSERT INTO {self._table}"
            f" (task_id, account_id, state, task_type, request_context,"
            f"  webhook_registration, webhook_registration_nonce, created_at, updated_at)"
            f" VALUES (%s, %s, 'submitted', %s, %s::jsonb, %s, %s, %s, %s)"
        )
        self._sql_update_progress = (  # noqa: S608
            f"UPDATE {self._table}"
            f" SET state = CASE state WHEN 'submitted' THEN 'working' ELSE state END,"
            f"     progress = %s::jsonb, updated_at = %s"
            f" WHERE task_id = %s AND state NOT IN ('completed', 'failed')"
        )
        self._sql_complete = (  # noqa: S608
            f"UPDATE {self._table}"
            f" SET state = 'completed', result = %s::jsonb, updated_at = %s"
            f" WHERE task_id = %s AND state NOT IN ('completed', 'failed')"
            f" RETURNING task_id, account_id, task_type, webhook_registration,"
            f" webhook_registration_nonce"
        )
        self._sql_fail = (  # noqa: S608
            f"UPDATE {self._table}"
            f" SET state = 'failed', error = %s::jsonb, updated_at = %s"
            f" WHERE task_id = %s AND state NOT IN ('completed', 'failed')"
            f" RETURNING task_id, account_id, task_type, webhook_registration,"
            f" webhook_registration_nonce"
        )
        self._sql_clear_webhook_registration = (  # noqa: S608
            f"UPDATE {self._table} SET webhook_registration = NULL,"
            f" webhook_registration_nonce = NULL WHERE task_id = %s"
        )
        # Explicit ``::text`` cast on the optional account-filter
        # parameter so psycopg's bind-param type inference doesn't
        # fail with ``IndeterminateDatatype: could not determine
        # data type of parameter $2``. Without the cast, the
        # ``%s IS NULL`` predicate gives psycopg no type context
        # for the parameter and the query fails at prepare time.
        self._sql_get = (  # noqa: S608
            f"SELECT task_id, account_id, state, task_type,"
            f"       progress, result, error, request_context, created_at, updated_at"
            f" FROM {self._table}"
            f" WHERE task_id = %s AND (%s::text IS NULL OR account_id = %s)"
        )
        self._sql_get_state_result = (  # noqa: S608
            f"SELECT state, result, task_type FROM {self._table} WHERE task_id = %s"
        )
        self._sql_get_task_type = (  # noqa: S608
            f"SELECT task_type FROM {self._table} WHERE task_id = %s"
        )
        self._sql_get_state_error = (  # noqa: S608
            f"SELECT state, error FROM {self._table} WHERE task_id = %s"
        )
        self._sql_discard = f"DELETE FROM {self._table} WHERE task_id = %s"  # noqa: S608
        self._sql_ddl = (  # noqa: S608
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            f'    task_id     TEXT             COLLATE "C" NOT NULL PRIMARY KEY,'
            f'    account_id  TEXT             COLLATE "C" NOT NULL,'
            f"    state       TEXT             NOT NULL DEFAULT 'submitted',"
            f"    task_type   TEXT             NOT NULL,"
            f"    progress    JSONB,"
            f"    result      JSONB,"
            f"    error       JSONB,"
            f"    request_context JSONB,"
            f"    webhook_registration BYTEA,"
            f"    webhook_registration_nonce BYTEA,"
            f"    created_at  DOUBLE PRECISION NOT NULL,"
            f"    updated_at  DOUBLE PRECISION NOT NULL"
            f");"
            f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS request_context JSONB;"
            f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS webhook_registration BYTEA;"
            f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS webhook_registration_nonce BYTEA;"
            f"CREATE INDEX IF NOT EXISTS {self._table}_account_idx"  # noqa: S608
            f"    ON {self._table} (account_id);"
        )

    # -- schema bootstrap -----------------------------------------------

    async def create_schema(self) -> None:
        """Create the task registry table and supporting index.

        Honors the ``_table`` kwarg the store was constructed with.
        Idempotent via ``CREATE TABLE IF NOT EXISTS`` — safe to call on every
        application boot. The equivalent raw DDL ships at
        ``adcp/decisioning/pg/decisioning_tasks.sql`` in the installed package
        for adopters using a migration tool (Alembic, Flyway, psql).
        """
        async with self._pool.connection() as conn:
            await conn.execute(self._sql_ddl)

    # -- TaskRegistry Protocol ------------------------------------------

    async def issue(
        self,
        *,
        account_id: str,
        task_type: str,
        request_context: dict[str, Any] | None = None,
        webhook_url: str | None = None,
        webhook_operation_id: str | None = None,
        webhook_token: str | None = None,
        **_extra: Any,
    ) -> str:
        """Allocate a task_id, persist a ``submitted`` row, return the id.

        Mirrors :meth:`~adcp.decisioning.InMemoryTaskRegistry.issue` including
        the account_id validation guard — empty or sentinel account_ids would
        allow cross-tenant task-id probing via the ``WHERE account_id = %s``
        predicate collapsing multiple tenants into one slot.
        """
        if not account_id or not account_id.strip() or account_id == "<unset>":
            raise ValueError(
                f"account_id must be a non-empty, non-default string; "
                f"got {account_id!r}. AccountStore.resolve must always "
                "return Account(id=<non-empty>) so cross-tenant cache "
                "scoping works correctly."
            )
        if (webhook_url is None) != (webhook_operation_id is None):
            raise ValueError("webhook_url and webhook_operation_id must be supplied together")
        if webhook_url is not None and not webhook_url:
            raise ValueError("webhook_url must be non-empty when supplied")
        if webhook_operation_id is not None and not webhook_operation_id:
            raise ValueError("webhook_operation_id must be non-empty when supplied")
        outbox = self.task_webhook_outbox
        if webhook_url is not None:
            if outbox is None:
                raise ValueError("webhook registration requires the registry's task_webhook_outbox")
            outbox.validate_registration(webhook_url)
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        encrypted_registration: bytes | None = None
        registration_nonce: bytes | None = None
        if webhook_url is not None:
            if webhook_operation_id is None or outbox is None:
                raise RuntimeError("validated webhook registration became incomplete")
            encrypted_registration, registration_nonce = outbox.protect_registration(
                account_id=account_id,
                task_id=task_id,
                task_type=task_type,
                url=webhook_url,
                operation_id=webhook_operation_id,
                token=webhook_token,
            )
        now = time.time()
        async with self._pool.connection() as conn:
            await conn.execute(
                self._sql_insert,
                (
                    task_id,
                    account_id,
                    task_type,
                    json.dumps(request_context) if request_context is not None else None,
                    encrypted_registration,
                    registration_nonce,
                    now,
                    now,
                ),
            )
        return task_id

    async def update_progress(
        self,
        task_id: str,
        progress: dict[str, Any],
    ) -> None:
        """Write a progress payload; transition ``submitted`` → ``working``.

        Silently no-ops when the task is already in a terminal state or
        unknown — the dispatch wrapper expects this method never to raise on
        transient conditions (see :class:`~adcp.decisioning.TaskRegistry`
        docstring).

        The ``state NOT IN ('completed', 'failed')`` predicate is evaluated
        server-side so a concurrent terminal write cannot be overwritten by a
        straggler progress event.
        """
        async with self._pool.connection() as conn:
            await conn.execute(
                self._sql_update_progress,
                (json.dumps(progress), time.time(), task_id),
            )
            # Zero rows updated means unknown task_id or terminal state — silent
            # no-op per Protocol contract. The InMemoryTaskRegistry logs a
            # WARNING on terminal-state drops; we omit the extra SELECT needed
            # to distinguish the two cases since the dispatch wrapper swallows
            # the result either way.

    async def complete(
        self,
        task_id: str,
        result: dict[str, Any],
    ) -> None:
        """Mark the task ``completed`` with ``result`` as the terminal artifact.

        Idempotent on repeated calls with an equal ``result``; raises
        :class:`ValueError` on conflicting re-completion.

        Uses an atomic ``UPDATE ... RETURNING`` so concurrent workers cannot
        race each other into double-completion without detection.
        """
        async with self._pool.connection() as conn:
            type_cursor = await conn.execute(self._sql_get_task_type, (task_id,))
            type_row = await type_cursor.fetchone()
            safe_result = (
                strip_credentials_from_wire_result(type_row[0], result)
                if type_row is not None
                else result
            )
            cur = await conn.execute(
                self._sql_complete,
                (json.dumps(safe_result), time.time(), task_id),
            )
            row = await cur.fetchone()
            if row is not None:
                await self._enqueue_terminal_if_registered(
                    conn,
                    row=row,
                    status="completed",
                    payload=safe_result,
                )
                return  # updated successfully

            # Zero rows in RETURNING — task is unknown or already terminal.
            cur2 = await conn.execute(self._sql_get_state_result, (task_id,))
            row = await cur2.fetchone()
            if row is None:
                raise ValueError(f"Task {task_id!r} not found")
            state, existing_result, task_type = row
            safe_result = strip_credentials_from_wire_result(task_type, result)
            if state == "completed":
                if existing_result == safe_result:
                    return  # idempotent
                raise ValueError(f"Task {task_id!r} already completed with a different result")
            raise ValueError(f"Task {task_id!r} already in terminal state {state!r}")

    async def fail(
        self,
        task_id: str,
        error: dict[str, Any],
    ) -> None:
        """Mark the task ``failed`` with ``error`` as the terminal payload.

        Idempotent on repeated calls with an equal ``error``; raises
        :class:`ValueError` on conflicting re-failure.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(self._sql_fail, (json.dumps(error), time.time(), task_id))
            row = await cur.fetchone()
            if row is not None:
                await self._enqueue_terminal_if_registered(
                    conn,
                    row=row,
                    status="failed",
                    payload=error,
                )
                return  # updated successfully

            # Zero rows in RETURNING — task is unknown or already terminal.
            cur2 = await conn.execute(self._sql_get_state_error, (task_id,))
            row = await cur2.fetchone()
            if row is None:
                raise ValueError(f"Task {task_id!r} not found")
            state, existing_error = row
            if state == "failed":
                if existing_error == error:
                    return  # idempotent
                raise ValueError(f"Task {task_id!r} already failed with a different error")
            raise ValueError(f"Task {task_id!r} already in terminal state {state!r}")

    async def get(
        self,
        task_id: str,
        *,
        expected_account_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up a task record; cross-tenant probes return ``None``.

        The ``expected_account_id`` predicate is enforced at the SQL level
        (``WHERE account_id = %s``), not as a Python-level filter after fetch.
        This guarantees the row is never materialized for a mismatched probe,
        eliminating the fetch-then-filter anti-pattern.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_get, (task_id, expected_account_id, expected_account_id)
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {
                "task_id": row[0],
                "account_id": row[1],
                "state": row[2],
                "task_type": row[3],
                "progress": row[4],
                "result": row[5],
                "error": row[6],
                "created_at": row[8],
                "updated_at": row[9],
                **({"context": row[7]} if row[7] is not None else {}),
            }

    async def _enqueue_terminal_if_registered(
        self,
        conn: Any,
        *,
        row: tuple[Any, ...],
        status: str,
        payload: dict[str, Any],
    ) -> None:
        """Enqueue on ``conn`` so task state and webhook commit atomically."""
        task_id, account_id, task_type, encrypted_registration, registration_nonce = row
        if encrypted_registration is None:
            return
        if self.task_webhook_outbox is None:
            raise RuntimeError(
                "Task carries push_notification_config but PgTaskRegistry has no "
                "task_webhook_outbox; refusing a non-atomic terminal transition"
            )
        if registration_nonce is None:
            raise RuntimeError(f"Task {task_id!r} has incomplete webhook registration")
        url, operation_id, token = self.task_webhook_outbox.open_registration(
            account_id=account_id,
            task_id=task_id,
            task_type=task_type,
            encrypted_registration=bytes(encrypted_registration),
            nonce=bytes(registration_nonce),
        )
        await self.task_webhook_outbox.enqueue_terminal(
            conn,
            task_id=task_id,
            account_id=account_id,
            task_type=task_type,
            status=status,
            result=payload,
            url=url,
            operation_id=operation_id,
            token=token,
        )
        # The encrypted outbox envelope now owns the callback registration.
        # Clear the task-row copy in this same transaction.
        await conn.execute(self._sql_clear_webhook_registration, (task_id,))

    async def discard(self, task_id: str) -> None:
        """Remove a task_id from the registry — rollback path.

        Idempotent: discarding an unknown task_id is a no-op (no raise),
        matching the :class:`~adcp.decisioning.InMemoryTaskRegistry` contract.
        """
        async with self._pool.connection() as conn:
            await conn.execute(self._sql_discard, (task_id,))


#: Backwards-compat alias. Renamed to :class:`PgTaskRegistry` in
#: 4.4.0 to match the framework's ``Pg*`` convention shared with
#: :class:`adcp.signing.PgReplayStore` and
#: :class:`adcp.decisioning.PgBuyerAgentRegistry`. The old name keeps
#: working for one minor (4.4.x) and is removed in 4.5.0.
PostgresTaskRegistry = PgTaskRegistry


__all__ = ["PG_AVAILABLE", "PgTaskRegistry", "PostgresTaskRegistry"]
