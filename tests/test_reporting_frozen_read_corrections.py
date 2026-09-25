"""Regressions from the independent review of the frozen buyer read slice."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from adcp.reporting import (
    ReportingReconciliationError,
    evaluate_reporting_ledger,
    load_reporting_ledger,
)
from adcp.reporting.ownership import with_revision_ownership
from adcp.validation.schema_loader import get_named_validator
from tests.test_reporting_frozen_read_safety import (
    ARRAYS,
    NOW,
    SECRET,
    Pages,
    _expected,
    _history,
    _load,
    _pages,
    _request,
    _with_status,
)


def _distinct_histories(count: int, *, distinction: str = "campaigns") -> dict[str, Any]:
    raw = _history()
    prototype = deepcopy(raw)
    raw.update({name: [] for name in ARRAYS})
    raw["scope"]["all_accessible_media_buys"] = True
    raw["scope"].pop("media_buy_ids")
    for number in range(count):
        owner = deepcopy(prototype["periods"][0])
        revision = deepcopy(prototype["revisions"][0])
        material = deepcopy(prototype["materializations"][0])
        owner.update(
            reporting_obligation_id=f"obligation-number-{number}",
            reconciliation_mode="delivery_only",
            reconciliation_status="not_required",
            receipt_count=0,
            accepted_receipt_count=0,
        )
        owner["schedule"].update(period_anchor=owner["period"]["start"], period_timezone="UTC")
        if distinction == "campaigns":
            buys = [f"buy-{number}-a", f"buy-{number}-b"]
            for item in (owner, revision):
                item["media_buy_ids"] = buys
                item["coverage"]["media_buy_ids"] = buys
                item["coverage"]["fully_covered_media_buy_ids"] = buys
        elif distinction == "profile":
            owner["reporting_profile"] = revision["reporting_profile"] = f"profile-{number}"
        revision["reporting_revision_id"] = f"revision-number-{number}"
        material.update(
            reporting_materialization_id=f"materialization-number-{number}",
            reporting_obligation_id=owner["reporting_obligation_id"],
            reporting_revision_id=revision["reporting_revision_id"],
        )
        raw["periods"].append(owner)
        raw["revisions"].append(revision)
        raw["materializations"].append(material)
    return raw


def _split(raw: dict[str, Any], explicit: bool) -> list[dict[str, Any]]:
    raw["pagination"]["total_count"] = sum(len(raw[name]) for name in ARRAYS)
    pages = _pages(raw, explicit=False)
    if explicit:
        ownership = {
            material["reporting_revision_id"]: material["reporting_obligation_id"]
            for material in raw["materializations"]
        }
        return [with_revision_ownership(page, ownership) for page in pages]
    return pages


async def _load_distinct(raw: dict[str, Any], explicit: bool):
    return await load_reporting_ledger(
        Pages(_split(raw, explicit)), _request(pagination={"max_results": 1})
    )


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("distinction", ["campaigns", "profile", "identical"])
async def test_ownerless_revisionless_status_does_not_certify_multiple_obligations(
    explicit, distinction
):
    raw = _distinct_histories(2, distinction=distinction)
    status = _with_status(_history())["consumer_statuses"][0]
    for key in (
        "reporting_obligation_id",
        "reporting_revision_id",
        "observed_revision_content_sha256",
    ):
        status.pop(key)
    status["consumer_status"] = "obligation_missing"
    validator = get_named_validator("core/reporting-consumer-status.json", version="3.2.0-rc.6")
    assert validator is not None
    assert list(validator.iter_errors(status)) == []
    raw["consumer_statuses"] = [status]
    for owner in raw["periods"]:
        owner.update(
            consumer_status_count=1, current_consumer_status_id=status["reporting_status_id"]
        )
    ledger = await _load_distinct(raw, explicit)
    result = evaluate_reporting_ledger(ledger, expected_periods=[], now=NOW)
    assert not result.definitive
    assert result.missing_expected_periods == []
    assert all("UNVERIFIED_LEDGER_SNAPSHOT" in outcome.reasons for outcome in result.obligations)
    # Resolving ambiguity must never rewrite the retained legacy statement.
    assert ledger.consumer_statuses[0].reporting_obligation_id is None
    assert ledger.consumer_statuses[0].reporting_revision_id is None


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("binding", ["owner", "revision"])
async def test_exact_status_binding_counts_once_even_when_logical_keys_match(explicit, binding):
    raw = _distinct_histories(2)
    status = _with_status(_history())["consumer_statuses"][0]
    status["reporting_revision_id"] = raw["revisions"][0]["reporting_revision_id"]
    if binding == "owner":
        status["reporting_obligation_id"] = raw["periods"][0]["reporting_obligation_id"]
    else:
        # Pure already-typed legacy tolerance; rc6 received-status wire still
        # requires the owner, and these tests claim no mounted compatibility.
        status.pop("reporting_obligation_id")
    raw["consumer_statuses"] = [status]
    raw["periods"][0].update(
        consumer_status_count=1, current_consumer_status_id=status["reporting_status_id"]
    )
    raw["periods"][1]["consumer_status_count"] = 0
    result = evaluate_reporting_ledger(
        await _load_distinct(raw, explicit), expected_periods=[], now=NOW
    )
    assert result.definitive, result.obligations


@pytest.mark.parametrize("explicit", [False, True])
async def test_an_explicit_current_status_cannot_be_claimed_by_a_second_owner(explicit):
    raw = _distinct_histories(2, distinction="identical")
    status = _with_status(_history())["consumer_statuses"][0]
    status.update(
        reporting_obligation_id=raw["periods"][0]["reporting_obligation_id"],
        reporting_revision_id=raw["revisions"][0]["reporting_revision_id"],
    )
    raw["consumer_statuses"] = [status]
    for owner in raw["periods"]:
        owner.update(
            consumer_status_count=1, current_consumer_status_id=status["reporting_status_id"]
        )
    with pytest.raises(ReportingReconciliationError):
        await _load_distinct(raw, explicit)


@pytest.mark.parametrize("explicit", [False, True])
async def test_exact_status_owner_and_revision_binding_cannot_disagree(explicit):
    raw = _distinct_histories(2, distinction="identical")
    status = _with_status(_history())["consumer_statuses"][0]
    status.update(
        reporting_obligation_id=raw["periods"][0]["reporting_obligation_id"],
        reporting_revision_id=raw["revisions"][1]["reporting_revision_id"],
    )
    raw["consumer_statuses"] = [status]
    raw["periods"][0].update(
        consumer_status_count=1, current_consumer_status_id=status["reporting_status_id"]
    )
    raw["periods"][1]["consumer_status_count"] = 0
    with pytest.raises(ReportingReconciliationError) as error:
        await _load_distinct(raw, explicit)
    assert error.value.code == "INVALID_LEDGER_DEPENDENCY"


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "materialization_count",
        "successful_materialization_count",
        "receipt_count",
        "accepted_receipt_count",
        "adjustment_count",
        "adjustment_receipt_count",
        "accepted_adjustment_receipt_count",
    ],
)
async def test_absent_optional_count_is_diagnostic_but_contradictory_count_is_rejected(
    explicit, field
):
    raw = _history(adjustments=True)
    raw["periods"][0]["health"] = "waiting"
    raw["periods"][0]["schedule"].update(
        period_anchor=raw["periods"][0]["period"]["start"], period_timezone="UTC"
    )
    actual = raw["periods"][0].pop(field)
    # These tier counts are schema-optional; missing evidence is not a false count.
    # Keep the conditional requirements for healthy/complete projections intact.
    validator = get_named_validator("core/reporting-obligation.json", version="3.2.0-rc.6")
    assert validator is not None
    assert list(validator.iter_errors(raw["periods"][0])) == []
    result = evaluate_reporting_ledger(
        await _load(raw, explicit=explicit), expected_periods=_expected(), now=NOW
    )
    assert not result.definitive
    assert "ASSOCIATED_HISTORY_INCOMPLETE" in result.obligations[0].reasons
    assert result.missing_expected_periods == []
    raw["periods"][0][field] = actual + 1
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw, explicit=explicit)
    assert error.value.code == "LEDGER_COUNT_MISMATCH"


def _partly_owned_legacy_history() -> dict[str, Any]:
    raw = _history(adjustments=True)
    first = raw["periods"][0]
    first["revision_count"] = 2
    second = deepcopy(first)
    second.update(
        reporting_obligation_id="obligation-second",
        reconciliation_mode="delivery_only",
        reconciliation_status="not_required",
        health="waiting",
        revision_count=1,
        materialization_count=0,
        successful_materialization_count=0,
        receipt_count=0,
        accepted_receipt_count=0,
        adjustment_count=0,
        adjustment_receipt_count=0,
        accepted_adjustment_receipt_count=0,
    )
    raw["periods"].append(second)
    unbound = deepcopy(raw["revisions"][0])
    unbound.update(reporting_revision_id="revision-unbound", finality="snapshot")
    for key in ("finalized_at", "finality_basis", "finality_policy_id"):
        unbound.pop(key)
    raw["revisions"].append(unbound)
    return raw


@pytest.mark.parametrize(
    ("field", "declared"),
    [
        ("revision_count", 0),
        ("revision_count", 3),
        ("adjustment_count", 0),
        ("adjustment_count", 2),
        ("adjustment_receipt_count", 0),
        ("adjustment_receipt_count", 2),
        ("accepted_adjustment_receipt_count", 0),
        ("accepted_adjustment_receipt_count", 2),
    ],
)
async def test_legacy_ambiguity_does_not_hide_counts_outside_proven_bounds(field, declared):
    raw = _partly_owned_legacy_history()
    # The first revision and its adjustment evidence have an exact materialization
    # owner. Only the extra snapshot can belong to either identical-scope period.
    raw["periods"][0][field] = declared
    with pytest.raises(ReportingReconciliationError) as error:
        await _load_distinct(raw, explicit=False)
    assert error.value.code == "LEDGER_COUNT_MISMATCH"


@pytest.mark.parametrize("declared", [1, 2])
async def test_legacy_count_inside_proven_bounds_remains_diagnostic(declared):
    raw = _partly_owned_legacy_history()
    raw["periods"][0]["revision_count"] = declared
    result = evaluate_reporting_ledger(
        await _load_distinct(raw, explicit=False), expected_periods=_expected(), now=NOW
    )
    assert not result.definitive
    assert result.missing_expected_periods == []
    assert all("AMBIGUOUS_REVISION_OWNERSHIP" in item.reasons for item in result.obligations)


async def test_pending_adjustment_count_cannot_invent_ownership_of_selected_official():
    raw = _partly_owned_legacy_history()
    owned, unbound = raw["revisions"]
    unbound["finality"] = "official"
    owned["finality"] = "snapshot"
    for key in ("finalized_at", "finality_basis", "finality_policy_id"):
        unbound[key] = owned.pop(key)
    raw["adjustments"][0]["adjusts_reporting_revision_id"] = unbound["reporting_revision_id"]
    raw["adjustment_receipts"] = []
    for owner in raw["periods"]:
        owner.update(
            adjustment_receipt_count=0,
            accepted_adjustment_receipt_count=0,
            pending_adjustment_count=0,
        )
    result = evaluate_reporting_ledger(
        await _load_distinct(raw, explicit=False), expected_periods=[], now=NOW
    )
    assert not result.definitive
    assert all("AMBIGUOUS_REVISION_OWNERSHIP" in item.reasons for item in result.obligations)
    # Even ambiguous ownership cannot explain more pending rows than exist.
    raw["periods"][0]["pending_adjustment_count"] = 2
    with pytest.raises(ReportingReconciliationError) as error:
        await _load_distinct(raw, explicit=False)
    assert error.value.code == "LEDGER_COUNT_MISMATCH"


@pytest.mark.parametrize("explicit", [False, True])
async def test_ownership_extension_does_not_make_absent_adjustment_count_an_error(explicit):
    raw = _distinct_histories(1)
    raw["periods"][0].pop("adjustment_count")
    result = evaluate_reporting_ledger(
        await _load_distinct(raw, explicit), expected_periods=[], now=NOW
    )
    assert not result.definitive
    assert "ASSOCIATED_HISTORY_INCOMPLETE" in result.obligations[0].reasons


@pytest.mark.parametrize("explicit", [False, True])
async def test_disabled_consumer_status_mode_does_not_require_optional_status_counts(explicit):
    raw = _history()
    assert "consumer_status_count" not in raw["periods"][0]
    result = evaluate_reporting_ledger(
        await _load(raw, explicit=explicit), expected_periods=_expected(), now=NOW
    )
    assert result.definitive


@pytest.mark.parametrize("finality", [["official"], ["snapshot"], []])
async def test_unproven_expected_finality_suppresses_absence_without_certifying_success(finality):
    raw = _history()
    raw["scope"]["finality"] = finality
    raw.update({name: [] for name in ARRAYS})
    raw["pagination"]["total_count"] = 0
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=_expected(), now=NOW)
    assert result.missing_expected_periods == []
    assert not result.definitive


async def test_official_only_scope_still_certifies_positive_returned_evidence():
    raw = _history()
    assert raw["scope"]["finality"] == ["official"]
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=_expected(), now=NOW)
    assert result.definitive
    # Missing a different profile is an unproven expectation, never a success.
    unproven = replace(_expected()[0], reporting_profile="other-profile")
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=[unproven], now=NOW)
    assert not result.definitive
    assert result.missing_expected_periods == []


@pytest.mark.parametrize("malformed", ["generation", "account", "pagination"])
async def test_request_construction_failure_has_a_distinct_closed_code_and_no_remote_call(
    malformed,
):
    client = Pages(_pages(_history()))
    changes = {
        "generation": {"delivery_config_ids": [""]},
        "account": {"account": SECRET},
        "pagination": {"pagination": SECRET},
    }[malformed]
    request = _request(context={"private": SECRET}).model_copy(update=changes)
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(client, request)
    assert error.value.code == "INVALID_STATUS_REQUEST"
    assert client.requests == []
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None


async def test_response_validation_failure_stays_remote_typed_and_redacted():
    raw = _with_status(_history())
    raw["consumer_statuses"][0]["reporting_status_id"] = f"{SECRET}/invalid"
    client = Pages(_pages(raw))
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(client, _request())
    assert error.value.code == "STATUS_READ_FAILED"
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None


@pytest.mark.parametrize("count", [20, 80])
@pytest.mark.parametrize("explicit", [False, True])
async def test_public_read_and_evaluation_do_not_rescan_unrelated_histories(
    monkeypatch, count, explicit
):
    from adcp.reporting import _reconcile

    comparisons = 0
    original = _reconcile._revision_matches_obligation

    def counted(revision, obligation):
        nonlocal comparisons
        comparisons += 1
        return original(revision, obligation)

    monkeypatch.setattr(_reconcile, "_revision_matches_obligation", counted)
    raw = _distinct_histories(count)
    result = evaluate_reporting_ledger(
        await _load_distinct(raw, explicit), expected_periods=[], now=NOW
    )
    assert result.definitive, result.obligations
    # Work grows with records, not all obligation/revision pairs; no time threshold.
    assert comparisons <= 32 * count


async def test_ambiguous_legacy_fanout_has_a_work_bound_as_well_as_a_row_bound():
    raw = _distinct_histories(450, distinction="identical")
    raw["materializations"] = []
    for owner in raw["periods"]:
        owner.update(
            revision_count=450,
            materialization_count=0,
            successful_materialization_count=0,
            health="waiting",
        )
    raw["pagination"]["total_count"] = 900
    # The 900 received rows could otherwise expand into over 200,000 ambiguous
    # owner/revision associations despite fitting the ordinary transport budget.
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(
            Pages(_pages(raw, explicit=False)),
            _request(pagination={"max_results": 1}),
            max_records=1000,
        )
    assert error.value.code == "LEDGER_LIMIT_EXCEEDED"
