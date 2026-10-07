"""The 7.x legacy catalog must preserve the 3.1 declaration branches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import RootModel, ValidationError

from adcp.types.generated_poc.core.format import Format as GeneratedFormat
from adcp.types.generated_poc.core.product_format_declaration import ProductFormatDeclaration
from adcp.types.generated_poc.creative.list_creative_formats_response import (
    ListCreativeFormatsResponseCreativeAgent as CreativeCatalog,
)
from adcp.types.generated_poc.formats.canonical.image import CanonicalFormatImage
from adcp.types.legacy import LegacyFormat, LegacyListCreativeFormatsResponse

_SCHEMA = json.loads(Path("schemas/cache/3.1/core/product-format-declaration.json").read_text())
_KINDS = [branch["properties"]["format_kind"]["const"] for branch in _SCHEMA["oneOf"]]


def _format_payload():
    return {
        "format_id": {
            "agent_url": "https://creative.example.com",
            "id": "display_300x250_image_2x",
        },
        "name": "Medium Rectangle - Image (2x)",
        "canonical_parameters": {
            "format_kind": "image",
            "params": {"width": 300, "height": 250, "pixel_ratios": [2]},
        },
    }


def test_generated_declaration_exposes_all_thirteen_schema_branches():
    assert issubclass(ProductFormatDeclaration, RootModel)
    branches = get_args(ProductFormatDeclaration.model_fields["root"].annotation)
    assert len(branches) == len(_KINDS) == 13
    actual_kinds = {
        get_args(branch.model_fields["format_kind"].annotation)[0] for branch in branches
    }
    assert actual_kinds == set(_KINDS)
    assert ProductFormatDeclaration.model_fields["root"].discriminator == "format_kind"
    source_by_kind = {arm["properties"]["format_kind"]["const"]: arm for arm in _SCHEMA["oneOf"]}
    for branch in branches:
        assert set(branch.model_fields) >= set(_SCHEMA["properties"]) | {"format_kind", "params"}
        kind = get_args(branch.model_fields["format_kind"].annotation)[0]
        source_params = source_by_kind[kind]["properties"]["params"]
        params_type = branch.model_fields["params"].annotation
        if "$ref" in source_params:
            expected_module = Path(source_params["$ref"]).stem.replace("-", "_")
            assert params_type.__module__ == (
                "adcp.types.generated_poc.formats.canonical." + expected_module
            )
        else:
            assert get_args(params_type) == (str, Any)


@pytest.mark.parametrize("kind", _KINDS)
def test_each_tagged_branch_survives_roundtrip(kind):
    payload = {"format_kind": kind, "params": {}}
    if kind == "custom":
        payload.update(
            canonical_formats_only=True,
            format_shape="multi_placement_takeover",
            format_schema={
                "uri": "https://formats.example.com/custom.json",
                "digest": "sha256:" + "a" * 64,
            },
        )
    parsed = ProductFormatDeclaration.model_validate(payload)
    assert parsed.root.format_kind == kind
    assert parsed.format_kind == kind  # Existing 7.x RootModel attribute proxy.
    assert parsed.model_dump(mode="json", exclude_unset=True) == payload


@pytest.mark.parametrize("model", [GeneratedFormat, LegacyFormat])
def test_legacy_format_keeps_typed_retina_image_parameters(model):
    payload = _format_payload()
    parsed = model.model_validate(payload)
    assert isinstance(parsed.canonical_parameters.root.params, CanonicalFormatImage)
    assert parsed.canonical_parameters.root.params.pixel_ratios == [2]
    emitted = parsed.model_dump(mode="json", exclude_none=True)
    assert emitted["canonical_parameters"]["format_kind"] == "image"
    assert emitted["canonical_parameters"]["params"]["width"] == 300
    assert emitted["canonical_parameters"]["params"]["pixel_ratios"] == [2]
    relay = parsed.model_dump(mode="json", exclude_unset=True)
    assert relay["canonical_parameters"] == payload["canonical_parameters"]
    from jsonschema import Draft7Validator, RefResolver

    schema_path = Path("schemas/cache/3.1/core/product-format-declaration.json").resolve()
    validator = Draft7Validator(
        _SCHEMA,
        resolver=RefResolver(base_uri=schema_path.as_uri(), referrer=_SCHEMA),
    )
    validator.validate(relay["canonical_parameters"])


@pytest.mark.parametrize("model", [CreativeCatalog, LegacyListCreativeFormatsResponse])
def test_catalog_relay_preserves_nested_canonical_parameters(model):
    payload = {"formats": [_format_payload()]}
    parsed = model.model_validate(payload)
    emitted = parsed.model_dump(mode="json", exclude_unset=True)
    assert (
        emitted["formats"][0]["canonical_parameters"]
        == payload["formats"][0]["canonical_parameters"]
    )
    assert model.model_validate(emitted).model_dump(mode="json", exclude_unset=True) == emitted


@pytest.mark.parametrize(
    "declaration",
    [
        {"params": {}},
        {"format_kind": "image"},
        {"format_kind": "unknown", "params": {}},
        {"format_kind": "image", "params": {"width": 0}},
        {"format_kind": "image", "params": {"width": "not-a-number"}},
    ],
)
def test_nested_declaration_enforces_tags_required_params_and_parameter_constraints(declaration):
    payload = _format_payload()
    payload["canonical_parameters"] = declaration
    with pytest.raises(ValidationError):
        LegacyListCreativeFormatsResponse.model_validate({"formats": [payload]})


def test_catalog_without_canonical_parameters_remains_valid():
    payload = _format_payload()
    del payload["canonical_parameters"]
    assert LegacyFormat.model_validate(payload).canonical_parameters is None
