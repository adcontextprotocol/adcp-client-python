"""Transaction-bound publication and worker signing use public typed APIs."""

from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from adcp import PgNotificationOutbox, PreparedWebhook, WebhookSenderResolver


def create(pool: AsyncConnectionPool, resolver: WebhookSenderResolver) -> PgNotificationOutbox:
    return PgNotificationOutbox(
        pool=pool,
        sender_resolver=resolver,
        encryption_key=b"x" * 32,
        delivery_retry_horizon_seconds=86400,
    )


async def publish(
    outbox: PgNotificationOutbox, conn: AsyncConnection[Any], prepared: PreparedWebhook
) -> int:
    async with conn.transaction():
        return await outbox.enqueue_prepared(
            conn,
            prepared,
            notification_type="principal.changed",
            caller_scope_id="publisher-tenant-1",
            signing_scope_id="buyer-tenant-1",
        )


async def worker(outbox: PgNotificationOutbox) -> None:
    await outbox.create_schema()
    await outbox.run_worker()
