"""Conditional metrics have the same meaning in Core and source conformance."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from typing import Any

import pytest

from adcp.reporting.conformance import (
    ReportingSourceConformanceError,
    run_reporting_source_replay_conformance,
    validate_reporting_source_execution,
    validate_reporting_source_failure,
)
from adcp.reporting.fixtures import SNAPSHOT_OFFERING_ID
from adcp.reporting.inline_source import InlineFetchResult, MetricEvidence
from adcp.reporting.ledger import LedgerConflictError, ReportingConfiguration
from adcp.reporting.service import ReliableReportingService, ReportingAccountContext
from adcp.reporting.source import (
    MetricOfferingV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceSliceRequestV1,
    parse_verified_source_batch_manifest_v1,
    reporting_source_capabilities_sha256_v1,
)
from adcp.reporting.testing import DeterministicReportingClock
from tests.test_reliable_reporting_service import _capability_offering

from ._generation_support import NOW, isolated_reporting_pool
from ._reliable_support import capabilities, configuration

METRICS = ("impressions", "clicks", "spend", "completed_views")


def _conditional_capabilities(
    *, exact_metrics: tuple[str, ...] = ("impressions", "spend")
) -> ReportingSourceCapabilitiesV1:
    base = capabilities()
    draft = base.model_copy(
        update={
            "offerings": [
                offering.model_copy(
                    update={
                        "metrics": [
                            MetricOfferingV1(
                                name=name,
                                support="exact" if name in exact_metrics else "partial",
                                reason=None if name in exact_metrics else "inventory_dependent",
                                semantic_contract_id=f"fixture.{name}",
                                semantic_contract_version="1",
                                semantic_contract_sha256="a" * 64,
                            )
                            for name in METRICS
                        ]
                    }
                )
                for offering in base.offerings
            ]
        }
    )
    return ReportingSourceCapabilitiesV1.model_validate(
        {
            **draft.model_dump(),
            "capabilities_sha256": reporting_source_capabilities_sha256_v1(draft),
        }
    )


class _ConditionalAdapter:
    def __init__(
        self,
        display_status: str,
        *,
        video_status: str = "present",
        unsupported_buys: tuple[str, ...] = (),
        exact_metrics: tuple[str, ...] = ("impressions", "spend"),
    ) -> None:
        self.capabilities = _conditional_capabilities(exact_metrics=exact_metrics)
        self.calls: list[ReportingSourceSliceRequestV1] = []
        self.display_status = display_status
        self.video_status = video_status
        self.unsupported_buys = unsupported_buys

    def fetch_slice(self, request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        self.calls.append(request)
        rows: list[dict[str, Any]] = []
        evidence: dict[str, dict[str, MetricEvidence]] = {}
        for item in request.coverage.constituents:
            if item.media_buy_id in self.unsupported_buys:
                evidence[item.constituent_id] = {
                    name: MetricEvidence.unavailable("not_applicable") for name in METRICS
                }
                continue
            status = (
                self.display_status if item.media_buy_id == "display-buy" else self.video_status
            )
            cell = {
                "present": MetricEvidence.present(request.period.end),
                "explicit_zero": MetricEvidence.explicit_zero(),
                "unsupported": MetricEvidence.unavailable("not_video_inventory"),
                "missing": MetricEvidence.missing("measurement_missing"),
                "delayed": MetricEvidence.delayed("measurement_pending"),
            }[status]
            row: dict[str, Any] = {
                "media_buy_id": item.media_buy_id,
                "impressions": 10,
                "clicks": 0,
                "spend": "1.25",
            }
            if status in {"present", "explicit_zero"}:
                row["completed_views"] = 0 if status == "explicit_zero" else 5
            rows.append(row)
            evidence[item.constituent_id] = {
                "clicks": MetricEvidence.explicit_zero(),
                "completed_views": cell,
            }
        return InlineFetchResult(rows=rows, currency=request.currency, cell_availability=evidence)


@pytest.fixture(params=["memory", "postgres"])
async def conditional_service(
    request: pytest.FixtureRequest,
) -> AsyncIterator[ReliableReportingService]:
    source_capabilities = _conditional_capabilities()

    def context(config: ReportingConfiguration) -> ReportingAccountContext:
        return ReportingAccountContext(
            account_id=config.account_id,
            adapter="conditional",
            currency="EUR",
            source_scope=source_capabilities.source_scope,
            snapshot_offering_id=SNAPSHOT_OFFERING_ID,
            requested_metrics=METRICS,
            publication_namespace="reporting-source:fixture",
            capability_offering=_capability_offering("conditional"),
        )

    clock = DeterministicReportingClock(NOW)
    async with AsyncExitStack() as stack:
        if request.param == "postgres":
            pool = await stack.enter_async_context(isolated_reporting_pool())
            service = ReliableReportingService.postgres(
                pool=pool, account_context=context, clock=clock
            )
        else:
            service = ReliableReportingService.memory(account_context=context, clock=clock)
        stack.push_async_callback(service.close)
        yield service


@pytest.mark.parametrize(
    ("display_status", "video_status", "completed_views_total"),
    [
        pytest.param("present", "present", "10", id="measured-everywhere"),
        pytest.param("explicit_zero", "present", "5", id="measured-zero"),
        pytest.param("unsupported", "present", None, id="conditional-inventory"),
        pytest.param("unsupported", "unsupported", None, id="unsupported-everywhere"),
    ],
)
async def test_full_core_publication_and_conformance_accept_conditional_metrics(
    conditional_service: ReliableReportingService,
    display_status: str,
    video_status: str,
    completed_views_total: str | None,
) -> None:
    service = conditional_service
    adapter = _ConditionalAdapter(display_status, video_status=video_status)
    registration = service.sources.register("conditional", adapter)
    config = replace(configuration("eur"), media_buy_ids=("display-buy", "video-buy"))
    await service.configure(config)
    turn = await service.run_worker(now=NOW)

    assert not turn.configuration_errors
    worker = turn.configurations[config.generation_key]
    assert not worker.slices_failed
    assert len(worker.revisions_committed) == 1
    (request,) = adapter.calls
    assert request.coverage.expected == "full"
    assert tuple(request.requested_metrics) == METRICS
    assert registration.object_reader is not None
    manifest = await run_reporting_source_replay_conformance(
        executor=registration.executor,
        request=request,
        object_reader=registration.object_reader,
        clock=lambda: NOW,
    )
    # Conformance reads the same sealed execution without calling the adapter again.
    assert len(adapter.calls) == 1
    assert manifest.coverage.status == "full"
    assert {item.status for item in manifest.coverage.constituents} == {"present"}
    owners = {
        item.constituent_id: item.constituent.media_buy_id
        for item in manifest.coverage.constituents
    }
    conditional_cells = {
        owners[cell.constituent_id]: cell
        for cell in manifest.metric_availability
        if cell.metric == "completed_views"
    }
    assert conditional_cells["display-buy"].status == display_status
    assert conditional_cells["video-buy"].status == video_status
    for cell in conditional_cells.values():
        if cell.status == "unsupported":
            assert cell.reason == "not_video_inventory"
            assert cell.data_through is None
    totals = {total.name: total.value for total in manifest.control_totals}
    assert totals == {
        "impressions": "20",
        "clicks": "0",
        "spend": "2.50",
        **({} if completed_views_total is None else {"completed_views": completed_views_total}),
    }
    (revision,) = await service.store.list_revisions(
        account_id="eur", reporting_obligation_id=request.identity.reporting_obligation_id
    )
    assert revision.source_manifest_sha256 == manifest.content_fingerprint.removeprefix("sha256:")
    assert dict(revision.control_totals) == totals
    page = await service.store.read_revision_rows(
        account_id="eur", reporting_revision_id=revision.reporting_revision_id, limit=10
    )
    assert page.total_count == 2 and not page.has_more
    display_row = next(row for row in page.rows if row["media_buy_id"] == "display-buy")
    assert ("completed_views" in display_row) == (display_status != "unsupported")


@pytest.mark.parametrize("display_status", ["missing", "delayed"])
async def test_full_core_publication_still_refuses_missing_or_delayed_applicable_cells(
    conditional_service: ReliableReportingService, display_status: str
) -> None:
    service = conditional_service
    adapter = _ConditionalAdapter(display_status)
    registration = service.sources.register("conditional", adapter)
    config = replace(configuration("eur"), media_buy_ids=("display-buy", "video-buy"))
    await service.configure(config)
    turn = await service.run_worker(now=NOW)

    assert not turn.configuration_errors
    worker = turn.configurations[config.generation_key]
    assert len(worker.slices_failed) == 1 and not worker.revisions_committed
    (request,) = adapter.calls
    error = validate_reporting_source_failure(
        await registration.executor.execute(request, cancel=asyncio.Event()), "PARTIAL_RESULT"
    )
    assert error.retry == "retryable"
    assert (
        await service.store.list_revisions(
            account_id="eur", reporting_obligation_id=request.identity.reporting_obligation_id
        )
        == ()
    )


@pytest.mark.parametrize("custom_executor", [False, True], ids=["inline", "custom-executor"])
async def test_exact_support_cannot_withdraw_a_cell_as_unsupported(
    conditional_service: ReliableReportingService,
    custom_executor: bool,
) -> None:
    service = conditional_service
    if custom_executor:
        adapter = _ConditionalAdapter("unsupported")
        inner = service.sources.register("partial", adapter)

        class ExactExecutor:
            capabilities = _conditional_capabilities(exact_metrics=METRICS)

            async def execute(self, request, *, cancel, heartbeat=None):
                return await inner.executor.execute(request, cancel=cancel, heartbeat=heartbeat)

        registration = service.sources.register_executor(
            "conditional", ExactExecutor(), object_reader=inner.object_reader
        )
    else:
        adapter = _ConditionalAdapter("unsupported", exact_metrics=METRICS)
        registration = service.sources.register("conditional", adapter)
    config = replace(configuration("eur"), media_buy_ids=("display-buy", "video-buy"))
    await service.configure(config)
    turn = await service.run_worker(now=NOW)

    assert not turn.configurations
    assert set(turn.configuration_errors) == {config.generation_key}
    error = turn.configuration_errors[config.generation_key]
    assert isinstance(error, LedgerConflictError if custom_executor else ValueError)
    assert "unsupported cells require partial metric support with a reason" in str(error)
    (request,) = adapter.calls
    assert (
        await service.store.list_revisions(
            account_id="eur", reporting_obligation_id=request.identity.reporting_obligation_id
        )
        == ()
    )
    if custom_executor:
        assert isinstance(error, LedgerConflictError) and error.code == "MANIFEST_MISMATCH"
        assert registration.object_reader is not None
        result = await registration.executor.execute(request, cancel=asyncio.Event())
        with pytest.raises(
            ReportingSourceConformanceError, match="partial metric support"
        ) as caught:
            await validate_reporting_source_execution(
                capabilities=registration.executor.capabilities,
                request=request,
                result=result,
                object_reader=registration.object_reader,
                clock=lambda: NOW,
            )
        assert caught.value.code == "MANIFEST_MISMATCH"


@pytest.mark.parametrize(
    ("media_buy_ids", "unsupported_buys", "expected_coverage"),
    [
        pytest.param(("display-buy",), ("display-buy",), "none", id="one-unsupported"),
        pytest.param(
            ("display-buy", "video-buy"),
            ("display-buy",),
            "partial",
            id="unsupported-and-available",
        ),
        pytest.param(
            ("display-buy", "video-buy"),
            ("display-buy", "video-buy"),
            "none",
            id="all-constituents-all-metrics-unsupported",
        ),
    ],
)
async def test_full_core_publication_does_not_turn_zero_applicable_metrics_into_coverage(
    conditional_service: ReliableReportingService,
    media_buy_ids: tuple[str, ...],
    unsupported_buys: tuple[str, ...],
    expected_coverage: str,
) -> None:
    service = conditional_service
    adapter = _ConditionalAdapter(
        "unsupported", unsupported_buys=unsupported_buys, exact_metrics=()
    )
    registration = service.sources.register("conditional", adapter)
    config = replace(configuration("eur"), media_buy_ids=media_buy_ids)
    await service.configure(config)
    turn = await service.run_worker(now=NOW)

    assert not turn.configuration_errors
    worker = turn.configurations[config.generation_key]
    assert len(worker.slices_failed) == 1 and not worker.revisions_committed
    (request,) = adapter.calls
    error = validate_reporting_source_failure(
        await registration.executor.execute(request, cancel=asyncio.Event()), "PARTIAL_RESULT"
    )
    assert error.retry == "retryable"
    assert registration.object_reader is not None
    partial = request.model_copy(
        update={
            "coverage": request.coverage.model_copy(update={"expected": "partial"}),
            "identity": request.identity.model_copy(
                update={"source_execution_key": request.identity.source_execution_key + "-partial"}
            ),
        }
    )
    manifest = await run_reporting_source_replay_conformance(
        executor=registration.executor,
        request=partial,
        object_reader=registration.object_reader,
        clock=lambda: NOW,
    )
    assert manifest.coverage.status == expected_coverage
    for item in manifest.coverage.constituents:
        if item.constituent.media_buy_id in unsupported_buys:
            assert item.status == "unsupported"
            assert item.data_through is None and item.reason == "not_applicable"
            assert {
                cell.status
                for cell in manifest.metric_availability
                if cell.constituent_id == item.constituent_id
            } == {"unsupported"}
    assert not manifest.explicit_zero
    assert bool(manifest.control_totals) is (expected_coverage == "partial")
    if expected_coverage == "none":
        assert manifest.row_count == 0
        assert {cell.status for cell in manifest.metric_availability} == {"unsupported"}
    with pytest.raises(ReportingSourceConformanceError, match="full-coverage request"):
        await validate_reporting_source_execution(
            capabilities=adapter.capabilities,
            request=partial.model_copy(update={"coverage": request.coverage}),
            result=await registration.executor.execute(partial, cancel=asyncio.Event()),
            object_reader=registration.object_reader,
            clock=lambda: NOW,
        )
    assert (
        await service.store.list_revisions(
            account_id="eur", reporting_obligation_id=request.identity.reporting_obligation_id
        )
        == ()
    )


@pytest.mark.parametrize("diagnostic_coverage", ["none", "partial"])
async def test_custom_executor_cannot_complete_a_full_request_with_diagnostic_coverage(
    conditional_service: ReliableReportingService,
    diagnostic_coverage: str,
) -> None:
    service = conditional_service
    adapter = _ConditionalAdapter(
        "unsupported",
        unsupported_buys=(
            ("display-buy", "video-buy") if diagnostic_coverage == "none" else ("display-buy",)
        ),
        exact_metrics=(),
    )
    inner = service.sources.register("partial", adapter)

    class DiagnosticExecutor:
        capabilities = adapter.capabilities

        async def execute(self, request, *, cancel, heartbeat=None):
            assert request.coverage.expected == "full"
            diagnostic_request = request.model_copy(
                update={"coverage": request.coverage.model_copy(update={"expected": "partial"})}
            )
            return await inner.executor.execute(
                diagnostic_request, cancel=cancel, heartbeat=heartbeat
            )

    registration = service.sources.register_executor(
        "conditional", DiagnosticExecutor(), object_reader=inner.object_reader
    )
    config = replace(configuration("eur"), media_buy_ids=("display-buy", "video-buy"))
    await service.configure(config)
    turn = await service.run_worker(now=NOW)

    assert not turn.configurations
    assert set(turn.configuration_errors) == {config.generation_key}
    error = turn.configuration_errors[config.generation_key]
    assert isinstance(error, LedgerConflictError) and error.code == "MANIFEST_MISMATCH"
    (diagnostic_request,) = adapter.calls
    assert (
        await service.store.list_revisions(
            account_id="eur",
            reporting_obligation_id=diagnostic_request.identity.reporting_obligation_id,
        )
        == ()
    )
    request = diagnostic_request.model_copy(
        update={"coverage": diagnostic_request.coverage.model_copy(update={"expected": "full"})}
    )
    assert registration.object_reader is not None
    result = await registration.executor.execute(request, cancel=asyncio.Event())
    assert result.ok and result.response is not None and result.manifest_bytes is not None
    manifest = parse_verified_source_batch_manifest_v1(
        result.response.manifest, result.manifest_bytes
    )
    assert manifest.coverage.status == diagnostic_coverage
    with pytest.raises(ReportingSourceConformanceError, match="full-coverage request"):
        await validate_reporting_source_execution(
            capabilities=registration.executor.capabilities,
            request=request,
            result=result,
            object_reader=registration.object_reader,
            clock=lambda: NOW,
        )
