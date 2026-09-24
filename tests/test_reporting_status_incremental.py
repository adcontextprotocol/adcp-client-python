"""Incremental status reads retain their boundary and continuation selection."""

from typing import Any, cast

import pytest

from adcp.reporting.ledger import LedgerConflictError, ReportingStatusHandler
from adcp.reporting.ledger.status_snapshot import ReportingStatusParticipant
from adcp.reporting.ledger.store import ReportingLedgerStore
from tests.test_reporting_ledger import CALLER, _configuration, _obligation, _revision, _seeded


class LegacyStore:
    """An adopter with the original ledger API, without the optional participant."""

    def __init__(self, store):
        self.store = store

    def __getattr__(self, name: str) -> Any:
        if name in {"read_status_snapshot", "record_consumer_status_with_lifecycle"}:
            raise AttributeError(name)
        return getattr(self.store, name)


async def test_legacy_incremental_repair_never_omits_a_later_obligation():
    store, first = await _seeded()
    second = await store.commit_obligation(_obligation(_configuration(), ordinal=2))
    for ordinal, obligation in enumerate((first, second), 1):
        revision, rows = _revision(obligation, reporting_revision_id=f"old_revision_{ordinal}")
        await store.commit_revision(revision, rows)
    legacy = LegacyStore(store)
    assert not isinstance(legacy, ReportingStatusParticipant)
    handler = ReportingStatusHandler(cast(ReportingLedgerStore, legacy), page_size=1)
    before = await handler.handle({"view": "periods"}, caller=CALLER)
    later = await store.commit_obligation(_obligation(_configuration(), ordinal=3))
    request = {"view": "periods", "changes_after": before["changes_checkpoint"]}
    seen = set()
    while True:
        page = await handler.handle(request, caller=CALLER)
        seen.update(p["reporting_obligation_id"] for p in page["periods"])
        if not page["pagination"]["has_more"]:
            break
        request = {**request, "pagination": {"cursor": page["pagination"]["cursor"]}}
    # Older immutable records may replay; losing the new record is forbidden.
    assert later.reporting_obligation_id in seen


async def test_incremental_cursor_keeps_its_lower_bound_when_request_omits_it():
    store, _ = await _seeded()
    handler = ReportingStatusHandler(store, page_size=1)
    before = await handler.handle({"view": "periods"}, caller=CALLER)
    expected = []
    for ordinal in range(2, 5):
        record = await store.commit_obligation(_obligation(_configuration(), ordinal=ordinal))
        expected.append(record.reporting_obligation_id)
    request = {"view": "periods", "changes_after": before["changes_checkpoint"]}
    seen = []
    while True:
        page = await handler.handle(request, caller=CALLER)
        assert page["pagination"]["total_count"] == len(expected)
        seen.extend(p["reporting_obligation_id"] for p in page["periods"])
        if not page["pagination"]["has_more"]:
            break
        request = {"view": "periods", "pagination": {"cursor": page["pagination"]["cursor"]}}
    assert seen == expected


async def test_incremental_cursor_rejects_a_different_explicit_lower_bound():
    store, _ = await _seeded()
    handler = ReportingStatusHandler(store, page_size=1)
    before = await handler.handle({"view": "periods"}, caller=CALLER)
    for ordinal in (2, 3):
        await store.commit_obligation(_obligation(_configuration(), ordinal=ordinal))
    first = await handler.handle(
        {"view": "periods", "changes_after": before["changes_checkpoint"]}, caller=CALLER
    )
    with pytest.raises(LedgerConflictError, match="lower bound"):
        await handler.handle(
            {
                "view": "periods",
                "changes_after": first["changes_checkpoint"],
                "pagination": {"cursor": first["pagination"]["cursor"]},
            },
            caller=CALLER,
        )


async def test_intervening_write_still_refuses_to_blend_snapshot_boundaries():
    store, _ = await _seeded()
    await store.commit_obligation(_obligation(_configuration(), ordinal=2))
    handler = ReportingStatusHandler(store, page_size=1)
    first = await handler.handle({"view": "periods"}, caller=CALLER)
    await store.commit_obligation(_obligation(_configuration(), ordinal=3))
    with pytest.raises(LedgerConflictError) as error:
        await handler.handle(
            {"view": "periods", "pagination": {"cursor": first["pagination"]["cursor"]}},
            caller=CALLER,
        )
    assert error.value.code == "CURSOR_SNAPSHOT_MISMATCH"


async def test_legacy_load_does_not_pull_revisions_past_its_page_boundary():
    store, obligation = await _seeded()
    revision, rows = _revision(obligation)

    class WriteAfterBoundary(LegacyStore):
        async def open_snapshot(self, **kwargs):
            boundary = await self.store.open_snapshot(**kwargs)
            await self.store.commit_revision(revision, rows)
            return boundary

    first = await ReportingStatusHandler(
        cast(ReportingLedgerStore, WriteAfterBoundary(store)), page_size=1
    ).handle({"view": "periods"}, caller=CALLER)
    assert first["revisions"] == []
    assert first["pagination"]["total_count"] == 1
    later = await ReportingStatusHandler(cast(ReportingLedgerStore, LegacyStore(store))).handle(
        {"view": "periods", "changes_after": first["changes_checkpoint"]}, caller=CALLER
    )
    assert [r["reporting_revision_id"] for r in later["revisions"]] == [
        revision.reporting_revision_id
    ]
