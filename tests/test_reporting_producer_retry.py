"""Producer retry scheduling protects an unavailable reporting source."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from adcp.reporting.fixtures import (
    OFFICIAL_OFFERING_ID,
    SNAPSHOT_OFFERING_ID,
    redacted_capabilities,
)
from adcp.reporting.inline_source import (
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    ProducerOfferings,
    ReportingConfiguration,
    ReportingProducer,
    ReportingScheduleSpec,
    RetryScheduleEntry,
    WorkerTurn,
)
from adcp.reporting.source import ReportingSourceErrorV1, ReportingSourceExecutorResult

NOW = datetime(2026, 11, 1, 2, 10, tzinfo=timezone.utc)


class FailingSource:
    capabilities = redacted_capabilities()

    def __init__(
        self,
        *,
        scope: str = "slice",
        retry_after_seconds: float | None = None,
        code: str = "RATE_LIMITED",
        retry: str = "retryable",
    ):
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds
        self.code = code
        self.retry = retry
        self.calls = 0
        self.requests = []

    async def execute(self, request, *, cancel):
        self.calls += 1
        self.requests.append(request)
        return ReportingSourceExecutorResult.failed(
            ReportingSourceErrorV1(
                code=self.code,
                retry=self.retry,
                scope=self.scope,
                safe_message="provider quota exhausted",
                retry_after_seconds=self.retry_after_seconds,
            )
        )


def configuration(account: str = "account-1", config: str = "config-1") -> ReportingConfiguration:
    return ReportingConfiguration(
        delivery_config_id=config,
        delivery_config_version=1,
        account_id=account,
        consumer_id="buyer",
        report_definition_id="PAID_MEDIA_DAILY_V1",
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=ReportingScheduleSpec(
            period_duration="PT1H", delivery_sla="PT10M", alignment="utc"
        ),
        required_finality="official",
        activated_at=datetime(2026, 11, 1, 0, 20, tzinfo=timezone.utc),
        media_buy_ids=("media-buy-1",),
    )


def producer(source, store, *, clock=None, **kwargs):
    return ReportingProducer(
        source=source,
        store=store,
        offerings=ProducerOfferings(
            official_offering_id=OFFICIAL_OFFERING_ID,
            publication_namespace="reporting-source:retry-test",
        ),
        clock=clock or (lambda: NOW),
        **kwargs,
    )


async def close_admitted_periods(producer, config, **kwargs):
    """Arrange an authenticated generation before exercising period closure."""
    await producer.store.put_configuration(config)
    return await producer.close_elapsed_periods(config, **kwargs)


async def test_retry_backoff_survives_producer_restart_and_reports_next_attempt():
    store = InMemoryReportingLedgerStore()
    source = FailingSource()
    config = configuration()
    await store.put_configuration(config)
    first = producer(source, store)
    obligation = (await close_admitted_periods(first, config, now=NOW))[0]

    turn = WorkerTurn()
    await first.acquire_obligation(config, obligation, turn=turn, now=NOW)
    assert source.calls == 1
    assert turn.slices_failed == [obligation.reporting_obligation_id]
    assert NOW + timedelta(seconds=48) <= turn.earliest_retry_at <= NOW + timedelta(seconds=72)

    restarted = producer(source, store)
    skipped = WorkerTurn()
    await restarted.acquire_obligation(
        config, obligation, turn=skipped, now=turn.earliest_retry_at - timedelta(seconds=1)
    )
    assert source.calls == 1
    assert skipped.earliest_retry_at == turn.earliest_retry_at

    second = WorkerTurn()
    await restarted.acquire_obligation(config, obligation, turn=second, now=turn.earliest_retry_at)
    assert source.calls == 2
    second_delay = second.earliest_retry_at - turn.earliest_retry_at
    assert timedelta(seconds=96) <= second_delay <= timedelta(seconds=144)


@pytest.mark.parametrize("scope", ["account", "source"])
async def test_scoped_backoff_suppresses_other_obligations(scope):
    store = InMemoryReportingLedgerStore()
    source = FailingSource(scope=scope, retry_after_seconds=900)
    first_config = configuration()
    other_config = configuration(
        account="account-2" if scope == "source" else "account-1", config="config-2"
    )
    first = producer(source, store)
    first_obligation = (await close_admitted_periods(first, first_config, now=NOW))[0]
    other_obligation = (await close_admitted_periods(first, other_config, now=NOW))[0]

    failed = WorkerTurn()
    await first.acquire_obligation(first_config, first_obligation, turn=failed, now=NOW)
    assert failed.earliest_retry_at == NOW + timedelta(minutes=15)

    other = WorkerTurn()
    await producer(source, store).acquire_obligation(
        other_config, other_obligation, turn=other, now=NOW + timedelta(minutes=1)
    )
    assert source.calls == 1
    assert other.earliest_retry_at == failed.earliest_retry_at


async def test_post_deadline_failure_uses_configured_slow_cadence():
    store = InMemoryReportingLedgerStore()
    source = FailingSource()
    config = configuration()
    first = producer(source, store, post_deadline_retry_interval=timedelta(hours=2))
    obligation = (await close_admitted_periods(first, config, now=NOW))[0]
    after_deadline = obligation.automated_recovery_deadline_at + timedelta(minutes=1)

    turn = WorkerTurn()
    await first.acquire_obligation(config, obligation, turn=turn, now=after_deadline)
    assert turn.earliest_retry_at == after_deadline + timedelta(hours=2)
    skipped = WorkerTurn()
    await first.acquire_obligation(
        config, obligation, turn=skipped, now=after_deadline + timedelta(hours=1)
    )
    assert source.calls == 1
    assert skipped.escalated == [obligation.reporting_obligation_id]
    assert skipped.earliest_retry_at == turn.earliest_retry_at


async def test_unanswered_slice_is_deferred_on_worker_turn():
    store = InMemoryReportingLedgerStore()
    calls = []

    def unanswered(request):
        calls.append(request)
        return None

    source = InlineReportingSource(
        capabilities=redacted_capabilities(),
        fetch=unanswered,
        staging=InMemoryStagingStore(),
        seals=InMemorySealStore(),
    )
    config = configuration()
    await store.put_configuration(config)
    worker = producer(source, store)

    first = await worker.run_configuration(config, now=NOW)
    assert len(calls) == 1
    assert len(first.slices_failed) == 1
    assert first.earliest_retry_at is not None

    second = await worker.run_configuration(config, now=NOW + timedelta(seconds=30))
    assert len(calls) == 1
    assert second.earliest_retry_at == first.earliest_retry_at


async def test_retry_starts_when_a_slow_source_finishes():
    clock = [NOW]

    class SlowSource(FailingSource):
        async def execute(self, request, *, cancel):
            clock[0] += timedelta(minutes=5)
            return await super().execute(request, cancel=cancel)

    store = InMemoryReportingLedgerStore()
    source = SlowSource()
    config = configuration()
    worker = producer(source, store, clock=lambda: clock[0])
    obligation = (await close_admitted_periods(worker, config, now=NOW))[0]

    turn = WorkerTurn()
    await worker.acquire_obligation(config, obligation, turn=turn, now=NOW)
    assert NOW + timedelta(minutes=5, seconds=48) <= turn.earliest_retry_at
    await worker.acquire_obligation(config, obligation, now=NOW + timedelta(minutes=5))
    assert source.calls == 1


async def test_later_shared_failure_updates_turn_next_attempt():
    store = InMemoryReportingLedgerStore()
    source = FailingSource()
    worker = producer(source, store)
    first_config = configuration(config="first")
    second_config = configuration(config="second")
    first_obligation = (await close_admitted_periods(worker, first_config, now=NOW))[0]
    second_obligation = (await close_admitted_periods(worker, second_config, now=NOW))[0]

    turn = WorkerTurn()
    await worker.acquire_obligation(first_config, first_obligation, turn=turn, now=NOW)
    source.scope = "account"
    source.retry_after_seconds = 900
    await worker.acquire_obligation(second_config, second_obligation, turn=turn, now=NOW)
    assert turn.earliest_retry_at == NOW + timedelta(minutes=15)


async def test_terminal_scope_is_parked_until_manual_replay():
    store = InMemoryReportingLedgerStore()
    source = FailingSource(code="AUTHENTICATION_FAILED", retry="terminal", scope="source")
    worker = producer(source, store)
    first_config = configuration(account="account-1", config="first")
    second_config = configuration(account="account-2", config="second")
    first_obligation = (await close_admitted_periods(worker, first_config, now=NOW))[0]
    second_obligation = (await close_admitted_periods(worker, second_config, now=NOW))[0]

    turn = WorkerTurn()
    await worker.acquire_obligation(first_config, first_obligation, turn=turn, now=NOW)
    assert turn.earliest_retry_at is None
    await worker.acquire_obligation(second_config, second_obligation, now=NOW + timedelta(days=1))
    assert source.calls == 1

    await worker.acquire_obligation(
        second_config, second_obligation, now=NOW + timedelta(days=1), manual_replay=True
    )
    assert source.calls == 2
    assert source.requests[-1].trigger == "manual_replay"
    await worker.acquire_obligation(second_config, second_obligation, now=NOW + timedelta(days=2))
    assert source.calls == 2


async def test_unbounded_provider_retry_floor_does_not_crash_worker():
    store = InMemoryReportingLedgerStore()
    source = FailingSource(retry_after_seconds=float("inf"))
    config = configuration()
    worker = producer(source, store)
    obligation = (await close_admitted_periods(worker, config, now=NOW))[0]
    turn = WorkerTurn()
    await worker.acquire_obligation(config, obligation, turn=turn, now=NOW)
    assert turn.slices_failed == [obligation.reporting_obligation_id]
    assert turn.earliest_retry_at == datetime.max.replace(tzinfo=timezone.utc)


async def test_manual_replay_can_resume_retryable_work_after_terminal_error():
    store = InMemoryReportingLedgerStore()
    source = FailingSource(code="AUTHENTICATION_FAILED", retry="terminal", scope="source")
    worker = producer(source, store)
    config = configuration()
    obligation = (await close_admitted_periods(worker, config, now=NOW))[0]
    await worker.acquire_obligation(config, obligation, now=NOW)

    source.code = "RATE_LIMITED"
    source.retry = "retryable"
    source.scope = "slice"
    source.retry_after_seconds = 900
    replay_at = NOW + timedelta(days=1)
    replay = WorkerTurn()
    await worker.acquire_obligation(
        config, obligation, turn=replay, now=replay_at, manual_replay=True
    )
    assert replay.earliest_retry_at == replay_at + timedelta(hours=1)
    await worker.acquire_obligation(config, obligation, now=replay_at + timedelta(minutes=1))
    assert source.calls == 2


async def test_stale_terminal_and_clear_writes_do_not_reverse_recovery():
    store = InMemoryReportingLedgerStore()
    key = "source:race"
    failed = RetryScheduleEntry(key, NOW, 1, True, recorded_at=NOW)
    cleared = RetryScheduleEntry(
        key,
        NOW + timedelta(minutes=2),
        0,
        recorded_at=NOW + timedelta(minutes=2),
    )
    assert await store.record_retry_schedule(failed) == failed
    assert await store.record_retry_schedule(cleared) == cleared
    assert await store.record_retry_schedule(failed) == cleared

    later_failure = RetryScheduleEntry(
        key,
        NOW + timedelta(minutes=3),
        1,
        True,
        recorded_at=NOW + timedelta(minutes=3),
    )
    assert await store.record_retry_schedule(later_failure) == later_failure
    assert await store.record_retry_schedule(cleared) == later_failure


async def test_turn_keeps_retry_that_became_due_during_later_fetch():
    clock = [NOW]

    class LaterSlowSource(FailingSource):
        async def execute(self, request, *, cancel):
            if self.calls:
                clock[0] += timedelta(minutes=5)
            return await super().execute(request, cancel=cancel)

    store = InMemoryReportingLedgerStore()
    source = LaterSlowSource()
    worker = producer(source, store, clock=lambda: clock[0])
    first_config = configuration(config="first")
    second_config = configuration(config="second")
    first_obligation = (await close_admitted_periods(worker, first_config, now=NOW))[0]
    second_obligation = (await close_admitted_periods(worker, second_config, now=NOW))[0]
    turn = WorkerTurn()
    await worker.acquire_obligation(first_config, first_obligation, turn=turn, now=NOW)
    first_due = turn.earliest_retry_at
    await worker.acquire_obligation(second_config, second_obligation, turn=turn, now=NOW)
    assert first_due < clock[0]
    assert turn.earliest_retry_at == first_due


async def test_provisional_manual_replay_marks_restored_request():
    store = InMemoryReportingLedgerStore()
    source = FailingSource(code="AUTHENTICATION_FAILED", retry="terminal")
    config = replace(configuration(), required_finality="snapshot")
    worker = ReportingProducer(
        source=source,
        store=store,
        offerings=ProducerOfferings(snapshot_offering_id=SNAPSHOT_OFFERING_ID),
        clock=lambda: NOW,
    )
    obligation = (await close_admitted_periods(worker, config, now=NOW))[0]
    await worker.acquire_obligation(config, obligation, now=NOW, track_settling=True)
    await worker.acquire_obligation(
        config,
        obligation,
        now=NOW + timedelta(hours=1),
        track_settling=True,
        manual_replay=True,
    )
    assert source.calls == 2
    assert source.requests[-1].trigger == "manual_replay"
