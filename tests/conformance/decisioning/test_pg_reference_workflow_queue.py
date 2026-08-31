"""Restart-recovery test for the reference PostgreSQL WorkflowHandoff queue.

Set ``ADCP_PG_TEST_URL`` to run this test against PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import BaseModel

psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping reference workflow queue test",
        allow_module_level=True,
    )

_EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "v3_reference_seller"
sys.path.insert(0, str(_EXAMPLE))

from src.workflow_queue import PgWorkflowQueue  # noqa: E402

from adcp.decisioning import Account, AdcpError, PgTaskRegistry, RequestContext  # noqa: E402
from adcp.decisioning.dispatch import _project_workflow_handoff  # noqa: E402


class _Request(BaseModel):
    context: dict[str, str] | None = None


@pytest.mark.asyncio
async def test_workflow_handoff_recovers_expired_lease_after_worker_restart() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"
    account_id = "tenant-a:account-a"
    expected_result = {"media_buy_id": "mb_recovered", "status": "active"}

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=1,
        max_size=4,
        open=False,
    ) as web_pool:
        await web_pool.open()
        registry = PgTaskRegistry(pool=web_pool, _table=task_table)
        queue = PgWorkflowQueue(
            pool=web_pool,
            registry=registry,
            table=workflow_table,
            lease_seconds=2,
        )
        await registry.create_schema()
        await queue.create_schema()
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            ctx = RequestContext(
                account=Account(id=account_id),
                tenant_id="tenant-a",
                caller_identity="buyer-a",
            )

            async def enqueue(task_ctx) -> None:
                await queue.enqueue_from_handoff(
                    task_ctx,
                    account_id=account_id,
                    workflow_type="manual_media_buy_approval",
                    payload={"result": expected_result},
                )

            submitted = await _project_workflow_handoff(
                ctx.handoff_to_workflow(enqueue),
                ctx,
                method_name="create_media_buy",
                registry=registry,
                executor=executor,
                request_params=_Request(context={"trace_id": "restart-test"}),
            )
            task_id = submitted["task_id"]
            assert submitted == {"task_id": task_id, "status": "submitted"}

            # Worker process 1 claims the row and dies before completion.
            async with psycopg_pool.AsyncConnectionPool(
                TEST_URL,
                min_size=1,
                max_size=2,
                open=False,
            ) as first_worker_pool:
                await first_worker_pool.open()
                first_worker_queue = PgWorkflowQueue(
                    pool=first_worker_pool,
                    registry=registry,
                    table=workflow_table,
                    lease_seconds=2,
                )
                first_claim = await first_worker_queue.claim()
                assert first_claim is not None
                assert first_claim.task_id == task_id
                assert first_claim.attempt_count == 0

            # Advance only the database lease, avoiding a wall-clock sleep.
            async with web_pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} "  # noqa: S608
                    "SET lease_expires_at = now() - interval '1 second' "
                    "WHERE task_id = %s",
                    (task_id,),
                )

            # Worker process 2 starts with fresh queue and registry objects,
            # reclaims the expired lease, and completes the original task.
            async with psycopg_pool.AsyncConnectionPool(
                TEST_URL,
                min_size=1,
                max_size=2,
                open=False,
            ) as replacement_pool:
                await replacement_pool.open()
                replacement_registry = PgTaskRegistry(
                    pool=replacement_pool,
                    _table=task_table,
                )
                replacement_queue = PgWorkflowQueue(
                    pool=replacement_pool,
                    registry=replacement_registry,
                    table=workflow_table,
                    lease_seconds=2,
                )

                async def complete(job):
                    assert job.attempt_count == 1
                    return job.payload["result"]

                assert await replacement_queue.process_one(complete) is True
                queue_record = await replacement_queue.get(task_id)
                task_record = await replacement_registry.get(
                    task_id,
                    expected_account_id=account_id,
                )

            assert queue_record is not None
            assert queue_record["state"] == "completed"
            assert queue_record["attempt_count"] == 1
            assert task_record is not None
            assert task_record["state"] == "completed"
            assert task_record["result"] == expected_result
            assert task_record["context"] == {"trace_id": "restart-test"}
        finally:
            executor.shutdown(wait=True)
            async with web_pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_workflow_retries_are_bounded_then_task_fails_and_job_dead_letters() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"
    account_id = "tenant-a:account-a"

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=1,
        max_size=4,
        open=False,
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)
        queue = PgWorkflowQueue(
            pool=pool,
            registry=registry,
            table=workflow_table,
            max_attempts=2,
            retry_base_seconds=1,
            max_retry_seconds=2,
        )
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = await registry.issue(
                account_id=account_id,
                task_type="create_media_buy",
            )
            await queue.enqueue(
                task_id=task_id,
                account_id=account_id,
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )
            attempts = 0

            async def fail(_job):
                nonlocal attempts
                attempts += 1
                raise RuntimeError("sensitive upstream failure")

            assert await queue.process_one(fail) is True
            first_record = await queue.get(task_id)
            assert first_record is not None
            assert first_record["state"] == "pending"
            assert first_record["attempt_count"] == 1
            assert first_record["last_error"] == "RuntimeError"

            # Advance the retry schedule without a wall-clock sleep.
            async with pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} SET available_at = now() "  # noqa: S608
                    "WHERE task_id = %s",
                    (task_id,),
                )

            assert await queue.process_one(fail) is True
            queue_record = await queue.get(task_id)
            task_record = await registry.get(
                task_id,
                expected_account_id=account_id,
            )

            assert attempts == 2
            assert queue_record is not None
            assert queue_record["state"] == "dead_lettered"
            assert queue_record["attempt_count"] == 2
            assert queue_record["last_error"] == "RuntimeError"
            assert task_record is not None
            assert task_record["state"] == "failed"
            assert task_record["error"] == {
                "code": "INTERNAL_ERROR",
                "message": "Workflow processing exhausted its retry budget.",
                "recovery": "terminal",
            }
            assert await queue.process_one(fail) is False
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issue_task", "job_account_id"),
    [
        pytest.param(False, "tenant-a:account-a", id="missing-task"),
        pytest.param(True, "tenant-b:account-b", id="account-mismatch"),
    ],
)
async def test_missing_or_mismatched_workflow_dead_letters_without_running(
    issue_task: bool,
    job_account_id: str,
) -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=1,
        max_size=4,
        open=False,
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)
        queue = PgWorkflowQueue(pool=pool, registry=registry, table=workflow_table)
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = (
                await registry.issue(
                    account_id="tenant-a:account-a",
                    task_type="create_media_buy",
                )
                if issue_task
                else f"task_missing_{suffix}"
            )
            await queue.enqueue(
                task_id=task_id,
                account_id=job_account_id,
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )
            handler_called = False

            async def handler(_job):
                nonlocal handler_called
                handler_called = True
                return {"status": "active"}

            assert await queue.process_one(handler) is True
            queue_record = await queue.get(task_id)
            task_record = await registry.get(
                task_id,
                expected_account_id="tenant-a:account-a",
            )

            assert handler_called is False
            assert queue_record is not None
            assert queue_record["state"] == "dead_lettered"
            assert queue_record["attempt_count"] == 0
            assert queue_record["last_error"] == "task_missing_or_account_mismatch"
            if issue_task:
                assert task_record is not None
                assert task_record["state"] == "submitted"
            else:
                assert task_record is None
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_failed_registry_commit_is_recovered_to_dead_letter_after_crash() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"
    account_id = "tenant-a:account-a"
    terminal_error = {
        "code": "INTERNAL_ERROR",
        "message": "Workflow processing exhausted its retry budget.",
        "recovery": "terminal",
    }

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=1,
        max_size=4,
        open=False,
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)
        queue = PgWorkflowQueue(
            pool=pool,
            registry=registry,
            table=workflow_table,
            lease_seconds=2,
            max_attempts=1,
        )
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = await registry.issue(
                account_id=account_id,
                task_type="create_media_buy",
            )
            await queue.enqueue(
                task_id=task_id,
                account_id=account_id,
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )
            claimed = await queue.claim()
            assert claimed is not None

            # Simulate a crash after the registry transaction commits but
            # before the separate queue dead-letter update begins.
            await registry.fail(task_id, terminal_error)
            async with pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} "  # noqa: S608
                    "SET lease_expires_at = now() - interval '1 second' "
                    "WHERE task_id = %s",
                    (task_id,),
                )

            handler_called = False

            async def handler(_job):
                nonlocal handler_called
                handler_called = True
                return {"status": "active"}

            assert await queue.process_one(handler) is True
            queue_record = await queue.get(task_id)

            assert handler_called is False
            assert queue_record is not None
            assert queue_record["state"] == "dead_lettered"
            assert queue_record["last_error"] == "retry_budget_exhausted"
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_create_schema_upgrades_pre_dead_letter_queue_table() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=1,
        max_size=4,
        open=False,
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)
        queue = PgWorkflowQueue(pool=pool, registry=registry, table=workflow_table)
        await registry.create_schema()
        async with pool.connection() as conn:
            await conn.execute(
                f"""CREATE TABLE {workflow_table} (  -- noqa: S608
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
                )"""
            )
        try:
            await queue.create_schema()
            task_id = f"task_missing_{suffix}"
            await queue.enqueue(
                task_id=task_id,
                account_id="tenant-a:account-a",
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )

            async def handler(_job):
                raise AssertionError("missing task must not reach handler")

            assert await queue.process_one(handler) is True
            queue_record = await queue.get(task_id)
            assert queue_record is not None
            assert queue_record["state"] == "dead_lettered"
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_exhausted_transient_error_survives_finalization_retry() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"
    account_id = "tenant-a:account-a"

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=1,
        max_size=4,
        open=False,
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)

        class FailOnceRegistry:
            def __init__(self) -> None:
                self.fail_calls = 0

            async def get(self, task_id, *, expected_account_id=None):
                return await registry.get(
                    task_id,
                    expected_account_id=expected_account_id,
                )

            async def complete(self, task_id, result):
                return await registry.complete(task_id, result)

            async def fail(self, task_id, error):
                self.fail_calls += 1
                if self.fail_calls == 1:
                    raise RuntimeError("registry temporarily unavailable")
                return await registry.fail(task_id, error)

        flaky_registry = FailOnceRegistry()
        queue = PgWorkflowQueue(
            pool=pool,
            registry=flaky_registry,  # type: ignore[arg-type]
            table=workflow_table,
            lease_seconds=2,
            max_attempts=1,
        )
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = await registry.issue(
                account_id=account_id,
                task_type="create_media_buy",
            )
            await queue.enqueue(
                task_id=task_id,
                account_id=account_id,
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )
            handler_calls = 0
            transient_error = AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Approval service is unavailable",
                recovery="transient",
                retry_after=30,
            )

            async def fail_handler(_job):
                nonlocal handler_calls
                handler_calls += 1
                raise transient_error

            assert await queue.process_one(fail_handler) is True
            staged_record = await queue.get(task_id)
            assert staged_record is not None
            assert staged_record["state"] == "pending"
            assert staged_record["finalization_action"] == "fail"
            assert staged_record["finalization_payload"] == transient_error.to_wire()

            async with pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} SET available_at = now() "  # noqa: S608
                    "WHERE task_id = %s",
                    (task_id,),
                )

            assert await queue.process_one(fail_handler) is True
            queue_record = await queue.get(task_id)
            task_record = await registry.get(
                task_id,
                expected_account_id=account_id,
            )

            assert handler_calls == 1
            assert flaky_registry.fail_calls == 2
            assert queue_record is not None
            assert queue_record["state"] == "dead_lettered"
            assert task_record is not None
            assert task_record["state"] == "failed"
            assert task_record["error"] == transient_error.to_wire()
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_mismatch_dead_letter_failure_never_fails_other_account_task() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=1,
        max_size=4,
        open=False,
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)

        class FailOnceDeadLetterQueue(PgWorkflowQueue):
            dead_letter_calls = 0

            async def _dead_letter(self, job, reason):
                self.dead_letter_calls += 1
                if self.dead_letter_calls == 1:
                    raise RuntimeError("queue temporarily unavailable")
                return await super()._dead_letter(job, reason)

        queue = FailOnceDeadLetterQueue(
            pool=pool,
            registry=registry,
            table=workflow_table,
            lease_seconds=2,
            max_attempts=1,
        )
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = await registry.issue(
                account_id="tenant-a:account-a",
                task_type="create_media_buy",
            )
            await queue.enqueue(
                task_id=task_id,
                account_id="tenant-b:account-b",
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )
            handler_called = False

            async def handler(_job):
                nonlocal handler_called
                handler_called = True
                return {"status": "active"}

            with pytest.raises(RuntimeError, match="queue temporarily unavailable"):
                await queue.process_one(handler)

            async with pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} "  # noqa: S608
                    "SET lease_expires_at = now() - interval '1 second' "
                    "WHERE task_id = %s",
                    (task_id,),
                )

            assert await queue.process_one(handler) is True
            queue_record = await queue.get(task_id)
            task_record = await registry.get(
                task_id,
                expected_account_id="tenant-a:account-a",
            )

            assert handler_called is False
            assert queue.dead_letter_calls == 2
            assert queue_record is not None
            assert queue_record["state"] == "dead_lettered"
            assert task_record is not None
            assert task_record["state"] == "submitted"
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_registry_lookup_outage_never_mutates_unverified_task() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"
    account_id = "tenant-a:account-a"

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=1,
        max_size=4,
        open=False,
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)

        class LookupOutageRegistry:
            lookup_calls = 0

            async def get(self, task_id, *, expected_account_id=None):
                self.lookup_calls += 1
                if self.lookup_calls <= 2:
                    raise RuntimeError("registry lookup unavailable")
                return await registry.get(task_id, expected_account_id=expected_account_id)

            async def complete(self, task_id, result):
                await registry.complete(task_id, result)

            async def fail(self, task_id, error):
                await registry.fail(task_id, error)

        queue = PgWorkflowQueue(
            pool=pool,
            registry=LookupOutageRegistry(),  # type: ignore[arg-type]
            table=workflow_table,
            max_attempts=2,
            retry_base_seconds=1,
        )
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = await registry.issue(
                account_id=account_id,
                task_type="create_media_buy",
            )
            await queue.enqueue(
                task_id=task_id,
                account_id=account_id,
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )
            handler_called = False

            async def handler(_job):
                nonlocal handler_called
                handler_called = True
                return {"status": "active"}

            assert await queue.process_one(handler) is True
            async with pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} SET available_at = now() "  # noqa: S608
                    "WHERE task_id = %s",
                    (task_id,),
                )
            assert await queue.process_one(handler) is True

            queue_record = await queue.get(task_id)
            task_record = await registry.get(
                task_id,
                expected_account_id=account_id,
            )
            assert handler_called is False
            assert queue_record is not None
            assert queue_record["state"] == "pending"
            assert queue_record["attempt_count"] == 0
            assert queue_record["last_error"] == "RuntimeError"
            assert task_record is not None
            assert task_record["state"] == "submitted"

            async with pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} SET available_at = now() "  # noqa: S608
                    "WHERE task_id = %s",
                    (task_id,),
                )
            assert await queue.process_one(handler) is True
            recovered_queue_record = await queue.get(task_id)
            recovered_task_record = await registry.get(
                task_id,
                expected_account_id=account_id,
            )
            assert handler_called is True
            assert recovered_queue_record is not None
            assert recovered_queue_record["state"] == "completed"
            assert recovered_queue_record["attempt_count"] == 1
            assert recovered_task_record is not None
            assert recovered_task_record["state"] == "completed"
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_completed_payload_is_staged_before_registry_retry() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"
    account_id = "tenant-a:account-a"
    unsafe_result = {
        "media_buy_id": "mb_staged",
        "status": "active",
        "account": {
            "billing_entity": {
                "legal_name": "Example Buyer",
                "bank": {"account_number": "secret-bank-account"},
            },
            "governance_agents": [
                {
                    "agent_url": "https://governance.example",
                    "authentication": {"credentials": "secret-token"},
                }
            ],
        },
    }
    safe_result = {
        "media_buy_id": "mb_staged",
        "status": "active",
        "account": {
            "billing_entity": {"legal_name": "Example Buyer"},
            "governance_agents": [{"agent_url": "https://governance.example"}],
        },
    }

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=4, open=False
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)

        class CompleteOnceUnavailableRegistry:
            complete_calls = 0

            async def get(self, task_id, *, expected_account_id=None):
                return await registry.get(task_id, expected_account_id=expected_account_id)

            async def complete(self, task_id, payload):
                self.complete_calls += 1
                if self.complete_calls == 1:
                    raise RuntimeError("registry temporarily unavailable")
                await registry.complete(task_id, payload)

            async def fail(self, task_id, error):
                await registry.fail(task_id, error)

        flaky_registry = CompleteOnceUnavailableRegistry()
        queue = PgWorkflowQueue(
            pool=pool,
            registry=flaky_registry,  # type: ignore[arg-type]
            table=workflow_table,
            retry_base_seconds=1,
        )
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = await registry.issue(account_id=account_id, task_type="create_media_buy")
            await queue.enqueue(
                task_id=task_id,
                account_id=account_id,
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )
            handler_calls = 0

            async def handler(_job):
                nonlocal handler_calls
                handler_calls += 1
                return unsafe_result

            assert await queue.process_one(handler) is True
            staged = await queue.get(task_id)
            assert staged is not None
            assert staged["state"] == "pending"
            assert staged["finalization_action"] == "complete"
            assert staged["finalization_payload"] == safe_result
            assert unsafe_result["account"]["billing_entity"]["bank"] == {
                "account_number": "secret-bank-account"
            }

            async with pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} SET available_at = now() WHERE task_id = %s",  # noqa: S608
                    (task_id,),
                )
            assert await queue.process_one(handler) is True
            task = await registry.get(task_id, expected_account_id=account_id)
            queue_record = await queue.get(task_id)
            assert handler_calls == 1
            assert flaky_registry.complete_calls == 2
            assert task is not None and task["result"] == safe_result
            assert queue_record is not None and queue_record["state"] == "completed"
            assert queue_record["finalization_payload"] is None
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["terminal", "correctable"])
async def test_non_transient_adcp_error_round_trips_exact_wire_payload(recovery) -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"
    account_id = "tenant-a:account-a"
    error = AdcpError(
        "POLICY_VIOLATION",
        message="Approval was denied",
        recovery=recovery,
        field="packages[0]",
        suggestion="Choose a different package",
        details={"policy_id": "policy-7"},
    )

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=4, open=False
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)
        queue = PgWorkflowQueue(pool=pool, registry=registry, table=workflow_table)
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = await registry.issue(account_id=account_id, task_type="create_media_buy")
            await queue.enqueue(
                task_id=task_id,
                account_id=account_id,
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )

            async def handler(_job):
                raise error

            assert await queue.process_one(handler) is True
            task = await registry.get(task_id, expected_account_id=account_id)
            queue_record = await queue.get(task_id)
            assert task is not None
            assert task["state"] == "failed"
            assert task["error"] == error.to_wire()
            assert queue_record is not None
            assert queue_record["state"] == "dead_lettered"
            assert queue_record["attempt_count"] == 1
            assert queue_record["finalization_payload"] is None
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_heartbeat_prevents_reclaim_while_handler_is_live() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"
    account_id = "tenant-a:account-a"

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=6, open=False
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)
        queue = PgWorkflowQueue(pool=pool, registry=registry, table=workflow_table, lease_seconds=2)
        contender = PgWorkflowQueue(
            pool=pool, registry=registry, table=workflow_table, lease_seconds=2
        )
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = await registry.issue(account_id=account_id, task_type="create_media_buy")
            await queue.enqueue(
                task_id=task_id,
                account_id=account_id,
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )
            started = asyncio.Event()
            finish = asyncio.Event()

            async def handler(_job):
                started.set()
                await finish.wait()
                return {"status": "active"}

            processing = asyncio.create_task(queue.process_one(handler))
            await asyncio.wait_for(started.wait(), timeout=2)
            # Make the initial lease expire before the first heartbeat. The
            # heartbeat must renew it before the contender's claim.
            async with pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} SET lease_expires_at = "  # noqa: S608
                    "now() + interval '0.2 seconds' WHERE task_id = %s",
                    (task_id,),
                )
            await asyncio.sleep(0.9)
            assert await contender.claim() is None
            finish.set()
            assert await asyncio.wait_for(processing, timeout=2) is True
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_worker_cancellation_releases_lease_and_cancels_handler() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    workflow_table = f"test_workflows_{suffix}"
    account_id = "tenant-a:account-a"

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=4, open=False
    ) as pool:
        await pool.open()
        registry = PgTaskRegistry(pool=pool, _table=task_table)
        queue = PgWorkflowQueue(
            pool=pool,
            registry=registry,
            table=workflow_table,
            lease_seconds=2,
            retry_base_seconds=1,
        )
        await registry.create_schema()
        await queue.create_schema()
        try:
            task_id = await registry.issue(account_id=account_id, task_type="create_media_buy")
            await queue.enqueue(
                task_id=task_id,
                account_id=account_id,
                workflow_type="manual_media_buy_approval",
                payload={"upstream_order_id": "order-1"},
            )
            started = asyncio.Event()
            handler_cancelled = asyncio.Event()

            async def blocked_handler(_job):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    handler_cancelled.set()

            processing = asyncio.create_task(queue.process_one(blocked_handler))
            await asyncio.wait_for(started.wait(), timeout=2)
            processing.cancel()
            with pytest.raises(asyncio.CancelledError):
                _ = await processing
            assert handler_cancelled.is_set()
            released = await queue.get(task_id)
            assert released is not None
            assert released["state"] == "pending"
            assert released["attempt_count"] == 1

            async with pool.connection() as conn:
                await conn.execute(
                    f"UPDATE {workflow_table} SET available_at = now() WHERE task_id = %s",  # noqa: S608
                    (task_id,),
                )

            async def replacement_handler(_job):
                return {"status": "active"}

            assert await queue.process_one(replacement_handler) is True
            task = await registry.get(task_id, expected_account_id=account_id)
            assert task is not None and task["state"] == "completed"
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608
