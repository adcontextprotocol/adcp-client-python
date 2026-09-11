"""Regression tests for open-enum canonical format kinds (issue #1140)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from adcp.types import (
    CanonicalFormatKind,
    Creative,
    CreativeAsset,
    CreativeManifest,
    DeliveryCreative,
    Format,
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
    now = datetime.now(UTC)
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
    [_creative_asset, _creative, _delivery_creative, _creative_manifest],
)
def test_unknown_format_kind_is_preserved(factory) -> None:
    model = factory(FUTURE_FORMAT_KIND)

    assert model.format_kind == FUTURE_FORMAT_KIND
    assert type(model.format_kind) is str
    assert model.model_dump(mode="json")["format_kind"] == FUTURE_FORMAT_KIND


@pytest.mark.parametrize(
    "factory",
    [_creative_asset, _creative, _delivery_creative, _creative_manifest],
)
def test_known_format_kind_still_coerces_to_enum(factory) -> None:
    model = factory("image")

    assert model.format_kind is CanonicalFormatKind.image
    assert model.model_dump(mode="json")["format_kind"] == "image"


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
