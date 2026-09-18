"""Admitted finish atomicity and permanent quarantine across production activation."""

from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    ReportingMaterializationAttempt,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
)
from adcp.reporting.materializer import ReportingDestinationRequest, ReportingWriterError
from adcp.reporting.materializer.memory import InMemoryReportingMaterializerStore
from adcp.reporting.materializer.work import ReportingMaterializerLease

from ._feed_support import feed_request, walk
from ._production_support import production_harness
from ._projection_support import drain
from .test_reporting_materializer_transactions import postgres_failure
from .test_reporting_production_lock_order import source_turn


async def production_queue(h):
    if h.pool is None:
        state = h.store._production_outbox
        return (
            ((), ())
            if state is None
            else (
                tuple(state.events.values()),
                tuple(w.state for w in state.expansions.values()),
            )
        )
    async with h.pool.connection() as c:
        events = await (
            await c.execute("SELECT snapshot FROM reporting_production_notification_events")
        ).fetchall()
        work = await (
            await c.execute("SELECT state FROM reporting_production_notification_expansions")
        ).fetchall()
    return tuple(r[0] for r in events), tuple(r[0] for r in work)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("position", ["outcome", "status_head", "account_head", "boundary", "ack"])
async def test_production_finish_fault_restores_every_row_head_capture_and_frozen_page(
    backend, notifications, position, monkeypatch, tmp_path
):
    async with production_harness(
        backend, tmp_path / "destination.sqlite", notifications=notifications, count=1
    ) as h:
        item = h.item
        await h.production.activate(account_id=item.config.account_id)
        lease = await item.claim()
        assert isinstance(lease, ReportingMaterializerLease) and lease.admission_epoch == 2
        prepared, verified = await item.verified(lease)
        first = await h.store.read_reporting_feed(feed_request(item), caller=item.binding.principal)
        frozen = await walk(h.store, feed_request(item), item.binding.principal, first=first)
        old_queue, ordinary = await h.queue(), await h.ordinary_events()
        before = await h.image()

        async def finish():
            return await h.store.finish_materialization(lease, prepared=prepared, verified=verified)

        if h.pool is not None:
            prefixes = {
                "outcome": "INSERT INTO reporting_reconciliation_records",
                "status_head": "INSERT INTO reporting_production_status_heads",
                "account_head": "UPDATE reporting_materializer_accounts SET captured_sequence=",
                "boundary": "INSERT INTO reporting_production_status_boundaries",
                "ack": "UPDATE reporting_production_work SET state='acked'",
            }
            with postgres_failure(monkeypatch, prefixes[position]) as hit:
                with pytest.raises(ReportingWriterError):
                    await finish()
                assert len(hit) == 1
        else:
            import adcp.reporting.materializer.memory as memory

            cls = InMemoryReportingMaterializerStore
            if position == "outcome":
                original = cls._commit_record_unlocked

                def fail(self, record, **kwargs):
                    result = original(self, record, **kwargs)
                    if isinstance(record, ReportingMaterializationRecord):
                        raise OSError("injected production finish")
                    return result

                target, method = cls, "_commit_record_unlocked"
            elif position in {"status_head", "account_head"}:

                def fail(*args, **kwargs):
                    raise OSError("injected production finish")

                target, method = memory, "ReportingMaterializerBoundary"
            else:
                method = "_materializer_dirty" if position == "boundary" else "_park"
                target, original = cls, getattr(cls, method)

                def fail(self, *args, **kwargs):
                    original(self, *args, **kwargs)
                    raise OSError("injected production finish")

            with monkeypatch.context() as patch:
                patch.setattr(target, method, fail)
                with pytest.raises(ReportingWriterError):
                    await finish()
        assert await h.image() == before
        assert (
            await walk(h.store, feed_request(item), item.binding.principal, first=first) == frozen
        )
        assert (await finish()).state == "verified"
        assert len(await item.outcomes()) == 1
        assert len(await h.store.read_production_boundaries(caller=item.binding.principal)) == 1
        assert await h.store.read_materializer_boundaries(caller=item.binding.principal) == ()
        assert len((await production_queue(h))[0]) == int(notifications)
        assert await h.queue() == old_queue and await h.ordinary_events() == ordinary
        committed = await h.image()
        assert (await finish()).state == "verified"
        assert await h.image() == committed
        assert item.writer.writes == 1


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("position", ["event", "expansion", "missing_event"])
async def test_enabled_production_enqueue_is_required_inside_verified_finish(
    backend, position, monkeypatch, tmp_path
):
    async with production_harness(
        backend, tmp_path / "destination.sqlite", notifications=True, count=1
    ) as h:
        item = h.item
        await h.production.activate(account_id=item.config.account_id)
        lease = await item.claim()
        prepared, verified = await item.verified(lease)
        before = await h.image()

        async def finish():
            return await h.store.finish_materialization(lease, prepared=prepared, verified=verified)

        if h.pool is not None and position != "missing_event":
            table = "events" if position == "event" else "expansions"
            with postgres_failure(
                monkeypatch, f"INSERT INTO reporting_production_notification_{table}"
            ) as hit:
                with pytest.raises(ReportingWriterError):
                    await finish()
                assert len(hit) == 1
        else:
            with monkeypatch.context() as patch:
                if h.pool is not None:
                    import adcp.reporting.materializer.pg as pg

                    async def empty(connection, event):
                        pass

                    patch.setattr(pg, "enqueue_materializer_event_on", empty)
                else:
                    import adcp.reporting.outbox.memory as memory

                    if position == "event":

                        def fail(*args, **kwargs):
                            raise OSError("injected production event")

                        patch.setattr(memory, "_Work", fail)
                    else:
                        original = memory.NotificationState.enqueue

                        def enqueue(self, event):
                            if position != "missing_event":
                                original(self, event)
                                raise OSError("injected production expansion")

                        patch.setattr(memory.NotificationState, "enqueue", enqueue)
                with pytest.raises(ReportingWriterError):
                    await finish()
        assert await h.image() == before
        assert await production_queue(h) == ((), ())
        assert (await finish()).state == "verified"
        events, work = await production_queue(h)
        assert len(events) == 1 and work == ("pending",)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_explicit_old_pending_import_keeps_epoch_and_external_identity_after_activation(
    backend, notifications, tmp_path, monkeypatch
):
    path = tmp_path / "destination.sqlite"
    async with production_harness(backend, path, notifications=notifications, count=1) as h:
        item = h.item
        await h.store.bind_obligation_delivery(
            ReportingObligationDeliveryRecord(
                item.scope,
                "USD",
                item.revision.created_at + timedelta(days=400),
                item.revision.created_at,
            )
        )
        attempt = ReportingMaterializationAttempt(
            item.scope,
            item.revision.reporting_revision_id,
            "preactivation-import",
            1,
            item.revision.created_at,
        )
        await h.store.commit_materialization_attempt(attempt)
        external = ReportingDestinationRequest.from_binding(
            item.binding, attempt, item.verifier.key
        ).external_id
        await h.store.import_pending_materialization(
            scope=item.scope,
            reporting_materialization_id=attempt.reporting_materialization_id,
            original_external_id=external,
            keys=item.keys,
        )
        await h.production.activate(account_id=item.config.account_id)
        original = await h.works()
        await h.production.aclose()
        observed = []
        original_finish = type(h.store).finish_materialization

        async def observe_finish(self, lease, **kwargs):
            result = await original_finish(self, lease, **kwargs)
            observed.append((lease, kwargs, result))
            return result

        monkeypatch.setattr(type(h.store), "finish_materialization", observe_finish)
        async with production_harness(
            backend,
            path,
            notifications=notifications,
            count=1,
            existing_store=h.store if h.pool is None else None,
            existing_pool=h.pool,
        ) as fresh:
            # start() waits for its first real worker turn. The cold support
            # has already resumed and completed the original pending effect.
            assert await fresh.works() == tuple((key, "acked", gen) for key, _, gen in original)
            assert len(observed) == 1
            lease, arguments, result = observed[0]
            await fresh.production.activate(account_id=item.config.account_id)
            assert lease.admission_epoch == 0
            assert lease.attempt == attempt and lease.request.external_id == external
            assert result.state == "verified"
            assert fresh.item.writer.writes == 1
            assert await production_queue(fresh) == ((), ())
            events, work = await fresh.queue()
            assert len(events) == int(notifications)
            assert work == (("quarantined",) if notifications else ())
            assert await fresh.store.read_production_boundaries(caller=item.binding.principal) == ()
            assert (
                len(await fresh.store.read_materializer_boundaries(caller=item.binding.principal))
                == 1
            )
            frozen = await fresh.image()
            assert (
                await fresh.store.finish_materialization(lease, **arguments)
            ).state == "verified"
            assert await fresh.image() == frozen
            await drain(fresh.projection, item.config.account_id)
            assert await production_queue(fresh) == ((), ())
            assert await fresh.queue() == (events, work)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("source_changes", [None, "readability", "configuration"])
async def test_producer_lease_bookkeeping_preserves_io_but_real_source_change_fences_it(
    backend, notifications, source_changes, tmp_path
):
    async with production_harness(
        backend, tmp_path / "destination.sqlite", notifications=notifications, count=1
    ) as h:
        item = h.item
        await h.production.activate(account_id=item.config.account_id)
        lease = await item.claim()
        assert isinstance(lease, ReportingMaterializerLease) and lease.admission_epoch == 2
        prepared, verified = await item.verified(lease)
        first = await h.store.read_reporting_feed(feed_request(item), caller=item.binding.principal)
        frozen = await walk(h.store, feed_request(item), item.binding.principal, first=first)
        # The source takes and releases its real fair configuration lease
        # while a verified external effect is still awaiting its fenced finish.
        turn = await source_turn(h.production)
        assert turn.leased is not None and not turn.revisions_committed
        producer = h.production.offerings[0].producer
        token = h.production._producer_turn.set(producer)
        try:

            async def acquire(worker, seconds):
                return await h.store.lease_period_close(
                    worker_id=worker,
                    now=h.source_clock() + timedelta(seconds=seconds),
                    lease_seconds=1,
                )

            crashed = await acquire("crashed-producer", 0)
            assert crashed is not None
            assert await acquire("duplicate-producer", 0) is None
            recovered = await acquire("recovered-producer", 2)
            assert recovered is not None
            await h.store.release_period_close(crashed, worker_id="crashed-producer")
            assert await acquire("duplicate-producer", 2) is None
            await h.store.release_period_close(recovered, worker_id="recovered-producer")
            for seconds in (3, 4):
                repeated = await acquire("scheduled-producer", seconds)
                assert repeated is not None
                await h.store.release_period_close(repeated, worker_id="scheduled-producer")
        finally:
            h.production._producer_turn.reset(token)
        assert (
            await walk(h.store, feed_request(item), item.binding.principal, first=first) == frozen
        )
        if source_changes == "readability":
            await h.store.set_revision_readable(
                account_id=item.config.account_id,
                reporting_revision_id=item.revision.reporting_revision_id,
                readable=False,
            )
        elif source_changes == "configuration":
            from dataclasses import replace

            await h.store.put_configuration(replace(item.config, deactivated_at=h.clock()))
        result = await h.store.finish_materialization(lease, prepared=prepared, verified=verified)
        assert result.state == ("failed" if source_changes else "verified")
        outcomes = await item.outcomes()
        assert len(outcomes) == 1
        assert (
            outcomes[0].reporting_materialization_id == lease.attempt.reporting_materialization_id
        )
        assert item.writer.writes == 1
        assert len((await production_queue(h))[0]) == int(notifications and not source_changes)
        assert await h.queue() == ((), ())
        committed = await h.image()
        assert (
            await h.store.finish_materialization(lease, prepared=prepared, verified=verified)
            == result
        )
        assert await h.image() == committed


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("release", [False, True])
async def test_pg_lease_bookkeeping_fault_rolls_back_configuration_candidate_and_fairness(
    notifications, release, monkeypatch, tmp_path
):
    async with production_harness(
        "postgres", tmp_path / "destination.sqlite", notifications=notifications, count=1
    ) as h:
        await h.production.activate(account_id=h.item.config.account_id)
        pending = await h.item.claim()
        assert isinstance(pending, ReportingMaterializerLease)
        producer = h.production.offerings[0].producer
        token = h.production._producer_turn.set(producer)
        try:

            async def acquire():
                return await h.store.lease_period_close(
                    worker_id="bookkeeping-fault", now=h.source_clock(), lease_seconds=30
                )

            lease = await acquire() if release else None
            before = await h.image()
            async with h.pool.connection() as c:
                ranks = await (
                    await c.execute(
                        "SELECT * FROM adcp_reporting_configuration_lease_turns"
                        " ORDER BY account_id,delivery_config_id,delivery_config_version"
                    )
                ).fetchall()
            with postgres_failure(
                monkeypatch, "UPDATE reporting_materializer_candidates SET generation=generation-1"
            ) as hit:
                with pytest.raises(OSError, match="injected transaction boundary"):
                    if release:
                        await h.store.release_period_close(lease, worker_id="bookkeeping-fault")
                    else:
                        await acquire()
                assert len(hit) == 1
            assert await h.image() == before
            async with h.pool.connection() as c:
                assert (
                    await (
                        await c.execute(
                            "SELECT * FROM adcp_reporting_configuration_lease_turns"
                            " ORDER BY account_id,delivery_config_id,delivery_config_version"
                        )
                    ).fetchall()
                    == ranks
                )
            lease = lease if release else await acquire()
            assert lease is not None
            await h.store.release_period_close(lease, worker_id="bookkeeping-fault")
            # A stale or duplicate release cannot remove another generation.
            image = await h.image()
            await h.store.release_period_close(lease, worker_id="bookkeeping-fault")
            assert await h.image() == image
            prepared, verified = await h.item.verified(pending)
            assert (
                await h.store.finish_materialization(pending, prepared=prepared, verified=verified)
            ).state == "verified"
        finally:
            h.production._producer_turn.reset(token)
