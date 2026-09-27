"""Store-independent restart assertions for the public reporting test kit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger import (
    ReportingObligationRecord,
    ReportingRevisionRecord,
    revision_content_sha256,
)
from adcp.reporting.ledger.store import ReportingLedgerStore


@dataclass(frozen=True)
class ReportingLifecycleSnapshot:
    """An obligation, its exact revisions, and detached canonical row bytes.

    Capture a quiescent obligation after publication, then compare it after
    closing/rebuilding the service and retrying the same work. The assertion
    checks every revision, so an extra publication is a failure too. This is
    evidence about the supplied store, not a durability or tier certification.
    """

    obligation: ReportingObligationRecord
    revisions: tuple[ReportingRevisionRecord, ...]
    rows: tuple[bytes, ...]


async def capture_reporting_lifecycle(
    store: ReportingLedgerStore,
    *,
    account_id: str,
    reporting_obligation_id: str,
    page_size: int = 500,
    max_pages: int = 1000,
) -> ReportingLifecycleSnapshot:
    """Read and verify one published obligation through public store methods.

    Works with in-memory, PostgreSQL, or adopter stores. All pages must repeat
    the exact revision identity/count and exhaust within ``max_pages``. The
    complete walk is hashed against the committed Core binding, including the
    typed control totals when the publisher supplied managed evidence.

    Keep workers quiescent during capture/replay comparison. For memory tests,
    retain the original stores across service instances; that models service
    recreation only. PostgreSQL tests should create fresh store instances over
    the retained database. No private state is copied or repaired by the kit.
    """
    if type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError("page_size must be an integer between 1 and 500")
    if type(max_pages) is not int or max_pages <= 0:
        raise ValueError("max_pages must be a positive integer")
    obligation = await store.get_obligation(
        account_id=account_id, reporting_obligation_id=reporting_obligation_id
    )
    if obligation is None:
        raise AssertionError("reporting obligation did not survive lifecycle recovery")
    if (obligation.account_id, obligation.reporting_obligation_id) != (
        account_id,
        reporting_obligation_id,
    ):
        raise AssertionError("obligation read returned a different account or identity")
    revisions = tuple(
        sorted(
            await store.list_revisions(
                account_id=account_id, reporting_obligation_id=reporting_obligation_id
            ),
            key=lambda item: item.reporting_revision_id,
        )
    )
    if not revisions:
        raise AssertionError("lifecycle conformance requires at least one published revision")
    if len({item.reporting_revision_id for item in revisions}) != len(revisions):
        raise AssertionError("revision listing repeated an identity")
    contents: list[bytes] = []
    for revision in revisions:
        revision_id = revision.reporting_revision_id
        if (revision.account_id, revision.reporting_obligation_id) != (
            account_id,
            reporting_obligation_id,
        ):
            raise AssertionError("revision read returned a different account or obligation")
        exact = await store.get_revision(account_id=account_id, reporting_revision_id=revision_id)
        if exact != revision:
            raise AssertionError("revision listing disagrees with the exact revision read")
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(max_pages):
            page = await store.read_revision_rows(
                account_id=account_id,
                reporting_revision_id=revision_id,
                cursor=cursor,
                limit=page_size,
            )
            if (
                page.reporting_revision_id != revision_id
                or type(page.total_count) is not int
                or page.total_count != revision.row_count
                or type(page.has_more) is not bool
                or len(page.rows) > page_size
            ):
                raise AssertionError("revision page changed its identity, count, or page bound")
            rows.extend(page.rows)
            if len(rows) > revision.row_count:
                raise AssertionError("revision walk returned more rows than its binding")
            if not page.has_more:
                if page.cursor is not None:
                    raise AssertionError("final revision page retained a continuation cursor")
                break
            if not page.rows or not isinstance(page.cursor, str) or not page.cursor:
                raise AssertionError("revision continuation made no progress")
            if page.cursor in seen:
                raise AssertionError("revision continuation repeated a cursor")
            seen.add(page.cursor)
            cursor = page.cursor
        else:
            raise AssertionError("revision walk exceeded max_pages")
        if len(rows) != revision.row_count:
            raise AssertionError("revision walk ended before all bound rows were read")
        digest = revision_content_sha256(
            reporting_revision_id=revision_id,
            row_count=revision.row_count,
            control_totals=revision.control_totals,
            control_total_evidence=revision.managed_control_totals,
            reporting_rows=rows,
        )
        if digest != revision.revision_content_sha256:
            raise AssertionError("revision rows do not match the committed content binding")
        contents.append(canonical_json_utf8_v1(rows))
    return ReportingLifecycleSnapshot(obligation, revisions, tuple(contents))


async def assert_reporting_lifecycle_replay(
    expected: ReportingLifecycleSnapshot,
    store: ReportingLedgerStore,
    *,
    page_size: int = 500,
    max_pages: int = 1000,
) -> None:
    """Assert no lost, changed, or extra publication after restart and retry."""
    actual = await capture_reporting_lifecycle(
        store,
        account_id=expected.obligation.account_id,
        reporting_obligation_id=expected.obligation.reporting_obligation_id,
        page_size=page_size,
        max_pages=max_pages,
    )
    if actual != expected:
        raise AssertionError("reporting obligation, revisions, or row bytes changed after replay")
