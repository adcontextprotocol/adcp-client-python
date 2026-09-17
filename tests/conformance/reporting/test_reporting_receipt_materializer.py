"""Receipt composition preserves the one reviewed materializer transaction."""

from datetime import datetime, timezone

import pytest

from adcp.reporting.materializer import ReportingWriterError
from adcp.reporting.materializer.capture import MaterializerNotificationState

from ._durable_materializer_support import durable_case
from ._receipt_support import receipt_harness
from ._reliable_support import ManualClock
from .test_reporting_receipt_transactions import fail_after


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_receipt_store_atomic_finish_capture_ack_and_enabled_enqueue(backend, monkeypatch):
    async with receipt_harness(backend, notifications=True) as h:
        h.clock = ManualClock(datetime.now(timezone.utc))
        h.store._clock = h.clock if h.pool is None else None
        case = await durable_case(h.store, reconciliation_mode="consumer_receipt")
        lease = await case.claim()
        prepared, verified = await case.verified(lease)
        before = await h.image()
        if h.pool is None:
            fail_after(monkeypatch, MaterializerNotificationState, "enqueue")
        else:
            from psycopg import AsyncConnection

            fail_after(
                monkeypatch,
                AsyncConnection,
                "execute",
                asynchronous=True,
                predicate=lambda args, _: isinstance(args[1], str)
                and args[1].startswith("INSERT INTO reporting_materializer_notification_events"),
            )
        with pytest.raises(ReportingWriterError):
            await h.store.finish_materialization(lease, prepared=prepared, verified=verified)
        monkeypatch.undo()
        assert await h.image() == before
        assert (
            await h.store.finish_materialization(lease, prepared=prepared, verified=verified)
        ).state == "verified"
        captured = await h.store.read_materializer_boundaries(caller=case.scope.principal)
        assert len(captured) == 1 and captured[0].account_sequence == 1
        assert (await h.queue())[1] == ("quarantined",)
        assert (await h.works())[0][1] == "acked"
        assert not await h.store.read_receipt_boundaries(caller=case.scope.principal)
        assert "reporting.delivery_ready" not in await h.ordinary_events()


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_receipt_composition_preserves_complete_parent_notification_advertisement_veto(
    backend, monkeypatch
):
    # Reuse the reviewed same-store vector, now instantiated with the additive
    # receipt store. This covers Core ledger_changed too, not just Managed.
    from . import test_reporting_materializer_durable as parent_vectors

    monkeypatch.setattr(parent_vectors, "durable_harness", receipt_harness)
    await parent_vectors.test_materializer_store_closes_core_advertisement_until_b24_admits_it(
        backend
    )
