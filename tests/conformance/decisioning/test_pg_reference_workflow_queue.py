"""Restart-recovery test for the reference PostgreSQL WorkflowHandoff queue.

Set ``ADCP_PG_TEST_URL`` to run this test against PostgreSQL.
"""

from __future__ import annotations

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

from adcp.decisioning import Account, PgTaskRegistry, RequestContext  # noqa: E402
from adcp.decisioning.dispatch import _project_workflow_handoff  # noqa: E402
from src.workflow_queue import PgWorkflowQueue  # noqa: E402


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
                assert first_claim.attempt_count == 1

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
                    assert job.attempt_count == 2
                    return job.payload["result"]

                assert await replacement_queue.process_one(complete) is True
                queue_record = await replacement_queue.get(task_id)
                task_record = await replacement_registry.get(
                    task_id,
                    expected_account_id=account_id,
                )

            assert queue_record is not None
            assert queue_record["state"] == "completed"
            assert queue_record["attempt_count"] == 2
            assert task_record is not None
            assert task_record["state"] == "completed"
            assert task_record["result"] == expected_result
            assert task_record["context"] == {"trace_id": "restart-test"}
        finally:
            executor.shutdown(wait=True)
            async with web_pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {workflow_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608
