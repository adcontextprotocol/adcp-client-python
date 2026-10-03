"""Best-effort metrics hooks count committed transitions, including races."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from typing import Any

import pytest

from adcp.decisioning import InMemoryTaskRegistry, TaskTransition


@pytest.fixture(params=["memory", "postgres", "postgres-autocommit"])
async def registry(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield InMemoryTaskRegistry()
        return
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        pytest.skip("ADCP_PG_TEST_URL required")
    from psycopg_pool import AsyncConnectionPool

    from adcp.decisioning.pg import PgTaskRegistry

    table = f"test_lifecycle_{secrets.token_hex(6)}"
    async with AsyncConnectionPool(
        url,
        open=False,
        min_size=1,
        max_size=4,
        kwargs={"autocommit": request.param == "postgres-autocommit"},
    ) as pool:
        reg = PgTaskRegistry(pool=pool, _table=table)
        await reg.create_schema()
        yield reg
        async with pool.connection() as conn:
            await conn.execute(f"DROP TABLE {table}")


async def test_observe_committed_transitions_and_suppress_noops(registry: Any) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def observe(event: TaskTransition, **metadata: Any) -> None:
        # A synchronous callback can inspect committed state on another connection.
        # It cannot observe an uncommitted row or run under the memory registry lock.
        if isinstance(registry, InMemoryTaskRegistry):
            assert not registry._lock.locked()
            record = registry._records.get(metadata["task_id"])
            assert record is None if event == "discarded" else record.state == event
        else:
            import psycopg

            with psycopg.connect(os.environ["ADCP_PG_TEST_URL"]) as conn:
                row = conn.execute(
                    f"SELECT state FROM {registry._table} WHERE task_id = %s",
                    (metadata["task_id"],),
                ).fetchone()
                assert row is None if event == "discarded" else row[0] == event
        events.append((event, metadata))

    registry.add_lifecycle_observer(observe)
    registry.add_lifecycle_observer(observe)
    task = await registry.issue(account_id="acct_1", task_type="get_products")
    await asyncio.gather(*(registry.update_progress(task, {"percentage": n}) for n in range(10)))
    await asyncio.gather(*(registry.complete(task, {"products": []}) for _ in range(10)))
    await registry.update_progress(task, {"percentage": 100})
    await registry.update_progress("absent", {})
    await registry.discard("absent")
    await registry.discard(task)
    await registry.discard(task)
    assert [event for event, _ in events] == ["submitted", "working", "completed", "discarded"]
    for _, metadata in events:
        assert metadata["task_id"] == task
        assert metadata["account_id"] == "acct_1"
        assert metadata["task_type"] == "get_products"
        assert metadata["created_at"] <= metadata["updated_at"]
    assert registry.remove_lifecycle_observer(observe) is True
    assert registry.remove_lifecycle_observer(observe) is False
    await registry.issue(account_id="acct_1", task_type="get_products")
    assert len(events) == 4


async def test_racing_terminal_transitions_emit_once(registry: Any) -> None:
    events = []

    def observe(event: TaskTransition, **metadata: Any) -> None:
        events.append(event)

    registry.add_lifecycle_observer(observe)
    task = await registry.issue(account_id="acct", task_type="get_products")
    results = await asyncio.gather(
        *(registry.complete(task, {"products": []}) for _ in range(10)),
        *(registry.fail(task, {"code": "INTERNAL_ERROR", "message": "Failed"}) for _ in range(10)),
        return_exceptions=True,
    )
    assert events[0] == "submitted"
    assert len(events) == 2 and events[1] in {"completed", "failed"}
    assert any(isinstance(result, ValueError) for result in results)
    record = await registry.get(task)
    assert record["state"] == events[1]
    if record["state"] == "completed":
        with pytest.raises(ValueError):
            await registry.fail(task, {"code": "CONFLICT", "message": "Conflict"})
    else:
        with pytest.raises(ValueError):
            await registry.complete(task, {"products": []})
    assert len(events) == 2


async def test_observer_failure_and_snapshot_removal(
    registry: Any, caplog: pytest.LogCaptureFixture
) -> None:
    events = []

    def broken(event: TaskTransition, **metadata: Any) -> None:
        raise RuntimeError("Metrics unavailable")

    def removed(event: TaskTransition, **metadata: Any) -> None:
        events.append(event)

    def remover(event: TaskTransition, **metadata: Any) -> None:
        registry.remove_lifecycle_observer(removed)

    registry.add_lifecycle_observer(broken)
    registry.add_lifecycle_observer(remover)
    registry.add_lifecycle_observer(removed)
    task = await registry.issue(account_id="acct", task_type="get_products")
    await registry.fail(task, {"code": "INTERNAL_ERROR", "message": "Failed"})
    await registry.fail(task, {"code": "INTERNAL_ERROR", "message": "Failed"})
    assert events == ["submitted"]
    assert "Task lifecycle observer failed" in caplog.text
    assert (await registry.get(task))["state"] == "failed"


async def test_failed_database_transaction_never_notifies(registry: Any) -> None:
    if isinstance(registry, InMemoryTaskRegistry):
        with pytest.raises(ValueError):
            await registry.complete("missing", {})
        return
    import psycopg

    events = []
    registry.add_lifecycle_observer(lambda event, **metadata: events.append(event))
    task = await registry.issue(account_id="acct", task_type="get_products")
    function = f"{registry._table}_reject"
    async with registry._pool.connection() as conn:
        await conn.execute(
            f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'fail commit'; END $$"
        )
        await conn.execute(
            f"CREATE CONSTRAINT TRIGGER reject_transition AFTER UPDATE ON {registry._table}"
            f" DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            await registry.complete(task, {"products": []})
        assert events == ["submitted"]
        assert (await registry.get(task))["state"] == "submitted"
    finally:
        async with registry._pool.connection() as conn:
            await conn.execute(f"DROP TRIGGER reject_transition ON {registry._table}")
            await conn.execute(f"DROP FUNCTION {function}()")
