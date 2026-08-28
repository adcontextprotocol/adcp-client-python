"""Version-scoped public models and MCP tool-schema advertisement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema.validators import validator_for
from pydantic import Field, TypeAdapter, ValidationError

from adcp.server import (
    ADCPHandler,
    ToolContext,
    adcp_server,
    create_mcp_server,
    create_mcp_tools,
)
from adcp.server.mcp_tools import get_tools_for_handler
from adcp.types.v31 import BuildCreativeSubmittedResponse
from adcp.types.v31 import CreateMediaBuyResponse as CreateMediaBuyResponse31
from adcp.types.v31 import GetBrandIdentityRequest as GetBrandIdentityRequest31
from adcp.types.v31 import ListCreativesRequest as ListCreativesRequest31
from adcp.types.v31 import PackageRequest as PackageRequest31
from adcp.types.v32 import ListCreativesRequest as ListCreativesRequest32
from adcp.types.v32 import PackageRequest as PackageRequest32
from adcp.types.versioned import make_versioned_base
from adcp.validation import get_mcp_schema, get_portable_schema, get_validator

ROOT = Path(__file__).parents[1]


def test_list_creatives_schema_is_version_scoped() -> None:
    schema31 = ListCreativesRequest31.model_json_schema()
    schema32 = ListCreativesRequest32.model_json_schema()

    assert "assignment_projection" not in schema31["properties"]
    assert "assignment_limit" not in schema31["properties"]
    assert "assignment_projection" in schema32["properties"]
    assert "assignment_limit" in schema32["properties"]


def test_package_budget_requirement_is_version_scoped() -> None:
    with pytest.raises(ValidationError, match="budget.*required property"):
        PackageRequest31(product_id="product-1", pricing_option_id="fixed")

    package32 = PackageRequest32(product_id="product-1", pricing_option_id="fixed")
    assert package32.budget is None
    assert package32.model_dump() == {
        "product_id": "product-1",
        "pricing_option_id": "fixed",
        "paused": False,
    }
    assert "budget" in PackageRequest31.model_json_schema()["required"]
    assert "budget" not in PackageRequest32.model_json_schema()["required"]


def test_union_response_fields_are_available_with_optional_attribute_semantics() -> None:
    response = CreateMediaBuyResponse31(
        status="failed",
        errors=[{"code": "INVALID_REQUEST", "message": "bad request"}],
    )
    assert response.errors[0]["code"] == "INVALID_REQUEST"
    assert response.media_buy_id is None


def test_union_success_status_preserves_all_of_intersection() -> None:
    with pytest.raises(ValidationError):
        CreateMediaBuyResponse31(
            status="active",
            media_buy_id="buy-1",
            confirmed_at=None,
            revision=1,
            packages=[],
        )

    response = CreateMediaBuyResponse31(
        status="completed",
        media_buy_id="buy-1",
        confirmed_at=None,
        revision=1,
        packages=[],
    )
    assert response.status == "completed"


def test_generated_stubs_preserve_nested_all_of_requiredness() -> None:
    stub = (ROOT / "src" / "adcp" / "types" / "v31.pyi").read_text()
    metrics_start = stub.index(
        "class _ExternalCoreSignalCoverageForecastPointsItemMetrics(TypedDict"
    )
    metrics_end = stub.index("\n\n", metrics_start)
    metrics = stub[metrics_start:metrics_end]

    assert "coverage_rate: Required[" in metrics


def test_versioned_base_supports_excluded_adopter_fields() -> None:
    base = make_versioned_base("3.1", "ListCreativesRequest")

    class SellerListCreativesRequest(base):
        internal_tenant_id: str = Field(exclude=True)

    request = SellerListCreativesRequest(
        internal_tenant_id="tenant-1",
        include_assignments=True,
    )
    payload = request.model_dump(mode="json")

    assert "include_assignments" in SellerListCreativesRequest.model_fields
    assert "internal_tenant_id" in SellerListCreativesRequest.model_fields
    assert payload["include_assignments"] is True
    assert "internal_tenant_id" not in payload
    assert "internal_tenant_id" not in json.loads(request.model_dump_json())


def test_versioned_base_uses_current_nested_runtime_models() -> None:
    from adcp.types import CreativeFilters

    base = make_versioned_base("3.1", "ListCreativesRequest")
    request = base(filters={"statuses": ["approved"]})

    assert isinstance(request.filters, CreativeFilters)
    assert request.model_dump(mode="json")["filters"] == {"statuses": ["approved"]}


def test_versioned_base_omits_explicit_none_for_optional_non_nullable_fields() -> None:
    base = make_versioned_base("3.1", "ListCreativesRequest")

    request = base(account=None, context=None, filters=None)

    assert request.account is None
    payload = request.model_dump()
    assert payload["include_assignments"] is True
    assert payload["include_snapshot"] is False
    assert {"account", "context", "filters"}.isdisjoint(payload)
    assert request.model_fields_set.isdisjoint({"account", "context", "filters"})


def test_versioned_base_preserves_none_when_schema_admits_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jsonschema.validators import validator_for

    from adcp.types import versioned

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "required_nullable": {"type": ["string", "null"]},
            "optional_any_of": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "optional_one_of": {"oneOf": [{"type": "string"}, {"type": "null"}]},
            "optional_bare_null": {"type": "null"},
            "optional_non_nullable": {"type": "string"},
        },
        "required": ["required_nullable"],
        "additionalProperties": False,
    }
    validator = validator_for(schema)(schema)
    monkeypatch.setattr(versioned, "get_portable_schema", lambda *args, **kwargs: schema)
    monkeypatch.setattr(versioned, "get_validator", lambda *args, **kwargs: validator)
    versioned.make_versioned_base.cache_clear()

    base = versioned.make_versioned_base("test", "SyntheticRequest")
    request = base(
        required_nullable=None,
        optional_any_of=None,
        optional_one_of=None,
        optional_bare_null=None,
        optional_non_nullable=None,
    )

    assert request.model_dump() == {
        "required_nullable": None,
        "optional_any_of": None,
        "optional_one_of": None,
        "optional_bare_null": None,
    }
    versioned.make_versioned_base.cache_clear()


def test_versioned_base_emits_only_the_canonical_pinned_schema() -> None:
    base = make_versioned_base("3.1", "ListCreativesRequest")

    class SellerListCreativesRequest(base):
        internal_tenant_id: str = Field(exclude=True)

    canonical = get_portable_schema("list_creatives", "request", version="3.1")
    assert canonical is not None
    assert SellerListCreativesRequest.model_json_schema() == canonical

    adapter_schema = TypeAdapter(SellerListCreativesRequest).json_schema()
    encoded = json.dumps(adapter_schema)
    assert "internal_tenant_id" not in encoded
    assert "assignment_projection" not in encoded


def test_versioned_base_enforces_version_delta_fields() -> None:
    request31 = make_versioned_base("3.1", "ListCreativesRequest")
    request32 = make_versioned_base("3.2-beta.4", "ListCreativesRequest")

    assert "assignment_projection" not in request31.model_fields
    assert "assignment_limit" not in request31.model_fields
    assert "assignment_projection" in request32.model_fields
    assert "assignment_limit" in request32.model_fields

    with pytest.raises(ValidationError, match="assignment_projection"):
        request31(assignment_projection="all")
    valid32 = request32(
        assignment_projection="matching",
        filters={"indicator_types": ["creative_fatigue"]},
    )
    assert valid32.assignment_projection == "matching"


def test_versioned_base_keeps_31_package_budget_required() -> None:
    package = make_versioned_base("3.1", "PackageRequest")

    assert package.model_fields["budget"].is_required()
    assert "format_ids" in package.model_fields
    with pytest.raises(ValidationError, match="budget"):
        package(product_id="product-1", pricing_option_id="fixed")
    with pytest.raises(ValidationError, match="budget"):
        package(product_id="product-1", pricing_option_id="fixed", budget=None)

    valid = package(product_id="product-1", pricing_option_id="fixed", budget=100.0)
    assert valid.budget == 100.0


def test_versioned_base_is_cached_and_rejects_unknown_models() -> None:
    assert make_versioned_base("3.1", "ListCreativesRequest") is make_versioned_base(
        "3.1", "ListCreativesRequest"
    )
    with pytest.raises(LookupError, match="no 3.1 schema"):
        make_versioned_base("3.1", "NotAProtocolRequest")


def test_versioned_models_keep_generated_model_ergonomics() -> None:
    request = ListCreativesRequest31(include_assignments=True)
    assert request.include_assignments is True
    assert request["include_assignments"] is True
    assert request.model_dump()["include_assignments"] is True


def test_versioned_models_apply_schema_defaults() -> None:
    request = ListCreativesRequest31()
    assert request.include_assignments is True
    assert request.include_snapshot is False


def test_type_adapter_exposes_versioned_schema() -> None:
    schema = TypeAdapter(ListCreativesRequest31).json_schema()
    assert "assignment_projection" not in schema["properties"]
    assert schema["properties"]["include_assignments"]["default"] is True


def test_non_bundled_and_async_models_are_public() -> None:
    request = GetBrandIdentityRequest31(brand_id="brand-1")
    submitted = BuildCreativeSubmittedResponse(task_id="task-1", status="submitted")
    assert request.brand_id == "brand-1"
    assert submitted.status == "submitted"


def _tool_map(version: str) -> dict[str, dict]:
    builder = adcp_server("versioned-seller", adcp_version=version)

    @builder.list_creatives
    async def list_creatives(params, context=None):  # noqa: ANN001, ANN202
        return {"creatives": []}

    @builder.list_products
    async def list_products(params, context=None):  # noqa: ANN001, ANN202
        return {"products": []}

    return {tool["name"]: tool for tool in get_tools_for_handler(builder.build_handler())}


def test_mcp_tools_list_uses_pinned_31_schemas() -> None:
    tools = _tool_map("3.1")
    properties = tools["list_creatives"]["inputSchema"]["properties"]

    assert "assignment_projection" not in properties
    assert "assignment_limit" not in properties
    assert "list_products" not in tools


def test_mcp_tools_list_uses_pinned_32_schemas() -> None:
    tools = _tool_map("3.2-beta.9")
    properties = tools["list_creatives"]["inputSchema"]["properties"]

    assert "assignment_projection" in properties
    assert "assignment_limit" in properties
    assert "list_products" in tools


def test_mcp_tools_list_keeps_non_bundled_tools() -> None:
    builder = adcp_server("brand-agent", adcp_version="3.1")

    @builder.get_brand_identity
    async def get_brand_identity(params, context=None):  # noqa: ANN001, ANN202
        return {"brand_id": params["brand_id"]}

    tools = {tool["name"]: tool for tool in get_tools_for_handler(builder.build_handler())}
    assert "get_brand_identity" in tools
    assert "brand_id" in tools["get_brand_identity"]["inputSchema"]["required"]


def test_mcp_32_uses_compact_transport_schemas() -> None:
    import json

    tools = _tool_map("3.2-beta.9")
    encoded = json.dumps(tools["list_creatives"])
    assert len(encoded) < 300_000


def _contains_nonlocal_ref(value: object) -> bool:
    if isinstance(value, dict):
        reference = value.get("$ref")
        return (
            isinstance(reference, str)
            and not reference.startswith("#/")
            or any(_contains_nonlocal_ref(item) for item in value.values())
        )
    if isinstance(value, list):
        return any(_contains_nonlocal_ref(item) for item in value)
    return False


@pytest.mark.parametrize("version", ["3.0", "3.1", "3.2-beta.4"])
def test_pinned_mcp_inventory_is_portable_and_context_bounded(version: str) -> None:
    tools = get_tools_for_handler(ADCPHandler, advertise_all=True, adcp_version=version)
    assert not _contains_nonlocal_ref(tools)

    sample = next(tool for tool in tools if tool["name"] == "list_creatives")
    schema = sample["inputSchema"]
    validator_for(schema).check_schema(schema)


def test_mcp_compaction_preserves_deep_validation() -> None:
    payload = {
        "account": {
            "brand": {"domain": 123},
            "operator": "agency.example",
        }
    }
    canonical = get_validator("list_accounts", "request", version="3.1")
    mcp_schema = get_mcp_schema("list_accounts", "request", version="3.1")
    assert canonical is not None and mcp_schema is not None
    mcp_validator = validator_for(mcp_schema)(mcp_schema)
    assert list(canonical.iter_errors(payload))
    assert list(mcp_validator.iter_errors(payload))


def test_mcp_compaction_preserves_normative_descriptions() -> None:
    schema = get_mcp_schema("buy_products", "request", version="3.2-beta.4")

    assert schema is not None
    assert "exactly one brand source" in schema["description"]
    assert "single brand source" in schema["properties"]["account"]["description"]


@pytest.mark.parametrize("version", ["3.0", "3.1", "3.2-beta.4"])
def test_pinned_mcp_discovery_remains_context_bounded(version: str) -> None:
    tools = get_tools_for_handler(ADCPHandler, advertise_all=True, adcp_version=version)

    assert len(json.dumps(tools, separators=(",", ":"))) < 5_000_000


def test_mcp_tools_reject_missing_schema_bundle() -> None:
    with pytest.raises(ValueError, match="no bundled AdCP schemas"):
        get_tools_for_handler(ADCPHandler, advertise_all=True, adcp_version="3.3")


class _VersionCapturingHandler(ADCPHandler):
    advertised_tools = {"list_creatives"}

    def __init__(self) -> None:
        super().__init__()
        self.resolved_version: str | None = None

    async def list_creatives(self, params, context=None):  # noqa: ANN001, ANN202
        assert isinstance(context, ToolContext)
        self.resolved_version = context.resolved_adcp_version
        return {"creatives": []}


@pytest.mark.asyncio
async def test_mcp_dispatch_uses_same_pin_as_advertisement() -> None:
    handler = _VersionCapturingHandler()
    tools = create_mcp_tools(handler, adcp_version="3.2-beta.4")
    await tools.call_tool("list_creatives", {})
    assert handler.resolved_version == "3.2-beta.4"


@pytest.mark.asyncio
async def test_create_mcp_server_dispatch_uses_advertised_pin() -> None:
    handler = _VersionCapturingHandler()
    handler._adcp_version = "3.2-beta.4"
    mcp = create_mcp_server(handler, validation=None)
    tool_fn = mcp._tool_manager._tools["list_creatives"].fn
    await tool_fn()
    assert handler.resolved_version == "3.2-beta.4"
