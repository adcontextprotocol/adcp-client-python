"""The same mixed-batch state machine runs in memory and on PostgreSQL 16."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import ReportingDeliveryPrincipal, ReportingMaterializationCheck
from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.receipts import ReportingReceiptError
from adcp.reporting.receipts.wire import ReceiptBatch, validate_receipt_response

from ._receipt_support import adjustment_for, batch_state, receipt_case, receipts, request_for

__all__ = ["receipts"]


async def test_mixed_batch_order_durable_failure_and_exact_response_replay(receipts):
    h = receipts
    s = await receipt_case(h, consumer_id="https://buyer.example.test/agent")
    adjustment = await adjustment_for(h, s)
    bad_revision = {
        **receipt_to_wire(s.receipt),
        "reporting_receipt_id": "revision-missing-0002",
        "reporting_revision_id": "missing",
    }
    bad_adjustment = {
        **adjustment,
        "reporting_receipt_id": "adjustment-missing-0002",
        "reporting_adjustment_id": "missing",
    }
    request = request_for(
        s,
        receipts=[bad_revision, receipt_to_wire(s.receipt)],
        adjustment_receipts=[adjustment, bad_adjustment],
        context={"trace": "original"},
    )
    result = await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    validate_receipt_response(result, ReceiptBatch.parse(request))
    assert [r["result"] for r in result["results"]] == ["failed", "recorded", "recorded", "failed"]
    assert result["results"][0]["reporting_receipt_id"] == bad_revision["reporting_receipt_id"]
    assert result["results"][3]["reporting_receipt_id"] == bad_adjustment["reporting_receipt_id"]
    assert result["results"][0]["errors"] == result["results"][3]["errors"]
    assert result["context"] == {"trace": "original"}
    captures = await h.store.read_receipt_boundaries(caller=s.attempt.scope.principal)
    assert len(captures) == 2 and captures[0].account_sequence < captures[1].account_sequence
    assert all(b.core.consumer_ids == (s.binding.consumer_id,) for b in captures)
    # A later authorization call can replay after readability/clock changes;
    # no item is recomputed and original recorded outcomes/time remain intact.
    await h.store.set_revision_readable(
        account_id=s.obligation.account_id,
        reporting_revision_id=s.revision.reporting_revision_id,
        readable=False,
    )
    h.clock.now += timedelta(days=450)
    before = await h.image()
    replay = await h.store.ingest_receipt_batch(deepcopy(request), caller=s.attempt.scope.principal)
    assert replay == result and "replayed" not in replay
    assert await h.image() == before
    replay["results"].clear()
    receipt_operation_1 = await h.store.ingest_receipt_batch(
        request, caller=s.attempt.scope.principal
    )
    assert receipt_operation_1 == result


@pytest.mark.parametrize(
    "case",
    [
        "absent",
        "empty",
        "empty_sibling",
        "null",
        "not_list",
        "duplicate",
        "cross_duplicate",
        "too_many",
        "received_at",
        "adjustment_received_at",
        "spoof",
        "bad_late_shape",
    ],
)
async def test_whole_shape_admission_has_zero_writes(receipts, case):
    h = receipts
    s = await receipt_case(h)
    adjustment = await adjustment_for(h, s)
    request = request_for(s)
    if case == "absent":
        request.pop("receipts")
    if case == "empty":
        request["receipts"] = []
    if case == "empty_sibling":
        request["adjustment_receipts"] = []
    if case == "null":
        request["adjustment_receipts"] = None
    if case == "not_list":
        request["receipts"] = {}
    if case == "duplicate":
        request["receipts"] *= 2
    if case == "cross_duplicate":
        request["adjustment_receipts"] = [
            {**adjustment, "reporting_receipt_id": s.receipt.reporting_receipt_id}
        ]
    if case == "too_many":
        request["receipts"] = [
            {**request["receipts"][0], "reporting_receipt_id": f"receipt-many-{i:06d}"}
            for i in range(50)
        ]
        request["adjustment_receipts"] = [
            {**adjustment, "reporting_receipt_id": f"adjustment-many-{i:06d}"} for i in range(51)
        ]
    if case == "received_at":
        request["receipts"][0]["received_at"] = s.receipt.observed_at.isoformat()
    if case == "adjustment_received_at":
        request["adjustment_receipts"] = [{**adjustment, "received_at": None}]
    if case == "spoof":
        request["consumer_id"] = "other"
    if case == "bad_late_shape":
        request["adjustment_receipts"] = [{"reporting_receipt_id": "invalid-late-0001"}]
    before = await h.image()
    with pytest.raises(ReportingReceiptError) as error:
        await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    assert error.value.code == "INVALID_REQUEST"
    assert await h.image() == before


@pytest.mark.parametrize(
    "change", ["order", "context", "extension", "target", "omitted_array", "time_spelling"]
)
async def test_whole_request_conflict_before_any_write(receipts, change):
    h = receipts
    s = await receipt_case(h)
    request = request_for(
        s,
        receipts=[
            receipt_to_wire(s.receipt),
            {
                **receipt_to_wire(s.receipt),
                "reporting_receipt_id": "receipt-second-0002",
                "reporting_revision_id": "missing",
            },
        ],
    )
    await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    changed = deepcopy(request)
    if change == "order":
        changed["receipts"].reverse()
    if change == "context":
        changed["context"] = {"trace": "changed"}
    if change == "extension":
        changed["ext"] = {"test": {"version": 1}}
    if change == "target":
        changed["receipts"][1]["reporting_revision_id"] = "another"
    if change == "omitted_array":
        changed["receipts"].pop()
    if change == "time_spelling":
        changed["receipts"][0]["observed_at"] = changed["receipts"][0]["observed_at"].replace(
            "Z", "+00:00"
        )
    before = await h.image()
    with pytest.raises(ReportingReceiptError) as error:
        await h.store.ingest_receipt_batch(changed, caller=s.attempt.scope.principal)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert await h.image() == before


async def test_concurrent_same_batch_and_changed_body_converge(receipts):
    h = receipts
    s = await receipt_case(h)
    adjustment = await adjustment_for(h, s)
    request = request_for(s, adjustment_receipts=[adjustment])
    responses = await asyncio.gather(
        *(
            h.store.ingest_receipt_batch(deepcopy(request), caller=s.attempt.scope.principal)
            for _ in range(8)
        )
    )
    assert all(response == responses[0] for response in responses)
    assert [r["result"] for r in responses[0]["results"]] == ["recorded", "recorded"]
    assert await batch_state(h) == ((2, True),)
    assert len(await h.store.read_receipt_boundaries(caller=s.attempt.scope.principal)) == 2
    other = {**request, "context": {"changed": True}}
    responses = await asyncio.gather(
        h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal),
        h.store.ingest_receipt_batch(other, caller=s.attempt.scope.principal),
        return_exceptions=True,
    )
    assert responses[0]["results"][0]["result"] == "recorded"
    assert (
        isinstance(responses[1], ReportingReceiptError)
        and responses[1].code == "IDEMPOTENCY_CONFLICT"
    )


async def test_same_ids_keys_scoped_to_exact_accounts_and_consumers(receipts):
    h = receipts
    cases = [
        await receipt_case(h, account_id=a, consumer_id=c)
        for a, c in (
            ("acct_a", "buyer"),
            ("acct_a", "https://buyer.example.test/agent"),
            ("acct_b", "buyer"),
            ("acct:a", "b:c"),
            ("acct:a:b", "c"),
        )
    ]
    for s in cases:
        response = await h.store.ingest_receipt_batch(
            request_for(s), caller=s.attempt.scope.principal
        )
        assert response["results"][0]["result"] == "recorded"
    assert len(await batch_state(h)) == len(cases)
    s = cases[0]
    unauthorized = await h.store.ingest_receipt_batch(
        request_for(s, key="unavailable-key-0001"),
        caller=ReportingDeliveryPrincipal("acct_a", "other"),
    )
    unknown = await h.store.ingest_receipt_batch(
        request_for(
            s,
            key="unavailable-key-0002",
            receipts=[{**receipt_to_wire(s.receipt), "reporting_revision_id": "missing"}],
        ),
        caller=ReportingDeliveryPrincipal("acct_a", "other"),
    )
    assert unauthorized == unknown


async def test_rejected_leaf_replacement_terminal_acceptance_and_no_retry_signal(receipts):
    h = receipts
    s = await receipt_case(h)
    request = request_for(
        s,
        receipts=[
            {**receipt_to_wire(s.receipt), "status": "rejected", "rejection_codes": ["LOAD_FAILED"]}
        ],
    )
    work_before = await h.works()
    first = await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    assert first["results"][0]["receipt"]["status"] == "rejected"
    assert await h.works() == work_before
    corrected = {
        **receipt_to_wire(s.receipt),
        "reporting_receipt_id": "receipt-replacement-0002",
        "supersedes_reporting_receipt_id": s.receipt.reporting_receipt_id,
    }
    accepted = await h.store.ingest_receipt_batch(
        request_for(s, key="replacement-batch-0002", receipts=[corrected]),
        caller=s.attempt.scope.principal,
    )
    assert accepted["results"][0]["receipt"]["status"] == "accepted"
    terminal = await h.store.ingest_receipt_batch(
        request_for(
            s,
            key="terminal-batch-0003",
            receipts=[
                {
                    **corrected,
                    "reporting_receipt_id": "receipt-after-accept-0003",
                    "supersedes_reporting_receipt_id": corrected["reporting_receipt_id"],
                }
            ],
        ),
        caller=s.attempt.scope.principal,
    )
    assert terminal["results"][0]["errors"][0]["code"] == "ACCEPTED_RECEIPT_TERMINAL"
    replay = await h.store.ingest_receipt_batch(
        request_for(s, key="unchanged-batch-0004", receipts=[corrected]),
        caller=s.attempt.scope.principal,
    )
    assert replay["results"][0] == {**accepted["results"][0], "result": "unchanged"}


@pytest.mark.parametrize("later", ["success", "failure", "expiry", "corruption"])
async def test_accepted_artifact_authority_survives_later_outcomes_separately_from_readability(
    receipts, later
):
    h = receipts
    s = await receipt_case(h)
    result = await h.store.ingest_receipt_batch(request_for(s), caller=s.attempt.scope.principal)
    original = result["results"][0]["receipt"]
    if later in {"success", "failure"}:
        attempt = replace(s.attempt, reporting_materialization_id="materialization-2", attempt=2)
        await h.store.commit_materialization_attempt(attempt)
        outcome = replace(
            s.outcome, reporting_materialization_id=attempt.reporting_materialization_id
        )
        if later == "failure":
            outcome = replace(
                outcome,
                status="failed",
                resource=None,
                verification=None,
                failure_code="CONTENT_CORRUPT",
            )
        await h.store.commit_materialization(outcome)
    at = h.clock.now
    if later == "expiry":
        at = s.outcome.resource.expires_at
    if later == "corruption":
        await h.store.record_materialization_check(
            ReportingMaterializationCheck(
                s.attempt.scope,
                s.attempt.reporting_materialization_id,
                "check-corrupt",
                "corrupt",
                h.clock.now,
            )
        )
    snapshot = await h.store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
    assert len(snapshot.current_receipts) == 1
    assert receipt_to_wire(snapshot.current_receipts[0]) == original
    view = snapshot.materialization(s.attempt.key)
    assert view.outcome == s.outcome
    assert view.readable_at(at) is (later not in {"expiry", "corruption"})
    receipt_operation_2 = await h.store.ingest_receipt_batch(
        request_for(s), caller=s.attempt.scope.principal
    )
    assert receipt_operation_2 == result


async def test_hundred_combined_results_and_adjustment_only_admission(receipts):
    h = receipts
    s = await receipt_case(h)
    adjustment = await adjustment_for(h, s)
    request = request_for(
        s,
        receipts=[
            {**receipt_to_wire(s.receipt), "reporting_receipt_id": f"revision-{i:010d}"}
            for i in range(50)
        ],
        adjustment_receipts=[
            {**adjustment, "reporting_receipt_id": f"adjustment-{i:010d}"} for i in range(50)
        ],
    )
    result = await h.store.ingest_receipt_batch(request, caller=s.attempt.scope.principal)
    assert len(result["results"]) == 100
    assert sum(r["result"] == "recorded" for r in result["results"]) == 2
    only = {
        "account": request["account"],
        "idempotency_key": "only-adjustment-0001",
        "adjustment_receipts": [request["adjustment_receipts"][0]],
    }
    receipt_operation_3 = await h.store.ingest_receipt_batch(only, caller=s.attempt.scope.principal)
    assert (receipt_operation_3)["results"][0]["result"] == "unchanged"


@pytest.mark.parametrize(
    "context",
    [
        {"field": "nul\x00value"},
        {"nul\x00key": "value"},
        {"field": "\ud800"},
        {"\udfff": "value"},
        {"field": "\ud83d\ude00"},
    ],
)
async def test_unrepresentable_jsonb_strings_fail_whole_shape_before_any_header(receipts, context):
    h = receipts
    s = await receipt_case(h)
    before = await h.image()
    with pytest.raises(ReportingReceiptError) as error:
        await h.store.ingest_receipt_batch(
            request_for(s, context=context), caller=s.binding.principal
        )
    assert error.value.code == "INVALID_REQUEST"
    assert await h.image() == before


async def test_whole_request_utf16_key_order_survives_storage_and_exact_replay(receipts):
    h = receipts
    s = await receipt_case(h)
    context = {
        "nested": {"\ue000": "BMP", "\U00010000": "supplementary"},
        "array": [{"\U0001f600": "face", "e\u0301": "combining", "x": "a\\b\n"}],
    }
    request = request_for(s, context=context)
    response = await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    assert response["results"][0]["result"] == "recorded"
    assert response["context"] == context
    receipt_operation_4 = await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    assert receipt_operation_4 == response
    before = await h.image()
    changed = {**request, "context": {**context, "nested": {"\ue000": "changed"}}}
    with pytest.raises(ReportingReceiptError) as error:
        await h.store.ingest_receipt_batch(changed, caller=s.binding.principal)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert await h.image() == before
