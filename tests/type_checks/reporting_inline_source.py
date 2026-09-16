"""Adopter patterns: typed synchronous/asynchronous fetches and metric evidence.

These adapters assume an offering requesting impressions, clicks, viewability,
and completed_views. The SDK supplies each metric's semantic-contract identity.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta

from adcp.reporting.inline_source import (
    InlineFetch,
    InlineFetchResult,
    InlineReportingSource,
    MetricEvidence,
    metric_delayed_through,
    metric_unsupported_everywhere,
)
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    ReportingSourceExecutor,
    ReportingSourceSliceRequestV1,
)


def read_rows(request: ReportingSourceSliceRequestV1) -> Sequence[Mapping[str, object]]:
    return [
        {"constituent_id": item.constituent_id, "impressions": 120, "clicks": 0}
        for item in request.coverage.constituents
    ]


def fetch_sync(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
    through = min(request.period.end, request.period.source_read_cutoff_at)
    evidence: dict[str, dict[str, MetricEvidence]] = {
        item.constituent_id: {
            "impressions": MetricEvidence.present(through),
            "clicks": MetricEvidence.explicit_zero(data_through=through),
            "viewability": MetricEvidence.delayed(
                "measurement_pending", data_through=through - timedelta(hours=1)
            ),
            "completed_views": MetricEvidence.unavailable("not_video_inventory"),
        }
        for item in request.coverage.constituents
    }
    return InlineFetchResult(rows=read_rows(request), cell_availability=evidence)


def async_source(
    capabilities: ReportingSourceCapabilitiesV1,
    read: Callable[
        [ReportingSourceSliceRequestV1], Awaitable[Sequence[Mapping[str, object]] | None]
    ],
) -> ReportingSourceExecutor:
    async def fetch(request: ReportingSourceSliceRequestV1) -> InlineFetchResult | None:
        rows = await read(request)
        if rows is None:
            return None
        return InlineFetchResult(
            rows=rows,
            cell_availability={
                item.constituent_id: {
                    "viewability": MetricEvidence.missing("not_returned"),
                    "completed_views": MetricEvidence.unavailable("not_video_inventory"),
                }
                for item in request.coverage.constituents
            },
        )

    async_fetch: InlineFetch = fetch
    return InlineReportingSource(capabilities=capabilities, fetch=async_fetch)


def unsupported_video(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
    return InlineFetchResult(
        rows=[{**row, "viewability": "0.75"} for row in read_rows(request)],
        cell_availability=metric_unsupported_everywhere(
            request, "completed_views", "not_video_inventory"
        ),
    )


def delayed_viewability(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
    return InlineFetchResult(
        rows=[{**row, "completed_views": 30} for row in read_rows(request)],
        cell_availability=metric_delayed_through(
            request,
            "viewability",
            request.period.source_read_cutoff_at - timedelta(hours=1),
            reason="measurement_pending",
        ),
    )


def all_present(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
    return InlineFetchResult.all_present(
        read_complete_rows(request), data_through=request.period.source_read_cutoff_at
    )


def read_complete_rows(request: ReportingSourceSliceRequestV1) -> Sequence[Mapping[str, object]]:
    return [{**row, "viewability": "0.75", "completed_views": 30} for row in read_rows(request)]


def configure_sources(
    capabilities: ReportingSourceCapabilitiesV1,
) -> tuple[ReportingSourceExecutor, ...]:
    sync_fetch: InlineFetch = fetch_sync
    # Bare row callbacks and richer results remain accepted.
    return tuple(
        InlineReportingSource(capabilities=capabilities, fetch=fetch)
        for fetch in (
            sync_fetch,
            read_complete_rows,
            unsupported_video,
            delayed_viewability,
            all_present,
        )
    )
