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
    from adcp.decisioning import PostgresTaskRegistry, serve
    from myapp import MyPlatform

    async def main():
        async with AsyncConnectionPool(
            "postgresql://user:pass@localhost/mydb",
            min_size=2,
            max_size=10,
        ) as pool:
            registry = PostgresTaskRegistry(pool=pool)
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
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

try:
    from psycopg_pool import AsyncConnectionPool as _AsyncConnectionPool  # noqa: F401

    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "PostgresTaskRegistry requires psycopg3 and psycopg-pool. "
    "Install the 'pg' extra: `pip install 'adcp[pg]'` "
    "(Poetry: `poetry add 'adcp[pg]'`)."
)


class PostgresTaskRegistry:
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
    """

    is_durable: ClassVar[bool] = True

    def __init__(self, *, pool: AsyncConnectionPool) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        self._pool = pool

    # -- schema bootstrap -----------------------------------------------

    async def create_schema(self) -> None:
        """Create the ``decisioning_tasks`` table and supporting index.

        Idempotent via ``CREATE TABLE IF NOT EXISTS`` — safe to call on
        every application boot. The equivalent raw DDL ships at
        :file:`src/adcp/decisioning/pg/decisioning_tasks.sql` for adopters
        using a migration tool (Alembic, Flyway, psql).
        """
        ddl = """
            CREATE TABLE IF NOT EXISTS decisioning_tasks (
                task_id     TEXT             COLLATE "C" NOT NULL PRIMARY KEY,
                account_id  TEXT             COLLATE "C" NOT NULL,
                state       TEXT             NOT NULL DEFAULT 'submitted',
                task_type   TEXT             NOT NULL,
                progress    JSONB,
                result      JSONB,
                error       JSONB,
                created_at  DOUBLE PRECISION NOT NULL,
                updated_at  DOUBLE PRECISION NOT NULL
            );
            CREATE INDEX IF NOT EXISTS decisioning_tasks_account_idx
                ON decisioning_tasks (account_id);
        """
        async with self._pool.connection() as conn:
            await conn.execute(ddl)

    # -- TaskRegistry Protocol ------------------------------------------

    async def issue(
        self,
        *,
        account_id: str,
        task_type: str,
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
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        now = time.time()
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO decisioning_tasks
                    (task_id, account_id, state, task_type, created_at, updated_at)
                VALUES (%s, %s, 'submitted', %s, %s, %s)
                """,
                (task_id, account_id, task_type, now, now),
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
                """
                UPDATE decisioning_tasks
                SET state     = CASE state WHEN 'submitted' THEN 'working' ELSE state END,
                    progress  = %s::jsonb,
                    updated_at = %s
                WHERE task_id = %s
                  AND state NOT IN ('completed', 'failed')
                """,
                (json.dumps(progress), time.time(), task_id),
            )
            # rowcount 0 means unknown task_id or terminal state — silent no-op
            # per Protocol contract. The InMemoryTaskRegistry logs a warning on
            # terminal-state drops; we omit the extra SELECT needed to distinguish
            # the two cases here since the dispatch wrapper swallows the result.

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
            cur = await conn.execute(
                """
                UPDATE decisioning_tasks
                SET state      = 'completed',
                    result     = %s::jsonb,
                    updated_at = %s
                WHERE task_id = %s
                  AND state NOT IN ('completed', 'failed')
                RETURNING task_id
                """,
                (json.dumps(result), time.time(), task_id),
            )
            if await cur.fetchone() is not None:
                return  # updated successfully

            # Zero rows updated — either unknown task_id or already terminal.
            cur2 = await conn.execute(
                "SELECT state, result FROM decisioning_tasks WHERE task_id = %s",
                (task_id,),
            )
            row = await cur2.fetchone()
            if row is None:
                raise ValueError(f"Task {task_id!r} not found")
            state, existing_result = row
            if state == "completed":
                if existing_result == result:
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
            cur = await conn.execute(
                """
                UPDATE decisioning_tasks
                SET state      = 'failed',
                    error      = %s::jsonb,
                    updated_at = %s
                WHERE task_id = %s
                  AND state NOT IN ('completed', 'failed')
                RETURNING task_id
                """,
                (json.dumps(error), time.time(), task_id),
            )
            if await cur.fetchone() is not None:
                return  # updated successfully

            cur2 = await conn.execute(
                "SELECT state, error FROM decisioning_tasks WHERE task_id = %s",
                (task_id,),
            )
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
                """
                SELECT task_id, account_id, state, task_type,
                       progress, result, error, created_at, updated_at
                FROM decisioning_tasks
                WHERE task_id = %s
                  AND (%s IS NULL OR account_id = %s)
                """,
                (task_id, expected_account_id, expected_account_id),
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
                "created_at": row[7],
                "updated_at": row[8],
            }

    async def discard(self, task_id: str) -> None:
        """Remove a task_id from the registry — rollback path.

        Idempotent: discarding an unknown task_id is a no-op (no raise),
        matching the :class:`~adcp.decisioning.InMemoryTaskRegistry` contract.
        """
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM decisioning_tasks WHERE task_id = %s",
                (task_id,),
            )


__all__ = ["PG_AVAILABLE", "PostgresTaskRegistry"]
