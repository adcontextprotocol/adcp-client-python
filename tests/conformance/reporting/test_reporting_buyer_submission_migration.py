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
