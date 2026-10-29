"""Cross-SDK parity: the TypeScript SDK's coverage table, run against Python.

``tests/fixtures/reporting_subscriptions/coverage-cases.json`` is copied from
the TypeScript SDK (its ``$source`` names the commit). Each case gives plain
input and the exact owed ``(subscription_version, media_buy_id, offering_id,
period)`` rows, derived by hand from the spec rule, or an expected error. Both
SDKs must produce identical periods, so this test maps the shared input onto
:func:`expected_subscription_periods` and compares instants row by row.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.subscriptions import (
    SubscriptionAuthorization,
    SubscriptionBuy,
    SubscriptionBuyFlight,
    SubscriptionBuyPackage,
    SubscriptionDestination,
    SubscriptionOffering,
    SubscriptionVersionWindow,
    expected_subscription_periods,
)

FIXTURE = Path(__file__).parent / "fixtures" / "reporting_subscriptions" / "coverage-cases.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


def at(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def windows(
    versions: list[dict[str, Any]], readback_intervals: list[dict[str, Any]] = ()
) -> list[SubscriptionVersionWindow]:
    # The readback's inactive_intervals (adcp#8585) apply to every version.
    shared = [
        (at(item["deactivated_at"]), at(item.get("reactivated_at"))) for item in readback_intervals
    ]
    result = []
    for index, record in enumerate(versions):
        following = versions[index + 1] if index + 1 < len(versions) else None
        ends_at = at(record.get("superseded_at")) or (
            at(following["accepted_at"]) if following else None
        )
        inactive = [
            (at(item["from"]), at(item.get("until")))
            for item in record.get("inactive_intervals", ())
        ]
        inactive.extend(shared)
        if "deactivated_at" in record:
            inactive.append((at(record["deactivated_at"]), None))
        method = record.get("destination_method")
        result.append(
            SubscriptionVersionWindow(
                subscription_version=record["subscription_version"],
                accepted_at=at(record["accepted_at"]),
                ends_at=ends_at,
                configuration=record["configuration"],
                inactive_intervals=tuple(inactive),
                destination=(
                    None
                    if method is None
                    else SubscriptionDestination(method["pattern"], method.get("transport"))
                ),
            )
        )
    return result


def offerings(input_: dict[str, Any]) -> list[SubscriptionOffering]:
    source_timezone = input_["account"].get("source_timezone")
    result = []
    for wire in input_["offerings"]:
        schedule = wire["schedule"]
        resolved = (
            source_timezone
            if schedule["alignment"] == "source_timezone"
            and schedule.get("period_timezone_policy") != "fixed"
            else None
        )
        result.append(
            SubscriptionOffering.from_offering(
                wire,
                period_timezone=resolved,
                available_from=at(wire.get("available_from")),
                withdrawn_at=at(wire.get("withdrawn_at")),
            )
        )
    return result


def buys(input_: dict[str, Any]) -> list[SubscriptionBuy]:
    result = []
    for buy in input_["buys"]:
        result.append(
            SubscriptionBuy(
                media_buy_id=buy["media_buy_id"],
                confirmed_at=at(buy.get("confirmed_at")),
                start_time=at(buy["start_time"]),
                end_time=at(buy["end_time"]),
                canceled_at=at(buy.get("canceled_at")),
                completed_at=at(buy.get("completed_at")),
                packages=tuple(
                    SubscriptionBuyPackage(
                        item["package_id"], tuple(item["reporting_delivery_offering_ids"])
                    )
                    for item in buy["packages"]
                ),
                superseded_flights=tuple(
                    SubscriptionBuyFlight(
                        superseded_at=at(flight["replaced_at"]),
                        start_time=at(flight["start_time"]),
                        end_time=at(flight["end_time"]),
                        canceled_at=at(flight.get("canceled_at")),
                        completed_at=at(flight.get("completed_at")),
                    )
                    for flight in buy.get("prior_flights", ())
                ),
            )
        )
    return result


def run(input_: dict[str, Any]) -> list[tuple]:
    account = input_["account"]
    periods = expected_subscription_periods(
        subscription=windows(input_["versions"], input_.get("inactive_intervals", [])),
        account_id=account["account_id"],
        buys=buys(input_),
        offerings=offerings(input_),
        account_timezone=account.get("timezone"),
        account_operator=account.get("operator"),
        now=at(input_["now"]),
        authorizations=[
            SubscriptionAuthorization(at(grant["authorized_at"]), at(grant.get("unauthorized_at")))
            for grant in account.get("grants", ())
        ],
        coverage=input_.get("coverage"),
        backfill_on_regrant=bool(input_.get("backfill_on_regrant", False)),
        status_retention_days=input_.get("status_retention_days"),
    )
    assert {p.delivery_config_id for p in periods} <= {input_["subscription_id"]}
    rows = [
        (
            at(p.period_start),
            p.delivery_config_version,
            p.offering_id.encode(),
            media_buy_id.encode(),
            at(p.period_end),
        )
        for p in periods
        for media_buy_id in p.media_buy_ids
    ]
    return sorted(rows)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_typescript_parity(case: dict[str, Any]) -> None:
    if "expected_error" in case:
        with pytest.raises((ValueError, TypeError)):
            run(case["input"])
        return
    expected = sorted(
        (
            at(row["period_start"]),
            row["subscription_version"],
            row["offering_id"].encode(),
            row["media_buy_id"].encode(),
            at(row["period_end"]),
        )
        for row in case["expected"]
    )
    assert run(case["input"]) == expected, case.get("rule")


def test_every_subscription_in_the_table_is_schema_valid() -> None:
    """The table's subscriptions validate against core/reporting-subscription.json."""
    from adcp.validation import schema_loader

    validator = schema_loader.get_named_validator(
        "core/reporting-subscription.json", version="3.3.0-beta.1"
    )
    for case in CASES:
        for version in case["input"]["versions"]:
            errors = [error.message for error in validator.iter_errors(version["configuration"])]
            assert errors == [], (case["name"], version["subscription_version"], errors)
