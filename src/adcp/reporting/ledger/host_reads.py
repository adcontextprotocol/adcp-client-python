"""Serving-grade reads for in-process consumers (pacing, alerts, warehouse sinks).

Shared SDK persistence spec, adcontextprotocol/adcp#7996, section 3.7. These return
revision *headers*; rows are read separately through the verified paged reader, and
nothing here reads from a warehouse. ``PgReportingLedgerStore.get_current_revision`` and
``list_current_revisions`` implement them.
"""

from __future__ import annotations

from dataclasses import dataclass

from adcp.reporting.ledger.models import ReportingRevisionRecord

__all__ = ["ReportingCurrentRevisionsPageV1"]


@dataclass(frozen=True, slots=True)
class ReportingCurrentRevisionsPageV1:
    """A page of current revision headers; ``cursor`` is ``None`` on the last page."""

    revisions: tuple[ReportingRevisionRecord, ...]
    cursor: str | None
