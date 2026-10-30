"""``postgres_storage``: every reporting store over one PostgreSQL pool (experimental).

The durable half of ``adcp.storage``. One object holds the reporting ledger,
staged source objects and seals, ``sync_principal`` principal records, and the
reporting-subscription engine state (grant journal, exceptions, version
snapshots)::

    from adcp.reporting import ReliableReportingService, SubscriptionOptions
    from adcp.storage.postgres import postgres_storage

    storage = postgres_storage(dsn=os.environ["DATABASE_URL"])  # or pool=...
    reporting = ReliableReportingService(
        storage=storage,
        account_context=resolve_account_context,
        subscriptions=SubscriptionOptions(max_accounts=500),
    )

The schema is managed by versioned migrations (:mod:`adcp_reporting_storage.migrations`):
run ``python -m adcp migrate`` before starting. Startup never changes the
schema; it refuses one that is behind with ``REPORTING_SCHEMA_BEHIND``, naming
the command and the pending versions. Durability is a property of the
storage, never a flag.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

__all__ = ["postgres_storage"]


def postgres_storage(
    *,
    pool: Any = None,
    dsn: str | None = None,
    clock: Callable[[], datetime] | None = None,
    max_size: int = 10,
) -> Any:
    """Durable :class:`adcp.storage.ReportingStorage` over PostgreSQL.

    Pass a borrowed psycopg ``AsyncConnectionPool`` (``pool=``), whose
    ``search_path`` selects the schema, or a ``dsn`` for a pool the storage
    opens at startup and closes at shutdown.
    """
    from adcp.reporting import ReliableReportingConfigurationError
    from adcp.storage import ReportingStorage

    if (pool is None) == (dsn is None):
        raise ValueError("postgres_storage needs exactly one of pool= or dsn=")
    owned = pool is None
    if dsn is not None:
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(dsn, min_size=1, max_size=max_size, open=False)

    from adcp_reporting_storage.inline_storage import PgReportingSealStore, PgReportingStagingStore
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import MIGRATE_COMMAND, pending_migrations
    from adcp_reporting_storage.subscriptions.pg import (
        PgPrincipalRecordStore,
        PgReportingSubscriptionStateStore,
    )

    staging = PgReportingStagingStore(pool=pool)
    opened = False

    async def open_pool() -> None:
        nonlocal opened
        if owned and not opened:
            await pool.open(wait=True)
            opened = True

    async def check_schema() -> None:
        await open_pool()
        pending = await pending_migrations(pool)
        if pending:
            versions = ", ".join(item.version for item in pending)
            stopped = [item.version for item in pending if item.stopped_worker]
            note = (
                f"; {', '.join(stopped)} is a stopped-worker upgrade, so stop every reporting "
                "worker first"
                if stopped
                else ""
            )
            raise ReliableReportingConfigurationError(
                f"the reporting schema is behind this SDK (pending: {versions}). "
                f'Run "{MIGRATE_COMMAND}" against this database{note}.',
                code="REPORTING_SCHEMA_BEHIND",
            )
        await staging.check_ready()

    async def close_pool() -> None:
        nonlocal opened
        if owned and opened:
            await pool.close()
            opened = False

    return ReportingStorage(
        durability="durable",
        ledger=PgReportingLedgerStore(pool=pool, clock=clock),
        principals=PgPrincipalRecordStore(pool=pool),
        subscriptions=PgReportingSubscriptionStateStore(pool=pool),
        staging=staging,
        seals=PgReportingSealStore(pool=pool),
        check_schema=check_schema,
        close=close_pool,
        pool=pool,
    )
