"""Optional bounded producer progress; the original ledger protocol stays intact."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingRevisionRecord,
    iso_duration_to_timedelta,
)
from adcp.reporting.ledger.schedule import committed_periods
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.revision_selection import select_reporting_revision
from adcp.reporting.source import ReportingConstituent


@runtime_checkable
class ReportingProducerProgress(Protocol):
    """Durable, generation-scoped closing position and indexed unfinished work.

    Only the production participant installs this optional path. An acquired
    configuration still uses the original lease/fairness and source execution
    identities. Closing position, obligation and its work item co-commit.
    Acquisition completion reselects immutable history under the account lock.
    """

    async def producer_closed_through(
        self, configuration: ReportingConfiguration
    ) -> datetime | None: ...

    async def producer_constituents(
        self, configuration: ReportingConfiguration, obligation: ReportingObligationRecord
    ) -> tuple[ReportingConstituent, ...]: ...

    async def commit_producer_period(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        previous_end: datetime | None,
    ) -> ReportingObligationRecord: ...

    async def next_producer_obligations(
        self, configuration: ReportingConfiguration, *, now: datetime, limit: int
    ) -> tuple[str, ...]: ...

    async def finish_producer_acquisition(
        self, configuration: ReportingConfiguration, *, reporting_obligation_id: str
    ) -> None: ...


def check_next_period(
    configuration: ReportingConfiguration,
    obligation: ReportingObligationRecord,
    previous_end: datetime | None,
) -> None:
    near = (
        None
        if previous_end is None
        else previous_end + iso_duration_to_timedelta(configuration.schedule.delivery_sla)
    )
    expected = next(
        (
            p
            for p in committed_periods(configuration, near=near)
            if previous_end is None or p.end > previous_end
        ),
        None,
    )
    if obligation.generation_key != configuration.generation_key or expected != obligation.period:
        raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer closing position is invalid")


def acquisition_state(
    obligation: ReportingObligationRecord, revisions: Sequence[ReportingRevisionRecord]
) -> Literal["pending", "settled", "parked"]:
    """The existing acquire-obligation predicates, without a new re-read policy."""
    selection = select_reporting_revision(
        revisions,
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        required_finality=obligation.required_finality,
    )
    if selection.kind == "corrupt":
        return "parked"
    current = selection.revision if selection.kind == "selected" else None
    if current is not None and (current.finality == "official" or current.readable):
        return "settled"
    return "pending"
