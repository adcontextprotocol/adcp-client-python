"""``get_reporting_status`` periods pages honour the buyer's page size and a node budget.

A periods record names every media buy in its configuration generation, so a
page's size grows with the buys per generation, not only with the record count.
A page the buyer's SDK cannot parse is as useless as no page, so the handler
bounds each page by rendered size as well as by ``pagination.max_results``.
Every shorter page is a correct step of the walk: the cursor carries an absolute
offset under one ``ledger_snapshot_id``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from adcp.reporting.ledger import (
    DEFAULT_STATUS_PAGE_NODE_BUDGET,
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ReportingStatusHandler,
)
from adcp.validation import DEFAULT_MAX_PAYLOAD_NODES, count_payload_nodes
from tests.test_reporting_ledger import CALLER, _configuration, _obligation, _revision, _seeded

_RECORD_KEYS = ("periods", "revisions", "adjustments", "consumer_statuses")


def _record_ids(page: dict[str, Any]) -> list[str]:
    ids = [p["reporting_obligation_id"] for p in page["periods"]]
    ids += [r["reporting_revision_id"] for r in page["revisions"]]
    ids += [a["reporting_adjustment_id"] for a in page["adjustments"]]
    return ids


def _records_on(page: dict[str, Any]) -> int:
    return sum(len(page.get(key) or ()) for key in _RECORD_KEYS)


async def _walk(handler: ReportingStatusHandler, request: dict[str, Any]) -> list[dict[str, Any]]:
    pages = [await handler.handle(request, caller=CALLER)]
    while pages[-1]["pagination"]["has_more"]:
        cursor = pages[-1]["pagination"]["cursor"]
        pagination = {**(request.get("pagination") or {}), "cursor": cursor}
        pages.append(await handler.handle({**request, "pagination": pagination}, caller=CALLER))
    return pages


def _store() -> InMemoryReportingLedgerStore:
    # Pinned well past every deadline, as ``_seeded`` does, so health is settled.
    return InMemoryReportingLedgerStore(clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))


async def _ledger(obligations: int, *, buys: int = 1) -> InMemoryReportingLedgerStore:
    """A ledger of ``obligations`` periods of one generation, each with one revision."""
    store = _store()
    configuration = _configuration(media_buy_ids=tuple(f"mb_{i:03d}" for i in range(buys)))
    await store.put_configuration(configuration)
    for ordinal in range(1, obligations + 1):
        obligation = await store.commit_obligation(_obligation(configuration, ordinal=ordinal))
        revision, rows = _revision(obligation, reporting_revision_id=f"rpr_{ordinal}")
        await store.commit_revision(revision, rows)
    return store


async def _large_generation_ledger() -> InMemoryReportingLedgerStore:
    """Forty periods of an 80-buy generation, the shape that overflowed on staging."""
    return await _ledger(40, buys=80)


async def test_max_results_bounds_the_page_and_the_walk_returns_every_record_once() -> None:
    store = await _ledger(5)
    handler = ReportingStatusHandler(store)
    pages = await _walk(handler, {"view": "periods", "pagination": {"max_results": 3}})

    assert [_records_on(p) for p in pages] == [3, 3, 3, 1]
    seen = [record for page in pages for record in _record_ids(page)]
    assert len(seen) == len(set(seen)) == 10
    assert {p["ledger_snapshot_id"] for p in pages} == {pages[0]["ledger_snapshot_id"]}
    assert {p["pagination"]["total_count"] for p in pages} == {10}


async def test_an_omitted_max_results_uses_the_schema_default_of_50() -> None:
    store = await _ledger(30)
    page = await ReportingStatusHandler(store).handle({"view": "periods"}, caller=CALLER)
    assert _records_on(page) == 50
    assert page["pagination"]["has_more"] is True
    assert page["pagination"]["total_count"] == 60


async def test_max_results_of_100_is_served_in_full() -> None:
    store = await _ledger(60)
    page = await ReportingStatusHandler(store).handle(
        {"view": "periods", "pagination": {"max_results": 100}}, caller=CALLER
    )
    assert _records_on(page) == 100
    assert page["pagination"]["has_more"] is True


async def test_the_sellers_page_size_caps_what_a_buyer_may_ask_for() -> None:
    store = await _ledger(5)
    handler = ReportingStatusHandler(store, page_size=2)
    assert handler.page_size == 2
    page = await handler.handle(
        {"view": "periods", "pagination": {"max_results": 50}}, caller=CALLER
    )
    assert _records_on(page) == 2


@pytest.mark.parametrize("max_results", [0, 101, -1, "5", 2.5, True])
async def test_a_max_results_outside_the_schema_is_refused(max_results: Any) -> None:
    store = await _ledger(2)
    with pytest.raises(LedgerConflictError) as error:
        await ReportingStatusHandler(store).handle(
            {"view": "periods", "pagination": {"max_results": max_results}}, caller=CALLER
        )
    assert error.value.code == "INVALID_PAGE_SIZE"


async def test_a_page_of_a_large_generation_stays_within_the_node_budget() -> None:
    store = await _large_generation_ledger()
    handler = ReportingStatusHandler(store)
    assert handler.page_node_budget == DEFAULT_STATUS_PAGE_NODE_BUDGET
    request = {"view": "periods", "pagination": {"max_results": 100}}

    unbounded = await ReportingStatusHandler(store, page_node_budget=None).handle(
        request, caller=CALLER
    )
    # Without the budget this page is the staging failure: over the SDK limit.
    assert count_payload_nodes(unbounded) >= DEFAULT_MAX_PAYLOAD_NODES

    pages = await _walk(handler, request)
    assert len(pages) > 1
    for page in pages:
        assert count_payload_nodes(page) <= DEFAULT_STATUS_PAGE_NODE_BUDGET
    seen = [record for page in pages for record in _record_ids(page)]
    assert len(seen) == len(set(seen)) == unbounded["pagination"]["total_count"] == 80
    assert {p["ledger_snapshot_id"] for p in pages} == {unbounded["ledger_snapshot_id"]}
    assert {p["changes_checkpoint"] for p in pages} == {unbounded["changes_checkpoint"]}


async def test_an_explicit_node_budget_shortens_pages_without_losing_records() -> None:
    store = await _ledger(10, buys=20)
    handler = ReportingStatusHandler(store, page_node_budget=1_500)
    pages = await _walk(handler, {"view": "periods", "pagination": {"max_results": 100}})
    assert len(pages) > 1
    assert all(count_payload_nodes(p) <= 1_500 for p in pages)
    seen = [record for page in pages for record in _record_ids(page)]
    assert len(seen) == len(set(seen)) == 20


async def test_a_record_larger_than_the_budget_is_still_served_one_per_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = await _ledger(2, buys=20)
    handler = ReportingStatusHandler(store, page_node_budget=10)
    with caplog.at_level(logging.WARNING, logger="adcp.reporting.ledger.status"):
        pages = await _walk(handler, {"view": "periods"})
    assert [_records_on(p) for p in pages] == [1, 1, 1, 1]
    seen = [record for page in pages for record in _record_ids(page)]
    assert len(seen) == len(set(seen)) == 4
    assert any("page node budget" in r.getMessage() for r in caplog.records)


async def test_a_budget_of_none_bounds_pages_by_record_count_only() -> None:
    store = await _ledger(10, buys=20)
    page = await ReportingStatusHandler(store, page_node_budget=None).handle(
        {"view": "periods", "pagination": {"max_results": 100}}, caller=CALLER
    )
    assert _records_on(page) == 20


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_size": 0},
        {"page_size": True},
        {"page_size": "10"},
        {"page_node_budget": 0},
        {"page_node_budget": False},
        {"page_node_budget": 1.5},
    ],
)
async def test_invalid_handler_limits_are_refused(kwargs: dict[str, Any]) -> None:
    store, _ = await _seeded()
    with pytest.raises(ValueError):
        ReportingStatusHandler(store, **kwargs)


def test_the_default_budget_leaves_room_under_the_buyers_validation_limit() -> None:
    assert DEFAULT_STATUS_PAGE_NODE_BUDGET == DEFAULT_MAX_PAYLOAD_NODES * 4 // 5
