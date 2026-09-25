"""Optional buyer intent protocol, independent of ReportingCheckpointStore."""

from __future__ import annotations

import asyncio
from typing import Protocol

from adcp.reporting.submissions.models import (
    ReportingReceiptSubmission,
    ReportingSubmissionCode,
    ReportingSubmissionError,
    ReportingSubmissionScope,
    confirm_submission,
    validate_submission,
)
from adcp.types import SyncReportingReceiptsResponse


class ReportingSubmissionIntentStore(Protocol):
    """Atomic exact-request reservation and monotonic confirmed chunk storage.

    Adopters implementing this protocol must run the shared store vectors against
    their durable backend. A scope has one unresolved reservation, never a TTL,
    lease expiry, or automatic abandonment. Each mutation commits before return.
    No transport call may run while holding a storage transaction or lock.

    The supplied scope is already resolved/authorized by a trusted adapter. The
    store is an internal persistence boundary, not a request authentication API.
    Old ReportingCheckpointStore implementations need no new methods.
    """

    async def reserve(self, proposed: ReportingReceiptSubmission) -> ReportingReceiptSubmission:
        """Atomically reserve or return the scope's earlier unresolved intent.

        A different proposal must not replace an uncertain request. An exact
        already completed proposal returns its retained outcomes. Preserve all
        request bytes, chunk keys/order and confirmed outcomes across restart.
        """
        ...

    async def get(
        self, scope: ReportingSubmissionScope, submission_id: str | None = None
    ) -> ReportingReceiptSubmission | None:
        """Read a validated reservation, including completed work after a lost return.

        With no ID return the latest reserved intent; do not erase it when it
        completes. Never resolve an ID outside the exact trusted scope.
        """
        ...

    async def confirm(
        self,
        scope: ReportingSubmissionScope,
        submission_id: str,
        chunk: int,
        response: SyncReportingReceiptsResponse,
    ) -> ReportingReceiptSubmission:
        """Atomically acknowledge full unique ID/body coverage for the next chunk.

        Retain item successes AND failures in the original plan order. Confirmed
        chunks never change. A concurrent duplicate acknowledgement returns the
        retained state; it cannot replace an earlier confirmed outcome.
        """
        ...


class InMemoryReportingSubmissionIntentStore:
    """Volatile reference implementation for tests; it cannot survive restart.

    Production callers must explicitly supply a durable implementation such as
    PgReportingSubmissionIntentStore. There is no implicit in-memory fallback.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._scopes: dict[str, tuple[bytes, str]] = {}
        self._submissions: dict[tuple[str, str], ReportingReceiptSubmission] = {}

    def _current(self, scope: ReportingSubmissionScope) -> ReportingReceiptSubmission | None:
        entry = self._scopes.get(scope.storage_key)
        if entry is None:
            return None
        if entry[0] != scope.canonical_identity:
            raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
        current = self._submissions.get((scope.storage_key, entry[1]))
        if current is None or current.scope != scope:
            raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
        validate_submission(current)
        return current

    async def reserve(self, proposed: ReportingReceiptSubmission) -> ReportingReceiptSubmission:
        validate_submission(proposed)
        if proposed.confirmed_chunks:
            raise ReportingSubmissionError(ReportingSubmissionCode.INVALID_PLAN)
        async with self._lock:
            current = self._current(proposed.scope)
            if current is not None and current.pending:
                return current
            key = proposed.scope.storage_key, proposed.submission_id
            prior = self._submissions.get(key)
            if prior is not None:
                validate_submission(prior)
                if prior._plan != proposed._plan or prior.scope != proposed.scope:
                    raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
                return prior
            self._submissions[key] = proposed
            self._scopes[key[0]] = proposed.scope.canonical_identity, proposed.submission_id
            return proposed

    async def get(
        self, scope: ReportingSubmissionScope, submission_id: str | None = None
    ) -> ReportingReceiptSubmission | None:
        async with self._lock:
            current = self._current(scope)
            if current is None or submission_id is None:
                return current
            result = self._submissions.get((scope.storage_key, submission_id))
            if result is not None:
                validate_submission(result)
                if result.scope != scope:
                    raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
            return result

    async def confirm(
        self,
        scope: ReportingSubmissionScope,
        submission_id: str,
        chunk: int,
        response: SyncReportingReceiptsResponse,
    ) -> ReportingReceiptSubmission:
        async with self._lock:
            current = self._current(scope)
            state = self._submissions.get((scope.storage_key, submission_id))
            if current is None or state is None:
                raise ReportingSubmissionError(ReportingSubmissionCode.NOT_FOUND)
            validate_submission(state)
            if state.scope != scope or (
                state.pending and state.submission_id != current.submission_id
            ):
                raise ReportingSubmissionError(ReportingSubmissionCode.HISTORY_CORRUPT)
            updated = confirm_submission(state, chunk, response)
            self._submissions[(scope.storage_key, submission_id)] = updated
            return updated
