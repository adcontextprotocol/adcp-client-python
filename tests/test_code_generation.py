"""Tests for code generation from schemas.

This test suite validates that the code generation pipeline works correctly:
1. Schemas can be downloaded
2. Types can be generated from schemas
3. Generated code is valid Python
4. Generated types can be imported
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_flatten_schemas_uses_stable_path_order(tmp_path, monkeypatch, capsys):
    """Aggregate model suffixes must not depend on filesystem insertion order."""
    from scripts import generate_types

    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "zeta.json").write_text('{"type": "object"}')
    (schemas / "alpha.json").write_text('{"type": "object"}')
    monkeypatch.setattr(generate_types, "SCHEMAS_DIR", schemas)
    monkeypatch.setattr(generate_types, "GENERATED_SCHEMA_EXCLUDE_FILES", frozenset())
    monkeypatch.setattr(generate_types, "GENERATED_SCHEMA_EXCLUDE_DIRS", frozenset())

    generate_types.flatten_schemas(tmp_path / "prepared")

    output = capsys.readouterr().out
    assert output.index("  alpha.json") < output.index("  zeta.json")


def test_restore_principal_result_aliases_uses_kind_discriminators(tmp_path, monkeypatch):
    """Principal aliases remain stable when anonymous class suffixes change."""
    from scripts import post_generate_fixes

    protocol_dir = tmp_path / "protocol"
    protocol_dir.mkdir()
    (protocol_dir / "get_principal_response.py").write_text(
        "from typing import Literal\n"
        "class Result42:\n"
        "    kind: Literal['current'] = 'current'\n"
        "class Result3:\n"
        "    kind: Literal['recognized'] = 'recognized'\n"
        "class Result:\n"
        "    kind: Literal['unconfigured'] = 'unconfigured'\n"
        "class Result8:\n"
        "    kind: Literal['failed'] = 'failed'\n"
    )
    (protocol_dir / "sync_principal_response.py").write_text(
        "from typing import Literal\n"
        "class Result4:\n"
        "    kind: Literal['validated'] = 'validated'\n"
        "class Result12:\n"
        "    kind: Literal['applied'] = 'applied'\n"
        "class Result99:\n"
        "    kind: Literal['failed'] = 'failed'\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", tmp_path)

    post_generate_fixes.restore_principal_result_aliases()

    get_source = (protocol_dir / "get_principal_response.py").read_text()
    sync_source = (protocol_dir / "sync_principal_response.py").read_text()
    assert "PrincipalCurrentResult = Result42" in get_source
    assert "PrincipalRecognizedResult = Result3" in get_source
    assert "PrincipalAppliedResult = Result12" in sync_source
    assert "PrincipalSyncFailedResult = Result99" in sync_source


def test_disambiguate_comply_response_arm_renames_class_and_references(tmp_path, monkeypatch):
    """Fresh codegen output cannot add a generic public Arm collision."""
    from scripts import post_generate_fixes

    compliance_dir = tmp_path / "compliance"
    compliance_dir.mkdir()
    target = compliance_dir / "comply_test_controller_response.py"
    target.write_text(
        "from enum import Enum\n"
        "class Arm(Enum):\n"
        "    submitted = 'submitted'\n"
        "class Forced:\n"
        "    arm: Arm\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", tmp_path)

    post_generate_fixes.disambiguate_comply_response_arm()

    source = target.read_text()
    assert "class ComplyResponseArm(Enum):" in source
    assert "arm: ComplyResponseArm" in source
    assert "class Arm(" not in source


def test_post_generation_restores_codegen_contract_compatibility(tmp_path, monkeypatch):
    """Known 0.64 flattening and response-union regressions stay repaired."""
    from scripts import post_generate_fixes

    core_dir = tmp_path / "core"
    core_dir.mkdir()
    (core_dir / "product_signal_targeting_option.py").write_text(
        "from typing import Annotated, Any\n"
        "from . import vendor_pricing_option\n"
        "from .signal_listing import SignalListing\n"
        "class ProductSignalTargetingOption(SignalListing):\n"
        "    signal_ref: Any\n"
    )
    (core_dir / "creative_representation.py").write_text(
        "from typing import Annotated, Any\n"
        "from pydantic import ConfigDict, Field\n"
        "from .creative_manifest import CreativeManifest\n"
        "class CreativeRepresentation(CreativeManifest):\n"
        "    model_config = ConfigDict(\n"
        "        extra='allow',\n"
        "    )\n"
        "    format_kind: Any\n"
    )
    (core_dir / "transformer.py").write_text(
        "from pydantic import AnyUrl, ConfigDict, Field, RootModel\n"
        "class Transformer(AdCPBaseModel):\n"
        "    pass\n"
    )
    response_specs = (
        ("compliance/comply_test_controller_response.py", "ComplyTestControllerResponse"),
        (
            "content_standards/create_content_standards_response.py",
            "CreateContentStandardsResponse",
        ),
        (
            "content_standards/list_content_standards_response.py",
            "ListContentStandardsResponse",
        ),
        ("account/sync_governance_response.py", "SyncGovernanceResponse"),
        (
            "content_standards/update_content_standards_response.py",
            "UpdateContentStandardsResponse",
        ),
    )
    for relative_path, response_name in response_specs:
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "class " + response_name + "1(AdcpVersionEnvelope, ProtocolEnvelope):\n    pass\n"
            "class "
            + response_name
            + "2(AdcpVersionEnvelope, ProtocolEnvelope):\n    pass\n"
            + response_name
            + " = "
            + response_name
            + "1 | "
            + response_name
            + "2\n"
        )

    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", tmp_path)
    post_generate_fixes.restore_flattened_contract_field_types()
    post_generate_fixes.enforce_transformer_output_contract()
    post_generate_fixes.restore_constructible_response_bases()
    # The functions are deliberately safe when the post-fix pass runs twice.
    post_generate_fixes.restore_flattened_contract_field_types()
    post_generate_fixes.enforce_transformer_output_contract()
    post_generate_fixes.restore_constructible_response_bases()

    product_source = (core_dir / "product_signal_targeting_option.py").read_text()
    assert "from . import signal_ref" in product_source
    assert "signal_ref: Annotated[\n        signal_ref.SignalRef," in product_source
    assert "Canonical signal reference." in product_source

    representation_source = (core_dir / "creative_representation.py").read_text()
    assert "from .canonical_format_kind import CanonicalFormatKind" in representation_source
    assert "format_kind: Annotated[\n        CanonicalFormatKind," in representation_source
    assert "Canonical 3.2 path." in representation_source
    assert "'representation_selection'" in representation_source
    assert "@model_validator(mode='before')" in representation_source

    transformer_source = (core_dir / "transformer.py").read_text()
    assert "@model_validator(mode='after')" in transformer_source
    assert "output_capability_ids" in transformer_source
    assert "output_format_ids" in transformer_source

    for relative_path, response_name in response_specs:
        response_source = (tmp_path / relative_path).read_text()
        assert f"class {response_name}(AdcpVersionEnvelope, ProtocolEnvelope):" in response_source
        assert f"class {response_name}1({response_name}):" in response_source
        assert f"{response_name} =" not in response_source


def test_normalize_enum_descriptions_preserves_enum_order():
    """Description maps become the positional list expected by codegen 0.64+."""
    from scripts.generate_types import normalize_enum_descriptions

    schema = {
        "enum": ["binary", "categorical", "numeric"],
        "x-enum-descriptions": {
            "numeric": "Continuous value",
            "binary": "Boolean value",
            "categorical": "Discrete value",
        },
    }

    assert normalize_enum_descriptions(schema)["x-enum-descriptions"] == [
        "Boolean value",
        "Discrete value",
        "Continuous value",
    ]


def test_normalize_enum_descriptions_recurses_into_embedded_schemas():
    from scripts.generate_types import normalize_enum_descriptions

    schema = {
        "$defs": {
            "state": {
                "enum": ["ready", "done"],
                "x-enum-descriptions": {"done": "Finished", "ready": "Available"},
            }
        }
    }

    normalize_enum_descriptions(schema)

    assert schema["$defs"]["state"]["x-enum-descriptions"] == ["Available", "Finished"]


def test_post_generate_removes_only_unused_pydantic_field_imports():
    from scripts.post_generate_fixes import _remove_unused_pydantic_field_import

    unused = (
        "from pydantic import ConfigDict, Field\n\n"
        "class Example:\n"
        "    model_config = ConfigDict(extra='forbid')\n"
    )
    updated, changed = _remove_unused_pydantic_field_import(unused)

    assert changed
    assert "from pydantic import ConfigDict\n" in updated
    assert "Field" not in updated
    assert _remove_unused_pydantic_field_import(updated) == (updated, False)

    used = "from pydantic import Field\n\nvalue = Field(default=None)\n"
    assert _remove_unused_pydantic_field_import(used) == (used, False)

    standalone = (
        "from __future__ import annotations\n\n"
        "from adcp.types._str_enum import StrEnum\n\n"
        "from pydantic import Field\n\n\n"
        "class Example(StrEnum):\n"
        "    value = 'value'\n"
    )
    cleaned, changed = _remove_unused_pydantic_field_import(standalone)
    assert changed
    assert cleaned == (
        "from __future__ import annotations\n\n"
        "from adcp.types._str_enum import StrEnum\n\n\n"
        "class Example(StrEnum):\n"
        "    value = 'value'\n"
    )


def test_flatten_validation_oneof_accepts_branch_annotations():
    from scripts.generate_types import flatten_validation_oneof

    schema = {
        "title": "Request",
        "type": "object",
        "properties": {"first": {"type": "string"}, "second": {"type": "string"}},
        "required": ["mode"],
        "anyOf": [
            {
                "title": "First mode",
                "description": "Requires a payload.",
                "required": ["first"],
            },
            {
                "title": "Second mode",
                "deprecated": True,
                "required": ["second"],
            },
        ],
    }

    flattened = flatten_validation_oneof(schema)

    assert "anyOf" not in flattened
    assert flattened["required"] == ["mode"]


def test_flatten_known_root_object_union_merges_branch_only_fields():
    from scripts.generate_types import flatten_root_object_validation_union

    schema = {
        "type": "object",
        "allOf": [{"$ref": "envelope.json"}],
        "properties": {"context": {"type": "object"}},
        "oneOf": [
            {
                "properties": {"status": {"const": "completed"}, "result": {"type": "string"}},
                "required": ["status", "result"],
            },
            {
                "properties": {"status": {"const": "failed"}, "errors": {"type": "array"}},
                "required": ["status", "errors"],
            },
        ],
    }

    flattened = flatten_root_object_validation_union(
        schema, Path("account/list-account-changes-response.json")
    )

    assert "oneOf" not in flattened
    assert flattened["properties"]["status"] == {"enum": ["completed", "failed"]}
    assert set(flattened["properties"]) == {"context", "status", "result", "errors"}
    assert flattened["required"] == ["status"]
    assert flattened["allOf"] == [{"$ref": "envelope.json"}]


def test_flatten_root_object_union_ignores_unlisted_schema():
    from scripts.generate_types import flatten_root_object_validation_union

    schema = {
        "type": "object",
        "properties": {"kind": {"type": "string"}},
        "oneOf": [{"properties": {"kind": {"const": "a"}}}],
    }

    assert flatten_root_object_validation_union(schema, Path("core/real-union.json")) is schema
    assert "oneOf" in schema


def test_rewrite_refs_localizes_canonical_schema_urls_without_corrupting_prerelease():
    """Canonical absolute refs become local module paths before normalization."""
    from scripts.generate_types import rewrite_refs

    schema = {
        "$ref": (
            "https://adcontextprotocol.org/schemas/3.2.0-beta.4/"
            "core/platform-extension-ref.json#/$defs/custom-shape"
        )
    }

    rewrite_refs(schema, Path("media-buy/list-products-response.json"))

    assert schema["$ref"] == "../core/platform_extension_ref.json#/$defs/custom-shape"


def test_rewrite_refs_preserves_domain_in_root_relative_source_refs():
    """The first component after /schemas is a domain, not a version."""
    from scripts.generate_types import rewrite_refs

    enum_ref = {"$ref": "/schemas/enums/account-status.json"}
    core_ref = {"$ref": "/schemas/core/brand-ref.json"}

    rewrite_refs(enum_ref, Path("core/account.json"))
    rewrite_refs(core_ref, Path("account/sync-accounts-request.json"))

    assert enum_ref["$ref"] == "../enums/account_status.json"
    assert core_ref["$ref"] == "../core/brand_ref.json"


def test_post_generate_ref_resolution_preserves_root_relative_domain():
    """Post-generation alias restoration resolves the same source ref shape."""
    from scripts.post_generate_fixes import _resolve_schema_ref

    assert _resolve_schema_ref(
        Path("account/sync-accounts-response.json"),
        "/schemas/core/brand-ref.json#/$defs/brand",
    ) == Path("core/brand-ref.json")


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
        "$ref": ("https://adcontextprotocol.org/schemas/3.2.0-beta.4/core/assets/image-asset.json")
    }

    rewrite_refs(schema, Path("core/assets/video-asset.json"))

    assert schema["$ref"] == "image_asset.json"


def test_rewrite_refs_preserves_macro_declaration_canonical_enum_ref():
    """Nested asset generation must not rebase MacroDeclaration's enum ref."""
    from scripts.generate_types import rewrite_refs

    schema = {
        "$ref": ("https://adcontextprotocol.org/schemas/3.2.0-beta.10/" "enums/macro-dialect.json")
    }

    rewrite_refs(schema, Path("core/macro-declaration.json"))

    assert schema["$ref"] == (
        "https://adcontextprotocol.org/schemas/3.2.0-beta.10/" "enums/macro-dialect.json"
    )


def test_rewrite_refs_normalizes_sibling_macro_and_equivalent_local_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Transitive macro refs consistently target the staged schema tree."""
    from scripts import generate_types

    monkeypatch.setattr(generate_types, "TEMP_DIR", tmp_path)
    sibling = {"$ref": "macro-declaration.json"}
    local_equivalent = {"$ref": "../enums/macro-dialect.json"}

    generate_types.rewrite_refs(sibling, Path("core/video-template.json"))
    generate_types.rewrite_refs(local_equivalent, Path("core/macro-declaration.json"))

    assert sibling["$ref"] == (tmp_path / "core" / "macro_declaration.json").as_posix()
    assert local_equivalent["$ref"] == (tmp_path / "enums" / "macro_dialect.json").as_posix()


@pytest.mark.parametrize("ref", ["../../outside.json", "/outside.json"])
def test_rewrite_refs_rejects_schema_root_escape(ref: str):
    """A malformed local reference cannot escape the staged schema root."""
    from scripts.generate_types import rewrite_refs

    with pytest.raises(ValueError, match="schema reference escapes its root"):
        rewrite_refs({"$ref": ref}, Path("core/model.json"))


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

    subject_ref = schema["properties"]["subject"]["$ref"]
    subject_schema = schema["$defs"][subject_ref.rsplit("/", 1)[-1]]
    assert subject_schema["properties"]["type"]["const"] == "resource"
    assert set(subject_schema["required"]) >= {"content_digest", "namespace", "id"}


def test_post_generate_legacy_purchase_losses_are_always_an_array(tmp_path, monkeypatch):
    """The schema's negated empty array must not become an empty object arm."""
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "media_buy" / "legacy_purchase_continuation_input.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from adcp.types.base import AdCPBaseModel\n\n"
        "class AcceptedLosses(AdCPBaseModel):\n"
        "    pass\n\n"
        "class CompatibilityPurchaseCoordinatorInput(AdCPBaseModel):\n"
        "    accepted_losses: list[AcceptedLoss] | AcceptedLosses\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_legacy_purchase_accepted_losses()

    fixed = target.read_text()
    assert "class AcceptedLosses" not in fixed
    assert "accepted_losses: list[AcceptedLoss]\n" in fixed


def test_post_generate_legacy_purchase_losses_restore_array_constraints(tmp_path, monkeypatch):
    """Runtime and emitted schemas retain constraints codegen drops."""
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "media_buy" / "legacy_purchase_continuation_input.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from pydantic import ConfigDict, Field, RootModel\n\n"
        "class CompatibilityPurchaseCoordinatorInput:\n"
        "    selected_product_ids: Annotated[\n"
        "        list[SelectedProductId],\n"
        "        Field(\n"
        "            description='Non-empty subset of the product IDs bound into the "
        "continuation.',\n"
        "            min_length=1,\n"
        "        ),\n"
        "    ]\n"
        "    accepted_losses: Annotated[\n"
        "        list[AcceptedLoss] | AcceptedLosses,\n"
        "        Field(\n"
        "            description='Exact loss set returned with the continuation. Missing, "
        "extra, or stale consent fails before mutation.',\n"
        "            min_length=2,\n"
        "        ),\n"
        "    ]\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_legacy_purchase_accepted_losses()
    post_generate_fixes.fix_legacy_purchase_accepted_losses()
    fixed = target.read_text()

    assert "field_validator" in fixed
    assert fixed.count("json_schema_extra=") == 2
    assert "'uniqueItems': True" in fixed
    assert "'feed_version_not_atomic'" in fixed
    assert "def _accepted_losses_match_schema(" in fixed


def test_post_generate_change_term_constraint_import_is_idempotent(tmp_path, monkeypatch):
    """Repeated post-generation fixes never duplicate model_validator imports."""
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "media_buy" / "change_term_constraints.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from pydantic import AwareDatetime, ConfigDict, Field, RootModel\n\n"
        "class MediaBuyChangeTermConstraints1(AdCPBaseModel):\n"
        "    max_delta_amount: object | None = None\n\n"
        "class MediaBuyChangeTermConstraints2(AdCPBaseModel):\n"
        "    max_change: object | None = None\n\n"
        "class MediaBuyChangeTermConstraints3(AdCPBaseModel):\n"
        "    max_additions: int | None = None\n\n"
        "class MediaBuyChangeTermConstraints4(AdCPBaseModel):\n"
        "    minimum_notice: object | None = None\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.enforce_change_term_runtime_constraints()
    first = target.read_text()
    post_generate_fixes.enforce_change_term_runtime_constraints()

    assert target.read_text() == first
    assert first.count("model_validator") == 5


def test_post_generate_preserves_request_signing_operation_strings(tmp_path, monkeypatch):
    """Constrained operation names remain plain strings after validation."""
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    targets = (
        generated_dir / "protocol" / "get_adcp_capabilities_response.py",
        generated_dir / "bundled" / "protocol" / "get_adcp_capabilities_response.py",
    )
    source = (
        "class RequiredConnection:\n"
        "    required_for: list[RequiredForItem] | None = []\n\n"
        "class RequestSigning(AdCPBaseModel):\n"
        "    required_for: list[RequiredForItem29] | None = []\n"
        "    warn_for: list[WarnForItem3] | None = []\n"
        "    supported_for: list[SupportedForItem2] | None = None\n"
        "    protocol_methods_required_for: list[ProtocolMethodsRequiredForItem] = []\n"
        "\nclass Algorithm:\n"
        "    pass\n"
    )
    for target in targets:
        target.parent.mkdir(parents=True)
        target.write_text(source)
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.preserve_request_signing_operation_strings()
    post_generate_fixes.preserve_request_signing_operation_strings()

    for target in targets:
        fixed = target.read_text()
        assert fixed.count("Annotated[str, Field(pattern='^[a-z][a-z0-9_]*$')]") == 3
        assert "class RequiredConnection:\n    required_for: list[RequiredForItem]" in fixed
        assert "list[ProtocolMethodsRequiredForItem]" in fixed


def test_allof_merge_selects_literal_base_independent_of_order(tmp_path, monkeypatch):
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "core" / "postal_area.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from typing import Literal\n\n"
        "class Loose:\n"
        "    country: str\n"
        "    system: str\n\n"
        "class Narrow:\n"
        "    country: Literal['US']\n"
        "    system: Literal['zip', 'zip_plus_four']\n\n"
        "class PostalArea(Loose, Narrow):\n"
        "    country: str\n"
        "    system: str\n"
        "    values: list[str]\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_allof_merge_field_override_conflicts()

    fixed = target.read_text()
    assert "class PostalArea(Narrow):" in fixed
    assert "    country: str" not in fixed.split("class PostalArea", 1)[1]
    assert "    system: str" not in fixed.split("class PostalArea", 1)[1]
    assert "    values: list[str]" in fixed


def test_allof_merge_leaves_named_disjoint_bases_untouched(tmp_path, monkeypatch):
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "core" / "disjoint.py"
    target.parent.mkdir(parents=True)
    source = (
        "class SignalBase:\n"
        "    signal_id: str\n\n"
        "class TargetingBase:\n"
        "    targeting: dict\n\n"
        "class SignalTargetingItem(SignalBase, TargetingBase):\n"
        "    pass\n\n"
        "class PricingBase:\n"
        "    model: str\n\n"
        "class PriceBase:\n"
        "    amount: float\n\n"
        "class VendorPricingOption(PricingBase, PriceBase):\n"
        "    pass\n"
    )
    target.write_text(source)
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_allof_merge_field_override_conflicts()

    assert target.read_text() == source


def test_allof_merge_preserves_non_type_literal_discriminator_branch(tmp_path, monkeypatch):
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "core" / "budget_allocation.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from typing import Literal\n\n"
        "class Fixed:\n"
        "    mode: Literal['fixed']\n\n"
        "class Percentage:\n"
        "    mode: Literal['percentage']\n\n"
        "class FixedBranch(Fixed, Percentage):\n"
        "    mode: Literal['fixed']\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_allof_merge_field_override_conflicts()

    fixed = target.read_text()
    assert "class FixedBranch(Fixed):" in fixed
    assert "    mode: Literal['fixed']" not in fixed.split("class FixedBranch", 1)[1]
    assert "class FixedBranch(Fixed):\n    pass\n" in fixed
    compile(fixed, str(target), "exec")


def test_allof_merge_fails_when_narrow_base_is_ambiguous(tmp_path, monkeypatch):
    import pytest

    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "core" / "ambiguous.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "class Left:\n"
        "    value: str\n\n"
        "class Right:\n"
        "    value: int\n\n"
        "class Merge(Left, Right):\n"
        "    value: str\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    with pytest.raises(RuntimeError, match="Cannot safely order allOf merge bases"):
        post_generate_fixes.fix_allof_merge_field_override_conflicts()


def test_list_creatives_merged_model_restores_xor_and_legacy_aliases(tmp_path, monkeypatch):
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "creative" / "list_creatives_response.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from __future__ import annotations\n\n"
        "from pydantic import AwareDatetime, ConfigDict, Field, RootModel, StringConstraints\n\n"
        "class AdCPBaseModel:\n"
        "    pass\n\n"
        "class AdcpVersionEnvelope:\n"
        "    pass\n\n"
        "class ProtocolEnvelope:\n"
        "    pass\n\n"
        "class Creative(AdCPBaseModel):\n"
        "    format_id = None\n"
        "    format_kind = None\n\n"
        "class ListCreativesResponse(AdcpVersionEnvelope, ProtocolEnvelope):\n"
        "    pass\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_list_creatives_format_reference_xor()

    fixed = target.read_text()
    assert "def _validate_format_reference_xor(self) -> Creative:" in fixed
    assert "Creatives = Creative\nCreatives1 = Creative" in fixed
    compile(fixed, str(target), "exec")


def test_allof_merge_preserves_concrete_type_constraints_and_requiredness(tmp_path, monkeypatch):
    import pytest
    from pydantic import ValidationError

    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "concrete.py"
    generated_dir.mkdir(parents=True)
    target.write_text(
        "from typing import Annotated, Any\n"
        "from pydantic import BaseModel, Field\n\n"
        "class Placeholder(BaseModel):\n"
        "    items: Annotated[Any, Field(min_length=1)]\n\n"
        "class Concrete(BaseModel):\n"
        "    items: Annotated[list[str] | None, Field(description='typed items')] = None\n\n"
        "class Merge(Placeholder, Concrete):\n"
        "    items: Any\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_allof_merge_field_override_conflicts()

    fixed = target.read_text()
    assert "class Merge(Concrete):" in fixed
    assert "Annotated[list[str]," in fixed
    assert "Field(description='typed items')" in fixed
    assert "Field(min_length=1)" in fixed

    namespace: dict[str, object] = {}
    exec(compile(fixed, str(target), "exec", dont_inherit=True), namespace)
    merge = namespace["Merge"]
    with pytest.raises(ValidationError, match="Field required"):
        merge.model_validate({})
    with pytest.raises(ValidationError, match="at least 1 item"):
        merge.model_validate({"items": []})
    with pytest.raises(ValidationError, match="valid list"):
        merge.model_validate({"items": None})
    assert merge.model_validate({"items": ["one"]}).items == ["one"]


def test_generated_adagents_requires_authorization_or_non_empty_catalog():
    import pytest
    from pydantic import ValidationError

    from adcp.types.generated_poc.adagents import AdcpAgentsAuthorization

    with pytest.raises(ValidationError):
        AdcpAgentsAuthorization.model_validate({"authorized_agents": []})

    model = AdcpAgentsAuthorization.model_validate(
        {"authorized_agents": [], "formats": [{"format_kind": "image"}]}
    )
    assert model.root.root.authorized_agents == []


def test_post_generate_restores_product_fields_item_reference(tmp_path, monkeypatch):
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "media_buy" / "get_products_request.py"
    target.parent.mkdir(parents=True)
    target.write_text("fields: list[product_fields.Items] | None = None\n")
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_product_fields_item_reference()
    post_generate_fixes.fix_product_fields_item_reference()

    fixed = target.read_text()
    assert "product_fields.ProductResponseField" in fixed
    assert "product_fields.Items" not in fixed


def test_post_generate_restores_combined_get_products_field_enum(tmp_path, monkeypatch):
    """The public projection enum remains the union after beta.9's schema split."""
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    media_buy_dir = generated_dir / "media_buy"
    media_buy_dir.mkdir(parents=True)
    target = media_buy_dir / "get_products_request.py"
    target.write_text(
        "class Fields(StrEnum):\n"
        "    format_ids = 'format_ids'\n\n\n"
        "class GetProductsRequest(AdcpVersionEnvelope):\n"
        "    pass\n"
    )
    (media_buy_dir / "product_fields.py").write_text(
        "class ProductResponseField(StrEnum):\n"
        "    product_id = 'product_id'\n"
        "    name = 'name'\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.restore_get_products_field_compatibility_enum()
    post_generate_fixes.restore_get_products_field_compatibility_enum()

    fixed = target.read_text()
    assert fixed.count("class Field1(StrEnum):") == 1
    assert "    product_id = 'product_id'" in fixed
    assert "    name = 'name'" in fixed
    assert "    format_ids = 'format_ids'" in fixed


@pytest.mark.parametrize("catalog_field", ["properties", "placements", "collections", "signals"])
def test_generated_adagents_rejects_null_required_catalog_arm(catalog_field):
    from pydantic import ValidationError

    from adcp.types.generated_poc.adagents import AdcpAgentsAuthorization

    with pytest.raises(ValidationError):
        AdcpAgentsAuthorization.model_validate({"authorized_agents": [], catalog_field: None})


def test_post_generate_injects_postal_pairing_validator_idempotently(tmp_path, monkeypatch):
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "core" / "postal_area.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from typing import Any\n"
        "from pydantic import RootModel\n\n"
        "class PostalArea(RootModel[dict[str, Any]]):\n"
        "    root: dict[str, Any]\n\n"
        "    def __getattr__(self, name: str) -> Any:\n"
        "        return getattr(self.root, name)\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_postal_country_system_pairing()
    post_generate_fixes.fix_postal_country_system_pairing()

    fixed = target.read_text()
    assert fixed.count("def _validate_country_system_pairing(") == 1
    assert "from pydantic import RootModel, model_validator" in fixed
    assert "'US': ('zip', 'zip_plus_four')" in fixed


def test_post_generate_prefers_legacy_postal_union_arm_idempotently(tmp_path, monkeypatch):
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    target = generated_dir / "core" / "postal_area.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from typing import Annotated\n"
        "from pydantic import RootModel\n\n"
        "class PostalArea(RootModel[PostalArea1 | PostalArea2]):\n"
        "    root: Annotated[PostalArea1 | PostalArea2, object()]\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.fix_postal_union_arm_order()
    post_generate_fixes.fix_postal_union_arm_order()

    fixed = target.read_text()
    assert "PostalArea1 | PostalArea2" not in fixed
    assert fixed.count("PostalArea2 | PostalArea1") == 2


def test_post_generate_exposes_account_reference_union_fields_idempotently(tmp_path, monkeypatch):
    from scripts import post_generate_fixes

    generated_dir = tmp_path / "generated_poc"
    account_ref = generated_dir / "core" / "account_ref.py"
    account_ref.parent.mkdir(parents=True)
    account_ref.write_text(
        "from pydantic import RootModel\n\n"
        "class AccountReference(RootModel["
        "AccountReference1 | AccountReference2 | AccountReference3]):\n"
        "    pass\n"
    )
    target = generated_dir / "sample_request.py"
    target.write_text(
        "account: account_ref.AccountReference | None\n"
        "accounts: list[account_ref_1.AccountReference]\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", generated_dir)

    post_generate_fixes.expose_account_reference_union_fields()
    post_generate_fixes.expose_account_reference_union_fields()

    assert target.read_text() == (
        "account: account_ref.AccountReference1 | account_ref.AccountReference2 | "
        "account_ref.AccountReference3 | None\n"
        "accounts: list[account_ref_1.AccountReference1 | account_ref_1.AccountReference2 | "
        "account_ref_1.AccountReference3]\n"
    )


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


def test_consolidated_export_timestamp_is_stable_when_content_is_unchanged():
    from scripts.consolidate_exports import preserve_generation_date_if_unchanged

    previous = "header\nGeneration date: 2026-08-24 04:36:02 UTC\nbody\n"
    generated = "header\nGeneration date: 2026-08-28 20:03:34 UTC\nbody\n"

    assert preserve_generation_date_if_unchanged(previous, generated) == previous
    assert (
        preserve_generation_date_if_unchanged(previous, generated + "changed\n")
        == generated + "changed\n"
    )


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
        # Collision disambiguators are private implementation names such as
        # ``_ProductIdFromRequestProposalsResponse``. They are not public
        # request/response models and may legitimately wrap a scalar.
        if name.startswith("_") or not (name.endswith("Request") or name.endswith("Response")):
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
