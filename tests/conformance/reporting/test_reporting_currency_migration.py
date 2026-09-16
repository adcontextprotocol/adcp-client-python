"""No invented currency when upgrading literal beta.15 and #1169 ledgers."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from adcp.reporting.ledger import (
    ProducerOfferings,
    ReportingConfiguration,
    ReportingCurrencyError,
    ReportingObligationRecord,
    ReportingProducer,
    ReportingStatusCaller,
    ReportingStatusHandler,
)
from adcp.reporting.ledger.pg import PgReportingLedgerStore
from adcp.types import GetReportingStatusResponse

from ._generation_support import (
    NOW,
    UncalledSource,
    configuration,
    isolated_reporting_pool,
    revision_for,
)
from .test_reporting_generation_migration import _TABLES, _primary_key

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
MIGRATION = files("adcp.reporting.ledger").joinpath("reporting_ledger_obligation_currency.sql")
ACCOUNT_MIGRATION = files("adcp.reporting.ledger").joinpath(
    "reporting_ledger_account_generations.sql"
)


def test_1169_schema_fixture_is_the_reviewed_stacked_base() -> None:
    # 91fa2786efdd3117ce2c1a875ede7431767d7e87, byte for byte.
    assert hashlib.sha256((FIXTURES / "reporting_ledger_1169.sql").read_bytes()).hexdigest() == (
        "6617d1c840b50530266f5126ec20f86c4b032735f51ecc78f983db5ac070a911"
    )


async def retained(pool: AsyncConnectionPool) -> dict[str, list[Any]]:
    from psycopg import sql

    result = {}
    async with pool.connection() as connection:
        for table in _TABLES:
            rows = await (
                await connection.execute(
                    sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY to_jsonb(t)::text").format(
                        sql.Identifier(table)
                    )
                )
            ).fetchall()
            result[table] = [row[0] for row in rows]
    return result


async def raw_upgrade(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(ACCOUNT_MIGRATION.read_text())
            await connection.execute(MIGRATION.read_text())


@pytest.mark.parametrize("schema", ["reporting_ledger_beta15.sql", "reporting_ledger_1169.sql"])
@pytest.mark.parametrize("autocommit", [False, True])
async def test_migration_preserves_evidence_and_quarantines_unknown_currency(
    schema: str,
    autocommit: bool,
) -> None:
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        async with pool.connection() as connection:
            await connection.execute((FIXTURES / schema).read_text())
            await connection.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
            # An unfulfilled old obligation demonstrates that neither a live
            # account lookup nor new process options can fill historical gaps.
            await connection.execute(
                "INSERT INTO reporting_obligations SELECT (jsonb_populate_record("
                "NULL::reporting_obligations, to_jsonb(o) || "
                '\'{"reporting_obligation_id":"legacy_pending","delivery_config_id":"pending"}\'::jsonb)).*'
                " FROM reporting_obligations o WHERE reporting_obligation_id = 'rpo_acct_a'"
            )
        before = await retained(pool)
        primary = await _primary_key(pool)
        await asyncio.gather(
            *(PgReportingLedgerStore(pool=pool).create_schema() for _ in range(3)),
            *(raw_upgrade(pool) for _ in range(3)),
        )
        after = await retained(pool)
        # The ONLY record change is the explicit unknown column; all hashes,
        # rows, units, issues, leases and sequence numbers are otherwise exact.
        for record in before["reporting_obligations"]:
            record["currency"] = None
        assert after == before
        assert (await _primary_key(pool))[
            3
        ] == "PRIMARY KEY (account_id, delivery_config_id, delivery_config_version)"
        if schema == "reporting_ledger_1169.sql":
            assert await _primary_key(pool) == primary
        await raw_upgrade(pool)
        await PgReportingLedgerStore(pool=pool).create_schema()
        assert await retained(pool) == after

        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        old = await store.get_obligation(account_id="acct_a", reporting_obligation_id="rpo_acct_a")
        pending = await store.get_obligation(
            account_id="acct_a", reporting_obligation_id="legacy_pending"
        )
        assert old is not None and old.currency is None
        assert pending is not None and pending.currency is None
        assert await store.commit_obligation(replace(old, currency="EUR")) == old
        revision = await store.get_revision(
            account_id="acct_a", reporting_revision_id="rpr_acct_a_official"
        )
        assert revision is not None
        rows = await store.read_revision_rows(
            account_id="acct_a", reporting_revision_id=revision.reporting_revision_id
        )
        assert rows.rows == ({"media_buy_id": "mb_acct_a", "impressions": 5},)
        assert await store.commit_revision(revision, rows.rows) == revision
        adjustments = await store.list_adjustments(
            account_id="acct_a", reporting_revision_ids=[revision.reporting_revision_id]
        )
        assert len(adjustments) == 1
        assert await store.commit_adjustment(adjustments[0]) == adjustments[0]

        def must_not_resolve(
            config: ReportingConfiguration, candidate: ReportingObligationRecord
        ) -> str:
            raise AssertionError("migration/retry cannot resolve a legacy obligation")

        producer = ReportingProducer(
            store=store,
            source=UncalledSource(),
            offerings=ProducerOfferings(currency="EUR"),
            currency_resolver=must_not_resolve,
            clock=lambda: NOW,
        )
        config = replace(
            configuration(), delivery_config_id="pending", required_finality="official"
        )
        assert await producer.close_elapsed_periods(config) == []
        # ``old`` closed officially before the upgrade: acquisition is already
        # terminal, so it stays the no-op it was rather than becoming an error
        # the worker loop would hit on every turn. Nothing is read or written.
        assert (
            await producer.acquire_obligation(
                replace(config, delivery_config_id=old.delivery_config_id), old, restate=True
            )
            is None
        )
        for candidate in (pending, replace(pending, currency="EUR")):
            with pytest.raises(ReportingCurrencyError, match="CURRENCY_UNRESOLVED"):
                await producer.acquire_obligation(
                    replace(config, delivery_config_id=candidate.delivery_config_id),
                    candidate,
                    restate=True,
                )
        fresh, fresh_rows = revision_for(pending)
        with pytest.raises(ReportingCurrencyError, match="CURRENCY_UNRESOLVED"):
            await store.commit_revision(fresh, fresh_rows)
        with pytest.raises(ReportingCurrencyError, match="CURRENCY_UNRESOLVED"):
            await store.commit_adjustment(
                replace(adjustments[0], reporting_adjustment_id="new_legacy_delta")
            )

        # Reads remain schema-valid and actionable, without re-labeling history.
        handler = ReportingStatusHandler(store)
        payload = await handler.handle(
            {"view": "summary"},
            caller=ReportingStatusCaller(account_id="acct_a", consumer_id="buyer"),
        )
        GetReportingStatusResponse.model_validate(payload)
        assert payload["health"] == "action_required"
        assert any(issue["code"] == "HISTORY_UNAVAILABLE" for issue in payload["issues"])
        assert await retained(pool) == after

        # Ordinary upgraded accounts still publish after the legacy quarantine.
        new_config = configuration("eur")
        await store.put_configuration(new_config)
        next_producer = ReportingProducer(
            store=store,
            source=UncalledSource(),
            offerings=ProducerOfferings(currency="EUR"),
            clock=lambda: NOW,
        )
        (new,) = await next_producer.close_elapsed_periods(new_config)
        assert new.currency == "EUR"


async def test_currency_constraints_and_immutability_cover_direct_sql() -> None:
    from psycopg.errors import CheckViolation

    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        producer = ReportingProducer(
            store=store,
            source=UncalledSource(),
            offerings=ProducerOfferings(currency="EUR"),
            clock=lambda: NOW,
        )
        (obligation,) = await producer.close_elapsed_periods(configuration())
        for value in ("USD", None, "eur", "EUR\n", "ÅBC", "US1", "EURO"):
            with pytest.raises(CheckViolation):
                async with pool.connection() as connection:
                    await connection.execute(
                        "UPDATE reporting_obligations SET currency = %s", (value,)
                    )
        async with pool.connection() as connection:
            await connection.execute("UPDATE reporting_obligations SET currency = currency")
        assert (
            await store.get_obligation(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            == obligation
        )
        for value in ("eur", "EUR\n", "ÅBC", "US1", "EURO"):
            with pytest.raises(CheckViolation):
                async with pool.connection() as connection:
                    await connection.execute(
                        "INSERT INTO reporting_obligations SELECT (jsonb_populate_record("
                        "NULL::reporting_obligations, to_jsonb(o) || jsonb_build_object("
                        "'currency', %s::text, 'account_id', 'invalid', "
                        "'reporting_obligation_id', 'invalid'))).*"
                        " FROM reporting_obligations o",
                        (value,),
                    )


async def test_unexpected_default_rolls_back_without_rewriting_evidence() -> None:
    from psycopg.errors import RaiseException

    async with isolated_reporting_pool() as pool:
        async with pool.connection() as connection:
            await connection.execute((FIXTURES / "reporting_ledger_beta15.sql").read_text())
            await connection.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
            await connection.execute(
                "ALTER TABLE reporting_obligations ADD COLUMN currency TEXT DEFAULT 'USD'"
            )
        before = await retained(pool)
        primary = await _primary_key(pool)
        with pytest.raises(RaiseException, match="Unexpected reporting_obligations.currency"):
            await PgReportingLedgerStore(pool=pool).create_schema()
        assert await retained(pool) == before
        assert await _primary_key(pool) == primary
