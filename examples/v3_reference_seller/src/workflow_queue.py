"""PostgreSQL queue for adopter-owned ``WorkflowHandoff`` work.

This is deliberately example code, not an SDK primitive. It demonstrates the
minimum production invariants: durable enqueue, single-worker leases,
restart recovery after lease expiry, account scoping, and completion through
the SDK's durable task registry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from adcp.decisioning import TaskRegistry

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
DEFAULT_WORKFLOW_TABLE = "adcp_reference_workflows"


@dataclass(frozen=True)
class WorkflowJob:
    """One leased workflow job returned to a worker."""

    task_id: str
    account_id: str
    workflow_type: str
    payload: dict[str, Any]
    lease_token: str
    attempt_count: int


WorkflowHandler = Callable[[WorkflowJob], Awaitable[dict[str, Any]]]


class PgWorkflowQueue:
    """A small durable queue that completes SDK-managed workflow tasks.

    Handlers must make external side effects idempotent. A worker can finish
    the side effect and die before acknowledging the queue row; after the
    lease expires, a replacement worker intentionally runs the job again.

    ``payload`` is ordinary JSONB, not an encrypted secret store. Enqueue only
    the minimum continuation data and never include callback credentials.
    """

    def __init__(
        self,
        *,
        pool: AsyncConnectionPool,
        registry: TaskRegistry,
        table: str = DEFAULT_WORKFLOW_TABLE,
        lease_seconds: int = 60,
    ) -> None:
        if not _SAFE_IDENTIFIER.fullmatch(table):
            raise ValueError("workflow table must be a safe lowercase PostgreSQL identifier")
        if lease_seconds < 2:
            raise ValueError("workflow lease_seconds must be at least 2")
        self._pool = pool
        self._registry = registry
        self._table = table
        self._lease_seconds = lease_seconds

        self._sql_claim = (  # noqa: S608 - table is validated above
            f"WITH candidate AS ("
            f" SELECT task_id FROM {table}"
            " WHERE state = 'pending' AND available_at <= now()"
            " ORDER BY available_at, created_at"
            " FOR UPDATE SKIP LOCKED LIMIT 1"
            ") "
            f"UPDATE {table} AS jobs SET"
            " state = 'in_flight', lease_token = %s,"
            " lease_expires_at = now() + (%s * interval '1 second'),"
            " attempt_count = attempt_count + 1, updated_at = now()"
            " FROM candidate WHERE jobs.task_id = candidate.task_id"
            " RETURNING jobs.task_id, jobs.account_id, jobs.workflow_type,"
            " jobs.payload, jobs.lease_token, jobs.attempt_count"
        )

    async def create_schema(self) -> None:
        """Bootstrap the example table for local development and tests.

        Production deployments should apply the equivalent DDL through their
        migration system before starting web or worker processes.
        """
        statements = [
            f"""CREATE TABLE IF NOT EXISTS {self._table} (
                task_id          TEXT COLLATE "C" PRIMARY KEY,
                account_id       TEXT COLLATE "C" NOT NULL,
                workflow_type    TEXT NOT NULL,
                payload          JSONB NOT NULL,
                state            TEXT NOT NULL DEFAULT 'pending',
                attempt_count    INTEGER NOT NULL DEFAULT 0,
                available_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                lease_token      TEXT COLLATE "C",
                lease_expires_at TIMESTAMPTZ,
                last_error       TEXT,
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                completed_at     TIMESTAMPTZ,
                CHECK (state IN ('pending', 'in_flight', 'completed')),
                CHECK (attempt_count >= 0),
                CHECK (
                    (state = 'in_flight') =
                    (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
                )
            )""",
            f"""CREATE INDEX IF NOT EXISTS {self._table}_work_idx
                ON {self._table} (available_at, created_at)
                WHERE state = 'pending'""",
            f"""CREATE INDEX IF NOT EXISTS {self._table}_lease_idx
                ON {self._table} (lease_expires_at)
                WHERE state = 'in_flight'""",
        ]
        async with self._pool.connection() as conn:
            for statement in statements:
                await conn.execute(statement)

    async def enqueue(
        self,
        *,
        task_id: str,
        account_id: str,
        workflow_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist work from a ``WorkflowHandoff`` enqueue callback."""
        if not task_id or not account_id or not workflow_type:
            raise ValueError("task_id, account_id, and workflow_type are required")
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        sql = (  # noqa: S608 - table is validated in __init__
            f"INSERT INTO {self._table} "
            "(task_id, account_id, workflow_type, payload) "
            "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (task_id) DO NOTHING "
            "RETURNING task_id"
        )
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    sql,
                    (task_id, account_id, workflow_type, serialized),
                )
            ).fetchone()
            if row is not None:
                return
            existing = await (
                await conn.execute(
                    f"SELECT account_id, workflow_type, payload "  # noqa: S608
                    f"FROM {self._table} WHERE task_id = %s",
                    (task_id,),
                )
            ).fetchone()
        if existing != (account_id, workflow_type, payload):
            raise ValueError("workflow task_id already exists with different work")

    async def enqueue_from_handoff(
        self,
        task_ctx: Any,
        *,
        account_id: str,
        workflow_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Adapter-friendly callback used with ``ctx.handoff_to_workflow``."""
        await self.enqueue(
            task_id=task_ctx.id,
            account_id=account_id,
            workflow_type=workflow_type,
            payload=payload,
        )

    async def claim(self) -> WorkflowJob | None:
        """Recover expired leases and claim one eligible job."""
        lease_token = uuid.uuid4().hex
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE {self._table} SET state = 'pending', lease_token = NULL, "  # noqa: S608
                "lease_expires_at = NULL, updated_at = now() "
                "WHERE state = 'in_flight' AND lease_expires_at <= now()"
            )
            row = await (
                await conn.execute(
                    self._sql_claim,
                    (lease_token, self._lease_seconds),
                )
            ).fetchone()
        if row is None:
            return None
        task_id, account_id, workflow_type, payload, token, attempts = row
        payload_dict = payload if isinstance(payload, dict) else json.loads(payload)
        return WorkflowJob(
            task_id=str(task_id),
            account_id=str(account_id),
            workflow_type=str(workflow_type),
            payload=payload_dict,
            lease_token=str(token),
            attempt_count=int(attempts),
        )

    async def _release(self, job: WorkflowJob, exc: Exception) -> None:
        # Persist the class for operations without writing arbitrary exception
        # text (which may contain request data or credentials) into PostgreSQL.
        message = type(exc).__name__
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE {self._table} SET state = 'pending', "  # noqa: S608
                "available_at = now() + interval '1 second', lease_token = NULL, "
                "lease_expires_at = NULL, last_error = %s, updated_at = now() "
                "WHERE task_id = %s AND state = 'in_flight' AND lease_token = %s",
                (message, job.task_id, job.lease_token),
            )

    async def _acknowledge(self, job: WorkflowJob) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"UPDATE {self._table} SET state = 'completed', "  # noqa: S608
                "lease_token = NULL, lease_expires_at = NULL, last_error = NULL, "
                "completed_at = now(), updated_at = now() "
                "WHERE task_id = %s AND state = 'in_flight' AND lease_token = %s",
                (job.task_id, job.lease_token),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("workflow lease was lost before acknowledgement")

    async def process_one(self, handler: WorkflowHandler) -> bool:
        """Run one leased job and complete the matching SDK task."""
        job = await self.claim()
        if job is None:
            return False
        try:
            task = await self._registry.get(
                job.task_id,
                expected_account_id=job.account_id,
            )
            if task is None:
                raise ValueError("workflow job does not match an account-scoped task")
            if task.get("state") in {"completed", "failed"}:
                # A prior worker may have committed terminal task state and
                # died before acknowledging this queue lease. Do not rerun the
                # business effect; reconcile the queue row to terminal state.
                await self._acknowledge(job)
                return True
            result = await handler(job)
            await self._registry.complete(job.task_id, result)
            await self._acknowledge(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._release(job, exc)
            logger.exception("Workflow job %s failed; released for retry", job.task_id)
        return True

    async def run_worker(
        self,
        handler: WorkflowHandler,
        *,
        poll_interval: float = 1.0,
    ) -> None:
        """Process jobs until cancellation; expired leases recover on restart."""
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        while True:
            try:
                processed = await self.process_one(handler)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Workflow worker iteration failed; retrying")
                processed = False
            if not processed:
                await asyncio.sleep(poll_interval)

    async def get(self, task_id: str) -> dict[str, Any] | None:
        """Read queue state for operations and recovery tests."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    f"SELECT account_id, workflow_type, state, attempt_count, last_error "  # noqa: S608
                    f"FROM {self._table} WHERE task_id = %s",
                    (task_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "account_id": str(row[0]),
            "workflow_type": str(row[1]),
            "state": str(row[2]),
            "attempt_count": int(row[3]),
            "last_error": row[4],
        }


__all__ = [
    "DEFAULT_WORKFLOW_TABLE",
    "PgWorkflowQueue",
    "WorkflowHandler",
    "WorkflowJob",
]
