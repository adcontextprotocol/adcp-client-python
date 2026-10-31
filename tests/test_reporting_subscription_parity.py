"""Cross-SDK parity: the spec's canonical coverage vectors, run against Python.

``tests/fixtures/reporting_subscriptions/spec/`` vendors
``static/compliance/source/test-vectors/reporting-subscriptions/`` from the spec
repository at :data:`SPEC_COMMIT` (adcp#8616), pinned below by SHA-256. Each
case gives the buyer-side input and either the exact owed rows, derived from
the spec text its ``spec_refs`` cite and reproduced by an independent
reference model, or the input error. Both SDKs must produce identical periods,
so this test maps each input onto :func:`owed_subscription_periods` and
compares instants row by row.

The rows are derived with IANA tzdata 2026f. The test pins zoneinfo to the
``tzdata==2026.6`` distribution (2026f), never the host's zoneinfo.
"""

from __future__ import annotations

import hashlib
import json
import zoneinfo
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import pytest

from adcp.reporting._timestamp import aware_timestamp
from adcp.reporting.subscriptions import (
    CoverageInputError,
    SubscriptionAuthorization,
    SubscriptionBuy,
    SubscriptionBuyFlight,
    SubscriptionBuyPackage,
    SubscriptionDestination,
    SubscriptionOffering,
    SubscriptionVersionWindow,
    owed_subscription_periods,
)

SPEC = Path(__file__).parent / "fixtures" / "reporting_subscriptions" / "spec"
#: adcontextprotocol/adcp commit the vendored files come from (adcp#8616).
SPEC_COMMIT = "023bbd26e7c230dcc74bea4087336ea6bc8d0f6a"
PINNED = {
    "vectors.json": "b392df771fc2bf635c5c441ff2dd5107e1cda13ab483fe902a6a56661f5e7904",
    "case.schema.json": "0550b51097ea2c9a6bbd8721de5b5855acfcbd097dff31edeb0df2f397f7dc42",
    "error-codes.json": "4e8e5142ada936fc8923735155d786a0801258922fb85b492785730479713481",
    "reason-codes.json": "2574b2a32fb8260fc7f9a7d9cd1eac1b588aa22c54a350e54964a893b80e77c4",
}
#: tzdata 2026f is the 2026.6 distribution on PyPI.
TZDATA = "2026.6"
VECTORS = json.loads((SPEC / "vectors.json").read_text())
CASES = VECTORS["cases"]


def test_the_vendored_vectors_are_the_pinned_spec_files() -> None:
    for name, digest in PINNED.items():
        assert hashlib.sha256((SPEC / name).read_bytes()).hexdigest() == digest, name
    assert VECTORS["tzdata"]["version"] == "2026f"


@pytest.fixture(scope="module", autouse=True)
def pinned_tzdata():
    """Resolve zones from the pinned tzdata distribution, never the host's zoneinfo."""
    from adcp.reporting.subscriptions import _clock

    try:
        installed = version("tzdata")
    except PackageNotFoundError:
        installed = None
    assert installed == TZDATA, (
        f"the coverage vectors need tzdata {TZDATA} (IANA 2026f); install the dev extra"
    )
    zoneinfo.reset_tzpath(to=[])
    zoneinfo.ZoneInfo.clear_cache()
    _clock._iana_zones.cache_clear()
    yield
    zoneinfo.reset_tzpath()
    zoneinfo.ZoneInfo.clear_cache()
    _clock._iana_zones.cache_clear()


def at(value: str | None) -> datetime | None:
    # The vectors carry 1-6 fractional digits, which Python 3.10's ISO parser refuses.
    return None if value is None else aware_timestamp(value)


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


def periods(input_: dict[str, Any]):
    account = input_["account"]
    return owed_subscription_periods(
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


def rows(owed) -> list[tuple]:
    """Per-buy rows in the table's order: start, version, offering, buy (byte order)."""
    return [
        (
            at(p.period_start),
            p.delivery_config_version,
            p.offering_id.encode(),
            media_buy_id.encode(),
            at(p.period_end),
        )
        for p in owed
        for media_buy_id in p.media_buy_ids
    ]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_spec_vectors(case: dict[str, Any]) -> None:
    if "expected_error" in case:
        expected = case["expected_error"]
        with pytest.raises(CoverageInputError) as raised:
            periods(case["input"])
        error = raised.value
        assert (error.code, error.detail) == (expected["code"], expected["detail"]), (
            case["id"],
            str(error),
        )
        assert error.recovery == "correctable"
        return
    owed = periods(case["input"])
    assert {p.delivery_config_id for p in owed} <= {case["input"]["subscription_id"]}
    expected = [
        (
            at(row["period_start"]),
            row["subscription_version"],
            row["offering_id"].encode(),
            row["media_buy_id"].encode(),
            at(row["period_end"]),
        )
        for row in case["expected"]
    ]
    # Strict: the same rows in the same order, not just the same set.
    assert rows(owed) == expected, case.get("rule")
    if "expected_obligations" in case:
        obligations = [
            (
                p.delivery_config_version,
                p.offering_id,
                at(p.period_start),
                at(p.period_end),
                list(p.media_buy_ids),
            )
            for p in owed
        ]
        assert obligations == [
            (
                item["subscription_version"],
                item["offering_id"],
                at(item["period_start"]),
                at(item["period_end"]),
                item["media_buy_ids"],
            )
            for item in case["expected_obligations"]
        ], case.get("rule")


def test_every_vector_subscription_is_schema_valid() -> None:
    """Each vector's subscriptions validate against core/reporting-subscription.json."""
    from adcp.validation import schema_loader

    validator = schema_loader.get_named_validator(
        "core/reporting-subscription.json", version="3.3.0-beta.1"
    )
    for case in CASES:
        if case.get("expected_error", {}).get("wire_valid") is False:
            continue  # the input deliberately fails AdCP schema validation (P1W)
        for version in case["input"]["versions"]:
            errors = [
                error.message
                for error in validator.iter_errors(version["configuration"])
                # The dimension vocabulary is x-extensible: receivers MUST
                # accept values the beta.1 enum does not list.
                if not (error.validator == "enum" and "dimensions" in list(error.absolute_path))
            ]
            assert errors == [], (case["name"], version["subscription_version"], errors)


def test_error_codes_match_the_frozen_spec_vocabulary() -> None:
    """Codes, recovery classes, dispatch and Python classes follow error-codes.json."""
    import adcp.reporting as reporting
    from adcp.reporting.subscriptions import (
        COVERAGE_INPUT_DETAILS,
        REPORTING_SUBSCRIPTION_ERROR_CODES,
        ReportingSubscriptionError,
    )

    vocabulary = json.loads((SPEC / "error-codes.json").read_text())
    assert vocabulary["vocabulary_version"] == 1
    classes = {
        "ReportingSubscriptionError": ReportingSubscriptionError,
        "CoverageInputError": CoverageInputError,
        "ReliableReportingConfigurationError": reporting.ReliableReportingConfigurationError,
    }
    buyer = {}
    details: set[str] = set()
    for entry in vocabulary["codes"]:
        assert entry["sdk_classes"]["python"] in classes, entry["code"]
        if entry["sdk_classes"]["python"] == "ReportingSubscriptionError":
            buyer[entry["code"]] = (entry["recovery"], entry["dispatched"] == "false")
        if entry["code"] in {"COVERAGE_INPUT_INCOMPLETE", "COVERAGE_INPUT_INVALID"}:
            for detail in entry["details"]:
                details.add(detail)
                error = CoverageInputError(detail, "x")
                assert (error.code, error.recovery) == (entry["code"], entry["recovery"])
                assert error.dispatched is None  # a local computation dispatches nothing
    assert dict(REPORTING_SUBSCRIPTION_ERROR_CODES) == buyer
    assert COVERAGE_INPUT_DETAILS == details


def test_reason_codes_match_the_frozen_spec_vocabulary() -> None:
    from adcp.reporting.subscriptions import (
        COVERAGE_REASON_CODES,
        COVERAGE_RULE_ORDER,
        COVERAGE_VOCABULARY_VERSION,
    )

    vocabulary = json.loads((SPEC / "reason-codes.json").read_text())
    assert vocabulary["vocabulary_version"] == COVERAGE_VOCABULARY_VERSION
    assert list(COVERAGE_RULE_ORDER) == vocabulary["rule_order"]
    assert dict(COVERAGE_REASON_CODES) == {
        item["code"]: item.get("rule") for item in vocabulary["reasons"]
    }


def test_seller_configuration_codes_match_the_frozen_spec_vocabulary() -> None:
    from adcp.reporting._errors import (
        SELLER_CONFIGURATION_CODES,
        ReliableReportingConfigurationError,
    )

    vocabulary = json.loads((SPEC / "error-codes.json").read_text())
    seller = {
        entry["code"]: entry["recovery"]
        for entry in vocabulary["codes"]
        if entry["sdk_classes"]["python"] == "ReliableReportingConfigurationError"
    }
    assert set(seller) == SELLER_CONFIGURATION_CODES
    for code, recovery in seller.items():
        error = ReliableReportingConfigurationError("x", code=code)
        assert (error.code, error.recovery, error.dispatched) == (code, recovery, None)
    with pytest.raises(ValueError):
        ReliableReportingConfigurationError("x", code="NOT_A_CODE")
