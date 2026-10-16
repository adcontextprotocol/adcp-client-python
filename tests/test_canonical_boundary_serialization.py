"""Canonical serializers avoid redundant public-method cleanup (#1519)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    field_serializer,
    model_serializer,
)
from pydantic_core import PydanticSerializationError, core_schema

from adcp.types import Format, Product, ProductFormatDeclaration
from adcp.types import canonical_creative as boundary
from adcp.types.base import AdCPBaseModel


def _declaration() -> Format:
    return Format.model_construct(
        format_kind="image",
        params={"width": 300, "nested": [{"format_id": "hidden", "keep": "yes"}]},
        vendor={"agent_url": "https://legacy.example", "id": "hidden"},
    )


@pytest.mark.parametrize("method", ["python", "json", "adapter_python", "adapter_json", "parent"])
def test_nested_canonical_models_are_cleaned_once_per_model(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Catalog(boundary.CanonicalBoundaryModel):
        options: list[Format]

    class Envelope(BaseModel):
        catalog: Catalog

    catalog = Catalog.model_construct(options=[_declaration(), _declaration()])
    strip = boundary.strip_legacy_creative_identity
    roots = 0

    def counted(value: Any, **kwargs: Any) -> Any:
        nonlocal roots
        if kwargs.get("_path", "$") == "$":
            roots += 1
        return strip(value, **kwargs)

    monkeypatch.setattr(boundary, "strip_legacy_creative_identity", counted)
    if method == "python":
        result = catalog.model_dump()
    elif method == "json":
        result = json.loads(catalog.model_dump_json())
    elif method == "adapter_python":
        result = TypeAdapter(Catalog).dump_python(catalog, exclude_none=True)
    elif method == "adapter_json":
        result = json.loads(TypeAdapter(Catalog).dump_json(catalog, exclude_none=True))
    else:
        result = Envelope(catalog=catalog).model_dump(exclude_none=True)["catalog"]

    # Two declarations and their catalog each enforce their own boundary.
    # The public dump methods must not add a fourth pass over the full tree.
    assert roots == 3
    assert result == {
        "options": [
            {
                "format_kind": "image",
                "params": {"width": 300, "nested": [{"keep": "yes"}]},
                "vendor": {"id": "hidden"},
            }
        ]
        * 2
    }


def test_serialization_flag_is_reset_after_an_error() -> None:
    class Failing(boundary.CanonicalBoundaryModel):
        value: int

        @field_serializer("value")
        def fail(self, value: int) -> int:
            raise ValueError("serialization failed")

    with pytest.raises(PydanticSerializationError, match="serialization failed"):
        Failing(value=1).model_dump()
    assert _declaration().model_dump()["params"]["nested"] == [{"keep": "yes"}]


def test_concurrent_dumps_each_enforce_the_boundary() -> None:
    barrier = Barrier(2)

    class Concurrent(boundary.CanonicalBoundaryModel):
        payload: dict[str, Any]

        @field_serializer("payload")
        def synchronize(self, value: dict[str, Any]) -> dict[str, Any]:
            barrier.wait(timeout=10)
            return value

    obj = Concurrent.model_construct(payload={"format_id": "hidden", "keep": True})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: obj.model_dump(), range(2)))
    assert results == [{"payload": {"keep": True}}] * 2


def test_dump_options_and_caller_context_are_preserved() -> None:
    class Labeled(boundary.CanonicalBoundaryModel):
        label: str = Field(serialization_alias="display_label")
        unused: str | None = None

        @field_serializer("label")
        def prefix(self, value: str, info: Any) -> str:
            return info.context["prefix"] + value

    context = {"prefix": "é"}
    obj = Labeled(label="x")
    options = {"by_alias": True, "exclude_unset": True, "context": context}
    assert obj.model_dump(**options) == {"display_label": "éx"}
    assert obj.model_dump_json(indent=2, ensure_ascii=True, **options) == (
        '{\n  "display_label": "\\u00e9x"\n}'
    )
    assert context == {"prefix": "é"}


@pytest.mark.parametrize(
    "method", ["model_dump", "model_dump_json", "adapter_python", "adapter_json"]
)
def test_explicit_dumps_inside_custom_serializers_keep_their_own_boundary(method: str) -> None:
    results = []

    class Embedded(boundary.CanonicalBoundaryModel):
        declaration: Format

        @field_serializer("declaration")
        def embed(self, value: Format) -> str:
            if method == "adapter_python":
                dumped = TypeAdapter(Format).dump_python(value, exclude_none=True)
            elif method == "adapter_json":
                dumped = TypeAdapter(Format).dump_json(value, exclude_none=True).decode()
            else:
                dumped = getattr(value, method)()
            results.append(dumped)
            return dumped if isinstance(dumped, str) else json.dumps(dumped)

    obj = Embedded.model_construct(declaration=_declaration())
    embedded = json.loads(obj.model_dump()["declaration"])
    assert embedded["params"]["nested"] == [{"keep": "yes"}]
    assert "agent_url" not in embedded["vendor"]
    assert len(results) == 1


@pytest.mark.parametrize("method", ["model_dump", "model_dump_json"])
def test_replacement_model_serializers_keep_the_public_dump_boundary(method: str) -> None:
    class Customized(Format):
        @model_serializer(mode="plain")
        def custom(self) -> dict[str, Any]:
            return {
                "format_kind": self.format_kind,
                "format_id": "hidden",
                "params": {"format_ids": ["hidden"], "keep": True},
            }

    result = getattr(Customized(format_kind="image", params={}), method)()
    if isinstance(result, str):
        result = json.loads(result)
    assert result == {"format_kind": "image", "params": {"keep": True}}


@pytest.mark.parametrize("method", ["model_dump", "model_dump_json"])
def test_custom_core_serializers_keep_the_public_dump_boundary(method: str) -> None:
    class Customized(Format):
        @classmethod
        def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
            schema = handler(source)
            schema["serialization"] = core_schema.plain_serializer_function_ser_schema(
                lambda value: {"format_kind": value.format_kind, "format_id": "hidden"}
            )
            return schema

    result = getattr(Customized(format_kind="image", params={}), method)()
    if isinstance(result, str):
        result = json.loads(result)
    assert result == {"format_kind": "image"}


def test_product_validation_does_not_serialize_nested_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declaration = ProductFormatDeclaration(
        format_kind="image", params={"width": 300, "height": 250}
    )

    def unexpected_dump(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Input scanning must not serialize nested models")

    monkeypatch.setattr(BaseModel, "model_dump", unexpected_dump)
    product = Product.model_validate(
        {
            "product_id": "display",
            "name": "Display",
            "description": "Display inventory",
            "publisher_properties": [{"publisher_domain": "example.com", "selection_type": "all"}],
            "channels": ["display"],
            "delivery_type": "guaranteed",
            "reporting_capabilities": {
                "available_reporting_frequencies": ["daily"],
                "expected_delay_minutes": 60,
                "timezone": "UTC",
                "supports_webhooks": False,
                "available_metrics": ["impressions"],
                "date_range_support": "date_range",
            },
            "format_options": [declaration],
            "pricing_options": [
                {
                    "pricing_option_id": "cpm",
                    "pricing_model": "cpm",
                    "currency": "USD",
                    "fixed_price": 1,
                }
            ],
        }
    )
    assert product.format_options[0] is declaration


@pytest.mark.parametrize("canonical", [False, True])
def test_validation_still_scans_unvalidated_or_mutated_nested_models(canonical: bool) -> None:
    class Extension(AdCPBaseModel):
        payload: dict[str, Any]

    if canonical:
        declaration = Format(format_kind="image", params={})
        declaration.params["format_id"] = "hidden"
        nested: AdCPBaseModel = declaration
    else:
        nested = Extension.model_construct(payload={"format_id": "hidden"})
    with pytest.raises(ValidationError, match="legacy creative identity"):
        Format(format_kind="image", params={}, extension=nested)


def test_credential_scan_reads_nested_model_fields_and_extras_without_serializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Extension(boundary.CanonicalBoundaryModel):
        payload: dict[str, Any]

    def unexpected_dump(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Credential scanning must not serialize nested models")

    monkeypatch.setattr(BaseModel, "model_dump", unexpected_dump)
    for nested in (
        Extension.model_construct(payload={"api_token": "hidden"}),
        Extension.model_construct(payload={}, vendor={"password": "hidden"}),
    ):
        with pytest.raises(ValidationError, match="credential-shaped key"):
            Format(format_kind="image", params={}, extension=nested)


def test_nested_optional_model_fields_keep_the_dump_default_null_semantics() -> None:
    class Extension(AdCPBaseModel):
        format_id: str | None = None
        access_token: str | None = None

    declaration = Format(format_kind="image", params={}, extension=Extension())
    assert declaration.model_dump()["extension"] == {}

    # Nulls in a plain extension dictionary still carry the forbidden key.
    with pytest.raises(ValidationError, match="legacy creative identity"):
        Format(format_kind="image", params={}, extension={"format_id": None})
    with pytest.raises(ValidationError, match="credential-shaped key"):
        Format(format_kind="image", params={}, extension={"access_token": None})
