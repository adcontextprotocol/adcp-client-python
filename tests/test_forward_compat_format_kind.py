"""Closed creative format kinds and tolerant delivery readback (issues #1241/#1140)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from adcp.types import (
    CanonicalFormatKind,
    Creative,
    CreativeAsset,
    CreativeManifest,
    CreativeVariant,
    DeliveryCreative,
    Format,
    GetCreativeDeliveryResponse,
    SyncCreativesRequest,
)
from adcp.types.aliases import DeliveryCreative as AliasDeliveryCreative
from adcp.types.creative import Creative as PartialCreative
from adcp.types.creative import CreativeAsset as PartialCreativeAsset
from adcp.types.creative import CreativeManifest as PartialCreativeManifest
from adcp.types.generated_poc.core.creative_manifest import (
    CreativeManifest as GeneratedCreativeManifest,
)
from adcp.types.generated_poc.creative.get_creative_delivery_response import (
    GetCreativeDeliveryResponse as GeneratedGetCreativeDeliveryResponse,
)

FUTURE_FORMAT_KIND = "future_canonical_format"


def _creative_asset(format_kind: str) -> CreativeAsset:
    return CreativeAsset(
        creative_id="creative-1",
        name="Creative",
        format_kind=format_kind,
        assets={},
    )


def _creative(format_kind: str) -> Creative:
    now = datetime.now(timezone.utc)
    return Creative(
        creative_id="creative-1",
        name="Creative",
        format_kind=format_kind,
        status="approved",
        created_date=now,
        updated_date=now,
    )


def _delivery_creative(format_kind: str) -> DeliveryCreative:
    return DeliveryCreative(
        creative_id="creative-1",
        format_kind=format_kind,
        variants=[],
    )


def _creative_manifest(format_kind: str) -> CreativeManifest:
    return CreativeManifest(format_kind=format_kind, assets={})


@pytest.mark.parametrize(
    "factory",
    [_creative_asset, _creative, _creative_manifest],
)
@pytest.mark.parametrize("value", [FUTURE_FORMAT_KIND, "totally_bogus", "IMAGE", ""])
def test_unknown_format_kind_is_rejected(factory, value) -> None:
    with pytest.raises(ValidationError) as error:
        factory(value)

    assert [(detail["loc"], detail["type"]) for detail in error.value.errors()] == [
        (("format_kind",), "enum")
    ]


@pytest.mark.parametrize("factory", [_creative_asset, _creative, _creative_manifest])
@pytest.mark.parametrize("json_input", [False, True], ids=["python", "json"])
def test_raw_creative_validation_rejects_unknown_format_kind(factory, json_input) -> None:
    model = factory("image")
    payload = model.model_dump(mode="json", exclude_unset=True)
    payload["format_kind"] = FUTURE_FORMAT_KIND

    with pytest.raises(ValidationError) as error:
        if json_input:
            type(model).model_validate_json(json.dumps(payload))
        else:
            type(model).model_validate(payload)

    assert error.value.errors()[0]["loc"] == ("format_kind",)


@pytest.mark.parametrize("factory", [_creative_asset, _creative, _creative_manifest])
def test_creative_validation_schema_rejects_unknown_format_kind(factory) -> None:
    model = factory("image")
    validator = Draft202012Validator(type(model).model_json_schema())
    payload = model.model_dump(mode="json", exclude_unset=True)
    validator.validate(payload)

    payload["format_kind"] = FUTURE_FORMAT_KIND
    errors = list(validator.iter_errors(payload))
    assert errors
    assert all(list(error.path) == ["format_kind"] for error in errors)


@pytest.mark.parametrize(
    ("public", "partial"),
    [
        (CreativeAsset, PartialCreativeAsset),
        (Creative, PartialCreative),
        (CreativeManifest, PartialCreativeManifest),
    ],
)
def test_partial_imports_use_the_same_creative_models(public, partial) -> None:
    assert public is partial


@pytest.mark.parametrize("factory", [_creative_asset, _creative])
def test_creative_format_kind_remains_required_and_non_nullable(factory) -> None:
    model = factory("image")
    payload = model.model_dump(mode="json", exclude_unset=True)
    del payload["format_kind"]
    with pytest.raises(ValidationError) as missing:
        type(model).model_validate(payload)
    assert missing.value.errors()[0]["loc"] == ("format_kind",)
    assert missing.value.errors()[0]["type"] == "missing"

    payload["format_kind"] = None
    with pytest.raises(ValidationError) as null:
        type(model).model_validate(payload)
    assert null.value.errors()[0]["loc"] == ("format_kind",)


def test_manifest_format_kind_keeps_its_optional_default() -> None:
    omitted = CreativeManifest(assets={})
    explicit = CreativeManifest(assets={}, format_kind=None)
    assert omitted.format_kind is None
    assert explicit.format_kind is None
    assert "format_kind" not in omitted.model_dump(exclude_unset=True)
    assert explicit.model_dump(exclude_unset=True, exclude_none=False)["format_kind"] is None


@pytest.mark.parametrize("json_input", [False, True], ids=["python", "json"])
def test_sync_request_rejects_unknown_creative_format_kind(json_input) -> None:
    payload = {
        "account": {"account_id": "account-1"},
        "idempotency_key": "creative-sync-idempotency-1",
        "creatives": [_creative_asset("image").model_dump(mode="json", exclude_unset=True)],
    }
    SyncCreativesRequest.model_validate(payload)
    payload["creatives"][0]["format_kind"] = FUTURE_FORMAT_KIND

    with pytest.raises(ValidationError) as error:
        if json_input:
            SyncCreativesRequest.model_validate_json(json.dumps(payload))
        else:
            SyncCreativesRequest.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("creatives", 0, "format_kind")


def test_public_variant_rejects_unknown_manifest_format_kind() -> None:
    variant = {"variant_id": "variant-1", "manifest": {"assets": {}, "format_kind": "image"}}
    CreativeVariant.model_validate(variant)
    variant["manifest"]["format_kind"] = FUTURE_FORMAT_KIND

    with pytest.raises(ValidationError) as error:
        CreativeVariant.model_validate(variant)
    assert error.value.errors()[0]["loc"][-2:] == ("manifest", "format_kind")


@pytest.mark.parametrize(
    ("response_type", "strict_manifest_type"),
    [
        (GetCreativeDeliveryResponse, CreativeManifest),
        (GeneratedGetCreativeDeliveryResponse, GeneratedCreativeManifest),
    ],
    ids=["canonical", "generated"],
)
def test_unknown_nested_manifest_kind_round_trips_in_delivery_readback(
    response_type, strict_manifest_type
) -> None:
    payload = {
        "currency": "USD",
        "reporting_period": {
            "start": "2026-09-01T00:00:00Z",
            "end": "2026-09-02T00:00:00Z",
        },
        "creatives": [
            {
                "creative_id": "creative-1",
                "format_kind": FUTURE_FORMAT_KIND,
                "variants": [
                    {
                        "variant_id": "variant-1",
                        "manifest": {"assets": {}, "format_kind": FUTURE_FORMAT_KIND},
                    }
                ],
            }
        ],
    }
    delivery = response_type.model_validate(payload)
    manifest = delivery.creatives[0].variants[0].manifest
    assert manifest is not None
    assert type(manifest) is not strict_manifest_type
    assert type(delivery.creatives[0].variants[0]) is not CreativeVariant
    assert manifest.format_kind == FUTURE_FORMAT_KIND

    encoded = delivery.model_dump_json()
    assert response_type.model_validate_json(encoded).model_dump(mode="json") == (
        delivery.model_dump(mode="json")
    )

    with pytest.raises(ValidationError) as error:
        strict_manifest_type.model_validate(payload["creatives"][0]["variants"][0]["manifest"])
    assert error.value.errors()[0]["loc"] == ("format_kind",)


@pytest.mark.parametrize(
    "factory",
    [_creative_asset, _creative, _delivery_creative, _creative_manifest],
)
@pytest.mark.parametrize("kind", list(CanonicalFormatKind))
def test_known_format_kind_still_coerces_to_enum(factory, kind) -> None:
    model = factory(kind.value)

    assert model.format_kind is kind
    assert model.model_dump(mode="json")["format_kind"] == kind.value
    assert type(model).model_validate_json(model.model_dump_json()).format_kind is kind


def test_delivery_creative_alias_is_open_and_keeps_its_identity() -> None:
    assert AliasDeliveryCreative is DeliveryCreative

    model = AliasDeliveryCreative(
        creative_id="creative-1",
        format_kind=FUTURE_FORMAT_KIND,
        variants=[],
    )
    assert model.format_kind == FUTURE_FORMAT_KIND


def _format_schema() -> dict[str, str]:
    return {
        "uri": "https://example.com/custom-format.json",
        "digest": f"sha256:{'0' * 64}",
    }


def test_custom_format_requires_shape() -> None:
    with pytest.raises(ValidationError, match="custom formats require format_shape"):
        Format(format_kind="custom", params={}, format_schema=_format_schema())


def test_custom_format_requires_schema() -> None:
    with pytest.raises(ValidationError, match="custom formats require format_schema"):
        Format(format_kind="custom", params={}, format_shape="new_shape")


def test_custom_format_accepts_shape_and_schema() -> None:
    model = Format(
        format_kind="custom",
        params={},
        format_shape="new_shape",
        format_schema=_format_schema(),
    )

    assert model.format_kind is CanonicalFormatKind.custom
    assert model.format_shape == "new_shape"
    assert model.format_schema is not None


@pytest.mark.parametrize("field", ["format_shape", "format_schema"])
def test_non_custom_format_rejects_custom_fields(field: str) -> None:
    value = "new_shape" if field == "format_shape" else _format_schema()

    with pytest.raises(
        ValidationError,
        match="format_shape and format_schema are only valid for custom formats",
    ):
        Format(format_kind="image", params={}, **{field: value})
