"""Coordinated real lock order, with the executable wrong-order control restored."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MethodType

import pytest

from ._generation_support import END
from ._legacy_row_first_lease import row_first_period_close
from ._production_support import production_harness


async def source_turn(support):
    producer = support.offerings[0].producer
    token = support._producer_turn.set(producer)
    try:
        return await producer.run_worker()
    finally:
        support._producer_turn.reset(token)


@pytest.mark.parametrize("wrong_order", [False, True])
async def test_activation_and_producer_actual_trigger_lock_order(
    wrong_order, tmp_path, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")
    async with production_harness("postgres", tmp_path / "destination.sqlite") as h:
        support, store, item = h.production, h.store, h.item
        locked, release, executing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pids = {}
        original_step = support.projection._activation_step_on
        original_execute = psycopg.AsyncConnection.execute
        original_lease = store.lease_period_close.__func__
        tasks = []
        activation = None

        async def activation_step(connection, account_id):
            # This phase already owns the real account lock and will acquire
            # configuration FK locks while installing the live checkpoints.
            # Earlier activation phases commit their own bounded transactions.
            if asyncio.current_task() is activation and not locked.is_set():
                pids["activation"] = connection.info.backend_pid
                locked.set()
                await asyncio.wait_for(release.wait(), 5)
            return await original_step(connection, account_id)

        async def observe(connection, query, *args, **kwargs):
            if isinstance(query, str) and query.startswith(
                "UPDATE reporting_configurations SET lease_worker_id"
            ):
                pids["producer"] = connection.info.backend_pid
                executing.set()
            return await original_execute(connection, query, *args, **kwargs)

        async def wait_for_actual_trigger_wait():
            await asyncio.wait_for(executing.wait(), 5)
            async with h.pool.connection() as observer:
                for _ in range(100):
                    row = await (
                        await observer.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s"
                            " AND locktype='advisory' AND NOT granted),"
                            " %s=ANY(pg_blocking_pids(%s))",
                            (pids["producer"], pids["activation"], pids["producer"]),
                        )
                    ).fetchone()
                    if row == (True, True):
                        return
                    await asyncio.sleep(0.01)
            pytest.fail("the producer did not block in the real account-lock trigger")

        # Both modes retain the inherited trigger and its original function.
        async with h.pool.connection() as c:
            trigger = await (
                await c.execute(
                    "SELECT t.tgenabled,pg_get_functiondef(t.tgfoid)"
                    " FROM pg_trigger t WHERE t.tgrelid='reporting_configurations'::regclass"
                    " AND t.tgname='reporting_materializer_configuration'"
                )
            ).fetchone()
            assert trigger[0] == "O" and "pg_advisory_xact_lock" in trigger[1]
        with monkeypatch.context() as patch:
            patch.setattr(support.projection, "_activation_step_on", activation_step)
            patch.setattr(psycopg.AsyncConnection, "execute", observe)
            if wrong_order:
                # Restore the actual predecessor acquisition; do not emulate
                # its result or replace the lock-taking database trigger.
                patch.setattr(
                    store,
                    "lease_period_close",
                    MethodType(row_first_period_close, store),
                )
            activation = asyncio.create_task(support.activate(account_id=item.config.account_id))
            tasks.append(activation)
            try:
                await asyncio.wait_for(locked.wait(), 5)
                producer = asyncio.create_task(source_turn(support))
                tasks.append(producer)
                if wrong_order:
                    await wait_for_actual_trigger_wait()
                else:
                    turn = await asyncio.wait_for(asyncio.shield(producer), 5)
                    assert turn.leased is None
                    assert not executing.is_set()
                print(
                    json.dumps(
                        {
                            "production_lock_order": "before_checkpoint",
                            "wrong_order_control": wrong_order,
                            "backend_pids": pids,
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                    flush=True,
                )
                release.set()
                results = await asyncio.wait_for(
                    asyncio.gather(activation, producer, return_exceptions=True), 8
                )
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        assert store.lease_period_close.__func__ is original_lease
        deadlocks = [r for r in results if isinstance(r, psycopg.errors.DeadlockDetected)]
        assert len(deadlocks) == int(wrong_order)
        assert all(not isinstance(r, BaseException) or r in deadlocks for r in results)

        async with h.pool.connection() as c:
            rows = await (
                await c.execute(
                    "SELECT (SELECT count(*) FROM reporting_projection_accounts),"
                    " (SELECT count(*) FROM reporting_production_accounts),"
                    " (SELECT count(*) FROM reporting_production_work),"
                    " (SELECT count(*) FROM reporting_materializer_work),"
                    " (SELECT count(*) FROM reporting_production_notification_events)"
                )
            ).fetchone()
            lease = await (
                await c.execute(
                    "SELECT lease_worker_id,lease_expires_at FROM reporting_configurations"
                    " WHERE account_id=%s",
                    (item.config.account_id,),
                )
            ).fetchone()
        if isinstance(results[0], BaseException):
            assert rows == (1, 0, 0, 0, 0)
            assert not await support.projection.baseline_ready(account_id=item.config.account_id)
        else:
            assert rows == (1, 1, 0, 0, 0)
        # A failed lease rolls back; a winning producer releases in its real
        # finally block. Neither path can leak a lease or reserve epoch-zero I/O.
        assert lease == (None, None)
        assert (
            await store.get_revision(
                account_id=item.config.account_id,
                reporting_revision_id=item.revision.reporting_revision_id,
            )
            == item.revision
        )
        # The negative control restores the real methods before resuming the
        # incomplete phase. Its original input and epoch-zero queues persist.
        await support.activate(account_id=item.config.account_id)
        assert await support.projection.baseline_ready(account_id=item.config.account_id)
        executing.clear()
        with monkeypatch.context() as patch:
            patch.setattr(psycopg.AsyncConnection, "execute", observe)
            turn = await asyncio.wait_for(source_turn(support), 5)
        assert turn.leased is not None and executing.is_set()
        assert turn.revisions_committed == []
        print(
            json.dumps(
                {
                    "production_lock_order": "wrong_order_control" if wrong_order else "restored",
                    "actual_trigger": True,
                    "observed_trigger_wait": wrong_order,
                    "deadlocks": len(deadlocks),
                    "backend_pids": pids,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "rollback_or_commit_verified": True,
                    "restored_acquisition": store.lease_period_close.__func__ is original_lease,
                    "producer_progress_after_restore": turn.leased is not None,
                }
            )
        )


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_selected_source_enrollment_and_expired_lease_fairness(backend, tmp_path):
    async with production_harness(
        backend, tmp_path / "destination.sqlite", second_source=True
    ) as h:
        support, store, item = h.production, h.store, h.item
        selected, other = support.offerings
        peer = replace(item.config, delivery_config_id="peer")
        other_config = replace(item.config, delivery_config_id="other-source")
        unadmitted = replace(item.config, delivery_config_id="unadmitted")
        outside = replace(item.config, account_id="acct_b")
        for configuration in (peer, other_config, unadmitted, outside):
            await store.put_configuration(configuration)
        before = {
            account: await store.list_configurations(account_id=account)
            for account in ("acct_a", "acct_b")
        }

        async def acquire(offering, worker, now=END):
            token = support._producer_turn.set(offering.producer)
            try:
                return await store.lease_period_close(worker_id=worker, now=now, lease_seconds=1)
            finally:
                support._producer_turn.reset(token)

        assert await acquire(selected, "preactivation") is None
        assert await acquire(other, "preactivation") is None
        await support.activate(account_id="acct_a")
        # Two actual source instances intentionally have identical report
        # contracts. Only explicit, verified offering admission chooses one.
        assert await acquire(selected, "unbound") is None
        for configuration, offering in (
            (item.config, selected),
            (peer, selected),
            (other_config, other),
        ):
            offering.producer._source.bind_generation(configuration)
            binding = replace(item.binding, generation_key=configuration.generation_key)
            item.writer.grant(binding)
            await store.admit_production_configuration(
                configuration, binding, offering_id=offering.offering_id
            )
        first = await acquire(selected, "crashed")
        second = await acquire(selected, "live")
        assert first.generation_key == item.config.generation_key
        assert second.generation_key == peer.generation_key
        await store.release_period_close(second, worker_id="live")
        expired = await acquire(selected, "replacement", END + timedelta(seconds=2))
        assert expired.generation_key == first.generation_key
        await store.release_period_close(expired, worker_id="replacement")
        isolated = await acquire(other, "other")
        assert isolated.generation_key == other_config.generation_key
        await store.release_period_close(isolated, worker_id="other")
        assert {
            account: await store.list_configurations(account_id=account)
            for account in ("acct_a", "acct_b")
        } == before
        if h.pool is None:
            turns = store._lease_turns
            assert unadmitted.generation_key not in turns
            assert outside.generation_key not in turns
            assert not store._leases
        else:
            async with h.pool.connection() as c:
                turns = await (
                    await c.execute(
                        "SELECT account_id,delivery_config_id"
                        " FROM adcp_reporting_configuration_lease_turns"
                        " ORDER BY account_id,delivery_config_id"
                    )
                ).fetchall()
                assert turns == [
                    ("acct_a", "daily"),
                    ("acct_a", "other-source"),
                    ("acct_a", "peer"),
                ]
                assert (
                    await (
                        await c.execute(
                            "SELECT count(*) FROM reporting_configurations"
                            " WHERE lease_worker_id IS NOT NULL"
                        )
                    ).fetchone()
                )[0] == 0
        assert not (await h.queue())[0]
