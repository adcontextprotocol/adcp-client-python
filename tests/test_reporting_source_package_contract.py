"""One source contract covers buy and package denominators."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from adcp.reporting.canonical_json import canonical_json_utf8_v1
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
    ReportingSourceCapabilitiesV1,
    ReportingSourceCoverageRequestV1,
    ReportingSourceSliceRequestV1,
    SourceBatchManifestV1,
    coverage_denominator_fingerprint_v1,
    encode_source_batch_manifest_v1,
    parse_verified_source_batch_manifest_v1,
    publication_content_fingerprint_v1,
    reporting_source_capabilities_sha256_v1,
    source_batch_manifest_reference_v1,
    validate_reporting_source_rows_v1,
    validate_reporting_source_staged_rows_v1,
)


def _package_request(*, campaign_dimension: bool = False) -> ReportingSourceSliceRequestV1:
    package = PackageItemConstituentV1(
        constituent_id="package-constituent",
        product_id="fixture-product",
        media_buy_id="buy-1",
        package_id="package-1",
    )
    payload = redacted_snapshot_request().model_dump(mode="json")
    payload["requested_dimensions"] = (
        ["package", "campaign_id"] if campaign_dimension else ["package"]
    )
    payload["coverage"] = ReportingSourceCoverageRequestV1(
        grain="package_item",
        media_buy_ids=("buy-1",),
        constituents=[package],
        denominator_fingerprint=coverage_denominator_fingerprint_v1(
            [package], grain="package_item", media_buy_ids=("buy-1",)
        ),
    ).model_dump(mode="json")
    return ReportingSourceSliceRequestV1.model_validate(payload)


def _empty_package_request(buy_id: str = "buy-1") -> ReportingSourceSliceRequestV1:
    payload = redacted_snapshot_request().model_dump(mode="json")
    payload["coverage"] = {
        "expected": "full",
        "grain": "package_item",
        "media_buy_ids": [buy_id],
        "constituents": [],
        "denominator_fingerprint": coverage_denominator_fingerprint_v1(
            [], grain="package_item", media_buy_ids=(buy_id,)
        ),
    }
    return ReportingSourceSliceRequestV1.model_validate(payload)


def test_ordinary_buy_v1_wire_and_hashes_remain_frozen() -> None:
    request = redacted_snapshot_request()
    request_json = request.model_dump_json()
    canonical_request = canonical_json_utf8_v1(request.model_dump(mode="json", exclude_none=True))
    assert hashlib.sha256(canonical_request).hexdigest() == (
        "0b81860effeac0424f5d2efa9d8fc680ee86cb226749dbe17812bf94296c0f50"
    )
    assert hashlib.sha256(request_json.encode()).hexdigest() == (
        "53b59efb8632ac2e314da2d4144ed2295ef1c7e9a49cb1fc3f4d55f9da724585"
    )
    assert request.coverage.denominator_fingerprint == (
        "sha256:27bf44bb30e8ae26980327cf0913d74327dc2c344eaad02c194604672f16b74d"
    )
    assert ReportingSourceSliceRequestV1.model_validate_json(request_json).model_dump_json() == (
        request_json
    )
    assert "grain" not in request.coverage.model_dump()
    assert "media_buy_ids" not in request.coverage.model_dump()
    _, manifest, _ = redacted_completed_result(request)
    assert manifest.content_fingerprint == (
        "sha256:178e0438af4767776bd58316b868c111a9f4df806f506c951ffae49e5410e45f"
    )
    assert "grain" not in manifest.coverage.model_dump()
    assert "media_buy_ids" not in manifest.coverage.model_dump()
    manifest_bytes = encode_source_batch_manifest_v1(manifest)
    assert (
        parse_verified_source_batch_manifest_v1(
            source_batch_manifest_reference_v1("manifest-buy-v1", manifest_bytes), manifest_bytes
        )
        == manifest
    )


def test_package_selector_and_pair_are_bound_on_the_v1_wire() -> None:
    request = _package_request()
    assert request.contract_version == "1.0"
    assert request.media_buy_ids == ("buy-1",)
    assert request.package_ids == ("package-1",)
    assert request.coverage.grain == "package_item"
    assert request.coverage.constituents[0].media_buy_id == "buy-1"


def test_complete_empty_package_scope_has_distinct_frozen_selectors() -> None:
    first = _empty_package_request("buy-1")
    second = _empty_package_request("buy-2")
    assert first.media_buy_ids == ("buy-1",)
    assert second.media_buy_ids == ("buy-2",)
    assert first.coverage.denominator_fingerprint != second.coverage.denominator_fingerprint


async def test_complete_empty_package_scope_is_full_without_explicit_zero() -> None:
    request = _empty_package_request()
    result, manifest, reader = redacted_completed_result(
        request, page_bodies=[""], row_counts=[0], explicit_zero=False
    )
    assert manifest.manifest_version == "1.0"
    assert manifest.coverage.grain == "package_item"
    assert manifest.coverage.media_buy_ids == ("buy-1",)
    assert manifest.coverage.status == "full"
    assert not manifest.metric_availability
    assert not manifest.explicit_zero
    assert (
        await validate_reporting_source_execution(
            capabilities=redacted_capabilities(),
            request=request,
            result=result,
            object_reader=reader,
        )
        == manifest
    )


def test_complete_empty_package_scope_requires_current_watermark() -> None:
    request = _empty_package_request()
    _, manifest, _ = redacted_completed_result(
        request, page_bodies=[""], row_counts=[0], explicit_zero=False
    )
    payload = manifest.model_dump(mode="json")
    payload["data_through"] = request.period.start.isoformat()
    payload["content_fingerprint"] = publication_content_fingerprint_v1(payload)
    with pytest.raises(ValidationError, match="current source cutoff"):
        SourceBatchManifestV1.model_validate(payload)


def test_missing_wire_scope_fields_are_rejected() -> None:
    for field in ("grain", "media_buy_ids"):
        request_payload = _package_request().model_dump(mode="json")
        request_payload["coverage"].pop(field)
        with pytest.raises(ValidationError):
            ReportingSourceSliceRequestV1.model_validate(request_payload)
        _, manifest, _ = redacted_completed_result(_package_request())
        manifest_payload = manifest.model_dump(mode="json")
        manifest_payload["coverage"].pop(field)
        with pytest.raises(ValidationError):
            SourceBatchManifestV1.model_validate(manifest_payload)


async def test_empty_package_scope_still_requires_package_capability() -> None:
    request = _empty_package_request()
    result, _, reader = redacted_completed_result(
        request, page_bodies=[""], row_counts=[0], explicit_zero=False
    )
    capabilities_payload = redacted_capabilities().model_dump(mode="json")
    capabilities_payload["offerings"][0]["constituent_kinds"] = ["media_buy"]
    capabilities_payload["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(
        capabilities_payload
    )
    capabilities = ReportingSourceCapabilitiesV1.model_validate(capabilities_payload)
    with pytest.raises(ReportingSourceConformanceError, match="package_item support"):
        await validate_reporting_source_execution(
            capabilities=capabilities,
            request=request,
            result=result,
            object_reader=reader,
        )


def test_package_denominator_rejects_duplicate_pairs_and_unselected_buy() -> None:
    package = _package_request().coverage.constituents[0]
    duplicate = package.model_copy(update={"constituent_id": "another-constituent"})
    for constituents, selector in (([package, duplicate], ("buy-1",)), ([package], ("buy-2",))):
        with pytest.raises(ValidationError):
            ReportingSourceCoverageRequestV1(
                grain="package_item",
                media_buy_ids=selector,
                constituents=constituents,
                denominator_fingerprint=coverage_denominator_fingerprint_v1(
                    constituents, grain="package_item", media_buy_ids=selector
                ),
            )


def test_one_package_id_can_be_used_by_two_selected_buys() -> None:
    first = _package_request().coverage.constituents[0]
    second = first.model_copy(
        update={"constituent_id": "second-constituent", "media_buy_id": "buy-2"}
    )
    coverage = ReportingSourceCoverageRequestV1(
        grain="package_item",
        media_buy_ids=("buy-1", "buy-2"),
        constituents=[first, second],
        denominator_fingerprint=coverage_denominator_fingerprint_v1(
            [first, second], grain="package_item", media_buy_ids=("buy-1", "buy-2")
        ),
    )
    assert [(item.media_buy_id, item.package_id) for item in coverage.constituents] == [
        ("buy-1", "package-1"),
        ("buy-2", "package-1"),
    ]


def test_manifest_must_echo_selector_and_pairs_exactly() -> None:
    request = _package_request()
    _, manifest, _ = redacted_completed_result(request)
    payload = manifest.model_dump(mode="json")
    payload["coverage"]["media_buy_ids"] = ["buy-1", "buy-2"]
    payload["coverage"]["denominator_fingerprint"] = coverage_denominator_fingerprint_v1(
        request.coverage.constituents,
        grain="package_item",
        media_buy_ids=("buy-1", "buy-2"),
    )
    payload["content_fingerprint"] = publication_content_fingerprint_v1(payload)
    widened = SourceBatchManifestV1.model_validate(payload)
    with pytest.raises(ValueError, match="manifest coverage differs"):
        validate_reporting_source_rows_v1(request, widened, [])


def test_explicit_zero_package_pair_rejects_a_row() -> None:
    request = _package_request()
    _, manifest, _ = redacted_completed_result(request, page_bodies=[""], row_counts=[0])
    with pytest.raises(ValueError, match="contradicts explicit zero"):
        validate_reporting_source_rows_v1(
            request,
            manifest,
            [{"media_buy_id": "buy-1", "package_id": "package-1", "impressions": 0}],
        )


def test_package_request_retains_the_1000_cell_limit() -> None:
    first = _package_request().coverage.constituents[0]
    for pair_count, allowed in ((500, True), (501, False)):
        constituents = [
            first.model_copy(
                update={"constituent_id": f"item-{index}", "package_id": f"package-{index}"}
            )
            for index in range(pair_count)
        ]
        payload = _package_request().model_dump(mode="json")
        payload["coverage"] = ReportingSourceCoverageRequestV1(
            grain="package_item",
            media_buy_ids=("buy-1",),
            constituents=constituents,
            denominator_fingerprint=coverage_denominator_fingerprint_v1(
                constituents, grain="package_item", media_buy_ids=("buy-1",)
            ),
        ).model_dump(mode="json")
        if allowed:
            ReportingSourceSliceRequestV1.model_validate(payload)
        else:
            with pytest.raises(ValidationError, match="at most 1000 constituent-metric cells"):
                ReportingSourceSliceRequestV1.model_validate(payload)


@pytest.mark.parametrize("stage", ["staged", "final"])
def test_package_rows_reject_outside_pairs_and_duplicate_requested_grain(stage: str) -> None:
    request = _package_request()
    rows = [
        {"media_buy_id": "buy-1", "package_id": "package-1", "impressions": 4},
        {"media_buy_id": "buy-1", "package_id": "package-1", "impressions": 5},
    ]
    manifest = redacted_completed_result(request)[1] if stage == "final" else None

    def validate(values: list[dict[str, str | int]]) -> None:
        if manifest is None:
            validate_reporting_source_staged_rows_v1(request, values)
        else:
            validate_reporting_source_rows_v1(request, manifest, values)

    with pytest.raises(ValueError, match="duplicate row"):
        validate(rows)
    for row in (
        {"media_buy_id": "buy-2", "package_id": "package-1", "impressions": 0},
        {"media_buy_id": "buy-1", "package_id": "package-2", "impressions": 0},
        {"media_buy_id": "buy-1", "impressions": 0},
    ):
        with pytest.raises(ValueError, match="outside the requested coverage"):
            validate([row])


@pytest.mark.parametrize("stage", ["staged", "final"])
def test_distinct_requested_campaign_values_remain_valid(stage: str) -> None:
    request = _package_request(campaign_dimension=True)
    rows = [
        {"media_buy_id": "buy-1", "package_id": "package-1", "campaign_id": "a"},
        {"media_buy_id": "buy-1", "package_id": "package-1", "campaign_id": "b"},
    ]
    if stage == "staged":
        validate_reporting_source_staged_rows_v1(request, rows)
    else:
        from adcp.reporting.fixtures import redacted_completed_result

        _, manifest, _ = redacted_completed_result(request)
        validate_reporting_source_rows_v1(request, manifest, rows)
