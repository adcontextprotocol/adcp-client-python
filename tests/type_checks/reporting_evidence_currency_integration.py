"""Strict adopter fixture: the two additive fields compose with the legacy six."""

from collections.abc import Mapping, Sequence
from datetime import datetime

from adcp.reporting.inline_source import InlineFetchResult, InlineReportingSource, MetricEvidence
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    ReportingSourceExecutor,
    ReportingSourceSliceRequestV1,
)


def fetch(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
    rows: Sequence[Mapping[str, object]] = [
        {"constituent_id": item.constituent_id, "impressions": 5, "clicks": 0}
        for item in request.coverage.constituents
    ]
    through: datetime = request.period.end
    evidence: Mapping[str, Mapping[str, MetricEvidence]] = {
        item.constituent_id: {
            "impressions": MetricEvidence.present(through),
            "clicks": MetricEvidence.explicit_zero(data_through=through),
            "spend": MetricEvidence.unavailable("billing_pending"),
        }
        for item in request.coverage.constituents
    }
    return InlineFetchResult(
        rows,
        through,
        None,
        {},
        "unsupported",
        (),
        currency=request.currency,
        cell_availability=evidence,
    )


def configure(capabilities: ReportingSourceCapabilitiesV1) -> ReportingSourceExecutor:
    async def fetch_async(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        return fetch(request)

    return InlineReportingSource(capabilities=capabilities, fetch=fetch_async)
