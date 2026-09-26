"""Real PostgreSQL read/validate/lock/CAS races and cancellation boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import threading

import pytest

from adcp.reporting.submissions import (
    PgReportingSubmissionIntentStore,
    ReportingSubmissionCode,
    ReportingSubmissionError,
    prepare_reporting_receipt_submission,
)
from adcp.reporting.submissions import pg as submission_pg

from ._buyer_submission_support import SCOPE, mixed, receipt, response_for
from ._generation_support import isolated_reporting_pool


class PausedValidator(PgReportingSubmissionIntentStore):
    """Pause CPU validation in its worker thread, without blocking the event loop."""

    def __init__(self, *, pool, pauses=1):
        super().__init__(pool=pool)
        self.loop = asyncio.get_running_loop()
        self.entered = [asyncio.Event() for _ in range(pauses)]
        self.release = [threading.Event() for _ in range(pauses)]
        self.validations = 0

    def _validate_snapshot(self, scope, snapshot):
        result = super()._validate_snapshot(scope, snapshot)
        index = self.validations
        self.validations += 1
        if index < len(self.entered):
            self.loop.call_soon_threadsafe(self.entered[index].set)
            assert self.release[index].wait(20), "test validator was not released"
        return result

    def release_all(self):
        for barrier in self.release:
            barrier.set()


async def take_scope_lock(pool):
    async with pool.connection() as connection, connection.transaction():
        await connection.execute(
            "SELECT 1 FROM reporting_buyer_submission_scopes WHERE scope_sha256=%s FOR UPDATE",
            (SCOPE.storage_key,),
        )


@pytest.mark.parametrize("winner", ["equivalent", "contradictory", "advanced", "new_lane"])
async def test_paused_validation_does_not_hold_scope_lock_and_rechecks_concurrent_confirmation(
    winner,
):
    async with isolated_reporting_pool() as pool:
        other = PgReportingSubmissionIntentStore(pool=pool)
        await other.create_schema()
        state = await other.reserve(prepare_reporting_receipt_submission(SCOPE, mixed(101)))
        paused = PausedValidator(pool=pool)
        response = response_for(state.request(0))
        task = asyncio.create_task(paused.confirm(SCOPE, state.submission_id, 0, response))
        try:
            await asyncio.wait_for(paused.entered[0].wait(), 10)
            # A distinct PG connection obtains FOR UPDATE while validation is
            # still paused. Timeout is only a deadlock guard, not a speed claim.
            await asyncio.wait_for(take_scope_lock(pool), 10)
            assert not paused.release[0].is_set()
            response.results.clear()  # The first attempt already froze its body.
            failed = {mixed(1)[0].reporting_receipt_id} if winner == "contradictory" else None
            retained = await other.confirm(
                SCOPE,
                state.submission_id,
                0,
                response_for(state.request(0), fail=failed, result="unchanged"),
            )
            if winner in {"advanced", "new_lane"}:
                retained = await other.confirm(
                    SCOPE, state.submission_id, 1, response_for(state.request(1))
                )
            latest = retained
            if winner == "new_lane":
                latest = await other.reserve(
                    prepare_reporting_receipt_submission(SCOPE, [receipt(99999)])
                )
            paused.release_all()
            if winner == "contradictory":
                with pytest.raises(ReportingSubmissionError) as error:
                    await task
                assert error.value.code == ReportingSubmissionCode.INVALID_RESPONSE
            else:
                assert await task == retained
            assert paused.validations >= 2
            assert await other.get(SCOPE) == latest
            assert await other.get(SCOPE, state.submission_id) == retained
        finally:
            paused.release_all()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "field",
    [
        "canonical_plan",
        "plan_sha256",
        "confirmed_results",
        "confirmed_sha256",
        "pending",
        "submission_id",
        "canonical_identity",
        "current_submission_id",
    ],
)
async def test_cas_checks_every_state_field_and_never_overwrites_new_corruption(field):
    async with isolated_reporting_pool() as pool:
        from psycopg import sql

        other = PgReportingSubmissionIntentStore(pool=pool)
        await other.create_schema()
        state = await other.reserve(prepare_reporting_receipt_submission(SCOPE, mixed()))
        paused = PausedValidator(pool=pool)
        task = asyncio.create_task(
            paused.confirm(SCOPE, state.submission_id, 0, response_for(state.request(0)))
        )
        try:
            await asyncio.wait_for(paused.entered[0].wait(), 10)
            scopes = field in {"canonical_identity", "current_submission_id"}
            table = (
                "reporting_buyer_submission_scopes"
                if scopes
                else ("reporting_buyer_submission_intents")
            )
            value = {
                "canonical_plan": state._plan.decode() + " ",
                "plan_sha256": "0" * 64,
                "confirmed_results": "[ ]",
                "confirmed_sha256": "0" * 64,
                "pending": False,
                "submission_id": "reporting-submission:" + "e" * 64,
                "canonical_identity": "{}",
                "current_submission_id": None,
            }[field]
            async with pool.connection() as connection:
                await connection.execute(
                    sql.SQL("UPDATE {} SET {}=%s").format(
                        sql.Identifier(table), sql.Identifier(field)
                    ),
                    (value,),
                )
                if field in {"canonical_plan", "confirmed_results"}:
                    # Even a fresh correct digest must not bless changed bytes.
                    digest_field = (
                        "plan_sha256" if field == "canonical_plan" else "confirmed_sha256"
                    )
                    await connection.execute(
                        sql.SQL("UPDATE {} SET {}=%s").format(
                            sql.Identifier(table), sql.Identifier(digest_field)
                        ),
                        (hashlib.sha256(value.encode()).hexdigest(),),
                    )
                before = await (
                    await connection.execute(
                        sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
                    )
                ).fetchall()
            paused.release_all()
            with pytest.raises(ReportingSubmissionError) as error:
                await task
            assert error.value.code == ReportingSubmissionCode.HISTORY_CORRUPT
            async with pool.connection() as connection:
                after = await (
                    await connection.execute(
                        sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
                    )
                ).fetchall()
            assert after == before
        finally:
            paused.release_all()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_repeated_real_cas_losses_are_bounded_without_abandoning_the_intent(monkeypatch):
    monkeypatch.setattr(submission_pg, "_MAX_CAS_ATTEMPTS", 2)
    async with isolated_reporting_pool() as pool:
        other = PgReportingSubmissionIntentStore(pool=pool)
        await other.create_schema()
        state = await other.reserve(prepare_reporting_receipt_submission(SCOPE, mixed(201)))
        paused = PausedValidator(pool=pool, pauses=2)
        task = asyncio.create_task(
            paused.confirm(SCOPE, state.submission_id, 0, response_for(state.request(0)))
        )
        try:
            for chunk in range(2):
                await asyncio.wait_for(paused.entered[chunk].wait(), 10)
                retained = await other.confirm(
                    SCOPE, state.submission_id, chunk, response_for(state.request(chunk))
                )
                paused.release[chunk].set()
            with pytest.raises(ReportingSubmissionError) as error:
                await task
            assert error.value.code == ReportingSubmissionCode.STORAGE_UNAVAILABLE
            assert paused.validations == 2
            assert retained.pending and retained.confirmed_chunks == 2
            assert await other.get(SCOPE) == retained
            assert (
                await other.reserve(prepare_reporting_receipt_submission(SCOPE, [receipt(99999)]))
                == retained
            )
            final = await other.confirm(
                SCOPE, state.submission_id, 2, response_for(state.request(2))
            )
            assert not final.pending and final._confirmed[:2] == retained._confirmed
        finally:
            paused.release_all()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["validation", "locked", "waiting"])
async def test_cancel_at_new_boundary_releases_transactions_and_retains_uncertainty(phase):
    async with isolated_reporting_pool() as pool:
        other = PgReportingSubmissionIntentStore(pool=pool)
        await other.create_schema()
        state = await other.reserve(prepare_reporting_receipt_submission(SCOPE, mixed()))
        entered, release = asyncio.Event(), asyncio.Event()

        class PausedLock(PgReportingSubmissionIntentStore):
            async def _snapshot_on(self, connection, key, selected_id, *, lock=False):
                if lock and phase == "waiting":
                    entered.set()
                result = await super()._snapshot_on(connection, key, selected_id, lock=lock)
                if lock and phase == "locked":
                    entered.set()
                    await release.wait()
                return result

        paused = PausedValidator(pool=pool) if phase == "validation" else PausedLock(pool=pool)

        async def run_and_cancel():
            task = asyncio.create_task(
                paused.confirm(SCOPE, state.submission_id, 0, response_for(state.request(0)))
            )
            barrier = paused.entered[0] if phase == "validation" else entered
            try:
                await asyncio.wait_for(barrier.wait(), 10)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                if isinstance(paused, PausedValidator):
                    paused.release_all()
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        if phase == "waiting":
            async with pool.connection() as connection, connection.transaction():
                await connection.execute(
                    "SELECT 1 FROM reporting_buyer_submission_scopes"
                    " WHERE scope_sha256=%s FOR UPDATE",
                    (SCOPE.storage_key,),
                )
                await run_and_cancel()
        else:
            await run_and_cancel()
        await asyncio.wait_for(take_scope_lock(pool), 10)
        assert await other.get(SCOPE) == state
        restored = await other.confirm(
            SCOPE, state.submission_id, 0, response_for(state.request(0))
        )
        assert not restored.pending
