"""Adapter evidence must survive sealing, admission, and replay without overclaiming."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adcp.reporting.conformance import (
    run_reporting_source_replay_conformance,
    validate_reporting_source_execution,
    validate_reporting_source_failure,
)
from adcp.reporting.fixtures import (
    redacted_authoritative_request,
    redacted_capabilities,
    redacted_snapshot_request,
)
from adcp.reporting.inline_source import (
    InlineFetchResult,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
    MetricEvidence,
    metric_delayed_through,
    metric_unsupported_everywhere,
)
from adcp.reporting.source import (
    MediaBuyConstituentV1,
    MetricOfferingV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceCoverageRequestV1,
    ReportingSourceSliceRequestV1,
    SourceBatchManifestV1,
    coverage_denominator_fingerprint_v1,
    reporting_source_capabilities_sha256_v1,
)

CID = "campaign-redacted-1"  # Deliberately different from media_buy_id.
SECOND_CID = "campaign-redacted-2"
METRICS = ["impressions", "clicks", "viewability", "completed_views"]
OBSERVED_AT = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
ROW = {
    "media_buy_id": "media-buy-redacted",
    "impressions": 10,
    "clicks": 0,
    "viewability": "0.75",
    "completed_views": 2,
}


def _capabilities() -> ReportingSourceCapabilitiesV1:
    base = redacted_capabilities()
    offerings = [
        offering.model_copy(
            update={
                "metrics": [
                    MetricOfferingV1(
                        name=metric,
                        semantic_contract_id=f"fixture.{offering.offering_id}.{metric}",
                        semantic_contract_version=str(index + 1),
                        semantic_contract_sha256=str(index + 1) * 64,
                    )
                    for index, metric in enumerate([*METRICS, "spend"])
                ]
            }
        )
        for offering in base.offerings
    ]
    draft = base.model_copy(update={"offerings": offerings})
    return ReportingSourceCapabilitiesV1.model_validate(
        {
            **draft.model_dump(),
            "capabilities_sha256": reporting_source_capabilities_sha256_v1(draft),
        }
    )


def _request(
    *,
    authoritative: bool = False,
    second: bool = False,
    metrics: list[str] | None = None,
) -> ReportingSourceSliceRequestV1:
    base = redacted_authoritative_request() if authoritative else redacted_snapshot_request()
    constituents = list(base.coverage.constituents)
    if second:
        constituents.append(
            MediaBuyConstituentV1(
                constituent_id=SECOND_CID,
                media_buy_id="media-buy-second",
                product_id="fixture-product",
            )
        )
    return base.model_copy(
        update={
            "requested_metrics": metrics or METRICS,
            "coverage": ReportingSourceCoverageRequestV1(
                expected="partial",
                constituents=constituents,
                denominator_fingerprint=coverage_denominator_fingerprint_v1(constituents),
            ),
        }
    )


def _source(fetch: Any, **kwargs: Any) -> InlineReportingSource:
    return InlineReportingSource(
        capabilities=_capabilities(), fetch=fetch, clock=lambda: OBSERVED_AT, **kwargs
    )


async def _seal(
    answer: Any, request: ReportingSourceSliceRequestV1 | None = None
) -> SourceBatchManifestV1:
    request = request or _request()
    source = _source(lambda req: answer)
    return await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await source.execute(request, cancel=asyncio.Event()),
        object_reader=source.staging,
    )


@pytest.mark.parametrize("authoritative", [False, True], ids=["snapshot", "official"])
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_one_constituent_has_four_independent_metric_statuses(
    authoritative: bool, asynchronous: bool
) -> None:
    request = _request(authoritative=authoritative)
    watermark = min(request.period.end, request.period.source_read_cutoff_at)
    delayed_through = watermark - timedelta(hours=4)
    cases = [
        ("impressions", MetricEvidence.present(watermark), "present", watermark, None),
        ("clicks", MetricEvidence.explicit_zero(), "explicit_zero", watermark, None),
        (
            "viewability",
            MetricEvidence.delayed("measurement_pending", data_through=delayed_through),
            "delayed",
            delayed_through,
            "measurement_pending",
        ),
        (
            "completed_views",
            MetricEvidence.unavailable("not_video_inventory"),
            "unsupported",
            None,
            "not_video_inventory",
        ),
    ]
    rows = [{key: value for key, value in ROW.items() if key != "completed_views"}]
    answer = InlineFetchResult(
        rows=rows,
        cell_availability={CID: {metric: evidence for metric, evidence, *_ in cases}},
    )

    async def fetch(req: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        return answer

    source = _source(fetch if asynchronous else lambda req: answer)
    manifest = await run_reporting_source_replay_conformance(
        executor=source, request=request, object_reader=source.staging
    )
    assert manifest.coverage.status == "partial"
    assert manifest.coverage.constituents[0].status == "partial"
    assert manifest.coverage.constituents[0].reason
    assert manifest.explicit_zero is False
    assert manifest.row_count == 1
    assert {total.name: total.value for total in manifest.control_totals} == {
        "impressions": "10",
        "clicks": "0",
    }
    cells = {cell.metric: cell for cell in manifest.metric_availability}
    declared = {
        metric.name: metric for metric in source.capabilities.offering(request.offering_id).metrics
    }
    for metric, _, status, through, reason in cases:
        cell = cells[metric]
        assert (cell.status, cell.data_through, cell.reason) == (status, through, reason)
        contract = declared[metric]
        assert (
            cell.semantic_contract_id,
            cell.semantic_contract_version,
            cell.semantic_contract_sha256,
        ) == (
            contract.semantic_contract_id,
            contract.semantic_contract_version,
            contract.semantic_contract_sha256,
        )


@pytest.mark.parametrize(
    "fallback", ["present", "missing", "unsupported", "delayed", "stale", "partial"]
)
async def test_explicit_cells_override_constituent_defaults(fallback: str) -> None:
    request = _request()
    fields: dict[str, Any] = {}
    if fallback == "missing":
        fields["covered_constituent_ids"] = ()
    elif fallback != "present":
        fields.update(
            unavailable_constituents={CID: "constituent_default"}, unavailable_status=fallback
        )
    explicit = (
        MetricEvidence.unavailable("metric_override")
        if fallback == "present"
        else MetricEvidence.present(request.period.source_read_cutoff_at)
    )
    manifest = await _seal(
        InlineFetchResult(rows=[ROW], cell_availability={CID: {"impressions": explicit}}, **fields),
        request,
    )
    cells = {cell.metric: cell for cell in manifest.metric_availability}
    assert cells["impressions"].status == explicit.status
    assert cells["impressions"].reason == explicit.reason
    assert {cells[metric].status for metric in METRICS[1:]} == {fallback}
    assert manifest.coverage.constituents[0].status == "partial"


async def test_explicit_cells_can_replace_all_missing_constituent_defaults() -> None:
    request = _request()
    manifest = await _seal(
        InlineFetchResult(
            rows=[ROW],
            covered_constituent_ids=(),
            cell_availability={
                CID: {
                    metric: MetricEvidence.present(request.period.source_read_cutoff_at)
                    for metric in METRICS
                }
            },
        ),
        request,
    )
    assert manifest.coverage.status == "full"
    assert not manifest.warnings
    assert len(manifest.control_totals) == len(METRICS)


async def test_present_and_explicit_zero_are_full_coverage() -> None:
    manifest = await _seal(
        InlineFetchResult(
            rows=[ROW], cell_availability={CID: {"clicks": MetricEvidence.explicit_zero()}}
        )
    )
    assert manifest.coverage.status == "full"
    assert manifest.coverage.constituents[0].status == "present"
    assert manifest.explicit_zero is False


@pytest.mark.parametrize("status", ["missing", "delayed", "unsupported"])
async def test_uniform_unavailability_does_not_become_partial_or_zero(status: str) -> None:
    evidence = MetricEvidence(status=status, reason="source_reason")
    manifest = await _seal(
        InlineFetchResult(
            rows=[], cell_availability={CID: {metric: evidence for metric in METRICS}}
        )
    )
    assert manifest.coverage.status == "none"
    assert manifest.coverage.constituents[0].status == status
    assert manifest.coverage.constituents[0].reason == "source_reason"
    assert not manifest.explicit_zero
    assert not manifest.control_totals


async def test_zero_row_mixed_unavailability_has_no_manufactured_measurements() -> None:
    manifest = await _seal(
        InlineFetchResult(
            rows=[],
            covered_constituent_ids=(),
            cell_availability={
                CID: {"completed_views": MetricEvidence.unavailable("not_video_inventory")}
            },
        )
    )
    assert manifest.coverage.status == "partial"
    assert {cell.status for cell in manifest.metric_availability} == {"missing", "unsupported"}
    assert manifest.row_count == 0
    assert not manifest.explicit_zero
    # The declared exception withdraws its column. The derived cells keep the
    # zero-row totals a legacy unavailable batch has always published, which
    # the wire rule constrains to zero rather than forbidding.
    assert {total.name: total.value for total in manifest.control_totals} == {
        "impressions": "0",
        "clicks": "0",
        "viewability": "0",
    }


async def test_full_request_with_mixed_cells_fails_without_staging() -> None:
    request = _request()
    request = request.model_copy(
        update={"coverage": request.coverage.model_copy(update={"expected": "full"})}
    )
    source = _source(
        lambda req: InlineFetchResult(
            rows=[ROW],
            cell_availability=metric_unsupported_everywhere(
                req, "completed_views", "not_video_inventory"
            ),
        ),
        staging=_NoStaging(),
    )
    error = validate_reporting_source_failure(
        await source.execute(request, cancel=asyncio.Event()), "PARTIAL_RESULT"
    )
    assert error.retry == "retryable"


class _NoStaging(InMemoryStagingStore):
    async def stage(self, **kwargs: Any) -> tuple[str, str]:
        pytest.fail("invalid evidence must be rejected before staging")


class _RepeatedMapping(Mapping[str, Any]):
    """A mapping that exposes repeated entries before a dict would discard them."""

    def __init__(self, key: str, value: Any) -> None:
        self.key, self.value = key, value

    def __getitem__(self, key: str) -> Any:
        return self.value

    def __iter__(self) -> Iterator[str]:
        return iter([self.key, self.key])

    def __len__(self) -> int:
        return 2


@pytest.mark.parametrize(
    ("availability", "error"),
    [
        ({"media-buy-redacted": {}}, "unknown constituent_id 'media-buy-redacted'"),
        ({"other": {}}, "unknown constituent_id 'other'"),
        ({CID: {"spend": MetricEvidence.explicit_zero()}}, "unknown requested metric 'spend'"),
        ({CID: {"other": MetricEvidence.explicit_zero()}}, "unknown requested metric 'other'"),
        ({(CID, "clicks"): MetricEvidence.explicit_zero()}, "unknown constituent_id"),
        ({CID: {42: MetricEvidence.explicit_zero()}}, "unknown requested metric 42"),
        ([], "must be a nested mapping"),
        ({CID: []}, "must be a metric mapping"),
        (
            {
                CID: {
                    "clicks": {
                        "status": "explicit_zero",
                        "semantic_contract_id": "adapter_override",
                    }
                }
            },
            "must be MetricEvidence",
        ),
        ({CID: {"clicks": None}}, "must be MetricEvidence"),
        ({CID: _RepeatedMapping("clicks", MetricEvidence.explicit_zero())}, "duplicate cell"),
        (
            _RepeatedMapping(CID, {"clicks": MetricEvidence.explicit_zero()}),
            "duplicate constituent_id",
        ),
    ],
)
async def test_invalid_matrices_fail_clearly_before_staging_or_sealing(
    availability: Any, error: str
) -> None:
    request = _request()
    seals = InMemorySealStore()
    source = _source(
        lambda req: InlineFetchResult(rows=[ROW], cell_availability=availability),
        staging=_NoStaging(),
        seals=seals,
    )
    with pytest.raises(ValueError, match=error):
        await source.execute(request, cancel=asyncio.Event())
    assert (
        await seals.get(
            account_id=request.identity.account_id,
            source_execution_key=request.identity.source_execution_key,
        )
        is None
    )


@pytest.mark.parametrize("value", [1, -1, "0.1", True, False, "not_numeric", "NaN", "Infinity"])
async def test_explicit_zero_cannot_hide_a_contradictory_row_value(value: Any) -> None:
    source = _source(
        lambda req: InlineFetchResult(
            rows=[{**ROW, "clicks": value}],
            cell_availability={CID: {"clicks": MetricEvidence.explicit_zero()}},
        ),
        staging=_NoStaging(),
    )
    with pytest.raises(ValueError, match="explicit_zero requires numeric zero row values"):
        await source.execute(_request(), cancel=asyncio.Event())


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"media_buy_id": "media-buy-redacted"}],
        [{**ROW, "impressions": None}],
        [ROW, {"media_buy_id": "media-buy-redacted"}],
    ],
)
async def test_explicit_present_requires_values_in_its_constituent_rows(rows: Any) -> None:
    source = _source(
        lambda req: InlineFetchResult(
            rows=rows,
            cell_availability={
                CID: {"impressions": MetricEvidence.present(req.period.source_read_cutoff_at)}
            },
        ),
        staging=_NoStaging(),
    )
    with pytest.raises(ValueError, match="present requires a value in every constituent row"):
        await source.execute(_request(), cancel=asyncio.Event())


async def test_zero_row_available_and_unavailable_mix_is_rejected_without_a_wire_change() -> None:
    source = _source(
        lambda req: InlineFetchResult(
            rows=[],
            cell_availability={
                CID: {"completed_views": MetricEvidence.unavailable("not_video_inventory")}
            },
        ),
        staging=_NoStaging(),
    )
    with pytest.raises(
        ValueError, match="a zero-row batch cannot mix available and unavailable cells"
    ):
        await source.execute(_request(), cancel=asyncio.Event())


async def test_the_zero_row_mixing_error_does_not_blame_an_unused_field() -> None:
    """A derived result reaches the same guard without ever naming a cell."""
    source = _source(
        lambda req: InlineFetchResult(rows=[], covered_constituent_ids=[CID]),
        staging=_NoStaging(),
    )
    with pytest.raises(ValueError, match="a zero-row batch cannot mix") as caught:
        await source.execute(_request(second=True), cancel=asyncio.Event())
    assert "cell_availability" not in str(caught.value)


@pytest.mark.parametrize("status", ["present", "explicit_zero"])
async def test_cell_watermarks_do_not_bypass_the_authoritative_freshness_gate(status: str) -> None:
    request = _request(authoritative=True)
    through = request.period.end - timedelta(hours=4)
    manifest = await _seal(
        InlineFetchResult(
            rows=[ROW],
            cell_availability={
                CID: {"clicks": MetricEvidence(status=status, data_through=through)}
            },
        ),
        request,
    )
    cell = next(cell for cell in manifest.metric_availability if cell.metric == "clicks")
    assert (cell.status, cell.data_through) == ("delayed", through)
    assert cell.reason
    assert manifest.coverage.status == "partial"
    assert "clicks" not in {total.name for total in manifest.control_totals}


@pytest.mark.parametrize("authoritative", [False, True])
async def test_newer_explicit_watermark_does_not_advance_fallback_cells(
    authoritative: bool,
) -> None:
    request = _request(authoritative=authoritative)
    through = min(request.period.end, request.period.source_read_cutoff_at)
    fallback_through = through - timedelta(hours=4)
    manifest = await _seal(
        InlineFetchResult(
            rows=[ROW],
            data_through=fallback_through,
            cell_availability={CID: {"impressions": MetricEvidence.present(through)}},
        ),
        request,
    )
    cells = {cell.metric: cell for cell in manifest.metric_availability}
    assert manifest.data_through == through
    assert cells["impressions"].data_through == through
    assert cells["impressions"].status == "present"
    assert cells["clicks"].status == ("delayed" if authoritative else "present")
    assert cells["clicks"].data_through == (None if authoritative else fallback_through)
    assert manifest.coverage.constituents[0].data_through == (
        None if authoritative else fallback_through
    )


async def test_explicit_watermarks_are_bounded_by_the_frozen_cutoff_and_observation() -> None:
    request = _request()
    observed_at = request.period.source_read_cutoff_at - timedelta(hours=1)
    source = InlineReportingSource(
        capabilities=_capabilities(),
        fetch=lambda req: InlineFetchResult(
            rows=[ROW],
            cell_availability={
                CID: {"impressions": MetricEvidence.present(req.period.end + timedelta(days=1))}
            },
        ),
        clock=lambda: observed_at,
    )
    manifest = await run_reporting_source_replay_conformance(
        executor=source, request=request, object_reader=source.staging
    )
    assert manifest.data_through == observed_at
    assert manifest.metric_availability[0].data_through == observed_at


async def test_explicit_watermarks_before_the_period_are_not_promoted_to_fresh_data() -> None:
    source = _source(
        lambda req: InlineFetchResult(
            rows=[ROW],
            cell_availability={
                CID: {
                    "impressions": MetricEvidence.present(req.period.start - timedelta(seconds=1))
                }
            },
        ),
        staging=_NoStaging(),
    )
    with pytest.raises(ValueError, match="data_through must not precede the reporting period"):
        await source.execute(_request(), cancel=asyncio.Event())


@pytest.mark.parametrize("status", ["missing", "delayed", "unsupported"])
async def test_control_total_needs_every_requested_cell_even_when_rows_have_values(
    status: str,
) -> None:
    request = _request(second=True)
    manifest = await _seal(
        InlineFetchResult(
            rows=[ROW, {**ROW, "media_buy_id": "media-buy-second"}],
            cell_availability={
                SECOND_CID: {
                    "completed_views": MetricEvidence(status=status, reason="source_reason")
                }
            },
        ),
        request,
    )
    assert {total.name: total.value for total in manifest.control_totals} == {
        "impressions": "20",
        "clicks": "0",
        "viewability": "1.50",
    }


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "not_numeric"])
async def test_complete_cell_status_does_not_make_invalid_values_totalable(value: Any) -> None:
    manifest = await _seal(InlineFetchResult(rows=[{**ROW, "viewability": value}]))
    assert "viewability" not in {total.name for total in manifest.control_totals}


async def test_omitted_unavailable_metrics_do_not_suppress_other_metric_totals() -> None:
    request = _request(second=True)
    manifest = await _seal(
        InlineFetchResult(
            rows=[
                ROW,
                {
                    **{key: value for key, value in ROW.items() if key != "completed_views"},
                    "media_buy_id": "media-buy-second",
                },
            ],
            cell_availability={
                SECOND_CID: {"completed_views": MetricEvidence.missing("not_returned")}
            },
        ),
        request,
    )
    assert {total.name for total in manifest.control_totals} == set(METRICS) - {"completed_views"}


async def test_unmatched_rows_are_warned_about_and_stay_in_the_staged_checksum() -> None:
    manifest = await _seal([ROW, {**ROW, "media_buy_id": "not_requested"}])
    assert manifest.row_count == 2
    assert manifest.warnings
    # A control total is recomputed from the revision's rows, so it must cover
    # every staged row -- including one no constituent claimed.
    assert {total.name: total.value for total in manifest.control_totals} == {
        "impressions": "20",
        "clicks": "0",
        "viewability": "1.50",
        "completed_views": "4",
    }


async def test_a_derived_missing_constituent_does_not_withdraw_its_neighbour_totals() -> None:
    request = _request(second=True)
    manifest = await _seal(InlineFetchResult(rows=[ROW], covered_constituent_ids=[CID]), request)
    assert manifest.coverage.status == "partial"
    assert manifest.coverage.constituents[1].status == "missing"
    assert {total.name: total.value for total in manifest.control_totals} == {
        "impressions": "10",
        "clicks": "0",
        "viewability": "0.75",
        "completed_views": "2",
    }


async def test_a_covered_zero_row_constituent_keeps_zeros_only_for_its_available_metrics() -> None:
    request = _request(second=True)
    manifest = await _seal(
        InlineFetchResult(
            rows=[ROW],
            cell_availability={
                SECOND_CID: {"completed_views": MetricEvidence.unavailable("not_video_inventory")}
            },
        ),
        request,
    )
    assert manifest.coverage.constituents[1].status == "partial"
    assert not manifest.explicit_zero
    for cell in manifest.metric_availability:
        if cell.constituent_id == SECOND_CID:
            assert cell.status == (
                "unsupported" if cell.metric == "completed_views" else "explicit_zero"
            )
    # SECOND_CID staged no rows, so its withdrawal reaches no sum: the checksum
    # over the rows that *were* measured is retained for every metric. Only a
    # withdrawal by a constituent that staged rows -- whose values the adapter
    # has disclaimed -- removes a total. See
    # test_a_withdrawal_only_removes_the_total_its_own_rows_could_corrupt.
    assert {total.name: total.value for total in manifest.control_totals} == {
        "impressions": "10",
        "clicks": "0",
        "viewability": "0.75",
        "completed_views": "2",
    }


async def test_a_withdrawal_only_removes_the_total_its_own_rows_could_corrupt() -> None:
    """The same withdrawal, from the constituent that actually staged the rows.

    Its rows carry ``completed_views``, and the adapter has said those values
    are not a measurement, so the total goes -- summing them would contradict
    the evidence. The distinction matters because a control total is the exact
    sum of the staged rows: a cell with no rows cannot put a disclaimed value
    in it, and withdrawing on its behalf would destroy a checksum the consumer
    (and, for money, the obligation ledger) reconciles against those rows.
    """
    request = _request(second=True)
    manifest = await _seal(
        InlineFetchResult(
            rows=[ROW, {**ROW, "media_buy_id": "media-buy-second"}],
            cell_availability={
                CID: {"completed_views": MetricEvidence.unavailable("not_video_inventory")}
            },
        ),
        request,
    )
    assert {total.name for total in manifest.control_totals} == set(METRICS) - {"completed_views"}


async def test_explicit_zero_evidence_for_every_cell_can_seal_a_real_empty_period() -> None:
    manifest = await _seal(
        InlineFetchResult(
            rows=[],
            cell_availability={CID: {metric: MetricEvidence.explicit_zero() for metric in METRICS}},
        )
    )
    assert manifest.explicit_zero
    assert manifest.coverage.status == "full"
    assert manifest.coverage.constituents[0].status == "explicit_zero"
    assert {total.name: total.value for total in manifest.control_totals} == dict.fromkeys(
        METRICS, "0"
    )


async def test_declared_zero_without_a_row_value_does_not_manufacture_a_total() -> None:
    manifest = await _seal(
        InlineFetchResult(
            rows=[{key: value for key, value in ROW.items() if key != "clicks"}],
            cell_availability={CID: {"clicks": MetricEvidence.explicit_zero()}},
        )
    )
    assert manifest.metric_availability[1].status == "explicit_zero"
    assert "clicks" not in {total.name for total in manifest.control_totals}


@pytest.mark.parametrize("pattern", ["unsupported", "delayed"])
async def test_bulk_helpers_cover_only_the_requested_metric(pattern: str) -> None:
    request = _request(second=True)
    through = request.period.source_read_cutoff_at - timedelta(hours=4)
    overrides = (
        metric_unsupported_everywhere(request, "completed_views", "not_video_inventory")
        if pattern == "unsupported"
        else metric_delayed_through(request, "completed_views", through)
    )
    manifest = await _seal(
        InlineFetchResult(
            rows=[ROW, {**ROW, "media_buy_id": "media-buy-second"}], cell_availability=overrides
        ),
        request,
    )
    for cell in manifest.metric_availability:
        if cell.metric == "completed_views":
            assert cell.status == pattern
            assert cell.reason
            assert cell.data_through == (through if pattern == "delayed" else None)
        else:
            assert cell.status == "present"


async def test_replay_uses_sealed_cell_evidence_after_the_adapter_changes() -> None:
    request = _request()
    overrides = metric_unsupported_everywhere(request, "completed_views", "not_video_inventory")
    rows = [dict(ROW)]
    calls = 0

    def fetch(req: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        nonlocal calls
        calls += 1
        return InlineFetchResult(rows=rows, cell_availability=overrides)

    source = _source(fetch)
    first = await source.execute(request, cancel=asyncio.Event())
    overrides[CID]["completed_views"] = MetricEvidence.present(request.period.source_read_cutoff_at)
    rows[0]["completed_views"] = 999
    cancel = asyncio.Event()
    cancel.set()
    replay = await source.execute(request, cancel=cancel)
    assert first.ok and replay.ok
    assert calls == 1
    assert replay.manifest_bytes == first.manifest_bytes
    assert replay.response == first.response
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=replay,
        object_reader=source.staging,
    )
    assert manifest.metric_availability[-1].status == "unsupported"


async def test_mapping_order_does_not_change_sealed_identity() -> None:
    request = _request(second=True)
    overrides = {
        CID: {
            "clicks": MetricEvidence.explicit_zero(),
            "completed_views": MetricEvidence.unavailable("not_video_inventory"),
        },
        SECOND_CID: {"clicks": MetricEvidence.explicit_zero()},
    }
    rows = [ROW, {**ROW, "media_buy_id": "media-buy-second"}]
    first = _source(lambda req: InlineFetchResult(rows=rows, cell_availability=overrides))
    reverse = {
        cid: dict(reversed(list(metrics.items())))
        for cid, metrics in reversed(list(overrides.items()))
    }
    second = _source(lambda req: InlineFetchResult(rows=rows, cell_availability=reverse))
    a = await first.execute(request, cancel=asyncio.Event())
    b = await second.execute(request, cancel=asyncio.Event())
    assert a.ok and b.ok
    assert a.manifest_bytes == b.manifest_bytes


async def test_status_and_reason_are_bound_by_the_content_fingerprint() -> None:
    fingerprints = set()
    for evidence in [
        MetricEvidence.unavailable("not_video_inventory"),
        MetricEvidence.unavailable("not_measured"),
        MetricEvidence.missing("not_measured"),
    ]:
        manifest = await _seal(
            InlineFetchResult(rows=[ROW], cell_availability={CID: {"completed_views": evidence}})
        )
        fingerprints.add(manifest.content_fingerprint)
    assert len(fingerprints) == 3


@pytest.mark.parametrize("rows", [[ROW], []])
async def test_legacy_rows_results_and_empty_overrides_seal_identically(rows: Any) -> None:
    request = _request()
    results = []
    for answer in [
        rows,
        InlineFetchResult(rows),
        InlineFetchResult(rows, cell_availability={}),
        InlineFetchResult(rows, cell_availability={CID: {}}),
        InlineFetchResult.all_present(rows, data_through=request.period.source_read_cutoff_at),
    ]:
        source = _source(lambda req: answer)
        result = await source.execute(request, cancel=asyncio.Event())
        assert result.ok
        results.append(result.manifest_bytes)
    assert all(payload == results[0] for payload in results)


@pytest.mark.parametrize(
    ("answer", "second", "totals"),
    [
        pytest.param(
            [ROW, {**ROW, "media_buy_id": "not_requested"}],
            False,
            {"impressions": "20", "clicks": "0", "viewability": "1.50", "completed_views": "4"},
            id="unmatched_row",
        ),
        pytest.param(
            InlineFetchResult(rows=[ROW], covered_constituent_ids=[CID]),
            True,
            {"impressions": "10", "clicks": "0", "viewability": "0.75", "completed_views": "2"},
            id="uncovered_constituent",
        ),
        pytest.param(
            InlineFetchResult(rows=[ROW], unavailable_constituents={SECOND_CID: "no_data"}),
            True,
            {"impressions": "10", "clicks": "0", "viewability": "0.75", "completed_views": "2"},
            id="unavailable_constituent",
        ),
        pytest.param(
            InlineFetchResult(
                rows=[ROW, {**ROW, "media_buy_id": "media-buy-second"}],
                unavailable_constituents={SECOND_CID: "late"},
                unavailable_status="delayed",
            ),
            True,
            {"impressions": "20", "clicks": "0", "viewability": "1.50", "completed_views": "4"},
            id="delayed_constituent",
        ),
        pytest.param(
            None,
            False,
            {"impressions": "0", "clicks": "0", "viewability": "0", "completed_views": "0"},
            id="not_ready",
        ),
    ],
)
async def test_results_without_declared_cells_keep_their_legacy_control_totals(
    answer: Any, second: bool, totals: dict[str, str]
) -> None:
    """A caller that never opted in must not lose evidence it already published.

    These are the shapes where the derived constituent status is not uniformly
    available. Withdrawing their totals would change sealed bytes, the content
    fingerprint, and the ledger revision content hash for adopters who supplied
    no ``cell_availability`` at all.
    """
    manifest = await _seal(answer, _request(second=second))
    assert {total.name: total.value for total in manifest.control_totals} == totals


async def test_five_independent_statuses_seal_in_one_constituent_matrix() -> None:
    metrics = [*METRICS, "spend"]
    request = _request(metrics=metrics)
    watermark = min(request.period.end, request.period.source_read_cutoff_at)
    delayed_through = watermark - timedelta(hours=4)
    expected = {
        "impressions": ("present", watermark, None),
        "clicks": ("explicit_zero", watermark, None),
        "viewability": ("delayed", delayed_through, "measurement_pending"),
        "completed_views": ("unsupported", None, "not_video_inventory"),
        "spend": ("missing", None, "not_returned"),
    }
    manifest = await _seal(
        InlineFetchResult(
            rows=[{"media_buy_id": "media-buy-redacted", "impressions": 10, "clicks": 0}],
            cell_availability={
                CID: {
                    "impressions": MetricEvidence.present(watermark),
                    "clicks": MetricEvidence.explicit_zero(),
                    "viewability": MetricEvidence.delayed(
                        "measurement_pending", data_through=delayed_through
                    ),
                    "completed_views": MetricEvidence.unavailable("not_video_inventory"),
                    "spend": MetricEvidence.missing("not_returned"),
                }
            },
        ),
        request,
    )
    assert manifest.coverage.constituents[0].status == "partial"
    assert {
        cell.metric: (cell.status, cell.data_through, cell.reason)
        for cell in manifest.metric_availability
    } == expected
    assert {total.name for total in manifest.control_totals} == {"impressions", "clicks"}


async def test_existing_positional_result_arguments_keep_their_meaning() -> None:
    request = _request()
    result = InlineFetchResult(
        [ROW], request.period.source_read_cutoff_at, None, {}, "unsupported", ("legacy_note",)
    )
    assert result.cell_availability is None
    manifest = await _seal(result, request)
    assert manifest.coverage.status == "full"
    assert manifest.warnings == ["legacy_note"]
