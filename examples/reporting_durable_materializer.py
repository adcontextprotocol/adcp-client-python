"""Durable seller composition; install migrations and drain legacy writers first.

Supply the application's installed verifier registry and trusted destination
resolver/writer pair. Sessions fetch credentials afresh for each I/O phase.
This B2.1 unit preserves private polling inputs and atomic notification intents;
it does not activate Managed/Reconciled capability advertising or HTTP delivery.
"""

from __future__ import annotations

import asyncio

from adcp.reporting.materializer import (
    PgReportingMaterializerStore,
    ReportingDestinationIO,
    ReportingDestinationResolver,
    ReportingDestinationWriter,
    ReportingMaterializerService,
    ReportingRevisionVerifierRegistry,
    ReportingWriterError,
    ReportingWriterFailure,
)


async def compose_materializer(
    store: PgReportingMaterializerStore,
    *,
    registry: ReportingRevisionVerifierRegistry,
    resolver: ReportingDestinationResolver,
    writer: ReportingDestinationWriter,
) -> ReportingMaterializerService:
    """Validate installed storage, then construct the single SDK orchestration path.

    The store's notifications option is an explicit deployment choice. It is
    frozen per reservation, so a replacement process cannot silently downgrade
    enabled work. Readiness of the complete seller offering is proved separately
    by its installed components, never by an adopter-maintained ready boolean.
    """
    if not writer.production_eligible:
        raise ReportingWriterError(ReportingWriterFailure("UNSUPPORTED_VERIFICATION"))
    await store.materializer_ready()
    return ReportingMaterializerService(
        store,
        ReportingDestinationIO(registry, resolver),
        writer,
        lease_seconds=30,
        io_timeout_seconds=300,
    )


async def run_materializer(service: ReportingMaterializerService, stop: asyncio.Event) -> None:
    """No account inventory: each turn claims bounded, fair durable work."""
    while not stop.is_set():
        turn = await service.run_once()
        # Observe only these closed fields in application metrics. Destination
        # credentials, signed locations and provider error text stay in sessions.
        if turn.state in {"idle", "parked", "pending"}:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
