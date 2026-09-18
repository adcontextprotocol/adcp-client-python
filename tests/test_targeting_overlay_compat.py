"""Typed targeting composition across the rc.3 mutation boundaries (#1181)."""

from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin

import pytest
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, create_model
from pydantic.json_schema import SkipJsonSchema

import adcp.types
from adcp._null_clear import preserve_explicit_nulls
from adcp.types import (
    BuyProductsRequest,
    ControlMediaBuyRequest,
    CreateMediaBuyRequest,
    LegacyCreateMediaBuyRequest,
    PackageRequest,
    PackageUpdate,
    TargetingOverlay,
    UpdateMediaBuyRequest,
)
from adcp.types.generated_poc.core.targeting_input import TargetingOverlayInput as GeneratedInput
from adcp.types.generated_poc.media_buy.package_control import PackageControl
from adcp.types.generated_poc.media_buy.package_request import (
    PackageRequest as GeneratedPackageRequest,
)
from adcp.types.generated_poc.media_buy.package_update import (
    PackageUpdate as GeneratedPackageUpdate,
)
from adcp.types.generated_poc.media_buy.product_purchase_input import ProductPurchaseInput
from adcp.validation import validate_request

_NEW_PACKAGE = {"product_id": "product-1", "pricing_option_id": "price-1", "budget": 1000}
_EXISTING_PACKAGE = {"package_id": "package-1"}
_CONTAINERS = [
    pytest.param(GeneratedPackageRequest, _NEW_PACKAGE, id="generated-PackageRequest"),
    pytest.param(GeneratedPackageUpdate, _EXISTING_PACKAGE, id="generated-PackageUpdate"),
    pytest.param(PackageControl, _EXISTING_PACKAGE, id="PackageControl"),
    pytest.param(ProductPurchaseInput, _NEW_PACKAGE, id="ProductPurchaseInput"),
    # The public canonical-creative facades copy fields from these two
    # generated models. Patching only the generated originals misses adopters.
    pytest.param(PackageRequest, _NEW_PACKAGE, id="public-PackageRequest"),
    pytest.param(PackageUpdate, _EXISTING_PACKAGE, id="public-PackageUpdate"),
]
_REQUEST_PATHS = [
    pytest.param("create_media_buy", "packages", id="create"),
    pytest.param("update_media_buy", "packages", id="update"),
    pytest.param("update_media_buy", "new_packages", id="update-new-packages"),
    pytest.param("control_media_buy", "packages", id="control"),
    pytest.param("buy_products", "purchases", id="direct-buy"),
]


@pytest.fixture(params=["TargetingOverlayInput", "TargetingOverlay"], ids=["input", "beta14"])
def overlay_class(request: pytest.FixtureRequest) -> type[BaseModel]:
    # Resolve from the public surface inside the fixture so the original
    # missing export is a test failure rather than a collection failure.
    base = getattr(adcp.types, request.param)

    class InternalOverlay(base):
        model_config = ConfigDict(extra="forbid")
        internal_id: str = Field(exclude=True)
        internal_null: str | None = Field(default=None, exclude=True)
        _routing: dict[str, str] = PrivateAttr(default_factory=dict)

        def route(self) -> str:
            return self._routing[self.internal_id]

    return InternalOverlay


def _overlay(overlay_class: type[BaseModel], dimensions: dict[str, Any]) -> Any:
    overlay = overlay_class.model_validate(
        {**dimensions, "internal_id": "route-1181", "internal_null": None}
    )
    overlay._routing["route-1181"] = "seller-1"
    return overlay


@pytest.mark.parametrize("container,required", _CONTAINERS)
def test_public_overlay_subclass_preserves_identity_and_internal_behavior(
    overlay_class: type[BaseModel], container: type[BaseModel], required: dict[str, Any]
) -> None:
    overlay = _overlay(overlay_class, {"geo_regions": ["US-NY"]})
    result = container.model_validate({**required, "targeting_overlay": overlay})

    assert result.targeting_overlay is overlay
    assert result.targeting_overlay.route() == "seller-1"
    # Mutation variants retain their collection RootModel wrappers; beta.14
    # objects keep their existing lists. Neither arm is converted into the other.
    if isinstance(overlay, TargetingOverlay):
        assert [region.root for region in overlay.geo_regions] == ["US-NY"]
    else:
        assert [region.root for region in overlay.geo_regions.root] == ["US-NY"]
    assert result.model_dump(mode="json", exclude_none=True)["targeting_overlay"] == {
        "geo_regions": ["US-NY"]
    }


@pytest.mark.parametrize("container,required", _CONTAINERS)
def test_exact_legacy_overlay_is_accepted(
    container: type[BaseModel], required: dict[str, Any]
) -> None:
    overlay = TargetingOverlay(geo_regions=["US-NY"])
    result = container.model_validate({**required, "targeting_overlay": overlay})
    assert result.targeting_overlay is overlay


@pytest.mark.parametrize("container,required", _CONTAINERS)
@pytest.mark.parametrize("dimensions", [{}, {"geo_regions": ["US-NY"]}, {"geo_countries": None}])
def test_raw_dict_keeps_input_materialization(
    container: type[BaseModel], required: dict[str, Any], dimensions: dict[str, Any]
) -> None:
    result = container.model_validate({**required, "targeting_overlay": dimensions})
    assert type(result.targeting_overlay) is GeneratedInput
    assert (
        result.targeting_overlay.model_dump(mode="json", exclude_unset=True, exclude_none=False)
        == dimensions
    )


def _request(task: str, collection: str, overlay: Any, *, legacy_create: bool = False) -> BaseModel:
    params: dict[str, Any] = {
        "idempotency_key": "targeting-compat-1181",
        "account": {"account_id": "account-1"},
    }
    if task in {"create_media_buy", "buy_products"}:
        params.update(
            brand={"brand_id": "brand_1", "domain": "brand.example"},
            start_time="2026-10-01T00:00:00Z",
            end_time="2026-10-31T00:00:00Z",
        )
        item = _NEW_PACKAGE
    else:
        params["media_buy_id"] = "buy-1"
        item = _NEW_PACKAGE if collection == "new_packages" else _EXISTING_PACKAGE
    if task == "control_media_buy":
        params["revision"] = 1
    elif task == "buy_products":
        params["feed_version"] = "feed-1"
    params[collection] = [{**item, "targeting_overlay": overlay}]
    models = {
        "create_media_buy": LegacyCreateMediaBuyRequest if legacy_create else CreateMediaBuyRequest,
        "update_media_buy": UpdateMediaBuyRequest,
        "control_media_buy": ControlMediaBuyRequest,
        "buy_products": BuyProductsRequest,
    }
    return models[task].model_validate(params)


def test_legacy_create_parent_refreshes_its_cached_nested_validator(
    overlay_class: type[BaseModel],
) -> None:
    overlay = _overlay(overlay_class, {"geo_countries": None})
    request = _request("create_media_buy", "packages", overlay, legacy_create=True)
    assert request.packages[0].targeting_overlay is overlay
    wire = preserve_explicit_nulls(
        request,
        request.model_dump(mode="json", exclude_none=True),
        task_name="create_media_buy",
        version="3.2.0-rc.3",
    )
    assert wire["packages"][0]["targeting_overlay"] == {"geo_countries": None}
    outcome = validate_request("create_media_buy", wire, version="3.2.0-rc.3")
    assert outcome.valid, outcome.issues
    assert outcome.variant == "request"


@pytest.mark.parametrize("task,collection", _REQUEST_PATHS)
@pytest.mark.parametrize("clear", [False, True], ids=["inherit", "clear"])
def test_subclass_null_intent_survives_request_serialization(
    overlay_class: type[BaseModel], task: str, collection: str, clear: bool
) -> None:
    dimensions: dict[str, Any] = {"geo_regions": ["US-NY"]}
    if clear:
        dimensions["geo_countries"] = None
    overlay = _overlay(overlay_class, dimensions)
    request = _request(task, collection, overlay)
    assert getattr(request, collection)[0].targeting_overlay is overlay
    params = request.model_dump(mode="json", exclude_none=True)
    assert params[collection][0]["targeting_overlay"] == {"geo_regions": ["US-NY"]}

    wire = preserve_explicit_nulls(request, params, task_name=task, version="3.2.0-rc.3")

    assert wire[collection][0]["targeting_overlay"] == dimensions
    outcome = validate_request(task, wire, version="3.2.0-rc.3")
    assert outcome.valid, outcome.issues
    assert outcome.variant == "request"


@pytest.mark.parametrize("overlay_name", ["TargetingOverlayInput", "TargetingOverlay"])
@pytest.mark.parametrize("task,collection", _REQUEST_PATHS)
@pytest.mark.parametrize(
    "dimensions",
    [
        {"geo_regions": ["US-NY"]},
        {"geo_regions": ["US-NY"], "geo_countries": None},
        {},
        {"geo_regions": ["US-NY"], "geo_countries": ["US"]},
    ],
    ids=["omitted-country", "null-country", "empty-overlay", "populated-country"],
)
def test_exact_overlay_objects_preserve_wire_intent(
    overlay_name: str, task: str, collection: str, dimensions: dict[str, Any]
) -> None:
    overlay_type = getattr(adcp.types, overlay_name)
    overlay = overlay_type.model_validate(dimensions)
    assert type(overlay) is overlay_type
    # Exact objects have no declared excluded extension fields. A private
    # runtime hint must also remain off the wire; excluded subclass fields
    # are covered separately above.
    overlay._routing_hint = "seller-1"
    request = _request(task, collection, overlay)
    assert getattr(request, collection)[0].targeting_overlay is overlay
    assert overlay._routing_hint == "seller-1"

    wire = preserve_explicit_nulls(
        request,
        request.model_dump(mode="json", exclude_none=True),
        task_name=task,
        version="3.2.0-rc.3",
    )

    assert wire[collection][0]["targeting_overlay"] == dimensions
    outcome = validate_request(task, wire, version="3.2.0-rc.3")
    assert outcome.valid, outcome.issues
    assert outcome.variant == "request"


@pytest.mark.parametrize("task,collection", _REQUEST_PATHS)
def test_raw_dict_keeps_input_materialization_in_enclosing_request(
    task: str, collection: str
) -> None:
    request = _request(task, collection, {"geo_countries": None})
    assert type(getattr(request, collection)[0].targeting_overlay) is GeneratedInput


@pytest.mark.parametrize("container,required", _CONTAINERS)
def test_compatibility_union_is_explicitly_input_first_and_left_to_right(
    container: type[BaseModel], required: dict[str, Any]
) -> None:
    annotation = container.model_fields["targeting_overlay"].annotation
    assert get_origin(annotation) is Annotated
    union, field = get_args(annotation)
    assert get_args(union) == (GeneratedInput, SkipJsonSchema[TargetingOverlay], type(None))
    assert any(
        getattr(metadata, "union_mode", None) == "left_to_right" for metadata in field.metadata
    )


@pytest.mark.parametrize("container,required", _CONTAINERS)
def test_container_json_schema_keeps_only_mutation_input_and_null(
    container: type[BaseModel], required: dict[str, Any]
) -> None:
    schema = container.model_json_schema()
    assert schema["properties"]["targeting_overlay"]["anyOf"] == [
        {"$ref": "#/$defs/TargetingOverlayInput"},
        {"type": "null"},
    ]


@pytest.mark.parametrize(
    "annotation",
    [
        None,  # Field removed entirely.
        TargetingOverlay | None,  # Reverted to the resolved-state schema.
        GeneratedInput,  # Optional shape changed.
        list[GeneratedInput] | None,  # Contains Input, but at the wrong level.
        GeneratedInput | str | None,  # Unexpected additional arm.
        GeneratedInput | TargetingOverlay | None,  # Already widened upstream.
    ],
)
def test_targeting_patch_fails_closed_on_generated_shape_drift(annotation: Any) -> None:
    from adcp.types._forward_compat import _patch_targeting_overlay

    fields = {} if annotation is None else {"targeting_overlay": (annotation, None)}
    model = create_model("DriftedPackage", **fields)
    original = dict(model.model_fields)
    with pytest.raises(RuntimeError, match=r"DriftedPackage.targeting_overlay lost its"):
        _patch_targeting_overlay(model)
    assert model.model_fields == original


def test_targeting_patch_preserves_generated_field_metadata() -> None:
    from adcp.types._forward_compat import _patch_targeting_overlay

    model = create_model(
        "InputPackage",
        targeting_overlay=(
            GeneratedInput | None,
            Field(default=None, alias="targeting", title="Targeting", description="Mutation input"),
        ),
    )
    _patch_targeting_overlay(model)
    field = model.model_fields["targeting_overlay"]
    assert field.default is None
    assert field.alias == "targeting"
    assert field.title == "Targeting"
    assert field.description == "Mutation input"
    assert type(model.model_validate({"targeting": {}}).targeting_overlay) is GeneratedInput
