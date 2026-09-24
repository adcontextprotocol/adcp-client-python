"""Pure preparation against exact, account-locked retained evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from adcp.reporting.evidence import ReportingCanonicalDigest
from adcp.reporting.ledger._delivery_state import DeliveryContext, fail, replay, validate_transition
from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingControlTotalRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingDeliveryScope,
    ReportingReceiptRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.models import (
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.receipts.wire import ReceiptKind
from adcp.reporting.revision_selection import select_reporting_revision

_REVISION_RECEIPT = TypeAdapter(ReportingRevisionReceiptRecord)
_ADJUSTMENT_RECEIPT = TypeAdapter(ReportingAdjustmentReceiptRecord)


def receipt_record(
    kind: ReceiptKind,
    item: dict[str, Any],
    caller: ReportingDeliveryPrincipal,
    obligation: ReportingObligationRecord | None,
) -> ReportingReceiptRecord:
    if obligation is None or obligation.account_id != caller.account_id:
        fail("REPORTING_RECORD_UNAVAILABLE")
    body = dict(item)
    body.pop("reporting_obligation_id", None)
    body["scope"] = ReportingDeliveryScope(
        obligation.generation_key, caller.consumer_id, obligation.reporting_obligation_id
    )
    value: ReportingReceiptRecord | None = None
    try:
        if kind == "revision_receipt":
            body["observed_control_totals"] = tuple(
                ReportingControlTotalRecord(**total) for total in body["observed_control_totals"]
            )
            digest = body.get("observed_canonical_content_digest")
            if digest is not None:
                body["observed_canonical_content_digest"] = ReportingCanonicalDigest(
                    **{k: v for k, v in digest.items() if k != "algorithm"}
                )
        adapter = _REVISION_RECEIPT if kind == "revision_receipt" else _ADJUSTMENT_RECEIPT
        value = adapter.validate_python(body)
    except (ValueError, TypeError, ValidationError):
        # Reject the record below without attaching private validation details as context.
        pass
    if value is None:
        fail("INVALID_REPORTING_RECORD")
    return value


def prepare_receipt(
    record: ReportingReceiptRecord,
    records: tuple[ReportingDeliveryRecord, ...],
    context: DeliveryContext,
    revisions: tuple[ReportingRevisionRecord, ...],
    now: datetime,
) -> tuple[ReportingReceiptRecord, bool]:
    existing = replay(record, records)
    if existing is not None:
        return cast(ReportingReceiptRecord, existing), False
    if context.obligation is None:
        fail("REPORTING_RECORD_UNAVAILABLE")
    obligation = context.obligation
    selection = select_reporting_revision(
        revisions,
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        required_finality=obligation.required_finality,
    )
    if selection.kind == "corrupt":
        fail("REPORTING_HISTORY_CORRUPT")
    # Historical snapshot receipts remain independently repairable after official
    # publication. Evidence is bound to the supplied artifact, never the latest try.
    return cast(ReportingReceiptRecord, validate_transition(record, records, context, now)), True


def success_result(record: ReportingReceiptRecord, added: bool) -> dict[str, Any]:
    key = "receipt" if isinstance(record, ReportingRevisionReceiptRecord) else "adjustment_receipt"
    return {"result": "recorded" if added else "unchanged", key: receipt_to_wire(record)}


def failed_result(item: dict[str, Any], error: LedgerConflictError) -> dict[str, Any]:
    # The shared state machine uses closed codes. Never persist exception text or
    # database details, and never distinguish an invisible target from a missing one.
    return {
        "result": "failed",
        "reporting_receipt_id": item["reporting_receipt_id"],
        "errors": [{"code": error.code, "message": "reporting receipt could not be recorded"}],
    }
