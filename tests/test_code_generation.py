"""Tests for code generation from schemas.

This test suite validates that the code generation pipeline works correctly:
1. Schemas can be downloaded
2. Types can be generated from schemas
3. Generated code is valid Python
4. Generated types can be imported
"""

from __future__ import annotations

from pathlib import Path


def test_rewrite_refs_localizes_canonical_schema_urls_without_corrupting_prerelease():
    """Canonical absolute refs become local module paths before normalization."""
    from scripts.generate_types import rewrite_refs

    schema = {
        "$ref": (
            "https://adcontextprotocol.org/schemas/3.2.0-beta.3/"
            "core/platform-extension-ref.json#/$defs/custom-shape"
        )
    }

    rewrite_refs(schema, Path("media-buy/list-products-response.json"))

    assert schema["$ref"] == "../core/platform_extension_ref.json#/$defs/custom-shape"


def test_rewrite_refs_preserves_external_urls_and_json_pointer_fragments():
    """Hyphen normalization applies to local files, never external identifiers."""
    from scripts.generate_types import rewrite_refs

    external = {"$ref": "https://schemas.example.com/custom-shape.json#/$defs/foo-bar"}
    local = {"$ref": "../core/custom-shape.json#/$defs/foo-bar"}

    rewrite_refs(external, Path("media-buy/list-products-response.json"))
    rewrite_refs(local, Path("media-buy/list-products-response.json"))

    assert external["$ref"] == "https://schemas.example.com/custom-shape.json#/$defs/foo-bar"
    assert local["$ref"] == "../core/custom_shape.json#/$defs/foo-bar"


def test_rewrite_refs_uses_shortest_path_for_canonical_sibling_ref():
    """Canonical sibling refs avoid root round-trips that codegen mis-rebases."""
    from scripts.generate_types import rewrite_refs

    schema = {
        "$ref": (
            "https://adcontextprotocol.org/schemas/3.2.0-beta.3/" "core/assets/image-asset.json"
        )
    }

    rewrite_refs(schema, Path("core/assets/video-asset.json"))

    assert schema["$ref"] == "image_asset.json"


def test_nested_format_discriminator_drops_only_codegen_ambiguous_outer_hint():
    """Format asset consts remain while the codegen-only outer hint is removed."""
    from scripts.generate_types import stabilize_nested_discriminators

    items = {
        "discriminator": {"propertyName": "item_type"},
        "oneOf": [
            {"properties": {"item_type": {"const": "individual"}}},
            {"properties": {"item_type": {"const": "repeatable_group"}}},
        ],
    }
    schema = {"properties": {"assets": {"items": items}}}

    stabilize_nested_discriminators(schema, Path("core/format.json"))

    assert "discriminator" not in items
    assert items["oneOf"][0]["properties"]["item_type"]["const"] == "individual"


def test_audience_evidence_attestation_subject_uses_narrowed_resource_arm():
    """The allOf-constrained evidence subject must build a valid Pydantic schema."""
    from adcp.types.generated_poc.core.audience_evidence import AttestationRef

    schema = AttestationRef.model_json_schema()

    assert schema["properties"]["subject"]["$ref"].endswith("/$defs/Subject94")


def test_product_change_map_uses_valid_constrained_string_key_type():
    """Constrained mapping keys must be valid for Pydantic and static type checkers."""
    from adcp.types.generated_poc.core.product_change_map import ProductChangeMap

    schema = ProductChangeMap.model_json_schema()

    assert schema["propertyNames"]["minLength"] == 1


def test_protocol_envelope_import_restored_for_response_arms():
    """Response arms that inherit ProtocolEnvelope must keep the import."""
    from scripts.post_generate_fixes import _sync_protocol_envelope_import

    source = (
        "from __future__ import annotations\n\n"
        "from ..core.version_envelope import AdcpVersionEnvelope\n\n"
        "class CreateMediaBuyResponse3(AdcpVersionEnvelope, ProtocolEnvelope):\n"
        "    pass\n"
    )

    fixed = _sync_protocol_envelope_import(source)

    protocol_import = "from ..core.protocol_envelope import ProtocolEnvelope\n"
    version_import = "from ..core.version_envelope import AdcpVersionEnvelope\n"
    assert protocol_import in fixed
    assert fixed.index(protocol_import) < fixed.index(version_import)


def test_aliases_do_not_use_eager_generated_fallbacks():
    """Avoid getattr defaults that eagerly evaluate stale generated names."""
    import re
    from pathlib import Path

    source = Path("src/adcp/types/aliases.py").read_text()
    unsafe_getattrs = re.findall(
        r'getattr\(_g,\s*"[^"]+",\s*_g\.[A-Za-z_][A-Za-z0-9_]*\s*\)',
        source,
        flags=re.DOTALL,
    )

    assert unsafe_getattrs == []


def test_consolidated_exports_include_annotated_type_aliases(tmp_path):
    """Annotated TypeAlias response unions are part of the public generated API."""
    from scripts.consolidate_exports import extract_exports_from_module

    module_path = tmp_path / "example_response.py"
    module_path.write_text(
        "from typing import TypeAlias\n\n"
        "class ExampleResponse1:\n"
        "    pass\n\n"
        "ExampleResponse: TypeAlias = ExampleResponse1\n"
    )

    assert extract_exports_from_module(module_path) == {"ExampleResponse", "ExampleResponse1"}


def test_consolidation_filters_aggregate_schema_helpers():
    """Aggregate/reference modules do not shadow canonical public models."""
    from scripts.consolidate_exports import GENERATED_POC_DIR, exports_for_public_consolidation

    asset_union = GENERATED_POC_DIR / "core/assets/asset_union.py"
    async_ref = (
        GENERATED_POC_DIR
        / "core/async_response_refs/media_buy/accept_proposal_async_response_submitted.py"
    )

    assert exports_for_public_consolidation(asset_union) == {"AssetVariant"}
    assert exports_for_public_consolidation(async_ref) == set()


def test_semantic_response_aliases_resolve_to_concrete_generated_arms():
    """Semantic aliases must not fall back to the whole response union."""
    from importlib import import_module

    import adcp.types as adcp_types

    expected_aliases = [
        (
            "ActivateSignalSuccessResponse",
            "adcp.types.generated_poc.signals.activate_signal_response",
            "ActivateSignalResponse1",
        ),
        (
            "AcquireRightsAcquiredResponse",
            "adcp.types.generated_poc.brand.acquire_rights_response",
            "AcquireRightsResponse1",
        ),
        (
            "GetBrandIdentitySuccessResponse",
            "adcp.types.generated_poc.brand.get_brand_identity_response",
            "GetBrandIdentityResponse1",
        ),
        (
            "GetContentStandardsSuccessResponse",
            "adcp.types.generated_poc.content_standards.get_content_standards_response",
            "GetContentStandardsResponse1",
        ),
        (
            "GetCreativeFeaturesSuccessResponse",
            "adcp.types.generated_poc.creative.get_creative_features_response",
            "GetCreativeFeaturesResponse1",
        ),
        (
            "GetMediaBuyArtifactsSuccessResponse",
            "adcp.types.generated_poc.content_standards.get_media_buy_artifacts_response",
            "GetMediaBuyArtifactsResponse1",
        ),
        (
            "CreateMediaBuySubmittedResponse",
            "adcp.types.canonical_creative",
            "CreateMediaBuyResponse3",
        ),
    ]

    for alias_name, module_name, class_name in expected_aliases:
        alias = getattr(adcp_types, alias_name)
        expected = getattr(import_module(module_name), class_name)

        assert isinstance(alias, type), f"{alias_name} resolved to non-class {alias!r}"
        assert alias is expected, f"{alias_name} no longer points at {module_name}.{class_name}"


def test_sync_creatives_response_arm_matches_schema_creative_fields():
    """sync_creatives response arm must expose every schema Creative field."""
    import json
    from pathlib import Path

    import pytest
    from pydantic import ValidationError

    from adcp._version import _read_packaged_version
    from adcp.types.generated_poc.creative.sync_creatives_response import Creative
    from adcp.validation.version import resolve_bundle_key

    bundle_key = resolve_bundle_key(_read_packaged_version())
    schema_path = (
        Path("schemas") / "cache" / bundle_key / "creative" / "sync-creatives-response.json"
    )
    schema = json.loads(schema_path.read_text())
    creative_schema = schema["oneOf"][0]["properties"]["creatives"]["items"]

    assert set(Creative.model_fields) >= set(creative_schema["properties"])

    for payload in [
        {"creative_id": "c1", "action": "updated", "status": "banana"},
        {"creative_id": "c1", "action": "banana"},
        {"creative_id": "c1", "action": "updated", "preview_url": "not a url"},
        {"creative_id": "c1", "action": "updated", "expires_at": "not a datetime"},
        {
            "creative_id": "c1",
            "action": "updated",
            "assignment_errors": {"bad.key": "not a package id"},
        },
    ]:
        with pytest.raises(ValidationError):
            Creative.model_validate(payload)


def test_sync_creatives_response_arm_accepts_submitted_response():
    """sync_creatives schema includes a submitted async response branch."""
    from pydantic import TypeAdapter

    from adcp.types.generated_poc.creative.sync_creatives_response import (
        SyncCreativesResponse,
        SyncCreativesResponse3,
    )

    response = TypeAdapter(SyncCreativesResponse).validate_python(
        {
            "status": "submitted",
            "task_id": "task_123",
            "message": "Batch ingestion queued",
        }
    )

    assert isinstance(response, SyncCreativesResponse3)
    assert response.status == "submitted"
    assert response.task_id == "task_123"


def test_schema_derived_response_arms_preserve_nested_validation():
    """Schema-derived response arms should not widen structured fields to Any."""
    from datetime import date

    import pytest
    from pydantic import ValidationError

    from adcp.types.generated_poc.account.get_account_financials_response import Invoice
    from adcp.types.generated_poc.brand.get_brand_identity_response import (
        File,
        Fonts,
        GetBrandIdentityResponse1,
    )
    from adcp.types.generated_poc.content_standards.validate_content_delivery_response import (
        ValidateContentDeliveryResponse1,
    )
    from adcp.types.generated_poc.creative.preview_creative_response import Input

    with pytest.raises(ValidationError):
        GetBrandIdentityResponse1.model_validate(
            {"brand_id": "b1", "house": {}, "names": [{"en": "Brand"}]}
        )

    with pytest.raises(ValidationError):
        GetBrandIdentityResponse1.model_validate(
            {
                "brand_id": "b1",
                "house": {"domain": "example.com", "name": "Example"},
                "names": [{"en": "Brand"}],
                "tagline": 123,
            }
        )

    with pytest.raises(ValidationError):
        Fonts.model_validate({"primary": 123})

    with pytest.raises(ValidationError):
        File.model_validate({"url": "not-a-url"})

    with pytest.raises(ValidationError):
        Input.model_validate({})

    with pytest.raises(ValidationError):
        ValidateContentDeliveryResponse1.model_validate(
            {
                "summary": {
                    "total_records": "x",
                    "passed_records": 0,
                    "failed_records": 0,
                },
                "results": [],
            }
        )

    invoice = Invoice.model_validate(
        {"invoice_id": "inv_1", "amount": 1, "status": "draft", "due_date": date(2026, 1, 1)}
    )
    assert invoice.due_date == date(2026, 1, 1)

    with pytest.raises(ValidationError):
        Invoice.model_validate(
            {"invoice_id": "inv_1", "amount": 1, "status": "draft", "due_date": "not-a-date"}
        )


def test_post_generate_sync_creatives_response_arms_match_schema_creative_fields(
    tmp_path, monkeypatch
):
    """The post-generation response arms must stay aligned with the schema."""
    import ast
    import json
    from pathlib import Path

    from adcp._version import _read_packaged_version
    from adcp.validation.version import resolve_bundle_key
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "creative" / "sync_creatives_response.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# generated by datamodel-codegen:\n"
        "#   filename:  creative/sync_creatives_response.json\n\n"
        "from __future__ import annotations\n\n"
        "from ..core.version_envelope import AdcpVersionEnvelope\n\n\n"
        "class SyncCreativesResponse(AdcpVersionEnvelope):\n"
        "    pass\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.restore_response_variant_aliases()

    generated_source = target.read_text()
    post_generate_fixes.restore_response_variant_aliases()
    assert target.read_text() == generated_source

    compile(generated_source, str(target), "exec")
    module = ast.parse(generated_source)
    creative_class = next(
        node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Creative"
    )
    generated_fields = {
        node.target.id
        for node in creative_class.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    bundle_key = resolve_bundle_key(_read_packaged_version())
    schema_path = (
        Path("schemas") / "cache" / bundle_key / "creative" / "sync-creatives-response.json"
    )
    schema = json.loads(schema_path.read_text())
    creative_schema = schema["oneOf"][0]["properties"]["creatives"]["items"]

    assert generated_fields >= set(creative_schema["properties"])

    response_classes = {
        node.name
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name.startswith("SyncCreativesResponse")
    }
    assert {"SyncCreativesResponse1", "SyncCreativesResponse2", "SyncCreativesResponse3"} <= (
        response_classes
    )


def test_generated_types_can_import():
    """Test that generated types module can be imported."""
    from adcp.types import _generated as generated

    # Should have a reasonable number of exported symbols
    symbols = dir(generated)
    assert len(symbols) > 100, f"Expected >100 symbols, got {len(symbols)}"

    # Check for key types that should always exist
    assert hasattr(generated, "Product")
    assert hasattr(generated, "Format")
    assert hasattr(generated, "MediaBuy")
    assert hasattr(generated, "Property")


def test_generated_poc_types_can_import():
    """Test that generated_poc types can be imported."""
    from adcp.types import _generated as generated_poc

    # The generated_poc package should exist
    assert generated_poc is not None


def test_product_type_structure():
    """Test that Product type has expected structure."""
    from adcp import Product

    # Product should be a Pydantic model
    assert hasattr(Product, "model_validate")
    assert hasattr(Product, "model_dump")

    # Check for key fields
    model_fields = Product.model_fields
    assert "product_id" in model_fields
    assert "name" in model_fields
    assert "description" in model_fields
    assert "publisher_properties" in model_fields


def test_format_type_structure():
    """The public Format is a canonical declaration, not a named format."""
    from adcp import Format

    # Format should be a Pydantic model
    assert hasattr(Format, "model_validate")
    assert hasattr(Format, "model_dump")

    # Check for key fields
    model_fields = Format.model_fields
    assert "format_kind" in model_fields
    assert "params" in model_fields
    assert "format_option_id" in model_fields
    assert "format_id" not in model_fields
    assert "agent_url" not in model_fields


def test_no_request_response_rootmodels():
    """Guard: Request/Response types must NOT be RootModel classes.

    Pydantic 2 forbids model_config overrides on RootModel subclasses,
    which blocks consumers who subclass library types with extra='forbid'
    or custom fields. All Request/Response union types should be unwrapped
    to plain Union type aliases in post_generate_fixes.py.

    If this test fails after a schema update, add the new type to
    _UNWRAP_TO_UNION in scripts/post_generate_fixes.py.

    See: https://github.com/adcontextprotocol/adcp-client-python/issues/155
    """
    import types as builtin_types

    from pydantic import RootModel

    from adcp.types import _generated as gen

    # Scan all exported Request/Response types
    rootmodel_violations = []
    for name in dir(gen):
        if not (name.endswith("Request") or name.endswith("Response")):
            continue
        obj = getattr(gen, name)
        # Skip Union type aliases (these are the correctly unwrapped ones)
        if isinstance(obj, builtin_types.UnionType):
            continue
        # Skip non-classes
        if not isinstance(obj, type):
            continue
        # Flag RootModel subclasses wrapping unions (the problematic pattern)
        if issubclass(obj, RootModel):
            rootmodel_violations.append(name)

    assert rootmodel_violations == [], (
        f"These Request/Response types are RootModel classes, which blocks "
        f"consumer subclassing. Add them to _UNWRAP_TO_UNION in "
        f"scripts/post_generate_fixes.py: {rootmodel_violations}"
    )


# ============================================================================
# Consumer subclassability contract
# ============================================================================
# Types that downstream consumers (e.g., sales agents) are known to subclass.
# These MUST remain concrete BaseModel classes — never Union aliases, never
# RootModels. If the upstream spec changes one of these into a oneOf union,
# it must be flattened (via flatten_validation_oneof in generate_types.py)
# or otherwise kept as a single subclassable class.
#
# Adding a type here is a stability promise to consumers.
# See: https://github.com/adcontextprotocol/adcp-client-python/issues/155

_SUBCLASSABLE_CONTRACT: list[str] = [
    # Request types
    "ActivateSignalRequest",
    "CreateMediaBuyRequest",
    "GetCreativeDeliveryRequest",
    "GetMediaBuyDeliveryRequest",
    "GetSignalsRequest",
    "ListCreativeFormatsRequest",
    "ListCreativesRequest",
    "PackageRequest",
    "ProvidePerformanceFeedbackRequest",
    "SiSendMessageRequest",
    "SyncCreativesRequest",
    "UpdateMediaBuyRequest",
    # Response types
    "GetCreativeDeliveryResponse",
    "GetMediaBuyDeliveryResponse",
    "GetSignalsResponse",
    "ListCreativeFormatsResponse",
    "ListCreativesResponse",
    # Core value types
    "CreativePolicy",
    "Format",
    "FrequencyCap",
    "MediaBuy",
    "PackageUpdate",
    "Product",
    "Signal",
    "SignalFilters",
    "Targeting",
]


def test_consumer_subclassability_contract():
    """Guard: consumer-facing types must be subclassable BaseModel classes.

    Downstream consumers (sales agents, campaign managers, etc.) subclass
    library types to add internal fields, override nested types, customize
    serialization, and add validators. If a type in the contract list stops
    being a concrete, subclassable BaseModel class, those consumers break.

    This test catches three failure modes:
    1. Type became a Union alias (types.UnionType) — can't subclass
    2. Type became a RootModel — Pydantic forbids model_config overrides
    3. Subclassing with extra fields or model_config actually fails

    If this test fails after a schema update:
    - For validation-only oneOf: add flattening in generate_types.py
    - For genuine unions: ensure the type consumers need is a named variant
    - Update _SUBCLASSABLE_CONTRACT if the type was intentionally removed
    """
    import types as builtin_types

    from pydantic import BaseModel, ConfigDict, RootModel

    from adcp.types import _generated as gen

    failures = []
    for type_name in _SUBCLASSABLE_CONTRACT:
        obj = getattr(gen, type_name, None)

        if obj is None:
            failures.append(f"{type_name}: not found in _generated")
            continue

        if isinstance(obj, builtin_types.UnionType):
            failures.append(
                f"{type_name}: is a Union type alias, not a class — consumers cannot subclass it"
            )
            continue

        if not isinstance(obj, type):
            failures.append(f"{type_name}: not a class ({type(obj).__name__})")
            continue

        if issubclass(obj, RootModel):
            failures.append(
                f"{type_name}: is a RootModel — Pydantic forbids "
                f"model_config overrides on RootModel subclasses"
            )
            continue

        if not issubclass(obj, BaseModel):
            failures.append(f"{type_name}: not a BaseModel subclass")
            continue

        # Verify subclassing actually works
        try:
            subclass = type(
                f"Consumer{type_name}",
                (obj,),
                {
                    "model_config": ConfigDict(extra="forbid"),
                    "__annotations__": {"_internal_field": str},
                },
            )
            # Verify the subclass is valid
            assert issubclass(subclass, obj)
        except Exception as exc:
            failures.append(f"{type_name}: subclassing failed — {exc}")

    assert failures == [], "Consumer subclassability contract violated:\n" + "\n".join(
        f"  - {f}" for f in failures
    )
