"""PostgreSQL stores for AdCP 3.3 reporting subscriptions (experimental).

:class:`PgReportingSubscriptionStateStore` implements
``adcp.reporting.subscriptions.ReportingSubscriptionStateStore`` and
:class:`PgPrincipalRecordStore` implements
``adcp.server.PrincipalRecordStore``. Together they make a seller's
``sync_principal`` subscriptions and the engine's grants, exceptions and
version snapshots survive restarts and agree across workers::

    from psycopg_pool import AsyncConnectionPool
    from adcp.reporting.subscriptions import ReportingSubscriptionEngine
    from adcp.server import PrincipalService
    from adcp_reporting_storage import (
        PgPrincipalRecordStore,
        PgReportingSubscriptionStateStore,
    )

    async with AsyncConnectionPool(dsn, min_size=2, max_size=10) as pool:
        records = PgPrincipalRecordStore(pool=pool)
        state = PgReportingSubscriptionStateStore(pool=pool)
        await state.create_schema()  # creates both tables
        principals = PrincipalService(store=records, emit_change=...)
        engine = ReportingSubscriptionEngine(
            principals=principals, host=..., offerings=..., store=state
        )

With the reporting service, prefer ``adcp.storage.postgres.postgres_storage``,
which builds both stores (and the ledger) over one pool and checks the schema
version that ``python -m adcp migrate`` applies; ``create_schema`` remains for
hand-wired compositions.

Each store keeps one JSONB document per subject and replaces it with a
single conditional statement on its revision, so the compare-and-swap is
atomic across every worker sharing the database. The pool is borrowed: the
stores never open, own or close it. Requires ``adcp`` 10.0.0b3 or later.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from adcp.reporting.subscriptions import StoredReportingSubscriptionState
    from adcp.server import PrincipalRecord

__all__ = [
    "PgPrincipalRecordStore",
    "PgReportingSubscriptionStateStore",
    "reporting_subscriptions_schema_sql",
]

_REQUIRES = "adcp_reporting_storage.subscriptions requires adcp 10.0.0b3 or later"


def reporting_subscriptions_schema_sql() -> str:
    """The idempotent DDL for both reporting-subscription tables."""
    return (
        files("adcp_reporting_storage.subscriptions")
        .joinpath("reporting_subscriptions.sql")
        .read_text()
    )


def _document(value: Any) -> dict[str, Any]:
    # psycopg decodes JSONB to Python by default; a custom loader may not.
    decoded = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    if not isinstance(decoded, dict):
        raise ValueError("stored reporting-subscription document is not a JSON object")
    return decoded


class _PgDocumentStore:
    #: Rows survive restarts and are shared by every worker using the database.
    is_durable = True

    def __init__(self, *, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def create_schema(self) -> None:
        """Create both reporting-subscription tables if they do not exist."""
        async with self._pool.connection() as connection:
            await connection.execute(reporting_subscriptions_schema_sql())


class PgReportingSubscriptionStateStore(_PgDocumentStore):
    """Engine state for reporting subscriptions, one CAS document per principal."""

    async def list_principals(self) -> list[str]:
        """Every subject with stored state, in subject order (the managed path's roster)."""
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT subject FROM adcp_reporting_subscription_state ORDER BY subject"
            )
            return [str(row[0]) for row in await cursor.fetchall()]

    async def load(self, subject: str) -> StoredReportingSubscriptionState | None:
        try:
            from adcp.reporting.subscriptions import StoredReportingSubscriptionState
        except ImportError as error:  # pragma: no cover - older adcp
            raise ImportError(_REQUIRES) from error
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT document, revision FROM adcp_reporting_subscription_state"
                " WHERE subject = %s",
                (subject,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return StoredReportingSubscriptionState(_document(row[0]), int(row[1]))

    async def compare_and_swap(
        self,
        subject: str,
        expected_revision: int | None,
        document: Mapping[str, Any],
    ) -> bool:
        payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
        async with self._pool.connection() as connection:
            if expected_revision is None:
                cursor = await connection.execute(
                    "INSERT INTO adcp_reporting_subscription_state (subject, revision, document)"
                    " VALUES (%s, 1, %s::jsonb) ON CONFLICT (subject) DO NOTHING",
                    (subject, payload),
                )
            else:
                cursor = await connection.execute(
                    "UPDATE adcp_reporting_subscription_state"
                    " SET revision = revision + 1, document = %s::jsonb, updated_at = now()"
                    " WHERE subject = %s AND revision = %s",
                    (payload, subject, expected_revision),
                )
            return bool(cursor.rowcount == 1)


class PgPrincipalRecordStore(_PgDocumentStore):
    """``sync_principal`` records, including reporting subscriptions and their versions."""

    async def get(self, subject: str) -> PrincipalRecord | None:
        try:
            from adcp.server import principal_record_from_document
        except ImportError as error:  # pragma: no cover - older adcp
            raise ImportError(_REQUIRES) from error
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT document, store_revision FROM adcp_principal_records WHERE subject = %s",
                (subject,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return principal_record_from_document(_document(row[0]), store_revision=int(row[1]))

    async def compare_and_swap(
        self,
        subject: str,
        expected_store_revision: int | None,
        record: PrincipalRecord,
    ) -> bool:
        try:
            from adcp.server import principal_record_to_document
        except ImportError as error:  # pragma: no cover - older adcp
            raise ImportError(_REQUIRES) from error
        if record.subject != subject:
            raise ValueError("a principal record is stored under its own subject")
        payload = json.dumps(
            principal_record_to_document(record), separators=(",", ":"), sort_keys=True
        )
        async with self._pool.connection() as connection:
            if expected_store_revision is None:
                cursor = await connection.execute(
                    "INSERT INTO adcp_principal_records (subject, store_revision, document)"
                    " VALUES (%s, %s, %s::jsonb) ON CONFLICT (subject) DO NOTHING",
                    (subject, record.store_revision, payload),
                )
            else:
                cursor = await connection.execute(
                    "UPDATE adcp_principal_records"
                    " SET store_revision = %s, document = %s::jsonb, updated_at = now()"
                    " WHERE subject = %s AND store_revision = %s",
                    (record.store_revision, payload, subject, expected_store_revision),
                )
            return bool(cursor.rowcount == 1)
