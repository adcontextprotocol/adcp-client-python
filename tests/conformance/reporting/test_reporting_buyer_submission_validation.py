"""Scaled validation work, exact-byte cache isolation and bounded retention."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.receipts.wire import ReceiptBatch
from adcp.reporting.submissions import (
    InMemoryReportingSubmissionIntentStore,
    ReportingSubmissionCode,
    ReportingSubmissionError,
    models,
    prepare_reporting_receipt_submission,
    submit_reporting_receipts,
)
from adcp.reporting.submissions._validation_cache import ValidationCache

from ._buyer_submission_support import SCOPE, ReceiptClient, TrustedAuthorizer, mixed, response_for


def cold_caches(monkeypatch):
    monkeypatch.setattr(models, "PLANS", ValidationCache(entries=16, byte_budget=32 * 1024 * 1024))
    monkeypatch.setattr(
        models, "CONFIRMATIONS", ValidationCache(entries=256, byte_budget=32 * 1024 * 1024)
    )


async def test_thousand_mixed_items_validate_schemas_once_per_chunk_and_revalidate_after_eviction(
    monkeypatch,
):
    cold_caches(monkeypatch)
    parses = responses = 0
    parse = ReceiptBatch.parse
    validate_response = models.validate_receipt_response

    def counting_parse(cls, body):
        nonlocal parses
        parses += 1
        return parse(body)

    def counting_response(body, batch):
        nonlocal responses
        responses += 1
        return validate_response(body, batch)

    monkeypatch.setattr(ReceiptBatch, "parse", classmethod(counting_parse))
    monkeypatch.setattr(models, "validate_receipt_response", counting_response)
    client, store = ReceiptClient(), InMemoryReportingSubmissionIntentStore()
    result = await submit_reporting_receipts(
        client, authorizer=TrustedAuthorizer(client), store=store, receipts=mixed(1000)
    )
    assert not result.pending and len(result.outcomes) == 1000
    assert len(client.requests) == parses == responses == 10
    for _ in range(3):
        assert await store.get(SCOPE) == result.submission
    assert (parses, responses) == (10, 10)
    cold_caches(monkeypatch)
    assert await store.get(SCOPE) == result.submission
    assert (parses, responses) == (20, 20)
    assert await store.get(SCOPE) == result.submission
    assert (parses, responses) == (20, 20)


@pytest.mark.parametrize("changed", ["scope", "id", "plan", "mutable_plan", "mutable_prefix"])
def test_warm_proof_never_uses_object_or_digest_identity(changed, monkeypatch):
    cold_caches(monkeypatch)
    state = prepare_reporting_receipt_submission(SCOPE, mixed())
    models.validate_submission(state)
    if changed == "scope":
        state = replace(state, scope=replace(SCOPE, consumer_id="another-consumer"))
    elif changed == "id":
        state = replace(state, submission_id="reporting-submission:" + "0" * 64)
    elif changed == "plan":
        plan = json.loads(state._plan)
        plan["requests"][0]["account"]["account_id"] = "asserted-account"
        state = replace(state, _plan=canonical_json_utf8_v1(plan))
    elif changed == "mutable_plan":
        state = replace(state, _plan=bytearray(state._plan))
    else:
        state = replace(state, _confirmed=[])
    with pytest.raises(ReportingSubmissionError) as error:
        models.validate_submission(state)
    assert error.value.code == ReportingSubmissionCode.HISTORY_CORRUPT


@pytest.mark.parametrize("changed", ["body", "space", "order", "wrong_chunk"])
async def test_warm_confirmation_proof_requires_exact_bytes_and_exact_chunk(changed, monkeypatch):
    cold_caches(monkeypatch)
    store = InMemoryReportingSubmissionIntentStore()
    state = await store.reserve(prepare_reporting_receipt_submission(SCOPE, mixed(101)))
    for chunk in range(2):
        state = await store.confirm(
            SCOPE, state.submission_id, chunk, response_for(state.request(chunk))
        )
    models.validate_submission(state)
    first = json.loads(state._confirmed[0])
    if changed == "body":
        first[0]["receipt"]["observed_manifest_sha256"] = "e" * 64
        altered = (canonical_json_utf8_v1(first), *state._confirmed[1:])
    elif changed == "space":
        altered = (state._confirmed[0] + b" ", *state._confirmed[1:])
    elif changed == "order":
        altered = (canonical_json_utf8_v1(list(reversed(first))), *state._confirmed[1:])
    else:
        altered = tuple(reversed(state._confirmed))
    with pytest.raises(ReportingSubmissionError) as error:
        models.validate_submission(replace(state, _confirmed=altered))
    assert error.value.code == ReportingSubmissionCode.HISTORY_CORRUPT
    assert await store.get(SCOPE) == state


async def test_cached_evidence_has_no_mutable_views_or_raw_private_diagnostics(monkeypatch):
    cold_caches(monkeypatch)
    inputs = mixed()
    client = ReceiptClient(fail={inputs[0].reporting_receipt_id})
    result = await submit_reporting_receipts(
        client,
        authorizer=TrustedAuthorizer(client),
        store=InMemoryReportingSubmissionIntentStore(),
        receipts=inputs,
    )
    before = result.submission._plan, result.submission._confirmed
    inputs.clear()
    result.submission.request(0).receipts.clear()
    result.outcomes[1].submitted_receipt.observed_manifest_sha256 = "e" * 64
    result.outcomes[1].receipt.observed_manifest_sha256 = "f" * 64
    assert (result.submission._plan, result.submission._confirmed) == before
    for cache in (models.PLANS, models.CONFIRMATIONS):
        for key, (value, _) in cache._values.items():
            assert type(key) is tuple and type(value) is tuple
            assert all(type(part) is bytes for part in (*key, *value))
            assert b"PRIVATE_SENTINEL" not in b"".join((*key, *value))
    models.validate_submission(result.submission)


@pytest.mark.parametrize("entries,budget", [(2, 100000), (100, 2500)])
def test_validation_proof_retention_has_entry_and_conservative_byte_bounds(entries, budget):
    cache = ValidationCache(entries=entries, byte_budget=budget)
    for index in range(10):
        key, value = (str(index).encode(),), (b"validated",)
        cache.put(key, value)
        assert cache.get(key) == value
        assert len(cache._values) <= entries
        assert cache._retained_bytes <= budget
    assert cache.get((b"0",)) is None
    cache.put((b"too large",), (b"x" * budget,))
    assert cache.get((b"too large",)) is None
    assert cache._retained_bytes == sum(size for _, size in cache._values.values())
    with pytest.raises(TypeError):
        cache.put((b"mutable",), (bytearray(b"x"),))
