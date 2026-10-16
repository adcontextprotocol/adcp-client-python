"""Direct boundary scans preserve Pydantic's serialization semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    computed_field,
    field_serializer,
    model_serializer,
)
from pydantic_core import core_schema

from adcp.types import Format
from adcp.types import canonical_creative as boundary
from adcp.types.base import AdCPBaseModel


def _format(extension: AdCPBaseModel) -> Format:
    return Format(format_kind="image", params={}, extension=extension)


@pytest.mark.parametrize("key", ["access_token", "format_id"])
def test_computed_fields_are_scanned(key: str) -> None:
    class Computed(AdCPBaseModel):
        @computed_field
        @property
        def value(self) -> dict[str, str]:
            return {key: "hidden"}

    with pytest.raises(ValidationError):
        _format(Computed())


def test_excluded_fields_are_not_scanned() -> None:
    class Internal(AdCPBaseModel):
        access_token: str = Field(exclude=True)
        format_id: str = Field(exclude=True)
        keep: str

    result = _format(Internal(access_token="hidden", format_id="hidden", keep="visible"))
    assert result.model_dump()["extension"] == {"keep": "visible"}


def test_conditional_exclusions_are_respected() -> None:
    class Conditional(AdCPBaseModel):
        access_token: str = Field(exclude_if=lambda value: value == "internal")

    assert _format(Conditional(access_token="internal")).model_dump()["extension"] == {}
    with pytest.raises(ValidationError, match="credential-shaped key"):
        _format(Conditional(access_token="visible"))


def test_custom_model_serializers_are_scanned() -> None:
    class Custom(AdCPBaseModel):
        @model_serializer(mode="plain")
        def serialize(self) -> dict[str, str]:
            return {"access_token": "hidden"}

    with pytest.raises(ValidationError, match="credential-shaped key"):
        _format(Custom())


def test_custom_field_serializers_are_scanned() -> None:
    class Custom(AdCPBaseModel):
        value: str

        @field_serializer("value")
        def serialize(self, value: str) -> dict[str, str]:
            return {"access_token": value}

    with pytest.raises(ValidationError, match="credential-shaped key"):
        _format(Custom(value="hidden"))


def test_serializers_inside_container_annotations_are_scanned() -> None:
    class Custom(boundary.CanonicalBoundaryModel):
        values: list[Annotated[str, PlainSerializer(lambda value: {"access_token": value})]]

    with pytest.raises(ValidationError, match="credential-shaped key"):
        _format(Custom(values=["hidden"]))


def test_custom_type_schema_serializers_are_scanned() -> None:
    class CustomScalar(str):
        @classmethod
        def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
            return core_schema.str_schema(
                serialization=core_schema.plain_serializer_function_ser_schema(
                    lambda value: {"access_token": value}
                )
            )

    class Custom(boundary.CanonicalBoundaryModel):
        value: CustomScalar

    with pytest.raises(ValidationError, match="credential-shaped key"):
        _format(Custom(value="hidden"))


def test_custom_model_dump_is_scanned() -> None:
    class Custom(AdCPBaseModel):
        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            return {"access_token": "hidden"}

    with pytest.raises(ValidationError, match="credential-shaped key"):
        _format(Custom())


def test_default_serialization_aliases_are_scanned() -> None:
    class Aliased(AdCPBaseModel):
        model_config = ConfigDict(serialize_by_alias=True)
        value: str = Field(serialization_alias="access_token")

    with pytest.raises(ValidationError, match="credential-shaped key"):
        _format(Aliased(value="hidden"))


@pytest.mark.parametrize("kind", ["model", "dataclass"])
def test_nested_python_objects_are_converted_before_scanning(kind: str) -> None:
    class Nested(BaseModel):
        access_token: str

    @dataclass
    class Data:
        access_token: str

    class Extension(AdCPBaseModel):
        value: Any

    nested = Nested(access_token="hidden") if kind == "model" else Data(access_token="hidden")
    with pytest.raises(ValidationError, match="credential-shaped key"):
        _format(Extension(value={"nested": [nested]}))


def test_plain_models_keep_the_direct_scan() -> None:
    class Plain(AdCPBaseModel):
        keep: str
        internal: str = Field(exclude=True)

    instance = Plain(keep="visible", internal="hidden")
    assert boundary._direct_boundary_fields(Plain) == frozenset({"keep"})
    assert boundary._model_boundary_values(instance) == {"keep": "visible"}
    assert boundary._direct_boundary_fields(Format) is not None


def test_macro_graph_with_urls_keeps_the_direct_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    declaration = Format(
        format_kind="image",
        params={},
        macro_resolution_capabilities=[
            {
                "dialect": "adcp",
                "dialect_semantic": "CACHEBUSTER",
                "mapping_status": "verified_universal",
                "universal_semantic": "CACHEBUSTER",
                "operation": "translate_to_native",
                "performed_by": "seller",
                "supported_contexts": ["url_query_value"],
                "translation_target": {
                    "token": "%%CACHEBUSTER%%",
                    "dialect": "vendor",
                    "dialect_namespace": "https://example.com/macros",
                    "dialect_revision": "1",
                    "dialect_semantic": "CACHEBUSTER",
                    "mapping_status": "verified_universal",
                    "universal_semantic": "CACHEBUSTER",
                    "performed_by": "source_ad_server",
                    "encoding": {"kind": "none", "depth": 0},
                },
            }
        ],
    )

    def unexpected_dump(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Ordinary format and macro graphs should use the direct scan")

    monkeypatch.setattr(BaseModel, "model_dump", unexpected_dump)
    assert boundary._legacy_creative_identity_path(declaration) is None
