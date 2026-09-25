"""No timeout, cancellation, malformed reply or lost commit permits replacement."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from adcp.reporting.submissions import (
    ReportingSubmissionCode,
    ReportingSubmissionError,
    submit_reporting_receipts,
)
from adcp.types.core import TaskResult, TaskStatus

from ._buyer_submission_support import (
    SCOPE,
    SECRET,
    ReceiptClient,
    TrustedAuthorizer,
    intent_store,
    mixed,
    response_for,
)

__all__ = ["intent_store"]


class UncertainClient(ReceiptClient):
    def __init__(self, failure, *, fail_on=1):
        super().__init__()
        self.failure = failure
        self.fail_on = fail_on
        self.attempts = 0
        self.started = asyncio.Event()

    async def sync_reporting_receipts(self, request):
        self.attempts += 1
        self.started.set()
        if self.attempts != self.fail_on:
            return await super().sync_reporting_receipts(request)
        self.requests.append(request.model_dump(mode="json", exclude_none=True))
        if self.failure == "exception":
            raise RuntimeError(SECRET)
        if self.failure in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        if self.failure == "failed":
            return TaskResult(status=TaskStatus.FAILED, success=False, error=SECRET)
        if self.failure == "pending":
            return TaskResult(status=TaskStatus.WORKING, data=response_for(request))
        if self.failure == "malformed":
            response = response_for(request)
            response.results.pop()
            return TaskResult(status=TaskStatus.COMPLETED, data=response)
        raise AssertionError("unknown test fault")


@pytest.mark.parametrize(
    "failure,diagnostic",
    [
        ("exception", ReportingSubmissionCode.TRANSPORT_UNCERTAIN),
        ("timeout", ReportingSubmissionCode.TRANSPORT_UNCERTAIN),
        ("failed", ReportingSubmissionCode.RESPONSE_UNCONFIRMED),
        ("pending", ReportingSubmissionCode.RESPONSE_UNCONFIRMED),
        ("malformed", ReportingSubmissionCode.INVALID_RESPONSE),
    ],
)
async def test_uncertainty_replays_exact_prior_body_and_defers_new_proposal(
    intent_store, failure, diagnostic
):
    store = intent_store.store
    client = UncertainClient(failure)
    authorizer = TrustedAuthorizer(client)
    original = mixed()
    first = await submit_reporting_receipts(
        client, authorizer=authorizer, store=store, receipts=original, timeout_seconds=0.01
    )
    assert first.pending and first.diagnostic == diagnostic
    assert first.outcomes == () and "PRIVATE_SENTINEL" not in repr(first)
    earlier_request = client.requests[0]
    replacement = mixed(4)
    recovered = await submit_reporting_receipts(
        client, authorizer=authorizer, store=store, receipts=replacement
    )
    assert not recovered.pending and recovered.proposal_deferred
    assert recovered.submission.submission_id == first.submission.submission_id
    assert client.requests == [earlier_request, earlier_request]
    assert [outcome.submitted_receipt for outcome in recovered.outcomes] == original
    # The same deferred proposal is not automatically submitted after recovery.
    assert await store.get(SCOPE) == recovered.submission


async def test_second_chunk_timeout_preserves_first_chunk_outcomes_across_resume(intent_store):
    store = intent_store.store
    client = UncertainClient("exception", fail_on=2)
    authorizer = TrustedAuthorizer(client)
    first = await submit_reporting_receipts(
        client, authorizer=authorizer, store=store, receipts=mixed(201)
    )
    assert first.pending and len(first.outcomes) == 100
    recovered = await submit_reporting_receipts(client, authorizer=authorizer, store=store)
    assert not recovered.pending and len(recovered.outcomes) == 201
    assert recovered.outcomes[:100] == first.outcomes
    assert len(client.requests) == 4
    assert client.requests[1] == client.requests[2]
    assert client.requests[0]["idempotency_key"] != client.requests[2]["idempotency_key"]


async def test_cancellation_preserves_reserved_request(intent_store):
    client = UncertainClient("cancel")
    authorizer = TrustedAuthorizer(client)
    task = asyncio.create_task(
        submit_reporting_receipts(
            client, authorizer=authorizer, store=intent_store.store, receipts=mixed()
        )
    )
    await asyncio.wait_for(client.started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = await intent_store.store.get(SCOPE)
    assert pending.pending and not pending.outcomes
    recovered = await submit_reporting_receipts(
        client, authorizer=authorizer, store=intent_store.store
    )
    assert not recovered.pending and client.requests[0] == client.requests[1]


class FaultingStore:
    """Inject failures on either side of actual backend commits, including PG."""

    def __init__(self, delegate, fault):
        self.delegate = delegate
        self.fault = fault

    async def reserve(self, proposed):
        if self.fault == "before_reserve":
            raise RuntimeError(SECRET)
        state = await self.delegate.reserve(proposed)
        if self.fault == "after_reserve":
            raise RuntimeError(SECRET)
        return state

    async def get(self, scope, submission_id=None):
        return await self.delegate.get(scope, submission_id)

    async def confirm(self, scope, submission_id, chunk, response):
        if self.fault == "before_confirm":
            raise RuntimeError(SECRET)
        state = await self.delegate.confirm(scope, submission_id, chunk, response)
        if self.fault == "after_confirm":
            raise RuntimeError(SECRET)
        return state


@pytest.mark.parametrize(
    "fault", ["before_reserve", "after_reserve", "before_confirm", "after_confirm"]
)
async def test_storage_failure_is_closed_and_uncertainty_uses_committed_state(intent_store, fault):
    client = ReceiptClient()
    authorizer = TrustedAuthorizer(client)
    inputs = mixed()
    with pytest.raises(ReportingSubmissionError) as error:
        await submit_reporting_receipts(
            client,
            authorizer=authorizer,
            store=FaultingStore(intent_store.store, fault),
            receipts=inputs,
        )
    assert error.value.code == ReportingSubmissionCode.STORAGE_UNAVAILABLE
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert "PRIVATE_SENTINEL" not in repr(error.value)
    state = await intent_store.store.get(SCOPE)
    if fault == "before_reserve":
        assert state is None and not client.requests
    elif fault == "after_reserve":
        assert state.pending and not client.requests
    elif fault == "before_confirm":
        assert state.pending and len(client.requests) == 1
    else:
        assert not state.pending and len(client.requests) == 1
    recovered = await submit_reporting_receipts(
        client,
        authorizer=authorizer,
        store=intent_store.store,
        receipts=inputs if state is None else None,
    )
    assert not recovered.pending and len(recovered.outcomes) == 3
    assert len(client.requests) == (2 if fault == "before_confirm" else 1)
    if len(client.requests) == 2:
        assert client.requests[0] == client.requests[1]


@pytest.mark.parametrize("cached", [False, True])
async def test_revoked_authorization_blocks_reservation_and_cached_replay(intent_store, cached):
    client = ReceiptClient()
    authorizer = TrustedAuthorizer(client)
    if cached:
        await submit_reporting_receipts(
            client, authorizer=authorizer, store=intent_store.store, receipts=mixed()
        )
    authorizer.allowed = False
    before = await intent_store.store.get(SCOPE)
    with pytest.raises(ReportingSubmissionError) as error:
        await submit_reporting_receipts(
            client, authorizer=authorizer, store=intent_store.store, receipts=mixed(4)
        )
    assert error.value.code == ReportingSubmissionCode.UNAUTHORIZED
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert "PRIVATE_SENTINEL" not in str(error.value)
    assert await intent_store.store.get(SCOPE) == before
    assert len(client.requests) == int(cached)


async def test_authorizer_must_bind_the_exact_client_before_store_use(intent_store):
    client, different_client = ReceiptClient(), ReceiptClient()
    with pytest.raises(ReportingSubmissionError) as error:
        await submit_reporting_receipts(
            client,
            authorizer=TrustedAuthorizer(different_client),
            store=intent_store.store,
            receipts=mixed(),
        )
    assert error.value.code == ReportingSubmissionCode.UNAUTHORIZED
    assert await intent_store.store.get(SCOPE) is None and not client.requests


async def test_mid_submission_identity_change_cannot_send_under_another_scope(intent_store):
    client = ReceiptClient()

    class ChangedAuthorizer(TrustedAuthorizer):
        async def __call__(self, client):
            scope = await super().__call__(client)
            return replace(scope, consumer_id="other-buyer") if self.calls >= 3 else scope

    authorizer = ChangedAuthorizer(client)
    with pytest.raises(ReportingSubmissionError) as error:
        await submit_reporting_receipts(
            client, authorizer=authorizer, store=intent_store.store, receipts=mixed(101)
        )
    assert error.value.code == ReportingSubmissionCode.UNAUTHORIZED
    assert len(client.requests) == 1
    state = await intent_store.store.get(SCOPE)
    assert state.pending and state.confirmed_chunks == 1
    assert await intent_store.store.get(replace(SCOPE, consumer_id="other-buyer")) is None
    recovered = await submit_reporting_receipts(
        client, authorizer=TrustedAuthorizer(client), store=intent_store.store
    )
    assert not recovered.pending and len(client.requests) == 2


async def test_resume_before_any_intent_is_a_closed_missing_state(intent_store):
    client = ReceiptClient()
    with pytest.raises(ReportingSubmissionError) as error:
        await submit_reporting_receipts(
            client, authorizer=TrustedAuthorizer(client), store=intent_store.store
        )
    assert error.value.code == ReportingSubmissionCode.NOT_FOUND
    assert not client.requests
