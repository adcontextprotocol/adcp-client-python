"""Isolated v2 status queues using the original SDK fanout/delivery transactions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from adcp.reporting.outbox.status_pg import PgReportingStatusOutbox


class _ProjectionQueueConnection:
    """Closed identifiers only; shares the caller's actual connection/transaction."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def transaction(self) -> Any:
        return self.connection.transaction()

    async def execute(self, query: str, params: Any = None) -> Any:
        for old, new in (
            ("reporting_status_notification_", "reporting_projection_notification_"),
            ("reporting_status_webhook_", "reporting_projection_webhook_"),
            ("reporting_notification_", "reporting_projection_notification_"),
            ("reporting_webhook_", "reporting_projection_webhook_"),
        ):
            query = query.replace(old, new)
        return await self.connection.execute(query, params)


class PgReportingProjectionOutbox(PgReportingStatusOutbox):
    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as connection:
            yield _ProjectionQueueConnection(connection)

    async def create_schema(self) -> None:
        from adcp.reporting.projection.pg import PgReportingProjectionStore

        await PgReportingProjectionStore(pool=self._pool, notifications=True).create_schema()
