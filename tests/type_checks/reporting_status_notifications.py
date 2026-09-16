"""Optional C types preserve the existing custom ledger and outbox protocols."""

from datetime import datetime

from psycopg_pool import AsyncConnectionPool

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    PgReportingLedgerStore,
    ReportingLedgerStore,
    ReportingStatusSnapshot,
    StatusProjectionInput,
    StatusProjectionResult,
    project_status_scope,
)
from adcp.reporting.outbox import (
    InMemoryReportingOutbox,
    InMemoryStatusNotificationStore,
    PgReportingOutbox,
    PgStatusNotificationStore,
    ReportingNotificationOutbox,
    ReportingStatusNotificationLifecycle,
    ReportingStatusProjector,
    ReportingStatusScope,
    ReportingStatusService,
    ReportingStatusSupport,
    ReportingStatusSweeper,
    StatusCheckpoint,
    StatusDueLease,
    StatusNotificationStore,
    StatusTurn,
)


def existing_core() -> ReportingLedgerStore:
    return InMemoryReportingLedgerStore()


def existing_outbox(pool: AsyncConnectionPool) -> ReportingNotificationOutbox:
    return PgReportingOutbox(pool=pool)


def existing_memory_outbox() -> ReportingNotificationOutbox:
    return InMemoryReportingOutbox(InMemoryReportingLedgerStore(notifications=True))


def optional_memory() -> StatusNotificationStore:
    return InMemoryStatusNotificationStore(InMemoryReportingLedgerStore(notifications=True))


def optional_postgres(pool: AsyncConnectionPool) -> StatusNotificationStore:
    return PgStatusNotificationStore(PgReportingLedgerStore(pool=pool, notifications=True))


def pure(snapshot: ReportingStatusSnapshot, scope: ReportingStatusScope) -> StatusProjectionResult:
    return project_status_scope(StatusProjectionInput(snapshot, scope))


async def snapshot_on_store(
    store: PgReportingLedgerStore, account_id: str
) -> ReportingStatusSnapshot:
    snapshot = await store.read_status_snapshot(account_id=account_id)
    at: datetime = snapshot.as_of
    assert at.tzinfo is not None
    return snapshot


async def turns(store: StatusNotificationStore, account_id: str) -> tuple[StatusCheckpoint, ...]:
    await store.create_schema()
    await store.baseline(account_id=account_id)
    dirty: StatusTurn = await ReportingStatusProjector(store).run_once(account_id=account_id)
    due: StatusTurn = await ReportingStatusSweeper(store).run_once(account_id=account_id)
    lease: StatusDueLease | None = await store.claim_due(account_id=account_id, lease_seconds=30)
    if lease is not None:
        await store.release_due(lease)
    assert isinstance(dirty.events + due.events, int)
    return await store.checkpoints(account_id=account_id)


def lifecycle(support: ReportingStatusSupport) -> ReportingStatusNotificationLifecycle:
    return ReportingStatusService(support)
