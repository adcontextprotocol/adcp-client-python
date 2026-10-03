"""Lazy PostgreSQL infrastructure resolves once without weakening store contracts."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp.decisioning import ListableTaskRegistry, TaskRegistry
from adcp.decisioning.pg import (
    LazyProposalStore,
    LazyTaskRegistry,
    LazyTaskWebhookOutbox,
    PgProposalStore,
    PgTaskRegistry,
    PgTaskWebhookOutbox,
)
from adcp.decisioning.webhook_emit import _sdk_task_outbox_pair_ready
from adcp.webhook_sender import PreparedWebhook, WebhookDeliveryResult


@pytest.fixture
def fake_pg_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake pools never call psycopg; retain the real constructor validation.

    This explicit fixture enables only the optional-driver availability guards
    for unit cases, following the existing task-outbox unit-test pattern. Real
    database cases and the missing-extra regression do not use it.
    """
    for module in ("proposal_store", "task_registry", "task_webhook_outbox"):
        monkeypatch.setattr(f"adcp.decisioning.pg.{module}.PG_AVAILABLE", True)


def _sender() -> MagicMock:
    import json

    sender = MagicMock()
    sender._owns_client = True
    sender._allow_private_destinations = False
    sender._timeout = 10.0
    sender.signs_with_rfc9421 = True
    sender._auth.alg = "ed25519"

    def prepare(**kwargs: Any) -> PreparedWebhook:
        key = f"whk_{secrets.token_hex(16)}"
        return PreparedWebhook(
            url=kwargs["url"],
            idempotency_key=key,
            body=json.dumps({"idempotency_key": key, **kwargs}).encode(),
        )

    sender.prepare_mcp.side_effect = prepare
    sender.send_prepared = AsyncMock(
        side_effect=lambda prepared: WebhookDeliveryResult(
            status_code=200,
            idempotency_key=prepared.idempotency_key,
            url=prepared.url,
            response_headers={},
            response_body=b"{}",
            sent_body=prepared.body,
        )
    )
    return sender


def outbox(pool: Any) -> PgTaskWebhookOutbox:
    return PgTaskWebhookOutbox(
        pool=pool, sender=_sender(), encryption_key=b"e" * 32, delivery_retry_horizon_seconds=86400
    )


@pytest.mark.parametrize("kind", ["proposal", "task", "outbox"])
@pytest.mark.usefixtures("fake_pg_driver")
async def test_concurrent_resolution_and_retry(kind: str) -> None:
    pool = MagicMock()
    concrete: Any = {
        "proposal": lambda: PgProposalStore(pool=pool),
        "task": lambda: PgTaskRegistry(pool=pool),
        "outbox": lambda: outbox(pool),
    }[kind]()
    classes = {
        "proposal": LazyProposalStore,
        "task": LazyTaskRegistry,
        "outbox": LazyTaskWebhookOutbox,
    }
    calls = 0

    async def factory() -> Any:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        if calls == 1:
            raise RuntimeError("Bootstrap unavailable")
        return concrete

    lazy = classes[kind](factory)
    assert lazy.resolved is None
    assert not hasattr(lazy, "clear_all")
    with pytest.raises(RuntimeError, match="Bootstrap"):
        await lazy.resolve()
    resolved = await asyncio.gather(*(lazy.resolve() for _ in range(20)))
    assert calls == 2
    assert all(store is concrete for store in resolved)
    assert concrete._pool is pool


@pytest.mark.parametrize("kind", ["proposal", "task", "outbox"])
@pytest.mark.usefixtures("fake_pg_driver")
async def test_first_method_use_resolves_once_and_forwards_arguments(kind: str) -> None:
    pool = MagicMock()
    if kind == "proposal":
        concrete = PgProposalStore(pool=pool)
        method = AsyncMock(return_value=None)
        concrete.get = method
        lazy = LazyProposalStore(lambda: concrete)
        assert lazy.is_durable is True
        await lazy.get("proposal", expected_account_id="acct")
        method.assert_awaited_once_with("proposal", expected_account_id="acct")
    elif kind == "task":
        concrete = PgTaskRegistry(pool=pool)
        method = AsyncMock(return_value={"pagination": {"has_more": False}, "tasks": []})
        concrete.list = method
        lazy = LazyTaskRegistry(lambda: concrete)
        assert lazy.is_durable is True
        assert isinstance(lazy, TaskRegistry) and isinstance(lazy, ListableTaskRegistry)
        await lazy.list(account_id="acct", filters={"status": "working"})
        method.assert_awaited_once_with(
            account_id="acct", filters={"status": "working"}, sort=None, pagination=None
        )
    else:
        concrete = outbox(pool)
        method = AsyncMock(return_value=True)
        concrete.process_one = method
        lazy = LazyTaskWebhookOutbox(lambda: concrete)
        assert lazy.delivery_state_is_durable and lazy.supports_atomic_task_outbox
        with pytest.raises(RuntimeError, match="resolve"):
            lazy.validate_registration("https://buyer.example/hooks")
        assert await lazy.process_one() is True
        lazy.validate_registration("https://buyer.example/hooks")
        method.assert_awaited_once_with()
    assert lazy.resolved is concrete
    concrete.clear_all = AsyncMock()
    assert not hasattr(lazy, "clear_all")


@pytest.mark.usefixtures("fake_pg_driver")
async def test_pair_shares_pool_resolution_and_observers() -> None:
    pool = MagicMock()
    calls = 0

    async def factory() -> tuple[PgTaskRegistry, PgTaskWebhookOutbox]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        box = outbox(pool)
        return PgTaskRegistry(pool=pool, task_webhook_outbox=box), box

    lazy = LazyTaskRegistry(factory)
    facade = LazyTaskWebhookOutbox.from_registry(lazy)
    events = []
    lazy.add_lifecycle_observer(lambda event, **metadata: events.append((event, metadata)))
    assert lazy.task_webhook_outbox is None and lazy.atomic_task_webhook_outbox is False
    registry, box = await asyncio.gather(lazy.resolve(), facade.resolve())
    assert calls == 1
    assert registry._pool is box._pool is pool
    assert lazy.task_webhook_outbox is box
    assert lazy.atomic_task_webhook_outbox is True
    assert _sdk_task_outbox_pair_ready(lazy, box)
    # The bridge is registered before any operation delegated by the facade.
    registry._notify_lifecycle_observers(
        "submitted",
        {
            "task_id": "task",
            "account_id": "acct",
            "task_type": "get_products",
            "created_at": 1.0,
            "updated_at": 1.0,
        },
    )
    assert events[0][0] == "submitted"


@pytest.mark.usefixtures("fake_pg_driver")
async def test_original_pool_identity_and_signing_scope_checks_remain_authoritative() -> None:
    pool = MagicMock()
    other_pool = MagicMock()
    attempts = 0

    def factory() -> PgTaskRegistry:
        nonlocal attempts
        attempts += 1
        box = outbox(other_pool if attempts == 1 else pool)
        return PgTaskRegistry(pool=pool, task_webhook_outbox=box)

    lazy = LazyTaskRegistry(factory)
    with pytest.raises(ValueError, match="same connection pool"):
        await lazy.resolve()
    assert lazy.resolved is None
    assert (await lazy.resolve()).task_webhook_outbox._pool is pool
    assert attempts == 2

    box = outbox(pool)
    bad_pair = LazyTaskRegistry(lambda: (PgTaskRegistry(pool=pool), box))
    with pytest.raises(ValueError, match="pair must share"):
        await bad_pair.resolve()

    def missing_resolver() -> PgTaskRegistry:
        return PgTaskRegistry(pool=pool, webhook_signing_scope_resolver=lambda ctx: "tenant")

    with pytest.raises(ValueError, match="required exactly"):
        await LazyTaskRegistry(missing_resolver).resolve()


@pytest.mark.usefixtures("fake_pg_driver")
async def test_cancelled_factory_retries_and_waiters_do_not_duplicate_success() -> None:
    started, allow = asyncio.Event(), asyncio.Event()
    pool = MagicMock()
    attempts = 0

    async def factory() -> PgTaskRegistry:
        nonlocal attempts
        attempts += 1
        started.set()
        await allow.wait()
        return PgTaskRegistry(pool=pool)

    lazy = LazyTaskRegistry(factory)
    owner = asyncio.create_task(lazy.resolve())
    await started.wait()
    waiter = asyncio.create_task(lazy.resolve())
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    allow.set()
    assert await waiter is await lazy.resolve()
    assert attempts == 2


@pytest.mark.usefixtures("fake_pg_driver")
def test_successful_store_refuses_cross_loop_reuse_without_reopening() -> None:
    pool = MagicMock()
    factory = MagicMock(return_value=PgTaskRegistry(pool=pool))
    lazy = LazyTaskRegistry(factory)
    asyncio.run(lazy.resolve())
    with pytest.raises(RuntimeError, match="another event loop"):
        asyncio.run(lazy.resolve())
    factory.assert_called_once_with()


@pytest.mark.usefixtures("fake_pg_driver")
def test_failed_initialization_can_retry_on_a_new_loop_after_old_loop_closes() -> None:
    pool = MagicMock()
    factory = MagicMock(side_effect=[RuntimeError("Bootstrap"), PgTaskRegistry(pool=pool)])
    lazy = LazyTaskRegistry(factory)
    with pytest.raises(RuntimeError, match="Bootstrap"):
        asyncio.run(lazy.resolve())
    assert asyncio.run(lazy.resolve())._pool is pool
    assert factory.call_count == 2


@pytest.mark.usefixtures("fake_pg_driver")
async def test_bad_factory_result_not_cached() -> None:
    factory = MagicMock(side_effect=[object(), PgTaskRegistry(pool=MagicMock())])
    lazy = LazyTaskRegistry(factory)
    with pytest.raises(TypeError, match="PgTaskRegistry"):
        await lazy.resolve()
    assert lazy.resolved is None
    assert isinstance(await lazy.resolve(), PgTaskRegistry)


@pytest.fixture
async def real_stack() -> AsyncIterator[Any]:
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        pytest.skip("ADCP_PG_TEST_URL required")
    from psycopg_pool import AsyncConnectionPool

    suffix = secrets.token_hex(6)
    pool = AsyncConnectionPool(url, open=False, min_size=1, max_size=4)
    sender = _sender()
    calls = 0

    async def factory() -> tuple[PgTaskRegistry, PgTaskWebhookOutbox]:
        nonlocal calls
        calls += 1
        await pool.open()
        box = PgTaskWebhookOutbox(
            pool=pool,
            sender=sender,
            encryption_key=b"e" * 32,
            delivery_retry_horizon_seconds=86400,
            table=f"test_lazy_box_{suffix}",
        )
        registry = PgTaskRegistry(
            pool=pool, task_webhook_outbox=box, _table=f"test_lazy_task_{suffix}"
        )
        await registry.create_schema()
        await box.create_schema()
        return registry, box

    lazy = LazyTaskRegistry(factory)
    facade = LazyTaskWebhookOutbox.from_registry(lazy)
    try:
        yield lazy, facade, pool, sender, lambda: calls
    finally:
        if not pool.closed:
            async with pool.connection() as conn:
                for table in (
                    f"test_lazy_box_{suffix}",
                    f"test_lazy_task_{suffix}",
                    f"test_lazy_proposal_{suffix}",
                ):
                    await conn.execute(f"DROP TABLE IF EXISTS {table}")
            await pool.close()


async def test_real_lazy_pair_atomic_completion_listing_and_observer_commit(
    real_stack: Any,
) -> None:
    lazy, facade, pool, sender, calls = real_stack
    events = []
    lazy.add_lifecycle_observer(lambda event, **metadata: events.append(event))
    task = await lazy.issue(
        account_id="acct",
        task_type="get_products",
        webhook_url="https://buyer.example/hooks",
        webhook_operation_id="buyer-operation",
    )
    await lazy.update_progress(task, {"percentage": 25})
    await asyncio.gather(*(lazy.complete(task, {"products": []}) for _ in range(10)))
    assert events == ["submitted", "working", "completed"]
    page = await lazy.list(account_id="acct")
    assert page["tasks"][0]["task_id"] == task
    assert (await lazy.get(task, expected_account_id="acct"))["state"] == "completed"
    assert await lazy.get(task, expected_account_id="other") is None
    assert await facade.process_one() is True
    assert sender.send_prepared.await_count == 1
    assert calls() == 1
    assert (await facade.resolve())._pool is (await lazy.resolve())._pool is pool
    # A closed caller-owned pool is not implicitly reopened or replaced.
    await pool.close()
    from psycopg_pool import PoolClosed

    with pytest.raises(PoolClosed):
        await lazy.get(task)
    assert calls() == 1


async def test_real_lazy_proposal_lifecycle(real_stack: Any) -> None:
    from datetime import datetime, timedelta, timezone

    from adcp.decisioning.proposal_store import ProposalState

    lazy, facade, pool, sender, calls = real_stack
    registry = await lazy.resolve()
    table = registry._table.replace("test_lazy_task_", "test_lazy_proposal_")
    store = LazyProposalStore(lambda: PgProposalStore(pool=pool, table_name=table))
    await store.create_schema()
    await store.put_draft(
        proposal_id="proposal", account_id="acct", recipes={}, proposal_payload={}
    )
    assert await store.get("proposal", expected_account_id="other") is None
    await store.commit(
        "proposal",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        proposal_payload={},
        expected_account_id="acct",
    )
    reserved = await store.try_reserve_consumption("proposal", expected_account_id="acct")
    assert reserved.state == ProposalState.CONSUMING
    await store.release_consumption("proposal", expected_account_id="acct")
    await store.try_reserve_consumption("proposal", expected_account_id="acct")
    await store.finalize_consumption("proposal", media_buy_id="buy", expected_account_id="acct")
    assert (
        await store.get_by_media_buy_id("buy", expected_account_id="acct")
    ).state == ProposalState.CONSUMED


async def test_observer_registration_and_removal_survive_resolution(real_stack: Any) -> None:
    lazy, facade, pool, sender, calls = real_stack
    before, after = [], []

    def observe_before(event: Any, **metadata: Any) -> None:
        before.append(event)

    def observe_after(event: Any, **metadata: Any) -> None:
        after.append(event)

    lazy.add_lifecycle_observer(observe_before)
    task = await lazy.issue(account_id="acct", task_type="get_products")
    assert lazy.remove_lifecycle_observer(observe_before) is True
    lazy.add_lifecycle_observer(observe_after)
    await lazy.update_progress(task, {"percentage": 50})
    await lazy.complete(task, {"products": []})
    assert before == ["submitted"]
    assert after == ["working", "completed"]
    lazy.remove_lifecycle_observer(observe_after)
    await lazy.discard(task)
    assert after == ["working", "completed"]


@pytest.mark.usefixtures("fake_pg_driver")
async def test_sync_outbox_crypto_and_worker_methods_delegate() -> None:
    concrete = outbox(MagicMock())
    lazy = LazyTaskWebhookOutbox(lambda: concrete)
    await lazy.resolve()
    protected, nonce = lazy.protect_registration(
        account_id="acct",
        task_id="task",
        task_type="get_products",
        url="https://buyer.example/hooks",
        operation_id="buyer",
        token="token",
    )
    assert lazy.open_registration(
        account_id="acct",
        task_id="task",
        task_type="get_products",
        encrypted_registration=protected,
        nonce=nonce,
    ) == ("https://buyer.example/hooks", "buyer", "token")
    concrete.run_worker = AsyncMock()
    concrete.purge_expired = AsyncMock()
    await lazy.run_worker(poll_interval=0.25, purge_interval=10)
    await lazy.purge_expired()
    concrete.run_worker.assert_awaited_once_with(poll_interval=0.25, purge_interval=10)
    concrete.purge_expired.assert_awaited_once_with()
    with pytest.raises(ValueError):
        lazy.validate_registration("http://buyer.example/hooks")


@pytest.mark.usefixtures("fake_pg_driver")
async def test_durability_is_not_silently_changed_by_factory() -> None:
    class LossyRegistry(PgTaskRegistry):
        is_durable = False

    lazy = LazyTaskRegistry(lambda: LossyRegistry(pool=MagicMock()))
    assert lazy.is_durable is True
    with pytest.raises(ValueError, match="durable"):
        await lazy.resolve()
    assert lazy.resolved is None


@pytest.mark.parametrize("flag", ["delivery_state_is_durable", "supports_atomic_task_outbox"])
@pytest.mark.usefixtures("fake_pg_driver")
async def test_outbox_factory_cannot_weaken_declared_guarantees(flag: str) -> None:
    box = outbox(MagicMock())
    setattr(box, flag, False)
    lazy = LazyTaskWebhookOutbox(lambda: box)
    with pytest.raises(ValueError, match="durable atomic"):
        await lazy.resolve()
    assert lazy.resolved is None
    setattr(box, flag, True)
    assert await lazy.resolve() is box


@pytest.mark.usefixtures("fake_pg_driver")
async def test_registry_factory_cannot_remove_declared_listing_support() -> None:
    concrete = PgTaskRegistry(pool=MagicMock())
    original_list = concrete.list
    concrete.list = None
    lazy = LazyTaskRegistry(lambda: concrete)
    assert isinstance(lazy, ListableTaskRegistry)
    with pytest.raises(TypeError, match="listing support"):
        await lazy.resolve()
    assert lazy.resolved is None
    concrete.list = original_list
    assert await lazy.resolve() is concrete


@pytest.mark.parametrize("kind", ["proposal", "task", "outbox"])
async def test_missing_pg_extra_remains_an_import_error_and_is_not_cached(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    modules = {
        "proposal": "proposal_store",
        "task": "task_registry",
        "outbox": "task_webhook_outbox",
    }
    monkeypatch.setattr(f"adcp.decisioning.pg.{modules[kind]}.PG_AVAILABLE", False)
    pool = MagicMock()
    constructors = {
        "proposal": lambda: PgProposalStore(pool=pool),
        "task": lambda: PgTaskRegistry(pool=pool),
        "outbox": lambda: outbox(pool),
    }
    wrappers = {
        "proposal": LazyProposalStore,
        "task": LazyTaskRegistry,
        "outbox": LazyTaskWebhookOutbox,
    }
    calls = 0

    def factory() -> Any:
        nonlocal calls
        calls += 1
        return constructors[kind]()

    lazy = wrappers[kind](factory)
    for _ in range(2):
        with pytest.raises(ImportError, match=r"adcp\[pg\]"):
            await lazy.resolve()
        assert lazy.resolved is None
    assert calls == 2
