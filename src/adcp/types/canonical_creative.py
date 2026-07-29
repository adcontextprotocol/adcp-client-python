"""Canonical-first creative models for the Python 7 public API.

The generated protocol models intentionally remain wire-faithful through the
AdCP 3.x transition and therefore contain legacy named-format identity.  They
are exposed from :mod:`adcp.types.legacy`.  This module provides the primary
application-facing models: legacy identity is absent from their declared
fields, JSON Schema, and serialized output at every nesting depth.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Sequence
from enum import Enum
from typing import Annotated, Any, ClassVar, TypeVar

from pydantic import (
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    WithJsonSchema,
    create_model,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import CoreSchema

from adcp.types._str_enum import StrEnum
from adcp.types.base import AdCPBaseModel
from adcp.types.generated_poc.core.canonical_format_kind import CanonicalFormatKind
from adcp.types.generated_poc.core.creative_asset import CreativeAsset2 as _CanonicalCreativeWire
from adcp.types.generated_poc.core.creative_filters import CreativeFilters as _LegacyCreativeFilters
from adcp.types.generated_poc.core.creative_manifest import (
    CreativeManifest2 as _CanonicalCreativeManifestWire,
)
from adcp.types.generated_poc.core.creative_variant import CreativeVariant as _LegacyCreativeVariant
from adcp.types.generated_poc.core.package import Package as _LegacyPackage
from adcp.types.generated_poc.core.placement import Placement as _LegacyPlacement
from adcp.types.generated_poc.core.platform_extension_ref import PlatformExtensionReference
from adcp.types.generated_poc.core.pricing_option import PricingOption as _LegacyPricingOption
from adcp.types.generated_poc.core.product import Product as _LegacyProduct
from adcp.types.generated_poc.core.product_filters import ProductFilters as _LegacyProductFilters
from adcp.types.generated_poc.core.product_format_declaration import SellerPreference
from adcp.types.generated_poc.creative.get_creative_delivery_response import (
    Creative as _LegacyDeliveryCreative,
)
from adcp.types.generated_poc.creative.get_creative_delivery_response import (
    GetCreativeDeliveryResponse as _LegacyGetCreativeDeliveryResponse,
)
from adcp.types.generated_poc.creative.list_creatives_request import (
    ListCreativesRequest as _LegacyListCreativesRequest,
)
from adcp.types.generated_poc.creative.list_creatives_response import (
    Creatives1 as _CanonicalListedCreative,
)
from adcp.types.generated_poc.creative.list_creatives_response import (
    ListCreativesResponse as _LegacyListCreativesResponse,
)
from adcp.types.generated_poc.creative.sync_creatives_request import (
    SyncCreativesRequest as _LegacySyncCreativesRequest,
)
from adcp.types.generated_poc.enums.channels import MediaChannel
from adcp.types.generated_poc.media_buy.create_media_buy_request import (
    CreateMediaBuyRequest as _LegacyCreateMediaBuyRequest,
)
from adcp.types.generated_poc.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse1 as _LegacyCreateMediaBuyResponse1,
)
from adcp.types.generated_poc.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse2 as _LegacyCreateMediaBuyResponse2,
)
from adcp.types.generated_poc.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse3 as _LegacyCreateMediaBuyResponse3,
)
from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (
    GetMediaBuyDeliveryResponse as _LegacyGetMediaBuyDeliveryResponse,
)
from adcp.types.generated_poc.media_buy.get_media_buys_response import (
    GetMediaBuysResponse as _LegacyGetMediaBuysResponse,
)
from adcp.types.generated_poc.media_buy.get_media_buys_response import (
    MediaBuy as _LegacyMediaBuy,
)
from adcp.types.generated_poc.media_buy.get_media_buys_response import (
    Package as _LegacyMediaBuyPackage,
)
from adcp.types.generated_poc.media_buy.get_products_request import (
    GetProductsRequest as _LegacyGetProductsRequest,
)
from adcp.types.generated_poc.media_buy.get_products_response import (
    GetProductsResponse as _LegacyGetProductsResponse,
)
from adcp.types.generated_poc.media_buy.package_request import (
    PackageRequest as _LegacyPackageRequest,
)
from adcp.types.generated_poc.media_buy.package_update import PackageUpdate as _LegacyPackageUpdate
from adcp.types.generated_poc.media_buy.update_media_buy_request import (
    UpdateMediaBuyRequest as _LegacyUpdateMediaBuyRequest,
)
from adcp.types.generated_poc.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse1 as _LegacyUpdateMediaBuyResponse1,
)
from adcp.types.generated_poc.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse2 as _LegacyUpdateMediaBuyResponse2,
)
from adcp.types.generated_poc.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse3 as _LegacyUpdateMediaBuyResponse3,
)
from adcp.types.legacy import LegacyFormatId
from adcp.types.media_buy_status_helpers import (
    MEDIA_BUY_LEGACY_STATUS_VALUES,
    unwrap_enum_value,
)

_LEGACY_IDENTITY_KEY = re.compile(r"(^|_)(?:format_ids?|v1_format_ref)($|_)")
_CREDENTIAL_SHAPED_KEY_SUFFIXES = (
    "credential",
    "credentials",
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "bearer",
)


def _walk_for_credential_keys(value: Any, *, path: str = "") -> str | None:
    """Return the first credential-shaped key path under ``value``."""

    if isinstance(value, dict):
        for key, nested in value.items():
            nested_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and any(
                key.lower().endswith(suffix) for suffix in _CREDENTIAL_SHAPED_KEY_SUFFIXES
            ):
                return nested_path
            found = _walk_for_credential_keys(nested, path=nested_path)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            found = _walk_for_credential_keys(nested, path=f"{path}[{index}]")
            if found is not None:
                return found
    elif isinstance(value, AdCPBaseModel):
        return _walk_for_credential_keys(value.model_dump(mode="python"), path=path)
    return None


def is_legacy_creative_identity_key(key: object) -> bool:
    """Return whether *key* names legacy creative routing identity."""

    return isinstance(key, str) and bool(_LEGACY_IDENTITY_KEY.search(key))


def strip_legacy_creative_identity(value: Any) -> Any:
    """Recursively remove legacy creative identity from a serialized value.

    This is deliberately a runtime boundary rather than a typing convention.
    Unknown extension bags are traversed too, so ``extra='allow'`` can never be
    used to smuggle ``format_id`` or ``format_ids`` through a primary model.
    """

    if isinstance(value, dict):
        keys = set(value)
        legacy_tuple = {"agent_url", "id"} <= keys and keys <= {
            "agent_url",
            "id",
            "width",
            "height",
            "duration_ms",
        }
        return {
            key: strip_legacy_creative_identity(item)
            for key, item in value.items()
            if not is_legacy_creative_identity_key(key)
            and not (legacy_tuple and key == "agent_url")
        }
    if isinstance(value, list):
        return [strip_legacy_creative_identity(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_legacy_creative_identity(item) for item in value)
    return value


def _legacy_creative_identity_path(
    value: Any,
    *,
    path: str = "$",
    allow_root_v1_ref: bool = False,
) -> str | None:
    """Locate legacy creative identity in model input without mutating it."""

    if isinstance(value, AdCPBaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, dict):
        keys = set(value)
        if {"agent_url", "id"} <= keys and keys <= {
            "agent_url",
            "id",
            "width",
            "height",
            "duration_ms",
        }:
            return f"{path}.agent_url"
        for key, nested in value.items():
            if is_legacy_creative_identity_key(key):
                if allow_root_v1_ref and path == "$" and key == "v1_format_ref":
                    continue
                return f"{path}.{key}"
            found = _legacy_creative_identity_path(nested, path=f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            found = _legacy_creative_identity_path(nested, path=f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _looks_like_legacy_format_tuple(schema: dict[str, Any], properties: dict[str, Any]) -> bool:
    keys = set(properties)
    title = str(schema.get("title", "")).lower()
    return {"agent_url", "id"} <= keys and (
        "format" in title or keys <= {"agent_url", "id", "width", "height", "duration_ms"}
    )


def _sanitize_schema_node(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_schema_node(item) for item in value]
    if not isinstance(value, dict):
        return value

    node = {key: _sanitize_schema_node(item) for key, item in value.items()}
    properties = node.get("properties")
    removed: set[str] = set()
    if isinstance(properties, dict):
        legacy_tuple = _looks_like_legacy_format_tuple(node, properties)
        cleaned: dict[str, Any] = {}
        for key, item in properties.items():
            if is_legacy_creative_identity_key(key) or (legacy_tuple and key == "agent_url"):
                removed.add(key)
                continue
            cleaned[key] = item
        node["properties"] = cleaned

    required = node.get("required")
    if isinstance(required, list):
        node["required"] = [
            key
            for key in required
            if key not in removed and not is_legacy_creative_identity_key(key)
        ]
    return node


def sanitize_canonical_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a defensive deep copy with legacy identity removed."""

    return _sanitize_schema_node(copy.deepcopy(schema))


class CanonicalBoundaryModel(AdCPBaseModel):
    """Base class enforcing the primary canonical runtime boundary."""

    model_config = ConfigDict(extra="allow", defer_build=True)
    __adcp_canonical_creative_model__: ClassVar[bool] = True

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_creative_identity(cls, value: Any) -> Any:
        found = _legacy_creative_identity_path(
            value,
            allow_root_v1_ref=cls.__name__ == "Format",
        )
        if found is not None:
            raise ValueError(
                f"{found} contains legacy creative identity; use an explicit Legacy* model"
            )
        return value

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("serialize_as_any", False)
        return strip_legacy_creative_identity(super().model_dump(**kwargs))

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("serialize_as_any", False)
        raw = super().model_dump_json(**kwargs)
        clean = strip_legacy_creative_identity(json.loads(raw))
        indent = kwargs.get("indent")
        return json.dumps(
            clean,
            ensure_ascii=False,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return sanitize_canonical_schema(super().model_json_schema(*args, **kwargs))

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> dict[str, Any]:
        """Enforce the boundary for TypeAdapter and containing-model schemas."""

        schema = sanitize_canonical_schema(handler(core_schema))
        # TypeAdapter assembles shared definitions outside the model's returned
        # node. Mutate the active generator's definition registry as well so
        # unreachable generated legacy definitions cannot leak into the final
        # recursive schema document.
        generator = handler.generate_json_schema
        for key, definition in list(generator.definitions.items()):
            generator.definitions[key] = sanitize_canonical_schema(definition)
        return schema


def _field_definitions(
    source: type[AdCPBaseModel],
    *,
    exclude: frozenset[str] = frozenset(),
    overrides: dict[str, tuple[Any, Any]] | None = None,
) -> dict[str, tuple[Any, Any]]:
    fields: dict[str, tuple[Any, Any]] = {}
    for name, info in source.model_fields.items():
        if name in exclude or is_legacy_creative_identity_key(name):
            continue
        fields[name] = (info.annotation, copy.deepcopy(info))
    fields.update(overrides or {})
    return fields


def _serialize_canonical_model(
    self: CanonicalBoundaryModel,
    handler: SerializerFunctionWrapHandler,
) -> Any:
    """Enforce the boundary for nested and TypeAdapter serialization too."""

    return strip_legacy_creative_identity(handler(self))


def _canonical_clone(
    name: str,
    source: type[AdCPBaseModel],
    *,
    exclude: frozenset[str] = frozenset(),
    overrides: dict[str, tuple[Any, Any]] | None = None,
) -> type[CanonicalBoundaryModel]:
    model = create_model(  # type: ignore[call-overload]
        name,
        __base__=CanonicalBoundaryModel,
        __module__=__name__,
        __validators__={
            "_serialize_canonical": model_serializer(mode="wrap")(_serialize_canonical_model)
        },
        **_field_definitions(source, exclude=exclude, overrides=overrides),
    )
    return model


_CanonicalParamsT = TypeVar("_CanonicalParamsT", bound=AdCPBaseModel)
CanonicalPricingOption = Annotated[
    _LegacyPricingOption,
    WithJsonSchema(
        {
            "type": "object",
            "description": "Pricing option; legacy format-scoped vendor fields are unavailable.",
        }
    ),
]


class Format(CanonicalBoundaryModel):
    """Canonical format declaration exposed as ``adcp.Format``."""

    format_option_id: str | None = Field(
        default=None,
        description="Stable option identifier within the product or publisher namespace.",
    )
    publisher_domain: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$",
    )
    display_name: str | None = None
    applies_to_channels: list[MediaChannel] | None = None
    seller_preference: SellerPreference | None = None
    canonical_formats_only: bool | None = None
    experimental: bool | None = None
    format_shape: str | None = None
    format_schema: PlatformExtensionReference | None = None
    format_kind: CanonicalFormatKind
    params: dict[str, Any]

    _legacy_format_refs: list[LegacyFormatId] = PrivateAttr(default_factory=list)

    _serialize_canonical = model_serializer(mode="wrap")(_serialize_canonical_model)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_conflicts_and_credentials(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("canonical_formats_only") is True and data.get("v1_format_ref"):
            raise ValueError(
                "canonical_formats_only=True is mutually exclusive with legacy v1_format_ref"
            )
        for bag_name, bag in (
            ("params", data.get("params")),
            (
                "extras",
                {
                    key: value
                    for key, value in data.items()
                    if key not in cls.model_fields and key != "v1_format_ref"
                },
            ),
        ):
            found = _walk_for_credential_keys(bag, path=bag_name)
            if found is not None:
                raise ValueError(
                    f"{found!r} matches a credential-shaped key suffix and cannot "
                    "be stored in a canonical format declaration"
                )
        return data

    def __init__(self, **data: Any) -> None:
        refs = data.get("v1_format_ref")
        if "capability_id" in data and "format_option_id" not in data:
            data["format_option_id"] = data.pop("capability_id")
        super().__init__(**data)
        if self.__pydantic_extra__ is not None:
            self.__pydantic_extra__.pop("v1_format_ref", None)
        if refs:
            self._legacy_format_refs = [LegacyFormatId.model_validate(ref) for ref in refs]

    @property
    def legacy_format_refs(self) -> tuple[LegacyFormatId, ...]:
        """Original tuples retained only for an explicit compatibility adapter."""

        return tuple(self._legacy_format_refs)

    def params_as(self, canonical_type: type[_CanonicalParamsT]) -> _CanonicalParamsT:
        """Validate the open parameter bag against a typed canonical model."""

        return canonical_type.model_validate(self.params)

    @model_validator(mode="after")
    def _validate_custom_shape(self) -> Format:
        if self.format_kind is CanonicalFormatKind.custom:
            if not self.format_shape:
                raise ValueError("custom formats require format_shape")
        elif self.format_shape is not None or self.format_schema is not None:
            raise ValueError("format_shape and format_schema are only valid for custom formats")
        return self


ProductFormatDeclaration = Format


Placement = _canonical_clone(
    "Placement",
    _LegacyPlacement,
    overrides={
        "format_options": (
            list[Format] | None,
            Field(default=None, min_length=1),
        )
    },
)

Product = _canonical_clone(
    "Product",
    _LegacyProduct,
    overrides={
        "format_options": (
            list[Format],
            Field(min_length=1, description="Canonical creative formats accepted by this product."),
        ),
        "placements": (list[Placement] | None, Field(default=None, min_length=1)),
        "pricing_options": (list[CanonicalPricingOption], Field(min_length=1)),
    },
)

CreativeAsset = _canonical_clone(
    "CreativeAsset",
    _CanonicalCreativeWire,
    overrides={"format_kind": (CanonicalFormatKind, Field())},
)

Creative = _canonical_clone(
    "Creative",
    _CanonicalListedCreative,
    overrides={"format_kind": (CanonicalFormatKind, Field())},
)

CreativeManifest = _canonical_clone("CreativeManifest", _CanonicalCreativeManifestWire)

CreativeVariant = _canonical_clone(
    "CreativeVariant",
    _LegacyCreativeVariant,
    overrides={"manifest": (CreativeManifest | None, Field(default=None))},
)

DeliveryCreative = _canonical_clone(
    "DeliveryCreative",
    _LegacyDeliveryCreative,
    overrides={
        "format_kind": (CanonicalFormatKind | None, Field(default=None)),
        "variants": (list[CreativeVariant], Field()),
    },
)

CreativeFilters = _canonical_clone("CreativeFilters", _LegacyCreativeFilters)
ProductFilters = _canonical_clone("ProductFilters", _LegacyProductFilters)

PackageRequest = _canonical_clone(
    "PackageRequest",
    _LegacyPackageRequest,
    overrides={"creatives": (list[CreativeAsset] | None, Field(default=None, min_length=1))},
)

PackageUpdate = _canonical_clone(
    "PackageUpdate",
    _LegacyPackageUpdate,
    overrides={"creatives": (list[CreativeAsset] | None, Field(default=None, min_length=1))},
)

Package = _canonical_clone("Package", _LegacyPackage)


def _canonical_enum(name: str, source: type[Enum]) -> type[StrEnum]:
    members = {
        member.name: member.value
        for member in source
        if not is_legacy_creative_identity_key(member.value)
    }
    return StrEnum(name, members, module=__name__)  # type: ignore[call-overload,return-value]


_GetProductsRequestBase = _canonical_clone(
    "_GetProductsRequestBase",
    _LegacyGetProductsRequest,
    overrides={"filters": (ProductFilters | None, Field(default=None))},
)


class GetProductsRequest(_GetProductsRequestBase):
    """Canonical discovery request with legacy response-field selection rejected."""

    @field_validator("fields")
    @classmethod
    def _reject_legacy_fields(cls, value: Any) -> Any:
        if value and any(
            is_legacy_creative_identity_key(getattr(item, "value", item)) for item in value
        ):
            raise ValueError(
                "format_id and format_ids are unavailable on the canonical get_products API"
            )
        return value


GetProductsResponse = _canonical_clone(
    "GetProductsResponse",
    _LegacyGetProductsResponse,
    overrides={"products": (list[Product] | None, Field(default=None))},
)

CreateMediaBuyRequest = _canonical_clone(
    "CreateMediaBuyRequest",
    _LegacyCreateMediaBuyRequest,
    overrides={"packages": (list[PackageRequest] | None, Field(default=None))},
)

UpdateMediaBuyRequest = _canonical_clone(
    "UpdateMediaBuyRequest",
    _LegacyUpdateMediaBuyRequest,
    overrides={
        "packages": (list[PackageUpdate] | None, Field(default=None)),
        "new_packages": (list[PackageRequest] | None, Field(default=None)),
    },
)

_CreateMediaBuyResponse1Base = _canonical_clone(
    "_CreateMediaBuyResponse1Base",
    _LegacyCreateMediaBuyResponse1,
    overrides={"packages": (list[Package], Field())},
)


class CreateMediaBuyResponse1(_CreateMediaBuyResponse1Base):
    """Canonical create response preserving the 3.x legacy-status normalizer."""

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_status(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_status = unwrap_enum_value(data.get("status"))
        media_buy_status = unwrap_enum_value(data.get("media_buy_status"))
        if raw_status is None or raw_status == "completed":
            return {**data, "status": "completed"}
        if media_buy_status is None and raw_status in MEDIA_BUY_LEGACY_STATUS_VALUES:
            return {**data, "media_buy_status": raw_status, "status": "completed"}
        if media_buy_status is not None and raw_status == media_buy_status:
            return {**data, "status": "completed"}
        return data


CreateMediaBuyResponse2 = _canonical_clone(
    "CreateMediaBuyResponse2", _LegacyCreateMediaBuyResponse2
)
CreateMediaBuyResponse3 = _canonical_clone(
    "CreateMediaBuyResponse3", _LegacyCreateMediaBuyResponse3
)
CreateMediaBuyResponse = CreateMediaBuyResponse1 | CreateMediaBuyResponse2 | CreateMediaBuyResponse3

_UpdateMediaBuyResponse1Base = _canonical_clone(
    "_UpdateMediaBuyResponse1Base",
    _LegacyUpdateMediaBuyResponse1,
    overrides={"affected_packages": (Sequence[Package] | None, Field(default=None))},
)


class UpdateMediaBuyResponse1(_UpdateMediaBuyResponse1Base):
    """Canonical update response preserving the 3.x legacy-status normalizer."""

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_status(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_status = unwrap_enum_value(data.get("status"))
        media_buy_status = unwrap_enum_value(data.get("media_buy_status"))
        if raw_status is None or raw_status == "completed":
            return {**data, "status": "completed"}
        if media_buy_status is None and raw_status in MEDIA_BUY_LEGACY_STATUS_VALUES:
            return {**data, "media_buy_status": raw_status, "status": "completed"}
        if media_buy_status is not None and raw_status == media_buy_status:
            return {**data, "status": "completed"}
        return data


UpdateMediaBuyResponse2 = _canonical_clone(
    "UpdateMediaBuyResponse2", _LegacyUpdateMediaBuyResponse2
)
UpdateMediaBuyResponse3 = _canonical_clone(
    "UpdateMediaBuyResponse3", _LegacyUpdateMediaBuyResponse3
)
UpdateMediaBuyResponse = UpdateMediaBuyResponse1 | UpdateMediaBuyResponse2 | UpdateMediaBuyResponse3

SyncCreativesRequest = _canonical_clone(
    "SyncCreativesRequest",
    _LegacySyncCreativesRequest,
    overrides={"creatives": (list[CreativeAsset], Field(min_length=1))},
)

_ListCreativesRequestBase = _canonical_clone(
    "_ListCreativesRequestBase",
    _LegacyListCreativesRequest,
    overrides={"filters": (CreativeFilters | None, Field(default=None))},
)


class ListCreativesRequest(_ListCreativesRequestBase):
    """Canonical creative read request with legacy field selection rejected."""

    @field_validator("fields")
    @classmethod
    def _reject_legacy_fields(cls, value: Any) -> Any:
        if value and any(
            is_legacy_creative_identity_key(getattr(item, "value", item)) for item in value
        ):
            raise ValueError(
                "format_id and format_ids are unavailable on the canonical list_creatives API"
            )
        return value


ListCreativesResponse = _canonical_clone(
    "ListCreativesResponse",
    _LegacyListCreativesResponse,
    overrides={"creatives": (list[Creative], Field())},
)

MediaBuyPackage = _canonical_clone("MediaBuyPackage", _LegacyMediaBuyPackage)
MediaBuy = _canonical_clone(
    "MediaBuy",
    _LegacyMediaBuy,
    overrides={"packages": (Sequence[MediaBuyPackage], Field())},
)
GetMediaBuysResponse = _canonical_clone(
    "GetMediaBuysResponse",
    _LegacyGetMediaBuysResponse,
    overrides={"media_buys": (Sequence[MediaBuy], Field())},
)
GetMediaBuyDeliveryResponse = _canonical_clone(
    "GetMediaBuyDeliveryResponse", _LegacyGetMediaBuyDeliveryResponse
)
GetCreativeDeliveryResponse = _canonical_clone(
    "GetCreativeDeliveryResponse",
    _LegacyGetCreativeDeliveryResponse,
    overrides={"creatives": (Sequence[DeliveryCreative], Field())},
)


PRIMARY_CANONICAL_MODELS: tuple[type[CanonicalBoundaryModel], ...] = (
    Format,
    Product,
    Placement,
    CreativeAsset,
    Creative,
    CreativeManifest,
    CreativeVariant,
    DeliveryCreative,
    CreativeFilters,
    ProductFilters,
    PackageRequest,
    PackageUpdate,
    Package,
    GetProductsRequest,
    GetProductsResponse,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse1,
    CreateMediaBuyResponse2,
    CreateMediaBuyResponse3,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse1,
    UpdateMediaBuyResponse2,
    UpdateMediaBuyResponse3,
    SyncCreativesRequest,
    ListCreativesRequest,
    ListCreativesResponse,
    GetMediaBuysResponse,
    MediaBuy,
    MediaBuyPackage,
    GetMediaBuyDeliveryResponse,
    GetCreativeDeliveryResponse,
)


__all__ = [
    "CanonicalBoundaryModel",
    "CreateMediaBuyRequest",
    "CreateMediaBuyResponse",
    "CreateMediaBuyResponse1",
    "CreateMediaBuyResponse2",
    "CreateMediaBuyResponse3",
    "Creative",
    "CreativeAsset",
    "CreativeFilters",
    "CreativeManifest",
    "CreativeVariant",
    "DeliveryCreative",
    "Format",
    "GetCreativeDeliveryResponse",
    "GetMediaBuyDeliveryResponse",
    "GetMediaBuysResponse",
    "GetProductsRequest",
    "GetProductsResponse",
    "ListCreativesRequest",
    "ListCreativesResponse",
    "MediaBuy",
    "MediaBuyPackage",
    "Package",
    "PackageRequest",
    "PackageUpdate",
    "Placement",
    "PRIMARY_CANONICAL_MODELS",
    "Product",
    "ProductFilters",
    "ProductFormatDeclaration",
    "SyncCreativesRequest",
    "UpdateMediaBuyRequest",
    "UpdateMediaBuyResponse",
    "UpdateMediaBuyResponse1",
    "UpdateMediaBuyResponse2",
    "UpdateMediaBuyResponse3",
    "is_legacy_creative_identity_key",
    "sanitize_canonical_schema",
    "strip_legacy_creative_identity",
]
