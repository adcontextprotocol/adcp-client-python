"""Closed, payload-free diagnostics for unexpected receipt storage failures."""

from __future__ import annotations

import logging
import re
from typing import Literal

_Boundary = Literal[
    "handler",
    "store.create_schema",
    "store.receipt_ingestion_ready",
    "store.ingest_receipt_batch",
    "store.read_receipt_boundaries",
]
_BOUNDARIES = frozenset(
    {
        "handler",
        "store.create_schema",
        "store.receipt_ingestion_ready",
        "store.ingest_receipt_batch",
        "store.read_receipt_boundaries",
    }
)
_LOGGER = logging.getLogger("adcp.reporting.receipts")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*", re.ASCII)


def _coordinate(value: object) -> str:
    # Invalid/dynamic paths are discarded, not partially retained by escaping.
    return (
        value
        if type(value) is str and len(value) <= 160 and _IDENTIFIER.fullmatch(value)
        else "unknown"
    )


def _storage_failure(error: Exception, *, boundary: _Boundary) -> None:
    """Emit only class and source coordinates, never the exception or its text."""
    if not _LOGGER.isEnabledFor(logging.ERROR):
        return
    origin = error.__traceback__
    while origin is not None and origin.tb_next is not None:
        origin = origin.tb_next
    fields = {
        "code": "RECEIPT_STORAGE_UNAVAILABLE",
        "boundary": boundary if boundary in _BOUNDARIES else "handler",
        "exception_type": _coordinate(type(error).__name__),
        "origin_module": (
            _coordinate(origin.tb_frame.f_globals.get("__name__")) if origin else "unknown"
        ),
        "origin_function": _coordinate(origin.tb_frame.f_code.co_name) if origin else "unknown",
        "origin_line": origin.tb_lineno if origin else 0,
    }
    # Construct a plain record so an ambient LogRecordFactory cannot attach
    # request context. Paths, task names and thread/process names can themselves
    # contain adopter data; none are part of this diagnostic's contract.
    record = logging.LogRecord(
        _LOGGER.name, logging.ERROR, "", 0, "Receipt storage is unavailable", (), None
    )
    record.threadName = None
    record.processName = None
    if hasattr(record, "taskName"):
        record.taskName = None
    record.__dict__.update(fields)
    try:
        _LOGGER.handle(record)
    except Exception:
        # A failing operator sink cannot replace the existing safe buyer error.
        # Never try a second logger, which could duplicate or expose the failure.
        return
