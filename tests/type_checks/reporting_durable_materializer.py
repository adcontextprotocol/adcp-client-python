"""The optional durable protocol and public lazy imports remain strict-adopter APIs."""

from typing_extensions import assert_type

from adcp.reporting.materializer import (
    InMemoryReportingMaterializerStore,
    PgReportingMaterializerStore,
    ReportingDestinationIO,
    ReportingDestinationWriter,
    ReportingMaterializerLease,
    ReportingMaterializerService,
    ReportingMaterializerStore,
    ReportingMaterializerTurn,
    ReportingVerificationKey,
)


async def adopter(
    postgres: PgReportingMaterializerStore,
    memory: InMemoryReportingMaterializerStore,
    io: ReportingDestinationIO,
    writer: ReportingDestinationWriter,
    key: ReportingVerificationKey,
) -> None:
    store: ReportingMaterializerStore = postgres
    store = memory
    service = ReportingMaterializerService(store, io, writer)
    assert_type(await service.run_once(), ReportingMaterializerTurn)
    lease = await store.claim_materialization(keys=(key,))
    if isinstance(lease, ReportingMaterializerLease):
        assert_type(await store.renew_materialization(lease, lease_seconds=30), bool)
        await store.authorize_materialization(lease)
