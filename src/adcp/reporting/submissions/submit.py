"""Explicit low-level receipt submission with durable uncertainty semantics."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from math import isfinite
from typing import Protocol, TypeVar

from adcp.reporting.submissions.models import (
    ReportingReceiptSubmission,
    ReportingSubmissionCode,
    ReportingSubmissionError,
    ReportingSubmissionReceipt,
    ReportingSubmissionResult,
    ReportingSubmissionScope,
    prepare_reporting_receipt_submission,
    validate_submission,
)
from adcp.reporting.submissions.store import ReportingSubmissionIntentStore
from adcp.types import SyncReportingReceiptsRequest, SyncReportingReceiptsResponse
from adcp.types.core import TaskResult


class ReportingReceiptSubmissionClient(Protocol):
    async def sync_reporting_receipts(
        self, request: SyncReportingReceiptsRequest
    ) -> TaskResult[SyncReportingReceiptsResponse]: ...


class ReportingSubmissionAuthorizer(Protocol):
    """Trusted application adapter resolving the exact client's current access.

    Resolve seller identity from trusted client/registry configuration, account
    from an authorized seller account lookup, and consumer from authenticated
    credentials/registry. Never source these identities from receipt payloads,
    an asserted account reference, or a debug/raw response. Aliases must already
    be resolved. Raise on revoked access or an identity disagreement.

    Called before any store read/reservation, before each send, and before
    returning completed cached outcomes. It must authorize replay afresh too.
    """

    async def __call__(
        self, client: ReportingReceiptSubmissionClient
    ) -> ReportingSubmissionScope: ...


async def _authorize(
    client: ReportingReceiptSubmissionClient,
    authorizer: ReportingSubmissionAuthorizer,
    expected: ReportingSubmissionScope | None = None,
) -> ReportingSubmissionScope:
    resolved = None
    try:
        scope = await authorizer(client)
        if type(scope) is ReportingSubmissionScope:
            scope.__post_init__()
            if expected is None or scope == expected:
                resolved = scope
    except Exception:
        resolved = None
    if resolved is None:
        raise ReportingSubmissionError(ReportingSubmissionCode.UNAUTHORIZED)
    return resolved


_T = TypeVar("_T")


async def _storage(operation: Awaitable[_T]) -> _T:
    try:
        return await operation
    except ReportingSubmissionError as error:
        code = error.code
    except Exception:
        code = ReportingSubmissionCode.STORAGE_UNAVAILABLE
    raise ReportingSubmissionError(code)


def _check_state(
    state: ReportingReceiptSubmission,
    scope: ReportingSubmissionScope,
    previous: ReportingReceiptSubmission | None = None,
) -> None:
    validate_submission(state)
    if state.scope != scope or (
        previous is not None
        and (
            previous.submission_id != state.submission_id
            or previous._plan != state._plan
            or state._confirmed[: previous.confirmed_chunks] != previous._confirmed
        )
    ):
        raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)


async def submit_reporting_receipts(
    client: ReportingReceiptSubmissionClient,
    *,
    authorizer: ReportingSubmissionAuthorizer,
    store: ReportingSubmissionIntentStore,
    receipts: Sequence[ReportingSubmissionReceipt] | None = None,
    timeout_seconds: float = 30.0,
) -> ReportingSubmissionResult:
    """Reserve or resume a receipt plan without replacing uncertain work.

    Omit ``receipts`` to resume the latest intent for the freshly authorized
    scope, including its completed outcomes after a lost return. Otherwise the
    supplied plan is frozen and atomically reserved before ANY network write.
    An earlier pending scope intent takes priority, with proposal_deferred=True.
    The caller must reconsider the deferred plan against fresh seller history.

    A transport failure, timeout, failed task or malformed response leaves the
    chunk uncertain and returns pending=True. Retry uses the exact durable body
    and idempotency key. Cancellation propagates with the intent still retained.
    Storage failure raises a closed error; resume the stored scope because the
    last commit may have succeeded. There is no expiry/abandon/replacement path.

    Every item failure is retained alongside successes. Completion only means
    every submission outcome is confirmed; it does not mean every receipt was
    recorded, accepted, readable, or sufficient for definitive reconciliation.
    This API does not select history, build adjustment evidence, post consumer
    statuses, or modify the original reconciliation/checkpoint interfaces.
    """
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ReportingSubmissionError(ReportingSubmissionCode.INVALID_PLAN)
    scope = await _authorize(client, authorizer)
    proposed = (
        prepare_reporting_receipt_submission(scope, receipts) if receipts is not None else None
    )
    state = await _storage(store.reserve(proposed) if proposed is not None else store.get(scope))
    if state is None:
        raise ReportingSubmissionError(ReportingSubmissionCode.NOT_FOUND)
    _check_state(state, scope)
    deferred = proposed is not None and proposed.submission_id != state.submission_id
    while state.pending:
        await _authorize(client, authorizer, scope)
        chunk = state.confirmed_chunks
        request = state.request(chunk)
        response = None
        try:
            response = await asyncio.wait_for(
                client.sync_reporting_receipts(request), timeout=timeout_seconds
            )
        except Exception:
            response = None
        if response is None:
            return ReportingSubmissionResult(
                state, deferred, ReportingSubmissionCode.TRANSPORT_UNCERTAIN
            )
        if (
            response.success is not True
            or response.status != "completed"
            or not isinstance(response.data, SyncReportingReceiptsResponse)
        ):
            return ReportingSubmissionResult(
                state, deferred, ReportingSubmissionCode.RESPONSE_UNCONFIRMED
            )
        invalid = False
        try:
            updated = await _storage(
                store.confirm(scope, state.submission_id, chunk, response.data)
            )
        except ReportingSubmissionError as error:
            if error.code != ReportingSubmissionCode.INVALID_RESPONSE:
                raise
            invalid = True
        if invalid:
            return ReportingSubmissionResult(
                state, deferred, ReportingSubmissionCode.INVALID_RESPONSE
            )
        _check_state(updated, scope, state)
        if updated.confirmed_chunks <= chunk:
            raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
        state = updated
    await _authorize(client, authorizer, scope)
    return ReportingSubmissionResult(state, deferred)
