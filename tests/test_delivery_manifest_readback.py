"""Delivery manifest tolerance must not weaken creative input validation (#1241)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import adcp.types as public_types
from adcp.protocols.mcp import MCPAdapter
from adcp.types import (
    CanonicalFormatKind,
    CreativeManifest,
    CreativeVariant,
    GetCreativeDeliveryResponse,
    ImageContent,
    LegacyGetCreativeDeliveryResponse,
)
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus
from adcp.types.generated_poc.core.creative_manifest import (
    CreativeManifest as WireCreativeManifest,
)
from adcp.types.generated_poc.core.creative_variant import (
    CreativeVariant as WireCreativeVariant,
)

FUTURE_KIND = "future_canonical_format"
RESPONSE_MODELS = (GetCreativeDeliveryResponse, LegacyGetCreativeDeliveryResponse)


def delivery_payload():
    return {
        "currency": "USD",
        "reporting_period": {
            "start": "2026-09-01T00:00:00Z",
            "end": "2026-09-02T00:00:00Z",
        },
        "creatives": [
            {
                "creative_id": "creative-1",
                "format_kind": FUTURE_KIND,
                "variants": [
                    {
                        "variant_id": "variant-known",
                        "manifest": {"assets": {}, "format_kind": "image"},
                        "impressions": 3,
                    },
                    {
                        "variant_id": "variant-future",
                        "manifest": {
                            "assets": {},
                            "format_kind": FUTURE_KIND,
                            "vendor_annotation": {"notes": ["preserved"]},
                        },
                        "locale_variant_id": "fr-FR",
                        "impressions": 7,
                    },
                ],
            }
        ],
    }


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=["canonical", "legacy"])
@pytest.mark.parametrize("json_input", [False, True], ids=["python", "json"])
def test_delivery_response_round_trips_unknown_nested_manifest(model, json_input):
    payload = delivery_payload()
    response = (
        model.model_validate_json(json.dumps(payload))
        if json_input
        else model.model_validate(payload)
    )
    creative = response.creatives[0]
    assert creative.format_kind == FUTURE_KIND
    assert creative.variants[0].manifest.format_kind is CanonicalFormatKind.image
    variant = creative.variants[1]
    assert variant.manifest.format_kind == FUTURE_KIND
    assert variant.impressions == 7
    assert variant.locale_variant_id == "fr-FR"

    wire = json.loads(response.model_dump_json())
    manifest = wire["creatives"][0]["variants"][1]["manifest"]
    assert manifest["format_kind"] == FUTURE_KIND
    assert manifest["vendor_annotation"] == {"notes": ["preserved"]}
    reparsed = model.model_validate(wire)
    assert reparsed.creatives[0].variants[1].manifest.format_kind == FUTURE_KIND


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=["canonical", "legacy"])
@pytest.mark.parametrize("input_model", [CreativeManifest, WireCreativeManifest])
@pytest.mark.parametrize("reuse_instance", [False, True], ids=["dump", "instance"])
def test_delivery_manifest_cannot_bypass_strict_input(model, input_model, reuse_instance):
    response = model.model_validate(delivery_payload())
    manifest = response.creatives[0].variants[1].manifest
    # A tolerant subclass could bypass Pydantic's default instance revalidation.
    value = manifest if reuse_instance else manifest.model_dump(mode="json")
    with pytest.raises(ValidationError):
        input_model.model_validate(value)
    assert not isinstance(manifest, input_model)


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=["canonical", "legacy"])
@pytest.mark.parametrize("input_model", [CreativeVariant, WireCreativeVariant])
def test_delivery_variant_cannot_bypass_strict_input(model, input_model):
    response = model.model_validate(delivery_payload())
    variant = response.creatives[0].variants[1]
    with pytest.raises(ValidationError):
        input_model.model_validate(variant)
    with pytest.raises(ValidationError):
        input_model.model_validate(variant.model_dump(mode="json"))
    assert not isinstance(variant, input_model)


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=["canonical", "legacy"])
def test_delivery_variants_keep_non_kind_constraints(model):
    payload = delivery_payload()
    payload["creatives"][0]["variants"][1]["locale_variant_id"] = "x" * 256
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert any(
        item["loc"][-1] == "locale_variant_id" and item["type"] == "string_too_long"
        for item in error.value.errors()
    )


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=["canonical", "legacy"])
def test_delivery_only_types_are_not_top_level_exports(model):
    response = model.model_validate(delivery_payload())
    variant = response.creatives[0].variants[1]
    for model in (type(variant), type(variant.manifest)):
        assert model.__name__ not in public_types.__all__
        assert not hasattr(public_types, model.__name__)


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=["canonical", "legacy"])
@pytest.mark.parametrize("mcp_content", [False, True], ids=["dict", "mcp-text"])
def test_adapter_keeps_successful_delivery_result(model, mcp_content):
    payload = delivery_payload()
    data = [{"type": "text", "text": json.dumps(payload)}] if mcp_content else payload
    adapter = MCPAdapter(
        AgentConfig(
            id="delivery-agent", agent_uri="https://seller.example/mcp", protocol=Protocol.MCP
        )
    )
    result = adapter._parse_response(TaskResult(status=TaskStatus.COMPLETED, data=data), model)
    assert result.success, result.error
    assert result.status is TaskStatus.COMPLETED
    assert result.data.creatives[0].variants[1].manifest.format_kind == FUTURE_KIND


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=["canonical", "legacy"])
@pytest.mark.parametrize("input_model", [CreativeManifest, WireCreativeManifest])
@pytest.mark.parametrize("as_variant", [False, True], ids=["manifest", "variant"])
def test_delivery_accepts_known_input_models(model, input_model, as_variant):
    manifest = input_model(format_kind="image", assets={})
    variant = {"variant_id": "variant-known", "manifest": manifest, "impressions": 3}
    if as_variant:
        variant_model = CreativeVariant if input_model is CreativeManifest else WireCreativeVariant
        variant = variant_model.model_validate(variant)
    payload = delivery_payload()
    payload["creatives"][0]["variants"][0] = variant
    response = model.model_validate(payload)
    served = response.creatives[0].variants[0]
    assert served.manifest.format_kind is CanonicalFormatKind.image
    assert served.impressions == 3


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=["canonical", "legacy"])
def test_delivery_manifest_normalizes_standalone_assets(model):
    payload = delivery_payload()
    image = ImageContent(url="https://example.com/image.png", width=300, height=250)
    payload["creatives"][0]["variants"][1]["manifest"]["assets"] = {"image_main": image}
    response = model.model_validate(payload)
    assets = response.model_dump(mode="json")["creatives"][0]["variants"][1]["manifest"]["assets"]
    assert assets["image_main"]["url"] == "https://example.com/image.png"


def test_delivery_keeps_legacy_identity_only_on_legacy_surface():
    payload = delivery_payload()
    manifest = payload["creatives"][0]["variants"][1]["manifest"]
    manifest.pop("format_kind")
    manifest["format_id"] = {"agent_url": "https://seller.example", "id": "legacy-banner"}
    legacy = LegacyGetCreativeDeliveryResponse.model_validate(payload)
    wire = legacy.model_dump(mode="json")
    assert wire["creatives"][0]["variants"][1]["manifest"]["format_id"]["id"] == "legacy-banner"
    with pytest.raises(ValidationError, match="legacy creative identity"):
        GetCreativeDeliveryResponse.model_validate(payload)


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=["canonical", "legacy"])
def test_delivery_keeps_optional_manifest_and_kind_defaults(model):
    payload = delivery_payload()
    variants = payload["creatives"][0]["variants"]
    variants[0].pop("manifest")
    variants[1]["manifest"].pop("format_kind")
    response = model.model_validate(payload)
    assert response.creatives[0].variants[0].manifest is None
    assert response.creatives[0].variants[1].manifest.format_kind is None
