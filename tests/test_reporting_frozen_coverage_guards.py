"""Obligations and revisions stay inside the coverage each obligation froze.

* **At freeze**: a package-grain period freezes its package scope from
  period-end roster evidence, and a zero-buy period owns no packages. A proven
  empty package set over frozen buys is legitimate (a buy with no package in
  flight that period) and still freezes ``full``.
* **At publish**: a revision whose rows name a media buy outside the frozen
  ``media_buy_ids``, or at package grain a package outside the covered
  packages, is refused with :class:`ReportingRowCoverageError` before anything
  is committed. Later periods still publish in the same turn, and the error is
  re-raised at the end of it.

Every consistent case below publishes exactly what it published before the
guards existed: each is run twice, once with the guards disabled, and the
stored obligations, revisions and rows must be identical.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from adcp.reporting.fixtures import OFFICIAL_OFFERING_ID, SNAPSHOT_OFFERING_ID
from adcp.reporting.inline_source import (
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.ledger import (
    CompleteMediaBuyScope,
    CompletePackageRoster,
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    MissingHistoricalPackageRoster,
    PerMediaBuyPackageRoster,
    ProducerOfferings,
    ReportingConfiguration,
    ReportingPackageScopeError,
    ReportingProducer,
    ReportingRowCoverageError,
    ReportingStatusCaller,
    ReportingStatusHandler,
    UnsupportedMediaBuyRoster,
)
from adcp.reporting.ledger import producer as producer_module
from adcp.reporting.ledger.coverage_guard import (
    _package_scope_conflict,
    _require_rows_within_frozen_coverage,
)
from adcp.reporting.subscriptions import SubscriptionObligationPlanner
from tests.test_reporting_flat_roster_freeze import _NoSource
from tests.test_reporting_ledger import CALLER, _configuration, _obligation
from tests.test_reporting_producer_retry import _ready_capabilities
from tests.test_reporting_subscription_engine import OFFERINGS, at, buy
from tests.test_reporting_subscription_ledger import AGENCY, _world_with

UTC = timezone.utc
# Activated 00:20, hourly: [01:00, 02:00) has closed by 02:30, and
# [02:00, 03:00) as well by 03:30.
ONE_CLOSED = datetime(2026, 9, 1, 2, 30, tzinfo=UTC)
TWO_CLOSED = datetime(2026, 9, 1, 3, 30, tzinfo=UTC)
FIRST_START = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)


def _offerings(*dimensions: str) -> ProducerOfferings:
    return ProducerOfferings(
        snapshot_offering_id=SNAPSHOT_OFFERING_ID,
        official_offering_id=OFFICIAL_OFFERING_ID,
        requested_dimensions=dimensions,
    )


PACKAGE = _offerings("package")
BUY = _offerings()


def _exact(**overrides: Any) -> ReportingConfiguration:
    overrides.setdefault("required_finality", "snapshot")
    return _configuration(**overrides)


def _all_scope(**overrides: Any) -> ReportingConfiguration:
    return _exact(media_buy_ids=(), media_buy_scope="all_media_buys", **overrides)


class _Fetch:
    """Answers each slice with ``rows(request)`` and records the periods asked for."""

    def __init__(self, rows: Callable[[Any], list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.periods: list[datetime] = []

    def __call__(self, request: Any) -> list[dict[str, Any]]:
        self.periods.append(request.period.start)
        return self.rows(request)


def _package_rows(*pairs: tuple[str, str]) -> Callable[[Any], list[dict[str, Any]]]:
    return lambda request: [
        {"media_buy_id": buy_id, "package_id": package_id, "impressions": 10}
        for buy_id, package_id in pairs
    ]


def _producer(
    store: InMemoryReportingLedgerStore,
    fetch: _Fetch,
    *,
    now: datetime,
    offerings: ProducerOfferings = PACKAGE,
    roster: Any = None,
    scope: Any = None,
) -> ReportingProducer:
    source = InlineReportingSource(
        capabilities=_ready_capabilities(),
        fetch=fetch,
        staging=InMemoryStagingStore(),
        seals=InMemorySealStore(),
        clock=lambda: now,
    )
    return ReportingProducer(
        source=source,
        object_reader=source.staging,
        offerings=offerings,
        store=store,
        clock=lambda: now,
        media_buy_scope_resolver=scope,
        package_roster_resolver=roster,
    )


async def _ledger(store: InMemoryReportingLedgerStore, configuration: ReportingConfiguration):
    """Everything a run left in the ledger: obligations, revisions and their rows."""
    obligations = await store.list_generation_obligations(configuration.generation_key)
    revisions = []
    for obligation in obligations:
        for revision in await store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        ):
            page = await store.read_revision_rows(
                account_id=obligation.account_id,
                reporting_revision_id=revision.reporting_revision_id,
            )
            revisions.append((revision, page.rows))
    return obligations, revisions


async def _run(
    configuration: ReportingConfiguration,
    fetch: _Fetch,
    *,
    now: datetime = ONE_CLOSED,
    **producer: Any,
):
    store = InMemoryReportingLedgerStore(clock=lambda: now)
    await store.put_configuration(configuration)
    turn = await _producer(store, fetch, now=now, **producer).run_configuration(
        configuration, now=now
    )
    return store, turn, await _ledger(store, configuration)


def _without_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """The producer as it was before the guards: neither check runs."""
    monkeypatch.setattr(producer_module, "_package_scope_conflict", lambda *a, **k: None)
    monkeypatch.setattr(
        producer_module, "_require_rows_within_frozen_coverage", lambda *a, **k: None
    )


async def _same_as_before_the_guards(
    monkeypatch: pytest.MonkeyPatch,
    configuration: ReportingConfiguration,
    rows: Callable[[Any], list[dict[str, Any]]],
    **producer: Any,
):
    _, turn, guarded = await _run(configuration, _Fetch(rows), **producer)
    with monkeypatch.context() as patch:
        _without_guards(patch)
        _, before_turn, before = await _run(configuration, _Fetch(rows), **producer)
    assert guarded == before
    assert turn.revisions_committed == before_turn.revisions_committed
    assert turn.slices_failed == before_turn.slices_failed == []
    return guarded


async def _period_coverage(store: InMemoryReportingLedgerStore) -> list[dict[str, Any]]:
    view = await ReportingStatusHandler(store).handle({"view": "periods"}, caller=CALLER)
    return [period["coverage"] for period in view["periods"]]


# -- must not break: consistent obligations publish exactly as before ------------------


async def test_a_consistent_exact_package_day_publishes_as_before(monkeypatch) -> None:
    async def roster(configuration, period):
        return CompletePackageRoster(("pkg_1", "pkg_2"))

    obligations, revisions = await _same_as_before_the_guards(
        monkeypatch,
        _exact(),
        _package_rows(("mb_1", "pkg_1"), ("mb_1", "pkg_2")),
        roster=roster,
    )
    (obligation,) = obligations
    assert (obligation.coverage_status, obligation.package_ids) == ("full", ("pkg_1", "pkg_2"))
    ((_, rows),) = revisions
    assert {row["package_id"] for row in rows} == {"pkg_1", "pkg_2"}


async def test_a_consistent_all_media_buys_per_buy_day_publishes_as_before(monkeypatch) -> None:
    async def scope(configuration, period):
        return CompleteMediaBuyScope(("mb_a", "mb_b"))

    async def roster(configuration, period):
        return PerMediaBuyPackageRoster(
            {"mb_a": CompletePackageRoster(("pkg_a",)), "mb_b": CompletePackageRoster(("pkg_b",))}
        )

    obligations, revisions = await _same_as_before_the_guards(
        monkeypatch,
        _all_scope(),
        _package_rows(("mb_a", "pkg_a"), ("mb_b", "pkg_b")),
        roster=roster,
        scope=scope,
    )
    (obligation,) = obligations
    assert obligation.package_ids == ("pkg_a", "pkg_b") and len(revisions) == 1


async def test_a_buy_grain_feed_keeps_publishing_flattened_package_rows(monkeypatch) -> None:
    """At buy grain ``package_id`` is not a coverage dimension.

    The SDK's own delivery flattening emits ``by_package`` rows on buy-grain
    feeds, so the guard does not hold them against the (empty) package set.
    """
    obligations, revisions = await _same_as_before_the_guards(
        monkeypatch,
        _exact(),
        _package_rows(("mb_1", "pkg_1")),
        offerings=BUY,
    )
    (obligation,) = obligations
    assert obligation.package_ids == () and obligation.coverage_status == "full"
    assert len(revisions) == 1


@pytest.mark.parametrize(
    "rows",
    [[], [{"media_buy_id": "mb_1", "impressions": 0}]],
    ids=["no-rows", "buy-only-zero-row"],
)
async def test_a_proven_empty_package_set_freezes_full_and_publishes(monkeypatch, rows) -> None:
    """A buy can have no package in flight in a period; the roster proves it with ``()``.

    An explicitly configured buy is echoed on a day outside its flight, for
    example. The period freezes ``full`` with no packages and publishes rows
    that name no package.
    """

    async def roster(configuration, period):
        return CompletePackageRoster(())

    obligations, revisions = await _same_as_before_the_guards(
        monkeypatch, _exact(), lambda request: list(rows), roster=roster
    )
    (obligation,) = obligations
    assert (obligation.media_buy_ids, obligation.package_ids) == (("mb_1",), ())
    assert obligation.coverage_status == "full"
    assert len(revisions) == 1


async def test_an_unknown_day_still_publishes_buy_rows(monkeypatch) -> None:
    """Status-only ``unknown`` coverage keeps publishing rows that name only its buys."""

    async def roster(configuration, period):
        return MissingHistoricalPackageRoster()

    obligations, revisions = await _same_as_before_the_guards(
        monkeypatch,
        _exact(),
        lambda request: [{"media_buy_id": "mb_1", "impressions": 10}],
        roster=roster,
    )
    (obligation,) = obligations
    assert obligation.coverage_status == "unknown" and obligation.coverage is None
    assert len(revisions) == 1


async def test_a_zero_buy_all_media_buys_day_still_closes_as_before(monkeypatch) -> None:
    async def scope(configuration, period):
        return CompleteMediaBuyScope(())

    async def roster(configuration, period):
        raise AssertionError("a zero-buy period has nothing to roster")

    obligations, revisions = await _same_as_before_the_guards(
        monkeypatch, _all_scope(), lambda request: [], roster=roster, scope=scope
    )
    (obligation,) = obligations
    assert obligation.media_buy_ids == () and obligation.coverage_status == "full"
    ((revision, rows),) = revisions
    assert revision.row_count == 0 and not rows


# -- refused: a revision outside its obligation's frozen coverage ----------------------


async def test_package_rows_on_a_day_frozen_with_no_packages_are_refused() -> None:
    """The incident: a package day froze ``full`` with ``[]``, then the source sent package rows.

    The wire coverage of that day says ``covered_package_ids: []``, so no buyer
    can accept these rows, and the obligation cannot change.
    """

    async def roster(configuration, period):
        return CompletePackageRoster(())

    configuration = _exact()
    store = InMemoryReportingLedgerStore(clock=lambda: ONE_CLOSED)
    await store.put_configuration(configuration)
    fetch = _Fetch(_package_rows(("mb_1", "pkg_1"), ("mb_1", "pkg_2")))
    with pytest.raises(ReportingRowCoverageError) as refused:
        await _producer(store, fetch, now=ONE_CLOSED, roster=roster).run_configuration(
            configuration, now=ONE_CLOSED
        )

    error = refused.value
    assert isinstance(error, LedgerConflictError) and error.code == "ROW_COVERAGE_MISMATCH"
    (obligation,) = await store.list_generation_obligations(configuration.generation_key)
    assert error.reporting_obligation_id == obligation.reporting_obligation_id
    assert (error.period_start, error.period_end) == (
        obligation.period.start,
        obligation.period.end,
    )
    assert error.package_ids == ("pkg_1", "pkg_2") and error.media_buy_ids == ()
    assert error.coverage_status == "full"
    for named in (obligation.reporting_obligation_id, "pkg_1", "pkg_2", "2026-09-01T01:00:00"):
        assert named in str(error)
    obligations, revisions = await _ledger(store, configuration)
    assert revisions == []
    assert (await _period_coverage(store))[0]["covered_package_ids"] == []


async def test_package_rows_on_an_unknown_package_day_are_refused() -> None:
    """11-23: the day froze ``unknown`` with no packages; its rows name packages."""

    async def roster(configuration, period):
        return MissingHistoricalPackageRoster()

    configuration = _exact()
    store = InMemoryReportingLedgerStore(clock=lambda: ONE_CLOSED)
    await store.put_configuration(configuration)
    with pytest.raises(ReportingRowCoverageError) as refused:
        await _producer(
            store, _Fetch(_package_rows(("mb_1", "pkg_1"))), now=ONE_CLOSED, roster=roster
        ).run_configuration(configuration, now=ONE_CLOSED)
    assert refused.value.coverage_status == "unknown"
    assert refused.value.package_ids == ("pkg_1",)
    assert (await _ledger(store, configuration))[1] == []


@pytest.mark.parametrize("offerings", [PACKAGE, BUY], ids=["package-grain", "buy-grain"])
async def test_a_row_for_a_buy_outside_the_frozen_buys_is_refused(offerings) -> None:
    async def roster(configuration, period):
        return CompletePackageRoster(("pkg_1",))

    configuration = _exact()
    store = InMemoryReportingLedgerStore(clock=lambda: ONE_CLOSED)
    await store.put_configuration(configuration)
    rows = _package_rows(("mb_1", "pkg_1"), ("mb_other", "pkg_1"))
    with pytest.raises(ReportingRowCoverageError) as refused:
        await _producer(
            store, _Fetch(rows), now=ONE_CLOSED, offerings=offerings, roster=roster
        ).run_configuration(configuration, now=ONE_CLOSED)
    assert refused.value.media_buy_ids == ("mb_other",) and refused.value.package_ids == ()
    assert (await _ledger(store, configuration))[1] == []


async def test_a_refused_day_does_not_block_later_days_and_is_not_refetched() -> None:
    """One bad, immutable day must not stall the feed behind it.

    The turn publishes the later day, then re-raises the refusal. The bad
    slice is blocked, so the next turn neither re-reads nor re-raises it.
    """

    async def roster(configuration, period):
        packages = () if period.start == FIRST_START else ("pkg_1",)
        return CompletePackageRoster(packages)

    configuration = _exact()
    store = InMemoryReportingLedgerStore(clock=lambda: TWO_CLOSED)
    await store.put_configuration(configuration)
    fetch = _Fetch(_package_rows(("mb_1", "pkg_1")))
    producer = _producer(store, fetch, now=TWO_CLOSED, roster=roster)
    with pytest.raises(ReportingRowCoverageError) as refused:
        await producer.run_configuration(configuration, now=TWO_CLOSED)

    bad, good = await store.list_generation_obligations(configuration.generation_key)
    assert refused.value.reporting_obligation_id == bad.reporting_obligation_id
    _, revisions = await _ledger(store, configuration)
    assert [revision.reporting_obligation_id for revision, _ in revisions] == [
        good.reporting_obligation_id
    ]
    assert fetch.periods == [bad.period.start, good.period.start]

    turn = await producer.run_configuration(configuration, now=TWO_CLOSED)
    assert bad.period.start not in fetch.periods[2:]
    assert turn.revisions_committed == []


async def test_a_refused_day_on_a_progress_store_is_retired_and_later_days_publish() -> None:
    """The durable producer-progress path parks the refused day like other ledger conflicts."""

    class ProgressStore(InMemoryReportingLedgerStore):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.closed_through: datetime | None = None
            self.finished: list[str] = []

        async def producer_closed_through(self, configuration):
            return self.closed_through

        async def commit_producer_period(self, configuration, obligation, *, previous_end):
            self.closed_through = obligation.period.end
            return await self.commit_obligation(obligation)

        async def producer_constituents(self, configuration, obligation):
            return None

        async def next_producer_obligations(self, configuration, *, now, limit):
            return tuple(
                item.reporting_obligation_id
                for item in await self.list_generation_obligations(configuration.generation_key)
                if item.reporting_obligation_id not in self.finished
            )

        async def finish_producer_acquisition(self, configuration, *, reporting_obligation_id):
            self.finished.append(reporting_obligation_id)

    async def roster(configuration, period):
        return CompletePackageRoster(() if period.start == FIRST_START else ("pkg_1",))

    configuration = _exact()
    store = ProgressStore(clock=lambda: TWO_CLOSED)
    await store.put_configuration(configuration)
    fetch = _Fetch(_package_rows(("mb_1", "pkg_1")))
    with pytest.raises(ReportingRowCoverageError):
        await _producer(store, fetch, now=TWO_CLOSED, roster=roster).run_configuration(
            configuration, now=TWO_CLOSED
        )
    bad, good = await store.list_generation_obligations(configuration.generation_key)
    assert bad.reporting_obligation_id in store.finished
    _, revisions = await _ledger(store, configuration)
    assert [revision.reporting_obligation_id for revision, _ in revisions] == [
        good.reporting_obligation_id
    ]


async def test_allow_partial_refusals_now_name_what_is_outside() -> None:
    """The ``allow_partial`` covered-subset check keeps its code and gains the IDs."""

    async def scope(configuration, period):
        return CompleteMediaBuyScope(("mb_a", "mb_b"))

    async def roster(configuration, period):
        return PerMediaBuyPackageRoster(
            {"mb_a": CompletePackageRoster(("pkg_a",)), "mb_b": MissingHistoricalPackageRoster()}
        )

    configuration = _all_scope(coverage_requirement="allow_partial")
    store = InMemoryReportingLedgerStore(clock=lambda: ONE_CLOSED)
    await store.put_configuration(configuration)
    rows = lambda request: [  # noqa: E731
        {"media_buy_id": "mb_a", "package_id": "pkg_a", "impressions": 1},
        {"media_buy_id": "mb_b", "package_id": "pkg_b", "impressions": 1},
        {"impressions": 1},
    ]
    with pytest.raises(ReportingRowCoverageError) as refused:
        await _producer(
            store, _Fetch(rows), now=ONE_CLOSED, roster=roster, scope=scope
        ).run_configuration(configuration, now=ONE_CLOSED)
    error = refused.value
    assert error.code == "ROW_COVERAGE_MISMATCH"
    assert (error.media_buy_ids, error.package_ids, error.unattributed_rows) == (
        ("mb_b",),
        ("pkg_b",),
        1,
    )


def test_the_publish_rule_reads_the_frozen_wire_coverage() -> None:
    configuration = _exact(media_buy_ids=("mb_1", "mb_2"))
    full = _obligation(configuration, package_ids=("pkg_1",))
    _require_rows_within_frozen_coverage(
        full,
        [
            {"media_buy_id": "mb_1", "package_id": "pkg_1"},
            {"media_buy_id": "mb_2"},
            {"impressions": 3},
        ],
        package_grain=True,
    )
    # Freezing packages makes the obligation package grain on its own.
    with pytest.raises(ReportingRowCoverageError) as refused:
        _require_rows_within_frozen_coverage(
            full, [{"media_buy_id": 7, "package_id": "pkg_9"}], package_grain=False
        )
    assert (refused.value.media_buy_ids, refused.value.package_ids) == (("7",), ("pkg_9",))
    # Status-only unknown coverage covers no package.
    unknown = replace(full, coverage_status="unknown")
    with pytest.raises(ReportingRowCoverageError):
        _require_rows_within_frozen_coverage(
            unknown, [{"media_buy_id": "mb_1", "package_id": "pkg_1"}], package_grain=True
        )
    _require_rows_within_frozen_coverage(unknown, [{"media_buy_id": "mb_1"}], package_grain=True)


# -- the freeze rule ----------------------------------------------------------------


def test_the_freeze_rule_admits_every_legitimate_package_scope() -> None:
    complete_empty = CompletePackageRoster(())
    assert _package_scope_conflict(("mb_1",), complete_empty, package_grain=True) is None
    assert (
        _package_scope_conflict(
            ("mb_1", "mb_2"),
            PerMediaBuyPackageRoster(
                {"mb_1": complete_empty, "mb_2": CompletePackageRoster(("pkg_2",))}
            ),
            package_grain=True,
        )
        is None
    )
    assert (
        _package_scope_conflict(("mb_1",), MissingHistoricalPackageRoster(), package_grain=True)
        is None
    )
    assert (
        _package_scope_conflict(
            ("mb_1",), UnsupportedMediaBuyRoster("provider_limitation"), package_grain=True
        )
        is None
    )
    assert _package_scope_conflict(("mb_1",), None, package_grain=False) is None
    assert _package_scope_conflict((), None, package_grain=True) is None
    assert _package_scope_conflict((), complete_empty, package_grain=True) is None


def test_the_freeze_rule_refuses_unproven_or_orphaned_package_scope() -> None:
    assert "roster evidence" in str(_package_scope_conflict(("mb_1",), None, package_grain=True))
    for roster in (
        CompletePackageRoster(("pkg_1",)),
        UnsupportedMediaBuyRoster("provider_limitation", ("pkg_1",)),
    ):
        assert "zero-buy" in str(_package_scope_conflict((), roster, package_grain=True))


async def test_the_producer_keeps_a_period_with_unproven_package_scope_open(monkeypatch) -> None:
    """Were a package-grain period ever to reach freezing without roster evidence.

    The producer always consults the roster at package grain today; this pins
    what happens if that invariant regresses: nothing is frozen.
    """
    store = InMemoryReportingLedgerStore()
    configuration = _exact()
    await store.put_configuration(configuration)

    async def roster(configuration, period):
        return CompletePackageRoster(("pkg_1",))

    monkeypatch.setattr(
        producer_module,
        "_package_scope_conflict",
        lambda buys, roster, *, package_grain: _package_scope_conflict(
            buys, None, package_grain=package_grain
        ),
    )
    producer = ReportingProducer(
        source=_NoSource(), offerings=PACKAGE, store=store, package_roster_resolver=roster
    )
    assert await producer.close_elapsed_periods(configuration, now=ONE_CLOSED) == []
    assert await store.list_generation_obligations(configuration.generation_key) == ()


async def _subscription(media_buy_ids: tuple[str, ...]):
    world = await _world_with(AGENCY, [buy("mb-1", "2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z")])
    (owed,) = await world.engine.frozen_owed_sets(AGENCY, now=at("2026-10-03T00:00:00Z"))
    return replace(owed, periods=(replace(owed.periods[0], media_buy_ids=media_buy_ids),))


async def test_a_subscription_zero_buy_period_with_packages_is_not_frozen() -> None:
    owed = await _subscription(())
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-03T00:00:00Z"))

    async def roster(configuration, boundary):
        return CompletePackageRoster(("pkg-orphan",))

    planner = SubscriptionObligationPlanner(
        store, offerings=OFFERINGS, currency="USD", package_roster_resolver=roster
    )
    with pytest.raises(ReportingPackageScopeError) as refused:
        await planner.plan(owed)
    assert refused.value.code == "PACKAGE_SCOPE_INCONSISTENT"
    (configuration,) = await store.list_configurations(
        caller=ReportingStatusCaller("acct-1", AGENCY.subject)
    )
    assert await store.list_generation_obligations(configuration.generation_key) == ()
    assert await store.owed_set_through(configuration.generation_key) is None


async def test_a_subscription_period_with_a_proven_empty_package_set_freezes_full() -> None:
    owed = await _subscription(("mb-1",))
    store = InMemoryReportingLedgerStore(clock=lambda: at("2026-10-03T00:00:00Z"))

    async def roster(configuration, boundary):
        return CompletePackageRoster(())

    planner = SubscriptionObligationPlanner(
        store, offerings=OFFERINGS, currency="USD", package_roster_resolver=roster
    )
    result = await planner.plan(owed)
    (obligation,) = await store.list_generation_obligations(result.generations[0])
    assert (obligation.media_buy_ids, obligation.package_ids) == (("mb-1",), ())
    assert obligation.coverage_status == "full"
