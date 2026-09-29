"""Conditional metrics have the same meaning in Core and source conformance."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from typing import Any

import pytest

from adcp.reporting.conformance import (
    run_reporting_source_replay_conformance,
    validate_reporting_source_failure,
)
from adcp.reporting.fixtures import SNAPSHOT_OFFERING_ID
from adcp.reporting.inline_source import InlineFetchResult, MetricEvidence
from adcp.reporting.ledger import ReportingConfiguration
from adcp.reporting.service import ReliableReportingService, ReportingAccountContext
from adcp.reporting.source import (
    MetricOfferingV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceSliceRequestV1,
    reporting_source_capabilities_sha256_v1,
)
from adcp.reporting.testing import DeterministicReportingClock
from tests.test_reliable_reporting_service import _capability_offering

from ._generation_support import NOW, isolated_reporting_pool
from ._reliable_support import capabilities, configuration

METRICS = ("impressions", "clicks", "spend", "completed_views")


def _conditional_capabilities() -> ReportingSourceCapabilitiesV1:
    base = capabilities()
    draft = base.model_copy(
        update={
            "offerings": [
                offering.model_copy(
                    update={
                        "metrics": [
                            MetricOfferingV1(
                                name=name,
                                support="exact" if name in {"impressions", "spend"} else "partial",
                                reason=(
                                    None
                                    if name in {"impressions", "spend"}
                                    else "inventory_dependent"
                                ),
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
    def __init__(self, display_status: str, *, all_unsupported: bool = False) -> None:
        self.capabilities = _conditional_capabilities()
        self.calls: list[ReportingSourceSliceRequestV1] = []
        self.display_status = display_status
        self.all_unsupported = all_unsupported

    def fetch_slice(self, request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        self.calls.append(request)
        rows: list[dict[str, Any]] = []
        evidence: dict[str, dict[str, MetricEvidence]] = {}
        for item in request.coverage.constituents:
            if self.all_unsupported and item.media_buy_id == "display-buy":
                evidence[item.constituent_id] = {
                    name: MetricEvidence.unavailable("not_applicable") for name in METRICS
                }
                continue
            status = self.display_status if item.media_buy_id == "display-buy" else "present"
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


@pytest.mark.parametrize("display_status", ["present", "explicit_zero", "unsupported"])
async def test_full_core_publication_and_conformance_accept_conditional_metrics(
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
    assert conditional_cells["video-buy"].status == "present"
    if display_status == "unsupported":
        assert conditional_cells["display-buy"].reason == "not_video_inventory"
        assert conditional_cells["display-buy"].data_through is None
    totals = {total.name: total.value for total in manifest.control_totals}
    assert totals == {
        "impressions": "20",
        "clicks": "0",
        "spend": "2.50",
        **(
            {}
            if display_status == "unsupported"
            else {"completed_views": "5" if display_status == "explicit_zero" else "10"}
        ),
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


@pytest.mark.parametrize("second_available", [False, True])
async def test_full_core_publication_does_not_turn_zero_applicable_metrics_into_coverage(
    conditional_service: ReliableReportingService,
    second_available: bool,
) -> None:
    service = conditional_service
    adapter = _ConditionalAdapter("unsupported", all_unsupported=True)
    registration = service.sources.register("conditional", adapter)
    config = replace(
        configuration("eur"),
        media_buy_ids=("display-buy", "video-buy") if second_available else ("display-buy",),
    )
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
    assert manifest.coverage.status == ("partial" if second_available else "none")
    display = next(
        item
        for item in manifest.coverage.constituents
        if item.constituent.media_buy_id == "display-buy"
    )
    assert display.status == "unsupported"
    assert display.data_through is None and display.reason == "not_applicable"
    assert {
        cell.status
        for cell in manifest.metric_availability
        if cell.constituent_id == display.constituent_id
    } == {"unsupported"}
    assert not manifest.explicit_zero
    assert bool(manifest.control_totals) is second_available
    assert (
        await service.store.list_revisions(
            account_id="eur", reporting_obligation_id=request.identity.reporting_obligation_id
        )
        == ()
    )
