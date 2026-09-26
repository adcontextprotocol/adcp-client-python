"""Automatic snapshot restatement and official close from source settling policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from adcp.reporting.fixtures import (
    OFFICIAL_OFFERING_ID,
    SNAPSHOT_OFFERING_ID,
    redacted_capabilities,
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
    ReportingProducer,
    ReportingScheduleSpec,
    ReportingStatusCaller,
    ReportingStatusHandler,
)
from adcp.reporting.source import (
    ProvisionalSnapshotOfferingV1,
    ReportingSourceCapabilitiesV1,
    reporting_source_capabilities_sha256_v1,
)

ACCOUNT = "account-redacted"
CONFIG_ID = "settling-config"
DEFINITION_ID = "PAID_MEDIA_DAILY_V1"
SCHEDULE = ReportingScheduleSpec(period_duration="PT1H", delivery_sla="PT10M", alignment="utc")
ACTIVATED_AT = datetime(2026, 11, 1, 0, 20, tzinfo=timezone.utc)


def _capabilities(
    *,
    restatement_window: str | None,
    restatement_cadence: str | None = None,
    official_close_lag: str | None = None,
) -> ReportingSourceCapabilitiesV1:
    base = redacted_capabilities()
    snapshot = base.offering(SNAPSHOT_OFFERING_ID)
    assert isinstance(snapshot, ProvisionalSnapshotOfferingV1)
    updated = ProvisionalSnapshotOfferingV1.model_validate(
        {
            **snapshot.model_dump(mode="python"),
            "restatement_window": restatement_window,
            "restatement_cadence": restatement_cadence,
            "official_close_lag": official_close_lag,
        }
    )
    payload = base.model_dump(mode="json")
    payload["offerings"] = [
        (
            updated.model_dump(mode="json", exclude_none=True)
            if item.offering_id == SNAPSHOT_OFFERING_ID
            else item.model_dump(mode="json", exclude_none=True)
        )
        for item in base.offerings
    ]
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
    return ReportingSourceCapabilitiesV1.model_validate(payload)


class _MutableFetch:
    def __init__(self) -> None:
        self.impressions = 10
        self.provisional_until: datetime | None = None
        self.calls: list[str] = []

    def __call__(self, request: Any) -> InlineFetchResult:
        self.calls.append(request.publication_class)
        return InlineFetchResult(
            rows=[
                {
                    "media_buy_id": "media-buy-redacted",
                    "impressions": self.impressions,
                    "spend": "1.25",
                }
            ],
            data_through=request.period.end,
            provisional_until=(
                self.provisional_until
                if request.publication_class == "PROVISIONAL_SNAPSHOT"
                else None
            ),
        )


async def _harness(
    capabilities: ReportingSourceCapabilitiesV1,
    *,
    store_factory: Any = None,
) -> tuple[
    ReportingProducer,
    InMemoryReportingLedgerStore,
    _MutableFetch,
    list[datetime],
]:
    clock = [datetime(2026, 11, 1, 2, 10, tzinfo=timezone.utc)]
    fetch = _MutableFetch()
    staging = InMemoryStagingStore()
    source = InlineReportingSource(
        capabilities=capabilities,
        fetch=fetch,
        staging=staging,
        seals=InMemorySealStore(),
        clock=lambda: clock[0],
    )
    store = (
        InMemoryReportingLedgerStore(clock=lambda: clock[0])
        if store_factory is None
        else store_factory(lambda: clock[0])
    )
    await store.create_schema()
    configuration = ReportingConfiguration(
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        account_id=ACCOUNT,
        report_definition_id=DEFINITION_ID,
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=SCHEDULE,
        required_finality="snapshot",
        activated_at=ACTIVATED_AT,
        media_buy_ids=("media-buy-redacted",),
    )
    await store.put_configuration(configuration)
    producer = ReportingProducer(
        source=source,
        offerings=ProducerOfferings(
            snapshot_offering_id=SNAPSHOT_OFFERING_ID,
            official_offering_id=OFFICIAL_OFFERING_ID,
            publication_namespace="reporting-source:fixture",
            requested_metrics=("impressions", "spend"),
            source_scope=dict(capabilities.source_scope),
        ),
        store=store,
        object_reader=staging,
        max_periods_per_turn=1,
        clock=lambda: clock[0],
    )
    return producer, store, fetch, clock


async def _only_obligation(store: InMemoryReportingLedgerStore):
    configurations = await store.list_configurations(account_id=ACCOUNT)
    configuration = configurations[0]
    from adcp.reporting.ledger.models import first_ordinal_after

    ordinal = first_ordinal_after(
        configuration.schedule,
        account_timezone=configuration.account_timezone,
        activated_at=configuration.activated_at,
    )
    from adcp.reporting.ledger import derive_period

    period = derive_period(
        configuration.schedule,
        account_timezone=configuration.account_timezone,
        ordinal=ordinal,
    )
    obligation = await store.find_obligation(
        account_id=ACCOUNT,
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        period_start=period.start,
        period_end=period.end,
    )
    assert obligation is not None
    return obligation


async def _revisions(store: InMemoryReportingLedgerStore):
    obligation = await _only_obligation(store)
    return await store.list_revisions(
        account_id=ACCOUNT,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )


async def test_unchanged_refresh_honors_cadence_and_commits_an_observation() -> None:
    producer, store, fetch, clock = await _harness(
        _capabilities(
            restatement_window="P3D",
            restatement_cadence="PT1H",
            official_close_lag="P3D",
        )
    )
    first = await producer.run_worker()
    assert len(first.revisions_committed) == 1

    clock[0] += timedelta(minutes=59)
    assert (await producer.run_worker()).revisions_committed == []
    assert fetch.calls == ["PROVISIONAL_SNAPSHOT"]

    clock[0] += timedelta(minutes=1)
    unchanged = await producer.run_worker()
    assert len(unchanged.revisions_committed) == 1
    assert fetch.calls == ["PROVISIONAL_SNAPSHOT", "PROVISIONAL_SNAPSHOT"]
    assert len(await _revisions(store)) == 2

    # The successful observation advances both the cadence clock and execution
    # ordinal, so an immediate worker turn neither polls nor replays it.
    await producer.run_worker()
    assert len(fetch.calls) == 2
    obligation = await _only_obligation(store)
    checkpoint = await store.get_restatement_checkpoint(
        account_id=ACCOUNT,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )
    assert checkpoint is not None and checkpoint.next_observation == 2


async def test_changed_refresh_supersedes_snapshot_and_window_expiry_closes_officially() -> None:
    producer, store, fetch, clock = await _harness(
        _capabilities(
            restatement_window="P3D",
            restatement_cadence="PT1H",
            official_close_lag="P3D",
        )
    )
    await producer.run_worker()
    first = (await _revisions(store))[0]

    fetch.impressions = 25
    clock[0] += timedelta(hours=1)
    changed = await producer.run_worker()
    assert len(changed.revisions_committed) == 1
    snapshots = await _revisions(store)
    assert len(snapshots) == 2
    assert snapshots[1].supersedes_reporting_revision_id == first.reporting_revision_id

    obligation = await _only_obligation(store)
    clock[0] = obligation.period.end + timedelta(days=3)
    closed = await producer.run_worker()
    assert len(closed.revisions_committed) == 1
    revisions = await _revisions(store)
    assert [revision.finality for revision in revisions] == [
        "snapshot",
        "snapshot",
        "official",
    ]
    assert revisions[-1].finality_basis == "stabilized"
    status = await ReportingStatusHandler(store).handle(
        {"view": "periods"},
        caller=ReportingStatusCaller(account_id=ACCOUNT, consumer_id="buyer-settling"),
    )
    assert status["periods"][0]["revision_count"] == 3
    assert {revision["finality"] for revision in status["revisions"]} == {
        "snapshot",
        "official",
    }

    clock[0] += timedelta(days=1)
    await producer.run_worker()
    assert fetch.calls[-1] == "AUTHORITATIVE"
    assert len(fetch.calls) == 3


async def test_slice_provisional_until_overrides_declared_window() -> None:
    producer, store, fetch, clock = await _harness(
        _capabilities(
            restatement_window="P3D",
            restatement_cadence="P1D",
            official_close_lag="P3D",
        )
    )
    await producer.run_worker()
    obligation = await _only_obligation(store)

    # Return direct source evidence that this particular period is moving for
    # one day longer than the offering default.
    fetch.provisional_until = obligation.period.end + timedelta(days=4)
    clock[0] += timedelta(days=1)
    await producer.run_worker()

    clock[0] = obligation.period.end + timedelta(days=3)
    await producer.run_worker()
    assert fetch.calls[-1] == "PROVISIONAL_SNAPSHOT"
    assert all(revision.finality == "snapshot" for revision in await _revisions(store))

    clock[0] = obligation.period.end + timedelta(days=4)
    await producer.run_worker()
    assert fetch.calls[-1] == "AUTHORITATIVE"
    assert (await _revisions(store))[-1].finality == "official"


async def test_source_without_a_window_retains_its_final_default_read() -> None:
    producer, store, fetch, clock = await _harness(_capabilities(restatement_window=None))
    await producer.run_worker()
    obligation = await _only_obligation(store)
    clock[0] = obligation.period.end + timedelta(days=10)
    await producer.run_worker()
    assert fetch.calls == ["PROVISIONAL_SNAPSHOT", "PROVISIONAL_SNAPSHOT"]
    assert len(await _revisions(store)) == 2
    checkpoint = await store.get_restatement_checkpoint(
        account_id=ACCOUNT,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )
    assert checkpoint is not None and checkpoint.next_observation == 2
    await producer.run_worker()
    assert len(fetch.calls) == 2


def test_settling_options_require_a_declared_window_and_safe_cadence() -> None:
    snapshot = redacted_capabilities().offering(SNAPSHOT_OFFERING_ID)
    assert isinstance(snapshot, ProvisionalSnapshotOfferingV1)
    payload = snapshot.model_dump(mode="python")
    with pytest.raises(ValidationError, match="require restatement_window"):
        ProvisionalSnapshotOfferingV1.model_validate({**payload, "restatement_cadence": "P1D"})
    with pytest.raises(ValidationError, match="fastest_safe_cadence"):
        ProvisionalSnapshotOfferingV1.model_validate(
            {
                **payload,
                "restatement_window": "P3D",
                "restatement_cadence": "PT1M",
            }
        )
