"""Public ownership/liveness APIs used by a strictly typed adopter."""

from typing import Protocol

from adcp.reporting import (
    ReliableReportingService,
    ReliableReportingServiceError,
    ReliableReportingShutdownTimeoutError,
    ReliableReportingState,
    ReliableReportingUnavailableError,
    ReportingServiceResource,
)
from adcp.reporting.ledger import ReportingConfiguration, ReportingLedgerStore
from adcp.reporting.service import ReportingContextResolver


class AsyncResource(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...


def compose(
    store: ReportingLedgerStore,
    context: ReportingContextResolver,
    owned: AsyncResource,
) -> ReliableReportingService:
    # The store is borrowed. Only the explicitly transferred resource closes.
    return ReliableReportingService(
        store=store,
        account_context=context,
        owned_resources=(ReportingServiceResource(open=owned.open, close=owned.close),),
    )


async def serve_one_turn(
    service: ReliableReportingService, configuration: ReportingConfiguration
) -> None:
    try:
        await service.configure(configuration)
        await service.run_worker()
    except ReliableReportingUnavailableError as unavailable:
        state: ReliableReportingState = unavailable.state
        assert state is not ReliableReportingState.READY
    finally:
        try:
            await service.close(timeout=5)
        except ReliableReportingShutdownTimeoutError:
            # Retain the service and its loop until admitted work has settled.
            await service.close()
    try:
        await service.wait()
    except ReliableReportingServiceError as failure:
        component: str = failure.component
        assert component
