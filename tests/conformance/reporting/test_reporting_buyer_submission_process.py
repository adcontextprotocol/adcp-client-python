"""Actual independent-process death around buyer intent and seller receipt commits."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.submissions import PgReportingSubmissionIntentStore, ReportingSubmissionScope

from ._receipt_support import adjustment_for, batch_state, receipt_case, receipt_harness
from .test_reporting_materializer_process import Child


@asynccontextmanager
async def buyer_worker(
    h, scope, inputs, *, pause=None, resume=False, python=None, script=None, installed=None
):
    process = await asyncio.create_subprocess_exec(
        str(python or sys.executable),
        *(["-I"] if installed else []),
        str(script or Path(__file__).with_name("_buyer_submission_process.py")),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    child = Child(process)
    try:
        await child.send(
            {
                "conninfo": h.pool.conninfo,
                "kwargs": h.pool.kwargs,
                "trusted_scope": {
                    "seller_id": scope.seller_id,
                    "account_id": scope.account_id,
                    "consumer_id": scope.consumer_id,
                },
                "receipts": inputs,
                "pause": pause,
                "resume": resume,
                "installed": installed,
            }
        )
        yield child
    finally:
        await child.kill()
        diagnostic = await process.stderr.read()
        if process.returncode not in {0, -9}:
            pytest.fail(diagnostic.decode())


@pytest.mark.parametrize(
    "point",
    [
        "before_intent",
        "scope_row",
        "intent_row",
        "intent_committed",
        "before_seller",
        "seller_committed",
        "response_delivered",
        "confirmation_row",
        "confirmation_committed",
        "returned",
    ],
)
async def test_process_death_preserves_exact_requests_and_confirmed_outcomes(point):
    async with receipt_harness("postgres") as h:
        s = await receipt_case(h, consumer_id="https://buyer.example.test/agent")
        adjustment = await adjustment_for(h, s)
        # Opposite order from wire grouping: recovery must preserve this order.
        inputs = [adjustment, receipt_to_wire(s.receipt)]
        scope = ReportingSubmissionScope(
            "https://seller.example.test", s.obligation.account_id, s.binding.consumer_id
        )
        buyer = PgReportingSubmissionIntentStore(pool=h.pool)
        await buyer.create_schema()
        intent_committed = point not in {"before_intent", "scope_row", "intent_row"}
        seller_committed = point in {
            "seller_committed",
            "response_delivered",
            "confirmation_row",
            "confirmation_committed",
            "returned",
        }
        confirmed = point in {"confirmation_committed", "returned"}
        async with buyer_worker(h, scope, inputs, pause=point) as child:
            await child.event(point)
            # Ordinary MVCC reads cannot see partial headers/plans/confirmations
            # even when the killed worker holds a transaction's scope row lock.
            async with h.pool.connection() as connection:
                row = await (
                    await connection.execute(
                        "SELECT (SELECT count(*) FROM reporting_buyer_submission_scopes),"
                        " (SELECT count(*) FROM reporting_buyer_submission_intents),"
                        " (SELECT count(*) FROM reporting_buyer_submission_intents"
                        " WHERE NOT pending)"
                    )
                ).fetchone()
            assert row == (int(intent_committed), int(intent_committed), int(confirmed))
            assert await batch_state(h) == (((2, True),) if seller_committed else ())
            await child.kill()
        before = await buyer.get(scope)
        if intent_committed:
            assert before is not None and before.pending != confirmed
        else:
            assert before is None
        async with buyer_worker(h, scope, inputs, resume=intent_committed) as restarted:
            result = await restarted.event("done")
            assert await asyncio.wait_for(restarted.process.wait(), 10) == 0
        assert result["outcomes"] == ["recorded", "recorded"]
        assert not result["pending"] and result["submitted"] == 2
        assert result["calls"] == int(not confirmed)
        assert await batch_state(h) == ((2, True),)
        after = await PgReportingSubmissionIntentStore(pool=h.pool).get(scope)
        assert not after.pending
        if before is not None:
            assert before._plan == after._plan and before.submission_id == after.submission_id
        assert [outcome.submitted_receipt.reporting_receipt_id for outcome in after.outcomes] == [
            item["reporting_receipt_id"] for item in inputs
        ]
        async with h.pool.connection() as connection:
            batches = await (
                await connection.execute(
                    "SELECT canonical_request FROM reporting_receipt_ingestion_batches"
                )
            ).fetchall()
        from adcp.reporting.canonical_json import canonical_json_utf8_v1

        expected = canonical_json_utf8_v1(
            after.request(0).model_dump(mode="json", exclude_none=True)
        ).decode()
        assert batches == [(expected,)]


async def test_independent_concurrent_buyers_replay_one_reserved_seller_request():
    async with receipt_harness("postgres") as h:
        s = await receipt_case(h)
        inputs = [await adjustment_for(h, s), receipt_to_wire(s.receipt)]
        competing = [
            {**item, "reporting_receipt_id": f"competing-receipt-{index:06d}"}
            for index, item in enumerate(inputs)
        ]
        scope = ReportingSubmissionScope("seller", s.obligation.account_id, s.binding.consumer_id)
        store = PgReportingSubmissionIntentStore(pool=h.pool)
        await store.create_schema()
        async with buyer_worker(h, scope, inputs, pause="before_seller") as first:
            await first.event("before_seller")
            async with buyer_worker(h, scope, competing, pause="before_seller") as second:
                await second.event("before_seller")
                await asyncio.gather(
                    first.send({"continue": True}), second.send({"continue": True})
                )
                a, b = await asyncio.gather(first.event("done"), second.event("done"))
                assert a["outcomes"] == b["outcomes"] == ["recorded", "recorded"]
                assert not a["proposal_deferred"] and b["proposal_deferred"]
                assert await asyncio.wait_for(first.process.wait(), 10) == 0
                assert await asyncio.wait_for(second.process.wait(), 10) == 0
        assert await batch_state(h) == ((2, True),)
        final = await store.get(scope)
        assert not final.pending
        assert [outcome.submitted_receipt.reporting_receipt_id for outcome in final.outcomes] == [
            item["reporting_receipt_id"] for item in inputs
        ]
