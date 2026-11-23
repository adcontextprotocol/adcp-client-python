"""Reporting scope ``all_media_buys``: per-period buy freezing at close."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from adcp.reporting.fixtures import OFFICIAL_OFFERING_ID, SNAPSHOT_OFFERING_ID
from adcp.reporting.ledger import (
    CompleteMediaBuyScope,
    CompletePackageRoster,
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    MissingHistoricalMediaBuyScope,
    MissingHistoricalPackageRoster,
    PerMediaBuyPackageRoster,
    ProducerOfferings,
    ReportingConfiguration,
    ReportingPeriodBoundary,
    ReportingProducer,
    ReportingScopeBuy,
    ReportingStatusCaller,
    ReportingStatusHandler,
    UnsupportedMediaBuyRoster,
    WorkerTurn,
    freeze_all_media_buys,
)
from adcp.reporting.ledger.store import _config_payload
from tests.test_reporting_ledger import _configuration
from tests.test_reporting_producer_retry import FailingSource

UTC = timezone.utc
CALLER = ReportingStatusCaller(account_id="acct_1", consumer_id="buyer_1")
# Activated 00:20, hourly: the first owed period is [01:00, 02:00).
FIRST_CLOSE = datetime(2026, 9, 1, 2, 30, tzinfo=UTC)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 1, hour, minute, tzinfo=UTC)


def _period(start: datetime, end: datetime) -> ReportingPeriodBoundary:
    return ReportingPeriodBoundary(
        period_key="p", start=start, end=end, source_timezone="UTC", expected_at=end
    )


def _all_scope(**overrides: object) -> ReportingConfiguration:
    overrides.setdefault("required_finality", "snapshot")
    return _configuration(media_buy_ids=(), media_buy_scope="all_media_buys", **overrides)


class _NoSource:
    @property
    def capabilities(self):  # pragma: no cover - asserting it is never reached
        raise AssertionError("a zero-buy close must not touch the source")

    async def execute(self, request, *, cancel, heartbeat=None):  # pragma: no cover
        raise AssertionError("a zero-buy close must not touch the source")


def _producer(store, *, source=None, **kwargs) -> ReportingProducer:
    return ReportingProducer(
        source=source or _NoSource(),
        offerings=kwargs.pop(
            "offerings",
            ProducerOfferings(
                snapshot_offering_id=SNAPSHOT_OFFERING_ID,
                official_offering_id=OFFICIAL_OFFERING_ID,
            ),
        ),
        store=store,
        clock=lambda: FIRST_CLOSE,
        **kwargs,
    )


class _Scope:
    """A resolver whose answer the test changes between turns."""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[ReportingConfiguration, ReportingPeriodBoundary]] = []

    async def __call__(self, configuration, period):
        self.calls.append((configuration, period))
        return self.result


# -- freeze_all_media_buys ------------------------------------------------------


def test_freeze_keeps_buys_overlapping_the_half_open_period_and_known_by_its_end() -> None:
    period = _period(_at(1), _at(2))
    buys = [
        ReportingScopeBuy("running", _at(0), _at(0), _at(5)),
        ReportingScopeBuy("known-at-cutoff", _at(2), _at(1, 30), _at(3)),
        ReportingScopeBuy("ends-at-start", _at(0), _at(0), _at(1)),
        ReportingScopeBuy("starts-at-end", _at(0), _at(2), _at(3)),
        ReportingScopeBuy("known-after-cutoff", _at(2, 1), _at(1), _at(3)),
        ReportingScopeBuy("running", _at(0), _at(0), _at(5)),
    ]
    assert freeze_all_media_buys(buys, period) == CompleteMediaBuyScope(
        ("known-at-cutoff", "running")
    )
    assert freeze_all_media_buys([], period).media_buy_ids == ()


def test_a_buy_with_gapped_flights_belongs_only_where_a_flight_overlaps() -> None:
    gapped = ReportingScopeBuy("gapped", _at(0), flights=((_at(0), _at(1)), (_at(3), _at(4))))
    assert (gapped.start_time, gapped.end_time) == (_at(0), _at(4))
    assert freeze_all_media_buys([gapped], _period(_at(1), _at(2))).media_buy_ids == ()
    assert freeze_all_media_buys([gapped], _period(_at(3), _at(4))).media_buy_ids == ("gapped",)
    assert ReportingScopeBuy("one", _at(0), _at(0), _at(1)) == ReportingScopeBuy(
        "one", _at(0), flights=((_at(0), _at(1)),)
    )
    with pytest.raises(ValueError):
        ReportingScopeBuy("both", _at(0), _at(0), _at(1), flights=((_at(0), _at(1)),))
    with pytest.raises(ValueError):
        ReportingScopeBuy("neither", _at(0))


def test_scope_buys_refuse_naive_times_and_inverted_flights() -> None:
    with pytest.raises(ValueError):
        ReportingScopeBuy("mb", datetime(2026, 9, 1), _at(0), _at(1))
    with pytest.raises(ValueError):
        ReportingScopeBuy("mb", _at(0), _at(2), _at(1))


# -- the configuration ------------------------------------------------------------


def test_scope_mode_is_in_the_content_hash_and_explicit_hashes_are_unchanged() -> None:
    explicit = _configuration(media_buy_ids=(), required_finality="snapshot")
    assert "media_buy_scope" not in _config_payload(explicit)
    assert _config_payload(_all_scope()) != _config_payload(explicit)
    assert _config_payload(_all_scope())["media_buy_scope"] == "all_media_buys"


async def test_memory_store_round_trips_the_scope_and_refuses_a_second_meaning() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _all_scope()
    await store.put_configuration(configuration)
    assert await store.list_configurations(caller=CALLER) == (configuration,)
    with pytest.raises(LedgerConflictError):
        await store.put_configuration(replace(configuration, media_buy_scope="media_buy_ids"))
    with pytest.raises(LedgerConflictError) as fixed:
        await store.put_configuration(
            replace(configuration, delivery_config_version=2, media_buy_ids=("mb_1",))
        )
    assert fixed.value.code == "INVALID_REQUEST"


# -- period close ----------------------------------------------------------------


async def test_close_freezes_the_resolved_buys_and_feeds_them_to_the_roster() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _all_scope()
    await store.put_configuration(configuration)
    scope = _Scope(CompleteMediaBuyScope(("mb_b", "mb_a")))
    rostered = []

    async def roster(config, period):
        rostered.append(config.media_buy_ids)
        return CompletePackageRoster(("pkg_1",))

    producer = _producer(
        store,
        offerings=ProducerOfferings(
            snapshot_offering_id=SNAPSHOT_OFFERING_ID, requested_dimensions=("package",)
        ),
        media_buy_scope_resolver=scope,
        package_roster_resolver=roster,
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=FIRST_CLOSE)
    assert [(config, period.end) for config, period in scope.calls] == [(configuration, _at(2))]
    assert obligation.media_buy_ids == ("mb_a", "mb_b")
    assert obligation.scope_resolved_at == obligation.period.end == _at(2)
    assert obligation.package_ids == ("pkg_1",) and obligation.coverage_status == "full"
    assert rostered == [("mb_a", "mb_b")]


async def test_frozen_buys_are_the_source_denominator() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _all_scope()
    await store.put_configuration(configuration)
    source = FailingSource()
    producer = _producer(
        store, source=source, media_buy_scope_resolver=_Scope(CompleteMediaBuyScope(("mb_x",)))
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=FIRST_CLOSE)
    await producer.acquire_obligation(configuration, obligation, now=FIRST_CLOSE)
    (request,) = source.requests
    assert [item.media_buy_id for item in request.coverage.constituents] == ["mb_x"]


async def test_a_later_or_backdated_buy_never_rewrites_a_committed_period() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _all_scope()
    await store.put_configuration(configuration)
    scope = _Scope(CompleteMediaBuyScope(("mb_a",)))
    producer = _producer(store, media_buy_scope_resolver=scope)
    (first,) = await producer.close_elapsed_periods(configuration, now=FIRST_CLOSE)
    scope.result = CompleteMediaBuyScope(("mb_a", "mb_backdated"))
    assert await producer.close_elapsed_periods(configuration, now=FIRST_CLOSE) == []
    assert len(scope.calls) == 1
    (retained,) = await store.list_generation_obligations(configuration.generation_key)
    assert retained == first and retained.media_buy_ids == ("mb_a",)
    # The next period freezes its own membership.
    (second,) = await producer.close_elapsed_periods(configuration, now=_at(3, 30))
    assert second.media_buy_ids == ("mb_a", "mb_backdated")


async def test_unresolved_scope_keeps_the_period_open_without_progress() -> None:
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
            raise AssertionError("closing a period needs no source work")

        async def next_producer_obligations(self, configuration, *, now, limit):
            raise AssertionError("closing a period needs no source work")

        async def finish_producer_acquisition(self, configuration, *, reporting_obligation_id):
            raise AssertionError("closing a period needs no source work")

    store = ProgressStore()
    configuration = _all_scope()
    await store.put_configuration(configuration)
    scope = _Scope(MissingHistoricalMediaBuyScope("buy edited after cutoff"))
    producer = _producer(store, media_buy_scope_resolver=scope)
    assert await producer.close_elapsed_periods(configuration, now=_at(3, 30)) == []
    assert len(scope.calls) == 1  # later periods wait behind the open one
    assert await store.list_generation_obligations(configuration.generation_key) == ()
    assert store.closed_through is None

    scope.result = CompleteMediaBuyScope(("mb_a",))
    closed = await producer.close_elapsed_periods(configuration, now=_at(3, 30))
    assert [item.period.end for item in closed] == [_at(2), _at(3)]
    assert store.closed_through == _at(3)


async def test_zero_buy_period_publishes_a_zero_row_snapshot_without_the_source() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _all_scope()
    await store.put_configuration(configuration)
    producer = _producer(store, media_buy_scope_resolver=_Scope(CompleteMediaBuyScope(())))
    turn = await producer.run_configuration(configuration, now=FIRST_CLOSE)
    (obligation,) = await store.list_generation_obligations(configuration.generation_key)
    assert obligation.media_buy_ids == () and obligation.coverage_status == "full"
    (revision,) = await store.list_revisions(
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )
    assert turn.revisions_committed == [revision.reporting_revision_id]
    assert (revision.finality, revision.row_count, revision.control_totals) == ("snapshot", 0, ())
    assert revision.data_through == obligation.period.end
    # A later turn is a no-op, not a second revision.
    again = await producer.run_configuration(configuration, now=FIRST_CLOSE)
    assert again.revisions_committed == []

    view = await ReportingStatusHandler(store).handle({"view": "periods"}, caller=CALLER)
    (period,) = view["periods"]
    assert period["coverage"]["status"] == "full"
    assert period["coverage"]["media_buy_ids"] == []
    assert period["coverage"]["evaluated_at"] == "2026-09-01T02:00:00Z"


async def test_an_explicit_empty_snapshot_scope_also_closes_with_zero_rows() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _configuration(media_buy_ids=(), required_finality="snapshot")
    await store.put_configuration(configuration)
    turn = await _producer(store).run_configuration(configuration, now=FIRST_CLOSE)
    assert len(turn.revisions_committed) == 1 and turn.slices_failed == []


async def test_a_zero_buy_official_period_is_never_closed_from_roster_proof() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _configuration(media_buy_ids=(), required_finality="official")
    await store.put_configuration(configuration)
    source = FailingSource()
    producer = ReportingProducer(
        source=source,
        offerings=ProducerOfferings(official_offering_id=OFFICIAL_OFFERING_ID),
        store=store,
        clock=lambda: FIRST_CLOSE,
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=FIRST_CLOSE)
    turn = WorkerTurn()
    with pytest.raises(LedgerConflictError) as refused:
        await producer.acquire_obligation(configuration, obligation, turn=turn, now=FIRST_CLOSE)
    assert refused.value.code == "EMPTY_DENOMINATOR"
    assert source.calls == 0 and turn.revisions_committed == []
    assert (
        await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        == ()
    )


async def test_without_a_resolver_an_all_scope_generation_never_closes() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _all_scope()
    await store.put_configuration(configuration)
    with pytest.raises(LedgerConflictError) as refused:
        await _producer(store).close_elapsed_periods(configuration, now=FIRST_CLOSE)
    assert refused.value.code == "UNSUPPORTED_FEATURE"
    assert await store.list_generation_obligations(configuration.generation_key) == ()


async def test_explicit_media_buy_ids_close_unchanged_without_calling_the_resolver() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    scope = _Scope(CompleteMediaBuyScope(("other",)))
    producer = _producer(store, media_buy_scope_resolver=scope)
    (obligation,) = await producer.close_elapsed_periods(configuration, now=FIRST_CLOSE)
    assert obligation.media_buy_ids == ("mb_1",) and obligation.coverage is None
    assert scope.calls == []


# -- per-buy coverage ---------------------------------------------------------------


async def test_per_buy_roster_partitions_coverage_and_withholds_publication() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _all_scope()
    await store.put_configuration(configuration)

    async def roster(config, period):
        return PerMediaBuyPackageRoster(
            {
                "mb_a": CompletePackageRoster(("pkg_a",)),
                "mb_b": MissingHistoricalPackageRoster(),
                "mb_c": UnsupportedMediaBuyRoster("provider_limitation", ("pkg_c",)),
            }
        )

    producer = _producer(
        store,
        offerings=ProducerOfferings(
            snapshot_offering_id=SNAPSHOT_OFFERING_ID, requested_dimensions=("package",)
        ),
        media_buy_scope_resolver=_Scope(CompleteMediaBuyScope(("mb_a", "mb_b", "mb_c"))),
        package_roster_resolver=roster,
    )
    turn = await producer.run_configuration(configuration, now=FIRST_CLOSE)
    assert turn.revisions_committed == [] and turn.slices_failed == []
    (obligation,) = await store.list_generation_obligations(configuration.generation_key)
    assert obligation.coverage_status == "partial"
    view = await ReportingStatusHandler(store).handle({"view": "periods"}, caller=CALLER)
    (period,) = view["periods"]
    coverage = period["coverage"]
    assert coverage == {
        "status": "partial",
        "evaluated_at": "2026-09-01T02:00:00Z",
        "media_buy_ids": ["mb_a", "mb_b", "mb_c"],
        "fully_covered_media_buy_ids": ["mb_a"],
        "partially_covered_media_buy_ids": [],
        "unsupported_media_buy_ids": ["mb_c"],
        "unknown_media_buy_ids": ["mb_b"],
        "package_ids": ["pkg_a", "pkg_c"],
        "covered_package_ids": ["pkg_a"],
        "unsupported_package_ids": ["pkg_c"],
        "unknown_package_ids": [],
        "limitations": [
            {"reason": "capability_unknown", "media_buy_id": "mb_b"},
            {"reason": "provider_limitation", "media_buy_id": "mb_c", "package_ids": ["pkg_c"]},
        ],
    }
    assert period["health"] == "action_required"
    assert "REPORTING_COVERAGE_INCOMPLETE" in {issue["code"] for issue in period["issues"]}


async def test_a_per_buy_roster_must_name_exactly_the_frozen_buys() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _all_scope()
    await store.put_configuration(configuration)

    async def roster(config, period):
        return PerMediaBuyPackageRoster({"mb_a": CompletePackageRoster(())})

    producer = _producer(
        store,
        offerings=ProducerOfferings(
            snapshot_offering_id=SNAPSHOT_OFFERING_ID, requested_dimensions=("package",)
        ),
        media_buy_scope_resolver=_Scope(CompleteMediaBuyScope(("mb_a", "mb_b"))),
        package_roster_resolver=roster,
    )
    with pytest.raises(ValueError, match="exactly the frozen media buys"):
        await producer.close_elapsed_periods(configuration, now=FIRST_CLOSE)
    assert await store.list_generation_obligations(configuration.generation_key) == ()


# -- the service configure path ---------------------------------------------------


async def _service(**kwargs):
    from adcp.reporting.service import ReliableReportingService
    from tests.test_reliable_reporting_service import NOW, _account_context

    return ReliableReportingService(
        store=InMemoryReportingLedgerStore(clock=lambda: NOW),
        clock=lambda: NOW,
        account_context=_account_context,
        **kwargs,
    )


async def test_service_refuses_all_scope_without_a_resolver() -> None:
    service = await _service()
    with pytest.raises(LedgerConflictError) as refused:
        await service.configure(_all_scope())
    assert refused.value.code == "UNSUPPORTED_FEATURE"


async def test_service_refuses_all_scope_at_official_finality() -> None:
    service = await _service(media_buy_scope_resolver=_Scope(CompleteMediaBuyScope(())))
    with pytest.raises(LedgerConflictError, match="attestation hook") as refused:
        await service.configure(_all_scope(required_finality="official"))
    assert refused.value.code == "UNSUPPORTED_FEATURE"


def test_scope_gate_accepts_snapshot_all_scope_and_leaves_explicit_official_alone() -> None:
    from adcp.reporting.ledger import reject_unsupported_media_buy_scope

    reject_unsupported_media_buy_scope(_all_scope(), resolver_wired=True)
    reject_unsupported_media_buy_scope(_configuration(), resolver_wired=False)
    assert _configuration().required_finality == "official"


async def test_service_admits_snapshot_all_scope_with_a_resolver_and_wires_it() -> None:
    from adcp.reporting.fixtures import redacted_capabilities
    from adcp.reporting.service import ReliableReportingService
    from adcp.reporting.testing import ScriptedReportingAdapter
    from tests.test_reliable_reporting_service import _account_context
    from tests.test_reliable_reporting_service import _configuration as _service_configuration

    scope = _Scope(CompleteMediaBuyScope(()))
    service = ReliableReportingService.memory(
        account_context=_account_context, media_buy_scope_resolver=scope
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
    configuration = replace(
        _service_configuration(), media_buy_ids=(), media_buy_scope="all_media_buys"
    )
    await service.configure(configuration)
    binding = service._bindings[configuration.generation_key]
    assert binding.producer._media_buy_scope_resolver is scope


async def test_explicit_scope_without_a_roster_resolver_publishes_as_before() -> None:
    """Explicit media_buy_ids, package dimension, no roster resolver: unchanged behaviour."""
    store = InMemoryReportingLedgerStore(clock=lambda: FIRST_CLOSE)
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    source = FailingSource()
    producer = _producer(
        store,
        source=source,
        offerings=ProducerOfferings(
            snapshot_offering_id=SNAPSHOT_OFFERING_ID, requested_dimensions=("package",)
        ),
    )
    (obligation,) = await producer.close_elapsed_periods(configuration, now=FIRST_CLOSE)
    assert (obligation.coverage_status, obligation.coverage) == ("unknown", None)
    assert obligation.package_ids == ()
    await producer.acquire_obligation(configuration, obligation, now=FIRST_CLOSE)
    assert source.calls == 1  # the source is read and its result published, as before
    view = await ReportingStatusHandler(store).handle({"view": "periods"}, caller=CALLER)
    (period,) = view["periods"]
    assert period["coverage"] == {
        "status": "unknown",
        "evaluated_at": "2026-09-01T02:00:00Z",
        "media_buy_ids": ["mb_1"],
        "fully_covered_media_buy_ids": [],
        "partially_covered_media_buy_ids": [],
        "unsupported_media_buy_ids": [],
        "unknown_media_buy_ids": ["mb_1"],
        "package_ids": [],
        "covered_package_ids": [],
        "unsupported_package_ids": [],
        "unknown_package_ids": [],
        "limitations": [],
    }
