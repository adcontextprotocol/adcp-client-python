"""Reporting-subscription rules settled for AdCP 3.3.0-beta.3.

The shared coverage rule is pinned by the spec's vectors
(``test_reporting_subscription_parity``). These tests cover what the vectors
do not reach: reading the beta.3 wire (adcp#8617, #8618), carrying a billing
stub from the owed set through the ledger to ``core/reporting-obligation.json``,
and the seller-side refusals.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from adcp.reporting.ledger import InMemoryReportingLedgerStore
from adcp.reporting.ledger.status import _obligation_to_wire
from adcp.reporting.subscriptions import (
    CoverageInputError,
    FrozenOwedSet,
    SubscriptionBuy,
    SubscriptionBuyPackage,
    SubscriptionObligationPlanner,
    SubscriptionOffering,
    SubscriptionVersionWindow,
    owed_subscription_periods,
)
from adcp.reporting.subscriptions.managed import ManagedSubscriptions
from adcp.validation import schema_loader


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _offering(offering_id: str, alignment: str, **schedule) -> dict:
    return {
        "offering_id": offering_id,
        "feed_purpose": "billing",
        "report_definition_id": "rd",
        "reporting_profile": {"id": "delivery", "primary_keys": ["account_id", "package_id"]},
        "schedule": {
            "period_duration": "P1D",
            "alignment": alignment,
            "delivery_sla": "PT6H",
            **schedule,
        },
        "supported_finality": ["official"],
        "reconciliation_mode": "delivery_only",
        "standard_dimensions": ["package"],
    }


def _billing(version: int, alignment: str) -> dict:
    return {
        "subscription_id": "billing",
        "subscription_version": version,
        "active": True,
        "scope": {"all_authorized_accounts": True},
        "selector": {
            "feed_purpose": "billing",
            "period_duration": "P1D",
            "required_finality": "official",
            "dimensions": ["package"],
            "alignment": alignment,
        },
        "reconciliation_mode": "delivery_only",
    }


# -- reading the wire ----------------------------------------------------------


def test_a_media_buy_is_read_from_the_beta3_wire() -> None:
    buy = SubscriptionBuy.from_media_buy(
        {
            "media_buy_id": "mb-1",
            "confirmed_at": "2026-11-01T00:00:00.123Z",
            "start_time": "2026-11-02T00:00:00Z",
            "end_time": "2026-11-30T00:00:00Z",
            "cancellation": {"canceled_at": "2026-11-10T00:00:00Z"},
            "cancellation_effective_at": "2026-11-17T00:00:00Z",
            "flight_history": [
                {
                    "recorded_at": "2026-11-05T00:00:00Z",
                    "start_time": "2026-11-03T00:00:00Z",
                    "stop_time": "2026-11-20T00:00:00Z",
                }
            ],
            "packages": [
                {"package_id": "pkg-1", "reporting_delivery_offering_ids": ["off-a"]},
                {"package_id": "pkg-2"},
            ],
        },
        package_offering_ids={"pkg-2": ["off-b"]},
    )
    # adcp#8617: delivery stops at cancellation_effective_at, not canceled_at.
    assert buy.stop_time == at("2026-11-17T00:00:00Z")
    # adcp#8618: the flight in force before the edit, from flight_history.
    assert buy.flight_at(at("2026-11-04T00:00:00Z")) == (
        at("2026-11-03T00:00:00Z"),
        at("2026-11-20T00:00:00Z"),
    )
    assert buy.flight_at(at("2026-11-06T00:00:00Z")) == (
        at("2026-11-02T00:00:00Z"),
        at("2026-11-17T00:00:00Z"),
    )
    # The per-package offering snapshot, with the caller's fallback.
    assert buy.packages == (
        SubscriptionBuyPackage("pkg-1", ("off-a",)),
        SubscriptionBuyPackage("pkg-2", ("off-b",)),
    )


def test_without_cancellation_effective_at_canceled_at_stops_delivery() -> None:
    buy = SubscriptionBuy.from_media_buy(
        {
            "media_buy_id": "mb-1",
            "confirmed_at": "2026-11-01T00:00:00Z",
            "start_time": "2026-11-02T00:00:00Z",
            "end_time": "2026-11-30T00:00:00Z",
            "cancellation": {"canceled_at": "2026-11-10T00:00:00Z"},
        }
    )
    assert buy.stop_time == at("2026-11-10T00:00:00Z")


def test_offering_availability_is_read_from_the_wire() -> None:
    offering = SubscriptionOffering.from_offering(
        {
            **_offering("off-a", "utc"),
            "available_from": "2026-11-01T00:00:00Z",
            "withdrawn_at": "2026-12-01T00:00:00Z",
        }
    )
    assert offering.available_from == at("2026-11-01T00:00:00Z")
    assert offering.withdrawn_at == at("2026-12-01T00:00:00Z")
    assert not offering.available_at(at("2026-12-01T00:00:00Z"))


def test_a_sub_daily_billing_cycle_is_refused() -> None:
    with pytest.raises(CoverageInputError) as refused:
        SubscriptionOffering.from_offering(
            _offering(
                "off-hourly",
                "billing_cycle",
                period_duration="PT1H",
                period_anchor="2026-01-01T00:00:00Z",
                period_anchor_policy="fixed",
                period_timezone="UTC",
            )
        )
    assert (refused.value.code, refused.value.detail) == (
        "COVERAGE_INPUT_INVALID",
        "invalid_period_duration",
    )


# -- billing stubs through the ledger -------------------------------------------


def test_a_billing_cutover_stub_is_owed_written_and_served() -> None:
    utc = SubscriptionOffering.from_offering(_offering("bill-utc", "utc"))
    new_york = SubscriptionOffering.from_offering(_offering("bill-ny", "account_timezone"))
    windows = [
        SubscriptionVersionWindow(
            1, at("2026-11-01T12:00:00Z"), at("2026-11-04T12:00:00Z"), _billing(1, "utc")
        ),
        SubscriptionVersionWindow(
            2, at("2026-11-04T12:00:00Z"), None, _billing(2, "account_timezone")
        ),
    ]
    buy = SubscriptionBuy(
        "mb-1",
        at("2026-11-02T15:00:00Z"),
        at("2026-11-03T00:00:00Z"),
        at("2026-11-07T05:00:00Z"),
        packages=(SubscriptionBuyPackage("pkg-1", ("bill-ny", "bill-utc")),),
    )
    owed = owed_subscription_periods(
        subscription=windows,
        account_id="acct-1",
        buys=[buy],
        offerings=[utc, new_york],
        account_timezone="America/New_York",
        now=at("2026-11-08T00:00:00Z"),
    )
    assert [(p.period_start, p.period_end, p.offering_id, p.stub) for p in owed] == [
        ("2026-11-03T00:00:00Z", "2026-11-04T00:00:00Z", "bill-utc", False),
        ("2026-11-04T00:00:00Z", "2026-11-05T00:00:00Z", "bill-utc", False),
        # [E, first New York boundary): billable time stays contiguous.
        ("2026-11-05T00:00:00Z", "2026-11-05T05:00:00Z", "bill-ny", True),
        ("2026-11-05T05:00:00Z", "2026-11-06T05:00:00Z", "bill-ny", False),
        ("2026-11-06T05:00:00Z", "2026-11-07T05:00:00Z", "bill-ny", False),
    ]

    async def plan() -> list:
        store = InMemoryReportingLedgerStore(clock=lambda: at("2026-11-08T00:00:00Z"))
        planner = SubscriptionObligationPlanner(store, offerings=[utc, new_york], currency="USD")
        frozen = FrozenOwedSet(
            "buyer",
            "acct-1",
            "billing",
            at("2026-11-08T00:00:00Z"),
            tuple(owed),
            {1: _billing(1, "utc"), 2: _billing(2, "account_timezone")},
        )
        result = await planner.plan(frozen)
        return [
            item
            for key in result.generations
            for item in await store.list_generation_obligations(key)
        ]

    obligations = asyncio.run(plan())
    assert [item.stub for item in obligations].count(True) == 1
    (stub,) = [item for item in obligations if item.stub]
    wire = _obligation_to_wire(
        stub,
        revisions=(),
        health="healthy",
        production_status="awaiting_period_end",
        issues=(),
        statuses=(),
    )
    assert wire["stub"] is True
    assert wire["period"]["start"] == "2026-11-05T00:00:00Z"
    assert wire["period"]["end"] == "2026-11-05T05:00:00Z"
    validator = schema_loader.get_named_validator(
        "core/reporting-obligation.json", version="3.3.0-beta.3"
    )
    stub_schema = validator.evolve(schema={"$ref": "#/properties/stub"})
    assert stub_schema.is_valid(wire["stub"])
    # An ordinary obligation never carries the flag.
    ordinary = next(item for item in obligations if not item.stub)
    assert "stub" not in _obligation_to_wire(
        ordinary,
        revisions=(),
        health="healthy",
        production_status="awaiting_period_end",
        issues=(),
        statuses=(),
    )


# -- seller refusals -------------------------------------------------------------


def test_a_destination_offering_must_key_rows_by_account_id() -> None:
    from adcp.reporting import ReliableReportingConfigurationError

    delivered = {
        **_offering("off-files", "utc"),
        "method": {"pattern": "file_transfer", "transport": "s3"},
        "reporting_profile": {"id": "delivery", "primary_keys": ["package_id"]},
    }
    with pytest.raises(ReliableReportingConfigurationError, match="primary_keys"):
        ManagedSubscriptions._offerings([delivered], {})
    keyed = {**delivered, "reporting_profile": {"id": "delivery", "primary_keys": ["account_id"]}}
    (offering,) = ManagedSubscriptions._offerings([keyed], {})
    assert offering.method_pattern == "file_transfer"
    # An API-delivered offering is not a managed-delivery offering.
    (api,) = ManagedSubscriptions._offerings(
        [{**_offering("off-api", "utc"), "reporting_profile": {"id": "delivery"}}], {}
    )
    assert api.method_pattern is None
