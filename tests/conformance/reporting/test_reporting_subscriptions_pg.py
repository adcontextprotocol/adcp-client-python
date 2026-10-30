"""PostgreSQL stores for AdCP 3.3 reporting subscriptions (#1695).

Requires a real PostgreSQL instance (``ADCP_PG_TEST_URL``); the module skips
without one. Each test uses a fresh schema, so runs never collide.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator

import pytest

psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip("ADCP_PG_TEST_URL not set", allow_module_level=True)

from adcp_reporting_storage import (  # noqa: E402
    PgPrincipalRecordStore,
    PgReportingSubscriptionStateStore,
)

from adcp.reporting.subscriptions import ReportingSubscriptionEngine  # noqa: E402
from adcp.server import PrincipalService  # noqa: E402
from tests.test_reporting_subscription_engine import (  # noqa: E402
    IDENTITY,
    MANAGED,
    OFFERINGS,
    World,
)
from tests.test_reporting_subscription_state_store import (  # noqa: E402
    CAPACITY,
    _observed,
    _scenario,
)


@pytest.fixture
async def pool() -> AsyncIterator[psycopg_pool.AsyncConnectionPool]:
    schema = f"test_subs_{secrets.token_hex(6)}"
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=8, open=False, kwargs={"autocommit": True}
    ) as admin:
        async with admin.connection() as connection:
            await connection.execute(f"CREATE SCHEMA {schema}")
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=1,
        max_size=8,
        open=False,
        kwargs={"autocommit": True, "options": f"-c search_path={schema}"},
    ) as scoped:
        yield scoped
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=2, open=False, kwargs={"autocommit": True}
    ) as admin:
        async with admin.connection() as connection:
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")


def _world(pool) -> World:
    world = World(support=CAPACITY)
    records = PgPrincipalRecordStore(pool=pool)
    world.principals = PrincipalService(store=records, emit_change=_record(world))
    world.engine = ReportingSubscriptionEngine(
        principals=world.principals,
        host=world.host,
        offerings=OFFERINGS,
        support=CAPACITY,
        store=PgReportingSubscriptionStateStore(pool=pool),
        clock=world.clock,
    )
    world.principals.set_reporting_subscriptions(world.engine.principal_options())
    return world


def _record(world: World):
    async def emit(change) -> None:
        world.changes.append(change)

    return emit


async def test_schema_creation_is_idempotent(pool) -> None:
    store = PgReportingSubscriptionStateStore(pool=pool)
    await store.create_schema()
    await store.create_schema()


async def test_state_compare_and_swap(pool) -> None:
    store = PgReportingSubscriptionStateStore(pool=pool)
    await store.create_schema()
    assert await store.load("p") is None
    assert await store.compare_and_swap("p", None, {"a": 1})
    assert not await store.compare_and_swap("p", None, {"a": 2})
    stored = await store.load("p")
    assert stored is not None and (stored.document, stored.revision) == ({"a": 1}, 1)
    assert not await store.compare_and_swap("p", 7, {"a": 3})
    assert await store.compare_and_swap("p", 1, {"a": 4})
    stored = await store.load("p")
    assert stored is not None and (stored.document, stored.revision) == ({"a": 4}, 2)


async def test_concurrent_writers_never_lose_a_change(pool) -> None:
    store = PgReportingSubscriptionStateStore(pool=pool)
    await store.create_schema()

    async def bump() -> None:
        while True:
            stored = await store.load("p")
            count = 0 if stored is None else stored.document["count"]
            if await store.compare_and_swap(
                "p", None if stored is None else stored.revision, {"count": count + 1}
            ):
                return

    await asyncio.gather(*(bump() for _ in range(20)))
    stored = await store.load("p")
    assert stored is not None and stored.document == {"count": 20} and stored.revision == 20


async def test_a_restarted_seller_on_postgres_owes_the_same_periods(pool) -> None:
    await PgReportingSubscriptionStateStore(pool=pool).create_schema()
    world = _world(pool)
    await _scenario(world)
    before = await _observed(world, world.engine)
    readback = await world.principals.get_principal(IDENTITY)

    restarted_principals = PrincipalService(
        store=PgPrincipalRecordStore(pool=pool), emit_change=_record(world)
    )
    restarted = ReportingSubscriptionEngine(
        principals=restarted_principals,
        host=world.host,
        offerings=(*OFFERINGS, MANAGED),
        support=CAPACITY,
        store=PgReportingSubscriptionStateStore(pool=pool),
        clock=world.clock,
    )
    restarted_principals.set_reporting_subscriptions(restarted.principal_options())
    assert await restarted_principals.get_principal(IDENTITY) == readback
    assert await _observed(world, restarted) == before
    assert before["owed"][("files", "acct-1")]
