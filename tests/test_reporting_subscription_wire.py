"""Version-gated projection of the adcp#8585 reporting-subscription fields."""

from __future__ import annotations

import pytest

from adcp.reporting.subscriptions import (
    project_reporting_delivery,
    project_subscription_coverage,
    project_subscription_state,
    serves_settled_subscription_fields,
)


@pytest.mark.parametrize(
    "version, served",
    [
        (None, False),
        ("3.2", False),
        ("3.3-beta.1", False),
        ("3.3.0-beta.1", False),
        ("3.3-beta.2", True),
        ("3.3.0-beta.10", True),
        ("3.3-rc.1", True),
        ("3.3", True),
        ("3.4", True),
        ("4.0", True),
        ("not-a-version", False),
    ],
)
def test_gate(version: str | None, served: bool) -> None:
    assert serves_settled_subscription_fields(version) is served


def test_offering_dimensions_and_inactive_intervals_are_dropped_below_beta_2() -> None:
    block = {
        "offerings": [
            {
                "offering_id": "a",
                "standard_dimensions": ["package"],
                "unmapped_dimensions": ["device"],
            }
        ]
    }
    assert project_reporting_delivery(block, "3.3-beta.1") == {"offerings": [{"offering_id": "a"}]}
    assert project_reporting_delivery(block, "3.3-beta.2") == block
    state = {"subscription_id": "s", "inactive_intervals": [{"deactivated_at": "x"}]}
    assert "inactive_intervals" not in project_subscription_state(state, "3.3-beta.1")
    assert project_subscription_state(state, "3.3-beta.2") == state


def test_per_offering_entries_become_account_wide_below_beta_2() -> None:
    coverage = {
        "subscription_id": "s",
        "gaps": [
            {
                "offering_id": "daily",
                "from": "2026-10-03T04:00:00Z",
                "until": "2026-10-05T04:00:00Z",
                "reason": "unauthorized",
            },
            {
                "offering_id": "utc",
                "from": "2026-10-03T00:00:00Z",
                "until": "2026-10-05T00:00:00Z",
                "reason": "unauthorized",
            },
        ],
        "timezone_cutovers": [
            {
                "offering_id": "daily",
                "effective_at": "2026-10-04T04:00:00Z",
                "timezone": "America/Los_Angeles",
                "previous_timezone": "America/New_York",
            },
            {
                "offering_id": "hourly",
                "effective_at": "2026-10-03T13:00:00Z",
                "timezone": "America/Los_Angeles",
                "previous_timezone": "America/New_York",
            },
        ],
    }
    projected = project_subscription_coverage(coverage, "3.3-beta.1")
    # One account-wide gap spanning both offerings' gaps.
    assert projected["gaps"] == [
        {"from": "2026-10-03T00:00:00Z", "until": "2026-10-05T04:00:00Z", "reason": "unauthorized"}
    ]
    # One cutover per change, at the latest effective_at, without the new fields.
    assert projected["timezone_cutovers"] == [
        {"effective_at": "2026-10-04T04:00:00Z", "timezone": "America/Los_Angeles"}
    ]
    assert project_subscription_coverage(coverage, "3.3-beta.2") == coverage


def test_distinct_changes_back_to_a_zone_stay_distinct_below_beta_2() -> None:
    cutovers = [
        {
            "effective_at": "2026-01-10T05:00:00Z",
            "timezone": "America/Los_Angeles",
            "previous_timezone": "America/New_York",
        },
        {
            "effective_at": "2026-03-10T08:00:00Z",
            "timezone": "America/New_York",
            "previous_timezone": "America/Los_Angeles",
        },
        {
            "effective_at": "2026-06-10T04:00:00Z",
            "timezone": "America/Los_Angeles",
            "previous_timezone": "America/New_York",
        },
    ]
    projected = project_subscription_coverage({"timezone_cutovers": cutovers}, "3.3-beta.1")
    assert [item["effective_at"] for item in projected["timezone_cutovers"]] == [
        "2026-01-10T05:00:00Z",
        "2026-03-10T08:00:00Z",
        "2026-06-10T04:00:00Z",
    ]


def test_prior_version_configuration_is_dropped_below_beta_2() -> None:
    state = {"prior_versions": [{"subscription_version": 1, "configuration": {"x": 1}}]}
    assert project_subscription_state(state, "3.3-beta.1")["prior_versions"] == [
        {"subscription_version": 1}
    ]
    assert project_subscription_state(state, "3.3-beta.2") == state
