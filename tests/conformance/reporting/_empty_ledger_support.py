"""Sync connections for the empty-ledger replacements, on every supported driver.

Each test owns one random schema. ``DRIVERS`` names the connections an Alembic
revision can hand the sync helpers: raw psycopg 3 and psycopg2 connections, and
SQLAlchemy connections over either driver.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

DRIVERS = ("psycopg", "psycopg2", "sqlalchemy+psycopg", "sqlalchemy+psycopg2")

# Every packaged reporting migration, in an order that installs: the complete
# owned surface any combination of stores can create.
OWNED_SQL = (
    "reporting_ledger.sql",
    "reporting_ledger_account_generations.sql",
    "reporting_ledger_obligation_currency.sql",
    "reporting_ledger_reconciliation.sql",
    "reporting_notification_outbox.sql",
    "reporting_webhook_activity.sql",
    "reporting_provisional_observations.sql",
    "reporting_materializer.sql",
    "reporting_receipt_ingestion.sql",
    "reporting_feed.sql",
    "reporting_status_notifications.sql",
    "reporting_status_selector_version.sql",
    "reporting_projection.sql",
    "reporting_projection_notifications.sql",
    "reporting_projection_feed.sql",
    "reporting_production.sql",
    "reporting_caller_ownership.sql",
    "reporting_changes.sql",
    "reporting_period_retirement.sql",
    "reporting_row_storage.sql",
    "reporting_row_migration.sql",
    "reporting_inline_storage.sql",
    "reporting_buyer_submissions.sql",
)
LEGACY_SQL = (
    "reporting_ledger.sql",
    "reporting_ledger_obligation_currency.sql",
    "reporting_ledger_reconciliation.sql",
)
_LEGACY_ROOT = Path(__file__).parents[2] / "fixtures/reporting_ownership"

SURFACE = (
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
    " WHERE n.nspname=current_schema() AND c.relkind IN ('r','p','S')"
    " AND c.relname ~ '^(adcp_)?reporting_'"
)
FUNCTIONS = (
    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
    " WHERE n.nspname=current_schema()"
)


def database_url() -> str:
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        pytest.skip("ADCP_PG_TEST_URL not set — requires real PostgreSQL")
    pytest.importorskip("psycopg")
    return url


def require_driver(driver: str) -> None:
    database_url()
    if driver.startswith("sqlalchemy"):
        pytest.importorskip("sqlalchemy")
    if driver.endswith("psycopg2"):
        pytest.importorskip("psycopg2")


@contextmanager
def isolated_schema() -> Iterator[str]:
    import psycopg
    from psycopg import sql

    schema = f"adcp_reporting_empty_{secrets.token_hex(6)}"
    with psycopg.connect(database_url(), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield schema
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _options(schema: str) -> str:
    return f"-csearch_path={schema} -cstatement_timeout=15000"


def connect(schema: str, *, autocommit: bool = False) -> Any:
    """A sync psycopg 3 connection bound to ``schema``."""
    import psycopg

    return psycopg.connect(database_url(), autocommit=autocommit, options=_options(schema))


@dataclass
class Session:
    """One caller connection: ``bind`` is what the sync helper receives."""

    bind: Any
    run: Callable[[str], list[tuple[Any, ...]]]
    commit: Callable[[], None]
    rollback: Callable[[], None]


@contextmanager
def session(schema: str, driver: str) -> Iterator[Session]:
    """A non-autocommit caller connection: the helper must join its transaction."""
    url = database_url()
    if driver.startswith("sqlalchemy"):
        from sqlalchemy import create_engine, make_url
        from sqlalchemy.pool import NullPool

        dialect = "postgresql+psycopg2" if driver.endswith("psycopg2") else "postgresql+psycopg"
        engine = create_engine(
            make_url(url).set(drivername=dialect),
            poolclass=NullPool,
            connect_args={"options": _options(schema)},
        )
        try:
            with engine.connect() as connection:

                def run(statement: str) -> list[tuple[Any, ...]]:
                    result = connection.exec_driver_sql(statement)
                    return [tuple(row) for row in result] if result.returns_rows else []

                yield Session(connection, run, connection.commit, connection.rollback)
        finally:
            engine.dispose()
        return
    if driver == "psycopg2":
        import psycopg2

        raw = psycopg2.connect(url, options=_options(schema))
    else:
        raw = connect(schema)

    def run_raw(statement: str) -> list[tuple[Any, ...]]:
        with raw.cursor() as cursor:
            cursor.execute(statement)
            return list(cursor.fetchall()) if cursor.description else []

    try:
        yield Session(raw, run_raw, raw.commit, raw.rollback)
    finally:
        raw.close()


def install_legacy(connection: Any) -> None:
    """The pre-ownership (8.x) ledger, as an 8.x image creates it."""
    for name in LEGACY_SQL:
        connection.execute((_LEGACY_ROOT / name).read_text())


def install_owned(connection: Any) -> None:
    root = files("adcp.reporting.ledger")
    for name in OWNED_SQL:
        connection.execute(root.joinpath(name).read_text())
    connection.execute(
        files("adcp.reporting.production").joinpath("service_context.sql").read_text()
    )


def create_owned_schema(schema: str, **options: Any) -> None:
    """Build the owned ledger the way an adopter does: ``create_schema()``."""
    pytest.importorskip("psycopg_pool")
    from psycopg_pool import AsyncConnectionPool

    from adcp.reporting.ledger.pg import PgReportingLedgerStore

    async def create() -> None:
        async with AsyncConnectionPool(
            database_url(),
            kwargs={"options": _options(schema), "autocommit": True},
            min_size=1,
            max_size=2,
            open=False,
        ) as pool:
            await PgReportingLedgerStore(pool=pool, **options).create_schema()

    asyncio.run(create())


def surface(connection: Any) -> int:
    """Reporting tables and sequences left in the schema."""
    return connection.execute(SURFACE).fetchone()[0]


def wait_until_blocked(schema: str, pid: int, thread: threading.Thread) -> None:
    """Return once ``pid`` waits on a lock, rather than treating time as evidence."""
    with connect(schema, autocommit=True) as observer:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if observer.execute(
                "SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted", (pid,)
            ).fetchone():
                assert thread.is_alive()
                return
            time.sleep(0.01)
    raise AssertionError("the replacement never waited on a lock")


class Background(threading.Thread):
    """Run a sync replacement on another thread, keeping its outcome."""

    def __init__(self, target: Callable[[], None]) -> None:
        super().__init__(daemon=True)
        self._target_call = target
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self._target_call()
        except BaseException as exc:  # surfaced by outcome()
            self.error = exc

    def outcome(self) -> BaseException | None:
        self.join(timeout=10)
        assert not self.is_alive()
        return self.error
