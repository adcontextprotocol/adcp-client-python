"""Faults at every reserve/finish write boundary with notifications off and on."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import ReportingMaterializationRecord
from adcp.reporting.materializer import ReportingWriterError
from adcp.reporting.materializer.memory import InMemoryReportingMaterializerStore

from ._durable_materializer_support import durable_case, durable_harness


@pytest.mark.parametrize("notifications", [False, True])
async def test_failed_memory_snapshot_does_not_leak_transaction_ownership(notifications):
    class Uncopyable:
        def __deepcopy__(self, memo):
            raise ValueError("injected snapshot fault")

    async with durable_harness("memory", notifications=notifications) as h:
        case = await durable_case(h.store)
        changed = replace(case.config, status_retention_days=case.config.status_retention_days + 1)
        h.store._injected_snapshot_fault = Uncopyable()
        with pytest.raises(ValueError, match="injected snapshot fault"):
            await h.store.put_configuration(changed)
        del h.store._injected_snapshot_fault
        before = await h.image()
        with pytest.raises(RuntimeError, match="rollback subsequent transaction"):
            async with h.store.transaction():
                await h.store.put_configuration(changed)
                raise RuntimeError("rollback subsequent transaction")
        assert await h.image() == before


@contextmanager
def postgres_failure(monkeypatch, prefix):
    from psycopg import AsyncConnection

    original = AsyncConnection.execute
    hit = []

    async def execute(self, query, *args, **kwargs):
        result = await original(self, query, *args, **kwargs)
        if isinstance(query, str) and query.startswith(prefix):
            hit.append(self.pgconn.backend_pid)
            raise OSError("injected transaction boundary")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "execute", execute)
        yield hit


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("position", ["delivery", "attempt", "work", "lease"])
async def test_reservation_rolls_back_attempt_work_delivery_and_sequence_heads(
    backend, notifications, position, monkeypatch
):
    async with durable_harness(backend, notifications=notifications) as h:
        case = await durable_case(h.store)
        before = await h.image()
        if backend == "postgres":
            prefixes = {
                "delivery": "INSERT INTO reporting_reconciliation_records",
                "attempt": "INSERT INTO reporting_reconciliation_changes",
                "work": "INSERT INTO reporting_materializer_work",
                "lease": "UPDATE reporting_materializer_work SET lease_token=",
            }
            # Count explicitly so attempt failure happens after the delivery's
            # own complete insertion, not at its earlier feed append.
            if position == "attempt":
                from adcp.reporting.materializer.pg import PgReportingMaterializerStore

                original = PgReportingMaterializerStore._commit_record_on

                async def fail(self, connection, record, **kwargs):
                    result = await original(self, connection, record, **kwargs)
                    if record.kind == "materialization_attempt":
                        raise OSError("injected transaction boundary")
                    return result

                with monkeypatch.context() as patch:
                    patch.setattr(PgReportingMaterializerStore, "_commit_record_on", fail)
                    with pytest.raises(ReportingWriterError):
                        await case.claim()
            else:
                with postgres_failure(monkeypatch, prefixes[position]) as hit:
                    with pytest.raises(ReportingWriterError):
                        await case.claim()
                    assert len(hit) == 1
        else:
            cls = InMemoryReportingMaterializerStore
            if position in {"delivery", "attempt"}:
                original = cls._commit_record_unlocked

                def fail(self, record, **kwargs):
                    result = original(self, record, **kwargs)
                    if (
                        record.kind
                        == {
                            "delivery": "obligation_delivery",
                            "attempt": "materialization_attempt",
                        }[position]
                    ):
                        raise OSError("injected transaction boundary")
                    return result

                method = "_commit_record_unlocked"
            else:
                original = cls._lease

                def fail(self, *args):
                    if position == "lease":
                        original(self, *args)
                    raise OSError("injected transaction boundary")

                method = "_lease"
            with monkeypatch.context() as patch:
                patch.setattr(cls, method, fail)
                with pytest.raises(ReportingWriterError):
                    await case.claim()
        assert await h.image() == before
        lease = await case.claim()
        assert lease.attempt.attempt == 1
        assert len(await h.works()) == 1


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("position", ["outcome", "status_head", "account_head", "boundary", "ack"])
async def test_finish_rolls_back_terminal_capture_ack_and_all_initialized_heads(
    backend, notifications, position, monkeypatch
):
    async with durable_harness(backend, notifications=notifications) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        prepared, verified = await case.verified(lease)
        before = await h.image()

        async def finish():
            return await h.store.finish_materialization(lease, prepared=prepared, verified=verified)

        if backend == "postgres":
            prefixes = {
                "outcome": "INSERT INTO reporting_reconciliation_records",
                "status_head": "INSERT INTO reporting_materializer_status_heads",
                "account_head": "UPDATE reporting_materializer_accounts SET captured_sequence=",
                "boundary": "INSERT INTO reporting_materializer_status_boundaries",
                "ack": "UPDATE reporting_materializer_work SET state='acked'",
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
                        raise OSError("injected transaction boundary")
                    return result

                target, method = cls, "_commit_record_unlocked"
            elif position in {"status_head", "account_head"}:

                def fail(*args, **kwargs):
                    raise OSError("injected transaction boundary")

                target, method = memory, "ReportingMaterializerBoundary"
            else:
                method = "_materializer_dirty" if position == "boundary" else "_park"
                target, original = cls, getattr(cls, method)

                def fail(self, *args, **kwargs):
                    original(self, *args, **kwargs)
                    raise OSError("injected transaction boundary")

            with monkeypatch.context() as patch:
                patch.setattr(target, method, fail)
                with pytest.raises(ReportingWriterError):
                    await finish()
        assert await h.image() == before
        materializer_operation_1 = await finish()
        assert (materializer_operation_1).state == "verified"
        assert len(await case.outcomes()) == 1
        assert len(await h.store.read_materializer_boundaries(caller=case.scope.principal)) == 1


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("position", ["event", "expansion_work"])
async def test_enabled_logical_enqueue_failure_never_downgrades_to_polling(
    backend, position, monkeypatch
):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        prepared, verified = await case.verified(lease)
        before = await h.image()

        async def finish():
            return await h.store.finish_materialization(lease, prepared=prepared, verified=verified)

        if backend == "postgres":
            table = "events" if position == "event" else "expansions"
            with postgres_failure(
                monkeypatch, f"INSERT INTO reporting_materializer_notification_{table}"
            ) as hit:
                with pytest.raises(ReportingWriterError):
                    await finish()
                assert len(hit) == 1
        else:
            import adcp.reporting.outbox.memory as memory
            from adcp.reporting.materializer.capture import MaterializerNotificationState

            if position == "event":
                target, method = memory, "_Work"

                def fail(*args, **kwargs):
                    raise OSError("injected transaction boundary")

            else:
                target, method = MaterializerNotificationState, "enqueue"
                original = target.enqueue

                def fail(self, event):
                    original(self, event)
                    raise OSError("injected transaction boundary")

            with monkeypatch.context() as patch:
                patch.setattr(target, method, fail)
                with pytest.raises(ReportingWriterError):
                    await finish()
        assert await h.image() == before
        assert await h.queue() == ((), ())
        materializer_operation_2 = await finish()
        assert (materializer_operation_2).state == "verified"
        events, work = await h.queue()
        assert len(events) == 1 and work == ("quarantined",)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_missing_enabled_logical_event_cannot_ack_a_verified_finish(backend, monkeypatch):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        prepared, evidence = await case.verified(lease)
        before = await h.image()
        if backend == "memory":
            from adcp.reporting.materializer.capture import MaterializerNotificationState

            def empty(self, event):
                pass

            monkeypatch.setattr(MaterializerNotificationState, "enqueue", empty)
        else:
            import adcp.reporting.materializer.pg as pg

            async def empty(connection, event):
                pass

            monkeypatch.setattr(pg, "enqueue_materializer_event_on", empty)
        with pytest.raises(ReportingWriterError):
            await h.store.finish_materialization(lease, prepared=prepared, verified=evidence)
        assert await h.image() == before
        assert await h.queue() == ((), ())


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_expiry_inside_finish_rolls_back_outcome_capture_event_and_ack(
    backend, notifications, monkeypatch
):
    async with durable_harness(backend, notifications=notifications) as h:
        case = await durable_case(h.store)
        lease = await case.claim(lease_seconds=3)
        prepared, evidence = await case.verified(lease)
        before = await h.image()
        cls = type(h.store)
        if backend == "memory":
            original = cls._materializer_dirty

            def expire(self, *args):
                original(self, *args)
                h.clock.advance(timedelta(seconds=4))

            method = "_materializer_dirty"
        else:
            original = cls._materializer_dirty_on

            async def expire(self, connection, *args):
                await original(self, connection, *args)
                await connection.execute(
                    "UPDATE reporting_materializer_work SET lease_until=clock_timestamp()"
                    " WHERE state='pending'"
                )

            method = "_materializer_dirty_on"
        with monkeypatch.context() as patch:
            patch.setattr(cls, method, expire)
            with pytest.raises(ReportingWriterError):
                await h.store.finish_materialization(lease, prepared=prepared, verified=evidence)
        assert await h.image() == before and await h.queue() == ((), ())
        await h.expire()
        materializer_operation_3 = await case.service().run_once()
        assert (materializer_operation_3).state == "verified"
        assert case.writer.write_effects == 1
        assert len(await h.works()) == 1
