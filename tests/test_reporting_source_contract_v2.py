"""Versioned package source contracts keep each package bound to its media buy."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from adcp.reporting.conformance import (
    ReportingSourceConformanceError,
    validate_reporting_source_execution,
)
from adcp.reporting.fixtures import (
    redacted_capabilities,
    redacted_completed_result,
    redacted_snapshot_request,
)
from adcp.reporting.source import (
    PackageItemConstituentV1,
    PackageItemConstituentV2,
    ReportingConstituentCoverageV2,
    ReportingSourceCapabilitiesV2,
    ReportingSourceCoverageRequestV2,
    ReportingSourceExecutionResponseV2,
    ReportingSourceExecutorResult,
    ReportingSourceSliceRequestV1,
    ReportingSourceSliceRequestV2,
    ReportingSourceStagedObjectReader,
    SourceBatchCoverageV1,
    SourceBatchCoverageV2,
    SourceBatchManifestError,
    SourceBatchManifestV2,
    coverage_denominator_fingerprint_v1,
    coverage_denominator_fingerprint_v2,
    encode_source_batch_manifest_v2,
    publication_content_fingerprint_v2,
    reporting_source_capabilities_sha256_v1,
    reporting_source_capabilities_sha256_v2,
    source_batch_coverage_fingerprint_v2,
    source_batch_manifest_reference_v1,
    validate_reporting_source_rows_v2,
    validate_reporting_source_staged_rows_v2,
)


def _package_request() -> ReportingSourceSliceRequestV2:
    base = redacted_snapshot_request()
    constituent = PackageItemConstituentV2(
        constituent_id="package-constituent",
        product_id="fixture-product",
        media_buy_id="buy-1",
        package_id="package-1",
    )
    payload = base.model_dump(mode="json")
    payload["contract_version"] = "2.0"
    payload["coverage"] = ReportingSourceCoverageRequestV2(
        media_buy_ids=["buy-1"],
        constituents=[constituent],
        denominator_fingerprint=coverage_denominator_fingerprint_v2([constituent]),
    ).model_dump(mode="json")
    return ReportingSourceSliceRequestV2.model_validate(payload)


def _v2_capabilities() -> ReportingSourceCapabilitiesV2:
    payload = redacted_capabilities().model_dump(mode="json")
    payload["contract_version"] = "2.0"
    payload["supported_contract_versions"] = ["2.0"]
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v2(payload)
    return ReportingSourceCapabilitiesV2.model_validate(payload)


def _v2_result(
    request: ReportingSourceSliceRequestV2,
) -> tuple[ReportingSourceExecutorResult, SourceBatchManifestV2, ReportingSourceStagedObjectReader]:
    base = redacted_snapshot_request()
    _, manifest, reader = redacted_completed_result(base)
    payload = manifest.model_dump(mode="json")
    payload["manifest_version"] = "2.0"
    payload["coverage"]["denominator_fingerprint"] = request.coverage.denominator_fingerprint
    payload["coverage"]["media_buy_ids"] = list(request.coverage.media_buy_ids)
    payload["coverage"]["constituents"][0]["constituent"] = request.coverage.constituents[
        0
    ].model_dump(mode="json")
    for cell in payload["metric_availability"]:
        cell["constituent_id"] = request.coverage.constituents[0].constituent_id
    payload["content_fingerprint"] = publication_content_fingerprint_v2(payload)
    versioned_manifest = SourceBatchManifestV2.model_validate(payload)
    manifest_bytes = encode_source_batch_manifest_v2(versioned_manifest)
    reference = source_batch_manifest_reference_v1("manifest-package-v2", manifest_bytes)
    result = ReportingSourceExecutorResult.completed(
        request=request, manifest=reference, manifest_bytes=manifest_bytes
    )
    return result, versioned_manifest, reader


def _empty_package_request(media_buy_id: str) -> ReportingSourceSliceRequestV2:
    payload = redacted_snapshot_request().model_dump(mode="json")
    payload["contract_version"] = "2.0"
    payload["coverage"] = {
        "expected": "full",
        "media_buy_ids": [media_buy_id],
        "constituents": [],
        "denominator_fingerprint": coverage_denominator_fingerprint_v2(
            [], media_buy_ids=[media_buy_id]
        ),
    }
    return ReportingSourceSliceRequestV2.model_validate(payload)


def test_v2_empty_pairs_keep_distinct_frozen_buy_selectors() -> None:
    first = _empty_package_request("buy-1")
    second = _empty_package_request("buy-2")
    assert first.media_buy_ids == ("buy-1",)
    assert second.media_buy_ids == ("buy-2",)
    assert first.coverage.denominator_fingerprint != second.coverage.denominator_fingerprint


async def test_v2_complete_empty_report_admits_zero_rows_without_metric_cells() -> None:
    request = _empty_package_request("buy-1")
    base = redacted_snapshot_request()
    _, manifest, reader = redacted_completed_result(base, page_bodies=[""], row_counts=[0])
    payload = manifest.model_dump(mode="json")
    payload["manifest_version"] = "2.0"
    payload["coverage"] = {
        "media_buy_ids": ["buy-1"],
        "denominator_fingerprint": request.coverage.denominator_fingerprint,
        "status": "full",
        "constituents": [],
    }
    payload["metric_availability"] = []
    payload["explicit_zero"] = False
    payload["content_fingerprint"] = publication_content_fingerprint_v2(payload)
    complete = SourceBatchManifestV2.model_validate(payload)
    manifest_bytes = encode_source_batch_manifest_v2(complete)
    result = ReportingSourceExecutorResult.completed(
        request=request,
        manifest=source_batch_manifest_reference_v1("manifest-complete-empty", manifest_bytes),
        manifest_bytes=manifest_bytes,
    )
    assert (
        await validate_reporting_source_execution(
            capabilities=_v2_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
        == complete
    )


def test_v2_complete_empty_report_requires_current_source_watermark() -> None:
    request = _empty_package_request("buy-1")
    _, manifest, _ = redacted_completed_result(
        redacted_snapshot_request(), page_bodies=[""], row_counts=[0]
    )
    payload = manifest.model_dump(mode="json")
    payload["manifest_version"] = "2.0"
    payload["coverage"] = {
        "media_buy_ids": ["buy-1"],
        "denominator_fingerprint": request.coverage.denominator_fingerprint,
        "status": "full",
        "constituents": [],
    }
    payload["metric_availability"] = []
    payload["explicit_zero"] = False
    payload["data_through"] = request.period.start.isoformat()
    payload["content_fingerprint"] = publication_content_fingerprint_v2(payload)
    with pytest.raises(ValidationError, match="current source cutoff"):
        SourceBatchManifestV2.model_validate(payload)


def test_v1_package_shape_and_request_bytes_remain_frozen() -> None:
    request = redacted_snapshot_request()
    assert request.contract_version == "1.0"
    encoded = request.model_dump_json()
    assert ReportingSourceSliceRequestV1.model_validate_json(encoded).model_dump_json() == encoded
    assert hashlib.sha256(encoded.encode()).hexdigest() == (
        "53b59efb8632ac2e314da2d4144ed2295ef1c7e9a49cb1fc3f4d55f9da724585"
    )
    with pytest.raises(ValidationError):
        PackageItemConstituentV1(
            constituent_id="item",
            product_id="fixture-product",
            package_id="package-1",
            media_buy_id="buy-1",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        ReportingSourceSliceRequestV1.model_validate(
            {**request.model_dump(mode="json"), "contract_version": "2.0"}
        )


def test_v1_coverage_rejects_nested_v2_package_instance() -> None:
    package = _package_request().coverage.constituents[0]
    assert isinstance(package, PackageItemConstituentV2)
    base = redacted_snapshot_request()
    payload = base.coverage.model_dump(mode="json")
    payload["constituents"] = [package]
    payload["denominator_fingerprint"] = coverage_denominator_fingerprint_v1([package])
    with pytest.raises(ValidationError):
        type(base.coverage).model_validate(payload)


def test_v1_manifest_coverage_rejects_completed_v2_constituent_evidence() -> None:
    package = _package_request().coverage.constituents[0]
    item = ReportingConstituentCoverageV2(constituent=package, status="present")
    with pytest.raises(ValidationError):
        SourceBatchCoverageV1(
            denominator_fingerprint=coverage_denominator_fingerprint_v1([package]),
            status="full",
            constituents=[item],
        )


def test_v1_manifest_coverage_rejects_v2_batch_instance() -> None:
    _, manifest, _ = _v2_result(_package_request())
    with pytest.raises(ValidationError):
        SourceBatchCoverageV1.model_validate(manifest.coverage)


def test_v2_package_requires_both_parent_and_package() -> None:
    package = _package_request().coverage.constituents[0]
    for absent in ("media_buy_id", "package_id"):
        payload = package.model_dump(mode="json")
        payload.pop(absent)
        with pytest.raises(ValidationError):
            PackageItemConstituentV2.model_validate(payload)


def test_v2_denominator_fingerprint_binds_both_ids() -> None:
    package = _package_request().coverage.constituents[0]
    original = coverage_denominator_fingerprint_v2([package])
    assert original != coverage_denominator_fingerprint_v2(
        [package.model_copy(update={"media_buy_id": "buy-2"})]
    )
    assert original != coverage_denominator_fingerprint_v2(
        [package.model_copy(update={"package_id": "package-2"})]
    )
    assert _package_request().media_buy_ids == ("buy-1",)
    assert _package_request().package_ids == ("package-1",)


def test_v2_selector_accepts_the_package_constituent_external_buy_id() -> None:
    payload = _package_request().model_dump(mode="json")
    package = (
        _package_request()
        .coverage.constituents[0]
        .model_copy(update={"media_buy_id": "buyer/buy-1"})
    )
    payload["coverage"] = {
        "expected": "full",
        "media_buy_ids": ["buyer/buy-1"],
        "constituents": [package.model_dump(mode="json")],
        "denominator_fingerprint": coverage_denominator_fingerprint_v2(
            [package], media_buy_ids=["buyer/buy-1"]
        ),
    }
    request = ReportingSourceSliceRequestV2.model_validate(payload)
    _, manifest, _ = _v2_result(request)
    assert request.media_buy_ids == manifest.coverage.media_buy_ids == ("buyer/buy-1",)


def test_v2_request_rejects_duplicate_pair_with_distinct_constituent_ids() -> None:
    package = _package_request().coverage.constituents[0]
    duplicate = package.model_copy(update={"constituent_id": "package-constituent-2"})
    with pytest.raises(ValidationError):
        ReportingSourceCoverageRequestV2(
            media_buy_ids=["buy-1"],
            constituents=[package, duplicate],
            denominator_fingerprint=coverage_denominator_fingerprint_v2([package, duplicate]),
        )


def test_v2_manifest_rejects_duplicate_pair_with_distinct_constituent_ids() -> None:
    request = _package_request()
    _, manifest, _ = _v2_result(request)
    package = request.coverage.constituents[0]
    duplicate = package.model_copy(update={"constituent_id": "package-constituent-2"})
    payload = manifest.model_dump(mode="json")
    item = payload["coverage"]["constituents"][0].copy()
    item["constituent"] = duplicate.model_dump(mode="json")
    payload["coverage"]["constituents"].append(item)
    payload["coverage"]["denominator_fingerprint"] = coverage_denominator_fingerprint_v2(
        [package, duplicate]
    )
    for cell in list(payload["metric_availability"]):
        payload["metric_availability"].append({**cell, "constituent_id": duplicate.constituent_id})
    payload["content_fingerprint"] = publication_content_fingerprint_v2(payload)
    with pytest.raises(ValidationError):
        SourceBatchManifestV2.model_validate(payload)


def test_v2_allows_one_package_id_under_two_buys() -> None:
    package = _package_request().coverage.constituents[0]
    shared_id = package.model_copy(
        update={"constituent_id": "package-constituent-2", "media_buy_id": "buy-2"}
    )
    denominator = coverage_denominator_fingerprint_v2([package, shared_id])
    request = ReportingSourceCoverageRequestV2(
        media_buy_ids=["buy-1", "buy-2"],
        constituents=[package, shared_id],
        denominator_fingerprint=denominator,
    )
    coverage = SourceBatchCoverageV2(
        media_buy_ids=["buy-1", "buy-2"],
        denominator_fingerprint=denominator,
        status="full",
        constituents=[
            ReportingConstituentCoverageV2(constituent=item, status="present")
            for item in request.constituents
        ],
    )
    assert [(item.media_buy_id, item.package_id) for item in request.constituents] == [
        ("buy-1", "package-1"),
        ("buy-2", "package-1"),
    ]
    assert coverage.status == "full"


def test_v2_rows_require_an_assigned_package_pair() -> None:
    from adcp.reporting.source import validate_reporting_source_rows_v2

    package = _package_request().coverage.constituents[0]
    other_buy = package.model_copy(
        update={"constituent_id": "package-constituent-2", "media_buy_id": "buy-2"}
    )
    request = _package_request().model_copy(
        update={
            "coverage": ReportingSourceCoverageRequestV2(
                media_buy_ids=["buy-1", "buy-2"],
                constituents=[package, other_buy],
                denominator_fingerprint=coverage_denominator_fingerprint_v2([package, other_buy]),
            )
        }
    )
    _, base_manifest, _ = _v2_result(_package_request())
    manifest_payload = base_manifest.model_dump(mode="json")
    manifest_payload["coverage"]["denominator_fingerprint"] = (
        request.coverage.denominator_fingerprint
    )
    manifest_payload["coverage"]["media_buy_ids"] = list(request.coverage.media_buy_ids)
    other_evidence = dict(manifest_payload["coverage"]["constituents"][0])
    other_evidence["constituent"] = other_buy.model_dump(mode="json")
    manifest_payload["coverage"]["constituents"].append(other_evidence)
    for cell in list(manifest_payload["metric_availability"]):
        manifest_payload["metric_availability"].append(
            {**cell, "constituent_id": other_buy.constituent_id}
        )
    manifest_payload["content_fingerprint"] = publication_content_fingerprint_v2(manifest_payload)
    manifest = SourceBatchManifestV2.model_validate(manifest_payload)
    with pytest.raises(ValueError, match="manifest coverage differs"):
        validate_reporting_source_rows_v2(_package_request(), manifest, [])
    validate_reporting_source_rows_v2(
        request,
        manifest,
        [
            {"media_buy_id": "buy-1", "package_id": "package-1", "impressions": 5},
            {"media_buy_id": "buy-2", "package_id": "package-1", "impressions": 0},
        ],
    )
    for row in (
        {"media_buy_id": "buy-3", "package_id": "package-1"},
        {"package_id": "package-1"},
        {"media_buy_id": "buy-1", "package_id": "external-line-1"},
        {
            "media_buy_id": "buy-1",
            "package_id": "package-1",
            "constituent_id": other_buy.constituent_id,
        },
        {"line_id": "external-line-1"},
    ):
        with pytest.raises(ValueError, match="outside the requested coverage"):
            validate_reporting_source_rows_v2(request, manifest, [row])


def test_v2_staged_rows_are_fenced_by_request_before_manifest_exists() -> None:
    from adcp.reporting.source import validate_reporting_source_staged_rows_v2

    request = _package_request()
    validate_reporting_source_staged_rows_v2(
        request, [{"media_buy_id": "buy-1", "package_id": "package-1"}]
    )
    for rows in (
        [{"media_buy_id": "buy-1"}],
        [{"media_buy_id": "buy-2", "package_id": "package-1"}],
        [{"media_buy_id": "buy-1", "package_id": "package-2"}],
        [{"media_buy_id": "buy-2", "package_id": "package-1", "impressions": 0}],
        [{"media_buy_id": "buy-1", "package_id": "package-2", "impressions": 0}],
    ):
        with pytest.raises(ValueError, match="outside the requested coverage"):
            validate_reporting_source_staged_rows_v2(request, rows)


@pytest.mark.parametrize("stage", ["staged", "final"])
def test_v2_rows_reject_duplicate_package_and_requested_dimension_values(stage: str) -> None:
    request = _package_request()
    _, manifest, _ = _v2_result(request)
    rows = [
        {
            "media_buy_id": "buy-1",
            "package_id": "package-1",
            "campaign_id": "campaign-1",
            "impressions": 5,
        },
        {
            "media_buy_id": "buy-1",
            "package_id": "package-1",
            "campaign_id": "campaign-1",
            "impressions": 7,
        },
    ]
    with pytest.raises(ValueError, match="duplicate row"):
        if stage == "staged":
            validate_reporting_source_staged_rows_v2(request, rows)
        else:
            validate_reporting_source_rows_v2(request, manifest, rows)


@pytest.mark.parametrize("stage", ["staged", "final"])
def test_v2_rows_preserve_distinct_requested_dimension_values(stage: str) -> None:
    request = _package_request()
    _, manifest, _ = _v2_result(request)
    rows = [
        {"media_buy_id": "buy-1", "package_id": "package-1", "campaign_id": "campaign-1"},
        {"media_buy_id": "buy-1", "package_id": "package-1", "campaign_id": "campaign-2"},
    ]
    if stage == "staged":
        validate_reporting_source_staged_rows_v2(request, rows)
    else:
        validate_reporting_source_rows_v2(request, manifest, rows)


def test_v2_manifest_keeps_explicit_zero_on_one_package_pair() -> None:
    package = _package_request().coverage.constituents[0]
    other_buy = package.model_copy(
        update={"constituent_id": "package-constituent-2", "media_buy_id": "buy-2"}
    )
    _, manifest, _ = _v2_result(_package_request())
    payload = manifest.model_dump(mode="json")
    payload["coverage"]["denominator_fingerprint"] = coverage_denominator_fingerprint_v2(
        [package, other_buy]
    )
    payload["coverage"]["media_buy_ids"] = ["buy-1", "buy-2"]
    zero_evidence = dict(payload["coverage"]["constituents"][0])
    zero_evidence["constituent"] = other_buy.model_dump(mode="json")
    zero_evidence["status"] = "explicit_zero"
    payload["coverage"]["constituents"].append(zero_evidence)
    for cell in list(payload["metric_availability"]):
        payload["metric_availability"].append(
            {**cell, "constituent_id": other_buy.constituent_id, "status": "explicit_zero"}
        )
    payload["content_fingerprint"] = publication_content_fingerprint_v2(payload)
    versioned = SourceBatchManifestV2.model_validate(payload)
    assert [item.status for item in versioned.coverage.constituents] == [
        "present",
        "explicit_zero",
    ]
    from adcp.reporting.source import validate_reporting_source_rows_v2

    request_payload = _package_request().model_dump(mode="json")
    request_payload["coverage"] = ReportingSourceCoverageRequestV2(
        media_buy_ids=["buy-1", "buy-2"],
        constituents=[package, other_buy],
        denominator_fingerprint=payload["coverage"]["denominator_fingerprint"],
    ).model_dump(mode="json")
    request = ReportingSourceSliceRequestV2.model_validate(request_payload)
    with pytest.raises(ValueError, match="explicit zero"):
        validate_reporting_source_rows_v2(
            request,
            versioned,
            [{"media_buy_id": "buy-2", "package_id": "package-1"}],
        )


def test_v2_request_retains_metric_cell_limit() -> None:
    package = _package_request().coverage.constituents[0]
    other_buy = package.model_copy(
        update={"constituent_id": "package-constituent-2", "media_buy_id": "buy-2"}
    )
    payload = _package_request().model_dump(mode="json")
    payload["coverage"] = ReportingSourceCoverageRequestV2(
        media_buy_ids=["buy-1", "buy-2"],
        constituents=[package, other_buy],
        denominator_fingerprint=coverage_denominator_fingerprint_v2([package, other_buy]),
    ).model_dump(mode="json")
    payload["requested_metrics"] = [f"metric_{index}" for index in range(501)]
    with pytest.raises(ValidationError, match="at most 1000 constituent-metric cells"):
        ReportingSourceSliceRequestV2.model_validate(payload)


def test_v2_two_metric_package_limit_is_checked_at_request_validation() -> None:
    first = _package_request().coverage.constituents[0]

    def request_with_packages(count: int) -> ReportingSourceSliceRequestV2:
        packages = [
            first.model_copy(
                update={"constituent_id": f"constituent-{index}", "package_id": f"package-{index}"}
            )
            for index in range(count)
        ]
        payload = _package_request().model_dump(mode="json")
        payload["coverage"] = {
            "expected": "full",
            "media_buy_ids": ["buy-1"],
            "constituents": [item.model_dump(mode="json") for item in packages],
            "denominator_fingerprint": coverage_denominator_fingerprint_v2(
                packages, media_buy_ids=["buy-1"]
            ),
        }
        return ReportingSourceSliceRequestV2.model_validate(payload)

    assert len(request_with_packages(500).coverage.constituents) == 500
    with pytest.raises(ValidationError, match="at most 1000 constituent-metric cells"):
        request_with_packages(501)


def test_v2_capability_checksum_binds_declared_version() -> None:
    payload = redacted_capabilities().model_dump(mode="json")
    payload["contract_version"] = "2.0"
    assert reporting_source_capabilities_sha256_v1(payload) != payload["capabilities_sha256"]
    with pytest.raises(ValidationError):
        ReportingSourceCapabilitiesV2.model_validate(payload)


def test_v2_contract_versions_are_explicit() -> None:
    capabilities_payload = _v2_capabilities().model_dump(mode="json")
    capabilities_payload.pop("contract_version")
    with pytest.raises(ValidationError):
        ReportingSourceCapabilitiesV2.model_validate(capabilities_payload)
    capabilities_payload = _v2_capabilities().model_dump(mode="json")
    capabilities_payload.pop("supported_contract_versions")
    with pytest.raises(ValidationError):
        ReportingSourceCapabilitiesV2.model_validate(capabilities_payload)

    request_payload = _package_request().model_dump(mode="json")
    request_payload.pop("contract_version")
    with pytest.raises(ValidationError):
        ReportingSourceSliceRequestV2.model_validate(request_payload)

    result, manifest, _ = _v2_result(_package_request())
    manifest_payload = manifest.model_dump(mode="json")
    manifest_payload.pop("manifest_version")
    with pytest.raises(ValidationError):
        SourceBatchManifestV2.model_validate(manifest_payload)
    assert result.response is not None
    response_payload = result.response.model_dump(mode="json")
    response_payload.pop("contract_version")
    with pytest.raises(ValidationError):
        ReportingSourceExecutionResponseV2.model_validate(response_payload)


def test_supported_contract_versions_cannot_change_after_checksum_validation() -> None:
    capabilities = _v2_capabilities()
    checksum = capabilities.capabilities_sha256
    with pytest.raises(AttributeError):
        capabilities.supported_contract_versions.append("1.0")
    assert capabilities.capabilities_sha256 == checksum
    assert capabilities.supported_contract_versions == ("2.0",)


@pytest.mark.parametrize("versions", [["1.0"], ["2.0", "2.0"]])
def test_v2_capability_rejects_incoherent_supported_versions(versions: list[str]) -> None:
    payload = _v2_capabilities().model_dump(mode="json")
    payload["supported_contract_versions"] = versions
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v2(payload)
    with pytest.raises(ValidationError):
        ReportingSourceCapabilitiesV2.model_validate(payload)


def test_v2_coverage_fingerprint_binds_package_pair() -> None:
    package = _package_request().coverage.constituents[0]
    item = ReportingConstituentCoverageV2(constituent=package, status="present")
    rebound = ReportingConstituentCoverageV2(
        constituent=package.model_copy(update={"media_buy_id": "buy-2"}), status="present"
    )
    assert source_batch_coverage_fingerprint_v2(
        [item], media_buy_ids=["buy-1"]
    ) != source_batch_coverage_fingerprint_v2([rebound], media_buy_ids=["buy-2"])


def test_v2_coverage_fingerprint_binds_empty_buy_selector() -> None:
    assert source_batch_coverage_fingerprint_v2(
        [], media_buy_ids=["buy-1"]
    ) != source_batch_coverage_fingerprint_v2([], media_buy_ids=["buy-2"])


async def test_v2_execution_requires_explicit_capability_and_matching_manifest() -> None:
    request = _package_request()
    result, manifest, reader = _v2_result(request)
    assert manifest.manifest_version == "2.0"
    assert manifest.contract_version == "2.0"
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "CAPABILITY_MISMATCH"
    assert (
        await validate_reporting_source_execution(
            capabilities=_v2_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
        == manifest
    )


async def test_v2_empty_pair_request_requires_package_capability() -> None:
    request = _empty_package_request("buy-1")
    payload = _v2_capabilities().model_dump(mode="json")
    for offering in payload["offerings"]:
        offering["constituent_kinds"] = ["media_buy"]
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v2(payload)
    capabilities = ReportingSourceCapabilitiesV2.model_validate(payload)
    result, _, reader = _v2_result(_package_request())
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=capabilities,
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "CAPABILITY_MISMATCH"


async def test_one_executor_can_negotiate_v1_and_v2_requests() -> None:
    payload = _v2_capabilities().model_dump(mode="json")
    payload["supported_contract_versions"] = ["1.0", "2.0"]
    payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v2(payload)
    capabilities = ReportingSourceCapabilitiesV2.model_validate(payload)

    v1_request = redacted_snapshot_request()
    v1_result, v1_manifest, v1_reader = redacted_completed_result(v1_request)
    assert (
        await validate_reporting_source_execution(
            capabilities=capabilities,
            request=v1_request,
            result=v1_result,
            object_reader=v1_reader,
        )
        == v1_manifest
    )

    v2_request = _package_request()
    v2_result, v2_manifest, v2_reader = _v2_result(v2_request)
    assert (
        await validate_reporting_source_execution(
            capabilities=capabilities,
            request=v2_request,
            result=v2_result,
            object_reader=v2_reader,
        )
        == v2_manifest
    )


async def test_v2_only_capability_rejects_v1_request() -> None:
    request = redacted_snapshot_request()
    result, _, reader = redacted_completed_result(request)
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=_v2_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "CAPABILITY_MISMATCH"


async def test_mixed_request_manifest_versions_fail_admission() -> None:
    request = _package_request()
    _, v1_manifest, reader = redacted_completed_result(redacted_snapshot_request())
    payload = v1_manifest.model_dump(mode="json")
    payload["coverage"]["denominator_fingerprint"] = request.coverage.denominator_fingerprint
    payload["coverage"]["constituents"][0]["constituent"] = request.coverage.constituents[
        0
    ].model_dump(mode="json")
    for cell in payload["metric_availability"]:
        cell["constituent_id"] = request.coverage.constituents[0].constituent_id
    payload["content_fingerprint"] = publication_content_fingerprint_v2(payload)
    from adcp.reporting.canonical_json import canonical_json_utf8_v1

    manifest_bytes = canonical_json_utf8_v1(payload)
    result = ReportingSourceExecutorResult.completed(
        request=request,
        manifest=source_batch_manifest_reference_v1("manifest-mixed", manifest_bytes),
        manifest_bytes=manifest_bytes,
    )
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=_v2_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "MANIFEST_MISMATCH"


async def test_v1_request_rejects_v2_manifest() -> None:
    request = redacted_snapshot_request()
    versioned_request = _package_request()
    versioned_result, _, reader = _v2_result(versioned_request)
    assert versioned_result.response is not None
    assert versioned_result.manifest_bytes is not None
    result = ReportingSourceExecutorResult.completed(
        request=request,
        manifest=versioned_result.response.manifest,
        manifest_bytes=versioned_result.manifest_bytes,
    )
    with pytest.raises(ReportingSourceConformanceError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
    assert error.value.code == "MANIFEST_MISMATCH"


def test_v2_manifest_rejects_package_without_parent_buy() -> None:
    request = _package_request()
    _, manifest, _ = _v2_result(request)
    payload = manifest.model_dump(mode="json")
    payload["coverage"]["constituents"][0]["constituent"].pop("media_buy_id")
    with pytest.raises(ValidationError):
        SourceBatchManifestV2.model_validate(payload)


def test_v2_execution_response_round_trips_without_downgrading() -> None:
    result, _, _ = _v2_result(_package_request())
    round_trip = ReportingSourceExecutorResult.model_validate_json(result.model_dump_json())
    assert round_trip.response is not None
    assert round_trip.response.contract_version == "2.0"


async def test_mixed_version_bytes_must_pass_reference_integrity_first() -> None:
    result, _, reader = _v2_result(_package_request())
    assert result.response is not None
    bad_reference = result.response.manifest.model_copy(update={"manifest_sha256": "0" * 64})
    mixed = ReportingSourceExecutorResult.completed(
        request=redacted_snapshot_request(),
        manifest=bad_reference,
        manifest_bytes=result.manifest_bytes or b"",
    )
    with pytest.raises(SourceBatchManifestError) as error:
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=redacted_snapshot_request(),
            result=mixed,
            object_reader=reader,
        )
    assert error.value.code == "SOURCE_MANIFEST_CHECKSUM_MISMATCH"
