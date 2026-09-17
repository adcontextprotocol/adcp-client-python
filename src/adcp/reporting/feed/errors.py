"""Closed errors for authorized frozen reporting reads."""

from __future__ import annotations

from typing import Literal

FeedErrorCode = Literal[
    "INVALID_REQUEST",
    "INVALID_CHECKPOINT",
    "UNAUTHORIZED",
    "REPORTING_FEED_SCHEMA_UNREADY",
    "REPORTING_FEED_HISTORY_CORRUPT",
    "REPORTING_FEED_STORAGE_UNAVAILABLE",
]

_MESSAGES: dict[FeedErrorCode, str] = {
    "INVALID_REQUEST": "supply a periods request with valid reporting filters and pagination",
    "INVALID_CHECKPOINT": "restart the reporting walk; this position is unavailable for this scope",
    "UNAUTHORIZED": "the reporting account or authenticated consumer is unavailable",
    "REPORTING_FEED_SCHEMA_UNREADY": "install and verify the isolated reporting feed schema",
    "REPORTING_FEED_HISTORY_CORRUPT": "retained reporting feed evidence requires operator repair",
    "REPORTING_FEED_STORAGE_UNAVAILABLE": (
        "reporting feed storage is unavailable; retry the request"
    ),
}


class ReportingFeedError(RuntimeError):
    def __init__(self, code: FeedErrorCode) -> None:
        self.code = code
        super().__init__(_MESSAGES[code])
