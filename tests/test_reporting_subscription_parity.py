"""Cross-SDK parity: the spec's canonical coverage vectors, run against Python.

``tests/fixtures/reporting_subscriptions/spec/`` vendors
``static/compliance/source/test-vectors/reporting-subscriptions/`` from the spec
repository at :data:`SPEC_COMMIT` (the signed 3.3.0-beta.3 source commit, with
adcp#8617, #8618, #8619 and #8622), pinned below by SHA-256. Each
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
    SubscriptionDestination,
    SubscriptionOffering,
    SubscriptionVersionWindow,
    owed_subscription_periods,
)

SPEC = Path(__file__).parent / "fixtures" / "reporting_subscriptions" / "spec"
#: adcontextprotocol/adcp commit the vendored files come from: the source commit
#: of the signed 3.3.0-beta.3 bundle (schemas/releases/3.3.0-beta.3.json).
SPEC_COMMIT = "d5f8de383df13525e11721ee3d493ee76352210f"
PINNED = {
    "vectors.json": "56cc381bc336b31cb0abc589d73d0f1a8ecd52f5c42532fc47fb9112c3b594a4",
    "case.schema.json": "17448cb98b1c8ce671b0c5f354492e4965f833320c78f4ee6722b184ab344c60",
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
            )
        )
    return result


def buys(input_: dict[str, Any]) -> list[SubscriptionBuy]:
    """Each buy as ``get_media_buys`` returns it (every input field is on the wire)."""
    result = []
    for buy in input_["buys"]:
        media_buy = {
            key: value
            for key, value in buy.items()
            if key not in {"canceled_at", "cancellation_effective_at"}
        }
        if "canceled_at" in buy:
            media_buy["cancellation"] = {"canceled_at": buy["canceled_at"]}
        if "cancellation_effective_at" in buy:
            media_buy["cancellation_effective_at"] = buy["cancellation_effective_at"]
        result.append(SubscriptionBuy.from_media_buy(media_buy))
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
            p.stub,
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
            row.get("stub", False),
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
                p.stub,
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
                item.get("stub", False),
                item["media_buy_ids"],
            )
            for item in case["expected_obligations"]
        ], case.get("rule")


def _wire_errors(validator, value) -> list[str]:
    # The dimension vocabulary is x-extensible: receivers MUST accept values
    # the enum does not list.
    return [
        error.message
        for error in validator.iter_errors(value)
        if not (
            error.validator == "enum"
            and any("dimensions" in str(part) for part in error.absolute_path)
        )
    ]


def test_every_vector_wire_object_is_schema_valid() -> None:
    """Each input's wire objects validate against the 3.3.0-beta.3 schemas.

    Subscriptions against ``core/reporting-subscription.json``, offerings
    (with ``available_from`` / ``withdrawn_at``) against
    ``core/reporting-delivery-offering.json``, and each buy's
    ``flight_history`` against ``core/media-buy.json`` (adcp#8618): every
    input a buyer needs is on the wire.
    """
    from adcp.validation import schema_loader

    version = "3.3.0-beta.3"
    subscription = schema_loader.get_named_validator(
        "core/reporting-subscription.json", version=version
    )
    offering = schema_loader.get_named_validator(
        "core/reporting-delivery-offering.json", version=version
    )
    flight_history = schema_loader.get_named_validator(
        "core/media-buy.json", version=version
    ).evolve(schema={"$ref": "#/properties/flight_history"})
    for case in CASES:
        input_ = case["input"]
        errors = [
            *(
                (f"versions[{index}]", _wire_errors(subscription, item["configuration"]))
                for index, item in enumerate(input_["versions"])
            ),
            *(
                (f"offerings[{index}]", _wire_errors(offering, item))
                for index, item in enumerate(input_["offerings"])
            ),
            *(
                (
                    f"buys[{index}].flight_history",
                    _wire_errors(flight_history, item["flight_history"]),
                )
                for index, item in enumerate(input_["buys"])
                if "flight_history" in item
            ),
        ]
        failures = [(where, found) for where, found in errors if found]
        if case.get("expected_error", {}).get("wire_valid") is False:
            assert failures, (
                case["id"],
                "expected_error.wire_valid is false but every object validates",
            )
            continue
        assert failures == [], (case["id"], failures)


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


def _baseline() -> dict[str, Any]:
    import copy

    return copy.deepcopy(next(case for case in CASES if case["id"] == "baseline")["input"])


def _starts(input_: dict[str, Any]) -> list[str]:
    return sorted({p.period_start for p in periods(input_)})


BASELINE_STARTS = ["2026-11-03T00:00:00Z", "2026-11-04T00:00:00Z", "2026-11-05T00:00:00Z"]


def test_issue_1769_an_account_zone_no_schedule_reads_is_not_validated() -> None:
    input_ = _baseline()
    input_["account"]["timezone"] = "+05:00"
    assert _starts(input_) == BASELINE_STARTS


def test_issue_1769_a_cutover_no_account_timezone_offering_reads_needs_no_previous_zone() -> None:
    input_ = _baseline()
    input_["coverage"] = {
        "timezone_cutovers": [
            {"effective_at": "2026-11-05T05:00:00Z", "timezone": "America/New_York"}
        ]
    }
    assert _starts(input_) == BASELINE_STARTS


def test_issue_1769_a_non_candidate_offering_without_standard_dimensions_is_ignored() -> None:
    import copy

    input_ = _baseline()
    extra = copy.deepcopy(input_["offerings"][0])
    extra.update(offering_id="off_z_pacing_hourly", feed_purpose="pacing")
    extra["schedule"] = {**extra["schedule"], "period_duration": "PT1H"}
    extra.pop("standard_dimensions", None)
    input_["offerings"].append(extra)
    assert _starts(input_) == BASELINE_STARTS
