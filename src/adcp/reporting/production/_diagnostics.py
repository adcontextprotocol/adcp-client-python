"""Closed operator signals for an unexpectedly stopped production worker."""

from __future__ import annotations

import logging
from typing import Literal

_WorkerBoundary = Literal[
    "producer", "materializer", "projection", "sweeper", "notifications", "worker"
]
_BOUNDARIES = frozenset(
    {"producer", "materializer", "projection", "sweeper", "notifications", "worker"}
)
_LOGGER = logging.getLogger("adcp.reporting.production")


def _boundary_label(value: object) -> str:
    return value if type(value) is str and value in _BOUNDARIES else "worker"


def _worker_stopped(*, boundary: _WorkerBoundary) -> None:
    """No exception, provider/request object or ambient context enters the record."""
    if not _LOGGER.isEnabledFor(logging.ERROR):
        return
    record = logging.LogRecord(
        _LOGGER.name, logging.ERROR, "", 0, "Reporting production worker stopped", (), None
    )
    # Bypass ambient LogRecordFactory additions. Names may themselves carry
    # adopter data, so this diagnostic does not retain them or a traceback.
    record.threadName = None
    record.processName = None
    if hasattr(record, "taskName"):
        record.taskName = None
    record.__dict__.update(
        code="REPORTING_PRODUCTION_WORKER_STOPPED",
        boundary=_boundary_label(boundary),
    )
    try:
        _LOGGER.handle(record)
    except Exception:
        # The failed/stop latch has already committed. A broken operator sink
        # cannot prevent sibling shutdown or replace the closed public error.
        return


_MAINTENANCE_OPERATIONS = frozenset({"intent_sweep", "period_retirement", "change_pruning"})


def _maintenance_failed(*, operation: str) -> None:
    """A maintenance pass failed. The worker keeps running; the next interval retries.

    Only the closed operation name is recorded, never an exception, a provider
    message or an identifier.
    """
    if not _LOGGER.isEnabledFor(logging.WARNING):
        return
    record = logging.LogRecord(
        _LOGGER.name, logging.WARNING, "", 0, "Reporting maintenance pass failed", (), None
    )
    record.threadName = None
    record.processName = None
    if hasattr(record, "taskName"):
        record.taskName = None
    record.__dict__.update(
        code="REPORTING_MAINTENANCE_FAILED",
        operation=operation if operation in _MAINTENANCE_OPERATIONS else "maintenance",
    )
    try:
        _LOGGER.handle(record)
    except Exception:
        return
