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


_MAINTENANCE_OPERATIONS = frozenset(
    {"intent_sweep", "row_pruning", "period_retirement", "change_pruning"}
)
#: The only counters a maintenance event may carry: integers, never identifiers.
_MAINTENANCE_COUNTERS = frozenset(
    {
        "retired",
        "swept",
        "failed",
        "protected",
        "held",
        "deferred",
        "stuck",
        "skipped_unconfigured",
        "overdue",
    }
)


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


def _maintenance_event(
    *, code: str, level: int, operation: str, counts: dict[str, int], message: str
) -> None:
    """A maintenance pass left work undone or unseen. Only the closed operation name and
    integer counters are recorded, never an identifier, an exception or a provider message."""
    if not _LOGGER.isEnabledFor(level):
        return
    record = logging.LogRecord(_LOGGER.name, level, "", 0, message, (), None)
    record.threadName = None
    record.processName = None
    if hasattr(record, "taskName"):
        record.taskName = None
    record.__dict__.update(
        code=code,
        operation=operation if operation in _MAINTENANCE_OPERATIONS else "maintenance",
        **{
            name: value
            for name, value in counts.items()
            if name in _MAINTENANCE_COUNTERS and type(value) is int
        },
    )
    try:
        _LOGGER.handle(record)
    except Exception:
        return


def _maintenance_pending(*, operation: str, counts: dict[str, int]) -> None:
    """Records were left in place on purpose: protected, held, parked or owned elsewhere."""
    _maintenance_event(
        code="REPORTING_MAINTENANCE_PENDING",
        level=logging.INFO,
        operation=operation,
        counts=counts,
        message="Reporting maintenance left work in place",
    )


def _maintenance_stalled(*, operation: str, counts: dict[str, int]) -> None:
    """Work has waited far longer than any maintenance interval and nobody is handling it."""
    _maintenance_event(
        code="REPORTING_MAINTENANCE_STALLED",
        level=logging.WARNING,
        operation=operation,
        counts=counts,
        message="Reporting maintenance work is stalled",
    )
