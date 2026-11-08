"""The reporting-issue observer as a strictly typed adopter writes it (#1258)."""

from datetime import datetime

from adcp.reporting import (
    ReliableReportingService,
    ReportingIssueEvent,
    ReportingIssueEventKind,
    ReportingIssueObserver,
)
from adcp.reporting.ledger import ReportingIssue, ReportingLedgerStore
from adcp.reporting.service import ReportingContextResolver

seen: set[str] = set()


async def on_issue(event: ReportingIssueEvent) -> None:
    # Delivery is at least once: deduplicate on the stable event id.
    if event.event_id in seen:
        return
    seen.add(event.event_id)
    kind: ReportingIssueEventKind = event.kind
    occurred_at: datetime = event.occurred_at
    attempt: int = event.attempt
    issue: ReportingIssue | None = event.issue
    status_id: str | None = event.reporting_status_id
    assert occurred_at.tzinfo is not None and attempt >= 1 and kind
    if issue is not None and issue.responsible_party != "buyer":
        assert issue.recommended_action and issue.issue_id == event.issue_id
    assert status_id is None or status_id


def log_issue(event: ReportingIssueEvent) -> None:
    """A synchronous observer is accepted too."""
    print(event.kind, event.issue_id, event.account_id, event.consumer_id)


def compose(
    store: ReportingLedgerStore, context: ReportingContextResolver, sync: bool
) -> ReliableReportingService:
    observer: ReportingIssueObserver = log_issue if sync else on_issue
    return ReliableReportingService(store=store, account_context=context, issue_observer=observer)
