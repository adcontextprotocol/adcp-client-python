"""Ownership is checked across the whole public, bounded periods walk."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from adcp.reporting import (
    ReportingReconciliationError,
    evaluate_reporting_ledger,
    load_reporting_ledger,
)
from adcp.reporting.ownership import (
    ReportingOwnershipError,
    page_revision_ownership,
    with_revision_ownership,
)
from adcp.types import GetReportingStatusRequest, GetReportingStatusResponse
from adcp.types.core import TaskResult, TaskStatus
from tests.test_reporting_reconciliation import REVISION, _obligation, _response

ARRAYS = (
    "periods",
    "revisions",
    "materializations",
    "receipts",
    "adjustments",
    "adjustment_receipts",
)
OWNER = "obligation-billing"
REVISION_ID = REVISION["reporting_revision_id"]


class Pages:
    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    async def get_reporting_status(self, request):
        page = self.pages[self.calls % len(self.pages)]
        self.calls += 1
        return TaskResult(
            status=TaskStatus.COMPLETED,
            data=GetReportingStatusResponse.model_validate(deepcopy(page)),
        )


def pages(raw=None):
    raw = deepcopy(raw or _response())
    owners = {REVISION_ID: OWNER, "revision-other": "obligation-other"}
    records = [(name, record) for name in ARRAYS for record in raw.get(name, [])]
    result = []
    for index, (name, record) in enumerate(records):
        page = {k: v for k, v in raw.items() if k not in ARRAYS}
        page.update({a: [] for a in ARRAYS})
        page[name] = [record]
        page["pagination"] = {"has_more": index + 1 < len(records), "total_count": len(records)}
        if page["pagination"]["has_more"]:
            page["pagination"]["cursor"] = f"cursor-{index}"
        page["changes_checkpoint"] = "constant-checkpoint"
        result.append(with_revision_ownership(page, owners))
    return result


async def load(values, **bounds):
    return await load_reporting_ledger(
        Pages(values),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
        **bounds,
    )


def test_page_local_merge_preserves_keys_and_input_and_explicit_empty_mode():
    raw = {
        "revisions": [{"reporting_revision_id": "r"}],
        "ext": {"vendor": {"a": 1}, "adcp": {"other": "retained"}},
    }
    original = deepcopy(raw)
    result = with_revision_ownership(raw, {"r": "o", "not-on-page": "o"})
    assert raw == original
    assert result["ext"]["vendor"] == {"a": 1}
    assert result["ext"]["adcp"]["other"] == "retained"
    assert page_revision_ownership(result) == {"r": "o"}
    assert with_revision_ownership(result, {"r": "o"}) == result
    assert page_revision_ownership(with_revision_ownership({"revisions": []}, {})) == {}
    assert page_revision_ownership({"revisions": []}) is None


@pytest.mark.parametrize(
    "reserved",
    [
        None,
        [],
        "bad",
        {},
        {"version": True, "bindings": []},
        {"version": 2, "bindings": []},
        {"version": 1, "bindings": {}},
        {"version": 1, "bindings": [], "unknown": 1},
        {
            "version": 1,
            "bindings": [{"reporting_revision_id": "r", "reporting_obligation_id": "o"}],
        },
    ],
)
def test_malformed_reserved_values_and_extra_page_binding_fail(reserved):
    with pytest.raises(ReportingOwnershipError):
        page_revision_ownership(
            {"revisions": [], "ext": {"adcp": {"reporting_revision_ownership": reserved}}}
        )


@pytest.mark.parametrize("ext", [None, [], "bad", {"adcp": None}, {"adcp": []}, {"adcp": "bad"}])
def test_non_object_reserved_namespaces_are_not_legacy(ext):
    with pytest.raises(ReportingOwnershipError):
        with_revision_ownership({"revisions": [], "ext": ext}, {})


def test_duplicate_missing_and_conflicting_binding_are_rejected():
    page = with_revision_ownership({"revisions": [{"reporting_revision_id": "r"}]}, {"r": "o"})
    duplicate = deepcopy(page)
    bindings = duplicate["ext"]["adcp"]["reporting_revision_ownership"]["bindings"]
    bindings.append(deepcopy(bindings[0]))
    with pytest.raises(ReportingOwnershipError):
        page_revision_ownership(duplicate)
    with pytest.raises(ReportingOwnershipError):
        with_revision_ownership(page, {"r": "other"})
    with pytest.raises(ReportingOwnershipError):
        with_revision_ownership({"revisions": [{"reporting_revision_id": "r"}]}, {})


@pytest.mark.parametrize("name", ["revision", "reporting_revision"])
def test_exact_view_checks_supplied_binding_without_proving_an_unknown_owner(name):
    raw = {
        name: {"reporting_revision_id": "r"},
        "reporting_revision_binding": {"reporting_revision_id": "r"},
    }
    page = with_revision_ownership(raw, {"r": "owner-needs-periods-walk"})
    assert page_revision_ownership(page) == {"r": "owner-needs-periods-walk"}
    for other in {"revision", "reporting_revision", "revisions"} - {name}:
        with pytest.raises(ReportingOwnershipError):
            page_revision_ownership({**page, other: []})
    for binding in (None, {}, {"reporting_revision_id": "other"}):
        with pytest.raises(ReportingOwnershipError):
            page_revision_ownership({**page, "reporting_revision_binding": binding})


def test_a2a_struct_numeric_version_preserves_integer_semantics():
    page = with_revision_ownership({"revisions": []}, {})
    reserved = page["ext"]["adcp"]["reporting_revision_ownership"]
    reserved["version"] = 1.0
    assert page_revision_ownership(page) == {}
    for bad in (True, "1", 1.5, float("nan"), float("inf")):
        reserved["version"] = bad
        with pytest.raises(ReportingOwnershipError):
            page_revision_ownership(page)


async def test_page_size_one_dependencies_and_repeated_identical_metadata():
    values = pages()
    # Revisions precede their owner and materialization on this wire walk.
    values[0]["periods"], values[1]["periods"] = [], values[0]["periods"]
    values[0]["revisions"], values[1]["revisions"] = values[1]["revisions"], []
    for i in (0, 1):
        values[i].pop("ext")
        values[i] = with_revision_ownership(values[i], {REVISION_ID: OWNER})
    repeated = deepcopy(values[0])
    repeated["pagination"]["cursor"] = "repeated-revision"
    values.insert(1, repeated)
    ledger = await load(values)
    assert ledger.revision_ownership == {REVISION_ID: OWNER}
    assert len(ledger.revisions) == len(ledger.obligations) == len(ledger.materializations) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "mixed",
        "unknown-owner",
        "foreign-account",
        "semantic",
        "count",
        "missing-revision",
        "changed-owner",
    ],
)
async def test_full_walk_rejects_inconsistent_ownership(mutation):
    values = pages()
    if mutation == "mixed":
        values[-1].pop("ext")
    elif mutation == "unknown-owner":
        values[1]["ext"]["adcp"]["reporting_revision_ownership"]["bindings"][0][
            "reporting_obligation_id"
        ] = "unknown"
    elif mutation == "foreign-account":
        values[0]["periods"][0]["account_id"] = "other-account"
    elif mutation == "semantic":
        values[1]["revisions"][0]["media_buy_ids"] = ["unknown-buy"]
    elif mutation == "count":
        values[0]["periods"][0]["revision_count"] = 2
    elif mutation == "missing-revision":
        values[1]["revisions"] = []
    else:
        extra = deepcopy(values[1])
        extra["pagination"]["cursor"] = "extra"
        extra["ext"]["adcp"]["reporting_revision_ownership"]["bindings"][0][
            "reporting_obligation_id"
        ] = "unknown"
        values.insert(2, extra)
    with pytest.raises(ReportingReconciliationError, match="ownership"):
        await load(values)


async def test_explicit_ownership_separates_identical_scopes_and_legacy_stays_conservative():
    raw = _response()
    raw["periods"].append(_obligation("obligation-other"))
    raw["revisions"].append({**deepcopy(REVISION), "reporting_revision_id": "revision-other"})
    raw["periods"][1].update(
        destination_ref=None,
        materialization_count=None,
        successful_materialization_count=None,
        reconciliation_mode="delivery_only",
        reconciliation_status="not_required",
        health="complete",
    )
    owned = await load(pages(raw))
    result = evaluate_reporting_ledger(owned, now=datetime(2026, 9, 3, tzinfo=timezone.utc))
    assert result.obligations[1].reporting_revision_id == "revision-other"
    assert result.obligations[1].definitive
    legacy_pages = pages(raw)
    for page in legacy_pages:
        page.pop("ext")
    legacy = await load(legacy_pages)
    assert legacy.revision_ownership is None
    assert not evaluate_reporting_ledger(legacy).obligations[1].definitive


@pytest.mark.parametrize("bounds", [{"max_pages": 1}, {"max_records": 1}])
async def test_walk_budgets_are_enforced(bounds):
    with pytest.raises(ReportingReconciliationError) as error:
        await load(pages(), **bounds)
    assert error.value.code == "LEDGER_LIMIT_EXCEEDED"
