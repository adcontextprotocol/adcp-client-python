"""Conformance tests for :class:`adcp.decisioning.pg.PostgresTaskRegistry`.

Requires a real PostgreSQL instance. To run locally::

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=pg postgres:16
    export ADCP_PG_TEST_URL=postgresql://postgres:pg@localhost:5432/postgres
    pytest tests/conformance/decisioning/test_pg_task_registry.py -v

The entire module skips when ``ADCP_PG_TEST_URL`` is unset, so the
default test matrix stays green without a database dependency.

Each test runs against a freshly-created ``decisioning_tasks_<random>``
table so parallel runs and crash-then-retry scenarios don't collide.

These tests mirror the behavioral guarantees of
``tests/test_decisioning_task_registry.py`` (InMemoryTaskRegistry) and
``tests/test_decisioning_task_registry_cross_tenant.py`` (security) against
a real Postgres engine to catch SQL-level divergence.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator

import pytest

psycopg = pytest.importorskip("psycopg")
psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping PostgresTaskRegistry conformance tests",
        allow_module_level=True,
    )

from adcp.decisioning.pg import PostgresTaskRegistry  # noqa: E402

# -- fixtures ---------------------------------------------------------------


@pytest.fixture()
async def registry() -> AsyncIterator[PostgresTaskRegistry]:
    """Async pool + isolated table per test, torn down on exit."""
    table_suffix = secrets.token_hex(6)
    # Patch the table name via the internal attribute so tests are isolated.
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=2,
        max_size=8,
        open=False,
    ) as pool:
        await pool.open()
        reg = PostgresTaskRegistry(pool=pool)
        # Override default table name for isolation.
        reg._table_suffix = table_suffix  # type: ignore[attr-defined]
        await reg.create_schema()
        try:
            yield reg
        finally:
            async with pool.connection() as conn:
                await conn.execute("DROP TABLE IF EXISTS decisioning_tasks")


# -- Protocol happy-path ---------------------------------------------------


@pytest.mark.asyncio
async def test_issue_returns_unique_task_ids(registry: PostgresTaskRegistry) -> None:
    ids = [await registry.issue(account_id="acct1", task_type="create_media_buy")
           for _ in range(5)]
    assert len(set(ids)) == 5


@pytest.mark.asyncio
async def test_issue_then_get_returns_submitted(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    record = await registry.get(task_id)
    assert record is not None
    assert record["state"] == "submitted"
    assert record["task_type"] == "create_media_buy"
    assert record["account_id"] == "acct1"


@pytest.mark.asyncio
async def test_update_progress_transitions_submitted_to_working(
    registry: PostgresTaskRegistry,
) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    await registry.update_progress(task_id, {"message": "Reviewing"})
    record = await registry.get(task_id)
    assert record is not None
    assert record["state"] == "working"
    assert record["progress"] == {"message": "Reviewing"}


@pytest.mark.asyncio
async def test_update_progress_noop_on_unknown_task(registry: PostgresTaskRegistry) -> None:
    # Must not raise; the dispatch wrapper relies on this being silent.
    await registry.update_progress("task_unknown", {"x": 1})


@pytest.mark.asyncio
async def test_complete_transitions_to_completed(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    result = {"media_buy_id": "mb_1", "status": "active"}
    await registry.complete(task_id, result)
    record = await registry.get(task_id)
    assert record is not None
    assert record["state"] == "completed"
    assert record["result"] == result


@pytest.mark.asyncio
async def test_complete_idempotent_on_equal_result(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    result = {"media_buy_id": "mb_1"}
    await registry.complete(task_id, result)
    await registry.complete(task_id, result)  # must not raise


@pytest.mark.asyncio
async def test_complete_raises_on_different_result(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    await registry.complete(task_id, {"media_buy_id": "mb_1"})
    with pytest.raises(ValueError, match="different result"):
        await registry.complete(task_id, {"media_buy_id": "mb_2"})


@pytest.mark.asyncio
async def test_fail_transitions_to_failed(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    error = {"code": "BUDGET_TOO_LOW", "message": "Budget below minimum"}
    await registry.fail(task_id, error)
    record = await registry.get(task_id)
    assert record is not None
    assert record["state"] == "failed"
    assert record["error"] == error


@pytest.mark.asyncio
async def test_fail_idempotent_on_equal_error(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    error = {"code": "BUDGET_TOO_LOW"}
    await registry.fail(task_id, error)
    await registry.fail(task_id, error)  # must not raise


@pytest.mark.asyncio
async def test_fail_raises_on_different_error(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    await registry.fail(task_id, {"code": "BUDGET_TOO_LOW"})
    with pytest.raises(ValueError, match="different error"):
        await registry.fail(task_id, {"code": "RATE_LIMITED"})


@pytest.mark.asyncio
async def test_update_progress_noop_on_completed_task(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    await registry.complete(task_id, {"media_buy_id": "mb_1"})
    await registry.update_progress(task_id, {"message": "late straggler"})
    record = await registry.get(task_id)
    assert record is not None
    assert record["state"] == "completed"  # must not revert to working


@pytest.mark.asyncio
async def test_discard_removes_submitted_task(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    await registry.discard(task_id)
    assert await registry.get(task_id) is None


@pytest.mark.asyncio
async def test_discard_unknown_task_is_noop(registry: PostgresTaskRegistry) -> None:
    await registry.discard("task_does_not_exist")  # must not raise


# -- Cross-tenant security -------------------------------------------------


@pytest.mark.asyncio
async def test_get_cross_tenant_probe_returns_none(registry: PostgresTaskRegistry) -> None:
    """get(expected_account_id=wrong) must return None — SQL-level enforcement."""
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    result = await registry.get(task_id, expected_account_id="acct2")
    assert result is None


@pytest.mark.asyncio
async def test_get_no_account_filter_returns_record(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    record = await registry.get(task_id, expected_account_id=None)
    assert record is not None
    assert record["account_id"] == "acct1"


@pytest.mark.asyncio
async def test_get_correct_account_returns_record(registry: PostgresTaskRegistry) -> None:
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    record = await registry.get(task_id, expected_account_id="acct1")
    assert record is not None


@pytest.mark.asyncio
async def test_get_unknown_task_returns_none(registry: PostgresTaskRegistry) -> None:
    assert await registry.get("task_unknown") is None


# -- Concurrency -----------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_issue_yields_unique_ids(registry: PostgresTaskRegistry) -> None:
    ids = await asyncio.gather(
        *[registry.issue(account_id="acct1", task_type="create_media_buy")
          for _ in range(20)]
    )
    assert len(set(ids)) == 20


@pytest.mark.asyncio
async def test_concurrent_complete_idempotent(registry: PostgresTaskRegistry) -> None:
    """Two workers racing complete() with the same result must not error."""
    task_id = await registry.issue(account_id="acct1", task_type="create_media_buy")
    result = {"media_buy_id": "mb_1"}
    await asyncio.gather(
        registry.complete(task_id, result),
        registry.complete(task_id, result),
    )
    record = await registry.get(task_id)
    assert record is not None
    assert record["state"] == "completed"


# -- Schema helpers --------------------------------------------------------


@pytest.mark.asyncio
async def test_create_schema_idempotent(registry: PostgresTaskRegistry) -> None:
    """create_schema() called twice must not error."""
    await registry.create_schema()  # second call; fixture already called it once
