"""Public activity types and optional handler mounting need no suppressions."""

from datetime import datetime
from typing import Any

from adcp.decisioning.accounts import ResolveContext
from adcp.reporting.outbox import (
    ActivityOutcome,
    ActivityRequest,
    InMemoryReportingOutbox,
    PgReportingOutbox,
    ReportingActivityProjector,
    ReportingActivityStore,
    WebhookAttempt,
    resolve_reporting_consumer,
)
from adcp.reporting.outbox.models import DeliveryLease


def reference_store(
    store: InMemoryReportingOutbox | PgReportingOutbox,
) -> ReportingActivityStore:
    return store


async def reserve_and_complete(
    store: ReportingActivityStore, lease: DeliveryLease, at: datetime
) -> WebhookAttempt | None:
    attempt = await store.reserve_attempt(
        lease, request=ActivityRequest("https://example.test/reporting", 1), now=at
    )
    if attempt is not None:
        await store.complete_attempt(attempt, outcome=ActivityOutcome("success", 200, 1), now=at)
    return attempt


async def authorized_read(
    store: ReportingActivityStore, account_id: str, context: ResolveContext, at: datetime
) -> list[dict[str, Any]]:
    consumer = resolve_reporting_consumer(auth_info=context.auth_info, agent=context.agent)
    await store.purge_activity(account_id=account_id, consumer_id=consumer, now=at)
    return await ReportingActivityProjector(store).for_account(
        account_id=account_id, context=context, limit=50
    )
