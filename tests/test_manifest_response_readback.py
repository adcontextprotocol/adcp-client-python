"""Strict manifest inputs and tolerant buyer response views (issue #1241)."""

from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

import adcp.types as public_types
from adcp import ADCPClient
from adcp.protocols.mcp import MCPAdapter
from adcp.types import (
    CanonicalFormatKind,
    ContextMatchResponse,
    CreativeManifest,
    LegacyBuildCreativeResponse,
    LegacyBuildCreativeResponse1,
    LegacyBuildCreativeResponse3,
    LegacyBuildCreativeResponse4,
    LegacyPreviewCreativeResponse,
    LegacyPreviewCreativeResponse3,
    ListCreativesResponse,
    McpWebhookPayload,
    TmpOffer,
)
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus
from adcp.types.generated_poc.core.async_response_data import AdcpAsyncResponseData
from adcp.types.generated_poc.core.creative_manifest import (
    CreativeManifest as InputManifest,
)
from adcp.types.generated_poc.core.version_envelope import AdcpVersionEnvelope
from adcp.types.generated_poc.media_buy.build_creative_response import (
    Creative as InputBuildCreative,
)
from adcp.types.generated_poc.trusted_match.provider_context_match_response import (
    ContextMatchResponseProviderRouter,
)

FUTURE_KIND = "future_canonical_format"
TASK_CASES = ("preview", "build-single", "build-multi", "build-variants")
CASES = (*TASK_CASES, "context-match", "provider-context-match")


def response_case(case, kind=FUTURE_KIND):
    manifest = {
        "format_kind": kind,
        "assets": {},
        "vendor_annotation": {"notes": ["preserved"]},
    }
    if case == "preview":
        return LegacyPreviewCreativeResponse3, {
            "response_type": "variant",
            "variant_id": "variant-1",
            "previews": [
                {
                    "preview_id": "preview-1",
                    "renders": [
                        {
                            "render_id": "render-1",
                            "output_format": "html",
                            "preview_html": "<p>Preview</p>",
                            "role": "primary",
                        }
                    ],
                }
            ],
            "manifest": manifest,
        }
    if case == "build-single":
        return LegacyBuildCreativeResponse1, {"creative_manifest": manifest}
    if case == "build-multi":
        return LegacyBuildCreativeResponse3, {"creative_manifests": [manifest]}
    if case in ("context-match", "provider-context-match"):
        model = (
            ContextMatchResponse if case == "context-match" else ContextMatchResponseProviderRouter
        )
        return model, {
            "request_id": "request-1",
            "offers": [{"package_id": "package-1", "creative_manifest": manifest}],
        }
    return LegacyBuildCreativeResponse4, {
        "creatives": [
            {
                "build_creative_id": "creative-1",
                "variants": [
                    {
                        "build_variant_id": "variant-1",
                        "creative_manifest": manifest,
                        "rank": 1,
                    }
                ],
            }
        ],
    }


def read_manifest(response, case):
    if case == "preview":
        return response.manifest
    if case == "build-single":
        return response.creative_manifest
    if case == "build-multi":
        return response.creative_manifests[0]
    if case in ("context-match", "provider-context-match"):
        return response.offers[0].creative_manifest
    return response.creatives[0].variants[0].creative_manifest


@pytest.mark.parametrize("kind", ["image", FUTURE_KIND, None])
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("json_input", [False, True], ids=["python", "json"])
def test_manifest_response_round_trip(case, kind, json_input):
    model, payload = response_case(case, kind)
    response = (
        model.model_validate_json(json.dumps(payload))
        if json_input
        else model.model_validate(payload)
    )
    manifest = read_manifest(response, case)
    if kind == "image":
        assert manifest.format_kind is CanonicalFormatKind.image
    else:
        assert manifest.format_kind == kind
    assert manifest.model_dump()["vendor_annotation"] == {"notes": ["preserved"]}
    assert model.model_validate_json(response.model_dump_json()).model_dump(mode="json") == (
        response.model_dump(mode="json")
    )
    assert type(manifest).__name__ not in public_types.__all__
    assert not hasattr(public_types, type(manifest).__name__)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("mcp_content", [False, True], ids=["dict", "mcp-text"])
def test_adapter_keeps_manifest_response(case, mcp_content):
    model, payload = response_case(case)
    union = (
        LegacyPreviewCreativeResponse
        if case == "preview"
        else LegacyBuildCreativeResponse if case.startswith("build-") else model
    )
    data = [{"type": "text", "text": json.dumps(payload)}] if mcp_content else payload
    adapter = MCPAdapter(
        AgentConfig(
            id="creative-agent",
            agent_uri="https://seller.example/mcp",
            protocol=Protocol.MCP,
        )
    )
    result = adapter._parse_response(TaskResult(status=TaskStatus.COMPLETED, data=data), union)
    assert result.success, result.error
    assert result.status is TaskStatus.COMPLETED
    assert read_manifest(result.data, case).format_kind == FUTURE_KIND
    reparsed = TypeAdapter(union).validate_json(result.data.model_dump_json())
    assert read_manifest(reparsed, case).format_kind == FUTURE_KIND


def completed_task(case):
    _, result = response_case(case)
    return {
        "idempotency_key": "whk_manifest_readback_1",
        "operation_id": "operation-1",
        "task_id": "task-1",
        "task_type": "preview_creative" if case == "preview" else "build_creative",
        "status": "completed",
        "timestamp": "2026-09-01T00:00:00Z",
        "result": result,
    }


@pytest.mark.parametrize("case", TASK_CASES)
def test_async_response_union_and_webhook_round_trip(case):
    model, payload = response_case(case)
    adapter = TypeAdapter(AdcpAsyncResponseData)
    result = adapter.validate_json(json.dumps(payload))
    assert isinstance(result, model)
    assert read_manifest(result, case).format_kind == FUTURE_KIND
    reparsed = adapter.validate_json(adapter.dump_json(result))
    assert isinstance(reparsed, model)
    assert read_manifest(reparsed, case).format_kind == FUTURE_KIND

    webhook = McpWebhookPayload.model_validate_json(json.dumps(completed_task(case)))
    assert isinstance(webhook.result, model)
    assert read_manifest(webhook.result, case).format_kind == FUTURE_KIND
    reparsed_webhook = McpWebhookPayload.model_validate_json(webhook.model_dump_json())
    assert isinstance(reparsed_webhook.result, model)
    assert read_manifest(reparsed_webhook.result, case).format_kind == FUTURE_KIND


@pytest.mark.asyncio
@pytest.mark.parametrize("case", TASK_CASES)
async def test_public_completed_task_parser_keeps_manifest_readback(case):
    model, _ = response_case(case)
    payload = completed_task(case)
    client = ADCPClient(
        AgentConfig(
            id="creative-agent", agent_uri="https://seller.example/mcp", protocol=Protocol.MCP
        ),
        allow_unauthenticated_webhooks=True,
    )
    with pytest.deprecated_call(match="handle_webhook_legacy"):
        result = await client.handle_webhook_legacy(
            payload,
            task_type=payload["task_type"],
            operation_id=payload["operation_id"],
        )
    assert result.success, result.error
    assert result.status is TaskStatus.COMPLETED
    # A successful untyped fallback would silently hide strict-union rejection.
    assert isinstance(result.data, model)
    assert read_manifest(result.data, case).format_kind == FUTURE_KIND
    reparsed = TaskResult[AdcpAsyncResponseData].model_validate_json(result.model_dump_json())
    assert isinstance(reparsed.data, model)
    assert read_manifest(reparsed.data, case).format_kind == FUTURE_KIND


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("input_model", [CreativeManifest, InputManifest])
@pytest.mark.parametrize("reuse_instance", [False, True], ids=["dump", "instance"])
def test_manifest_readback_cannot_bypass_input_validation(case, input_model, reuse_instance):
    model, payload = response_case(case)
    manifest = read_manifest(model.model_validate(payload), case)
    value = manifest if reuse_instance else manifest.model_dump(mode="json")
    with pytest.raises(ValidationError):
        input_model.model_validate(value)
    assert not isinstance(manifest, input_model)


@pytest.mark.parametrize("case", ["build-variants", "context-match", "provider-context-match"])
def test_nested_readback_accepts_known_source_models(case):
    model, payload = response_case(case, "image")
    collection = "creatives" if case == "build-variants" else "offers"
    source = InputBuildCreative if case == "build-variants" else TmpOffer
    payload[collection][0] = source.model_validate(payload[collection][0])
    response = model.model_validate(payload)
    assert read_manifest(response, case).format_kind is CanonicalFormatKind.image
    node = getattr(response, collection)[0]
    assert type(node).__name__ not in public_types.__all__
    assert not hasattr(public_types, type(node).__name__)


def test_nested_build_readback_preserves_envelopes_and_constraints():
    model, payload = response_case("build-variants")
    response = model.model_validate(payload)
    creative = response.creatives[0]
    variant = creative.variants[0]
    for node in (creative, variant):
        assert isinstance(node, AdcpVersionEnvelope)
        assert type(node).__name__ not in public_types.__all__
        assert not hasattr(public_types, type(node).__name__)

    payload["creatives"][0]["variants"][0]["rank"] = 0
    with pytest.raises(ValidationError) as invalid_rank:
        model.model_validate(payload)
    assert invalid_rank.value.errors()[0]["type"] == "greater_than_equal"
    payload["creatives"][0]["variants"] = []
    with pytest.raises(ValidationError) as empty_variants:
        model.model_validate(payload)
    assert empty_variants.value.errors()[0]["type"] == "too_short"
    payload["creatives"] = []
    with pytest.raises(ValidationError) as empty_creatives:
        model.model_validate(payload)
    assert empty_creatives.value.errors()[0]["type"] == "too_short"


def test_build_readback_preserves_empty_and_error_branch_rules():
    with pytest.raises(ValidationError) as empty_manifests:
        LegacyBuildCreativeResponse3.model_validate({"creative_manifests": []})
    assert empty_manifests.value.errors()[0]["type"] == "too_short"

    response = LegacyBuildCreativeResponse4.model_validate(
        {
            "creatives": [
                {
                    "build_creative_id": "creative-1",
                    "errors": [
                        {"code": "BUILD_FAILED", "message": "Unable to build this creative"}
                    ],
                }
            ],
        }
    )
    assert response.creatives[0].variants is None
    assert response.creatives[0].errors[0].code == "BUILD_FAILED"


def test_direct_listed_creative_kind_stays_strict():
    payload = {
        "query_summary": {"total_matching": 1, "returned": 1},
        "pagination": {"has_more": False},
        "creatives": [
            {
                "creative_id": "creative-1",
                "name": "Image",
                "format_kind": "image",
                "status": "approved",
                "created_date": "2026-09-01T00:00:00Z",
                "updated_date": "2026-09-01T00:00:00Z",
            }
        ],
    }
    assert ListCreativesResponse.model_validate(payload).creatives[0].format_kind is (
        CanonicalFormatKind.image
    )
    payload["creatives"][0]["format_kind"] = FUTURE_KIND
    with pytest.raises(ValidationError) as error:
        ListCreativesResponse.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("creatives", 0, "format_kind")
    assert error.value.errors()[0]["type"] == "enum"
