"""Bounded committed-period progress, with a settled first acquisition window."""

import asyncio
import hashlib
import json
import sqlite3
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import pytest

from ._generation_support import END, START
from ._production_support import production_harness
from .test_reporting_production_lock_order import source_turn


async def sample_configurations(h, *, count=32):
    selected = h.production.offerings[0]
    blocked = [h.item.config] + [
        replace(h.item.config, delivery_config_id=f"busy-{i:02}") for i in range(count - 1)
    ]
    outside = replace(h.item.config, account_id="acct_b")
    for configuration in [*blocked, outside]:
        selected.producer._source.bind_generation(configuration)
        binding = replace(h.item.binding, generation_key=configuration.generation_key)
        h.item.writer.grant(binding)
        await h.store.admit_production_configuration(
            configuration, binding, offering_id=selected.offering_id
        )
    for account in ("acct_a", "acct_b"):
        await h.production.activate(account_id=account)
    return blocked, outside


async def lease_source(support, worker, *, index=0):
    token = support._producer_turn.set(support.offerings[index].producer)
    try:
        return await support.store.lease_period_close(worker_id=worker, now=END, lease_seconds=30)
    finally:
        support._producer_turn.reset(token)


def observe_samples(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    original = psycopg.AsyncConnection.execute
    samples = []

    async def execute(connection, query, *args, **kwargs):
        result = await original(connection, query, *args, **kwargs)
        if isinstance(query, str) and query.startswith(
            "SELECT c.account_id,c.delivery_config_id,c.delivery_config_version,"
        ):
            assert query.endswith("LIMIT 32")
            assert 0 <= result.rowcount <= 32
            samples.append(result.rowcount)
        return result

    monkeypatch.setattr(psycopg.AsyncConnection, "execute", execute)
    return samples


async def bounded_turn(call, samples):
    start = len(samples)
    result = await asyncio.wait_for(call(), 5)
    fetched = samples[start:]
    assert 1 <= len(fetched) <= 2 and sum(fetched) <= 32
    return result


def account_image(image, account):
    return {
        table: [row for row in rows if row[0].get("account_id") == account]
        for table, rows in image.items()
    }


@pytest.mark.parametrize("notifications", [False, True])
async def test_busy_account_window_advances_wraps_and_revisits_after_unlock(
    notifications, tmp_path, monkeypatch
):
    async with production_harness(
        "postgres",
        tmp_path / "destination.sqlite",
        count=0,
        source_publication=True,
        second_source=True,
        notifications=notifications,
    ) as h:
        support, store = h.production, h.store
        blocked, outside = await sample_configurations(h)
        other = replace(h.item.config, delivery_config_id="other-source")
        unadmitted = replace(h.item.config, delivery_config_id="000-unadmitted")
        await store.put_configuration(unadmitted)
        support.offerings[1].producer._source.bind_generation(other)
        binding = replace(h.item.binding, generation_key=other.generation_key)
        h.item.writer.grant(binding)
        await store.admit_production_configuration(
            other, binding, offering_id=support.offerings[1].offering_id
        )
        source = support.offerings[0].producer._source
        samples = observe_samples(monkeypatch)
        before = await h.image()
        async with h.pool.connection() as holder, holder.transaction():
            await store._lock_account(holder, "acct_a")
            first = await bounded_turn(lambda: source_turn(support), samples)
            assert first.leased is None and samples[-1] == 32
            # Another producer has its own hint; its blocked turn cannot erase
            # the first producer's progress beyond the full 32-row window.
            production_operation_1 = await bounded_turn(
                lambda: lease_source(support, "other", index=1), samples
            )
            assert production_operation_1 is None
            assert await h.image() == before
            second = await bounded_turn(lambda: source_turn(support), samples)
            assert second.leased is not None, "locked prefix hid the eligible second account"
            assert second.leased.generation_key == outside.generation_key
            assert len(second.obligations_committed) == len(second.revisions_committed) == 1
            assert not second.slices_failed
            assert account_image(await h.image(), "acct_a") == account_image(before, "acct_a")
            assert len(source.requests) == 1
            assert source.requests[0].identity.account_id == "acct_b"
            assert not support.offerings[1].producer._source.requests
            # Successful acquisition resets discovery to the durable rank.
            production_operation_2 = await bounded_turn(lambda: source_turn(support), samples)
            assert (production_operation_2).leased is None
            held = await bounded_turn(lambda: lease_source(support, "tail-held"), samples)
            assert held is not None and held.generation_key == outside.generation_key
            held_image = await h.image()
            production_operation_3 = await bounded_turn(lambda: source_turn(support), samples)
            assert (production_operation_3).leased is None
            start = len(samples)
            production_operation_4 = await bounded_turn(lambda: source_turn(support), samples)
            assert (production_operation_4).leased is None
            assert samples[start:] == [0, 32]  # empty tail wraps once, never an unbounded scan
            assert await h.image() == held_image
        try:
            returned = await bounded_turn(lambda: source_turn(support), samples)
            assert returned.leased.generation_key in {c.generation_key for c in blocked}
            assert len(returned.obligations_committed) == len(returned.revisions_committed) == 1
            assert not returned.slices_failed
        finally:
            await store.release_period_close(held, worker_id="tail-held")
        assert len(source.requests) == 2
        assert {r.identity.account_id for r in source.requests} == {"acct_a", "acct_b"}
        assert len({r.identity.source_execution_key for r in source.requests}) == 2
        async with h.pool.connection() as c:
            turns = await (
                await c.execute(
                    "SELECT account_id,delivery_config_id"
                    " FROM adcp_reporting_configuration_lease_turns"
                    " ORDER BY account_id,delivery_config_id"
                )
            ).fetchall()
            assert turns == [
                ("acct_a", returned.leased.delivery_config_id),
                ("acct_b", outside.delivery_config_id),
            ]
            assert await (
                await c.execute("SELECT count(*) FROM reporting_production_source_probe_turns")
            ).fetchone() == (0,)
            assert await (
                await c.execute(
                    "SELECT count(*) FROM reporting_configurations"
                    " WHERE lease_worker_id IS NOT NULL"
                )
            ).fetchone() == (0,)
        assert not (await h.queue())[0]
        print(
            json.dumps(
                {
                    "busy_account_progress": {
                        "notifications": notifications,
                        "bound": 32,
                        "blocked_configurations": 32,
                        "publications": 2,
                        "separate_producer_hints": True,
                        "wrap_and_unlock": True,
                        "blocked_state_unchanged": True,
                    }
                }
            ),
            flush=True,
        )


@pytest.mark.parametrize("notifications", [False, True])
async def test_fresh_stores_and_concurrent_workers_preserve_bounded_sampling_and_acquisition(
    notifications, tmp_path, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")
    path = tmp_path / "destination.sqlite"
    async with production_harness(
        "postgres",
        path,
        count=0,
        source_publication=True,
        notifications=notifications,
    ) as h:
        # Two fresh services each perform a real startup turn. Even after
        # those acquisitions, more than a full window belongs to the busy A.
        blocked, outside = await sample_configurations(h, count=65)
        async with h.pool.connection() as holder, holder.transaction():
            await h.store._lock_account(holder, "acct_a")
            production_operation_5 = await source_turn(h.production)
            assert (production_operation_5).leased is None
        await h.production.aclose()
        bindings = [
            (c, h.production.offerings[0].offering_id, "catalog-7391") for c in [*blocked, outside]
        ]
        async with (
            production_harness(
                "postgres",
                path,
                count=0,
                source_publication=True,
                notifications=notifications,
                existing_pool=h.pool,
                source_bindings=bindings,
            ) as fresh,
            production_harness(
                "postgres",
                path,
                count=0,
                source_publication=True,
                notifications=notifications,
                existing_pool=h.pool,
                source_bindings=bindings,
            ) as peer,
        ):
            assert fresh.store is not h.store and peer.store is not fresh.store
            sources = [v.production.offerings[0].producer._source for v in (fresh, peer)]
            assert [len(s.requests) for s in sources] == [1, 1]
            before = await h.image()
            samples = observe_samples(monkeypatch)
            original = psycopg.AsyncConnection.execute
            holding, release = asyncio.Event(), asyncio.Event()
            winner = None

            async def coordinate(connection, query, *args, **kwargs):
                result = await original(connection, query, *args, **kwargs)
                if (
                    asyncio.current_task() is winner
                    and isinstance(query, str)
                    and query.startswith("UPDATE reporting_configurations SET lease_worker_id")
                ):
                    # The real statement, including its inherited trigger,
                    # has executed under the account lock, but not committed.
                    holding.set()
                    await asyncio.wait_for(release.wait(), 5)
                return result

            monkeypatch.setattr(psycopg.AsyncConnection, "execute", coordinate)
            async with h.pool.connection() as holder, holder.transaction():
                await h.store._lock_account(holder, "acct_a")
                for current in (fresh, peer):
                    turn = await bounded_turn(lambda: source_turn(current.production), samples)
                    assert turn.leased is None and samples[-1] == 32
                assert await h.image() == before
                winner = asyncio.create_task(source_turn(fresh.production))
                try:
                    await asyncio.wait_for(holding.wait(), 5)
                    losing = await bounded_turn(lambda: source_turn(peer.production), samples)
                    assert losing.leased is None and not losing.revisions_committed
                    # Observe committed rows without requesting the account
                    # lock deliberately held by the winning transaction.
                    async with h.pool.connection() as observer:
                        assert await (
                            await observer.execute(
                                "SELECT count(*) FROM reporting_revisions WHERE account_id=%s",
                                ("acct_b",),
                            )
                        ).fetchone() == (0,)
                    release.set()
                    won = await asyncio.wait_for(winner, 5)
                finally:
                    release.set()
                    if not winner.done():
                        winner.cancel()
                    await asyncio.gather(winner, return_exceptions=True)
                assert won.leased.generation_key == outside.generation_key
                assert len(won.obligations_committed) == len(won.revisions_committed) == 1
                assert not won.slices_failed
                assert account_image(await h.image(), "acct_a") == account_image(before, "acct_a")
            # The losing worker resumes; it cannot duplicate B's committed
            # source execution or fabricate an acquisition for the held A.
            repeat = await source_turn(peer.production)
            assert not repeat.slices_failed
            snapshot = await h.store.read_status_snapshot(account_id="acct_b")
            assert len(snapshot.obligations) == len(snapshot.revisions) == 1
            requests = [r for s in sources for r in s.requests if r.identity.account_id == "acct_b"]
            assert len(requests) == 1
            print(
                json.dumps(
                    {
                        "busy_account_fresh_concurrent": {
                            "notifications": notifications,
                            "bound": 32,
                            "blocked_configurations": 65,
                            "fresh_store_instances": 2,
                            "coordinated_actual_trigger": True,
                            "second_account_publications": 1,
                        }
                    }
                ),
                flush=True,
            )


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_rejected_source_window_does_not_starve_later_admitted_generation(
    backend, tmp_path, monkeypatch
):
    path = tmp_path / "destination.sqlite"
    async with production_harness(backend, path, count=0, source_publication=True) as h:
        support, store, item = h.production, h.store, h.item
        source = support.offerings[0].producer._source
        blocked = [replace(item.config, delivery_config_id=f"blocked-{i:02}") for i in range(32)]
        for configuration in blocked:
            source.bind_generation(configuration)
            binding = replace(item.binding, generation_key=configuration.generation_key)
            item.writer.grant(binding)
            await store.admit_production_configuration(
                configuration, binding, offering_id=support.offerings[0].offering_id
            )
        outside = replace(item.config, account_id="acct_b")
        await store.put_configuration(outside)
        await support.activate(account_id=item.config.account_id)
        for configuration in blocked:
            del source.bindings[configuration.generation_key]
        before = await store.list_configurations(account_id=item.config.account_id)
        if h.pool is not None:
            import psycopg

            before_image = await h.image()
            original_execute = psycopg.AsyncConnection.execute

            async def fail_after_probe(connection, query, *args, **kwargs):
                result = await original_execute(connection, query, *args, **kwargs)
                if isinstance(query, str) and query.startswith(
                    "INSERT INTO reporting_production_source_probe_turns"
                ):
                    raise RuntimeError("injected probe commit failure")
                return result

            with monkeypatch.context() as patch:
                patch.setattr(psycopg.AsyncConnection, "execute", fail_after_probe)
                with pytest.raises(RuntimeError, match="injected probe commit failure"):
                    await source_turn(support)
            assert await h.image() == before_image
        first = await source_turn(support)
        assert not first.slices_failed
        if h.pool is not None:
            async with h.pool.connection() as c:
                probes = await (
                    await c.execute(
                        "SELECT account_id,delivery_config_id"
                        " FROM reporting_production_source_probe_turns"
                        " ORDER BY account_id,delivery_config_id"
                    )
                ).fetchall()
                assert probes == [(item.config.account_id, v.delivery_config_id) for v in blocked]
                assert await (
                    await c.execute("SELECT count(*) FROM adcp_reporting_configuration_lease_turns")
                ).fetchone() == (0,)
        await support.aclose()
        # A fresh store/service in PG uses persisted rejection progress. Memory
        # keeps its state across a new service, without claiming process durability.
        async with production_harness(
            backend,
            path,
            count=0,
            source_publication=True,
            existing_store=store if backend == "memory" else None,
            existing_pool=h.pool,
        ) as fresh:
            second = await source_turn(fresh.production)
            assert not second.slices_failed
            # start() itself executes a bounded producer turn. Count that
            # actual acquisition as well as the explicitly requested turn.
            fresh_source = fresh.production.offerings[0].producer._source
            assert len(first.revisions_committed) + len(fresh_source.requests) == 1
            for completed in (first, second):
                if completed.leased is not None:
                    assert completed.leased.generation_key == item.config.generation_key
            repeat = await source_turn(fresh.production)
            assert not repeat.obligations_committed and not repeat.revisions_committed
            snapshot = await fresh.store.read_status_snapshot(account_id=item.config.account_id)
            assert len(snapshot.obligations) == len(snapshot.revisions) == 1
            assert snapshot.obligations[0].generation_key == item.config.generation_key
            assert not (await fresh.store.read_status_snapshot(account_id="acct_b")).obligations
            assert (
                await fresh.store.list_configurations(account_id=item.config.account_id) == before
            )
            if h.pool is not None:
                after_image = await fresh.image()
                for table in (
                    "reporting_production_generations",
                    "reporting_production_destination_bindings",
                    "reporting_materializer_work",
                    "reporting_materializer_notification_events",
                ):
                    assert after_image[table] == before_image[table]
            print(
                json.dumps(
                    {
                        "rejected_window": 32,
                        "backend": backend,
                        "publications": 1,
                        "fresh_store": backend == "postgres",
                    }
                ),
                flush=True,
            )


def turn_document(turn, source):
    assert not turn.slices_failed
    return {
        "obligations": turn.obligations_committed,
        "revisions": turn.revisions_committed,
        "executions": [r.identity.source_execution_key for r in source.requests],
    }


async def default_observation_states(h, snapshot, executions):
    """Every bounded acquisition retains its identity, hourly due time and checkpoint."""
    states, retained_keys = {}, set()
    revisions = {revision.reporting_revision_id: revision for revision in snapshot.revisions}
    for obligation in snapshot.obligations:
        identity = {
            "account_id": obligation.account_id,
            "reporting_obligation_id": obligation.reporting_obligation_id,
        }
        observation = await h.store.get_provisional_observation(**identity)
        assert observation is not None
        acquisition = observation.acquisition
        seeded = obligation.reporting_obligation_id == h.item.obligation.reporting_obligation_id
        assert acquisition.ordinal == (1 if seeded else 0)
        assert acquisition.predecessor_revision_id == (
            h.item.revision.reporting_revision_id if seeded else None
        )
        assert acquisition.binds(obligation)
        assert acquisition.policy.window == timedelta(days=3)
        assert acquisition.policy.cadence == timedelta(hours=1)
        assert acquisition.policy.official_close_lag is None
        assert observation.provisional_until == obligation.period.end + timedelta(days=3)
        expected_due = (
            min(observation.checked_at + timedelta(hours=1), observation.provisional_until)
            if observation.checked_at < observation.provisional_until
            else None
        )
        assert observation.next_due_at == expected_due
        checkpoint = await h.store.get_restatement_checkpoint(**identity)
        assert checkpoint is not None
        assert checkpoint.checked_at == observation.checked_at
        assert checkpoint.next_observation == acquisition.ordinal + 1
        assert checkpoint.provisional_until == observation.provisional_until
        revision = revisions[observation.revision_id]
        assert revision.finality == "snapshot"
        assert revision.supersedes_reporting_revision_id == acquisition.predecessor_revision_id
        assert acquisition.execution_key not in retained_keys
        retained_keys.add(acquisition.execution_key)
        states[obligation.reporting_obligation_id] = "pending" if expected_due else "settled"
    assert retained_keys == set(executions)
    assert list(states.values()).count("settled") == 60
    assert list(states.values()).count("pending") == 71
    return states


@asynccontextmanager
async def restarted_process(h, path, *, pause):
    from .test_reporting_materializer_process import Child

    class ProgressChild(Child):
        async def event(self, point):
            line = await asyncio.wait_for(self.process.stdout.readline(), 120)
            assert line, f"producer exited before {point}; retained diagnostic: {log}"
            result = json.loads(line)
            assert result["point"] == point, result
            return result

    log = path.with_name(
        "producer-restart-paused.log" if pause else "producer-restart-finished.log"
    )
    with log.open("wb") as diagnostic:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.conformance.reporting._production_progress_process",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=diagnostic,
        )
        child = ProgressChild(process)
        try:
            await child.send(
                {
                    "conninfo": h.pool.conninfo,
                    "kwargs": h.pool.kwargs,
                    "path": str(path),
                    "pause": pause,
                }
            )
            yield child
        finally:
            await child.kill()
    print(
        json.dumps(
            {
                "producer_restart_diagnostic": str(log),
                "bytes": log.stat().st_size,
                "sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            }
        ),
        flush=True,
    )


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_bounded_producer_advances_past_processed_first_window(backend, tmp_path):
    async with production_harness(
        backend, tmp_path / "destination.sqlite", count=0, periods=131
    ) as h:
        support, item = h.production, h.item
        producer = support.offerings[0].producer
        source = producer._source
        assert producer._max_periods_per_turn == 64
        h.source_clock.advance(timedelta(hours=131))
        # Same caller-selected ID in another account and a different retained
        # generation must not acquire a position just because this source runs.
        untouched = (
            replace(item.config, account_id="acct_b"),
            replace(item.config, delivery_config_version=2),
        )
        for config in untouched:
            await h.store.put_configuration(config)
        await support.activate(account_id=item.config.account_id)
        print(
            json.dumps({"progress_backend": backend, "phase": "activated", "bound": 64}), flush=True
        )
        first = await source_turn(support)
        assert not first.slices_failed
        # The first period was published before activation. Its default-policy
        # read retains that predecessor and adds an observation; all 64 periods
        # still fit in the original bounded acquisition window.
        assert len(first.obligations_committed) == 63
        assert len(first.revisions_committed) == 64
        assert len(source.requests) == 64
        seeded_observation = await h.store.get_provisional_observation(
            account_id=item.config.account_id,
            reporting_obligation_id=item.obligation.reporting_obligation_id,
        )
        assert seeded_observation is not None
        assert seeded_observation.acquisition.ordinal == 1
        assert seeded_observation.acquisition.predecessor_revision_id == (
            item.revision.reporting_revision_id
        )
        assert seeded_observation.checked_at == h.source_clock()
        assert seeded_observation.next_due_at is None
        assert seeded_observation.revision_id in first.revisions_committed
        first_document = turn_document(first, source)
        print(
            json.dumps({"progress_backend": backend, "phase": "first_window", "count": 64}),
            flush=True,
        )
        await support.aclose()
        path = tmp_path / "destination.sqlite"
        if backend == "memory":
            # This is a new service/source/projector over the reference store's
            # retained state, not a claim of memory durability across processes.
            async with production_harness(
                backend, path, count=0, periods=131, existing_store=h.store
            ) as fresh:
                fresh.source_clock.advance(timedelta(hours=131, seconds=61))
                new_source = fresh.production.offerings[0].producer._source
                second = turn_document(await source_turn(fresh.production), new_source)
                previous_requests = len(new_source.requests)
                third = turn_document(await source_turn(fresh.production), new_source)
                third["executions"] = third["executions"][previous_requests:]
                repeat = await source_turn(fresh.production)
                assert not repeat.obligations_committed and not repeat.revisions_committed
                assert len(new_source.requests) == 67
        else:
            async with restarted_process(h, path, pause=True) as child:
                second = await child.event("committed_before_release")
                async with h.pool.connection() as c:
                    row = await (
                        await c.execute(
                            "SELECT c.lease_worker_id,p.closed_through"
                            " FROM reporting_configurations c"
                            " JOIN reporting_production_source_progress p"
                            " USING(account_id,delivery_config_id,delivery_config_version)"
                            " WHERE c.account_id=%s AND c.delivery_config_id=%s"
                            " AND c.delivery_config_version=%s",
                            (
                                item.config.account_id,
                                item.config.delivery_config_id,
                                item.config.delivery_config_version,
                            ),
                        )
                    ).fetchone()
                    assert row[0] is not None and row[1] == START + timedelta(hours=128)
                await child.kill()
                assert child.process.returncode == -9
            async with restarted_process(h, path, pause=False) as child:
                third = await child.event("done")
                production_operation_6 = await asyncio.wait_for(child.process.wait(), 10)
                assert production_operation_6 == 0
        assert len(second["obligations"]) == len(second["revisions"]) == 64
        assert len(third["obligations"]) == len(third["revisions"]) == 3
        executions = first_document["executions"] + second["executions"] + third["executions"]
        assert len(executions) == len(set(executions)) == 131
        snapshot = await h.store.read_status_snapshot(account_id=item.config.account_id)
        assert len(snapshot.obligations) == 131
        assert len(snapshot.revisions) == 132
        assert (
            await h.store.get_provisional_observation(
                account_id=item.config.account_id,
                reporting_obligation_id=item.obligation.reporting_obligation_id,
            )
            == seeded_observation
        )
        expected_states = await default_observation_states(h, snapshot, executions)
        assert max(o.period.end for o in snapshot.obligations) == START + timedelta(hours=131)
        assert {o.generation_key for o in snapshot.obligations} == {item.config.generation_key}
        assert not (await h.store.read_status_snapshot(account_id="acct_b")).obligations
        assert (
            await h.store.get_revision(
                account_id=item.config.account_id,
                reporting_revision_id=item.revision.reporting_revision_id,
            )
            == item.revision
        )
        with sqlite3.connect(tmp_path / "source.seals") as connection:
            assert connection.execute("SELECT count(*) FROM seals").fetchone() == (131,)
        if h.pool is None:
            assert h.store._production_closed == {
                item.config.generation_key: START + timedelta(hours=131)
            }
            assert {
                identifier: work.state
                for identifier, work in h.store._production_source_work.items()
            } == expected_states
            assert not {c.generation_key for c in untouched} & set(h.store._production_closed)
        else:
            async with h.pool.connection() as c:
                assert await (
                    await c.execute(
                        "SELECT account_id,delivery_config_id,delivery_config_version,"
                        " closed_through"
                        " FROM reporting_production_source_progress"
                    )
                ).fetchall() == [
                    (
                        item.config.account_id,
                        item.config.delivery_config_id,
                        1,
                        START + timedelta(hours=131),
                    )
                ]
                assert (
                    dict(
                        await (
                            await c.execute(
                                "SELECT reporting_obligation_id,state"
                                " FROM reporting_production_source_work"
                            )
                        ).fetchall()
                    )
                    == expected_states
                )
        print(
            json.dumps(
                {
                    "progress_backend": backend,
                    "periods": 131,
                    "bound": 64,
                    "unique_acquisitions": 131,
                    "restart": "SIGKILL" if h.pool else "new-service",
                }
            ),
            flush=True,
        )
