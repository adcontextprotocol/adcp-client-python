"""Table tests for the reporting-subscription coverage rule (AdCP 3.3).

Every expected period below is written out by hand from the rule in
``core/reporting-subscription.json`` (coverage, cutover) and
``core/reporting-subscription-coverage.json``. The seller runtime owes exactly
what :func:`expected_subscription_periods` returns, so these tables are the
billing contract: change one only with a spec citation.

Unless a test says otherwise the account is in ``America/New_York`` in
October 2026 (EDT, UTC-4), so a local day runs from 04:00Z to 04:00Z.
"""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta, timezone

import pytest

from adcp.reporting import ExpectedReportingPeriod
from adcp.reporting.subscriptions import (
    AccountTimezoneChange,
    ExpectedSubscriptionPeriod,
    SubscriptionAuthorization,
    SubscriptionBuy,
    SubscriptionBuyFlight,
    SubscriptionBuyPackage,
    SubscriptionDestination,
    SubscriptionException,
    SubscriptionOffering,
    SubscriptionVersionWindow,
    expected_subscription_periods,
    resolve_subscription_offerings,
    subscription_exceptions,
    subscription_version_windows,
)
from adcp.types.domains.core.reporting_subscription_coverage import (
    ReportingSubscriptionCoverage,
)
from adcp.types.domains.core.reporting_subscription_state import ReportingSubscriptionState

NY = "America/New_York"
UTC = timezone.utc


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def offering(
    offering_id: str,
    *,
    dimensions: tuple[str, ...] = ("package",),
    unmapped: tuple[str, ...] = (),
    duration: str = "P1D",
    alignment: str = "account_timezone",
    sla: str = "PT6H",
    feed_purpose: str = "analytics",
    finality: tuple[str, ...] = ("official",),
    reconciliation_mode: str = "delivery_only",
    method_pattern: str | None = None,
    method_transport: str | None = None,
    period_timezone: str | None = None,
    period_anchor: datetime | None = None,
    period_timezone_policy: str | None = None,
    available_from: datetime | None = None,
    withdrawn_at: datetime | None = None,
) -> SubscriptionOffering:
    return SubscriptionOffering(
        offering_id=offering_id,
        report_definition_id=f"def-{offering_id}",
        reporting_profile="profile-1",
        feed_purpose=feed_purpose,
        period_duration=duration,
        alignment=alignment,
        delivery_sla=sla,
        supported_finality=frozenset(finality),
        reconciliation_mode=reconciliation_mode,
        dimensions=frozenset(dimensions),
        unmapped_dimensions=frozenset(unmapped),
        method_pattern=method_pattern,
        method_transport=method_transport,
        period_timezone=period_timezone,
        period_anchor=period_anchor,
        period_timezone_policy=period_timezone_policy,
        available_from=available_from,
        withdrawn_at=withdrawn_at,
    )


DAILY = offering("daily-pkg")
HOURLY_UTC = offering("hourly-pkg", duration="PT1H", alignment="utc", sla="PT2H")


def subscription_config(
    *,
    version: int = 1,
    duration: str = "P1D",
    dimensions: list[str] | None = None,
    match: str | None = None,
    scope: dict | None = None,
    alignment: str | None = None,
    exclude: list[str] | None = None,
    operator_domains: list[str] | None = None,
    destination: dict | None = None,
    period_timezone: str | None = None,
) -> dict:
    selector: dict = {
        "feed_purpose": "analytics",
        "period_duration": duration,
        "required_finality": "official",
    }
    if dimensions is not None:
        selector["dimensions"] = dimensions
    if match is not None:
        selector["match"] = match
    if alignment is not None:
        selector["alignment"] = alignment
    if period_timezone is not None:
        selector["period_timezone"] = period_timezone
    config: dict = {
        "subscription_id": "analytics-daily",
        "subscription_version": version,
        "active": True,
        "scope": scope or {"all_authorized_accounts": True},
        "selector": selector,
        "reconciliation_mode": "delivery_only",
    }
    if exclude:
        config["exclude_account_ids"] = exclude
    if operator_domains:
        config["operator_domains"] = operator_domains
    if destination:
        config["destination"] = destination
    return config


def state(
    accepted_at: str,
    *,
    config: dict | None = None,
    version: int = 1,
    prior: list[tuple[int, str, str]] = (),
    deactivated_at: str | None = None,
) -> dict:
    payload: dict = {
        "subscription_id": "analytics-daily",
        "subscription_version": version,
        "state": "inactive" if deactivated_at else "active",
        "accepted_at": accepted_at,
        "configuration": config or subscription_config(version=version),
    }
    if prior:
        payload["prior_versions"] = [
            {"subscription_version": v, "accepted_at": a, "superseded_at": s} for v, a, s in prior
        ]
    if deactivated_at:
        payload["deactivated_at"] = deactivated_at
    return payload


def buy(
    media_buy_id: str = "mb-1",
    *,
    confirmed_at: str = "2026-10-01T00:00:00Z",
    start_time: str = "2026-10-01T04:00:00Z",
    end_time: str = "2026-10-31T04:00:00Z",
    canceled_at: str | None = None,
    completed_at: str | None = None,
    offering_ids: tuple[str, ...] = ("daily-pkg",),
    packages: tuple[SubscriptionBuyPackage, ...] = (),
    superseded: tuple[SubscriptionBuyFlight, ...] = (),
) -> SubscriptionBuy:
    return SubscriptionBuy(
        media_buy_id=media_buy_id,
        confirmed_at=at(confirmed_at) if confirmed_at else None,
        start_time=at(start_time),
        end_time=at(end_time),
        canceled_at=at(canceled_at) if canceled_at else None,
        completed_at=at(completed_at) if completed_at else None,
        reporting_delivery_offering_ids=offering_ids,
        packages=packages,
        superseded_flights=superseded,
    )


def periods(**kwargs) -> list[tuple[str, str]]:
    return [(item.period_start, item.period_end) for item in run(**kwargs)]


def starts(**kwargs) -> list[str]:
    return [item.period_start for item in run(**kwargs)]


def run(**kwargs) -> list[ExpectedSubscriptionPeriod]:
    kwargs.setdefault("subscription", state("2026-10-01T00:00:00Z"))
    kwargs.setdefault("account_id", "acct-1")
    kwargs.setdefault("offerings", [DAILY, HOURLY_UTC])
    kwargs.setdefault("account_timezone", NY)
    kwargs.setdefault("now", at("2026-12-31T00:00:00Z"))
    return expected_subscription_periods(**kwargs)


def days(first: str, count: int) -> list[str]:
    """``count`` consecutive EDT local-midnight starts from ``first`` (YYYY-MM-DD)."""
    day = datetime.fromisoformat(first)
    return [(day + timedelta(days=index)).strftime("%Y-%m-%dT04:00:00Z") for index in range(count)]


# -- the start rule ------------------------------------------------------------


class TestCoverageStart:
    def test_whole_flight_after_acceptance(self) -> None:
        result = run(buys=[buy(end_time="2026-10-04T04:00:00Z")])
        assert [(p.period_start, p.period_end) for p in result] == [
            ("2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z"),
            ("2026-10-02T04:00:00Z", "2026-10-03T04:00:00Z"),
            ("2026-10-03T04:00:00Z", "2026-10-04T04:00:00Z"),
        ]
        first = result[0]
        assert isinstance(first, ExpectedReportingPeriod)
        assert first.delivery_config_id == "analytics-daily"
        assert first.delivery_config_version == 1
        assert first.offering_id == "daily-pkg"
        assert first.report_definition_id == "def-daily-pkg"
        assert first.source_timezone == NY
        assert first.media_buy_ids == ("mb-1",)
        assert first.expected_at == "2026-10-02T10:00:00Z"
        assert first.withheld is False

    def test_mid_period_confirmation_starts_at_next_boundary(self) -> None:
        # Confirmed 11:00 local on Oct 5 with a flight that began Oct 3: the
        # Oct 5 local day started before confirmation and is never owed.
        result = starts(
            buys=[
                buy(
                    confirmed_at="2026-10-05T15:00:00Z",
                    start_time="2026-10-03T04:00:00Z",
                    end_time="2026-10-08T04:00:00Z",
                )
            ]
        )
        assert result == days("2026-10-06", 2)

    def test_confirmation_on_a_boundary_owes_that_period(self) -> None:
        result = starts(
            buys=[
                buy(
                    confirmed_at="2026-10-05T04:00:00Z",
                    start_time="2026-10-03T04:00:00Z",
                    end_time="2026-10-07T04:00:00Z",
                )
            ]
        )
        assert result == days("2026-10-05", 2)

    def test_mid_period_acceptance_starts_at_next_boundary(self) -> None:
        result = starts(
            subscription=state("2026-10-02T12:00:00Z"),
            buys=[buy(end_time="2026-10-05T04:00:00Z")],
        )
        assert result == days("2026-10-03", 2)

    def test_authorization_later_than_acceptance_and_confirmation(self) -> None:
        result = starts(
            buys=[buy(end_time="2026-10-06T04:00:00Z")],
            authorizations=[SubscriptionAuthorization(at("2026-10-03T09:00:00Z"))],
        )
        assert result == days("2026-10-04", 2)

    def test_confirmed_early_with_a_later_flight_owes_nothing_before_the_flight(self) -> None:
        # A buy confirmed Oct 1 with an Oct 20 start owes nothing before Oct 20.
        result = starts(
            buys=[
                buy(
                    confirmed_at="2026-10-01T00:00:00Z",
                    start_time="2026-10-20T04:00:00Z",
                    end_time="2026-10-22T04:00:00Z",
                )
            ]
        )
        assert result == days("2026-10-20", 2)

    def test_mid_period_flight_start_owes_the_whole_containing_period(self) -> None:
        # No clipping: a flight starting at 15:00Z overlaps the Oct 10 local day.
        result = periods(
            buys=[buy(start_time="2026-10-10T15:00:00Z", end_time="2026-10-11T15:00:00Z")]
        )
        assert result == [
            ("2026-10-10T04:00:00Z", "2026-10-11T04:00:00Z"),
            ("2026-10-11T04:00:00Z", "2026-10-12T04:00:00Z"),
        ]

    def test_unconfirmed_buy_is_not_covered(self) -> None:
        unconfirmed = SubscriptionBuy(
            media_buy_id="mb-1",
            confirmed_at=None,
            start_time=at("2026-10-01T04:00:00Z"),
            end_time=at("2026-10-03T04:00:00Z"),
            reporting_delivery_offering_ids=("daily-pkg",),
        )
        assert run(buys=[unconfirmed]) == []

    def test_periods_are_owed_only_once_resolved(self) -> None:
        now = at("2026-10-03T10:00:00Z")
        assert starts(buys=[buy()], now=now) == days("2026-10-01", 2)
        unresolved = run(
            buys=[buy(end_time="2026-10-05T04:00:00Z")], now=now, include_unresolved=True
        )
        assert [p.period_start for p in unresolved] == days("2026-10-01", 4)


# -- the stop rule -------------------------------------------------------------


class TestCoverageStop:
    def test_end_time_on_a_boundary(self) -> None:
        assert starts(buys=[buy(end_time="2026-10-03T04:00:00Z")]) == days("2026-10-01", 2)

    def test_end_time_mid_period_owes_that_period_in_full(self) -> None:
        result = periods(buys=[buy(end_time="2026-10-03T10:00:00Z")])
        assert result[-1] == ("2026-10-03T04:00:00Z", "2026-10-04T04:00:00Z")
        assert len(result) == 3

    def test_cancellation_mid_flight(self) -> None:
        # A buy canceled mid-period owes that period and none after it.
        result = starts(
            buys=[buy(canceled_at="2026-10-04T17:30:00Z", end_time="2026-10-31T04:00:00Z")]
        )
        assert result == days("2026-10-01", 4)

    def test_cancellation_on_a_boundary_owes_nothing_from_it(self) -> None:
        result = starts(buys=[buy(canceled_at="2026-10-04T04:00:00Z")])
        assert result == days("2026-10-01", 3)

    def test_early_completion(self) -> None:
        result = starts(buys=[buy(completed_at="2026-10-02T23:00:00Z")])
        assert result == days("2026-10-01", 2)

    def test_earliest_of_end_cancel_and_complete_wins(self) -> None:
        result = starts(
            buys=[
                buy(
                    end_time="2026-10-06T04:00:00Z",
                    canceled_at="2026-10-05T12:00:00Z",
                    completed_at="2026-10-03T12:00:00Z",
                )
            ]
        )
        assert result == days("2026-10-01", 3)

    def test_canceled_before_start_owes_nothing(self) -> None:
        result = run(
            buys=[
                buy(
                    start_time="2026-10-10T04:00:00Z",
                    canceled_at="2026-10-05T00:00:00Z",
                )
            ]
        )
        assert result == []


class TestFlightAsOfResolution:
    def test_extension_adds_only_unresolved_periods(self) -> None:
        # Flight ended Oct 4; on Oct 7 at 12:00Z it was extended to Oct 10.
        # Periods that resolved before the extension (Oct 4..6 local days)
        # are never owed retroactively; the Oct 7 day resolves after it.
        extended = buy(
            end_time="2026-10-10T04:00:00Z",
            superseded=(
                SubscriptionBuyFlight(
                    superseded_at=at("2026-10-07T12:00:00Z"),
                    start_time=at("2026-10-01T04:00:00Z"),
                    end_time=at("2026-10-04T04:00:00Z"),
                ),
            ),
        )
        assert starts(buys=[extended]) == [*days("2026-10-01", 3), *days("2026-10-07", 3)]

    def test_extension_effective_exactly_at_resolution_is_visible(self) -> None:
        extended = buy(
            end_time="2026-10-06T04:00:00Z",
            superseded=(
                SubscriptionBuyFlight(
                    superseded_at=at("2026-10-05T04:00:00Z"),
                    start_time=at("2026-10-01T04:00:00Z"),
                    end_time=at("2026-10-04T04:00:00Z"),
                ),
            ),
        )
        # Oct 4 local day resolves at Oct 5 04:00Z, the instant of the change.
        assert starts(buys=[extended]) == days("2026-10-01", 5)

    def test_reinstatement_never_revives_elapsed_periods(self) -> None:
        reinstated = buy(
            end_time="2026-10-08T04:00:00Z",
            superseded=(
                SubscriptionBuyFlight(
                    superseded_at=at("2026-10-06T00:00:00Z"),
                    start_time=at("2026-10-01T04:00:00Z"),
                    end_time=at("2026-10-08T04:00:00Z"),
                    canceled_at=at("2026-10-03T00:00:00Z"),
                ),
            ),
        )
        # Canceled during the Oct 2 local day; reinstated Oct 6 00:00Z (Oct 5
        # local). The Oct 5 day resolves after the reinstatement.
        assert starts(buys=[reinstated]) == [*days("2026-10-01", 2), *days("2026-10-05", 3)]

    def test_shortened_flight_keeps_already_resolved_periods(self) -> None:
        shortened = buy(
            end_time="2026-10-03T04:00:00Z",
            superseded=(
                SubscriptionBuyFlight(
                    superseded_at=at("2026-10-06T00:00:00Z"),
                    start_time=at("2026-10-01T04:00:00Z"),
                    end_time=at("2026-10-31T04:00:00Z"),
                ),
            ),
        )
        # Days through Oct 4 resolved before the change at Oct 6 00:00Z.
        assert starts(buys=[shortened]) == days("2026-10-01", 4)


# -- versions and cutovers -----------------------------------------------------


class TestVersionCutovers:
    def test_new_version_same_schedule_is_seamless(self) -> None:
        subscription = state(
            "2026-10-03T12:00:00Z",
            version=2,
            config=subscription_config(version=2),
            prior=[(1, "2026-10-01T00:00:00Z", "2026-10-03T12:00:00Z")],
        )
        result = run(subscription=subscription, buys=[buy(end_time="2026-10-06T04:00:00Z")])
        assert [(p.period_start, p.delivery_config_version) for p in result] == [
            ("2026-10-01T04:00:00Z", 1),
            ("2026-10-02T04:00:00Z", 1),
            ("2026-10-03T04:00:00Z", 1),  # started before T: old version, in full
            ("2026-10-04T04:00:00Z", 2),  # E = 2026-10-04T04:00Z
            ("2026-10-05T04:00:00Z", 2),
        ]

    def test_new_version_on_a_boundary(self) -> None:
        subscription = state(
            "2026-10-03T04:00:00Z",
            version=2,
            config=subscription_config(version=2),
            prior=[(1, "2026-10-01T00:00:00Z", "2026-10-03T04:00:00Z")],
        )
        result = run(subscription=subscription, buys=[buy(end_time="2026-10-05T04:00:00Z")])
        assert [(p.period_start, p.delivery_config_version) for p in result] == [
            ("2026-10-01T04:00:00Z", 1),
            ("2026-10-02T04:00:00Z", 1),
            ("2026-10-03T04:00:00Z", 2),
            ("2026-10-04T04:00:00Z", 2),
        ]

    def test_daily_to_hourly_cutover_with_misaligned_boundaries(self) -> None:
        # v1 resolves the local-day offering, v2 the UTC-hourly one. T is
        # 2026-10-02T09:30Z: v1 owes the Oct 2 local day in full, ending at
        # E = 2026-10-03T04:00Z; v2 owes hours starting at or after E.
        subscription = state(
            "2026-10-02T09:30:00Z",
            version=2,
            config=subscription_config(version=2, duration="PT1H"),
            prior=[(1, "2026-10-01T00:00:00Z", "2026-10-02T09:30:00Z")],
        )
        result = run(
            subscription=subscription,
            version_configurations={1: subscription_config(version=1)},
            buys=[
                buy(
                    end_time="2026-10-03T07:00:00Z",
                    offering_ids=("daily-pkg", "hourly-pkg"),
                )
            ],
        )
        assert [(p.period_start, p.period_end, p.offering_id) for p in result] == [
            ("2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z", "daily-pkg"),
            ("2026-10-02T04:00:00Z", "2026-10-03T04:00:00Z", "daily-pkg"),
            ("2026-10-03T04:00:00Z", "2026-10-03T05:00:00Z", "hourly-pkg"),
            ("2026-10-03T05:00:00Z", "2026-10-03T06:00:00Z", "hourly-pkg"),
            ("2026-10-03T06:00:00Z", "2026-10-03T07:00:00Z", "hourly-pkg"),
        ]

    def test_hourly_to_daily_cutover_leaves_an_unowed_stretch(self) -> None:
        # T = 10:30Z: v1 owes the 10:00-11:00 hour in full (E = 11:00Z). The
        # first local day starting at or after E begins at 04:00Z next day;
        # 11:00Z..04:00Z is owed under neither schedule.
        subscription = state(
            "2026-10-02T10:30:00Z",
            version=2,
            config=subscription_config(version=2),
            prior=[(1, "2026-10-02T08:00:00Z", "2026-10-02T10:30:00Z")],
        )
        result = run(
            subscription=subscription,
            version_configurations={1: subscription_config(version=1, duration="PT1H")},
            buys=[
                buy(
                    end_time="2026-10-04T04:00:00Z",
                    offering_ids=("daily-pkg", "hourly-pkg"),
                )
            ],
        )
        assert [(p.period_start, p.period_end, p.delivery_config_version) for p in result] == [
            ("2026-10-02T08:00:00Z", "2026-10-02T09:00:00Z", 1),
            ("2026-10-02T09:00:00Z", "2026-10-02T10:00:00Z", 1),
            ("2026-10-02T10:00:00Z", "2026-10-02T11:00:00Z", 1),
            ("2026-10-03T04:00:00Z", "2026-10-04T04:00:00Z", 2),
        ]

    def test_account_added_by_a_new_version_follows_the_ordinary_start(self) -> None:
        subscription = state(
            "2026-10-03T12:00:00Z",
            version=2,
            config=subscription_config(version=2, scope={"account_ids": ["acct-1", "acct-2"]}),
            prior=[(1, "2026-10-01T00:00:00Z", "2026-10-03T12:00:00Z")],
        )
        result = run(
            subscription=subscription,
            version_configurations={1: subscription_config(scope={"account_ids": ["acct-2"]})},
            buys=[buy(end_time="2026-10-06T04:00:00Z")],
        )
        assert [(p.period_start, p.delivery_config_version) for p in result] == [
            ("2026-10-04T04:00:00Z", 2),
            ("2026-10-05T04:00:00Z", 2),
        ]

    def test_account_dropped_by_a_new_version_keeps_started_periods(self) -> None:
        subscription = state(
            "2026-10-03T12:00:00Z",
            version=2,
            config=subscription_config(version=2, scope={"account_ids": ["acct-2"]}),
            prior=[(1, "2026-10-01T00:00:00Z", "2026-10-03T12:00:00Z")],
        )
        result = run(
            subscription=subscription,
            version_configurations={1: subscription_config()},
            buys=[buy(end_time="2026-10-06T04:00:00Z")],
        )
        assert [p.period_start for p in result] == days("2026-10-01", 3)

    def test_deactivation_keeps_started_periods_and_creates_no_later_ones(self) -> None:
        subscription = state("2026-10-01T00:00:00Z", deactivated_at="2026-10-03T12:00:00Z")
        assert starts(subscription=subscription, buys=[buy()]) == days("2026-10-01", 3)

    def test_deactivation_on_a_boundary(self) -> None:
        subscription = state("2026-10-01T00:00:00Z", deactivated_at="2026-10-03T04:00:00Z")
        assert starts(subscription=subscription, buys=[buy()]) == days("2026-10-01", 2)

    def test_three_versions_back_to_back(self) -> None:
        subscription = state(
            "2026-10-02T06:00:00Z",
            version=3,
            config=subscription_config(version=3),
            prior=[
                (2, "2026-10-02T05:00:00Z", "2026-10-02T06:00:00Z"),
                (1, "2026-10-01T00:00:00Z", "2026-10-02T05:00:00Z"),
            ],
        )
        result = run(subscription=subscription, buys=[buy(end_time="2026-10-05T04:00:00Z")])
        # v2 is in effect for one hour inside the Oct 2 day, which v1 already
        # owes in full; the next boundary belongs to v3.
        assert [(p.period_start, p.delivery_config_version) for p in result] == [
            ("2026-10-01T04:00:00Z", 1),
            ("2026-10-02T04:00:00Z", 1),
            ("2026-10-03T04:00:00Z", 3),
            ("2026-10-04T04:00:00Z", 3),
        ]

    def test_version_windows_from_a_generated_state_model(self) -> None:
        model = ReportingSubscriptionState.model_validate(
            state(
                "2026-10-03T12:00:00Z",
                version=2,
                config=subscription_config(version=2),
                prior=[(1, "2026-10-01T00:00:00Z", "2026-10-03T12:00:00Z")],
            )
        )
        windows = subscription_version_windows(model)
        assert [(w.subscription_version, w.accepted_at, w.ends_at) for w in windows] == [
            (1, at("2026-10-01T00:00:00Z"), at("2026-10-03T12:00:00Z")),
            (2, at("2026-10-03T12:00:00Z"), None),
        ]
        from_model = run(subscription=model, buys=[buy(end_time="2026-10-06T04:00:00Z")])
        from_windows = run(subscription=windows, buys=[buy(end_time="2026-10-06T04:00:00Z")])
        assert from_model == from_windows

    def test_out_of_order_versions_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            subscription_version_windows(
                state(
                    "2026-10-03T12:00:00Z",
                    version=2,
                    prior=[(1, "2026-10-01T00:00:00Z", "2026-10-04T00:00:00Z")],
                )
            )


# -- resolution ----------------------------------------------------------------


PACKAGE = offering("off-b-package", dimensions=("package",))
PACKAGE_CREATIVE = offering("off-a-pkg-creative", dimensions=("package", "creative"))
PACKAGE_COUNTRY = offering("off-c-pkg-country", dimensions=("package", "country"))
PACKAGE_DEVICE = offering("off-d-pkg-device", dimensions=("package",), unmapped=("device",))
BREAKDOWNS = [PACKAGE, PACKAGE_CREATIVE, PACKAGE_COUNTRY, PACKAGE_DEVICE]
ALL_IDS = tuple(item.offering_id for item in BREAKDOWNS)


def resolved(config: dict, *, product: tuple[str, ...] = ALL_IDS, **kwargs) -> tuple[str, ...]:
    return resolve_subscription_offerings(
        config, kwargs.pop("offerings", BREAKDOWNS), product_offering_ids=product, **kwargs
    ).offering_ids


class TestResolution:
    def test_exact_is_the_default_with_dimensions(self) -> None:
        assert resolved(subscription_config(dimensions=["package"])) == ("off-b-package",)

    def test_unmapped_native_dimensions_count_as_distinct(self) -> None:
        # package x device does not equal package.
        only_device = (PACKAGE_DEVICE.offering_id,)
        assert resolved(subscription_config(dimensions=["package"]), product=only_device) == ()

    def test_finest_is_the_default_without_dimensions_and_ties_go_to_lowest_id(self) -> None:
        # Two offerings have two mapped dimensions; byte order picks "off-a...".
        assert resolved(subscription_config()) == ("off-a-pkg-creative",)

    def test_finest_with_dimensions(self) -> None:
        config = subscription_config(dimensions=["package"], match="finest")
        assert resolved(config) == ("off-a-pkg-creative",)
        config = subscription_config(dimensions=["country"], match="finest")
        assert resolved(config) == ("off-c-pkg-country",)

    def test_all_selects_every_including_offering_in_byte_order(self) -> None:
        config = subscription_config(dimensions=["package"], match="all")
        assert resolved(config) == (
            "off-a-pkg-creative",
            "off-b-package",
            "off-c-pkg-country",
            "off-d-pkg-device",
        )

    def test_exact_ties_go_to_the_lowest_offering_id_in_byte_order(self) -> None:
        upper = offering("Z-package")
        lower = offering("a-package")
        result = resolved(
            subscription_config(dimensions=["package"]),
            offerings=[lower, upper],
            product=("a-package", "Z-package"),
        )
        assert result == ("Z-package",)  # b"Z" < b"a"

    def test_only_offerings_the_product_lists_are_candidates(self) -> None:
        config = subscription_config(dimensions=["package"], match="all")
        assert resolved(config, product=("off-b-package",)) == ("off-b-package",)

    def test_unknown_vocabulary_value_matches_nothing(self) -> None:
        assert resolved(subscription_config(dimensions=["dma_zone"])) == ()

    def test_period_duration_finality_purpose_and_mode_must_match(self) -> None:
        assert resolved(subscription_config(duration="PT1H")) == ()
        snapshot_only = offering("snap", finality=("snapshot",))
        assert resolved(subscription_config(), offerings=[snapshot_only], product=("snap",)) == ()
        billing = offering("bill", feed_purpose="billing")
        assert resolved(subscription_config(), offerings=[billing], product=("bill",)) == ()
        receipt = offering("rcpt", reconciliation_mode="consumer_receipt")
        assert resolved(subscription_config(), offerings=[receipt], product=("rcpt",)) == ()

    def test_alignment_filter(self) -> None:
        utc_daily = offering("utc-daily", alignment="utc")
        both = [DAILY, utc_daily]
        product = ("daily-pkg", "utc-daily")
        config = subscription_config(dimensions=["package"], alignment="utc")
        assert resolved(config, offerings=both, product=product) == ("utc-daily",)

    def test_destination_selects_managed_offerings_only(self) -> None:
        managed = offering("managed", method_pattern="file_transfer", method_transport="s3")
        both = [DAILY, managed]
        product = ("daily-pkg", "managed")
        core = subscription_config(dimensions=["package"])
        assert resolved(core, offerings=both, product=product) == ("daily-pkg",)
        config = subscription_config(
            dimensions=["package"], destination={"destination_id": "warehouse"}
        )
        s3 = SubscriptionDestination(pattern="file_transfer", transport="s3")
        gcs = SubscriptionDestination(pattern="file_transfer", transport="gcs")
        assert resolved(config, offerings=both, product=product, destination=s3) == ("managed",)
        assert resolved(config, offerings=both, product=product, destination=gcs) == ()
        with pytest.raises(ValueError):
            resolved(config, offerings=both, product=product)

    def test_period_timezone_override(self) -> None:
        # account_timezone and account-resolved schedules can produce periods
        # in any zone; fixed-zone schedules only in theirs; utc only in UTC.
        source = offering(
            "src",
            alignment="source_timezone",
            period_timezone="America/Chicago",
            period_timezone_policy="fixed",
        )
        utc_daily = offering("utc-daily", alignment="utc")
        offers = [DAILY, source, utc_daily]
        product = ("daily-pkg", "src", "utc-daily")

        def override(zone: str) -> dict:
            return subscription_config(
                dimensions=["package"],
                match="all",
                scope={"account_ids": ["acct-1"]},
                period_timezone=zone,
            )

        assert resolved(override("America/Chicago"), offerings=offers, product=product) == (
            "daily-pkg",
            "src",
        )
        assert resolved(override("Etc/UTC"), offerings=offers, product=product) == (
            "daily-pkg",
            "utc-daily",
        )
        # The override moves an account_timezone schedule into its zone.
        result = periods(
            subscription=state("2026-10-01T00:00:00Z", config=override("America/Chicago")),
            offerings=[DAILY],
            buys=[buy(end_time="2026-10-03T05:00:00Z")],
        )
        assert result[0] == ("2026-10-01T05:00:00Z", "2026-10-02T05:00:00Z")

    def test_configurable_billing_cycles_are_never_candidates(self) -> None:
        # A subscription has no way to supply a billing anchor.
        configurable = offering("cycle", alignment="billing_cycle")
        assert resolved(subscription_config(), offerings=[configurable], product=("cycle",)) == ()
        monthly = offering("monthly", duration="P1M")
        config = subscription_config(duration="P1M")
        assert resolved(config, offerings=[monthly], product=("monthly",)) == ("monthly",)

    def test_from_wire_offering(self) -> None:
        wire = {
            "offering_id": "gam-daily",
            "feed_purpose": "analytics",
            "report_definition_id": "gam-delivery-v1",
            "report_definition_uri": "https://seller.example/defs/gam-delivery-v1.json",
            "report_definition_sha256": "0" * 64,
            "reporting_profile": {"id": "delivery", "version": "1"},
            "schedule": {
                "period_duration": "P1D",
                "alignment": "source_timezone",
                "period_timezone_policy": "fixed",
                "period_timezone": "America/Los_Angeles",
                "delivery_sla": "PT12H",
            },
            "supported_finality": ["snapshot", "official"],
            "reconciliation_mode": "delivery_only",
        }
        projected = SubscriptionOffering.from_offering(wire, dimensions=("package", "creative"))
        assert projected.reporting_profile == "delivery"
        assert projected.zone(NY) == "America/Los_Angeles"
        assert projected.schedulable
        start, end = next(
            iter(
                (p.period_start, p.period_end)
                for p in run(
                    offerings=[projected],
                    subscription=state(
                        "2026-10-01T00:00:00Z",
                        config=subscription_config(dimensions=["package", "creative"]),
                    ),
                    buys=[buy(offering_ids=("gam-daily",), end_time="2026-10-03T07:00:00Z")],
                )
            )
        )
        # Local midnight in Los Angeles (PDT, UTC-7).
        assert (start, end) == ("2026-10-01T07:00:00Z", "2026-10-02T07:00:00Z")


class TestResolutionInCoverage:
    def test_all_owes_each_offering_its_own_periods(self) -> None:
        config = subscription_config(dimensions=["package"], match="all")
        result = run(
            subscription=state("2026-10-01T00:00:00Z", config=config),
            offerings=[PACKAGE, PACKAGE_CREATIVE],
            buys=[
                buy(
                    end_time="2026-10-03T04:00:00Z",
                    offering_ids=(PACKAGE.offering_id, PACKAGE_CREATIVE.offering_id),
                )
            ],
        )
        assert [(p.period_start, p.offering_id) for p in result] == [
            ("2026-10-01T04:00:00Z", "off-a-pkg-creative"),
            ("2026-10-01T04:00:00Z", "off-b-package"),
            ("2026-10-02T04:00:00Z", "off-a-pkg-creative"),
            ("2026-10-02T04:00:00Z", "off-b-package"),
        ]

    def test_a_new_finer_offering_takes_over_from_the_next_boundary(self) -> None:
        # Under finest, a finer offering published at 2026-10-03T12:00Z is a
        # resolution change: the old offering keeps the Oct 3 day.
        finer = offering(
            "off-a-pkg-creative",
            dimensions=("package", "creative"),
            available_from=at("2026-10-03T12:00:00Z"),
        )
        config = subscription_config(dimensions=["package"], match="finest")
        result = run(
            subscription=state("2026-10-01T00:00:00Z", config=config),
            offerings=[PACKAGE, finer],
            buys=[
                buy(
                    end_time="2026-10-06T04:00:00Z",
                    offering_ids=(PACKAGE.offering_id, finer.offering_id),
                )
            ],
        )
        assert [(p.period_start, p.offering_id) for p in result] == [
            ("2026-10-01T04:00:00Z", "off-b-package"),
            ("2026-10-02T04:00:00Z", "off-b-package"),
            ("2026-10-03T04:00:00Z", "off-b-package"),
            ("2026-10-04T04:00:00Z", "off-a-pkg-creative"),
            ("2026-10-05T04:00:00Z", "off-a-pkg-creative"),
        ]

    def test_withdrawn_offering_with_no_successor_stops_after_started_periods(self) -> None:
        withdrawn = offering("daily-pkg", withdrawn_at=at("2026-10-03T12:00:00Z"))
        result = starts(offerings=[withdrawn], buys=[buy()])
        assert result == days("2026-10-01", 3)

    def test_packages_resolve_separately_and_union_per_buy(self) -> None:
        config = subscription_config(dimensions=["package"])
        mixed = buy(
            end_time="2026-10-02T04:00:00Z",
            packages=(
                SubscriptionBuyPackage("pkg-1", (PACKAGE.offering_id,)),
                SubscriptionBuyPackage("pkg-2", (PACKAGE_CREATIVE.offering_id,)),
            ),
        )
        result = run(
            subscription=state("2026-10-01T00:00:00Z", config=config),
            offerings=[PACKAGE, PACKAGE_CREATIVE],
            buys=[mixed],
        )
        # pkg-2 has no exact match: the buy stays covered for pkg-1.
        assert [(p.period_start, p.offering_id) for p in result] == [
            ("2026-10-01T04:00:00Z", "off-b-package"),
        ]

    def test_buys_share_one_expected_period_per_offering(self) -> None:
        result = run(
            buys=[
                buy("mb-2", end_time="2026-10-03T04:00:00Z"),
                buy("mb-1", end_time="2026-10-02T04:00:00Z"),
            ]
        )
        assert [(p.period_start, p.media_buy_ids) for p in result] == [
            ("2026-10-01T04:00:00Z", ("mb-1", "mb-2")),
            ("2026-10-02T04:00:00Z", ("mb-2",)),
        ]

    def test_scope_filters(self) -> None:
        excluded = state("2026-10-01T00:00:00Z", config=subscription_config(exclude=["acct-1"]))
        assert run(subscription=excluded, buys=[buy()]) == []
        named = state(
            "2026-10-01T00:00:00Z", config=subscription_config(scope={"account_ids": ["acct-2"]})
        )
        assert run(subscription=named, buys=[buy()]) == []
        operator = state(
            "2026-10-01T00:00:00Z",
            config=subscription_config(operator_domains=["agency.example"]),
        )
        assert run(subscription=operator, buys=[buy()], account_operator="other.example") == []
        assert run(subscription=operator, buys=[buy()], account_operator="agency.example")
        # An account whose operator is unknown is not selected.
        assert run(subscription=operator, buys=[buy()]) == []


# -- exceptions ----------------------------------------------------------------


class TestExceptions:
    def test_account_level_exception_removes_periods_in_from_until(self) -> None:
        result = starts(
            buys=[buy(end_time="2026-10-08T04:00:00Z")],
            exceptions=[
                {
                    "reason": "capacity",
                    "from": "2026-10-03T04:00:00Z",
                    "until": "2026-10-05T04:00:00Z",
                }
            ],
        )
        assert result == [*days("2026-10-01", 2), *days("2026-10-05", 3)]

    def test_open_exception_removes_everything_from_it(self) -> None:
        result = starts(
            buys=[buy(end_time="2026-10-08T04:00:00Z")],
            exceptions=[
                {
                    "reason": "overridden",
                    "from": "2026-10-04T04:00:00Z",
                    "overridden_by": {"delivery_config_id": "acme"},
                }
            ],
        )
        assert result == days("2026-10-01", 3)

    def test_buy_level_exception_only_touches_that_buy(self) -> None:
        result = run(
            buys=[
                buy("mb-1", end_time="2026-10-03T04:00:00Z"),
                buy("mb-2", end_time="2026-10-03T04:00:00Z"),
            ],
            exceptions=[
                {
                    "media_buy_id": "mb-2",
                    "reason": "offering_unsupported",
                    "from": "2026-10-02T04:00:00Z",
                }
            ],
        )
        assert [(p.period_start, p.media_buy_ids) for p in result] == [
            ("2026-10-01T04:00:00Z", ("mb-1", "mb-2")),
            ("2026-10-02T04:00:00Z", ("mb-1",)),
        ]

    def test_package_exceptions_never_remove_periods(self) -> None:
        exceptions = [
            {
                "media_buy_id": "mb-1",
                "reason": "offering_unsupported",
                "from": "2026-10-01T04:00:00Z",
                "package_ids": ["pkg-9"],
            }
        ]
        with_exception = run(buys=[buy(end_time="2026-10-03T04:00:00Z")], exceptions=exceptions)
        assert with_exception == run(buys=[buy(end_time="2026-10-03T04:00:00Z")])

    def test_generated_coverage_model_round_trips(self) -> None:
        coverage = ReportingSubscriptionCoverage.model_validate(
            {
                "subscription_id": "analytics-daily",
                "subscription_version": 1,
                "status": "active",
                "excluded": [
                    {
                        "reason": "capacity",
                        "from": "2026-10-03T04:00:00Z",
                        "until": "2026-10-04T04:00:00Z",
                    }
                ],
                "gaps": [
                    {
                        "from": "2026-10-05T04:00:00Z",
                        "until": "2026-10-06T04:00:00Z",
                        "reason": "unauthorized",
                    }
                ],
            }
        )
        normalized = subscription_exceptions(coverage.excluded, coverage.gaps or ())
        assert [(e.reason, e.from_, e.until) for e in normalized] == [
            ("capacity", at("2026-10-03T04:00:00Z"), at("2026-10-04T04:00:00Z")),
            ("unauthorized", at("2026-10-05T04:00:00Z"), at("2026-10-06T04:00:00Z")),
        ]
        result = starts(
            buys=[buy(end_time="2026-10-07T04:00:00Z")],
            exceptions=coverage.excluded,
            gaps=coverage.gaps or (),
        )
        assert result == [*days("2026-10-01", 2), "2026-10-04T04:00:00Z", "2026-10-06T04:00:00Z"]

    def test_exceptions_only_ever_remove_periods(self) -> None:
        rng = random.Random(8543)
        buys = [
            buy("mb-1", end_time="2026-10-09T04:00:00Z"),
            buy("mb-2", start_time="2026-10-03T10:00:00Z", end_time="2026-10-12T04:00:00Z"),
        ]
        baseline = run(buys=buys)
        owed = {(p.period_start, b) for p in baseline for b in p.media_buy_ids}
        instants = [
            f"2026-10-{day:02d}T{hour:02d}:00:00Z" for day in range(1, 13) for hour in (0, 4, 13)
        ]
        for _ in range(200):
            entries = []
            for _ in range(rng.randint(1, 4)):
                lo, hi = sorted(rng.sample(instants, 2))
                entry: dict = {
                    "reason": rng.choice(["capacity", "excluded_by_buyer", "offering_unsupported"]),
                    "from": lo,
                }
                if rng.random() < 0.7:
                    entry["until"] = hi
                if rng.random() < 0.5:
                    entry["media_buy_id"] = rng.choice(["mb-1", "mb-2"])
                    entry["reason"] = "offering_unsupported"
                entries.append(entry)
            trimmed = run(buys=buys, exceptions=entries)
            remaining = {(p.period_start, b) for p in trimmed for b in p.media_buy_ids}
            assert remaining <= owed
            removed = owed - remaining
            for start, media_buy_id in removed:
                assert any(
                    e["from"] <= start
                    and ("until" not in e or start < e["until"])
                    and e.get("media_buy_id", media_buy_id) == media_buy_id
                    for e in entries
                )


# -- lost authority --------------------------------------------------------------


GRANT = at("2026-10-01T00:00:00Z")


class TestLostAuthority:
    def test_revocation_still_in_effect(self) -> None:
        # Lost at 2026-10-04T10:00Z: the Oct 4 day started before the loss
        # but ends after it, so it is not owed. Oct 3 ended before the loss
        # but its expected_at (Oct 4 10:00Z) is not after the loss, so it
        # stays due at its original deadline.
        result = run(
            buys=[buy(end_time="2026-10-08T04:00:00Z")],
            authorizations=[SubscriptionAuthorization(GRANT, at("2026-10-04T10:00:00Z"))],
        )
        assert [(p.period_start, p.expected_at, p.withheld) for p in result] == [
            ("2026-10-01T04:00:00Z", "2026-10-02T10:00:00Z", False),
            ("2026-10-02T04:00:00Z", "2026-10-03T10:00:00Z", False),
            ("2026-10-03T04:00:00Z", "2026-10-04T10:00:00Z", False),
        ]

    def test_period_pending_at_the_loss_is_withheld(self) -> None:
        result = run(
            buys=[buy(end_time="2026-10-08T04:00:00Z")],
            authorizations=[SubscriptionAuthorization(GRANT, at("2026-10-04T08:00:00Z"))],
        )
        assert [(p.period_start, p.expected_at, p.withheld) for p in result] == [
            ("2026-10-01T04:00:00Z", "2026-10-02T10:00:00Z", False),
            ("2026-10-02T04:00:00Z", "2026-10-03T10:00:00Z", False),
            ("2026-10-03T04:00:00Z", None, True),
        ]

    def test_regrant_without_backfill_records_a_gap(self) -> None:
        # Lost 2026-10-04T08:00Z, re-granted 2026-10-06T15:00Z. Gap from the
        # last boundary at or before the loss (Oct 4 04:00Z) until the first
        # boundary at or after the re-grant (Oct 7 04:00Z).
        result = run(
            buys=[buy(end_time="2026-10-09T04:00:00Z")],
            status_retention_days=30,
            authorizations=[
                SubscriptionAuthorization(GRANT, at("2026-10-04T08:00:00Z")),
                SubscriptionAuthorization(at("2026-10-06T15:00:00Z")),
            ],
        )
        assert [(p.period_start, p.expected_at) for p in result] == [
            ("2026-10-01T04:00:00Z", "2026-10-02T10:00:00Z"),
            ("2026-10-02T04:00:00Z", "2026-10-03T10:00:00Z"),
            # withheld at the loss, due at re-grant + delivery_sla
            ("2026-10-03T04:00:00Z", "2026-10-06T21:00:00Z"),
            ("2026-10-07T04:00:00Z", "2026-10-08T10:00:00Z"),
            ("2026-10-08T04:00:00Z", "2026-10-09T10:00:00Z"),
        ]
        assert not any(p.withheld for p in result)

    def test_regrant_with_backfill_keeps_the_revoked_interval_owed(self) -> None:
        result = run(
            buys=[buy(end_time="2026-10-09T04:00:00Z")],
            backfill_on_regrant=True,
            status_retention_days=30,
            authorizations=[
                SubscriptionAuthorization(GRANT, at("2026-10-04T08:00:00Z")),
                SubscriptionAuthorization(at("2026-10-06T15:00:00Z")),
            ],
        )
        assert [(p.period_start, p.expected_at) for p in result] == [
            ("2026-10-01T04:00:00Z", "2026-10-02T10:00:00Z"),
            ("2026-10-02T04:00:00Z", "2026-10-03T10:00:00Z"),
            ("2026-10-03T04:00:00Z", "2026-10-06T21:00:00Z"),
            ("2026-10-04T04:00:00Z", "2026-10-06T21:00:00Z"),
            ("2026-10-05T04:00:00Z", "2026-10-06T21:00:00Z"),
            ("2026-10-06T04:00:00Z", "2026-10-07T10:00:00Z"),
            ("2026-10-07T04:00:00Z", "2026-10-08T10:00:00Z"),
            ("2026-10-08T04:00:00Z", "2026-10-09T10:00:00Z"),
        ]

    def test_regrant_after_retention_is_a_fresh_start_without_backfill(self) -> None:
        result = run(
            buys=[buy(end_time="2026-10-09T04:00:00Z")],
            backfill_on_regrant=True,
            status_retention_days=1,
            authorizations=[
                SubscriptionAuthorization(GRANT, at("2026-10-04T08:00:00Z")),
                SubscriptionAuthorization(at("2026-10-06T15:00:00Z")),
            ],
        )
        assert [(p.period_start, p.expected_at, p.withheld) for p in result] == [
            ("2026-10-01T04:00:00Z", "2026-10-02T10:00:00Z", False),
            ("2026-10-02T04:00:00Z", "2026-10-03T10:00:00Z", False),
            ("2026-10-03T04:00:00Z", None, True),
            ("2026-10-07T04:00:00Z", "2026-10-08T10:00:00Z", False),
            ("2026-10-08T04:00:00Z", "2026-10-09T10:00:00Z", False),
        ]

    def test_loss_on_a_boundary(self) -> None:
        result = starts(
            buys=[buy(end_time="2026-10-09T04:00:00Z")],
            status_retention_days=30,
            authorizations=[
                SubscriptionAuthorization(GRANT, at("2026-10-04T04:00:00Z")),
                SubscriptionAuthorization(at("2026-10-06T04:00:00Z")),
            ],
        )
        assert result == [*days("2026-10-01", 3), *days("2026-10-06", 3)]

    def test_regrant_needs_status_retention_days(self) -> None:
        with pytest.raises(ValueError):
            run(
                buys=[buy()],
                authorizations=[
                    SubscriptionAuthorization(GRANT, at("2026-10-04T08:00:00Z")),
                    SubscriptionAuthorization(at("2026-10-06T15:00:00Z")),
                ],
            )

    def test_regrant_exactly_at_retention_is_a_fresh_start(self) -> None:
        # A re-grant continues the chain only before unauthorized_at + retention.
        kwargs = dict(
            buys=[buy(end_time="2026-10-09T04:00:00Z")],
            backfill_on_regrant=True,
            status_retention_days=2,
        )
        inside = run(
            authorizations=[
                SubscriptionAuthorization(GRANT, at("2026-10-04T08:00:00Z")),
                SubscriptionAuthorization(at("2026-10-06T07:59:59Z")),
            ],
            **kwargs,
        )
        at_limit = run(
            authorizations=[
                SubscriptionAuthorization(GRANT, at("2026-10-04T08:00:00Z")),
                SubscriptionAuthorization(at("2026-10-06T08:00:00Z")),
            ],
            **kwargs,
        )
        assert "2026-10-04T04:00:00Z" in [p.period_start for p in inside]
        assert "2026-10-04T04:00:00Z" not in [p.period_start for p in at_limit]

    def test_overlapping_grants_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            run(
                buys=[buy()],
                authorizations=[
                    SubscriptionAuthorization(GRANT, at("2026-10-05T00:00:00Z")),
                    SubscriptionAuthorization(at("2026-10-04T00:00:00Z")),
                ],
            )


# -- timezones and clocks --------------------------------------------------------


class TestTimezones:
    def test_account_timezone_sets_the_boundaries(self) -> None:
        result = periods(
            account_timezone="America/Los_Angeles",
            buys=[buy(start_time="2026-10-01T07:00:00Z", end_time="2026-10-03T07:00:00Z")],
        )
        assert result == [
            ("2026-10-01T07:00:00Z", "2026-10-02T07:00:00Z"),
            ("2026-10-02T07:00:00Z", "2026-10-03T07:00:00Z"),
        ]

    def test_daily_periods_follow_local_midnight_across_fall_back(self) -> None:
        # 2026-11-01 is the US fall-back day: it lasts 25 hours.
        result = periods(
            subscription=state("2026-10-30T00:00:00Z"),
            buys=[
                buy(
                    confirmed_at="2026-10-30T00:00:00Z",
                    start_time="2026-10-31T04:00:00Z",
                    end_time="2026-11-03T05:00:00Z",
                )
            ],
        )
        assert result == [
            ("2026-10-31T04:00:00Z", "2026-11-01T04:00:00Z"),
            ("2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z"),
            ("2026-11-02T05:00:00Z", "2026-11-03T05:00:00Z"),
        ]

    def test_local_hourly_periods_skip_the_spring_forward_gap(self) -> None:
        local_hourly = offering("local-hourly", duration="PT1H", sla="PT1H")
        result = periods(
            subscription=state("2026-03-08T00:00:00Z", config=subscription_config(duration="PT1H")),
            offerings=[local_hourly],
            buys=[
                buy(
                    confirmed_at="2026-03-08T00:00:00Z",
                    start_time="2026-03-08T05:00:00Z",
                    end_time="2026-03-08T08:00:00Z",
                    offering_ids=("local-hourly",),
                )
            ],
        )
        # 00:00 EST, 01:00 EST, then 03:00 EDT: the 02:00 slot has no length.
        assert result == [
            ("2026-03-08T05:00:00Z", "2026-03-08T06:00:00Z"),
            ("2026-03-08T06:00:00Z", "2026-03-08T07:00:00Z"),
            ("2026-03-08T07:00:00Z", "2026-03-08T08:00:00Z"),
        ]

    def test_timezone_change_is_a_cutover(self) -> None:
        # The spec's example: a daily feed moves from UTC-5 to UTC-8 at 12:00Z.
        # The old schedule ends at 05:00Z the next day; the first new-timezone
        # day starts at 08:00Z; 05:00Z..08:00Z is owed under neither.
        result = run(
            account_timezone="Etc/GMT+5",
            timezone_changes=[AccountTimezoneChange(at("2026-10-03T12:00:00Z"), "Etc/GMT+8")],
            buys=[buy(start_time="2026-10-02T05:00:00Z", end_time="2026-10-05T08:00:00Z")],
        )
        assert [(p.period_start, p.period_end, p.source_timezone) for p in result] == [
            ("2026-10-02T05:00:00Z", "2026-10-03T05:00:00Z", "Etc/GMT+5"),
            ("2026-10-03T05:00:00Z", "2026-10-04T05:00:00Z", "Etc/GMT+5"),
            ("2026-10-04T08:00:00Z", "2026-10-05T08:00:00Z", "Etc/GMT+8"),
        ]

    def test_timezone_change_does_not_touch_utc_offerings(self) -> None:
        kwargs = dict(
            subscription=state("2026-10-03T00:00:00Z", config=subscription_config(duration="PT1H")),
            buys=[
                buy(
                    start_time="2026-10-03T10:00:00Z",
                    end_time="2026-10-03T14:00:00Z",
                    offering_ids=("hourly-pkg",),
                )
            ],
        )
        moved = run(
            timezone_changes=[AccountTimezoneChange(at("2026-10-03T12:30:00Z"), "Etc/GMT+8")],
            **kwargs,
        )
        assert moved == run(**kwargs)

    def test_billing_cycle_anchor(self) -> None:
        cycle = offering(
            "weekly-cycle",
            duration="P7D",
            alignment="billing_cycle",
            period_timezone=NY,
            period_anchor=at("2026-09-28T04:00:00Z"),  # a Monday, local midnight
        )
        result = periods(
            subscription=state("2026-10-01T00:00:00Z", config=subscription_config(duration="P7D")),
            offerings=[cycle],
            buys=[buy(end_time="2026-10-13T04:00:00Z", offering_ids=("weekly-cycle",))],
        )
        assert result == [
            ("2026-10-05T04:00:00Z", "2026-10-12T04:00:00Z"),
            ("2026-10-12T04:00:00Z", "2026-10-19T04:00:00Z"),
        ]

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            run(buys=[buy()], now=datetime(2026, 12, 1))


def test_explicit_windows_and_exception_values() -> None:
    windows = [
        SubscriptionVersionWindow(1, at("2026-10-01T00:00:00Z"), None, subscription_config())
    ]
    result = run(
        subscription=windows,
        buys=[buy(end_time="2026-10-04T04:00:00Z")],
        exceptions=[
            SubscriptionException(
                "capacity", at("2026-10-02T04:00:00Z"), at("2026-10-03T04:00:00Z")
            )
        ],
    )
    assert [p.period_start for p in result] == ["2026-10-01T04:00:00Z", "2026-10-03T04:00:00Z"]


@pytest.mark.parametrize(
    "confirmed, accepted, granted",
    list(
        itertools.product(
            ["2026-10-02T00:00:00Z", "2026-10-02T04:00:00Z", "2026-10-03T13:00:00Z"],
            ["2026-10-01T00:00:00Z", "2026-10-03T04:00:00Z", "2026-10-03T23:59:00Z"],
            ["2026-09-01T00:00:00Z", "2026-10-02T16:00:00Z", "2026-10-04T04:00:00Z"],
        )
    ),
)
def test_start_is_the_first_boundary_after_the_latest_of_three(
    confirmed: str, accepted: str, granted: str
) -> None:
    latest = max(at(confirmed), at(accepted), at(granted))
    expected = [
        start
        for start in days("2026-09-30", 10)
        if at(start) >= latest and at(start) < at("2026-10-08T04:00:00Z")
    ]
    result = starts(
        subscription=state(accepted),
        authorizations=[SubscriptionAuthorization(at(granted))],
        buys=[
            buy(
                confirmed_at=confirmed,
                start_time="2026-09-30T04:00:00Z",
                end_time="2026-10-08T04:00:00Z",
            )
        ],
    )
    assert result == expected


# -- fields settled by adcp#8585 ----------------------------------------------------


class TestSettledFields:
    def test_readback_inactive_intervals_are_a_fresh_start(self) -> None:
        readback = state("2026-10-01T00:00:00Z")
        readback["inactive_intervals"] = [
            {"deactivated_at": "2026-10-03T12:00:00Z", "reactivated_at": "2026-10-05T12:00:00Z"}
        ]
        model = ReportingSubscriptionState.model_validate(readback)
        assert starts(subscription=model, buys=[buy(end_time="2026-10-08T04:00:00Z")]) == [
            *days("2026-10-01", 3),
            *days("2026-10-06", 2),
        ]

    def test_timezone_cutover_from_coverage_with_previous_timezone(self) -> None:
        coverage = ReportingSubscriptionCoverage.model_validate(
            {
                "subscription_id": "analytics-daily",
                "subscription_version": 1,
                "status": "active",
                "excluded": [],
                "timezone_cutovers": [
                    {
                        "effective_at": "2026-10-04T05:00:00Z",
                        "timezone": "Etc/GMT+8",
                        "previous_timezone": "Etc/GMT+5",
                    }
                ],
            }
        )
        # No account_timezone: the zone before the cutover is previous_timezone.
        result = run(
            account_timezone=None,
            coverage=coverage,
            buys=[buy(start_time="2026-10-02T05:00:00Z", end_time="2026-10-05T08:00:00Z")],
        )
        assert [(p.period_start, p.source_timezone) for p in result] == [
            ("2026-10-02T05:00:00Z", "Etc/GMT+5"),
            ("2026-10-03T05:00:00Z", "Etc/GMT+5"),
            ("2026-10-04T08:00:00Z", "Etc/GMT+8"),
        ]

    def test_per_offering_cutovers_and_gaps(self) -> None:
        local_hourly = offering("local-hourly", duration="PT1H", sla="PT1H")
        config = subscription_config(dimensions=["package"], match="all")
        config["selector"]["period_duration"] = "P1D"
        daily_buy = buy(end_time="2026-10-05T04:00:00Z", offering_ids=("daily-pkg",))
        gaps = [
            {
                "from": "2026-10-02T04:00:00Z",
                "until": "2026-10-03T04:00:00Z",
                "reason": "unauthorized",
                "offering_id": "some-other-offering",
            }
        ]
        # A gap naming another offering leaves this one's periods owed.
        assert starts(
            subscription=state("2026-10-01T00:00:00Z", config=config),
            offerings=[DAILY, local_hourly],
            buys=[daily_buy],
            gaps=gaps,
        ) == days("2026-10-01", 4)
        gaps[0]["offering_id"] = "daily-pkg"
        assert starts(
            subscription=state("2026-10-01T00:00:00Z", config=config),
            offerings=[DAILY],
            buys=[daily_buy],
            gaps=gaps,
        ) == ["2026-10-01T04:00:00Z", *days("2026-10-03", 2)]
        # A timezone cutover naming another offering leaves this one's zone.
        moved = run(
            offerings=[DAILY],
            buys=[daily_buy],
            timezone_changes=[
                AccountTimezoneChange(
                    at("2026-10-02T04:00:00Z"), "America/Los_Angeles", offering_id="other"
                )
            ],
        )
        assert {p.source_timezone for p in moved} == {NY}

    def test_period_timezone_matching_is_as_specified(self) -> None:
        utc_daily = offering("utc-daily", alignment="utc")
        resolved_source = offering(
            "src-resolved",
            alignment="source_timezone",
            period_timezone="Asia/Tokyo",
            period_timezone_policy="account_resolved",
        )

        def override(zone: str) -> dict:
            return subscription_config(
                dimensions=["package"],
                match="all",
                scope={"account_ids": ["acct-1"]},
                period_timezone=zone,
            )

        product = ("utc-daily", "src-resolved")
        offers = [utc_daily, resolved_source]
        # Only UTC and Etc/UTC are UTC aliases; an account-resolved source
        # zone is not a published zone, so it never matches an override.
        assert resolved(override("UTC"), offerings=offers, product=product) == ("utc-daily",)
        assert resolved(override("Etc/Zulu"), offerings=offers, product=product) == ()
        assert resolved(override("Asia/Tokyo"), offerings=offers, product=product) == ()

    def test_wire_offering_dimensions_drive_matching(self) -> None:
        wire = {
            "offering_id": "gam-daily",
            "feed_purpose": "analytics",
            "report_definition_id": "gam-delivery-v1",
            "reporting_profile": {"id": "delivery"},
            "schedule": {"period_duration": "P1D", "alignment": "utc", "delivery_sla": "PT6H"},
            "supported_finality": ["official"],
            "reconciliation_mode": "delivery_only",
            "standard_dimensions": ["package"],
            "unmapped_dimensions": ["device"],
        }
        projected = SubscriptionOffering.from_offering(wire)
        assert projected.dimensions == frozenset({"package"})
        assert projected.unmapped_dimensions == frozenset({"device"})
        exact = subscription_config(dimensions=["package"])
        finest = subscription_config(dimensions=["package"], match="finest")
        assert resolved(exact, offerings=[projected], product=("gam-daily",)) == ()
        assert resolved(finest, offerings=[projected], product=("gam-daily",)) == ("gam-daily",)
