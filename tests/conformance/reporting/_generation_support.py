"""Shared account-isolation scenarios and an isolated, real PostgreSQL schema."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest

from adcp.reporting.ledger import (
    ReportingConfiguration,
    ReportingDefinitionBinding,
    ReportingObligationRecord,
    ReportingRevisionRecord,
    ReportingScheduleSpec,
    derive_period,
    revision_content_sha256,
)
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    ReportingSourceExecutorResult,
    ReportingSourceSliceRequestV1,
)

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
NOW = START + timedelta(hours=3)


@asynccontextmanager
async def isolated_reporting_pool(
    *, autocommit: bool = False
) -> AsyncIterator[AsyncConnectionPool]:
    """Never touch an adopter's tables; each invocation owns one random schema."""
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        pytest.skip("ADCP_PG_TEST_URL not set — requires real PostgreSQL")
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    from psycopg import AsyncConnection, sql
    from psycopg_pool import AsyncConnectionPool

    schema = f"adcp_reporting_identity_{secrets.token_hex(6)}"
    async with await AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with AsyncConnectionPool(
                url,
                kwargs={
                    "options": f"-csearch_path={schema} -cstatement_timeout=15000",
                    "autocommit": autocommit,
                },
                min_size=2,
                max_size=8,
                open=False,
            ) as pool:
                await pool.wait(timeout=10)
                yield pool
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def assert_c_collated_rolling_database() -> None:
    """Frozen-artifact readiness is only measurable on a C-collated database.

    Reviewed A digests each table's constraints as one aggregate ordered by
    ``pg_get_constraintdef()`` -- a ``text`` expression sorted under the
    *database* default collation. Under any other default collation A's bundled
    contract does not reproduce even against A's own freshly created schema, so
    A readiness is already closed before anything is migrated and a rolling
    assertion would measure the locale instead of the upgrade. Every SDK
    identity column is ``TEXT COLLATE "C"``, so C is the contract.

    Every fixture that executes a frozen A/B artifact must call this, and every
    CI job that runs one must initialise its cluster with
    ``--encoding=UTF8 --lc-collate=C --lc-ctype=C``. Fail loudly rather than
    weaken or skip the artifact assertions.
    """
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        pytest.skip("actual A/B compatibility requires real PostgreSQL")
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(url, autocommit=True) as connection:
        row = connection.execute(
            "SELECT datcollate, datlocprovider, daticulocale FROM pg_database"
            " WHERE datname = current_database()"
        ).fetchone()
    assert row is not None
    collate, provider, icu = row
    assert collate == "C" and (provider != "i" or icu in {None, "C"}), (
        "the frozen A/B rolling gates require a C-collated database"
        f" (found datcollate={collate!r} provider={provider!r} icu={icu!r});"
        " create the cluster with initdb --encoding=UTF8 --lc-collate=C --lc-ctype=C"
    )


def configuration(account_id: str = "acct_a") -> ReportingConfiguration:
    return ReportingConfiguration(
        delivery_config_id="daily",
        delivery_config_version=1,
        account_id=account_id,
        report_definition_id="hourly_delivery",
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=ReportingScheduleSpec("PT1H", "PT1H", period_anchor=START),
        required_finality="snapshot",
        activated_at=START,
        deactivated_at=END,
        media_buy_ids=(f"mb_{account_id}",),
        definition=ReportingDefinitionBinding(
            report_definition_uri=f"https://contracts.example.test/{account_id}/hourly",
            report_definition_sha256="a" * 64,
            schema_version="1.0.0",
            schema_uri=f"https://contracts.example.test/{account_id}/rows.json",
            schema_sha256="b" * 64,
        ),
    )


def obligation_for(config: ReportingConfiguration) -> ReportingObligationRecord:
    period = derive_period(config.schedule, account_timezone=config.account_timezone, ordinal=0)
    return ReportingObligationRecord(
        reporting_obligation_id=f"rpo_{config.account_id}",
        account_id=config.account_id,
        delivery_config_id=config.delivery_config_id,
        delivery_config_version=config.delivery_config_version,
        report_definition_id=config.report_definition_id,
        reporting_profile=config.reporting_profile,
        feed_purpose=config.feed_purpose,
        period=period,
        scope_resolved_at=period.end,
        media_buy_ids=config.media_buy_ids,
        required_finality=config.required_finality,
        automated_recovery_deadline_at=period.expected_at + config.automated_recovery_window,
        schedule=config.schedule,
        definition=config.definition,
        created_at=END,
        currency="USD",
    )


def revision_for(
    obligation: ReportingObligationRecord, *, suffix: str = "first"
) -> tuple[ReportingRevisionRecord, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = [{"media_buy_id": obligation.media_buy_ids[0], "impressions": 5}]
    revision_id = f"rpr_{obligation.account_id}_{suffix}"
    totals = (("impressions", "5"),)
    return (
        ReportingRevisionRecord(
            reporting_revision_id=revision_id,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            finality="snapshot",
            revision_content_sha256=revision_content_sha256(
                reporting_revision_id=revision_id,
                row_count=1,
                control_totals=totals,
                reporting_rows=rows,
            ),
            row_count=1,
            control_totals=totals,
            observed_at=END,
            data_through=END,
            created_at=END,
        ),
        rows,
    )


class UncalledSource:
    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        raise AssertionError("period close must not acquire source data")

    async def execute(
        self,
        request: ReportingSourceSliceRequestV1,
        *,
        cancel: asyncio.Event,
        heartbeat: Callable[[], None] | None = None,
    ) -> ReportingSourceExecutorResult:
        raise AssertionError("period close must not acquire source data")
