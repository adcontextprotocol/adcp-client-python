"""New status queue identity, old-worker exclusion and same-transaction rollback."""

import pytest

from adcp.reporting.outbox.status_memory import InMemoryReportingStatusOutbox

from ._projection_support import projection_harness
from ._receipt_support import receipt_case, request_for


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("fault", [False, True])
async def test_status_queue_isolation_and_atomic_projection(backend, fault, monkeypatch):
    async with projection_harness(backend, notifications=True) as h:
        case = await receipt_case(h)
        account = case.obligation.account_id
        if h.pool is None:
            old = InMemoryReportingStatusOutbox(h.store)
        else:
            from adcp.reporting.outbox.status_pg import PgReportingStatusOutbox

            old = PgReportingStatusOutbox(pool=h.pool)
        assert await old.list_events(account_id=account) == ()
        original_readiness = await h.queue()
        await h.projection.activate(account_id=account)
        assert await h.projection.outbox.list_events(account_id=account) == ()
        await h.store.ingest_receipt_batch(request_for(case), caller=case.binding.principal)
        before = await h.image()
        if fault:
            if h.pool is None:
                original = h.projection._enqueue_status

                def fail(event):
                    original(event)
                    raise RuntimeError("injected status enqueue failure")

                method = "_enqueue_status"
            else:
                original = h.projection._enqueue_status_on

                async def fail(connection, event):
                    await original(connection, event)
                    raise RuntimeError("injected status enqueue failure")

                method = "_enqueue_status_on"
            with monkeypatch.context() as patch:
                patch.setattr(h.projection, method, fail)
                with pytest.raises(RuntimeError, match="injected status enqueue failure"):
                    await h.projection.project_one(account_id=account)
            assert await h.image() == before
        turn = await h.projection.project_one(account_id=account)
        assert turn.did_work and turn.events > 0
        events = await h.projection.outbox.list_events(account_id=account)
        assert len(events) == turn.events
        assert all(e.notification_type == "reporting.status_changed" for e in events)
        assert await old.list_events(account_id=account) == ()
        assert (
            await old.claim_expansion(account_id=account, now=h.clock(), lease_seconds=30) is None
        )
        assert await h.queue() == original_readiness
        # Persist a real expansion lease, then verify a competing incarnation
        # cannot claim it and a separately constructed new outbox resumes it.
        current = h.projection.outbox
        lease = await current.claim_expansion(account_id=account, now=h.clock(), lease_seconds=30)
        assert lease is not None
        restarted = type(current)(h.store) if h.pool is None else type(current)(pool=h.pool)
        await restarted.complete_expansion(lease, (), now=h.clock())
        assert await current.list_events(account_id=account) == events
        assert await h.queue() == original_readiness
        await h.store.ingest_receipt_batch(request_for(case), caller=case.binding.principal)
        assert not (await h.projection.project_one(account_id=account)).did_work
        assert await current.list_events(account_id=account) == events
