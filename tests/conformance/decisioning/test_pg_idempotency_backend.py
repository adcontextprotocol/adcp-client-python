"""Conformance tests for :class:`adcp.server.idempotency.PgBackend`.

Requires a real PostgreSQL instance. To run locally::

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=pg postgres:16
    export ADCP_PG_TEST_URL=postgresql://postgres:pg@localhost:5432/postgres
    pytest tests/conformance/decisioning/test_pg_idempotency_backend.py -v

The entire module skips when ``ADCP_PG_TEST_URL`` is unset, so the
default test matrix stays green without a database dependency. CI runs
this in the same Postgres-16 job as the PgReplayStore tests.

Each test runs in an isolated table (``test_adcp_idem_<random>``) so
parallel runs and rerun-after-crash don't collide.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

psycopg = pytest.importorskip("psycopg")
psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping PgBackend conformance tests",
        allow_module_level=True,
    )

from adcp.server.idempotency import IdempotencyStore  # noqa: E402
from adcp.server.idempotency.backends import CachedResponse, PgBackend  # noqa: E402


@pytest_asyncio.fixture()
async def isolated_backend() -> AsyncIterator[PgBackend]:
    """Fresh async pool + isolated table per test. Drops on teardown."""
    table = f"test_adcp_idem_{secrets.token_hex(6)}"
    async with (
        psycopg_pool.AsyncConnectionPool(TEST_URL, min_size=2, max_size=8) as pool,
        psycopg_pool.AsyncConnectionPool(TEST_URL, min_size=2, max_size=8) as lock_pool,
    ):
        backend = PgBackend(pool=pool, lock_pool=lock_pool, table_name=table)
        await backend.create_schema()
        try:
            yield backend
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")


# ----- create_schema bootstrap ------------------------------------------


@pytest.mark.asyncio
async def test_create_schema_is_idempotent(isolated_backend: PgBackend) -> None:
    """Safe to call multiple times — ``CREATE TABLE IF NOT EXISTS``."""
    await isolated_backend.create_schema()
    await isolated_backend.create_schema()


# ----- get / put round-trip ---------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_key(isolated_backend: PgBackend) -> None:
    assert await isolated_backend.get("scope-x", "key-1") is None


@pytest.mark.asyncio
async def test_put_then_get_round_trips(isolated_backend: PgBackend) -> None:
    entry = CachedResponse(
        payload_hash="hash-abc",
        response={"status": "ok", "task_id": "task-1"},
        expires_at_epoch=time.time() + 3600,
    )
    await isolated_backend.put("scope-a", "key-1", entry)

    cached = await isolated_backend.get("scope-a", "key-1")
    assert cached is not None
    assert cached.payload_hash == "hash-abc"
    assert cached.response == {"status": "ok", "task_id": "task-1"}
    # Allow ~1s of round-trip / clock-skew slack.
    assert abs(cached.expires_at_epoch - entry.expires_at_epoch) < 2.0


@pytest.mark.asyncio
async def test_put_against_fresh_slot_is_noop(isolated_backend: PgBackend) -> None:
    """First-writer-wins: a sequential put against a non-expired slot
    is a no-op. The cache invariant ('same key → same hash') depends
    on the first writer's row staying put."""
    first = CachedResponse(
        payload_hash="hash-1", response={"a": 1}, expires_at_epoch=time.time() + 3600
    )
    await isolated_backend.put("scope", "key", first)

    second = CachedResponse(
        payload_hash="hash-2", response={"a": 2}, expires_at_epoch=time.time() + 7200
    )
    await isolated_backend.put("scope", "key", second)

    cached = await isolated_backend.get("scope", "key")
    assert cached is not None
    # First writer wins — second put's UPDATE arm was filtered out by
    # the WHERE expires_at <= now() guard.
    assert cached.payload_hash == "hash-1"
    assert cached.response == {"a": 1}


@pytest.mark.asyncio
async def test_scope_key_isolation(isolated_backend: PgBackend) -> None:
    """Same key under different scopes must not collide."""
    e1 = CachedResponse(
        payload_hash="h1", response={"who": "a"}, expires_at_epoch=time.time() + 3600
    )
    e2 = CachedResponse(
        payload_hash="h2", response={"who": "b"}, expires_at_epoch=time.time() + 3600
    )
    await isolated_backend.put("scope-a", "shared-key", e1)
    await isolated_backend.put("scope-b", "shared-key", e2)

    assert (await isolated_backend.get("scope-a", "shared-key")).response == {"who": "a"}
    assert (await isolated_backend.get("scope-b", "shared-key")).response == {"who": "b"}


# ----- expiry -----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_filters_expired_entries(isolated_backend: PgBackend) -> None:
    """``expires_at <= now()`` rows must NOT be returned by ``get``."""
    expired = CachedResponse(payload_hash="h", response={"v": 1}, expires_at_epoch=time.time() - 1)
    await isolated_backend.put("scope", "key", expired)
    assert await isolated_backend.get("scope", "key") is None


@pytest.mark.asyncio
async def test_delete_expired_removes_only_expired(isolated_backend: PgBackend) -> None:
    fresh = CachedResponse(payload_hash="h-fresh", response={}, expires_at_epoch=time.time() + 3600)
    stale = CachedResponse(payload_hash="h-stale", response={}, expires_at_epoch=time.time() - 60)
    await isolated_backend.put("scope", "fresh", fresh)
    await isolated_backend.put("scope", "stale", stale)

    deleted = await isolated_backend.delete_expired()
    assert deleted == 1
    # Fresh remains queryable after the sweep.
    assert (await isolated_backend.get("scope", "fresh")).payload_hash == "h-fresh"


# ----- concurrent put under ON CONFLICT ---------------------------------


@pytest.mark.asyncio
async def test_concurrent_put_first_writer_wins(isolated_backend: PgBackend) -> None:
    """``ON CONFLICT (scope_key, key) DO UPDATE … WHERE expires_at <= now()``
    means a second concurrent put against a fresh slot is a no-op —
    the first writer's payload_hash + response remain. Without the
    WHERE guard this would be last-writer-wins and violate the
    cache invariant 'same key → same hash'."""
    import asyncio

    e1 = CachedResponse(
        payload_hash="hash-A", response={"v": "A"}, expires_at_epoch=time.time() + 3600
    )
    e2 = CachedResponse(
        payload_hash="hash-B", response={"v": "B"}, expires_at_epoch=time.time() + 3600
    )

    # Two concurrent writers racing the same (scope, key). The first to
    # commit wins; the second's INSERT hits ON CONFLICT, the UPDATE
    # arm's WHERE rejects the overwrite (existing row not yet expired).
    await asyncio.gather(
        isolated_backend.put("scope", "key", e1),
        isolated_backend.put("scope", "key", e2),
    )

    cached = await isolated_backend.get("scope", "key")
    assert cached is not None
    # We don't assert WHICH writer won — only that the result is a
    # consistent first-writer-wins, not a torn read or last-writer.
    assert cached.payload_hash in {"hash-A", "hash-B"}
    assert cached.response in ({"v": "A"}, {"v": "B"})


@pytest.mark.asyncio
async def test_concurrent_put_overwrites_expired_row(isolated_backend: PgBackend) -> None:
    """A put against an expired slot DOES overwrite — the WHERE clause
    only blocks overwriting *fresh* rows."""
    expired = CachedResponse(
        payload_hash="h-expired",
        response={"v": "expired"},
        expires_at_epoch=time.time() - 60,
    )
    await isolated_backend.put("scope", "key", expired)

    fresh = CachedResponse(
        payload_hash="h-fresh",
        response={"v": "fresh"},
        expires_at_epoch=time.time() + 3600,
    )
    await isolated_backend.put("scope", "key", fresh)

    cached = await isolated_backend.get("scope", "key")
    assert cached is not None
    assert cached.payload_hash == "h-fresh"


# ----- IdempotencyStore composition -------------------------------------


@pytest.mark.asyncio
async def test_idempotency_store_replays_via_pg_backend(isolated_backend: PgBackend) -> None:
    """End-to-end: a wrapped handler returns from cache on a second call
    with the same scope + key + payload."""
    store = IdempotencyStore(backend=isolated_backend, ttl_seconds=3600)
    call_count = {"n": 0}

    @store.wrap
    async def handler(self, params, context=None):
        call_count["n"] += 1
        return {"task_id": f"task-{call_count['n']}", "status": "ok"}

    class Ctx:
        caller_identity = "buyer-acme"
        tenant_id = "tenant-1"

    params = {"idempotency_key": "key-1", "x": 42}
    r1 = await handler(None, params, Ctx())
    r2 = await handler(None, params, Ctx())

    assert call_count["n"] == 1  # second call replayed
    # AdCP L1/security rule 4 (#714): replay envelope carries ``replayed: true``.
    assert r2.get("replayed") is True
    assert {k: v for k, v in r2.items() if k != "replayed"} == r1


@pytest.mark.asyncio
async def test_concurrent_wrapped_calls_execute_once(isolated_backend: PgBackend) -> None:
    import asyncio

    store = IdempotencyStore(backend=isolated_backend, ttl_seconds=3600)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    @store.wrap
    async def handler(self, params, context=None):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"task_id": "only", "status": "ok"}

    class Ctx:
        caller_identity = "buyer-acme"
        tenant_id = "tenant-1"

    params = {"idempotency_key": "shared-key", "x": 42}
    first = asyncio.create_task(handler(None, params, Ctx()))
    await entered.wait()
    second = asyncio.create_task(handler(None, params, Ctx()))
    await asyncio.sleep(0.05)
    assert calls == 1
    release.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.get("replayed") is not True
    assert second_result["replayed"] is True
