"""Automatic reads spread over worker turns and retain their persisted phase."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import InMemoryReportingLedgerStore, ReportingProducer
from tests.test_reporting_settling import _capabilities as settling_capabilities
from tests.test_reporting_source_availability import (
    ProgressStore,
    _capabilities,
    _closing_capabilities,
    _harness,
)

WINDOW = timedelta(minutes=5)


@pytest.mark.parametrize("store_class", [InMemoryReportingLedgerStore, ProgressStore])
async def test_first_read_jitter_spreads_one_minute_turns_after_actual_readiness(store_class):
    producer, _, configuration, obligation, calls, clock = await _harness(
        store_class, read_jitter_window=WINDOW
    )
    ready = producer._source_available_at(obligation)
    dues = [
        producer._scheduled_source_ready_at(
            replace(configuration, consumer_id=f"buyer-{index}"),
            replace(obligation, consumer_id=f"buyer-{index}"),
        )
        for index in range(200)
    ]
    assert all(ready <= due <= ready + WINDOW for due in dues)
    assert len({(due - ready) // timedelta(minutes=1) for due in dues}) == 5
    due = producer._scheduled_source_ready_at(configuration, obligation)
    assert due > ready
    clock[0] = due - timedelta(microseconds=1)
    waiting = await producer.run_configuration(configuration, now=clock[0])
    assert not waiting.revisions_committed and not calls
    restarted = ReportingProducer(
        source=producer._source,
        offerings=producer._offerings,
        store=producer.store,
        object_reader=producer._object_reader,
        clock=lambda: clock[0],
    )
    assert restarted._scheduled_source_ready_at(configuration, obligation) == due
    clock[0] = due
    published = await restarted.run_configuration(configuration, now=clock[0])
    assert len(published.revisions_committed) == 1 and calls == [due]


@pytest.mark.parametrize("store_class", [InMemoryReportingLedgerStore, ProgressStore])
async def test_consumer_phases_survive_restart_without_double_jitter(store_class):
    producer, store, configuration, obligation, calls, clock = await _harness(
        store_class,
        read_jitter_window=WINDOW,
        capabilities=_capabilities(restatement_cadence="PT1H", restatement_window="P3D"),
    )
    other_configuration = replace(configuration, consumer_id="buyer-other")
    await store.put_configuration(other_configuration)
    other = (await producer.close_elapsed_periods(other_configuration, now=clock[0]))[0]
    records = sorted(
        [(configuration, obligation), (other_configuration, other)],
        key=lambda pair: producer._scheduled_source_ready_at(*pair),
    )
    dues = [producer._scheduled_source_ready_at(*pair) for pair in records]
    assert dues[0] != dues[1]
    for pair, due in zip(records, dues):
        clock[0] = due
        await producer.run_configuration(pair[0], now=due)
    assert calls == dues
    next_dues = []
    for _, record in records:
        observation = await store.get_provisional_observation(
            account_id=record.account_id, reporting_obligation_id=record.reporting_obligation_id
        )
        next_dues.append(observation.next_due_at)
    assert next_dues == [due + timedelta(hours=1) for due in dues]
    # A deployment changing the first-read window cannot alter a retained
    # observation's cadence or add another phase offset.
    restarted = ReportingProducer(
        source=producer._source,
        offerings=producer._offerings,
        store=store,
        object_reader=producer._object_reader,
        clock=lambda: clock[0],
        read_jitter_window=timedelta(minutes=50),
    )
    for pair, due in zip(records, next_dues):
        clock[0] = due - timedelta(microseconds=1)
        before = len(calls)
        await restarted.run_configuration(pair[0], now=clock[0])
        assert len(calls) == before
        clock[0] = due
        await restarted.run_configuration(pair[0], now=clock[0])
        observation = await store.get_provisional_observation(
            account_id=pair[1].account_id, reporting_obligation_id=pair[1].reporting_obligation_id
        )
        assert observation.checked_at == due
        assert observation.next_due_at == due + timedelta(hours=1)
    assert calls == dues + next_dues
    assert next_dues[1] - next_dues[0] == dues[1] - dues[0]


async def test_jitter_is_stable_and_bounded_by_sla_short_period_and_cadence():
    producer, _, configuration, obligation, _, _ = await _harness(read_jitter_window=WINDOW)
    ready = producer._source_available_at(obligation)
    assert producer._scheduled_source_ready_at(
        configuration, obligation
    ) == producer._scheduled_source_ready_at(configuration, obligation)
    tight = replace(
        obligation, period=replace(obligation.period, expected_at=ready + timedelta(seconds=2))
    )
    assert (
        ready
        <= producer._scheduled_source_ready_at(configuration, tight)
        <= ready + timedelta(seconds=2)
    )
    overdue = replace(obligation, period=replace(obligation.period, expected_at=ready))
    assert producer._scheduled_source_ready_at(configuration, overdue) == ready
    short = replace(
        obligation,
        period=replace(obligation.period, start=obligation.period.end - timedelta(seconds=20)),
    )
    assert (
        ready
        <= producer._scheduled_source_ready_at(configuration, short)
        <= ready + timedelta(seconds=2)
    )
    producer._read_jitter_window = timedelta(0)
    assert producer._scheduled_source_ready_at(configuration, obligation) == ready
    with pytest.raises(ValueError, match="nonnegative"):
        ReportingProducer(
            source=producer._source,
            offerings=producer._offerings,
            store=producer.store,
            read_jitter_window=timedelta(seconds=-1),
        )

    cadence_producer, _, config, record, _, _ = await _harness(
        read_jitter_window=timedelta(minutes=50),
        capabilities=_capabilities(restatement_cadence="PT1H", restatement_window="P3D"),
    )
    cadence_ready = cadence_producer._source_available_at(record)
    assert (
        cadence_ready
        <= cadence_producer._scheduled_source_ready_at(config, record)
        <= cadence_ready + timedelta(minutes=6)
    )


async def test_jitter_keeps_explicit_official_boundary_and_manual_acquisition():
    producer, store, configuration, obligation, calls, clock = await _harness(
        read_jitter_window=WINDOW, capabilities=_closing_capabilities()
    )
    ready = producer._source_available_at(obligation)
    clock[0] = ready
    revision = await producer.acquire_obligation(
        configuration, obligation, now=ready, track_settling=True, manual_replay=True
    )
    assert revision is not None and calls == [ready]
    clock[0] = obligation.period.end + timedelta(hours=6)
    closed = await producer.run_configuration(configuration, now=clock[0])
    assert len(closed.revisions_committed) == 1 and calls[-1] == clock[0]
    observation = await store.get_provisional_observation(
        account_id=obligation.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert observation.acquisition.request().period.source_read_cutoff_at == clock[0]


@pytest.mark.parametrize("store_class", [InMemoryReportingLedgerStore, ProgressStore])
async def test_manual_pending_retry_keeps_its_clock_before_first_automatic_phase(store_class):
    producer, store, configuration, obligation, _, clock = await _harness(
        store_class, read_jitter_window=WINDOW, restatement_window="PT1H"
    )
    source = producer._source
    original_fetch = source._fetch
    requests = []

    def unavailable_once(request):
        requests.append(request)
        return None if len(requests) == 1 else original_fetch(request)

    source._fetch = unavailable_once
    producer._retry_initial_delay = timedelta(seconds=1)
    ready = producer._source_available_at(obligation)
    clock[0] = ready
    await producer.acquire_obligation(
        configuration, obligation, now=ready, track_settling=True, manual_replay=True
    )
    retry = await store.get_retry_schedule(scope_key=producer._retry_keys(obligation)[0])
    assert (
        ready
        < retry.retry_not_before
        < producer._scheduled_source_ready_at(configuration, obligation)
    )
    restarted = ReportingProducer(
        source=source,
        offerings=producer._offerings,
        store=store,
        object_reader=producer._object_reader,
        clock=lambda: clock[0],
        read_jitter_window=WINDOW,
    )
    clock[0] = retry.retry_not_before - timedelta(microseconds=1)
    await restarted.run_configuration(configuration, now=clock[0])
    assert len(requests) == 1
    clock[0] = retry.retry_not_before
    await restarted.run_configuration(configuration, now=clock[0])
    assert len(requests) == 2 and requests[1].identity == requests[0].identity
    assert requests[1].period.source_read_cutoff_at == requests[0].period.source_read_cutoff_at
    owner = dict(
        account_id=configuration.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )
    recovered = await store.get_provisional_observation(**owner)
    assert recovered.acquisition.request().trigger == "manual_replay"
    scheduled_due = producer._scheduled_source_ready_at(configuration, obligation)
    clock[0] = scheduled_due - timedelta(microseconds=1)
    await restarted.run_configuration(configuration, now=clock[0])
    assert len(requests) == 2
    clock[0] = scheduled_due
    await restarted.run_configuration(configuration, now=clock[0])
    assert [r.trigger for r in requests] == ["manual_replay", "manual_replay", "scheduled_poll"]
    assert requests[2].identity.source_execution_key != requests[0].identity.source_execution_key
    history = await store.list_provisional_observations(**owner, limit=3)
    assert [o.acquisition.request().trigger for o in history] == ["manual_replay", "scheduled_poll"]
    assert [o.is_scheduled for o in history] == [False, True]
    assert history[-1].checked_at == scheduled_due


async def test_successful_manual_snapshot_keeps_immediate_automatic_official_close():
    producer, store, configuration, obligation, calls, clock = await _harness(
        read_jitter_window=WINDOW,
        capabilities=settling_capabilities(restatement_window="PT0S", official_close_lag="PT0S"),
    )
    ready = producer._source_available_at(obligation)
    assert ready < producer._scheduled_source_ready_at(configuration, obligation)
    clock[0] = ready
    snapshot = await producer.acquire_obligation(
        configuration,
        obligation,
        now=ready,
        target_finality="snapshot",
        track_settling=True,
        manual_replay=True,
    )
    assert snapshot is not None and snapshot.finality == "snapshot"
    closed = await producer.run_configuration(configuration, now=ready)
    assert len(closed.revisions_committed) == 1
    revisions = await store.list_revisions(
        account_id=obligation.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert {revision.finality for revision in revisions} == {"snapshot", "official"}
    assert calls == [ready, ready]
