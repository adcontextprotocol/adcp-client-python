"""Account-serialized receipt batches on the existing unconditional rollback model."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger._delivery_state import RecordT
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingDeliveryPrincipal,
    ReportingReceiptRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.status_snapshot import settle_memory_snapshot
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer.capture import private_snapshot
from adcp.reporting.materializer.memory import InMemoryReportingMaterializerStore
from adcp.reporting.receipts.capture import ReportingReceiptBoundary
from adcp.reporting.receipts.errors import ReportingReceiptError
from adcp.reporting.receipts.records import (
    failed_result,
    prepare_receipt,
    receipt_record,
    success_result,
)
from adcp.reporting.receipts.wire import (
    ReceiptBatch,
    validate_receipt_response,
    validate_receipt_results,
)
from adcp.server.helpers import inject_context


@dataclass
class _BatchState:
    batch: ReceiptBatch
    expected_count: int
    created_at: datetime
    results: tuple[dict[str, Any], ...] = ()
    response: bytes | None = None


class InMemoryReportingReceiptStore(InMemoryReportingMaterializerStore):
    """Reference participant. No production tier or notification readiness claim."""

    # Lazily initialized deliberately: first-use allocations must roll back too.
    _receipt_batches: dict[tuple[str, str, str], _BatchState]
    _receipt_boundaries: list[ReportingReceiptBoundary]
    _receipt_status_heads: dict[ReportingDeliveryPrincipal, int]

    def _receipt_batch(
        self, caller: ReportingDeliveryPrincipal, batch: ReceiptBatch
    ) -> _BatchState:
        if not hasattr(self, "_receipt_batches"):
            self._receipt_batches = {}
        key = caller.account_id, caller.consumer_id, batch.key
        state = self._receipt_batches.get(key)
        if state is None:
            state = _BatchState(batch, len(batch.items), self._clock())
            self._receipt_batches[key] = state
        elif state.batch.canonical_request != batch.canonical_request:
            raise ReportingReceiptError("IDEMPOTENCY_CONFLICT")
        if state.expected_count != len(batch.items) or len(state.results) > state.expected_count:
            raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
        validate_receipt_results(list(state.results), batch)
        return state

    def _append_receipt_result(self, state: _BatchState, result: dict[str, Any]) -> None:
        state.results = (*state.results, deepcopy(result))

    def _save_receipt_response(self, state: _BatchState) -> None:
        response = inject_context(
            state.batch.request, {"status": "completed", "results": deepcopy(list(state.results))}
        )
        validate_receipt_response(response, state.batch)
        state.response = canonical_json_utf8_v1(response)

    def _assemble_receipt_response(self, state: _BatchState) -> dict[str, Any]:
        if state.response is None:
            raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
        response: dict[str, Any] = json.loads(state.response)
        validate_receipt_response(response, state.batch)
        if response["results"] != list(state.results):
            raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
        return response

    async def ingest_receipt_batch(
        self, request: dict[str, Any], *, caller: ReportingDeliveryPrincipal
    ) -> dict[str, Any]:
        batch = ReceiptBatch.parse(request)
        while True:
            # Each turn chooses the next *durable* ordinal under the account
            # lock. Concurrent/resumed callers cooperate; no process-local batch
            # lock, pending task, or TTL is a correctness dependency.
            async with self._mutation():
                state = self._receipt_batch(caller, batch)
                if state.response is not None:
                    return self._assemble_receipt_response(state)
                if len(state.results) == state.expected_count:
                    self._save_receipt_response(state)
                    return self._assemble_receipt_response(state)
                kind, item = batch.items[len(state.results)]
                candidate = None
                try:
                    revision_id = item.get(
                        "reporting_revision_id", item.get("adjusts_reporting_revision_id")
                    )
                    revision = (
                        self._revisions.get(revision_id) if isinstance(revision_id, str) else None
                    )
                    obligation_id = item.get("reporting_obligation_id")
                    if (
                        kind == "adjustment_receipt"
                        and revision is not None
                        and revision.account_id == caller.account_id
                    ):
                        obligation_id = revision.reporting_obligation_id
                    obligation = (
                        self._obligations.get(obligation_id)
                        if isinstance(obligation_id, str)
                        else None
                    )
                    candidate = receipt_record(kind, item, caller, obligation)
                    records = tuple(c.record for c in self._caller_changes(caller))
                    stored, added = prepare_receipt(
                        candidate,
                        records,
                        self._delivery_context(candidate),
                        tuple(
                            r
                            for r in self._revisions.values()
                            if obligation is not None
                            and r.account_id == caller.account_id
                            and r.reporting_obligation_id == obligation.reporting_obligation_id
                        ),
                        self._clock(),
                    )
                    result = success_result(stored, added)
                except LedgerConflictError as error:
                    result = failed_result(item, error)
                    added = False
                # Nothing in the semantic-error catch above mutates domain state.
                # Any insertion/capture/result failure below escapes and rolls
                # this entire ordinal back, with notifications both off and on.
                if added:
                    assert candidate is not None
                    stored, inserted = self._commit_record_unlocked(candidate)
                    assert inserted
                    result = success_result(stored, True)
                self._append_receipt_result(state, result)

    def _commit_record_unlocked(
        self, record: RecordT, *, notify: bool = True, dirty: bool = True
    ) -> tuple[RecordT, bool]:
        stored, added = super()._commit_record_unlocked(record, notify=notify, dirty=dirty)
        if added and isinstance(
            stored, (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord)
        ):
            self._capture_receipt(stored)
        return stored, added

    def _capture_receipt(self, receipt: ReportingReceiptRecord) -> None:
        assert receipt.received_at is not None
        caller = receipt.scope.principal
        if not hasattr(self, "_receipt_status_heads"):
            self._receipt_status_heads = {}
        if not hasattr(self, "_receipt_boundaries"):
            self._receipt_boundaries = []
        sequence = self._receipt_status_heads.get(caller, 0) + 1
        self._receipt_status_heads[caller] = sequence
        # Preserve cross-kind account order using B2.1's original capture clock.
        # No materializer work, retry decision, or readiness event is created.
        account_sequence = self._materializer_account_heads.get(caller.account_id, 0) + 1
        self._materializer_account_heads[caller.account_id] = account_sequence
        core = replace(settle_memory_snapshot(self, caller.account_id), as_of=receipt.received_at)
        self._receipt_boundaries.append(
            ReportingReceiptBoundary(
                caller,
                sequence,
                account_sequence,
                receipt.reporting_receipt_id,
                receipt.received_at,
                private_snapshot(core, caller),
                tuple(c.record for c in self._caller_changes(caller)),
            )
        )

    async def read_receipt_boundaries(
        self, *, caller: ReportingDeliveryPrincipal, after: int = 0, limit: int = 100
    ) -> tuple[ReportingReceiptBoundary, ...]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ReportingReceiptError("INVALID_REQUEST")
        async with self._lock:
            return tuple(
                b
                for b in getattr(self, "_receipt_boundaries", ())
                if b.caller == caller and b.sequence > after
            )[:limit]
