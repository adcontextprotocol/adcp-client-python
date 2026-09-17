"""Long I/O, reauthorization, cleanup and uncertain-effect service convergence."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    ReportingMaterializationAttempt,
    ReportingMaterializationCheck,
    ReportingObligationDeliveryRecord,
)
from adcp.reporting.materializer import (
    ReportingDestinationIO,
    ReportingMaterializerService,
    ReportingWriterError,
    ReportingWriterFailure,
)

from ._durable_materializer_support import durable_case, durable_harness
from .test_reporting_materializer_lifecycle import SECRET, Phases, safe_exception


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("stage", ["write-before", "write-after", "object", "readback-resolve"])
async def test_cancellation_and_restart_never_allocate_a_new_external_identity(
    backend, stage, tmp_path, caplog
):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store, count=501)
        owner = Phases(case, tmp_path, stage, "cancel")
        service = ReportingMaterializerService(
            h.store, ReportingDestinationIO(case.registry, owner), case.writer, lease_seconds=3
        )
        before = set(asyncio.all_tasks())
        task = asyncio.create_task(service.run_once())
        await asyncio.wait_for(owner.entered.wait(), 10)
        initial = await h.works()
        task.cancel(SECRET)
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(task, 10)
        safe_exception(error.value)
        assert all(s._closed and s._credential is None for s in owner.sessions)
        assert not await case.outcomes() and await h.queue() == ((), ())
        assert not list(tmp_path.iterdir())
        assert not (set(asyncio.all_tasks()) - before)
        await h.expire()
        assert (await case.service().run_once()).state == "verified"
        works = await h.works()
        assert len(works) == 1 and works[0][0] == initial[0][0]
        assert case.writer.write_effects == 1
        assert case.writer.open_count == case.writer.close_count
        assert SECRET not in caplog.text


@pytest.mark.parametrize("stage", ["write-after", "object"])
async def test_service_heartbeat_keeps_long_io_alive_with_size_one_postgres_pool(
    stage, tmp_path, monkeypatch
):
    from adcp.reporting.materializer import PgReportingMaterializerStore

    async with durable_harness("postgres", notifications=True) as h:
        from psycopg_pool import AsyncConnectionPool

        case = await durable_case(h.store, count=501)
        async with AsyncConnectionPool(
            h.pool.conninfo, kwargs=h.pool.kwargs, min_size=1, max_size=1, open=False
        ) as single:
            store = PgReportingMaterializerStore(pool=single, notifications=True)
            case.store = store
            owner = Phases(case, tmp_path, stage, "wait")
            renewed, turns = asyncio.Event(), []
            original = store.renew_materialization

            async def renew(*args, **kwargs):
                held = await original(*args, **kwargs)
                turns.append(held)
                if len(turns) == 4:
                    renewed.set()
                return held

            monkeypatch.setattr(store, "renew_materialization", renew)
            service = ReportingMaterializerService(
                store, ReportingDestinationIO(case.registry, owner), case.writer, lease_seconds=3
            )
            task = asyncio.create_task(service.run_once())
            await asyncio.wait_for(owner.entered.wait(), 10)
            await asyncio.wait_for(renewed.wait(), 10)  # Four DB-time heartbeats > one whole lease.
            assert all(turns) and len(turns) >= 4
            assert not hasattr(
                await store.claim_materialization(keys=case.keys, lease_seconds=3), "attempt"
            )
            owner.release.set()
            assert (await asyncio.wait_for(task, 10)).state == "verified"
            assert case.writer.write_effects == 1
            assert case.writer.open_count == case.writer.close_count == 2


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("change", ["rotate", "revoke", "official", "readability"])
async def test_write_to_readback_window_reauthorizes_exact_principal_and_current_generation(
    backend, change, tmp_path
):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        owner = Phases(case, tmp_path, "write-after", "wait")
        service = ReportingMaterializerService(
            h.store, ReportingDestinationIO(case.registry, owner), case.writer
        )
        task = asyncio.create_task(service.run_once())
        await asyncio.wait_for(owner.entered.wait(), 10)
        if change == "rotate":
            case.resolver.rotate()
        elif change == "revoke":
            case.resolver.revoke(case.scope.principal)
        elif change == "official":
            await case.publish()
        else:
            await h.store.set_revision_readable(
                account_id="acct_a",
                reporting_revision_id=case.revision.reporting_revision_id,
                readable=False,
            )
        owner.release.set()
        turn = await asyncio.wait_for(task, 10)
        assert turn.state == ("verified" if change == "rotate" else "failed")
        assert len((await h.queue())[0]) == int(change == "rotate")
        assert case.writer.open_count == case.writer.close_count
        assert all(s._credential is None for s in owner.sessions)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("state", ["corrupt", "unavailable"])
async def test_later_health_loss_never_resets_or_retries_successful_history(backend, state):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        assert (await case.service().run_once()).state == "verified"
        outcome = (await case.outcomes())[0]
        await h.store.record_materialization_check(
            ReportingMaterializationCheck(
                case.scope,
                outcome.reporting_materialization_id,
                "health",
                state,
                outcome.completed_at,
            )
        )
        result = await case.claim()
        assert result.reason == "operator_required"
        assert (await case.claim()).state == "idle"
        assert len(await h.works()) == 1
        assert await case.outcomes() == (outcome,)


async def test_retention_loss_between_readback_and_finish_is_immutable_safe_failure():
    async with durable_harness("memory", notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim(lease_seconds=300)
        prepared, evidence = await case.verified(lease)
        h.clock.advance(timedelta(seconds=60))
        result = await h.store.finish_materialization(lease, prepared=prepared, verified=evidence)
        assert result.state == "failed" and await h.queue() == ((), ())
        assert (await case.outcomes())[0].failure_code == "RESOURCE_UNAVAILABLE"


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("terminal", ["verified", "failed"])
async def test_public_next_attempt_is_still_permitted_after_any_terminal_outcome(backend, terminal):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        if terminal == "verified":
            prepared, evidence = await case.verified(lease)
            await h.store.finish_materialization(lease, prepared=prepared, verified=evidence)
        else:
            await h.store.finish_materialization(
                lease, error=ReportingWriterFailure("WRITE_FAILED", "never", "not_started")
            )
        outcome = (await case.outcomes())[0]
        attempt = replace(
            lease.attempt,
            attempt=2,
            reporting_materialization_id="operator-next",
            created_at=outcome.completed_at,
        )
        await h.store.commit_materialization_attempt(attempt)
        snapshot = await h.store.read_reconciliation_snapshot(caller=case.scope.principal)
        assert [
            r.attempt for r in snapshot.records if isinstance(r, ReportingMaterializationAttempt)
        ] == [1, 2]
        await h.expire()
        assert not hasattr(await case.claim(), "attempt")
        assert len(await h.works()) == 1


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_driver_failure_is_a_closed_error_without_provider_context(backend, monkeypatch):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        if backend == "memory":

            def fail(*args, **kwargs):
                raise OSError(SECRET)

            monkeypatch.setattr(type(h.store), "_context", fail)
        else:

            async def fail(*args, **kwargs):
                raise OSError(SECRET)

            monkeypatch.setattr(type(h.store), "_materializer_context_on", fail)
        with pytest.raises(ReportingWriterError) as error:
            await case.claim()
        safe_exception(error.value)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("cause", ["timeout", "cleanup"])
async def test_timeout_or_cleanup_failure_after_effect_resumes_original_identity(
    backend, cause, tmp_path, caplog
):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        # This fixture's short I/O deadline must not be its artifact lifetime.
        # Recovery reuses a conditional object that still meets the full
        # required retention window, independently of the timed-out request.
        await h.store.bind_obligation_delivery(
            ReportingObligationDeliveryRecord(
                case.scope, "USD", h.clock() + timedelta(days=401), h.clock()
            )
        )
        owner = Phases(
            case,
            tmp_path,
            "write-after" if cause == "timeout" else "close",
            "wait" if cause == "timeout" else "error",
        )
        service = ReportingMaterializerService(
            h.store,
            ReportingDestinationIO(case.registry, owner),
            case.writer,
            io_timeout_seconds=1 if cause == "timeout" else 30,
        )
        assert (await asyncio.wait_for(service.run_once(), 10)).state == "pending"
        before = await h.works()
        assert not await case.outcomes() and await h.queue() == ((), ())
        assert case.writer.write_effects == 1
        assert all(s._closed and s._credential is None for s in owner.sessions)
        assert not tuple(tmp_path.iterdir()) and SECRET not in caplog.text
        await h.expire()
        assert (await case.service().run_once()).state == "verified"
        assert (await h.works())[0][0] == before[0][0]
        assert case.writer.write_effects == 1


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_heartbeat_cancels_io_after_expiry_and_competing_worker_steals_fence(
    backend, tmp_path
):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        owner = Phases(case, tmp_path, "write-after", "wait")
        service = ReportingMaterializerService(
            h.store, ReportingDestinationIO(case.registry, owner), case.writer, lease_seconds=3
        )
        task = asyncio.create_task(service.run_once())
        try:
            await asyncio.wait_for(owner.entered.wait(), 10)
            before = await h.works()
            await h.expire()
            winner = await case.claim()
            assert (await asyncio.wait_for(task, 10)).state == "pending"
            assert all(s._closed and s._credential is None for s in owner.sessions)
            assert not tuple(tmp_path.iterdir())
            assert not await case.outcomes() and await h.queue() == ((), ())
            prepared, evidence = await case.verified(winner)
            assert (
                await h.store.finish_materialization(winner, prepared=prepared, verified=evidence)
            ).state == "verified"
            assert (await h.works())[0][0] == before[0][0]
            assert case.writer.write_effects == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
