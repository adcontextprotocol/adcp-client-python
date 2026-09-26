"""Capability failure is explicit; one observation conflict cannot starve siblings."""

from datetime import timedelta

import pytest

from adcp.reporting.ledger import LedgerConflictError

from ._production_support import production_harness
from .test_reporting_production_lock_order import source_turn


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_one_observation_conflict_preserves_sibling_progress_and_retry(backend, tmp_path):
    async with production_harness(
        backend, tmp_path / "destination.sqlite", count=0, periods=3
    ) as h:
        store, support = h.store, h.production
        source = support.offerings[0].producer._source
        h.source_clock.advance(timedelta(hours=3))
        await support.activate(account_id=h.item.config.account_id)
        identifier = h.item.obligation.reporting_obligation_id
        original = store.reserve_provisional_acquisition

        async def conflict(acquisition):
            if acquisition.obligation_id == identifier:
                raise LedgerConflictError("OBSERVATION_CONFLICT", "concurrent observation won")
            return await original(acquisition)

        store.reserve_provisional_acquisition = conflict
        first = await source_turn(support)
        assert first.slices_failed == [identifier]
        assert len(first.revisions_committed) == 2
        assert len(source.requests) == 2
        identity = {"account_id": h.item.config.account_id, "reporting_obligation_id": identifier}
        assert await store.get_provisional_observation(**identity) is None
        assert await store.get_restatement_checkpoint(**identity) is None
        store.reserve_provisional_acquisition = original
        second = await source_turn(support)
        assert not second.slices_failed
        assert len(second.revisions_committed) == 1
        assert len(source.requests) == 3
        assert len({request.identity.source_execution_key for request in source.requests}) == 3
        observation = await store.get_provisional_observation(**identity)
        assert observation is not None and observation.acquisition.ordinal == 1
        assert (
            observation.acquisition.predecessor_revision_id == h.item.revision.reporting_revision_id
        )


async def test_basic_ledger_observation_conflict_does_not_abort_later_periods():
    from tests.test_reporting_settling import _capabilities, _harness

    producer, store, fetch, clock = await _harness(_capabilities(restatement_window=None))
    producer._max_periods_per_turn = 2
    clock[0] += timedelta(hours=1)
    original = store.reserve_provisional_acquisition
    conflicted = []

    async def conflict(acquisition):
        if not conflicted:
            conflicted.append(acquisition.obligation_id)
            raise LedgerConflictError("OBSERVATION_CONFLICT", "concurrent observation won")
        return await original(acquisition)

    store.reserve_provisional_acquisition = conflict
    first = await producer.run_worker()
    assert first.slices_failed == conflicted
    assert len(first.revisions_committed) == len(fetch.calls) == 1
    store.reserve_provisional_acquisition = original
    second = await producer.run_worker()
    assert not second.slices_failed
    assert len(second.revisions_committed) == 1
    assert len(fetch.calls) == 2


@pytest.mark.parametrize("explicit_checkpoints", [False, True])
async def test_dynamic_protocol_forwarding_requires_explicit_publisher_composition(
    explicit_checkpoints,
):
    from tests.test_reporting_settling import _capabilities, _harness

    producer, store, fetch, _ = await _harness(_capabilities(restatement_window=None))

    class Proxy:
        def __getattr__(self, name):
            return getattr(store, name)

    class CheckpointProxy(Proxy):
        async def get_restatement_checkpoint(self, **identity):
            return await store.get_restatement_checkpoint(**identity)

        async def record_restatement_checkpoint(self, checkpoint):
            return await store.record_restatement_checkpoint(checkpoint)

    producer._store = CheckpointProxy() if explicit_checkpoints else Proxy()
    with pytest.raises(LedgerConflictError) as failure:
        await producer.run_worker()
    assert failure.value.code == (
        "PROVISIONAL_OBSERVATIONS_NOT_SUPPORTED"
        if explicit_checkpoints
        else "RESTATEMENT_CHECKPOINTS_NOT_SUPPORTED"
    )
    assert fetch.calls == []
    assert not store._provisional_acquisitions
    assert not store._provisional_observations
    assert not store._revisions
