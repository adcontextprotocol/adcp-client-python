"""Immutable receipt dirty inputs; projection activation belongs to B2.4."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from adcp.reporting.evidence import aware_utc
from adcp.reporting.ledger._delivery_state import decode_record, payload, principal
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.status_projection import ReportingStatusSnapshot
from adcp.reporting.receipts.errors import ReportingReceiptError

_CORE = TypeAdapter(ReportingStatusSnapshot)


@dataclass(frozen=True)
class ReportingReceiptBoundary:
    caller: ReportingDeliveryPrincipal
    sequence: int
    account_sequence: int
    reporting_receipt_id: str
    as_of: datetime
    core: ReportingStatusSnapshot = field(repr=False)
    reconciliation: tuple[ReportingDeliveryRecord, ...] = field(repr=False)

    def __post_init__(self) -> None:
        receipts = tuple(
            r
            for r in self.reconciliation
            if isinstance(r, (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord))
            and r.reporting_receipt_id == self.reporting_receipt_id
        )
        if (
            type(self.sequence) is not int
            or self.sequence < 1
            or type(self.account_sequence) is not int
            or self.account_sequence < self.sequence
            or self.core.account_id != self.caller.account_id
            or self.core.as_of != self.as_of
            or self.core.consumer_ids != (self.caller.consumer_id,)
            or any(s.consumer_id != self.caller.consumer_id for s in self.core.statuses)
            or any(
                i.consumer_id not in {None, self.caller.consumer_id} for i in self.core.lifecycles
            )
            or any(principal(r) != self.caller for r in self.reconciliation)
            or len(receipts) != 1
            or receipts[0].received_at != self.as_of
        ):
            raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
        object.__setattr__(self, "as_of", aware_utc(self.as_of))

    def to_storage(self) -> dict[str, Any]:
        return {
            "version": 1,
            "admission_epoch": 0,
            "account_id": self.caller.account_id,
            "consumer_id": self.caller.consumer_id,
            "sequence": self.sequence,
            "account_sequence": self.account_sequence,
            "reporting_receipt_id": self.reporting_receipt_id,
            "as_of": self.as_of.isoformat(),
            "core": _CORE.dump_python(self.core, mode="json"),
            "reconciliation": [payload(r) for r in self.reconciliation],
        }


def decode_receipt_boundary(value: dict[str, Any]) -> ReportingReceiptBoundary:
    result = None
    try:
        if type(value) is dict and type(value.get("version")) is int and value["version"] == 1:
            result = ReportingReceiptBoundary(
                ReportingDeliveryPrincipal(value["account_id"], value["consumer_id"]),
                value["sequence"],
                value["account_sequence"],
                value["reporting_receipt_id"],
                datetime.fromisoformat(value["as_of"]),
                _CORE.validate_python(value["core"]),
                tuple(decode_record(r) for r in value["reconciliation"]),
            )
    except (ValueError, TypeError, KeyError, ValidationError):
        pass
    if result is None or result.to_storage() != value:
        raise ReportingReceiptError("RECEIPT_HISTORY_CORRUPT")
    return result
