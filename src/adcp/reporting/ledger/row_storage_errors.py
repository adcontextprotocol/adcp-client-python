"""Stable, secret-free row-storage failures."""

from __future__ import annotations

from typing import Literal

__all__ = ["ReportingRowStoreError", "ReportingRowStoreErrorCode"]

ReportingRowStoreErrorCode = Literal[
    "INVALID_INPUT",
    "CONTENT_CONFLICT",
    "ROWS_INTEGRITY_FAILED",
    "ROWS_UNAVAILABLE",
    "ROWS_EXPIRED",
    "PROVIDER_UNAVAILABLE",
    "DEADLINE_EXCEEDED",
    "ABORTED",
    "UNSAFE_BINDING",
    "STATE_UNAVAILABLE",
]


class ReportingRowStoreError(RuntimeError):
    """A stable, secret-free row-storage failure. Switch on :attr:`code`.

    Operator codes are never buyer-facing: callers collapse them before a wire
    response so a buyer cannot probe whether an object exists. Raw provider
    errors are never surfaced.
    """

    def __init__(self, code: ReportingRowStoreErrorCode, detail: str | None = None) -> None:
        super().__init__(
            f"Reporting row store: {code}: {detail}" if detail else f"Reporting row store: {code}"
        )
        self.code: ReportingRowStoreErrorCode = code
        self.detail = detail
