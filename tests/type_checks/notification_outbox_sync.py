"""Synchronous transaction-bound publication uses public typed APIs (#1607)."""

from typing import Any

from psycopg import Connection
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typing_extensions import assert_type

from adcp import PgNotificationOutbox, PreparedWebhook


def publish_psycopg(
    outbox: PgNotificationOutbox, conn: Connection[Any], prepared: PreparedWebhook
) -> int:
    with conn.transaction():
        conn.execute("UPDATE accounts SET status='active' WHERE id=%s", ("acct_1",))
        row_id = outbox.enqueue_prepared_sync(
            conn,
            prepared,
            notification_type="account.status_changed",
            caller_scope_id="seller-tenant-1",
            account_id="acct_1",
        )
    assert_type(row_id, int)
    return row_id


def publish_sqlalchemy(
    outbox: PgNotificationOutbox, engine: Engine, prepared: PreparedWebhook
) -> int:
    with engine.begin() as conn:
        conn.execute(text("UPDATE accounts SET status='active' WHERE id=:id"), {"id": "acct_1"})
        return outbox.enqueue_prepared_sync(
            conn,
            prepared,
            notification_type="account.status_changed",
            caller_scope_id="seller-tenant-1",
            account_id="acct_1",
        )


def publish_in_unit_of_work(
    outbox: PgNotificationOutbox, session: Session, prepared: PreparedWebhook
) -> int:
    with session.begin():
        return outbox.enqueue_prepared_sync(
            session.connection(),
            prepared,
            notification_type="principal.changed",
            caller_scope_id="seller-tenant-1",
            signing_scope_id="buyer-tenant-1",
        )
