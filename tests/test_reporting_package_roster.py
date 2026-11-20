"""Frozen flat package coverage for media-buy reporting obligations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from adcp.reporting.inline_source import InlineFetchResult
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ProducerOfferings,
    ReportingProducer,
    ReportingStatusCaller,
    ReportingStatusHandler,
)
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    reporting_source_capabilities_sha256_v1,
)
from adcp.reporting.subscriptions import SubscriptionObligationPlanner
from tests.test_reporting_ledger import CALLER, _configuration, _obligation, _revision
from tests.test_reporting_settling import ACCOUNT, _capabilities, _harness
from tests.test_reporting_subscription_engine import OFFERINGS, at, buy
from tests.test_reporting_subscription_ledger import AGENCY, _world_with


class _NoSource:
    @property
    def capabilities(self):
        raise AssertionError("source fetch is not needed to close an obligation")

    async def execute(self, request, *, cancel, heartbeat=None):
        raise AssertionError("source fetch is not needed to close an obligation")


async def test_producer_freezes_flat_package_roster_before_source_fetch() -> None:
    from adcp.reporting.ledger.package_roster import CompletePackageRoster

    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    seen: list[tuple[tuple[str, ...], datetime]] = []

    async def roster(config, period_end):
        seen.append((config.media_buy_ids, period_end))
        return CompletePackageRoster(("pkg-zero", "pkg-measured", "pkg-measured"))

    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(
            official_offering_id="HOURLY_OFFICIAL_V1", requested_dimensions=("package",)
        ),
        store=store,
        package_roster_resolver=roster,
    )
    (obligation,) = await producer.close_elapsed_periods(
        configuration, now=datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
    )
    assert seen == [(("mb_1",), obligation.period.end)]
    assert obligation.package_ids == ("pkg-measured", "pkg-zero")
    view = await ReportingStatusHandler(store).handle({"view": "periods"}, caller=CALLER)
    assert view["periods"][0]["coverage"]["package_ids"] == ["pkg-measured", "pkg-zero"]
    assert view["periods"][0]["coverage"]["covered_package_ids"] == ["pkg-measured", "pkg-zero"]


async def test_proven_empty_roster_is_full_and_distinct_from_missing_history() -> None:
    from adcp.reporting.ledger.package_roster import CompletePackageRoster

    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)

    async def roster(config, period_end):
        return CompletePackageRoster(())

    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(requested_dimensions=("package",)),
        store=store,
        package_roster_resolver=roster,
    )
    (obligation,) = await producer.close_elapsed_periods(
        configuration, now=datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
    )
    assert obligation.package_ids == ()
    assert obligation.coverage_status == "full"


async def test_missing_historical_roster_never_fetches_or_claims_full_coverage() -> None:
    from adcp.reporting.ledger.package_roster import MissingHistoricalPackageRoster

    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)

    async def roster(config, period_end):
        return MissingHistoricalPackageRoster()

    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(
            official_offering_id="HOURLY_OFFICIAL_V1", requested_dimensions=("package",)
        ),
        store=store,
        package_roster_resolver=roster,
    )
    (obligation,) = await producer.close_elapsed_periods(
        configuration, now=datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
    )
    assert obligation.coverage_status == "unknown"
    assert obligation.package_ids == ()
    assert await producer.acquire_obligation(configuration, obligation) is None
    assert (
        await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        == ()
    )


@pytest.mark.parametrize("coverage_status", ["partial", "none"])
async def test_incomplete_package_roster_blocks_source_fetch(coverage_status: str) -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(
        _obligation(
            configuration,
            package_reporting=True,
            package_ids=("pkg-a",),
            coverage_status=coverage_status,
        )
    )
    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(
            official_offering_id="HOURLY_OFFICIAL_V1", requested_dimensions=("package",)
        ),
        store=store,
    )
    assert await producer.acquire_obligation(configuration, obligation) is None


async def test_nonpackage_unknown_coverage_still_reaches_source() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(
        _obligation(configuration, coverage_status="unknown", package_reporting=False)
    )
    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(official_offering_id="HOURLY_OFFICIAL_V1"),
        store=store,
    )
    with pytest.raises(AssertionError, match="source fetch is not needed"):
        await producer.acquire_obligation(configuration, obligation)


async def test_zero_buy_scope_requires_independent_history_proof() -> None:
    from adcp.reporting.ledger.package_roster import CompletePackageRoster

    store = InMemoryReportingLedgerStore()
    configuration = replace(_configuration(), media_buy_ids=())
    await store.put_configuration(configuration)
    calls = 0

    async def roster(config, period_end):
        nonlocal calls
        calls += 1
        return CompletePackageRoster(())

    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(requested_dimensions=("package",)),
        store=store,
        package_roster_resolver=roster,
    )
    (obligation,) = await producer.close_elapsed_periods(
        configuration, now=datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
    )
    assert obligation.coverage_status == "unknown"
    assert obligation.package_ids == ()
    assert calls == 0


async def test_missing_historical_obligation_blocks_acquisition_after_mode_change() -> None:
    from adcp.reporting.ledger.package_roster import MissingHistoricalPackageRoster

    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)

    async def roster(config, period_end):
        return MissingHistoricalPackageRoster()

    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(requested_dimensions=("package",)),
        store=store,
        package_roster_resolver=roster,
    )
    (obligation,) = await producer.close_elapsed_periods(
        configuration, now=datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
    )
    producer._offerings = replace(producer._offerings, requested_dimensions=("campaign_id",))
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(configuration, obligation)
    assert conflict.value.code == "PACKAGE_MODE_MISMATCH"


async def test_reclose_keeps_saved_roster_after_live_mapping_changes() -> None:
    from adcp.reporting.ledger.package_roster import CompletePackageRoster

    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    current = ["pkg-measured", "pkg-zero"]
    calls = 0

    async def roster(config, period_end):
        nonlocal calls
        calls += 1
        return CompletePackageRoster(tuple(current))

    def producer():
        return ReportingProducer(
            source=_NoSource(),
            offerings=ProducerOfferings(requested_dimensions=("package",)),
            store=store,
            package_roster_resolver=roster,
        )

    (saved,) = await producer().close_elapsed_periods(
        configuration, now=datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
    )
    current[:] = ["pkg-later"]
    assert (
        await producer().close_elapsed_periods(
            configuration, now=datetime(2026, 9, 1, 2, 31, tzinfo=timezone.utc)
        )
        == []
    )
    retained = await store.get_obligation(
        account_id=saved.account_id, reporting_obligation_id=saved.reporting_obligation_id
    )
    assert retained is not None and retained.package_ids == ("pkg-measured", "pkg-zero")
    assert calls == 1


async def test_subscription_planner_resolves_exact_period_buys() -> None:
    from adcp.reporting.ledger.package_roster import CompletePackageRoster

    world = await _world_with(AGENCY, [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z")])
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-03T00:00:00Z"))
    seen: list[tuple[tuple[str, ...], datetime]] = []

    async def roster(config, period_end):
        seen.append((config.media_buy_ids, period_end))
        return CompletePackageRoster(("pkg-zero", "pkg-measured"))

    planner = SubscriptionObligationPlanner(
        store,
        offerings=OFFERINGS,
        currency="USD",
        package_roster_resolver=roster,
    )
    (owed,) = await world.engine.frozen_owed_sets(AGENCY, now=at("2026-10-03T00:00:00Z"))
    await planner.plan(owed)
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller("acct-1", AGENCY.subject)
    )
    obligations = await store.list_generation_obligations(configuration.generation_key)
    assert obligations and all(o.package_ids == ("pkg-measured", "pkg-zero") for o in obligations)
    assert seen and all(buys == ("mb-1",) for buys, _end in seen)


def _package_capabilities() -> ReportingSourceCapabilitiesV1:
    capabilities = _capabilities(restatement_window="P3D")
    payload = capabilities.model_dump(mode="json")
    payload["offerings"] = [
        item.model_dump(mode="json", exclude_none=True) for item in capabilities.offerings
    ]
    for offering in payload["offerings"]:
        if offering["publication_class"] == "PROVISIONAL_SNAPSHOT":
            offering["dimensions"] = [
                {"name": "package", "support": "exact"},
                {"name": "campaign_id", "support": "exact"},
            ]
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(payload)
    return ReportingSourceCapabilitiesV1.model_validate(payload)


async def _package_producer(package_ids: tuple[str, ...], rows: list[dict]):
    from adcp.reporting.ledger.package_roster import CompletePackageRoster

    producer, store, _fetch, clock = await _harness(_package_capabilities())

    async def roster(config, period_end):
        return CompletePackageRoster(package_ids)

    producer._package_roster_resolver = roster
    producer._offerings = replace(producer._offerings, requested_dimensions=("package",))
    producer._source._fetch = lambda request: InlineFetchResult(
        rows=rows, data_through=request.period.end
    )
    return producer, store, clock


async def test_measured_and_zero_package_rows_keep_flat_revision_coverage() -> None:
    producer, store, clock = await _package_producer(
        ("pkg-zero", "pkg-measured"),
        [
            {
                "media_buy_id": "media-buy-redacted",
                "package_id": "pkg-measured",
                "impressions": 100,
                "spend": "5",
            },
            {
                "media_buy_id": "media-buy-redacted",
                "package_id": "pkg-zero",
                "impressions": 0,
                "spend": "0",
            },
        ],
    )
    turn = await producer.run_worker()
    assert len(turn.revisions_committed) == 1
    view = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    expected = ["pkg-measured", "pkg-zero"]
    assert view["periods"][0]["coverage"]["package_ids"] == expected
    assert view["periods"][0]["coverage"]["covered_package_ids"] == expected
    assert view["revisions"][0]["coverage"]["package_ids"] == expected
    assert view["revisions"][0]["coverage"]["covered_package_ids"] == expected
    assert view["revisions"][0]["row_count"] == 2


async def test_complete_empty_roster_admits_only_an_empty_package_revision() -> None:
    producer, store, _clock = await _package_producer((), [])
    turn = await producer.run_worker()
    assert len(turn.revisions_committed) == 1
    view = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    assert view["periods"][0]["coverage"]["package_ids"] == []
    assert view["revisions"][0]["coverage"]["covered_package_ids"] == []
    assert view["revisions"][0]["row_count"] == 0


async def test_retry_uses_saved_roster_after_live_mapping_changes() -> None:
    producer, store, clock = await _package_producer(
        ("pkg-measured",),
        [
            {
                "media_buy_id": "media-buy-redacted",
                "package_id": "pkg-measured",
                "impressions": 100,
                "spend": "5",
            }
        ],
    )
    current = ["pkg-measured"]
    calls = 0

    async def roster(config, period_end):
        from adcp.reporting.ledger.package_roster import CompletePackageRoster

        nonlocal calls
        calls += 1
        return CompletePackageRoster(tuple(current))

    producer._package_roster_resolver = roster
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=clock[0])
    current[:] = ["pkg-later"]
    revision = await producer.acquire_obligation(configuration, obligation, now=clock[0])
    assert revision is not None and revision.row_count == 1
    assert calls == 1
    view = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    assert view["revisions"][0]["coverage"]["covered_package_ids"] == ["pkg-measured"]


@pytest.mark.parametrize("package_ids", [("pkg-a",), ()])
async def test_restart_cannot_bypass_frozen_package_admission(
    package_ids: tuple[str, ...],
) -> None:
    producer, store, clock = await _package_producer(
        package_ids,
        [
            {
                "media_buy_id": "media-buy-redacted",
                "package_id": "pkg-outside",
                "impressions": 1,
                "spend": "1",
            }
        ],
    )
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=clock[0])
    assert obligation.package_reporting is True
    restarted = ReportingProducer(
        source=producer._source,
        offerings=replace(producer._offerings, requested_dimensions=()),
        store=store,
        object_reader=producer._object_reader,
        clock=lambda: clock[0],
    )
    with pytest.raises(LedgerConflictError) as conflict:
        await restarted.acquire_obligation(configuration, obligation, now=clock[0])
    assert conflict.value.code == "PACKAGE_MODE_MISMATCH"
    assert (
        await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        == ()
    )


async def test_package_row_outside_frozen_roster_is_refused_before_commit() -> None:
    producer, store, clock = await _package_producer(
        ("pkg-measured",),
        [
            {
                "media_buy_id": "media-buy-redacted",
                "package_id": "pkg-outside",
                "impressions": 1,
                "spend": "1",
            }
        ],
    )
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=clock[0])
    with pytest.raises(LedgerConflictError, match="frozen package roster"):
        await producer.acquire_obligation(configuration, obligation, now=clock[0])
    assert (
        await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        == ()
    )


async def test_full_roster_requires_a_row_for_each_package_id() -> None:
    producer, store, clock = await _package_producer(
        ("pkg-measured", "pkg-zero"),
        [
            {
                "media_buy_id": "media-buy-redacted",
                "package_id": "pkg-measured",
                "impressions": 100,
                "spend": "5",
            }
        ],
    )
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=clock[0])
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(configuration, obligation, now=clock[0])
    assert conflict.value.code == "PACKAGE_ROW_MISSING"
    assert (
        await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        == ()
    )


async def test_duplicate_package_row_grain_is_rejected() -> None:
    row = {
        "media_buy_id": "media-buy-redacted",
        "package_id": "pkg-a",
        "impressions": 5,
        "spend": "1",
    }
    producer, store, clock = await _package_producer(("pkg-a",), [row, dict(row)])
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=clock[0])
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(configuration, obligation, now=clock[0])
    assert conflict.value.code == "PACKAGE_ROW_DUPLICATE"
    assert (
        await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        == ()
    )


@pytest.mark.parametrize(
    ("package_ids", "rows", "code"),
    [
        (
            ("pkg-a",),
            [{"media_buy_id": "mb_1", "package_id": "pkg-outside", "impressions": 5}],
            "PACKAGE_ROW_OUTSIDE_ROSTER",
        ),
        (
            ("pkg-a", "pkg-b"),
            [{"media_buy_id": "mb_1", "package_id": "pkg-a", "impressions": 5}],
            "PACKAGE_ROW_MISSING",
        ),
    ],
)
async def test_direct_store_rejects_rows_that_do_not_cover_frozen_packages(
    package_ids: tuple[str, ...], rows: list[dict], code: str
) -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(
        _obligation(configuration, package_ids=package_ids, package_reporting=True)
    )
    revision, committed_rows = _revision(obligation, rows=rows)
    with pytest.raises(LedgerConflictError) as conflict:
        await store.commit_revision(revision, committed_rows)
    assert conflict.value.code == code
    assert (
        await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        == ()
    )


@pytest.mark.parametrize("coverage_status", ["partial", "none", "unknown"])
async def test_direct_store_requires_full_package_roster(coverage_status: str) -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(
        _obligation(
            configuration,
            package_ids=("pkg-a",),
            package_reporting=True,
            coverage_status=coverage_status,
        )
    )
    revision, rows = _revision(
        obligation, rows=[{"media_buy_id": "mb_1", "package_id": "pkg-a", "impressions": 5}]
    )
    with pytest.raises(LedgerConflictError) as conflict:
        await store.commit_revision(revision, rows)
    assert conflict.value.code == "PACKAGE_ROSTER_UNAVAILABLE"


async def test_direct_store_accepts_proven_empty_package_roster() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(
        _obligation(configuration, package_reporting=True, package_ids=())
    )
    revision, rows = _revision(obligation, rows=[], control_totals=())
    assert (await store.commit_revision(revision, rows)).row_count == 0


async def test_distinct_requested_dimension_values_remain_valid() -> None:
    producer, store, _clock = await _package_producer(
        ("pkg-a",),
        [
            {
                "media_buy_id": "media-buy-redacted",
                "package_id": "pkg-a",
                "campaign_id": "campaign-a",
                "impressions": 5,
                "spend": "1",
            },
            {
                "media_buy_id": "media-buy-redacted",
                "package_id": "pkg-a",
                "campaign_id": "campaign-b",
                "impressions": 7,
                "spend": "2",
            },
        ],
    )
    producer._offerings = replace(
        producer._offerings, requested_dimensions=("package", "campaign_id")
    )
    turn = await producer.run_worker()
    assert len(turn.revisions_committed) == 1
    view = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    assert view["revisions"][0]["row_count"] == 2


async def test_missing_historical_roster_blocks_direct_empty_revision_commit() -> None:
    from adcp.reporting.ledger.package_roster import MissingHistoricalPackageRoster

    producer, store, clock = await _package_producer((), [])

    async def roster(config, period_end):
        return MissingHistoricalPackageRoster()

    producer._package_roster_resolver = roster
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller(ACCOUNT, "buyer-settling")
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=clock[0])
    request = producer._build_slice(
        configuration,
        obligation,
        producer._offerings.snapshot_offering_id,
        now=clock[0],
    )
    result = await producer._source.execute(request, cancel=asyncio.Event())
    assert result.ok
    manifest = producer._verified_manifest(result)
    rows = await producer._read_rows(request, manifest)
    producer._offerings = replace(producer._offerings, requested_dimensions=("campaign_id",))
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation, manifest, rows=rows, finality="snapshot", now=clock[0]
        )
    assert conflict.value.code == "PACKAGE_ROSTER_UNAVAILABLE"
    assert (
        await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        == ()
    )


async def test_service_passes_roster_resolver_to_subscription_planner_and_producer() -> None:
    from tests.test_reporting_subscription_service import AGENCY, NOW, _buy, _Seller

    seen: list[tuple[tuple[str, ...], datetime]] = []

    async def roster(config, period_end):
        from adcp.reporting.ledger.package_roster import CompletePackageRoster

        seen.append((config.media_buy_ids, period_end))
        return CompletePackageRoster(("pkg-media-buy-a1",))

    seller = _Seller(service_kwargs={"package_roster_resolver": roster})
    await seller.subscribe(
        AGENCY, [_buy("media-buy-a1", "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z")]
    )
    turn = await seller.service.run_worker(now=NOW)
    assert turn.subscription_obligations_written == 2
    assert sum(len(item.revisions_committed) for item in turn.configurations.values()) == 2
    assert seen and {buys for buys, _end in seen} == {("media-buy-a1",)}
    assert all(
        binding.producer._package_roster_resolver is roster
        for binding in seller.service._bindings.values()
    )
    view = await seller.status(AGENCY)
    assert all(
        period["coverage"]["package_ids"] == ["pkg-media-buy-a1"] for period in view["periods"]
    )


async def test_package_subscription_rejects_nonpackage_source_context() -> None:
    from tests.test_reporting_subscription_service import AGENCY, NOW, _buy, _context, _Seller

    seller = _Seller(principals=(AGENCY,), service_kwargs={"package_roster_resolver": None})
    seller.service._context_resolver = _context
    await seller.subscribe(
        AGENCY, [_buy("media-buy-a1", "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z")]
    )
    turn = await seller.service.run_worker(now=NOW)
    assert turn.subscription_errors
    assert any("package dimension" in str(error) for error in turn.subscription_errors.values())
    assert seller.source.calls == []
    assert all(not item.revisions_committed for item in turn.configurations.values())
