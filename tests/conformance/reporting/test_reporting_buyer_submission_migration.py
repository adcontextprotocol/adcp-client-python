"""Additive isolated SQL, schema preconditions, corruption and closed PG errors."""

from __future__ import annotations

import traceback

import pytest

from adcp.reporting.submissions import (
    PgReportingSubmissionIntentStore,
    ReportingSubmissionCode,
    ReportingSubmissionError,
    prepare_reporting_receipt_submission,
)

from ._buyer_submission_support import SCOPE, SECRET, mixed, response_for
from ._generation_support import isolated_reporting_pool


async def test_buyer_migration_is_isolated_additive_and_preserves_existing_intents():
    async with isolated_reporting_pool() as pool:
        store = PgReportingSubmissionIntentStore(pool=pool)
        await store.create_schema()
        state = await store.reserve(prepare_reporting_receipt_submission(SCOPE, mixed()))
        state = await store.confirm(SCOPE, state.submission_id, 0, response_for(state.request(0)))
        await store.create_schema()
        assert await store.get(SCOPE) == state
        async with pool.connection() as connection:
            names = await (
                await connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
                    " ORDER BY tablename"
                )
            ).fetchall()
        assert names == [
            ("reporting_buyer_submission_intents",),
            ("reporting_buyer_submission_scopes",),
        ]


async def test_existing_weakened_reservation_index_fails_rollout_closed():
    async with isolated_reporting_pool() as pool:
        store = PgReportingSubmissionIntentStore(pool=pool)
        await store.create_schema()
        async with pool.connection() as connection:
            await connection.execute("DROP INDEX reporting_buyer_one_pending_scope")
            await connection.execute(
                "CREATE INDEX reporting_buyer_one_pending_scope"
                " ON reporting_buyer_submission_intents(scope_sha256)"
            )
        with pytest.raises(ReportingSubmissionError) as error:
            await store.create_schema()
        assert error.value.code == ReportingSubmissionCode.HISTORY_CORRUPT
        assert error.value.__context__ is None


@pytest.mark.parametrize(
    "table,suffix",
    [
        ("scopes", "scope_digest"),
        ("scopes", "scope_bound"),
        ("intents", "submission_id"),
        ("intents", "plan_digest"),
        ("intents", "confirmed_digest"),
        ("intents", "plan_bound"),
        ("intents", "confirmed_bound"),
    ],
)
@pytest.mark.parametrize("replacement", [None, "CHECK (true)"])
async def test_each_missing_or_weakened_check_constraint_fails_rollout_closed(
    table, suffix, replacement
):
    async with isolated_reporting_pool() as pool:
        from psycopg import sql

        store = PgReportingSubmissionIntentStore(pool=pool)
        await store.create_schema()
        table_name = sql.Identifier(f"reporting_buyer_submission_{table}")
        constraint_name = sql.Identifier(f"reporting_buyer_{suffix}")
        async with pool.connection() as connection:
            await connection.execute(
                sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(table_name, constraint_name)
            )
            if replacement is not None:
                await connection.execute(
                    sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} CHECK (true)").format(
                        table_name, constraint_name
                    )
                )
        # create_schema must leave the weakened schema rejected; no automatic
        # repair or previously completed migration can stand in for validation.
        for _ in range(2):
            with pytest.raises(ReportingSubmissionError) as error:
                await store.create_schema()
            assert error.value.code == ReportingSubmissionCode.HISTORY_CORRUPT
            assert error.value.__context__ is None and error.value.__cause__ is None


@pytest.mark.parametrize("modifier", ["NOT VALID", "NO INHERIT"])
async def test_correct_check_expression_with_weakened_enforcement_is_rejected(modifier):
    async with isolated_reporting_pool() as pool:
        from psycopg import sql

        store = PgReportingSubmissionIntentStore(pool=pool)
        await store.create_schema()
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE reporting_buyer_submission_intents"
                " DROP CONSTRAINT reporting_buyer_plan_bound"
            )
            await connection.execute(
                sql.SQL(
                    "ALTER TABLE reporting_buyer_submission_intents"
                    " ADD CONSTRAINT reporting_buyer_plan_bound"
                    " CHECK (octet_length(canonical_plan) <= 16777216) {}"
                ).format(sql.SQL(modifier))
            )
        with pytest.raises(ReportingSubmissionError) as error:
            await store.create_schema()
        assert error.value.code == ReportingSubmissionCode.HISTORY_CORRUPT


async def test_identically_named_check_on_the_wrong_table_does_not_prove_readiness():
    async with isolated_reporting_pool() as pool:
        store = PgReportingSubmissionIntentStore(pool=pool)
        await store.create_schema()
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE reporting_buyer_submission_scopes"
                " DROP CONSTRAINT reporting_buyer_scope_digest"
            )
            await connection.execute(
                "ALTER TABLE reporting_buyer_submission_intents"
                " ADD CONSTRAINT reporting_buyer_scope_digest"
                " CHECK (scope_sha256 ~ '^[a-f0-9]{64}$')"
            )
        with pytest.raises(ReportingSubmissionError) as error:
            await store.create_schema()
        assert error.value.code == ReportingSubmissionCode.HISTORY_CORRUPT


@pytest.mark.parametrize(
    "mutation", ["plan", "confirmed", "identity", "pointer", "pending", "driver"]
)
async def test_pg_corruption_and_driver_diagnostics_never_escape_or_permit_replacement(mutation):
    async with isolated_reporting_pool() as pool:
        store = PgReportingSubmissionIntentStore(pool=pool)
        await store.create_schema()
        state = await store.reserve(prepare_reporting_receipt_submission(SCOPE, mixed()))
        async with pool.connection() as connection:
            if mutation == "plan":
                await connection.execute(
                    "UPDATE reporting_buyer_submission_intents SET canonical_plan=%s", (SECRET,)
                )
            elif mutation == "confirmed":
                await connection.execute(
                    "UPDATE reporting_buyer_submission_intents SET confirmed_results=%s", (SECRET,)
                )
            elif mutation == "identity":
                await connection.execute(
                    "UPDATE reporting_buyer_submission_scopes SET canonical_identity=%s", (SECRET,)
                )
            elif mutation == "pointer":
                await connection.execute(
                    "UPDATE reporting_buyer_submission_scopes SET current_submission_id=NULL"
                )
            elif mutation == "pending":
                await connection.execute(
                    "UPDATE reporting_buyer_submission_intents SET pending=false"
                )
            elif mutation == "driver":
                await connection.execute(
                    "ALTER TABLE reporting_buyer_submission_intents DROP COLUMN confirmed_results"
                )
        for operation in (store.get(SCOPE), store.reserve(state)):
            with pytest.raises(ReportingSubmissionError) as error:
                await operation
            assert error.value.code == (
                ReportingSubmissionCode.STORAGE_UNAVAILABLE
                if mutation == "driver"
                else ReportingSubmissionCode.HISTORY_CORRUPT
            )
            assert error.value.__context__ is None and error.value.__cause__ is None
            assert "PRIVATE_SENTINEL" not in "".join(traceback.format_exception(error.value))
