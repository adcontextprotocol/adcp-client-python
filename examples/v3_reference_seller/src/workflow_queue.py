"""PostgreSQL queue for adopter-owned ``WorkflowHandoff`` work.

This is deliberately example code, not an SDK primitive. It demonstrates the
production invariants needed around a durable task registry: leases with
heartbeats, bounded handler attempts, durable finalization, account scoping,
and restart-safe reconciliation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from adcp.decisioning import AdcpError
from adcp.decisioning.account_projection import strip_credentials_from_wire_result

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from adcp.decisioning import TaskRegistry

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
DEFAULT_WORKFLOW_TABLE = "adcp_reference_workflows"
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_MAX_RETRY_SECONDS = 300.0
DEFAULT_RETRY_BASE_SECONDS = 1.0

_RETRY_EXHAUSTED_ERROR: dict[str, str] = {
    "code": "INTERNAL_ERROR",
    "message": "Workflow processing exhausted its retry budget.",
    "recovery": "terminal",
}

FinalizationAction = Literal["complete", "fail"]


class WorkflowLeaseLostError(RuntimeError):
    """The row is no longer owned by this worker's lease token."""


@dataclass(frozen=True)
class WorkflowJob:
    """One leased workflow job returned to a worker."""

    task_id: str
    account_id: str
    workflow_type: str
    payload: dict[str, Any]
    lease_token: str
    attempt_count: int
    finalization_action: FinalizationAction | None = None
    finalization_payload: dict[str, Any] | None = None


WorkflowHandler = Callable[[WorkflowJob], Awaitable[dict[str, Any]]]


class PgWorkflowQueue:
    """A small durable queue that completes SDK-managed workflow tasks.

    A handler's result (or terminal error) is written to the queue before the
    task registry is updated. Registry outages therefore retry finalization,
    never the business handler. Handlers must still make external side effects
    idempotent because a process can die before that staging write commits.

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
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
        max_retry_seconds: float = DEFAULT_MAX_RETRY_SECONDS,
    ) -> None:
        if not _SAFE_IDENTIFIER.fullmatch(table):
            raise ValueError("workflow table must be a safe lowercase PostgreSQL identifier")
        if lease_seconds < 2:
            raise ValueError("workflow lease_seconds must be at least 2")
        if max_attempts < 1:
            raise ValueError("workflow max_attempts must be at least 1")
        if retry_base_seconds <= 0:
            raise ValueError("workflow retry_base_seconds must be positive")
        if max_retry_seconds < retry_base_seconds:
            raise ValueError("workflow max_retry_seconds must be at least retry_base_seconds")
        self._pool = pool
        self._registry = registry
        self._table = table
        suffix = "_state_check"
        self._state_constraint = f"{table[: 63 - len(suffix)]}{suffix}"
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._max_retry_seconds = max_retry_seconds

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
            " updated_at = now()"
            " FROM candidate WHERE jobs.task_id = candidate.task_id"
            " RETURNING jobs.task_id, jobs.account_id, jobs.workflow_type,"
            " jobs.payload, jobs.lease_token, jobs.attempt_count,"
            " jobs.finalization_action, jobs.finalization_payload"
        )

    async def create_schema(self) -> None:
        """Bootstrap or forward-upgrade the example table."""
        statements = [
            f"""CREATE TABLE IF NOT EXISTS {self._table} (
                task_id              TEXT COLLATE "C" PRIMARY KEY,
                account_id           TEXT COLLATE "C" NOT NULL,
                workflow_type        TEXT NOT NULL,
                payload              JSONB NOT NULL,
                state                TEXT NOT NULL DEFAULT 'pending',
                attempt_count        INTEGER NOT NULL DEFAULT 0,
                available_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                lease_token          TEXT COLLATE "C",
                lease_expires_at     TIMESTAMPTZ,
                finalization_action  TEXT,
                finalization_payload JSONB,
                last_error           TEXT,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
                completed_at         TIMESTAMPTZ,
                dead_lettered_at     TIMESTAMPTZ,
                CHECK (attempt_count >= 0),
                CHECK (
                    (state = 'in_flight') =
                    (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
                )
            )""",
            f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS dead_lettered_at TIMESTAMPTZ",
            f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS finalization_action TEXT",
            f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS finalization_payload JSONB",
            f"ALTER TABLE {self._table} DROP CONSTRAINT IF EXISTS {self._state_constraint}",
            f"""ALTER TABLE {self._table}
                ADD CONSTRAINT {self._state_constraint}
                CHECK (state IN ('pending', 'in_flight', 'completed', 'dead_lettered'))""",
            f"""CREATE INDEX IF NOT EXISTS {self._table}_work_idx
                ON {self._table} (available_at, created_at)
                WHERE state = 'pending'""",
            f"""CREATE INDEX IF NOT EXISTS {self._table}_lease_idx
                ON {self._table} (lease_expires_at)
                WHERE state = 'in_flight'""",
        ]
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"adcp.workflow_queue.schema:{self._table}",),
                )
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
                await conn.execute(sql, (task_id, account_id, workflow_type, serialized))
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
        """Recover expired leases and claim one eligible job.

        Claiming does not consume a handler attempt. The attempt counter is
        incremented only after the account-scoped registry lookup succeeds.
        """
        lease_token = uuid.uuid4().hex
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE {self._table} SET state = 'pending', lease_token = NULL, "  # noqa: S608
                "lease_expires_at = NULL, updated_at = now() "
                "WHERE state = 'in_flight' AND lease_expires_at <= now()"
            )
            row = await (
                await conn.execute(self._sql_claim, (lease_token, self._lease_seconds))
            ).fetchone()
        if row is None:
            return None
        payload = row[3] if isinstance(row[3], dict) else json.loads(row[3])
        finalization_payload = row[7]
        if finalization_payload is not None and not isinstance(finalization_payload, dict):
            finalization_payload = json.loads(finalization_payload)
        return WorkflowJob(
            task_id=str(row[0]),
            account_id=str(row[1]),
            workflow_type=str(row[2]),
            payload=payload,
            lease_token=str(row[4]),
            attempt_count=int(row[5]),
            finalization_action=row[6],
            finalization_payload=finalization_payload,
        )

    def _retry_delay(self, attempt_count: int) -> float:
        exponent = min(max(attempt_count - 1, 0), 30)
        return float(min(self._max_retry_seconds, self._retry_base_seconds * (2**exponent)))

    async def _token_update(
        self,
        job: WorkflowJob,
        sql: str,
        params: tuple[Any, ...],
        operation: str,
    ) -> None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(sql, params)
        if cursor.rowcount != 1:
            raise WorkflowLeaseLostError(f"workflow lease was lost before {operation}")

    async def _release(self, job: WorkflowJob, reason: str) -> None:
        retry_delay = self._retry_delay(job.attempt_count)
        await self._token_update(
            job,
            f"UPDATE {self._table} SET state = 'pending', "  # noqa: S608
            "available_at = now() + (%s * interval '1 second'), "
            "lease_token = NULL, lease_expires_at = NULL, last_error = %s, "
            "updated_at = now() WHERE task_id = %s AND state = 'in_flight' "
            "AND lease_token = %s",
            (retry_delay, reason, job.task_id, job.lease_token),
            "release",
        )

    async def _dead_letter(self, job: WorkflowJob, reason: str) -> None:
        await self._token_update(
            job,
            f"UPDATE {self._table} SET state = 'dead_lettered', "  # noqa: S608
            "lease_token = NULL, lease_expires_at = NULL, "
            "finalization_payload = NULL, "
            "last_error = COALESCE(last_error, %s), "
            "dead_lettered_at = now(), updated_at = now() "
            "WHERE task_id = %s AND state = 'in_flight' AND lease_token = %s",
            (reason, job.task_id, job.lease_token),
            "dead-lettering",
        )

    async def _acknowledge(self, job: WorkflowJob) -> None:
        await self._token_update(
            job,
            f"UPDATE {self._table} SET state = 'completed', "  # noqa: S608
            "lease_token = NULL, lease_expires_at = NULL, last_error = NULL, "
            "finalization_payload = NULL, "
            "completed_at = now(), updated_at = now() "
            "WHERE task_id = %s AND state = 'in_flight' AND lease_token = %s",
            (job.task_id, job.lease_token),
            "acknowledgement",
        )

    async def _renew_lease(self, job: WorkflowJob) -> None:
        await self._token_update(
            job,
            f"UPDATE {self._table} SET "  # noqa: S608
            "lease_expires_at = now() + (%s * interval '1 second'), updated_at = now() "
            "WHERE task_id = %s AND state = 'in_flight' AND lease_token = %s",
            (self._lease_seconds, job.task_id, job.lease_token),
            "lease renewal",
        )

    async def _heartbeat(self, job: WorkflowJob) -> None:
        interval = self._lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            await self._renew_lease(job)

    async def _begin_handler_attempt(self, job: WorkflowJob) -> WorkflowJob | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    f"UPDATE {self._table} SET attempt_count = attempt_count + 1, "  # noqa: S608
                    "updated_at = now() WHERE task_id = %s AND state = 'in_flight' "
                    "AND lease_token = %s AND finalization_action IS NULL "
                    "AND attempt_count < %s RETURNING attempt_count",
                    (job.task_id, job.lease_token, self._max_attempts),
                )
            ).fetchone()
        if row is None:
            return None
        return replace(job, attempt_count=int(row[0]))

    async def _stage_finalization(
        self,
        job: WorkflowJob,
        action: FinalizationAction,
        payload: dict[str, Any],
        *,
        last_error: str | None = None,
    ) -> WorkflowJob:
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        await self._token_update(
            job,
            f"UPDATE {self._table} SET finalization_action = %s, "  # noqa: S608
            "finalization_payload = %s::jsonb, last_error = %s, updated_at = now() "
            "WHERE task_id = %s AND state = 'in_flight' AND lease_token = %s "
            "AND finalization_action IS NULL",
            (action, serialized, last_error, job.task_id, job.lease_token),
            "staging finalization",
        )
        return replace(
            job,
            finalization_action=action,
            finalization_payload=dict(payload),
        )

    async def _stage_despite_cancellation(
        self,
        job: WorkflowJob,
        action: FinalizationAction,
        payload: dict[str, Any],
        *,
        last_error: str | None = None,
    ) -> WorkflowJob:
        """Finish the short DB write even if worker shutdown races it."""
        staging = asyncio.create_task(
            self._stage_finalization(job, action, payload, last_error=last_error)
        )
        try:
            return await asyncio.shield(staging)
        except asyncio.CancelledError:
            staged_job = await staging
            try:
                await asyncio.shield(self._release(staged_job, "CancelledError"))
            except WorkflowLeaseLostError:
                pass
            raise

    async def _run_handler(self, job: WorkflowJob, handler: WorkflowHandler) -> dict[str, Any]:
        handler_task: asyncio.Future[dict[str, Any]] = asyncio.ensure_future(handler(job))
        heartbeat_task = asyncio.create_task(self._heartbeat(job))
        waiters: set[asyncio.Future[Any]] = {handler_task, heartbeat_task}
        try:
            done, _ = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                heartbeat_error = heartbeat_task.exception()
                if heartbeat_error is not None:
                    handler_task.cancel()
                    await asyncio.gather(handler_task, return_exceptions=True)
                    raise heartbeat_error
            return await handler_task
        except asyncio.CancelledError:
            handler_task.cancel()
            heartbeat_task.cancel()
            await asyncio.gather(handler_task, heartbeat_task, return_exceptions=True)
            try:
                await asyncio.shield(self._release(job, "CancelledError"))
            except WorkflowLeaseLostError:
                pass
            raise
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _apply_finalization(self, job: WorkflowJob) -> None:
        action = job.finalization_action
        payload = job.finalization_payload
        if action is None or payload is None:
            raise RuntimeError("workflow finalization is incomplete")
        if action == "complete":
            await self._registry.complete(job.task_id, payload)
            await self._acknowledge(job)
        else:
            await self._registry.fail(job.task_id, payload)
            await self._dead_letter(job, str(payload.get("code", "workflow_failed")))

    async def _retry_finalization(self, job: WorkflowJob) -> None:
        try:
            await self._apply_finalization(job)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._release(job, "CancelledError"))
            except WorkflowLeaseLostError:
                pass
            raise
        except Exception as exc:
            await self._release(job, type(exc).__name__)
            logger.warning(
                "Workflow task %s finalization failed with %s; released for retry",
                job.task_id,
                type(exc).__name__,
            )

    async def process_one(self, handler: WorkflowHandler) -> bool:
        """Run one leased job and reconcile it with the matching SDK task."""
        job = await self.claim()
        if job is None:
            return False

        try:
            task = await self._registry.get(
                job.task_id,
                expected_account_id=job.account_id,
            )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._release(job, "CancelledError"))
            except WorkflowLeaseLostError:
                pass
            raise
        except Exception as exc:
            # Registry availability is not a business-handler failure. Retry
            # indefinitely without consuming the handler attempt budget.
            await self._release(job, type(exc).__name__)
            logger.warning(
                "Workflow task lookup failed with %s; released for retry",
                type(exc).__name__,
            )
            return True

        if task is None:
            # Unknown and cross-account task ids are intentionally
            # indistinguishable. Never mutate the registry by task id alone.
            await self._dead_letter(job, "task_missing_or_account_mismatch")
            logger.error(
                "Workflow job %s has no matching account-scoped task; dead-lettered",
                job.task_id,
            )
            return True

        if task.get("state") in {"completed", "failed"}:
            if task.get("state") == "failed":
                reason = (
                    "retry_budget_exhausted"
                    if task.get("error") == _RETRY_EXHAUSTED_ERROR
                    else "registry_task_already_failed"
                )
                await self._dead_letter(job, reason)
            else:
                await self._acknowledge(job)
            return True

        if job.finalization_action is not None:
            await self._retry_finalization(job)
            return True

        attempted_job = await self._begin_handler_attempt(job)
        if attempted_job is None:
            # A crash after the last handler attempt but before outcome staging
            # must not permit an extra execution.
            staged = await self._stage_despite_cancellation(
                job,
                "fail",
                dict(_RETRY_EXHAUSTED_ERROR),
                last_error="retry_budget_exhausted",
            )
            await self._retry_finalization(staged)
            return True
        job = attempted_job

        try:
            result = await self._run_handler(job, handler)
        except asyncio.CancelledError:
            raise
        except AdcpError as exc:
            # Correctable errors require a changed buyer request, so retrying
            # this unchanged queued job cannot resolve them. Only transient
            # failures consume the workflow retry budget.
            if exc.recovery == "transient" and job.attempt_count < self._max_attempts:
                await self._release(job, type(exc).__name__)
                return True
            staged = await self._stage_despite_cancellation(
                job,
                "fail",
                exc.to_wire(),
                last_error=exc.code,
            )
            await self._retry_finalization(staged)
            return True
        except WorkflowLeaseLostError:
            # The handler was cancelled by _run_handler before this escapes.
            # A replacement owner is responsible for the row.
            raise
        except Exception as exc:
            if job.attempt_count < self._max_attempts:
                await self._release(job, type(exc).__name__)
                logger.warning(
                    "Workflow job %s failed on attempt %d with %s; released for retry",
                    job.task_id,
                    job.attempt_count,
                    type(exc).__name__,
                )
                return True
            staged = await self._stage_despite_cancellation(
                job,
                "fail",
                dict(_RETRY_EXHAUSTED_ERROR),
                last_error=type(exc).__name__,
            )
            await self._retry_finalization(staged)
            return True

        safe_result = strip_credentials_from_wire_result(str(task["task_type"]), result)
        if not isinstance(safe_result, dict):
            raise TypeError("workflow handler result must serialize to an object")
        staged = await self._stage_despite_cancellation(job, "complete", safe_result)
        await self._retry_finalization(staged)
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
                    f"SELECT account_id, workflow_type, state, attempt_count, last_error, "  # noqa: S608
                    f"finalization_action, finalization_payload FROM {self._table} "
                    "WHERE task_id = %s",
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
            "finalization_action": row[5],
            "finalization_payload": row[6],
        }


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_RETRY_SECONDS",
    "DEFAULT_RETRY_BASE_SECONDS",
    "DEFAULT_WORKFLOW_TABLE",
    "PgWorkflowQueue",
    "WorkflowHandler",
    "WorkflowJob",
    "WorkflowLeaseLostError",
]
