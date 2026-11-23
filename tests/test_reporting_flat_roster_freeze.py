"""Close-time flat package coverage for exact media-buy obligations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    ProducerOfferings,
    ReportingProducer,
    ReportingStatusCaller,
    ReportingStatusHandler,
)
from adcp.reporting.ledger.package_roster import (
    CompletePackageRoster,
    MissingHistoricalPackageRoster,
)
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.service_subscriptions import (
    ReportingSubscriptionServiceOptions,
    plan_subscription_cycle,
)
from adcp.reporting.subscriptions import SubscriptionObligationPlanner
from adcp.reporting.subscriptions import ledger as subscription_ledger
from adcp.types import SyncAccountsRequest
from adcp.validation.schema_loader import get_named_validator
from tests.test_reporting_ledger import CALLER, _configuration, _obligation
from tests.test_reporting_scope_models import onboarding_request
from tests.test_reporting_subscription_engine import OFFERINGS, at, buy
from tests.test_reporting_subscription_ledger import AGENCY, OTHER, _world_with


class _NoSource:
    @property
    def capabilities(self):
        raise AssertionError("source fetch is not needed to close an obligation")

    async def execute(self, request, *, cancel, heartbeat=None):
        raise AssertionError("source fetch is not needed to close an obligation")


async def test_producer_freezes_flat_roster_and_exact_period_before_source_fetch() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    seen = []

    async def roster(config, period):
        seen.append((config.media_buy_ids, period))
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
    assert seen == [(("mb_1",), obligation.period)]
    assert obligation.package_ids == ("pkg-measured", "pkg-zero")
    assert obligation.coverage_status == "full"
    view = await ReportingStatusHandler(store).handle({"view": "periods"}, caller=CALLER)
    assert view["periods"][0]["coverage"]["package_ids"] == ["pkg-measured", "pkg-zero"]
    assert view["periods"][0]["coverage"]["covered_package_ids"] == [
        "pkg-measured",
        "pkg-zero",
    ]


async def test_reclose_keeps_the_frozen_roster_after_the_resolver_changes() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    package_ids = ["pkg-original"]
    calls = 0

    async def roster(config, period):
        nonlocal calls
        calls += 1
        return CompletePackageRoster(tuple(package_ids))

    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(requested_dimensions=("package",)),
        store=store,
        package_roster_resolver=roster,
    )
    now = datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
    (first,) = await producer.close_elapsed_periods(configuration, now=now)
    package_ids[:] = ["pkg-later"]
    assert await producer.close_elapsed_periods(configuration, now=now) == []
    (retained,) = await store.list_generation_obligations(configuration.generation_key)
    assert retained == first
    assert retained.package_ids == ("pkg-original",)
    assert calls == 1


async def test_account_producer_refuses_unresolved_buy_scope_before_progress() -> None:
    class ProgressStore(InMemoryReportingLedgerStore):
        def __init__(self) -> None:
            super().__init__()
            self.closed_through: datetime | None = None

        async def producer_closed_through(self, configuration):
            return self.closed_through

        async def commit_producer_period(self, configuration, obligation, *, previous_end):
            self.closed_through = obligation.period.end
            return await self.commit_obligation(obligation)

        async def producer_constituents(self, configuration, obligation):
            raise AssertionError("source fetch is not needed to close an obligation")

        async def next_producer_obligations(self, configuration, *, now, limit):
            raise AssertionError("source fetch is not needed to close an obligation")

        async def finish_producer_acquisition(self, configuration, *, reporting_obligation_id):
            raise AssertionError("source fetch is not needed to close an obligation")

    request = SyncAccountsRequest.model_validate(onboarding_request({"all_media_buys": True}))
    scope = request.accounts[0].reporting_delivery_configs[0].scope
    assert scope.all_media_buys is True and scope.media_buy_ids is None
    store = ProgressStore()
    # all_media_buys is its own persisted scope mode, never an explicit empty list.
    configuration = replace(
        _configuration(required_finality="snapshot"),
        media_buy_ids=(),
        media_buy_scope="all_media_buys",
    )
    explicit_empty = replace(
        _configuration(), delivery_config_id="explicit_empty", media_buy_ids=()
    )
    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(requested_dimensions=("package",)),
        store=store,
    )
    for generation, code in (
        (configuration, "UNSUPPORTED_FEATURE"),  # no media buy scope resolver is wired
        (explicit_empty, "HISTORY_UNAVAILABLE"),
    ):
        await store.put_configuration(generation)
        with pytest.raises(LedgerConflictError) as denied:
            await producer.close_elapsed_periods(
                generation, now=datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
            )
        assert denied.value.code == code
        assert await store.list_generation_obligations(generation.generation_key) == ()
    assert store.closed_through is None


@pytest.mark.parametrize("stub", [False, True])
async def test_subscription_complete_empty_uses_exact_period_and_full_coverage(stub: bool) -> None:
    world = await _world_with(AGENCY, [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z")])
    (owed,) = await world.engine.frozen_owed_sets(AGENCY, now=at("2026-10-03T00:00:00Z"))
    period = replace(owed.periods[0], media_buy_ids=(), stub=stub)
    if stub:
        period = replace(period, period_start="2026-10-01T05:30:00Z")
    owed = replace(owed, periods=(period,))
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-03T00:00:00Z"))
    seen = []

    async def roster(configuration, boundary):
        seen.append((configuration.media_buy_ids, boundary))
        return CompletePackageRoster(())

    planner = SubscriptionObligationPlanner(
        store, offerings=OFFERINGS, currency="USD", package_roster_resolver=roster
    )
    result = await planner.plan(owed)
    (obligation,) = await store.list_generation_obligations(result.generations[0])
    assert obligation.media_buy_ids == ()
    assert obligation.package_ids == ()
    assert obligation.coverage_status == "full"
    assert seen == [((), obligation.period)]
    assert obligation.period.start == at(period.period_start)
    assert obligation.stub is stub


async def test_subscription_unresolved_empty_roster_does_not_advance_owed_set() -> None:
    world = await _world_with(AGENCY, [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z")])
    (owed,) = await world.engine.frozen_owed_sets(AGENCY, now=at("2026-10-03T00:00:00Z"))
    owed = replace(owed, periods=(replace(owed.periods[0], media_buy_ids=()),))
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-03T00:00:00Z"))

    async def roster(configuration, boundary):
        assert configuration.media_buy_ids == ()
        return MissingHistoricalPackageRoster()

    planner = SubscriptionObligationPlanner(
        store, offerings=OFFERINGS, currency="USD", package_roster_resolver=roster
    )
    with pytest.raises(LedgerConflictError) as denied:
        await planner.plan(owed)
    assert denied.value.code == "HISTORY_UNAVAILABLE"
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller("acct-1", AGENCY.subject)
    )
    assert await store.list_generation_obligations(configuration.generation_key) == ()
    assert await store.owed_set_through(configuration.generation_key) is None


async def test_subscription_roster_periods_share_one_planning_deadline(monkeypatch) -> None:
    world = await _world_with(AGENCY, [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z")])
    (owed,) = await world.engine.frozen_owed_sets(AGENCY, now=at("2026-10-04T00:00:00Z"))
    owed = replace(owed, periods=owed.periods[:2])
    assert len(owed.periods) == 2
    store = InMemoryReportingLedgerStore()
    deadlines: list[float] = []
    resolve = subscription_ledger._resolve_roster_until

    async def capture(resolver, configuration, period, *, deadline):
        deadlines.append(deadline)
        await asyncio.sleep(0.001)
        return await resolve(resolver, configuration, period, deadline=deadline)

    monkeypatch.setattr(subscription_ledger, "_resolve_roster_until", capture)

    async def roster(configuration, period):
        return CompletePackageRoster(("pkg-1",))

    planner = SubscriptionObligationPlanner(
        store,
        offerings=OFFERINGS,
        currency="USD",
        package_roster_resolver=roster,
        package_roster_timeout=timedelta(seconds=1),
    )
    await planner.plan(owed)
    assert len(deadlines) == 2
    assert deadlines[0] == deadlines[1]


async def test_subscription_cycle_reserves_roster_budget_for_later_owed_sets() -> None:
    class Engine:
        async def frozen_owed_sets(self, principal, *, now):
            return (object(), object()) if principal == AGENCY else (object(),)

        async def subscription_coverage(self, *args, **kwargs):
            return ()

    class Planner:
        def __init__(self) -> None:
            self.deadlines: list[float | None] = []
            self.cycle_deadline: float | None = None

        def roster_deadline(self) -> float:
            self.cycle_deadline = asyncio.get_running_loop().time() + 30
            return self.cycle_deadline

        async def plan(self, owed, *, through, roster_deadline=None, stopping=None):
            self.deadlines.append(roster_deadline)
            return SimpleNamespace(obligations_written=())

    planner = Planner()
    options = ReportingSubscriptionServiceOptions(
        engine=Engine(),
        list_principals=lambda: (AGENCY, OTHER),
        resolve_principal=lambda request, context: AGENCY,
    )
    written, failures = await plan_subscription_cycle(
        options, planner, now=at("2026-10-04T00:00:00Z"), stopping=lambda: False
    )
    assert written == 0 and not failures
    assert planner.cycle_deadline is not None
    assert len(planner.deadlines) == 3
    assert all(deadline is not None for deadline in planner.deadlines)
    assert planner.deadlines[0] < planner.deadlines[1] < planner.deadlines[2]
    assert planner.deadlines[2] <= planner.cycle_deadline


async def test_subscription_stop_during_roster_does_not_advance_owed_set() -> None:
    world = await _world_with(AGENCY, [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z")])
    (owed,) = await world.engine.frozen_owed_sets(AGENCY, now=at("2026-10-04T00:00:00Z"))
    owed = replace(owed, periods=owed.periods[:2])
    assert len(owed.periods) == 2
    store = InMemoryReportingLedgerStore()
    stopping = False

    async def roster(configuration, period):
        nonlocal stopping
        stopping = True
        return CompletePackageRoster(("pkg-1",))

    planner = SubscriptionObligationPlanner(
        store, offerings=OFFERINGS, currency="USD", package_roster_resolver=roster
    )
    with pytest.raises(asyncio.CancelledError):
        await planner.plan(owed, stopping=lambda: stopping)
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller("acct-1", AGENCY.subject)
    )
    assert await store.list_generation_obligations(configuration.generation_key) == ()
    assert await store.owed_set_through(configuration.generation_key) is None


async def test_summary_classifies_unknown_buy_when_package_roster_is_missing() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)

    async def roster(config, period):
        return MissingHistoricalPackageRoster()

    producer = ReportingProducer(
        source=_NoSource(),
        offerings=ProducerOfferings(requested_dimensions=("package",)),
        store=store,
        package_roster_resolver=roster,
    )
    await producer.close_elapsed_periods(
        configuration, now=datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)
    )
    summary = await ReportingStatusHandler(store).handle({"view": "summary"}, caller=CALLER)
    coverage = summary["coverage"]
    validator = get_named_validator("core/reporting-coverage.json")
    assert validator is not None and not list(validator.iter_errors(coverage))
    assert coverage["status"] == "unknown"
    assert coverage["media_buy_ids"] == ["mb_1"]
    assert coverage["unknown_media_buy_ids"] == ["mb_1"]


async def test_summary_marks_a_buy_partial_across_full_and_unknown_periods() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    await store.commit_obligation(_obligation(configuration, coverage_status="unknown"))
    await store.commit_obligation(_obligation(configuration, ordinal=2, coverage_status="full"))

    summary = await ReportingStatusHandler(store).handle({"view": "summary"}, caller=CALLER)
    coverage = summary["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["media_buy_ids"] == ["mb_1"]
    assert coverage["partially_covered_media_buy_ids"] == ["mb_1"]
    assert coverage["unknown_media_buy_ids"] == []


async def test_service_forwards_roster_to_subscription_planner_and_producer() -> None:
    from tests.test_reporting_subscription_service import AGENCY, NOW, _buy, _Seller

    seen = []

    async def roster(configuration, period):
        seen.append((configuration.media_buy_ids, period))
        return CompletePackageRoster(("pkg-media-buy-a1",))

    seller = _Seller(service_kwargs={"package_roster_resolver": roster})
    await seller.subscribe(
        AGENCY, [_buy("media-buy-a1", "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z")]
    )
    turn = await seller.service.run_worker(now=NOW)
    assert turn.subscription_obligations_written == 2
    assert seen and {buys for buys, _period in seen} == {("media-buy-a1",)}
    assert all(
        binding.producer._package_roster_resolver is roster
        for binding in seller.service._bindings.values()
    )
    view = await seller.status(AGENCY)
    assert all(
        period["coverage"]["package_ids"] == ["pkg-media-buy-a1"] for period in view["periods"]
    )
