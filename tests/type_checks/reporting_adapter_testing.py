"""Public test-kit composition for a strictly typed adapter author."""

from datetime import timedelta

from adcp.reporting import (
    DeterministicReportingClock,
    FaultInjectingReportingSealStore,
    FaultInjectingReportingStagingStore,
    ReportingFailurePlan,
    ReportingLifecycleSnapshot,
    ReportingTestBarrier,
    ScriptedReportingAdapter,
    assert_reporting_lifecycle_replay,
    capture_reporting_lifecycle,
    run_reporting_adapter_conformance,
)
from adcp.reporting.inline_source import ReportingSealStore, ReportingStagingStore
from adcp.reporting.ledger import ReportingLedgerStore
from adcp.reporting.service import ReportingAdapter
from adcp.reporting.source import ReportingSourceSliceRequestV1, SourceBatchManifestV1


async def source_replay(
    adapter: ReportingAdapter,
    request: ReportingSourceSliceRequestV1,
    staging: ReportingStagingStore,
    seals: ReportingSealStore,
) -> SourceBatchManifestV1:
    clock = DeterministicReportingClock(request.period.source_read_cutoff_at)
    clock.advance(timedelta(0))
    failures = ReportingFailurePlan()
    objects: ReportingStagingStore = FaultInjectingReportingStagingStore(staging, failures)
    replay: ReportingSealStore = FaultInjectingReportingSealStore(seals, failures)
    return await run_reporting_adapter_conformance(
        adapter, request, clock=clock, staging=objects, seals=replay
    )


async def script(adapter: ReportingAdapter) -> ScriptedReportingAdapter:
    failures = ReportingFailurePlan()
    barrier = ReportingTestBarrier()
    failures.at("fetch.before", None, barrier, OSError("test fault"))
    result = ScriptedReportingAdapter(
        adapter.capabilities, [[], []], asynchronous=True, failures=failures
    )
    barrier.release()
    return result


async def checkpoint(
    before: ReportingLedgerStore,
    after: ReportingLedgerStore,
    account_id: str,
    obligation_id: str,
) -> ReportingLifecycleSnapshot:
    snapshot = await capture_reporting_lifecycle(
        before, account_id=account_id, reporting_obligation_id=obligation_id
    )
    await assert_reporting_lifecycle_replay(snapshot, after)
    return snapshot
