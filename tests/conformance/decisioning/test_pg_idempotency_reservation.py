"""Real PostgreSQL atomicity, lifecycle, and cross-process crash regressions."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import secrets
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

psycopg = pytest.importorskip("psycopg")
psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip("ADCP_PG_TEST_URL not set", allow_module_level=True)

from adcp.exceptions import IdempotencyConflictError
from adcp.server.idempotency import (
    IdempotencyReservationError,
    IdempotencyStore,
    PgBackend,
)


@pytest_asyncio.fixture
async def backend() -> AsyncIterator[PgBackend]:
    table = f"test_reservation_{secrets.token_hex(6)}"
    async with (
        psycopg_pool.AsyncConnectionPool(TEST_URL, min_size=1, max_size=4) as pool,
        psycopg_pool.AsyncConnectionPool(TEST_URL, min_size=1, max_size=4) as lock_pool,
    ):
        backend = PgBackend(pool=pool, lock_pool=lock_pool, table_name=table)
        await backend.create_schema()
        async with pool.connection() as conn:
            await conn.execute(f"CREATE TABLE {table}_business (id SERIAL PRIMARY KEY, key TEXT)")
        try:
            yield backend
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE {table}_business CASCADE")
                await conn.execute(f"DROP TABLE {table}")


async def counts(backend: PgBackend) -> tuple[int, int]:
    async with backend._pool.connection() as conn:
        business = await (
            await conn.execute(f"SELECT count(*) FROM {backend._table}_business")
        ).fetchone()
        replay = await (await conn.execute(f"SELECT count(*) FROM {backend._table}")).fetchone()
    return business[0], replay[0]


async def write_business(slot: Any, table: str) -> None:
    await slot.connection.execute(f"INSERT INTO {table}_business (key) VALUES ('key')")


async def test_business_and_replay_commit_together_and_replay_without_business(backend):
    store = IdempotencyStore(backend, ttl_seconds=3600)
    req, ctx = {"idempotency_key": "key", "budget": 10}, {"caller_identity": "buyer"}
    async with store.reserve(req, ctx) as slot:
        assert not slot.replayed
        await write_business(slot, backend._table)
        await slot.record({"media_buy_id": "one", "nested": [1]})
        assert slot.recorded
        assert await counts(backend) == (0, 0)  # another connection sees neither
    assert await counts(backend) == (1, 1)
    async with store.reserve(req, ctx) as replay:
        assert replay.replayed
        assert replay.response == {"media_buy_id": "one", "nested": [1], "replayed": True}
        replay.response["nested"].append(2)
        assert replay.response["nested"] == [1]
        with pytest.raises(IdempotencyReservationError):
            _ = replay.connection
        with pytest.raises(IdempotencyReservationError):
            await replay.record({})
    assert await counts(backend) == (1, 1)
    with pytest.raises(IdempotencyReservationError):
        await slot.record({})
    with pytest.raises(IdempotencyConflictError):
        async with store.reserve({**req, "budget": 11}, ctx):
            pytest.fail("conflicting payload must not execute")


async def test_request_scope_isolated_and_ttl_uses_database_clock(backend):
    # Deliberately wrong application clock cannot affect reservation expiry.
    store = IdempotencyStore(backend, ttl_seconds=3600, clock=lambda: 0)
    req = {"idempotency_key": "key"}
    for tenant in ("a", "b"):
        async with store.reserve(req, {"caller_identity": "buyer", "tenant_id": tenant}) as slot:
            await write_business(slot, backend._table)
            await slot.record({"tenant": tenant})
    assert await counts(backend) == (2, 2)
    async with backend._pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT min(EXTRACT(EPOCH FROM expires_at - clock_timestamp())) "
                f"FROM {backend._table}"
            )
        ).fetchone()
        assert 3590 < row[0] <= 3600


@pytest.mark.parametrize("failure", ["handler", "unrecorded", "serialization", "caught_persist"])
async def test_failed_operation_rolls_back_both_writes(backend, failure):
    with pytest.raises((RuntimeError, TypeError, psycopg.Error)):
        async with backend.reserve("buyer", "key", "hash") as slot:
            await write_business(slot, backend._table)
            if failure == "handler":
                await slot.record({"ok": True})
                raise RuntimeError("business failed")
            if failure == "serialization":
                await slot.record({"bad": object()})
            if failure == "caught_persist":
                original = backend._sql_put_if_absent
                backend._sql_put_if_absent = "SELECT 1/0"
                try:
                    with pytest.raises(psycopg.Error):
                        await slot.record({"ok": True})
                finally:
                    backend._sql_put_if_absent = original
    assert await counts(backend) == (0, 0)
    # Neither an unrecorded reservation nor a failed attempt leaves a durable claim.
    async with backend.reserve("buyer", "key", "hash") as retry:
        await write_business(retry, backend._table)
        await retry.record({"ok": True})
    assert await counts(backend) == (1, 1)


async def test_commit_failure_rolls_back_business_and_record(backend):
    async with backend._pool.connection() as conn:
        await conn.execute(f"ALTER TABLE {backend._table}_business ADD COLUMN parent INTEGER")
        await conn.execute(
            f"ALTER TABLE {backend._table}_business ADD CONSTRAINT missing_parent "
            f"FOREIGN KEY (parent) REFERENCES {backend._table}_business(id) "
            "DEFERRABLE INITIALLY DEFERRED"
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        async with backend.reserve("buyer", "key", "hash") as slot:
            await slot.connection.execute(
                f"INSERT INTO {backend._table}_business (key, parent) VALUES ('key', 9999)"
            )
            await slot.record({"ok": True})
    assert await counts(backend) == (0, 0)


async def test_outer_transaction_rejected_and_idle_caller_connection_supported(backend):
    async with backend._pool.connection() as conn:
        async with conn.transaction():
            with pytest.raises(IdempotencyReservationError, match="outer transaction"):
                async with backend.reserve("buyer", "key", "hash", connection=conn):
                    pytest.fail("must reject before business code")
        async with backend.reserve("buyer", "key", "hash", connection=conn) as slot:
            assert slot.connection is conn
            await write_business(slot, backend._table)
            await slot.record({"ok": True})
        assert not conn.closed
    assert await counts(backend) == (1, 1)
    with pytest.raises(TypeError, match="psycopg.AsyncConnection"):
        async with backend.reserve("buyer", "key", "hash", connection=object()):
            pass
    async with backend.hold("buyer", "key"):
        with pytest.raises(IdempotencyReservationError, match="cannot nest"):
            async with backend.reserve("buyer", "key", "hash"):
                pass


@pytest.mark.parametrize("shadow", ["schema", "temporary"])
async def test_caller_connection_cannot_shadow_the_backend_replay_table(backend, shadow):
    schema = "test_idem_shadow_" + secrets.token_hex(6)
    async with backend._pool.connection() as admin:
        original_schema = (await (await admin.execute("SELECT current_schema()")).fetchone())[0]
        await admin.execute(f"CREATE SCHEMA {schema}")
    conn = await psycopg.AsyncConnection.connect(TEST_URL, autocommit=True)
    try:
        qualified = f"{original_schema}.{backend._table}"
        if shadow == "schema":
            await conn.execute(
                f"CREATE TABLE {schema}.{backend._table} (LIKE {qualified} INCLUDING ALL)"
            )
            await conn.execute(f"SET search_path TO {schema}")
        else:
            await conn.execute(
                f"CREATE TEMP TABLE {backend._table} (LIKE {qualified} INCLUDING ALL)"
            )
        entered = False
        with pytest.raises(IdempotencyReservationError, match="replay table"):
            async with backend.reserve("buyer", "key", "hash", connection=conn):
                entered = True
        assert not entered
        assert await counts(backend) == (0, 0)
        # The rejected shadow reservation cannot commit a business effect that
        # a retry through the backend's ordinary pool would repeat.
        async with backend.reserve("buyer", "key", "hash") as retry:
            await write_business(retry, backend._table)
            await retry.record({"ok": True})
        async with backend.reserve("buyer", "key", "hash") as replay:
            assert replay.replayed
        assert await counts(backend) == (1, 1)
    finally:
        await conn.close()
        async with backend._pool.connection() as admin:
            await admin.execute(f"DROP SCHEMA {schema} CASCADE")


async def test_savepoint_rollback_of_record_is_detected(backend):
    with pytest.raises(IdempotencyReservationError, match="rolled back or changed"):
        async with backend.reserve("buyer", "key", "hash") as slot:
            await write_business(slot, backend._table)
            async with slot.connection.transaction(force_rollback=True):
                await slot.record({"ok": True})
    assert await counts(backend) == (0, 0)
    # Completed nested business savepoints are supported.
    async with backend.reserve("buyer", "key", "hash") as slot:
        async with slot.connection.transaction(force_rollback=True):
            await write_business(slot, backend._table)
        await write_business(slot, backend._table)
        await slot.record({"ok": True})
    assert await counts(backend) == (1, 1)


async def test_explicit_rollback_is_not_reported_as_success(backend):
    with pytest.raises(IdempotencyReservationError, match="explicitly rolled back"):
        async with backend.reserve("buyer", "key", "hash") as slot:
            await write_business(slot, backend._table)
            await slot.record({"ok": True})
            raise psycopg.Rollback()
    assert await counts(backend) == (0, 0)


async def test_cancellation_rolls_back_and_releases_lock(backend):
    recorded = asyncio.Event()

    async def attempt():
        async with backend.reserve("buyer", "key", "hash") as slot:
            await write_business(slot, backend._table)
            await slot.record({"ok": True})
            recorded.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(attempt())
    await asyncio.wait_for(recorded.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await counts(backend) == (0, 0)

    async def retry():
        async with backend.reserve("buyer", "key", "hash") as slot:
            await write_business(slot, backend._table)
            await slot.record({"ok": True})

    await asyncio.wait_for(retry(), 5)
    assert await counts(backend) == (1, 1)


async def test_reservation_cannot_be_used_from_inherited_child_task(backend):
    async with backend.reserve("buyer", "key", "hash") as slot:

        async def child():
            await slot.record({"bad": True})

        with pytest.raises(IdempotencyReservationError, match="owning task"):
            await asyncio.create_task(child())
        await write_business(slot, backend._table)
        await slot.record({"ok": True})


# Each worker owns independent pools/connections and a separate process. Pipe
# checkpoints mark actual PostgreSQL execution/commit, never a fake transaction.
def process_worker(url, table, pipe, pause):
    from contextlib import asynccontextmanager

    class CommitCheckpointConnection(psycopg.AsyncConnection):
        @asynccontextmanager
        async def transaction(self, *args, **kwargs):
            async with super().transaction(*args, **kwargs) as tx:
                yield tx
            if pause == "after_commit":
                pipe.send("committed")
                while not pipe.poll():
                    await asyncio.sleep(0.01)
                pipe.recv()

    async def run():
        async with (
            psycopg_pool.AsyncConnectionPool(
                url, min_size=1, max_size=1, connection_class=CommitCheckpointConnection
            ) as pool,
            psycopg_pool.AsyncConnectionPool(url, min_size=1, max_size=1) as lock_pool,
        ):
            backend = PgBackend(pool=pool, lock_pool=lock_pool, table_name=table)
            pipe.send("ready")
            async with backend.reserve("buyer", "key", "hash") as slot:
                if slot.replayed:
                    result = slot.response
                else:
                    await write_business(slot, table)
                    await slot.record({"media_buy_id": "one"})
                    if pause == "before_commit":
                        pipe.send("recorded")
                        while not pipe.poll():
                            await asyncio.sleep(0.01)
                        pipe.recv()
                    result = slot.response
            pipe.send(result)

    try:
        asyncio.run(run())
    except BaseException as exc:
        pipe.send((type(exc).__name__, str(exc)))
        raise
    finally:
        pipe.close()


async def receive(pipe):
    async def wait():
        while not pipe.poll():
            await asyncio.sleep(0.02)
        return pipe.recv()

    return await asyncio.wait_for(wait(), 120)


async def wait_for_advisory_waiter(backend):
    async def wait():
        while True:
            async with backend._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                        "AND database = (SELECT oid FROM pg_database "
                        "WHERE datname = current_database())"
                    )
                ).fetchone()
            if row[0]:
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait(), 15)


@pytest.mark.parametrize("pause", ["before_commit", "after_commit", "normal_release"])
async def test_separate_process_retry_and_termination_at_commit_boundary(backend, pause):
    ctx = multiprocessing.get_context("spawn")
    parent1, child1 = ctx.Pipe()
    parent2, child2 = ctx.Pipe()
    first = ctx.Process(
        target=process_worker,
        args=(
            TEST_URL,
            backend._table,
            child1,
            "before_commit" if pause == "normal_release" else pause,
        ),
    )
    second = ctx.Process(target=process_worker, args=(TEST_URL, backend._table, child2, "none"))
    try:
        first.start()
        assert await receive(parent1) == "ready"
        checkpoint = await receive(parent1)
        assert checkpoint == ("committed" if pause == "after_commit" else "recorded")
        assert await counts(backend) == ((1, 1) if pause == "after_commit" else (0, 0))
        second.start()
        assert await receive(parent2) == "ready"
        await wait_for_advisory_waiter(backend)
        assert (
            not parent2.poll()
        )  # lock still spans recorded/uncommitted *and* committed checkpoints
        if pause == "normal_release":
            parent1.send("release")
            assert await receive(parent1) == {"media_buy_id": "one"}
        else:
            first.kill()  # controlled SIGKILL: no Python cleanup or graceful rollback
            await asyncio.to_thread(first.join, 5)
            assert first.exitcode is not None and first.exitcode < 0
        result = await receive(parent2)
        assert result == (
            {"media_buy_id": "one"}
            if pause == "before_commit"
            else {"media_buy_id": "one", "replayed": True}
        )
        assert await counts(backend) == (1, 1)
    finally:
        for process in (first, second):
            if process.pid is not None:
                if process.is_alive():
                    process.kill()
                await asyncio.to_thread(process.join, 5)
        for pipe in (parent1, child1, parent2, child2):
            pipe.close()


@pytest.mark.parametrize("switch_at", ["before_record", "after_record"])
async def test_replay_table_cannot_change_during_business_transaction(backend, switch_at):
    schema = f"test_shadow_{secrets.token_hex(6)}"
    async with backend._pool.connection() as conn:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(
            f"CREATE TABLE {schema}.{backend._table} (LIKE public.{backend._table} INCLUDING ALL)"
        )
    try:
        with pytest.raises(IdempotencyReservationError, match="replay table changed"):
            async with backend.reserve("buyer", "key", "hash") as slot:
                await write_business(slot, backend._table)
                if switch_at == "after_record":
                    await slot.record({"ok": True})
                await slot.connection.execute(f"SET LOCAL search_path TO {schema}, public")
                if switch_at == "before_record":
                    await slot.record({"ok": True})
        assert await counts(backend) == (0, 0)
        async with backend._pool.connection() as conn:
            row = await (
                await conn.execute(f"SELECT count(*) FROM {schema}.{backend._table}")
            ).fetchone()
            assert row == (0,)
        async with backend.reserve("buyer", "key", "hash") as retry:
            assert not retry.replayed
            await write_business(retry, backend._table)
            await retry.record({"ok": True})
        async with backend.reserve("buyer", "key", "hash") as replay:
            assert replay.replayed
        assert await counts(backend) == (1, 1)
    finally:
        async with backend._pool.connection() as conn:
            await conn.execute(f"DROP SCHEMA {schema} CASCADE")
