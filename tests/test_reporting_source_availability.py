"""Source availability gates scheduled acquisition without dropping owed work."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adcp.reporting.fixtures import (
    OFFICIAL_OFFERING_ID,
    SNAPSHOT_OFFERING_ID,
    redacted_capabilities,
    redacted_completed_result,
    redacted_contract_identity,
)
from adcp.reporting.inline_source import (
    InlineFetchResult,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    ProducerOfferings,
    ReportingConfiguration,
    ReportingContractualCutoffPolicy,
    ReportingDefinitionBinding,
    ReportingFinalityPolicy,
    ReportingObligationRecord,
    ReportingProducer,
    ReportingScheduleSpec,
    ReportingSourceFinalPolicy,
    WorkerTurn,
)
from adcp.reporting.source import (
    MediaBuyConstituentV1,
    ReportingFinalityEvidenceV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceExecutorResult,
    ReportingSourceSliceRequestV1,
    ReportingSourceStagedObjectReader,
    SourceBatchManifestV1,
    encode_source_batch_manifest_v1,
    reporting_source_capabilities_sha256_v1,
    source_batch_manifest_reference_v1,
)
from tests.test_reporting_producer_retry import FailingSource

END = datetime(2026, 11, 2, tzinfo=timezone.utc)
PROVIDER_ROW = '{"campaign_id":"campaign-redacted-1","impressions":10}\n'


def _capabilities(*, official: bool = False, **changes: Any) -> ReportingSourceCapabilitiesV1:
    payload = redacted_capabilities().model_dump(mode="json")
    identifier = OFFICIAL_OFFERING_ID if official else SNAPSHOT_OFFERING_ID
    for offering in payload["offerings"]:
        offering.update(
            source_timezone="UTC", product_ids=["fixture-product", "PAID_MEDIA_DAILY_V1"]
        )
        if offering["offering_id"] == identifier:
            offering.update(expected_availability_lag="PT1H", worst_case_availability_lag="PT6H")
            offering.update(changes)
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
    return ReportingSourceCapabilitiesV1.model_validate(payload)


class ProgressStore(InMemoryReportingLedgerStore):
    """A custom store implementing the optional durable progress protocol."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.closed: dict[Any, datetime] = {}
        self.pending: dict[str, Any] = {}
        self.finished: list[str] = []

    async def producer_closed_through(
        self, configuration: ReportingConfiguration
    ) -> datetime | None:
        return self.closed.get(configuration.generation_key)

    async def producer_constituents(
        self, configuration: ReportingConfiguration, obligation: ReportingObligationRecord
    ) -> tuple[MediaBuyConstituentV1, ...]:
        return tuple(
            MediaBuyConstituentV1(
                constituent_id=identifier, product_id="fixture-product", media_buy_id=identifier
            )
            for identifier in obligation.media_buy_ids
        )

    async def commit_producer_period(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        previous_end: datetime | None,
    ) -> ReportingObligationRecord:
        stored = await self.commit_obligation(obligation)
        self.closed[configuration.generation_key] = obligation.period.end
        self.pending[obligation.reporting_obligation_id] = configuration.generation_key
        return stored

    async def next_producer_obligations(
        self, configuration: ReportingConfiguration, *, now: datetime, limit: int
    ) -> tuple[str, ...]:
        return tuple(
            key
            for key, generation in self.pending.items()
            if generation == configuration.generation_key
        )[:limit]

    async def finish_producer_acquisition(
        self, configuration: ReportingConfiguration, *, reporting_obligation_id: str
    ) -> None:
        self.pending.pop(reporting_obligation_id, None)
        self.finished.append(reporting_obligation_id)


class EvidenceSource:
    """A provider with an explicit authoritative signal and generation-pinned staged rows."""

    def __init__(
        self,
        capabilities: ReportingSourceCapabilitiesV1,
        clock: list[datetime],
        calls: list[datetime],
    ) -> None:
        self.capabilities = capabilities
        self.clock = clock
        self.calls = calls
        self.requests: list[ReportingSourceSliceRequestV1] = []
        self.signal: str | None = "delivery.is_final"
        self.basis = "provider_declared"
        self.read_delay = timedelta(0)
        self.manifest: SourceBatchManifestV1
        self.reader: ReportingSourceStagedObjectReader

    async def execute(
        self, request: ReportingSourceSliceRequestV1, **kwargs: Any
    ) -> ReportingSourceExecutorResult:
        self.calls.append(self.clock[0])
        self.requests.append(request)
        result, self.manifest, self.reader = redacted_completed_result(
            request, observed_at=self.clock[0], page_bodies=[PROVIDER_ROW]
        )
        if request.publication_class == "AUTHORITATIVE":
            evidence = ReportingFinalityEvidenceV1.model_validate(
                {"basis": self.basis, "evidence_ref": self.signal, "observed_at": self.clock[0]}
            )
            self.manifest = self.manifest.model_copy(update={"finality_evidence": evidence})
            raw = encode_source_batch_manifest_v1(self.manifest)
            return ReportingSourceExecutorResult.completed(
                request=request,
                manifest=source_batch_manifest_reference_v1("manifest-redacted-v1", raw),
                manifest_bytes=raw,
            )
        return result

    async def read(self, **kwargs: Any) -> bytes:
        rows = await self.reader.read(**kwargs)
        self.clock[0] += self.read_delay
        return rows


async def _harness(
    store_class: Any = InMemoryReportingLedgerStore,
    *,
    official: bool = False,
    delivery_sla: str = "PT6H",
    capabilities: ReportingSourceCapabilitiesV1 | None = None,
    finality_policy: ReportingFinalityPolicy | None = None,
    read_jitter_window: timedelta = timedelta(0),
    **changes: Any,
) -> tuple[Any, ...]:
    clock = [END + timedelta(seconds=30)]
    calls: list[datetime] = []

    def fetch(request: Any) -> InlineFetchResult:
        calls.append(clock[0])
        return InlineFetchResult(
            rows=[{"media_buy_id": "media-buy-redacted", "impressions": 10, "spend": "1.25"}],
            data_through=request.period.end,
        )

    capabilities = capabilities or _capabilities(official=official, **changes)
    staging = InMemoryStagingStore()
    source = InlineReportingSource(
        capabilities=capabilities,
        fetch=fetch,
        staging=staging,
        seals=InMemorySealStore(),
        clock=lambda: clock[0],
    )
    store = store_class(clock=lambda: clock[0])
    await store.create_schema()
    configuration = ReportingConfiguration(
        delivery_config_id="availability",
        delivery_config_version=1,
        account_id="account-redacted",
        consumer_id="buyer",
        report_definition_id="PAID_MEDIA_DAILY_V1",
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=ReportingScheduleSpec(
            period_duration="P1D", delivery_sla=delivery_sla, alignment="utc"
        ),
        required_finality="official" if official else "snapshot",
        definition=(
            ReportingDefinitionBinding(
                **redacted_contract_identity.model_dump(
                    exclude={"report_definition_id", "reporting_profile"}
                ),
                finality_policy=finality_policy,
            )
            if finality_policy is not None
            else None
        ),
        automated_recovery_window=timedelta(0),
        activated_at=END - timedelta(days=1, minutes=10),
        media_buy_ids=("media-buy-redacted",),
    )
    await store.put_configuration(configuration)
    offerings = ProducerOfferings(
        snapshot_offering_id=SNAPSHOT_OFFERING_ID,
        official_offering_id=OFFICIAL_OFFERING_ID,
        source_scope=dict(capabilities.source_scope),
        publication_namespace="reporting-source:fixture",
    )
    provider = EvidenceSource(capabilities, clock, calls) if finality_policy is not None else None
    producer = ReportingProducer(
        source=provider or source,
        offerings=offerings,
        store=store,
        object_reader=provider or staging,
        clock=lambda: clock[0],
        read_jitter_window=read_jitter_window,
    )
    obligation = (await producer.close_elapsed_periods(configuration, now=clock[0]))[0]
    return producer, store, configuration, obligation, calls, clock


def _closing_capabilities() -> ReportingSourceCapabilitiesV1:
    payload = _capabilities().model_dump(mode="json")
    for offering in payload["offerings"]:
        if offering["offering_id"] == SNAPSHOT_OFFERING_ID:
            offering.update(
                restatement_window="PT2H", restatement_cadence="PT1H", official_close_lag="PT2H"
            )
        elif offering["offering_id"] == OFFICIAL_OFFERING_ID:
            offering.update(
                expected_availability_lag="PT4H",
                worst_case_availability_lag="PT8H",
                source_timezone="UTC",
                days_after_period_end=0,
                source_local_ready_time="06:00",
            )
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
    return ReportingSourceCapabilitiesV1.model_validate(payload)


@pytest.mark.parametrize("store_class", [InMemoryReportingLedgerStore, ProgressStore])
@pytest.mark.parametrize("pending_lookup", [True, False])
async def test_snapshot_official_close_waits_for_the_authoritative_offering(
    store_class: Any, pending_lookup: bool
) -> None:
    producer, store, configuration, obligation, calls, clock = await _harness(
        store_class,
        capabilities=_closing_capabilities(),
        finality_policy=ReportingSourceFinalPolicy("delivery-v1", "delivery.is_final"),
    )
    if not pending_lookup:
        # Custom stores can keep the original observation contract.
        setattr(store, "get_provisional_acquisition", None)
    clock[0] = END + timedelta(hours=1)
    first = await producer.run_configuration(configuration, now=clock[0])
    assert len(first.revisions_committed) == 1
    assert calls == [clock[0]]

    for instant in (END + timedelta(hours=2), END + timedelta(hours=6, seconds=-1)):
        clock[0] = instant
        waiting = await producer.run_configuration(configuration, now=instant)
        assert not waiting.revisions_committed
        assert not waiting.slices_failed
        assert calls == [END + timedelta(hours=1)]
        if isinstance(store, ProgressStore):
            assert obligation.reporting_obligation_id in store.pending

    clock[0] = END + timedelta(hours=6)
    closed = await producer.run_configuration(configuration, now=clock[0])
    assert len(closed.revisions_committed) == 1
    assert calls == [END + timedelta(hours=1), clock[0]]
    revisions = await store.list_revisions(
        account_id=configuration.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )
    assert {revision.finality for revision in revisions} == {"snapshot", "official"}
    if isinstance(store, ProgressStore):
        assert obligation.reporting_obligation_id not in store.pending


@pytest.mark.parametrize("retryable", [True, False])
async def test_official_close_uses_its_own_worst_case_window(retryable: bool) -> None:
    capabilities = _closing_capabilities()
    producer, _, configuration, obligation, _, clock = await _harness(
        delivery_sla="PT10M",
        capabilities=capabilities,
        finality_policy=ReportingSourceFinalPolicy("delivery-v1", "delivery.is_final"),
    )
    clock[0] = END + timedelta(hours=1)
    await producer.run_configuration(configuration, now=clock[0])
    source = FailingSource(
        code="PROVIDER_TRANSIENT" if retryable else "AUTHENTICATION_FAILED",
        retry="retryable" if retryable else "terminal",
    )
    source.capabilities = capabilities
    producer._source = source

    clock[0] = END + timedelta(hours=6)
    failed = await producer.run_configuration(configuration, now=clock[0])
    assert source.calls == 1
    assert source.requests[0].offering_id == OFFICIAL_OFFERING_ID
    assert failed.escalated == ([] if retryable else [obligation.reporting_obligation_id])

    clock[0] += timedelta(seconds=1)
    deferred = await producer.run_configuration(configuration, now=clock[0])
    assert source.calls == 1
    assert deferred.escalated == ([] if retryable else [obligation.reporting_obligation_id])

    clock[0] = END + timedelta(hours=8)
    expired = await producer.run_configuration(configuration, now=clock[0])
    assert expired.escalated == [obligation.reporting_obligation_id]


@pytest.mark.parametrize("store_class", [InMemoryReportingLedgerStore, ProgressStore])
async def test_first_read_waits_for_the_declared_lag_and_survives_restart(store_class: Any) -> None:
    producer, store, configuration, obligation, calls, clock = await _harness(store_class)
    expected_at = obligation.period.expected_at
    for instant in (END + timedelta(seconds=30), END + timedelta(hours=1) - timedelta(seconds=1)):
        clock[0] = instant
        turn = await producer.run_configuration(configuration, now=instant)
        assert calls == []
        assert not turn.revisions_committed
        assert not turn.slices_failed
        if isinstance(store, ProgressStore):
            assert obligation.reporting_obligation_id in store.pending
            assert store.finished == []

    restarted = ReportingProducer(
        source=producer._source,
        offerings=producer._offerings,
        store=store,
        object_reader=producer._object_reader,
        clock=lambda: clock[0],
        read_jitter_window=timedelta(0),
    )
    clock[0] = END + timedelta(hours=1)
    turn = await restarted.run_configuration(configuration, now=clock[0])
    assert calls == [clock[0]]
    assert len(turn.revisions_committed) == 1
    assert (
        await store.get_obligation(
            account_id=configuration.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
    ).period.expected_at == expected_at


async def test_zero_lag_keeps_immediate_acquisition() -> None:
    producer, _, configuration, _, calls, clock = await _harness(expected_availability_lag="PT0S")
    turn = await producer.run_configuration(configuration, now=clock[0])
    assert calls == [clock[0]]
    assert len(turn.revisions_committed) == 1


@pytest.mark.parametrize("store_class", [InMemoryReportingLedgerStore, ProgressStore])
async def test_contractual_cutoff_waits_for_snapshot_availability(store_class: Any) -> None:
    producer, store, config, obligation, calls, clock = await _harness(
        store_class,
        official=True,
        capabilities=_closing_capabilities(),
        finality_policy=ReportingContractualCutoffPolicy("half-hour", "PT30M"),
    )
    clock[0] = END + timedelta(minutes=59)
    assert not (await producer.run_configuration(config)).revisions_committed
    assert not calls
    if isinstance(store, ProgressStore):
        assert obligation.reporting_obligation_id in store.pending
    clock[0] = END + timedelta(hours=1)
    assert len((await producer.run_configuration(config)).revisions_committed) == 1
    assert calls == [clock[0]]
    assert producer._source.requests[0].offering_id == SNAPSHOT_OFFERING_ID


@pytest.mark.parametrize(
    ("period_end", "days_after", "ready_time", "expected"),
    [
        (END, 1, "06:00", datetime(2026, 11, 2, 11, tzinfo=timezone.utc)),
        (
            datetime(2026, 11, 1, 4, tzinfo=timezone.utc),
            0,
            "01:30",
            datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 3, 8, 5, tzinfo=timezone.utc),
            0,
            "02:30",
            datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc),
        ),
    ],
)
async def test_authoritative_local_readiness_is_conservative_across_clock_changes(
    period_end: datetime, days_after: int, ready_time: str, expected: datetime
) -> None:
    producer, _, _, obligation, _, _ = await _harness(
        official=True,
        expected_availability_lag="PT0S",
        finality_policy=ReportingSourceFinalPolicy("delivery-v1", "delivery.is_final"),
        source_timezone="America/New_York",
        days_after_period_end=days_after,
        source_local_ready_time=ready_time,
    )
    obligation = replace(
        obligation,
        period=replace(
            obligation.period,
            start=period_end - timedelta(days=1),
            end=period_end,
            expected_at=period_end + timedelta(hours=6),
        ),
        scope_resolved_at=period_end,
        automated_recovery_deadline_at=period_end + timedelta(hours=6),
    )
    assert producer._source_available_at(obligation) == expected


async def test_authoritative_readiness_uses_period_timezone_when_offering_omits_it() -> None:
    producer, _, _, obligation, _, _ = await _harness(
        official=True,
        source_timezone=None,
        finality_policy=ReportingSourceFinalPolicy("delivery-v1", "delivery.is_final"),
        days_after_period_end=1,
        source_local_ready_time="06:00",
    )
    assert producer._source_available_at(obligation) == END + timedelta(days=1, hours=6)


@pytest.mark.parametrize("retryable", [True, False])
async def test_source_window_delays_retryable_escalation_without_hiding_terminal_errors(
    retryable: bool,
) -> None:
    producer, store, configuration, obligation, _, clock = await _harness(delivery_sla="PT10M")
    source = FailingSource(
        code="PROVIDER_TRANSIENT" if retryable else "AUTHENTICATION_FAILED",
        retry="retryable" if retryable else "terminal",
    )
    source.capabilities = _capabilities()
    producer._source = source
    # A retained legacy obligation can have a deadline predating the current
    # source window. Preserve that deadline, but do not escalate readiness early.
    assert obligation.automated_recovery_deadline_at == END + timedelta(minutes=10)
    clock[0] = END + timedelta(hours=1)
    turn = WorkerTurn()
    await producer.acquire_obligation(configuration, obligation, turn=turn, now=clock[0])
    assert source.calls == 1
    assert turn.escalated == ([] if retryable else [obligation.reporting_obligation_id])
    deferred = WorkerTurn()
    await producer.acquire_obligation(
        configuration, obligation, turn=deferred, now=clock[0] + timedelta(seconds=1)
    )
    assert source.calls == 1
    assert deferred.escalated == ([] if retryable else [obligation.reporting_obligation_id])
    clock[0] = END + timedelta(hours=6)
    expired = WorkerTurn()
    await producer.acquire_obligation(configuration, obligation, turn=expired, now=clock[0])
    assert expired.escalated == [obligation.reporting_obligation_id]
