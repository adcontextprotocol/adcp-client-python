"""Shared memory/PostgreSQL buyer reservation and response evidence contracts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.submissions import (
    ReportingReceiptFailureCode,
    ReportingSubmissionCode,
    ReportingSubmissionError,
    prepare_reporting_receipt_submission,
    submit_reporting_receipts,
)
from adcp.reporting.submissions import models as submission_models
from adcp.types import SyncReportingReceiptsResponse

from ._buyer_submission_support import (
    RECEIVED,
    SCOPE,
    SECRET,
    ReceiptClient,
    TrustedAuthorizer,
    intent_store,
    mixed,
    receipt,
    response_for,
)

__all__ = ["intent_store"]


async def test_mixed_201_inputs_have_exact_bounded_chunks_and_ordered_outcomes(intent_store):
    store = intent_store.store
    inputs = mixed(201)
    failures = {inputs[i].reporting_receipt_id for i in (0, 99, 100, 200)}
    client = ReceiptClient(fail=failures)
    result = await submit_reporting_receipts(
        client, authorizer=TrustedAuthorizer(client), store=store, receipts=inputs
    )
    assert not result.pending and result.diagnostic is None and not result.proposal_deferred
    assert len(result.outcomes) == 201 and len(result.submitted_receipts) == 197
    assert [outcome.ordinal for outcome in result.outcomes] == list(range(201))
    assert [outcome.submitted_receipt for outcome in result.outcomes] == inputs
    assert [
        len(call.get("receipts", [])) + len(call.get("adjustment_receipts", []))
        for call in client.requests
    ] == [100, 100, 1]
    assert len({call["idempotency_key"] for call in client.requests}) == 3
    assert all(call["account"] == {"account_id": SCOPE.account_id} for call in client.requests)
    for outcome in result.outcomes:
        if outcome.submitted_receipt.reporting_receipt_id in failures:
            assert outcome.result == "failed" and outcome.receipt is None
            assert outcome.error_codes == (
                ReportingReceiptFailureCode.REPORTING_RECORD_UNAVAILABLE,
                ReportingReceiptFailureCode.UNKNOWN,
            )
        else:
            assert outcome.result == "recorded"
            assert outcome.receipt.received_at.isoformat() == "2026-09-01T02:00:00+00:00"
    restored = await store.get(SCOPE)
    assert restored == result.submission
    replay = await submit_reporting_receipts(
        client, authorizer=TrustedAuthorizer(client), store=store, receipts=inputs
    )
    assert replay == result and len(client.requests) == 3
    assert SECRET not in repr(result) and "PRIVATE_SENTINEL" not in repr(result)
    assert all(b"PRIVATE_SENTINEL" not in chunk for chunk in restored._confirmed)


async def test_concurrent_different_proposals_reserve_one_exact_scope_intent(intent_store):
    store = intent_store.store
    proposals = [prepare_reporting_receipt_submission(SCOPE, [receipt(i)]) for i in range(8)]
    reserved = await asyncio.gather(*(store.reserve(proposal) for proposal in proposals))
    assert len({state.submission_id for state in reserved}) == 1
    winner = reserved[0]
    assert winner in proposals
    assert all(state == winner for state in reserved)
    assert await store.get(SCOPE) == winner
    confirmations = await asyncio.gather(
        *(
            store.confirm(SCOPE, winner.submission_id, 0, response_for(winner.request(0)))
            for _ in range(8)
        )
    )
    assert all(not state.pending for state in confirmations)
    assert len({state._confirmed for state in confirmations}) == 1
    assert (await store.reserve(winner)) == confirmations[0]
    loser = next(
        proposal for proposal in proposals if proposal.submission_id != winner.submission_id
    )
    assert await store.reserve(loser) == loser
    # An old concurrent acknowledgement cannot clear the new pending lane.
    await store.confirm(
        SCOPE, winner.submission_id, 0, response_for(winner.request(0), result="unchanged")
    )
    assert await store.get(SCOPE) == loser
    assert await store.reserve(winner) == loser


@pytest.mark.parametrize("part", ["seller_id", "account_id", "consumer_id"])
async def test_exact_scope_isolates_seller_account_and_canonical_consumer(intent_store, part):
    first = prepare_reporting_receipt_submission(SCOPE, mixed())
    scope = replace(SCOPE, **{part: "other-identity"})
    second = prepare_reporting_receipt_submission(scope, mixed())
    assert first.submission_id != second.submission_id
    assert first.request(0).idempotency_key != second.request(0).idempotency_key
    store = intent_store.store
    await asyncio.gather(store.reserve(first), store.reserve(second))
    assert await store.get(SCOPE) == first and await store.get(scope) == second
    assert await store.get(scope, first.submission_id) is None
    with pytest.raises(ReportingSubmissionError) as error:
        await store.confirm(scope, first.submission_id, 0, response_for(first.request(0)))
    assert error.value.code == ReportingSubmissionCode.NOT_FOUND


async def test_maximum_url_principal_survives_real_index_and_restart(intent_store):
    principal = "https://buyer.example.test/" + "x" * (2048 - len("https://buyer.example.test/"))
    scope = replace(SCOPE, consumer_id=principal)
    state = prepare_reporting_receipt_submission(scope, mixed())
    await intent_store.store.reserve(state)
    assert await intent_store.store.get(scope) == state
    assert len(scope.storage_key) == 64
    assert principal not in repr(scope) and principal not in repr(state)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "extra",
        "wrong_id",
        "wrong_kind",
        "wrong_revision",
        "wrong_materialization",
        "wrong_obligation",
        "wrong_adjustment",
        "wrong_digest",
        "wrong_status",
        "wrong_time",
        "optional_field_removed",
    ],
)
async def test_no_chunk_ack_before_complete_unique_response_and_immutable_body_validation(
    intent_store, mutation
):
    inputs = [receipt(), receipt(1, adjustment=True)]
    inputs[0].consumer_commit_ref = "public-load-1"
    state = prepare_reporting_receipt_submission(SCOPE, inputs)
    store = intent_store.store
    await store.reserve(state)
    body = response_for(state.request(0)).model_dump(mode="json", exclude_none=True)
    results = body["results"]
    if mutation == "missing":
        results.pop()
    elif mutation == "duplicate":
        results[1] = results[0]
    elif mutation == "extra":
        results.append(
            {
                "result": "failed",
                "reporting_receipt_id": "unrequested-receipt-1",
                "errors": [{"code": "INVALID_REQUEST", "message": SECRET}],
            }
        )
    elif mutation == "wrong_id":
        results[0]["receipt"]["reporting_receipt_id"] = "unrequested-receipt-1"
    elif mutation == "wrong_kind":
        results[0] = {
            "result": "recorded",
            "adjustment_receipt": {
                **results[1]["adjustment_receipt"],
                "reporting_receipt_id": inputs[0].reporting_receipt_id,
            },
        }
    elif mutation == "wrong_revision":
        results[0]["receipt"]["reporting_revision_id"] = "other-revision"
    elif mutation == "wrong_materialization":
        results[0]["receipt"]["reporting_materialization_id"] = "other-materialization"
    elif mutation == "wrong_obligation":
        results[0]["receipt"]["reporting_obligation_id"] = "other-obligation"
    elif mutation == "wrong_adjustment":
        results[1]["adjustment_receipt"]["reporting_adjustment_id"] = "other-adjustment"
    elif mutation == "wrong_digest":
        results[1]["adjustment_receipt"]["observed_adjustment_sha256"] = "c" * 64
    elif mutation == "wrong_status":
        results[1]["adjustment_receipt"]["status"] = "rejected"
        results[1]["adjustment_receipt"]["rejection_codes"] = ["ADJUSTMENT_DIGEST_MISMATCH"]
    elif mutation == "wrong_time":
        results[1]["adjustment_receipt"]["observed_at"] = RECEIVED
    elif mutation == "optional_field_removed":
        del results[0]["receipt"]["consumer_commit_ref"]
    response = SyncReportingReceiptsResponse.model_validate(body)
    with pytest.raises(ReportingSubmissionError) as error:
        await store.confirm(SCOPE, state.submission_id, 0, response)
    assert error.value.code == ReportingSubmissionCode.INVALID_RESPONSE
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert await store.get(SCOPE) == state
    replacement = prepare_reporting_receipt_submission(SCOPE, [receipt(99)])
    assert await store.reserve(replacement) == state


async def test_confirmations_are_prefix_ordered_and_first_outcomes_are_immutable(intent_store):
    store = intent_store.store
    state = await store.reserve(prepare_reporting_receipt_submission(SCOPE, mixed(101)))
    with pytest.raises(ReportingSubmissionError) as error:
        await store.confirm(SCOPE, state.submission_id, 1, response_for(state.request(1)))
    assert error.value.code == ReportingSubmissionCode.HISTORY_CORRUPT
    failed_id = state.request(0).receipts[0].reporting_receipt_id
    first = await store.confirm(
        SCOPE, state.submission_id, 0, response_for(state.request(0), fail={failed_id})
    )
    duplicate = await store.confirm(
        SCOPE,
        state.submission_id,
        0,
        response_for(state.request(0), fail={failed_id}, result="unchanged"),
    )
    assert duplicate == first
    assert any(outcome.result == "failed" for outcome in duplicate.outcomes)
    assert duplicate.pending
    with pytest.raises(ReportingSubmissionError) as contradiction:
        await store.confirm(SCOPE, state.submission_id, 0, response_for(state.request(0)))
    assert contradiction.value.code == ReportingSubmissionCode.INVALID_RESPONSE
    assert await store.get(SCOPE) == first
    completed = await store.confirm(SCOPE, state.submission_id, 1, response_for(state.request(1)))
    assert not completed.pending and completed._confirmed[:1] == first._confirmed
    assert len(completed.outcomes) == 101


def test_outbound_capture_is_immutable_and_each_public_model_view_is_detached():
    inputs = mixed()
    first = prepare_reporting_receipt_submission(SCOPE, inputs)
    same = prepare_reporting_receipt_submission(SCOPE, inputs)
    assert first == same
    inputs[0].observed_adjustment_sha256 = "f" * 64
    first.request(0).adjustment_receipts[0].observed_adjustment_sha256 = "e" * 64
    assert same.request(0).adjustment_receipts[0].observed_adjustment_sha256 == "b" * 64
    assert first.request(0).adjustment_receipts[0].observed_adjustment_sha256 == "b" * 64
    assert (
        prepare_reporting_receipt_submission(SCOPE, list(reversed(mixed()))).submission_id
        != first.submission_id
    )
    assert "account-a" not in repr(first) and "observed_at" not in repr(first)


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "duplicate",
        "received_at",
        "too_many",
        "oversized",
        "malformed",
        "accepted_rejections",
    ],
)
def test_admission_rejects_invalid_whole_plans_before_reservation(case):
    inputs = mixed()
    if case == "empty":
        inputs = []
    elif case == "duplicate":
        inputs[1].reporting_receipt_id = inputs[0].reporting_receipt_id
    elif case == "received_at":
        inputs[0].received_at = inputs[0].observed_at
    elif case == "too_many":
        inputs = mixed(10001)
    elif case == "oversized":
        inputs[1].consumer_commit_ref = "x" * (1024 * 1024)
    elif case == "malformed":
        inputs[0].reporting_receipt_id = SECRET
    elif case == "accepted_rejections":
        inputs[0].rejection_codes = ["ADJUSTMENT_DIGEST_MISMATCH"]
    with pytest.raises(ReportingSubmissionError) as error:
        prepare_reporting_receipt_submission(SCOPE, inputs)
    assert error.value.code == ReportingSubmissionCode.INVALID_PLAN
    assert SECRET not in str(error.value) and error.value.__context__ is None


@pytest.mark.parametrize(
    "consumer",
    [
        "anonymous",
        " buyer ",
        "https://a:b@buyer.invalid",
        "https://buyer.invalid/?token=PRIVATE_SENTINEL",
        "x" * 2049,
    ],
)
def test_trusted_identity_syntax_is_bounded_and_errors_are_closed(consumer):
    with pytest.raises(ReportingSubmissionError) as error:
        replace(SCOPE, consumer_id=consumer)
    assert error.value.code == ReportingSubmissionCode.INVALID_SCOPE
    assert error.value.__context__ is None and "PRIVATE_SENTINEL" not in str(error.value)


async def test_corrupt_proposal_is_never_an_escape_from_pending_intent(intent_store):
    good = prepare_reporting_receipt_submission(SCOPE, mixed())
    await intent_store.store.reserve(good)
    plan = json.loads(good._plan)
    plan["requests"][0]["account"] = {"account_id": "request-asserted-other-account"}
    bad = replace(good, _plan=json.dumps(plan).encode())
    with pytest.raises(ReportingSubmissionError) as error:
        await intent_store.store.reserve(bad)
    assert error.value.code == ReportingSubmissionCode.HISTORY_CORRUPT
    assert await intent_store.store.get(SCOPE) == good


async def test_retained_response_byte_limit_fails_closed_before_confirmation(
    intent_store, monkeypatch
):
    state = prepare_reporting_receipt_submission(SCOPE, mixed())
    await intent_store.store.reserve(state)
    response = response_for(state.request(0))
    # Sanitized retention adds an explicit ID per result. Check that limit too,
    # even when the actual incoming response is within its byte budget.
    limit = len(canonical_json_utf8_v1(response.model_dump(mode="json", exclude_none=True)))
    monkeypatch.setattr(submission_models, "MAX_RESPONSE_BYTES", limit)
    with pytest.raises(ReportingSubmissionError) as error:
        await intent_store.store.confirm(SCOPE, state.submission_id, 0, response)
    assert error.value.code == ReportingSubmissionCode.INVALID_RESPONSE
    assert await intent_store.store.get(SCOPE) == state


async def test_cumulative_confirmation_bound_preserves_the_confirmed_prefix(
    intent_store, monkeypatch
):
    store = intent_store.store
    state = await store.reserve(prepare_reporting_receipt_submission(SCOPE, mixed(101)))
    first = await store.confirm(SCOPE, state.submission_id, 0, response_for(state.request(0)))
    # Both individual responses fit. The retained array has only enough room
    # for the first one; the second must not partially commit or free the scope.
    retained = canonical_json_utf8_v1([json.loads(chunk) for chunk in first._confirmed])
    monkeypatch.setattr(submission_models, "MAX_CONFIRMATION_BYTES", len(retained))
    response = response_for(state.request(1))
    with pytest.raises(ReportingSubmissionError) as error:
        await store.confirm(SCOPE, state.submission_id, 1, response)
    assert error.value.code == ReportingSubmissionCode.INVALID_RESPONSE
    assert await store.get(SCOPE) == first
    assert await store.reserve(prepare_reporting_receipt_submission(SCOPE, [receipt(500)])) == first
