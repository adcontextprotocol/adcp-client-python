"""``{period_date}`` is AdCP's ``YYYY-MM-DD`` calendar date of a period start in its timezone.

These tests pin the local-date rule, the daylight-saving edges, fail-closed behaviour, and how
object keys render it. They mirror the JavaScript SDK's tests, so both SDKs name the same object.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError
from adcp.reporting.ledger.row_storage_object import (
    REPORTING_ROW_DEFAULT_KEY_TEMPLATE,
    render_reporting_row_object_key_v1,
    reporting_period_date_v1,
)

date = reporting_period_date_v1


def test_a_los_angeles_period_dates_in_pacific_time_not_utc() -> None:
    # PDT is UTC-7: 07:00Z is local midnight.
    assert date("2026-10-02T07:00:00Z", "America/Los_Angeles") == "2026-10-02"
    assert date("2026-10-02T06:59:59Z", "America/Los_Angeles") == "2026-10-01"
    assert datetime.fromisoformat("2026-10-02T06:59:59+00:00").date().isoformat() == "2026-10-02"
    assert date("2026-10-02T23:30:00-07:00", "America/Los_Angeles") == "2026-10-02"
    assert date("2026-10-03T06:30:00Z", "America/Los_Angeles") == "2026-10-02"


def test_a_tokyo_period_dates_ahead_of_utc() -> None:
    assert date("2026-10-01T15:00:00Z", "Asia/Tokyo") == "2026-10-02"
    assert date("2026-10-01T14:59:59Z", "Asia/Tokyo") == "2026-10-01"
    assert date("2026-10-02T09:00:00+09:00", "Asia/Tokyo") == "2026-10-02"
    assert date("2026-10-01T23:59:59Z", "Asia/Tokyo") == "2026-10-02"


def test_half_hour_and_45_minute_offsets() -> None:
    assert date("2026-10-01T18:30:00Z", "Asia/Kolkata") == "2026-10-02"
    assert date("2026-10-01T18:29:59Z", "Asia/Kolkata") == "2026-10-01"
    assert date("2026-10-01T18:15:00Z", "Asia/Kathmandu") == "2026-10-02"
    assert date("2026-10-01T18:14:59Z", "Asia/Kathmandu") == "2026-10-01"


@pytest.mark.parametrize("zone", ["UTC", "Etc/UTC"])
def test_utc_and_its_alias_match_the_utc_date(zone: str) -> None:
    assert date("2026-10-01T23:59:59Z", zone) == "2026-10-01"
    assert date("2026-10-02T00:00:00Z", zone) == "2026-10-02"


@pytest.mark.parametrize(
    ("start", "zone", "expected"),
    [
        # Spring forward, Los Angeles, 2026-03-08.
        ("2026-03-08T07:59:59Z", "America/Los_Angeles", "2026-03-07"),
        ("2026-03-08T08:00:00Z", "America/Los_Angeles", "2026-03-08"),
        ("2026-03-08T09:59:59Z", "America/Los_Angeles", "2026-03-08"),
        ("2026-03-08T10:00:00Z", "America/Los_Angeles", "2026-03-08"),
        ("2026-03-09T06:59:59Z", "America/Los_Angeles", "2026-03-08"),
        ("2026-03-09T07:00:00Z", "America/Los_Angeles", "2026-03-09"),
        # Fall back, Los Angeles, 2026-11-01: 01:00 occurs twice.
        ("2026-11-01T06:59:59Z", "America/Los_Angeles", "2026-10-31"),
        ("2026-11-01T07:00:00Z", "America/Los_Angeles", "2026-11-01"),
        ("2026-11-01T08:30:00Z", "America/Los_Angeles", "2026-11-01"),
        ("2026-11-01T09:30:00Z", "America/Los_Angeles", "2026-11-01"),
        ("2026-11-02T07:59:59Z", "America/Los_Angeles", "2026-11-01"),
        ("2026-11-02T08:00:00Z", "America/Los_Angeles", "2026-11-02"),
        # Southern hemisphere, Sydney, 2026-10-04.
        ("2026-10-03T13:59:59Z", "Australia/Sydney", "2026-10-03"),
        ("2026-10-03T14:00:00Z", "Australia/Sydney", "2026-10-04"),
        ("2026-10-04T12:59:59Z", "Australia/Sydney", "2026-10-04"),
        ("2026-10-04T13:00:00Z", "Australia/Sydney", "2026-10-05"),
        # London, 2026-03-29.
        ("2026-03-28T23:59:59Z", "Europe/London", "2026-03-28"),
        ("2026-03-29T00:00:00Z", "Europe/London", "2026-03-29"),
        ("2026-03-29T23:00:00Z", "Europe/London", "2026-03-30"),
    ],
)
def test_daylight_saving_transitions_keep_the_local_date_the_period_begins_on(
    start: str, zone: str, expected: str
) -> None:
    assert date(start, zone) == expected


def test_a_year_boundary_and_leap_day_follow_the_local_calendar() -> None:
    assert date("2026-12-31T15:00:00Z", "Asia/Tokyo") == "2027-01-01"
    assert date("2027-01-01T07:59:59Z", "America/Los_Angeles") == "2026-12-31"
    assert date("2028-02-29T08:00:00Z", "America/Los_Angeles") == "2028-02-29"
    assert date("2028-02-29T07:59:59Z", "America/Los_Angeles") == "2028-02-28"


def test_accepts_rfc_3339_fractions_and_offsets() -> None:
    assert date("2026-10-02T07:00:00.000Z", "America/Los_Angeles") == "2026-10-02"
    assert date("2026-10-02T06:59:59.999999Z", "America/Los_Angeles") == "2026-10-01"
    assert date("2026-10-02T09:00:00+02:00", "America/Los_Angeles") == "2026-10-02"


def test_midnight_transitions_and_extreme_zones() -> None:
    # Havana springs forward at local midnight on 2026-03-08.
    assert date("2026-03-08T04:59:59Z", "America/Havana") == "2026-03-07"
    assert date("2026-03-08T05:00:00Z", "America/Havana") == "2026-03-08"
    assert date("2026-11-01T04:30:00Z", "America/Havana") == "2026-11-01"
    assert date("2026-11-01T05:30:00Z", "America/Havana") == "2026-11-01"
    # Samoa skipped 2011-12-30 entirely when it crossed the date line.
    assert date("2011-12-30T09:59:59Z", "Pacific/Apia") == "2011-12-29"
    assert date("2011-12-30T10:00:00Z", "Pacific/Apia") == "2011-12-31"
    # Extremes: UTC+14 and UTC-12.
    assert date("2026-10-01T10:00:00Z", "Pacific/Kiritimati") == "2026-10-02"
    assert date("2026-10-02T11:59:59Z", "Etc/GMT+12") == "2026-10-01"


def test_accepts_the_supported_year_range_and_refuses_what_cannot_render() -> None:
    assert date("0001-01-01T00:00:00Z", "Asia/Tokyo") == "0001-01-01"
    assert date("9999-12-31T14:59:59Z", "Asia/Tokyo") == "9999-12-31"
    with pytest.raises(ValueError):
        date("9999-12-31T15:00:00Z", "Asia/Tokyo")  # year 10000
    with pytest.raises(ValueError):
        date("0001-01-01T00:00:00Z", "America/Los_Angeles")  # year 0


@pytest.mark.parametrize(
    "start",
    [
        "2026-02-30T00:00:00Z",
        "2026-04-31T00:00:00Z",
        "2027-02-29T00:00:00Z",
        "2100-02-29T00:00:00Z",
        "2026-00-10T00:00:00Z",
        "2026-10-00T00:00:00Z",
        "2026-10-02T24:00:00Z",
    ],
)
def test_refuses_calendar_fields_that_would_roll_forward(start: str) -> None:
    with pytest.raises(ValueError, match="RFC 3339"):
        date(start, "UTC")


def test_leap_days_that_exist_are_accepted() -> None:
    assert date("2028-02-29T00:00:00Z", "UTC") == "2028-02-29"
    assert date("2000-02-29T00:00:00Z", "UTC") == "2000-02-29"


@pytest.mark.parametrize("zone", [None, "", "x" * 256, 42])
def test_a_missing_timezone_never_falls_back_to_utc(zone: Any) -> None:
    with pytest.raises(ValueError, match="source_timezone"):
        date("2026-10-02T07:00:00Z", zone)


@pytest.mark.parametrize(
    "zone",
    [
        "+05:30",
        "-07:00",
        "+0530",
        "+05",
        "Mars/Olympus_Mons",
        "not a zone",
        "UTC ",
        "America/Los_Angeles\n",
        "Z",
        "UTC+5",
        "../etc/passwd",
    ],
)
def test_a_non_iana_or_unknown_timezone_is_refused(zone: str) -> None:
    with pytest.raises(ValueError):
        date("2026-10-02T07:00:00Z", zone)


def test_legacy_zone_aliases_resolve_to_the_canonical_date() -> None:
    for zone in ("US/Pacific", "PST8PDT"):
        assert date("2026-10-02T06:59:59Z", zone) == "2026-10-01"
        assert date("2026-10-02T07:00:00Z", zone) == "2026-10-02"
    assert date("2026-10-01T18:30:00Z", "Asia/Calcutta") == "2026-10-02"


@pytest.mark.parametrize(
    "start",
    [None, "", "yesterday", "2026-10-02", "2026-10-02 07:00:00", "2026-13-01T00:00:00Z"],
)
def test_an_unusable_period_start_is_refused(start: Any) -> None:
    with pytest.raises(ValueError):
        date(start, "America/Los_Angeles")


# -- object keys ------------------------------------------------------------------------------


def key(**overrides: Any) -> str:
    values: dict[str, Any] = {
        "template": REPORTING_ROW_DEFAULT_KEY_TEMPLATE,
        "prefix": "adcp-rows",
        "namespace_key": "n" * 64,
        "account_id": "acct_1",
        "row_set_id": "rrev_1",
        "period_start": None,
        "finality": "snapshot",
        "content_sha256": "c" * 64,
        "chunk_index": 0,
        "compression": "gzip",
    }
    values.update(overrides)
    return render_reporting_row_object_key_v1(**values)


def test_keys_carry_the_local_date_as_one_path_segment() -> None:
    segments = key(period_start="2026-10-01T15:00:00Z", source_timezone="Asia/Tokyo").split("/")
    assert segments[3] == "2026-10-02"
    # prefix, namespace, account, date, finality, revision, content, chunk
    assert len(segments) == 8
    assert "/2026-10-01/" in key(
        period_start="2026-10-02T06:59:59Z", source_timezone="America/Los_Angeles"
    )
    assert "/2026-10-02/" in key(period_start="2026-10-02T06:59:59Z", source_timezone="UTC")


def test_undated_only_when_there_is_no_period_start_at_all() -> None:
    assert "/undated/" in key()
    assert "/undated/" in key(source_timezone="America/Los_Angeles")


@pytest.mark.parametrize("zone", [None, "", "Mars/Base", "+05:30"])
def test_a_period_start_without_a_usable_timezone_is_refused(zone: Any) -> None:
    with pytest.raises(ReportingRowStoreError) as refused:
        key(period_start="2026-10-02T07:00:00Z", source_timezone=zone)
    assert refused.value.code == "INVALID_INPUT"


def test_a_bad_period_start_is_refused() -> None:
    with pytest.raises(ReportingRowStoreError) as refused:
        key(period_start="not-a-date", source_timezone="UTC")
    assert refused.value.code == "INVALID_INPUT"


def test_a_template_without_period_date_needs_no_timezone() -> None:
    rendered = key(
        template="{prefix}/{namespace_key}/{content_sha256}/{chunk_index}",
        period_start="2026-10-02T07:00:00Z",
    )
    assert rendered.endswith("000000.jsonl.gz")


def test_the_rendered_key_is_bounded_at_the_adcp_object_ref_length() -> None:
    wide = (
        "{prefix}/{namespace_key}/{account_key}/"
        + "/".join(["{content_sha256}"] * 8)
        + "/{chunk_index}"
    )
    with pytest.raises(ReportingRowStoreError) as refused:
        key(
            template=wide,
            prefix="p" * 450,
            period_start="2026-10-02T07:00:00Z",
            source_timezone="UTC",
        )
    assert refused.value.code == "INVALID_INPUT"
    narrow = key(prefix="p" * 300, period_start="2026-10-02T07:00:00Z", source_timezone="UTC")
    assert len(narrow) <= 1024


def test_utc_is_not_assumed_for_a_naive_local_date_helper() -> None:
    # The helper takes an instant, so the host's own zone never leaks in.
    assert datetime(2026, 10, 2, tzinfo=timezone.utc).date().isoformat() == "2026-10-02"
