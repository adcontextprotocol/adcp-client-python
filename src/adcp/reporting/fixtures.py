"""Redacted, deterministic fixtures for the reporting source contract.

These exist so you can write conformance tests for *your* executor without
first hand-rolling a valid capability declaration, a valid slice request, and a
manifest whose eight interlocking fingerprints all agree.  Everything here is
synthetic: the ``example.test`` contract URIs, the ``aaaa...`` digests, and the
account identifiers are placeholders, not redacted production data.

Typical use::

    from adcp.reporting.fixtures import redacted_snapshot_request
    from adcp.reporting.conformance import run_reporting_source_replay_conformance

    await run_reporting_source_replay_conformance(
        executor=MyExecutor(),
        request=redacted_snapshot_request(),
        object_reader=my_reader,
    )
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from adcp.reporting.source import (
    AuthoritativeOfferingV1,
    DimensionOfferingV1,
    MediaBuyConstituentV1,
    MetricOfferingV1,
    ProviderExecutionV1,
    ProvisionalSnapshotOfferingV1,
    ReportingAdapterBuildIdentityV1,
    ReportingConstituentCoverageV1,
    ReportingContractIdentityV1,
    ReportingFinalityEvidenceV1,
    ReportingManifestStrictness,
    ReportingMetricAvailabilityV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceCoverageRequestV1,
    ReportingSourceExecutorResult,
    ReportingSourceIdentityV1,
    ReportingSourcePeriodV1,
    ReportingSourceSliceRequestV1,
    ReportingSourceStagedObjectReader,
    ReportingSourceWindowingV1,
    SourceBatchCoverageV1,
    SourceBatchManifestV1,
    SourceBatchObjectV1,
    SourceControlTotalV1,
    SourceFormatV1,
    coverage_denominator_fingerprint_v1,
    deterministic_source_publication_id_v1,
    encode_source_batch_manifest_v1,
    publication_content_fingerprint_v1,
    reporting_source_capabilities_sha256_v1,
    source_batch_manifest_reference_v1,
    source_batch_object_set_sha256_v1,
)

__all__ = [
    "SNAPSHOT_OFFERING_ID",
    "OFFICIAL_OFFERING_ID",
    "InMemoryStagedObjectReader",
    "redacted_adapter_build",
    "redacted_authoritative_request",
    "redacted_capabilities",
    "redacted_completed_result",
    "redacted_contract_identity",
    "redacted_multi_currency_requests",
    "redacted_snapshot_request",
]

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64

SNAPSHOT_OFFERING_ID = "FIXTURE_PULSE_V1"
OFFICIAL_OFFERING_ID = "FIXTURE_DAILY_OFFICIAL_V1"

_SOURCE_SCOPE: dict[str, Any] = {
    "provider": "fixture.source",
    "source_account_id": "account-redacted",
    "source_account_generation": 7,
}

redacted_contract_identity = ReportingContractIdentityV1(
    report_definition_id="PAID_MEDIA_DAILY_V1",
    report_definition_uri="https://contracts.example.test/reporting/paid-media-daily-v1",
    report_definition_sha256=_A,
    reporting_profile="paid_media_delivery",
    schema_version="1.0.0",
    schema_uri="https://contracts.example.test/reporting/paid-media-daily-v1/schema",
    schema_sha256=_B,
)

redacted_adapter_build = ReportingAdapterBuildIdentityV1(
    executor_id="fixture-reporting",
    adapter_id="fixture",
    adapter_version="fixture-1",
    adapter_build_sha256=_A,
)

_SNAPSHOT_WINDOWING = ReportingSourceWindowingV1(
    kind="cumulative_current_window",
    minimum_window="PT1M",
    maximum_window="P1D",
    overlapping_windows_supported=True,
)
_OFFICIAL_WINDOWING = ReportingSourceWindowingV1(
    kind="fixed_closed_window",
    minimum_window="P1D",
    maximum_window="P31D",
    overlapping_windows_supported=True,
)

_COMMON_OFFERING: dict[str, Any] = {
    "publication_namespace": "reporting-source:fixture",
    "product_ids": ["fixture-product"],
    "constituent_kinds": ["package_item", "media_buy"],
    "contract": redacted_contract_identity,
    "source_timezone": "America/New_York",
    "dimensions": [DimensionOfferingV1(name="campaign_id")],
    "formats": [SourceFormatV1(media_type="application/x-ndjson")],
    "provider_execution": ProviderExecutionV1(
        pagination="cursor",
        async_jobs="optional",
        maximum_window_days_per_request=31,
        supports_cancellation=True,
    ),
    "retention_days": 30,
}


def redacted_capabilities(
    *, strictness: ReportingManifestStrictness = "basic"
) -> ReportingSourceCapabilitiesV1:
    """A two-offering effective-account declaration: one snapshot, one official."""
    payload: dict[str, Any] = {
        "scope": "effective_account",
        "adapter_build": redacted_adapter_build,
        "source_scope": _SOURCE_SCOPE,
        "capability_version": "fixture-1",
        "capabilities_sha256": _A,
        "offerings": [
            ProvisionalSnapshotOfferingV1(
                offering_id=SNAPSHOT_OFFERING_ID,
                grain="current_source_day_cumulative",
                windowing=_SNAPSHOT_WINDOWING,
                metrics=[
                    MetricOfferingV1(
                        name="impressions",
                        semantic_contract_id="paid_media.impressions.snapshot",
                        semantic_contract_version="1",
                        semantic_contract_sha256=_C,
                    ),
                    MetricOfferingV1(
                        name="spend",
                        semantic_contract_id="paid_media.spend.snapshot",
                        semantic_contract_version="1",
                        semantic_contract_sha256=_A,
                    ),
                ],
                fastest_safe_cadence="PT47M",
                alignment="unaligned",
                expected_availability_lag="PT30M",
                worst_case_availability_lag="PT6H",
                manifest_strictness=strictness,
                **_COMMON_OFFERING,
            ),
            AuthoritativeOfferingV1(
                offering_id=OFFICIAL_OFFERING_ID,
                grain="source_local_day",
                windowing=_OFFICIAL_WINDOWING,
                metrics=[
                    MetricOfferingV1(
                        name="impressions",
                        semantic_contract_id="paid_media.impressions.authoritative",
                        semantic_contract_version="1",
                        semantic_contract_sha256=_B,
                    ),
                    MetricOfferingV1(
                        name="spend",
                        semantic_contract_id="paid_media.spend.authoritative",
                        semantic_contract_version="1",
                        semantic_contract_sha256=_C,
                    ),
                ],
                source_local_ready_time="06:00",
                days_after_period_end=1,
                expected_availability_lag="P1D",
                worst_case_availability_lag="P7D",
                correction_window="P28D",
                correction_policy="immutable_correction",
                manifest_strictness=strictness,
                **_COMMON_OFFERING,
            ),
        ],
    }
    # The checksum binds the declaration, so it has to be computed from the
    # declaration with the placeholder removed -- exactly what an adopter does.
    draft = ReportingSourceCapabilitiesV1.model_construct(**payload)
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(draft)
    return ReportingSourceCapabilitiesV1(**payload)


_CONSTITUENT = MediaBuyConstituentV1(
    constituent_id="campaign-redacted-1",
    product_id="fixture-product",
    media_buy_id="media-buy-redacted",
)


def redacted_multi_currency_requests() -> tuple[ReportingSourceSliceRequestV1, ...]:
    """Two frozen account scopes for adopters testing a shared USD/EUR adapter."""
    return tuple(
        redacted_snapshot_request(
            account_id=f"account-{currency.lower()}",
            currency=currency,
            source_execution_key=f"currency-{currency.lower()}-001",
        )
        for currency in ("USD", "EUR")
    )


def redacted_snapshot_request(
    *,
    source_execution_key: str = "snapshot-execution-001",
    run_id: str = "run-redacted-1",
    source_read_cutoff_at: datetime | None = None,
    trigger: str = "scheduled_poll",
    currency: str = "USD",
    account_id: str = "account-redacted",
) -> ReportingSourceSliceRequestV1:
    """A frozen ``PROVISIONAL_SNAPSHOT`` slice over one media-buy constituent."""
    return ReportingSourceSliceRequestV1(
        identity=ReportingSourceIdentityV1(
            account_id=account_id,
            delivery_config_id="config-redacted",
            delivery_config_version=1,
            report_definition_id="PAID_MEDIA_DAILY_V1",
            reporting_obligation_id="obligation-redacted-1",
            period_key="2026-11-01-current-day",
            source_execution_key=source_execution_key,
            run_id=run_id,
            logical_slice_fingerprint=f"sha256:{_A}",
            source_scope=_SOURCE_SCOPE,
        ),
        adapter_build=redacted_adapter_build,
        offering_id=SNAPSHOT_OFFERING_ID,
        publication_namespace="reporting-source:fixture",
        publication_class="PROVISIONAL_SNAPSHOT",
        contract=redacted_contract_identity,
        period=ReportingSourcePeriodV1(
            period_key="2026-11-01-current-day",
            source_local_date="2026-11-01",
            start=datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc),
            end=datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc),
            source_timezone="America/New_York",
            source_read_cutoff_at=source_read_cutoff_at
            or datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc),
            grain="current_source_day_cumulative",
            windowing=_SNAPSHOT_WINDOWING,
        ),
        revision_kind="snapshot",
        trigger=trigger,  # type: ignore[arg-type]
        coverage=ReportingSourceCoverageRequestV1(
            expected="full",
            constituents=[_CONSTITUENT],
            denominator_fingerprint=coverage_denominator_fingerprint_v1([_CONSTITUENT]),
        ),
        requested_metrics=["impressions", "spend"],
        requested_dimensions=["campaign_id"],
        currency=currency,
        deadline_at=datetime(2099, 11, 2, 10, 0, tzinfo=timezone.utc),
    )


def redacted_authoritative_request(
    *,
    source_execution_key: str = "official-execution-001",
    supersedes_publication_id: str | None = None,
) -> ReportingSourceSliceRequestV1:
    """A frozen ``AUTHORITATIVE`` slice, optionally a correction of a predecessor."""
    base = redacted_snapshot_request(
        source_execution_key=source_execution_key,
        run_id="run-redacted-2",
        source_read_cutoff_at=datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc),
    )
    return base.model_copy(
        update={
            "offering_id": OFFICIAL_OFFERING_ID,
            "publication_class": "AUTHORITATIVE",
            "period": base.period.model_copy(
                update={"grain": "source_local_day", "windowing": _OFFICIAL_WINDOWING}
            ),
            "revision_kind": "correction" if supersedes_publication_id else "authoritative",
            "supersedes_publication_id": supersedes_publication_id,
        }
    )


class InMemoryStagedObjectReader:
    """A staged-object reader over a dict, enforcing the account scope fence."""

    def __init__(
        self, objects: dict[tuple[str, str], bytes], *, account_id: str, source_scope: Any
    ) -> None:
        self._objects = objects
        self._account_id = account_id
        self._source_scope = dict(source_scope)

    async def read(
        self,
        *,
        object_ref: str,
        object_generation: str,
        account_id: str,
        source_scope: Any,
        cancel: Any,
    ) -> bytes:
        if account_id != self._account_id or dict(source_scope) != self._source_scope:
            raise PermissionError("staged object scope mismatch")
        try:
            return self._objects[(object_ref, object_generation)]
        except KeyError as error:
            raise FileNotFoundError("generation-pinned object not found") from error


def redacted_completed_result(
    request: ReportingSourceSliceRequestV1,
    *,
    page_bodies: list[str] | None = None,
    row_counts: list[int] | None = None,
    explicit_zero: bool | None = None,
    constituent_status: str | None = None,
    observed_at: datetime | None = None,
    data_through: datetime | None = None,
) -> tuple[ReportingSourceExecutorResult, SourceBatchManifestV1, ReportingSourceStagedObjectReader]:
    """Build a conforming ``basic`` publication for ``request``.

    Returns the executor result, the parsed manifest, and a reader that will
    serve exactly the staged objects the manifest names.
    """
    page_bodies = page_bodies or [
        '{"campaign_id":"campaign-redacted-1","impressions":10,"spend":"1.25"}\n'
    ]
    row_counts = row_counts if row_counts is not None else [1] * len(page_bodies)
    if len(page_bodies) != len(row_counts):
        raise ValueError("page bodies and row counts differ")

    payloads = [body.encode("utf-8") for body in page_bodies]
    objects = [
        SourceBatchObjectV1(
            ordinal=ordinal,
            object_ref=f"object-redacted-{ordinal}",
            object_generation=f"generation-redacted-{ordinal}",
            media_type="application/x-ndjson",
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_count=len(payload),
            row_count=row_counts[ordinal],
        )
        for ordinal, payload in enumerate(payloads)
    ]
    rows_are_zero = all(count == 0 for count in row_counts)
    status = constituent_status or ("explicit_zero" if rows_are_zero else "present")
    # A batch-wide explicit zero is a claim that the source *answered* for
    # every requested cell. Zero rows plus an unavailable constituent is the
    # opposite claim, so the two are derived separately.
    zero = (
        explicit_zero
        if explicit_zero is not None
        else (rows_are_zero and status == "explicit_zero")
    )
    observed = observed_at or request.period.source_read_cutoff_at
    through = data_through or min(request.period.source_read_cutoff_at, request.period.end)
    available = status in {"present", "explicit_zero"}
    evidence: dict[str, Any] = (
        {"data_through": through} if available else {"reason": "Source did not return this scope"}
    )
    capabilities = redacted_capabilities()
    offering = capabilities.offering(request.offering_id)
    declared = {metric.name: metric for metric in offering.metrics}

    coverage = SourceBatchCoverageV1(
        denominator_fingerprint=request.coverage.denominator_fingerprint,
        status="full" if available else "none",
        constituents=[
            ReportingConstituentCoverageV1(constituent=item, status=status, **evidence)  # type: ignore[arg-type]
            for item in request.coverage.constituents
        ],
    )
    draft: dict[str, Any] = {
        "identity": request.identity,
        "publication_namespace": request.publication_namespace,
        "publication_id": deterministic_source_publication_id_v1(
            source_execution_key=request.identity.source_execution_key,
            logical_slice_fingerprint=request.identity.logical_slice_fingerprint,
            publication_class=request.publication_class,
            revision_kind=request.revision_kind,
            supersedes_publication_id=request.supersedes_publication_id,
        ),
        "publication_class": request.publication_class,
        "content_fingerprint": f"sha256:{_A}",
        "adapter_build": request.adapter_build,
        "offering_id": request.offering_id,
        "contract": request.contract,
        "period": request.period,
        "revision_kind": request.revision_kind,
        "supersedes_publication_id": request.supersedes_publication_id,
        "finality_evidence": ReportingFinalityEvidenceV1(
            basis=(
                "provisional_observation"
                if request.publication_class == "PROVISIONAL_SNAPSHOT"
                else "provider_declared"
            ),
            observed_at=(
                max(observed, request.period.end)
                if request.publication_class == "AUTHORITATIVE"
                else observed
            ),
            evidence_ref="source-evidence-redacted",
        ),
        "observed_at": observed,
        "data_through": through,
        "acquired_at": max(
            observed,
            request.period.end if request.publication_class == "AUTHORITATIVE" else observed,
        ),
        "currency": request.currency,
        "requested_dimensions": list(request.requested_dimensions),
        "objects": objects,
        "object_set_sha256": source_batch_object_set_sha256_v1(objects),
        "row_count": sum(row_counts),
        "byte_count": sum(len(payload) for payload in payloads),
        # Totals follow the rows, not the explicit-zero flag: an unavailable
        # zero-row batch has nothing to total either.
        "control_totals": [
            SourceControlTotalV1(name="impressions", value="10" if sum(row_counts) else "0")
        ],
        "metric_availability": [
            ReportingMetricAvailabilityV1(
                constituent_id=constituent.constituent_id,
                metric=metric,
                semantic_contract_id=declared[metric].semantic_contract_id,
                semantic_contract_version=declared[metric].semantic_contract_version,
                semantic_contract_sha256=declared[metric].semantic_contract_sha256,
                status=status,  # type: ignore[arg-type]
                **evidence,
            )
            for constituent in request.coverage.constituents
            for metric in request.requested_metrics
        ],
        "coverage": coverage,
        "explicit_zero": zero,
        "event_time_range": None if sum(row_counts) == 0 else (request.period.start, through),
        "prior_checkpoint": request.prior_checkpoint,
    }
    draft["content_fingerprint"] = publication_content_fingerprint_v1(
        SourceBatchManifestV1.model_construct(**draft)
    )
    manifest = SourceBatchManifestV1(**draft)
    manifest_bytes = encode_source_batch_manifest_v1(manifest)
    reference = source_batch_manifest_reference_v1("manifest-redacted-v1", manifest_bytes)
    reader = InMemoryStagedObjectReader(
        {
            (item.object_ref, item.object_generation): payload
            for item, payload in zip(objects, payloads)
        },
        account_id=request.identity.account_id,
        source_scope=request.identity.source_scope,
    )
    result = ReportingSourceExecutorResult.completed(
        request=request, manifest=reference, manifest_bytes=manifest_bytes
    )
    return result, manifest, reader


def one_day_later(value: datetime) -> datetime:
    """Small helper so fixture callers do not import :mod:`datetime` themselves."""
    return value + timedelta(days=1)
