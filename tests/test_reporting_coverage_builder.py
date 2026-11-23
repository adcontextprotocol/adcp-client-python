"""``build_reporting_coverage`` enforces the reporting-coverage partition and status rules."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adcp.reporting.ledger import (
    CompletePackageRoster,
    MissingHistoricalPackageRoster,
    PerMediaBuyPackageRoster,
    ReportingCoverageRecord,
    UnsupportedMediaBuyRoster,
    build_reporting_coverage,
)
from adcp.validation.schema_loader import get_named_validator

END = datetime(2026, 9, 1, 2, tzinfo=timezone.utc)


def _valid(record: ReportingCoverageRecord) -> dict:
    wire = record.to_wire()
    validator = get_named_validator("core/reporting-coverage.json")
    assert validator is not None
    assert list(validator.iter_errors(wire)) == []
    assert ReportingCoverageRecord.from_wire(wire) == record
    return wire


def test_the_empty_denominator_is_full() -> None:
    for roster in (None, CompletePackageRoster(()), PerMediaBuyPackageRoster({})):
        wire = _valid(build_reporting_coverage((), roster, END))
        assert wire["status"] == "full" and wire["media_buy_ids"] == []
        assert wire["evaluated_at"] == "2026-09-01T02:00:00Z"


def test_no_package_dimension_and_a_complete_roster_are_full() -> None:
    assert build_reporting_coverage(("b", "a"), None, END).fully_covered_media_buy_ids == (
        "a",
        "b",
    )
    wire = _valid(build_reporting_coverage(("a",), CompletePackageRoster(("p",)), END))
    assert (wire["status"], wire["covered_package_ids"]) == ("full", ["p"])


def test_a_missing_roster_is_unknown_per_buy_with_capability_unknown() -> None:
    wire = _valid(build_reporting_coverage(("a", "b"), MissingHistoricalPackageRoster(), END))
    assert wire["status"] == "unknown"
    assert wire["unknown_media_buy_ids"] == ["a", "b"]
    assert wire["limitations"] == [
        {"reason": "capability_unknown", "media_buy_id": "a"},
        {"reason": "capability_unknown", "media_buy_id": "b"},
    ]


def test_unsupported_only_is_none_and_unsupported_with_unknown_is_unknown() -> None:
    unsupported = UnsupportedMediaBuyRoster("offering_unsupported", ("p1",))
    wire = _valid(build_reporting_coverage(("a",), unsupported, END))
    assert (wire["status"], wire["unsupported_package_ids"]) == ("none", ["p1"])
    mixed = PerMediaBuyPackageRoster({"a": unsupported, "b": MissingHistoricalPackageRoster()})
    assert _valid(build_reporting_coverage(("a", "b"), mixed, END))["status"] == "unknown"


def test_one_covered_buy_makes_an_incomplete_set_partial() -> None:
    roster = PerMediaBuyPackageRoster(
        {"a": CompletePackageRoster(("p1",)), "b": MissingHistoricalPackageRoster()}
    )
    wire = _valid(build_reporting_coverage(("a", "b"), roster, END))
    assert wire["status"] == "partial"
    assert (wire["fully_covered_media_buy_ids"], wire["unknown_media_buy_ids"]) == (["a"], ["b"])


def test_per_buy_keys_must_equal_the_denominator() -> None:
    roster = PerMediaBuyPackageRoster({"a": CompletePackageRoster(())})
    with pytest.raises(ValueError):
        build_reporting_coverage(("a", "b"), roster, END)
    with pytest.raises(ValueError):
        build_reporting_coverage((), roster, END)


def test_a_package_cannot_be_covered_and_unsupported() -> None:
    roster = PerMediaBuyPackageRoster(
        {
            "a": CompletePackageRoster(("p",)),
            "b": UnsupportedMediaBuyRoster("provider_limitation", ("p",)),
        }
    )
    with pytest.raises(ValueError):
        build_reporting_coverage(("a", "b"), roster, END)


def test_the_record_refuses_a_broken_partition_or_a_contradicting_status() -> None:
    with pytest.raises(ValueError, match="partition"):
        ReportingCoverageRecord(status="full", evaluated_at=END, media_buy_ids=("a",))
    with pytest.raises(ValueError, match="status"):
        ReportingCoverageRecord(
            status="full", evaluated_at=END, media_buy_ids=("a",), unknown_media_buy_ids=("a",)
        )
    with pytest.raises(ValueError):
        UnsupportedMediaBuyRoster("capability_unknown")  # type: ignore[arg-type]
