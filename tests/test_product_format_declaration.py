"""The authoring declaration is a class graded against its bundled root schema."""

from __future__ import annotations

import copy
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import adcp
from adcp.canonical_formats import (
    find_declaration_by_kind,
    group_declarations_by_product,
    project_declaration_to_v1,
)
from adcp.types import (
    CanonicalFormatImage,
    CanonicalFormatKind,
    Format,
    LegacyProductFormatDeclaration,
    Package,
    ProductFormatDeclaration,
    validate_union,
)
from adcp.types.domains.core.product_format_declaration import (
    ProductFormatDeclaration as GeneratedProductFormatDeclaration,
)
from adcp.validation.schema_loader import get_named_validator

_REF = {"agent_url": "https://creative.example.com", "id": "display_300x250"}
_SCHEMA = {"uri": "https://formats.example.com/custom.json", "digest": "sha256:" + "a" * 64}


def _payload(kind: str = "image") -> dict[str, Any]:
    data: dict[str, Any] = {"format_kind": kind, "params": {}}
    if kind == "custom":
        data.update(format_shape="roadblock", format_schema=_SCHEMA, canonical_formats_only=True)
    elif kind == "seller_rendered_stateful_display":
        data["params"] = {
            "states": [
                {
                    "state_id": "initial",
                    "anchoring": "inline",
                    "breakpoints": [{"breakpoint_id": "desktop", "width": 300, "height": 250}],
                    "close_affordance": False,
                }
            ],
            "initial_state_id": "initial",
            "user_controls": {"dismissible": False, "user_collapsible": False},
        }
    elif kind == "coordinated_placements":
        data["params"] = {
            "components": [
                {
                    "component_id": identifier,
                    "placement_ref": {"placement_id": identifier},
                    "required": True,
                    "format_option_ref": {
                        "scope": "product",
                        "format_option_id": f"{identifier}_image",
                    },
                }
                for identifier in ("left", "right")
            ]
        }
    return data


# --- the public name is a class ------------------------------------------------


def test_public_name_is_a_constructible_class() -> None:
    assert adcp.ProductFormatDeclaration is ProductFormatDeclaration
    assert isinstance(ProductFormatDeclaration, type)
    declaration = ProductFormatDeclaration(
        format_kind="image", params={"width": 300, "height": 250}
    )
    assert declaration.format_kind == "image"


def test_public_name_supports_isinstance_and_model_validate() -> None:
    declaration = ProductFormatDeclaration.model_validate(_payload())
    assert isinstance(declaration, ProductFormatDeclaration)
    assert not isinstance(Format(format_kind="image", params={}), ProductFormatDeclaration)


def test_declaration_is_a_format_so_the_projection_surface_accepts_it() -> None:
    declaration = ProductFormatDeclaration.model_validate(
        {"format_kind": "image", "params": {"width": 300, "height": 250}, "v1_format_ref": [_REF]}
    )
    assert isinstance(declaration, Format)
    assert project_declaration_to_v1(declaration) is not None
    assert find_declaration_by_kind("image", [declaration]) is declaration
    assert group_declarations_by_product([declaration], {"product_1": ["image"]}) is not None


def test_consumer_accessors_stay_reachable_on_the_public_name() -> None:
    declaration = ProductFormatDeclaration.model_validate(
        {"format_kind": "image", "params": {"width": 300, "height": 250}, "v1_format_ref": [_REF]}
    )
    assert [ref.id for ref in declaration.legacy_format_refs] == ["display_300x250"]
    assert declaration.params_as(CanonicalFormatImage).width == 300


def test_the_generated_union_remains_exported_under_its_own_name() -> None:
    assert LegacyProductFormatDeclaration is GeneratedProductFormatDeclaration
    assert ProductFormatDeclaration is not LegacyProductFormatDeclaration
    assert ProductFormatDeclaration is not Format


# --- the schema's root rules ---------------------------------------------------


@pytest.mark.parametrize("kind", [kind.value for kind in CanonicalFormatKind])
def test_every_canonical_kind_validates_through_the_public_class(kind: str) -> None:
    declaration = ProductFormatDeclaration.model_validate(_payload(kind))
    assert declaration.format_kind == kind
    assert isinstance(declaration, ProductFormatDeclaration)


def test_a_format_kind_outside_the_closed_set_is_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        ProductFormatDeclaration.model_validate({"format_kind": "not_a_kind", "params": {}})
    assert exc.value.errors()[0]["type"] == "oneOf"
    # The open consumer type still reads a kind it does not know.
    assert Format(format_kind="not_a_kind", params={}).format_kind == "not_a_kind"


def test_per_kind_parameters_are_graded_against_the_branch_schema() -> None:
    with pytest.raises(ValidationError) as exc:
        ProductFormatDeclaration.model_validate(
            {"format_kind": "image", "params": {"width": "not-an-int", "height": 250}}
        )
    error = exc.value.errors()[0]
    assert error["type"] == "type"
    # The discriminator selects one branch, so the refusal names the field.
    assert "params.width" in error["msg"]


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical_formats_only": True, "v1_format_ref": [_REF]},
        {"canonical_formats_only": True, "v1_format_ref": None},
        {"capability_id": "creative-agent-only"},
        {"publisher_domain": "publisher.example.com"},
        {"tracker_execution_contract": None},
        {"format_shape": None},
        {"format_schema": _SCHEMA},
        {"locale_policy": {"accepted_language_ranges": ["en"]}},
    ],
)
def test_every_root_rule_rejects_with_its_schema_keyword(changes: dict[str, Any]) -> None:
    payload = {**_payload(), **changes}
    validator = get_named_validator("core/product-format-declaration.json")
    assert validator is not None
    expected = next(
        validator.evolve(schema={"allOf": validator.schema["allOf"]}).iter_errors(payload)
    )
    with pytest.raises(ValidationError) as exc:
        ProductFormatDeclaration.model_validate(payload)
    assert exc.value.errors()[0]["type"] == expected.validator


def test_capability_id_is_refused_rather_than_migrated() -> None:
    # ``Format`` keeps its reader tolerance and renames the key.
    assert Format(format_kind="image", params={}, capability_id="c1").format_option_id == "c1"
    # The declaration enforces the schema's allOf, which forbids the key.
    for build in (
        lambda: ProductFormatDeclaration(format_kind="image", params={}, capability_id="c1"),
        lambda: ProductFormatDeclaration.model_validate(
            {"format_kind": "image", "params": {}, "capability_id": "c1"}
        ),
    ):
        with pytest.raises(ValidationError) as exc:
            build()
        assert exc.value.errors()[0]["type"] == "not"


@pytest.mark.parametrize(
    "changes",
    [
        {"v1_format_ref": [_REF]},
        {"canonical_formats_only": True},
        {"publisher_domain": "publisher.example.com", "format_option_id": "image_1"},
        {"locale_policy": {"accepted_language_ranges": ["en"]}, "canonical_formats_only": True},
    ],
)
def test_conforming_root_rule_combinations_still_validate(changes: dict[str, Any]) -> None:
    payload = {**_payload(), **changes}
    assert ProductFormatDeclaration.model_validate(payload).format_kind == "image"


def test_custom_declarations_need_the_source_schemas_complete_contract() -> None:
    assert ProductFormatDeclaration.model_validate(_payload("custom")).format_kind == "custom"
    with pytest.raises(ValidationError):
        ProductFormatDeclaration.model_validate({"format_kind": "custom", "params": {}})
    without_projection = _payload("custom")
    without_projection.pop("canonical_formats_only")
    with pytest.raises(ValidationError):
        ProductFormatDeclaration.model_validate(without_projection)


def test_explicit_presence_is_distinguished_from_an_unset_default() -> None:
    # An unset default is absent from the wire document, so a rule keyed on
    # ``required`` does not fire for it; an explicitly supplied null does.
    assert ProductFormatDeclaration.model_validate(_payload()).format_kind == "image"
    with pytest.raises(ValidationError) as exc:
        ProductFormatDeclaration.model_validate({**_payload(), "tracker_execution_contract": None})
    assert exc.value.errors()[0]["type"] == "required"


def test_an_unset_default_is_not_read_as_present_when_a_model_is_revalidated() -> None:
    # Revalidating across classes dumps with ``exclude_unset``, so the nine
    # declared-but-unset fields must not arrive as explicit nulls — otherwise
    # allOf[2]'s ``else`` branch would refuse every non-custom declaration.
    generated = TypeAdapter(GeneratedProductFormatDeclaration).validate_python(_payload())
    assert ProductFormatDeclaration.model_validate(generated).format_kind == "image"


# --- behaviour the rebinding must not lose ------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{"params": {"nested": {"api_token": "credential"}}}, {"upstream_secret": "credential"}],
)
def test_credential_screening_survives_the_public_rebinding(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="credential-shaped key"):
        ProductFormatDeclaration.model_validate({**_payload(), **changes})


def test_validation_does_not_mutate_the_buyers_document() -> None:
    payload = {**_payload(), "v1_format_ref": [_REF]}
    original = copy.deepcopy(payload)
    ProductFormatDeclaration.model_validate(payload)
    assert payload == original


def test_the_declaration_can_be_used_in_adopter_models() -> None:
    class ProductCatalog(BaseModel):
        format_options: list[ProductFormatDeclaration]

    result = ProductCatalog.model_validate({"format_options": [_payload()]})
    assert isinstance(result.format_options[0], ProductFormatDeclaration)
    with pytest.raises(ValidationError):
        ProductCatalog.model_validate(
            {
                "format_options": [
                    {**_payload(), "canonical_formats_only": True, "v1_format_ref": [_REF]}
                ]
            }
        )


def test_validate_union_and_type_adapter_both_return_the_class() -> None:
    for result in (
        validate_union(ProductFormatDeclaration, _payload()),
        TypeAdapter(ProductFormatDeclaration).validate_python(_payload()),
    ):
        assert isinstance(result, ProductFormatDeclaration)


def test_the_wrap_serializer_tolerates_a_dict_at_a_canonical_position() -> None:
    # A field annotated with a canonical model can hold a plain dict, and the
    # wrap serializer then receives that dict rather than a model. Reading the
    # format-declaration capability off it must not raise.
    class Response(BaseModel):
        affected_packages: list[Package] | None = None

    response = Response.model_construct(affected_packages=[{"package_id": "pkg_1"}])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert response.model_dump() == {"affected_packages": [{"package_id": "pkg_1"}]}


def test_open_consumer_format_and_params_helper_remain_available() -> None:
    future = Format(format_kind="future_kind", params={})
    assert future.model_dump()["format_kind"] == "future_kind"
    image = Format(format_kind="image", params={"width": 300})
    assert image.params_as(CanonicalFormatImage).width == 300


def test_concurrent_authoring_validation_uses_independent_resolvers() -> None:
    def validate_batch(_: int) -> None:
        for _ in range(20):
            assert ProductFormatDeclaration.model_validate(_payload()).format_kind == "image"
            with pytest.raises(ValidationError):
                ProductFormatDeclaration.model_validate(
                    {**_payload(), "canonical_formats_only": True, "v1_format_ref": [_REF]}
                )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(validate_batch, range(4)))
