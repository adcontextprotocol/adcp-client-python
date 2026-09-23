"""The inline source adapter: an ordinary delivery fetch, made conforming.

The rules being asserted are the ones a seller gets wrong when wrapping an
existing reporting read: conflating "not ready" with "zero", publishing a
partial answer against a full-coverage slice, claiming an official close the
freshness watermark does not support, and returning different bytes on replay.
"""

from __future__ import annotations

import asyncio
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adcp.reporting.conformance import (
    run_reporting_source_replay_conformance,
    validate_reporting_source_execution,
    validate_reporting_source_failure,
)
from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import (
    FileSystemStagingStore,
    InlineFetchResult,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.source import ReportingSourceError, ReportingSourceSliceRequestV1

ROW = {
    "media_buy_id": "media-buy-redacted",
    "campaign_id": "campaign-redacted-1",
    "impressions": 10,
    "spend": "1.25",
}

#: The fixture period sits in the future relative to the real clock, so every
#: test pins the observation instant past it.  A watermark can never follow the
#: moment it was observed at, and leaving that to the wall clock would make
#: these assertions quietly depend on the date the suite runs.
OBSERVED_AT = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _source(fetch: Any, **kwargs: Any) -> InlineReportingSource:
    kwargs.setdefault("clock", lambda: OBSERVED_AT)
    return InlineReportingSource(capabilities=redacted_capabilities(), fetch=fetch, **kwargs)


def _partial(request: ReportingSourceSliceRequestV1) -> ReportingSourceSliceRequestV1:
    return request.model_copy(
        update={"coverage": request.coverage.model_copy(update={"expected": "partial"})}
    )


async def _run(source: InlineReportingSource, request: ReportingSourceSliceRequestV1):
    return await source.execute(request, cancel=asyncio.Event())


# -- the three answers ------------------------------------------------------


async def test_rows_publish_a_present_full_coverage_manifest() -> None:
    request = redacted_snapshot_request()
    source = _source(lambda _request: [ROW])
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert manifest.coverage.status == "full"
    assert manifest.row_count == 1
    assert manifest.explicit_zero is False
    assert [item.status for item in manifest.coverage.constituents] == ["present"]


async def test_an_empty_row_list_is_an_observed_zero_not_an_absence() -> None:
    # The single most consequential distinction in the module: a quiet
    # campaign commits a revision and satisfies its obligation.
    request = redacted_snapshot_request()
    source = _source(lambda _request: [])
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert manifest.explicit_zero is True
    assert manifest.coverage.status == "full"
    assert manifest.row_count == 0
    assert [cell.status for cell in manifest.metric_availability] == ["explicit_zero"] * 2


async def test_none_is_not_ready_and_fails_a_full_coverage_slice_retryably() -> None:
    request = redacted_snapshot_request()
    source = _source(lambda _request: None)
    error = validate_reporting_source_failure(await _run(source, request), "PARTIAL_RESULT")
    assert error.retry == "retryable"


async def test_none_publishes_delayed_evidence_for_a_partial_coverage_slice() -> None:
    # A partial slice *wants* the evidence: "late" and "dead" look the same to
    # a health projection that never sees a publication at all.
    request = _partial(redacted_snapshot_request())
    source = _source(lambda _request: None)
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert manifest.coverage.status == "none"
    assert manifest.explicit_zero is False
    assert [item.status for item in manifest.coverage.constituents] == ["delayed"]
    assert manifest.coverage.constituents[0].reason


async def test_a_typed_source_error_is_returned_verbatim() -> None:
    request = redacted_snapshot_request()

    def fetch(_request: ReportingSourceSliceRequestV1) -> None:
        raise ReportingSourceError("RATE_LIMITED", "upstream asked us to slow down")

    error = validate_reporting_source_failure(await _run(_source(fetch), request), "RATE_LIMITED")
    assert error.retry == "retryable"


async def test_an_unclassified_exception_is_retryable_and_redacted() -> None:
    request = redacted_snapshot_request()
    secret = "https://api.example.test/report?access_token=super-secret-value"

    def fetch(_request: ReportingSourceSliceRequestV1) -> None:
        raise RuntimeError(f"GET {secret} failed")

    error = validate_reporting_source_failure(
        await _run(_source(fetch), request), "PROVIDER_TRANSIENT"
    )
    assert error.retry == "retryable"
    assert "RuntimeError" in error.safe_message
    # A manifest is permanent, replicated evidence; a token in it is a leak.
    assert "super-secret-value" not in error.safe_message


async def test_exception_detail_is_available_when_explicitly_opted_into() -> None:
    request = redacted_snapshot_request()

    def fetch(_request: ReportingSourceSliceRequestV1) -> None:
        raise RuntimeError("quota exceeded for project 42")

    source = _source(fetch, include_exception_detail=True)
    error = validate_reporting_source_failure(await _run(source, request), "PROVIDER_TRANSIENT")
    assert "quota exceeded for project 42" in error.safe_message


# -- coverage nuance --------------------------------------------------------


async def test_an_uncovered_constituent_is_missing_not_zero() -> None:
    request = _partial(redacted_snapshot_request())
    source = _source(lambda _request: InlineFetchResult(rows=[], covered_constituent_ids=()))
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert [item.status for item in manifest.coverage.constituents] == ["missing"]
    assert manifest.explicit_zero is False


async def test_a_known_unsupported_scope_is_reported_as_unsupported() -> None:
    request = _partial(redacted_snapshot_request())
    source = _source(
        lambda _request: InlineFetchResult(
            rows=[],
            unavailable_constituents={
                "campaign-redacted-1": "Media buy lives on another ad server"
            },
        )
    )
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert [item.status for item in manifest.coverage.constituents] == ["unsupported"]
    assert manifest.coverage.constituents[0].reason == "Media buy lives on another ad server"


async def test_partial_coverage_cannot_complete_a_full_coverage_slice() -> None:
    request = redacted_snapshot_request()
    source = _source(lambda _request: InlineFetchResult(rows=[], covered_constituent_ids=()))
    error = validate_reporting_source_failure(await _run(source, request), "PARTIAL_RESULT")
    assert "cannot complete partially" in error.safe_message


async def test_rows_matching_no_constituent_are_retained_and_flagged() -> None:
    request = redacted_snapshot_request()
    stray = {**ROW, "media_buy_id": "a-buy-nobody-asked-about"}
    source = _source(lambda _request: [ROW, stray])
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert manifest.row_count == 2
    assert any("matched no requested constituent" in warning for warning in manifest.warnings)


# -- freshness --------------------------------------------------------------


async def test_an_official_close_short_of_the_window_is_delayed_not_present() -> None:
    # The ad-server freshness gate: real numbers that do not yet cover the
    # whole window are late, not complete.
    from adcp.reporting.fixtures import redacted_authoritative_request

    request = _partial(redacted_authoritative_request())
    source = _source(
        lambda req: InlineFetchResult(rows=[ROW], data_through=req.period.end - timedelta(hours=4))
    )
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert [item.status for item in manifest.coverage.constituents] == ["delayed"]
    assert manifest.data_through < request.period.end


async def test_an_official_close_covering_the_window_is_present() -> None:
    from adcp.reporting.fixtures import redacted_authoritative_request

    request = redacted_authoritative_request()
    source = _source(lambda req: InlineFetchResult(rows=[ROW], data_through=req.period.end))
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert manifest.coverage.status == "full"
    assert manifest.data_through == request.period.end


async def test_a_watermark_beyond_the_frozen_slice_is_clamped() -> None:
    request = redacted_snapshot_request()
    source = _source(
        lambda req: InlineFetchResult(rows=[ROW], data_through=req.period.end + timedelta(days=30))
    )
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert manifest.data_through <= request.period.source_read_cutoff_at


# -- control totals ---------------------------------------------------------


async def test_money_totals_are_exact_decimal_strings() -> None:
    request = redacted_snapshot_request()
    rows = [
        {**ROW, "impressions": 10, "spend": "1.25"},
        {**ROW, "impressions": 5, "spend": "0.10"},
    ]
    source = _source(lambda _request: rows)
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    totals = {total.name: (total.value, total.value_type) for total in manifest.control_totals}
    assert totals["impressions"] == ("15", "integer")
    # Not 1.3499999999999999: a float sum would round differently in another
    # language and break a consumer's equality check.
    assert totals["spend"] == ("1.35", "decimal")


async def test_a_metric_missing_from_some_rows_gets_no_total() -> None:
    request = redacted_snapshot_request()
    rows = [ROW, {k: v for k, v in ROW.items() if k != "spend"}]
    source = _source(lambda _request: rows)
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert [total.name for total in manifest.control_totals] == ["impressions"]


# -- sync and async fetches -------------------------------------------------


async def test_a_sync_fetch_does_not_block_the_event_loop() -> None:
    request = redacted_snapshot_request()
    main_thread = threading.current_thread().ident
    fetch_thread: list[int | None] = []
    ticks = 0

    def blocking(_request: ReportingSourceSliceRequestV1) -> list[dict[str, Any]]:
        fetch_thread.append(threading.current_thread().ident)
        time.sleep(0.1)
        return [ROW]

    async def heartbeat_loop() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    source = _source(blocking)
    ticker = asyncio.ensure_future(heartbeat_loop())
    try:
        result = await _run(source, request)
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)

    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=result,
        object_reader=source.staging,
    )
    assert manifest.row_count == 1
    assert fetch_thread == [fetch_thread[0]] and fetch_thread[0] != main_thread
    # The loop kept running while the fetch slept, so the sleep was in a thread.
    assert ticks > 5


async def test_a_plain_sync_callable_is_supported() -> None:
    request = redacted_snapshot_request()

    def fetch(_request: ReportingSourceSliceRequestV1) -> list[dict[str, Any]]:
        return [ROW]

    source = _source(fetch)
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert manifest.row_count == 1


async def test_an_async_callable_is_supported() -> None:
    request = redacted_snapshot_request()

    async def fetch(_request: ReportingSourceSliceRequestV1) -> list[dict[str, Any]]:
        await asyncio.sleep(0)
        return [ROW]

    source = _source(fetch)
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert manifest.row_count == 1


# -- replay -----------------------------------------------------------------


async def test_replay_returns_byte_identical_sealed_bytes() -> None:
    # Real observation times mean a recomputed manifest would differ; the seal
    # store is what makes replay identity possible at all.
    request = redacted_snapshot_request()
    calls = 0

    def fetch(_request: ReportingSourceSliceRequestV1) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        time.sleep(0.01)
        return [ROW]

    source = _source(fetch)
    manifest = await run_reporting_source_replay_conformance(
        executor=source, request=request, object_reader=source.staging
    )
    assert manifest.row_count == 1
    # The second execution served the seal instead of re-reading the source.
    assert calls == 1


async def test_a_concurrent_double_dispatch_resolves_to_one_publication() -> None:
    request = redacted_snapshot_request()
    release = asyncio.Event()

    async def fetch(_request: ReportingSourceSliceRequestV1) -> list[dict[str, Any]]:
        await release.wait()
        return [ROW]

    source = _source(fetch)
    first = asyncio.ensure_future(_run(source, request))
    second = asyncio.ensure_future(_run(source, request))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert results[0].manifest_bytes == results[1].manifest_bytes


async def test_cancellation_before_any_read_publishes_nothing() -> None:
    request = redacted_snapshot_request()
    cancel = asyncio.Event()
    cancel.set()
    source = _source(lambda _request: [ROW])
    error = validate_reporting_source_failure(
        await source.execute(request, cancel=cancel), "CANCELLED"
    )
    assert error.retry == "cancelled"


async def test_a_sealed_result_replays_even_after_cancellation() -> None:
    request = redacted_snapshot_request()
    source = _source(lambda _request: [ROW])
    sealed = await _run(source, request)
    cancel = asyncio.Event()
    cancel.set()
    replayed = await source.execute(request, cancel=cancel)
    assert replayed.ok
    assert replayed.manifest_bytes == sealed.manifest_bytes


# -- staging ----------------------------------------------------------------


async def test_filesystem_staging_is_immutable_and_scope_fenced(tmp_path) -> None:
    request = redacted_snapshot_request()
    staging = FileSystemStagingStore(tmp_path)
    source = _source(lambda _request: [ROW], staging=staging)
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=staging,
    )
    staged = manifest.objects[0]
    payload = await staging.read(
        object_ref=staged.object_ref,
        object_generation=staged.object_generation,
        account_id=request.identity.account_id,
        source_scope=request.identity.source_scope,
        cancel=asyncio.Event(),
    )
    assert len(payload) == staged.byte_count
    with pytest.raises(OSError):
        await staging.read(
            object_ref=staged.object_ref,
            object_generation=staged.object_generation,
            account_id="a-different-account",
            source_scope=request.identity.source_scope,
            cancel=asyncio.Event(),
        )


async def test_in_memory_staging_refuses_a_cross_account_read() -> None:
    request = redacted_snapshot_request()
    staging = InMemoryStagingStore()
    source = _source(lambda _request: [ROW], staging=staging)
    result = await _run(source, request)
    assert result.ok
    manifest_objects = (
        await validate_reporting_source_execution(
            capabilities=source.capabilities,
            request=request,
            result=result,
            object_reader=staging,
        )
    ).objects
    with pytest.raises(PermissionError):
        await staging.read(
            object_ref=manifest_objects[0].object_ref,
            object_generation=manifest_objects[0].object_generation,
            account_id="a-different-account",
            source_scope=request.identity.source_scope,
            cancel=asyncio.Event(),
        )


async def test_row_encoding_is_key_order_independent() -> None:
    request = redacted_snapshot_request()
    forward = _source(lambda _request: [dict(ROW)], seals=InMemorySealStore())
    reversed_keys = {key: ROW[key] for key in reversed(list(ROW))}
    backward = _source(lambda _request: [reversed_keys], seals=InMemorySealStore())
    first = await _run(forward, request)
    second = await _run(backward, request)
    assert first.ok and second.ok
    first_manifest = await validate_reporting_source_execution(
        capabilities=forward.capabilities,
        request=request,
        result=first,
        object_reader=forward.staging,
    )
    second_manifest = await validate_reporting_source_execution(
        capabilities=backward.capabilities,
        request=request,
        result=second,
        object_reader=backward.staging,
    )
    assert first_manifest.objects[0].sha256 == second_manifest.objects[0].sha256


# -- delivery-response projection -------------------------------------------


@pytest.mark.parametrize("currency", ["USD", "EUR"])
async def test_a_get_media_buy_delivery_response_projects_into_rows(currency: str) -> None:
    from adcp.types import GetMediaBuyDeliveryResponse

    request = redacted_snapshot_request(currency=currency)
    response = GetMediaBuyDeliveryResponse.model_validate(
        {
            "reporting_period": {
                "start": request.period.start.isoformat(),
                "end": request.period.source_read_cutoff_at.isoformat(),
            },
            "currency": currency,
            "media_buy_deliveries": [
                {
                    "media_buy_id": "media-buy-redacted",
                    "status": "active",
                    "totals": {"impressions": 10, "spend": 1.25},
                    "by_package": [
                        {
                            "package_id": "pkg-1",
                            "pricing_model": "cpm",
                            "rate": 1.0,
                            "currency": currency,
                            "impressions": 6,
                            "spend": 0.75,
                        },
                        {
                            "package_id": "pkg-2",
                            "pricing_model": "cpm",
                            "rate": 1.0,
                            "currency": currency,
                            "impressions": 4,
                            "spend": 0.5,
                        },
                    ],
                }
            ],
        }
    )
    source = _source(lambda _request: response)
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=await _run(source, request),
        object_reader=source.staging,
    )
    assert manifest.row_count == 2
    assert manifest.coverage.status == "full"
    assert manifest.currency == currency
    totals = {total.name: total.value for total in manifest.control_totals}
    assert totals["impressions"] == "10"
    assert totals["spend"] == "1.25"


async def test_the_deprecated_response_currency_is_checked_without_warning() -> None:
    """The legacy response-wide label still has to be read -- silently.

    ``GetMediaBuyDeliveryResponse.currency`` is deprecated in AdCP 3.2, so a
    naive attribute read emits a DeprecationWarning on every single fetch. An
    adopter running warnings as errors would see that surface as an opaque
    ``PROVIDER_TRANSIENT`` failure instead of a published slice.
    """
    from adcp.types import GetMediaBuyDeliveryResponse

    request = redacted_snapshot_request(currency="EUR")
    response = GetMediaBuyDeliveryResponse.model_validate(
        {
            "reporting_period": {
                "start": request.period.start.isoformat(),
                "end": request.period.source_read_cutoff_at.isoformat(),
            },
            "currency": "EUR",
            "media_buy_deliveries": [
                {
                    "media_buy_id": "media-buy-redacted",
                    "status": "active",
                    "totals": {"impressions": 10, "spend": 1.25},
                    "by_package": [],
                }
            ],
        }
    )
    source = _source(lambda _request: response)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        result = await _run(source, request)
    assert result.ok


async def test_an_unrecognized_return_value_is_a_typed_failure() -> None:
    request = redacted_snapshot_request()
    source = _source(lambda _request: 42)
    error = validate_reporting_source_failure(await _run(source, request), "INVALID_REQUEST")
    assert error.retry == "terminal"


# -- configuration guards ---------------------------------------------------


def test_an_evidenced_offering_is_refused_at_construction() -> None:
    # This adapter did not make the upstream calls, so it cannot honestly
    # produce per-call evidence for them.
    with pytest.raises(ValueError, match="basic manifests only"):
        InlineReportingSource(
            capabilities=redacted_capabilities(strictness="evidenced"),
            fetch=lambda _request: [],
        )
