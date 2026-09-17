"""Closed, payload-free receipt ingress failures."""

from __future__ import annotations

from typing import Literal

ReceiptErrorCode = Literal[
    "INVALID_REQUEST",
    "UNAUTHORIZED",
    "IDEMPOTENCY_CONFLICT",
    "RECEIPT_SCHEMA_UNREADY",
    "RECEIPT_HISTORY_CORRUPT",
    "RECEIPT_STORAGE_UNAVAILABLE",
]

_MESSAGES: dict[ReceiptErrorCode, str] = {
    "INVALID_REQUEST": "supply a valid receipt batch with 1..100 distinct receipt IDs",
    "UNAUTHORIZED": "the reporting account or authenticated consumer is unavailable",
    "IDEMPOTENCY_CONFLICT": "reuse the original receipt batch body or choose a new batch key",
    "RECEIPT_SCHEMA_UNREADY": "install and verify the isolated receipt ingestion schema",
    "RECEIPT_HISTORY_CORRUPT": "retained receipt batch evidence requires operator repair",
    "RECEIPT_STORAGE_UNAVAILABLE": "receipt storage is unavailable; retry the same batch and key",
}


class ReportingReceiptError(RuntimeError):
    """An actionable classification; never includes request, driver or auth details."""

    def __init__(self, code: ReceiptErrorCode) -> None:
        self.code = code
        super().__init__(_MESSAGES[code])
