"""Construction-time contracts for the inline adapter's typed evidence surface."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any

import pytest

from adcp.reporting.conformance import validate_reporting_source_failure
from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import (
    InlineFetchResult,
    InlineReportingSource,
    MetricEvidence,
    metric_delayed_through,
    metric_unsupported_everywhere,
)

WATERMARK = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"status": "unavailable"}, "status is not a supported cell status"),
        ({"status": "partial", "reason": "partial"}, "status is not a supported cell status"),
        ({"status": "present"}, "present requires data_through"),
        (
            {"status": "present", "data_through": WATERMARK, "reason": "reason"},
            "present must not carry a reason",
        ),
        ({"status": "explicit_zero", "reason": "reason"}, "explicit_zero must not carry a reason"),
        ({"status": "missing"}, "missing requires a stable reason"),
        ({"status": "delayed", "data_through": WATERMARK}, "delayed requires a stable reason"),
        ({"status": "unsupported"}, "unsupported requires a stable reason"),
        (
            {"status": "missing", "reason": "reason", "data_through": WATERMARK},
            "missing must not carry data_through",
        ),
        (
            {"status": "unsupported", "reason": "reason", "data_through": WATERMARK},
            "unsupported must not carry data_through",
        ),
        (
            {"status": "present", "data_through": WATERMARK.replace(tzinfo=None)},
            "data_through must be a timezone-aware datetime",
        ),
        (
            {"status": "explicit_zero", "data_through": "2026-11-01"},
            "data_through must be a timezone-aware datetime",
        ),
    ],
)
def test_invalid_evidence_is_rejected_at_construction(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MetricEvidence(**fields)


@pytest.mark.parametrize(
    "reason", ["", " ", " leading", "trailing ", "not\nsafe", "non_ascii_é", "x" * 513, 42]
)
def test_reasons_use_the_existing_wire_evidence_contract(reason: Any) -> None:
    with pytest.raises(ValueError, match="reason must be a wire-valid stable reason"):
        MetricEvidence.unavailable(reason)


def test_reason_at_the_wire_limit_is_accepted() -> None:
    assert MetricEvidence.missing("x" * 512).reason == "x" * 512


@pytest.mark.parametrize(
    "evidence",
    [
        MetricEvidence.present(WATERMARK),
        MetricEvidence.explicit_zero(),
        MetricEvidence.explicit_zero(data_through=WATERMARK),
        MetricEvidence.missing("not_returned"),
        MetricEvidence.delayed("processing"),
        MetricEvidence.delayed("processing", data_through=WATERMARK),
        MetricEvidence.unavailable("not_video_inventory"),
    ],
)
def test_evidence_is_immutable(evidence: MetricEvidence) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(evidence, "reason", "changed")


@pytest.mark.parametrize(
    "field", ["semantic_contract_id", "semantic_contract_version", "semantic_contract_sha256"]
)
def test_adapters_cannot_supply_semantic_contract_fields(field: str) -> None:
    fields = {"status": "present", "data_through": WATERMARK, field: "adapter_override"}
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        MetricEvidence(**fields)


@pytest.mark.parametrize("pattern", ["unsupported", "delayed"])
def test_bulk_helpers_reject_unrequested_metrics(pattern: str) -> None:
    request = redacted_snapshot_request()
    with pytest.raises(ValueError, match="unknown requested metric 'completed_views'"):
        if pattern == "unsupported":
            metric_unsupported_everywhere(request, "completed_views", "not_video_inventory")
        else:
            metric_delayed_through(request, "completed_views", WATERMARK)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_constructor_errors_inside_fetch_remain_actionable(asynchronous: bool) -> None:
    def invalid(req: Any) -> InlineFetchResult:
        return InlineFetchResult(
            rows=[],
            cell_availability={"campaign-redacted-1": {"impressions": MetricEvidence("present")}},
        )

    async def invalid_async(req: Any) -> InlineFetchResult:
        return invalid(req)

    source = InlineReportingSource(
        capabilities=redacted_capabilities(), fetch=invalid_async if asynchronous else invalid
    )
    with pytest.raises(ValueError, match="MetricEvidence present requires data_through"):
        await source.execute(redacted_snapshot_request(), cancel=asyncio.Event())


async def test_ordinary_provider_value_errors_keep_their_legacy_classification() -> None:
    def fetch(req: Any) -> None:
        raise ValueError("provider diagnostic detail")

    source = InlineReportingSource(capabilities=redacted_capabilities(), fetch=fetch)
    error = validate_reporting_source_failure(
        await source.execute(redacted_snapshot_request(), cancel=asyncio.Event()),
        "PROVIDER_TRANSIENT",
    )
    assert error.retry == "retryable"
    assert error.safe_message == "the reporting source raised ValueError"
