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
from collections.abc import Collection, Sequence
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, TypeVar

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
from adcp.types.generated_poc.core.creative_asset import CreativeAsset as _CanonicalCreativeWire
from adcp.types.generated_poc.core.creative_filters import CreativeFilters as _LegacyCreativeFilters
from adcp.types.generated_poc.core.creative_manifest import (
    CreativeManifest as _CanonicalCreativeManifestWire,
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

_OpenCanonicalFormatKind = Annotated[
    CanonicalFormatKind | str,
    Field(union_mode="left_to_right"),
]

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


def _is_format_scoped_agent_tuple(value: dict[Any, Any], *, path: str) -> bool:
    """Recognize legacy format owners, including tuples with extension keys."""

    keys = set(value)
    if not {"agent_url", "id"} <= keys:
        return False
    # Several protocol surfaces intentionally carry ordinary agent, signal,
    # and list descriptors with the same structural pair. Match RC3's explicit
    # non-creative contexts; unknown extension paths fail closed because a
    # LegacyFormatId may itself contain extension keys.
    path_segment = re.sub(r"\[\d+\]$", "", path.rsplit(".", 1)[-1]).lower()
    noncreative_agent = not re.search(
        r"(^|_)(?:creative|format|legacy)($|_)", path_segment
    ) and bool(re.search(r"(^|_)(?:agents?|agent_details|agent_info)($|_)", path_segment))
    noncreative_signal = value.get("source") == "agent" and bool(
        re.search(r"(^|_)signal_ids?($|_)", path_segment)
    )
    noncreative_list = isinstance(value.get("list_id"), str) and bool(
        re.search(r"(^|_)(?:property_list|collection_list|list_ref)($|_)", path_segment)
    )
    if noncreative_agent or noncreative_signal or noncreative_list:
        return False
    return True


def strip_legacy_creative_identity(
    value: Any,
    *,
    _path: str = "$",
    _format_scope: bool = False,
) -> Any:
    """Recursively remove legacy creative identity from a serialized value.

    This is deliberately a runtime boundary rather than a typing convention.
    Unknown extension bags are traversed too, so ``extra='allow'`` can never be
    used to smuggle ``format_id`` or ``format_ids`` through a primary model.
    """

    if isinstance(value, dict):
        legacy_tuple = _is_format_scoped_agent_tuple(
            value,
            path=_path,
        )
        return {
            key: strip_legacy_creative_identity(
                item,
                _path=f"{_path}.{key}",
                _format_scope=_format_scope or (isinstance(key, str) and "format" in key.lower()),
            )
            for key, item in value.items()
            if not is_legacy_creative_identity_key(key)
            and not (legacy_tuple and key == "agent_url")
        }
    if isinstance(value, list):
        return [
            strip_legacy_creative_identity(
                item,
                _path=f"{_path}[{index}]",
                _format_scope=_format_scope,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return tuple(
            strip_legacy_creative_identity(
                item,
                _path=f"{_path}[{index}]",
                _format_scope=_format_scope,
            )
            for index, item in enumerate(value)
        )
    return value


def _legacy_creative_identity_path(
    value: Any,
    *,
    path: str = "$",
    allow_root_v1_ref: bool = False,
    format_scope: bool = False,
) -> str | None:
    """Locate legacy creative identity in model input without mutating it."""

    if isinstance(value, AdCPBaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, dict):
        if _is_format_scoped_agent_tuple(value, path=path):
            return f"{path}.agent_url"
        for key, nested in value.items():
            if is_legacy_creative_identity_key(key):
                if allow_root_v1_ref and path == "$" and key == "v1_format_ref":
                    continue
                return f"{path}.{key}"
            found = _legacy_creative_identity_path(
                nested,
                path=f"{path}.{key}",
                format_scope=format_scope or (isinstance(key, str) and "format" in key.lower()),
            )
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            found = _legacy_creative_identity_path(
                nested,
                path=f"{path}[{index}]",
                format_scope=format_scope,
            )
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
    #: The generated class a canonical clone stands in for; ``None`` on a model
    #: declared directly. Set by :func:`_canonical_clone`.
    __adcp_canonical_source__: ClassVar[type[AdCPBaseModel] | None] = None
    #: Source validators a clone left behind, each with the dropped field names
    #: it touches. Empty on a model declared directly.
    __adcp_canonical_validators_left_behind__: ClassVar[dict[str, set[str]]] = {}

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_creative_identity(cls, value: Any) -> Any:
        found = _legacy_creative_identity_path(
            value,
            allow_root_v1_ref=cls.__name__ == "Format",
            format_scope=cls.__name__ == "Format",
        )
        if found is not None:
            raise ValueError(
                f"{found} contains legacy creative identity; use an explicit Legacy* model"
            )
        return value

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("serialize_as_any", False)
        return strip_legacy_creative_identity(
            super().model_dump(**kwargs),
            _format_scope=self.__class__.__name__ == "Format",
        )

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("serialize_as_any", False)
        raw = super().model_dump_json(**kwargs)
        clean = strip_legacy_creative_identity(
            json.loads(raw),
            _format_scope=self.__class__.__name__ == "Format",
        )
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

    return strip_legacy_creative_identity(
        handler(self),
        _format_scope=self.__class__.__name__ == "Format",
    )


def _canonical_clone_bases(source: type[AdCPBaseModel]) -> tuple[type[AdCPBaseModel], ...]:
    """Return the clone bases for ``source``: what it composes, then the boundary.

    A canonical clone copies the composed *fields*, but a clone built on
    ``CanonicalBoundaryModel`` alone would drop the composed *ancestry* — so
    ``issubclass(GetProductsResponse, ProtocolEnvelope)`` would be ``False``
    even though the response carries ``status``/``replayed``/``task_id``. The
    bases ``source`` already has are what its schema composes at its root, so
    carrying them over keeps the canonical surface's ancestry identical to the
    generated surface it replaces. Reading them off ``source`` rather than
    naming the envelopes is what makes that true for every composed base:
    naming them covered ``AdcpVersionEnvelope`` and ``ProtocolEnvelope`` and
    silently dropped ``DeliveryMetrics`` from ``CreativeVariant`` and
    ``IndicatorBearingResourceState`` from ``MediaBuy``.

    ``CanonicalBoundaryModel`` comes last on purpose. Pydantic merges
    ``model_config`` across bases left to right, so the right-most base wins;
    a composed base inherits :class:`AdCPBaseModel`'s ``extra`` policy and
    would otherwise override the boundary's ``extra="allow"`` and start
    dropping caller-supplied extension keys. Method resolution is unaffected —
    the composed bases override nothing, so ``CanonicalBoundaryModel`` still
    supplies ``model_dump``/``model_json_schema`` ahead of
    :class:`AdCPBaseModel`.

    ``source`` is sometimes a clone itself — ``_DeliveryCreativeVariantBase``
    and the guard fixtures in ``tests/test_code_generation.py`` clone one — so
    the two bases this function supplies are dropped before it supplies them
    again. Without that, re-cloning a clone is ``TypeError: duplicate base
    class CanonicalBoundaryModel``.
    """

    supplied = (AdCPBaseModel, CanonicalBoundaryModel)
    composed = tuple(base for base in source.__bases__ if base not in supplied)
    return (*composed, CanonicalBoundaryModel)


def _validator_references(function: Any) -> set[str]:
    """Names a validator's code touches: attributes read and string constants.

    A generated validator reaches a field as ``self.<name>`` (``co_names``) or
    names it in a literal — the root required-group validator carries its
    groups as a tuple of field-name strings. Nested code objects (comprehensions)
    are walked too.
    """
    code = getattr(function, "__func__", function).__code__
    names: set[str] = set()
    pending = [code]
    while pending:
        current = pending.pop()
        names.update(current.co_names)
        constants: list[Any] = list(current.co_consts)
        while constants:
            constant = constants.pop()
            if isinstance(constant, str):
                names.add(constant)
            elif isinstance(constant, (tuple, frozenset)):
                constants.extend(constant)
            elif hasattr(constant, "co_code"):
                pending.append(constant)
    return names


def _own_validators(
    source: type[AdCPBaseModel], fields: Collection[str]
) -> tuple[dict[str, Any], dict[str, set[str]]]:
    """Return the validators ``source`` declares on itself, re-decorated for a clone.

    A canonical clone is built with ``create_model`` over ``source``'s *bases*
    (:func:`_canonical_clone_bases`) and a copy of its *fields*
    (:func:`_field_definitions`), so ``source`` itself is not in the clone's
    MRO and nothing declared on its class body crosses over: the validators
    the generator writes onto a request class — the root-level required
    groups of ``create-media-buy-request.json`` (#1361), the uniqueness checks
    on reporting selectors, the publisher-property coercion on ``Product`` —
    would silently stop applying to the canonical name. Inherited validators do
    cross, through the bases, so only the ones ``source`` declares on its own
    body are carried here.

    A validator that touches a field the clone does not carry is left behind,
    and the second mapping names which field for each: the clone drops the
    legacy creative identity fields on purpose, so a rule written against
    ``format_id`` — the ``format_id | format_kind`` root group on a manifest,
    the format-reference XOR on a listed creative — has nothing to enforce on
    the canonical boundary and would raise ``AttributeError`` on the read.

    The class namespace, not ``Decorator.func``, is what gets re-decorated:
    pydantic stores a ``before`` validator's ``classmethod`` there, and the
    unwrapped ``func`` has already lost its ``cls`` binding.
    """
    own = vars(source)
    decorators = source.__pydantic_decorators__
    dropped = set(source.model_fields) - set(fields)
    carried: dict[str, Any] = {}
    left_behind: dict[str, set[str]] = {}
    for attribute, decorator in decorators.model_validators.items():
        if attribute not in own:
            continue
        touched = _validator_references(own[attribute]) & dropped
        if touched:
            left_behind[attribute] = touched
            continue
        carried[attribute] = model_validator(mode=decorator.info.mode)(own[attribute])
    for attribute, decorator in decorators.field_validators.items():
        if attribute not in own:
            continue
        info = decorator.info
        touched = (set(info.fields) | _validator_references(own[attribute])) & dropped
        if touched:
            left_behind[attribute] = touched
            continue
        carried[attribute] = field_validator(
            *info.fields,
            mode=info.mode,
            check_fields=info.check_fields,
        )(own[attribute])
    return carried, left_behind


def _canonical_clone(
    name: str,
    source: type[AdCPBaseModel],
    *,
    exclude: frozenset[str] = frozenset(),
    overrides: dict[str, tuple[Any, Any]] | None = None,
) -> type[CanonicalBoundaryModel]:
    fields = _field_definitions(source, exclude=exclude, overrides=overrides)
    carried, left_behind = _own_validators(source, fields)
    model = create_model(  # type: ignore[call-overload]
        name,
        __base__=_canonical_clone_bases(source),
        __module__=__name__,
        __validators__={
            **carried,
            "_serialize_canonical": model_serializer(mode="wrap")(_serialize_canonical_model),
        },
        **fields,
    )
    # The generated class a clone stands in for, and the validators it could
    # not carry. ``tests/test_canonical_validator_parity.py`` reads both to
    # assert the clone refuses every document its source refuses.
    model.__adcp_canonical_source__ = source
    model.__adcp_canonical_validators_left_behind__ = left_behind
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
            self._legacy_format_refs = [
                LegacyFormatId.model_validate(copy.deepcopy(ref)) for ref in refs
            ]

    @property
    def legacy_format_refs(self) -> tuple[LegacyFormatId, ...]:
        """Original tuples retained only for an explicit compatibility adapter."""

        return tuple(copy.deepcopy(ref) for ref in self._legacy_format_refs)

    def params_as(self, canonical_type: type[_CanonicalParamsT]) -> _CanonicalParamsT:
        """Validate the open parameter bag against a typed canonical model."""

        return canonical_type.model_validate(self.params)

    @model_validator(mode="after")
    def _validate_custom_shape(self) -> Format:
        if self.format_kind is CanonicalFormatKind.custom:
            if not self.format_shape:
                raise ValueError("custom formats require format_shape")
            if self.format_schema is None:
                raise ValueError("custom formats require format_schema")
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

_CreativeManifestBase = _canonical_clone(
    "_CreativeManifestBase",
    _CanonicalCreativeManifestWire,
)


class CreativeManifest(_CreativeManifestBase):
    """Canonical manifest accepting the SDK's public standalone asset models.

    The 3.2 aggregate asset-union schema currently generates structurally
    duplicate Pydantic classes. Convert public ``ImageContent``/``UrlContent``
    (and peers) back to their wire dictionaries before the aggregate union
    validates them. This keeps the public constructors composable without
    relaxing the on-wire discriminator checks.
    """

    @model_validator(mode="before")
    @classmethod
    def _normalize_standalone_assets(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("assets"), dict):
            return data

        def wire_value(value: Any) -> Any:
            if isinstance(value, AdCPBaseModel):
                return value.model_dump(mode="json", exclude_none=True)
            if isinstance(value, list):
                return [wire_value(item) for item in value]
            return value

        return {
            **data,
            "assets": {key: wire_value(value) for key, value in data["assets"].items()},
        }


CreativeVariant = _canonical_clone(
    "CreativeVariant",
    _LegacyCreativeVariant,
    overrides={"manifest": (CreativeManifest | None, Field(default=None))},
)


_DeliveryCreativeManifestBase = _canonical_clone(
    "_DeliveryCreativeManifestBase",
    _CanonicalCreativeManifestWire,
    overrides={
        "format_kind": (
            _OpenCanonicalFormatKind | None,
            copy.deepcopy(_CanonicalCreativeManifestWire.model_fields["format_kind"]),
        )
    },
)


class _DeliveryCreativeManifest(_DeliveryCreativeManifestBase):
    """Tolerant served output, deliberately not a subtype of the strict input."""

    @model_validator(mode="before")
    @classmethod
    def _normalize_readback(cls, data: Any) -> Any:
        if isinstance(data, AdCPBaseModel) and not isinstance(data, cls):
            data = data.model_dump(mode="python")
        return CreativeManifest._normalize_standalone_assets(data)


_DeliveryCreativeVariantBase = _canonical_clone(
    "_DeliveryCreativeVariantBase",
    _LegacyCreativeVariant,
    overrides={
        "manifest": (
            _DeliveryCreativeManifest | None,
            copy.deepcopy(_LegacyCreativeVariant.model_fields["manifest"]),
        )
    },
)


class _DeliveryCreativeVariant(_DeliveryCreativeVariantBase):
    """A delivery row whose rendered manifest may use a future format kind."""

    @model_validator(mode="before")
    @classmethod
    def _normalize_readback(cls, data: Any) -> Any:
        if isinstance(data, AdCPBaseModel) and not isinstance(data, cls):
            return data.model_dump(mode="python")
        return data


DeliveryCreative = _canonical_clone(
    "DeliveryCreative",
    _LegacyDeliveryCreative,
    overrides={
        "format_kind": (_OpenCanonicalFormatKind | None, Field(default=None)),
        "variants": (
            list[_DeliveryCreativeVariant],
            copy.deepcopy(_LegacyDeliveryCreative.model_fields["variants"]),
        ),
    },
)

CreativeFilters = _canonical_clone("CreativeFilters", _LegacyCreativeFilters)
ProductFilters = _canonical_clone("ProductFilters", _LegacyProductFilters)

_PackageRequestBase = _canonical_clone(
    "PackageRequest",
    _LegacyPackageRequest,
    overrides={"creatives": (list[CreativeAsset] | None, Field(default=None, min_length=1))},
)


class PackageRequest(_PackageRequestBase):
    """Canonical package request preserving beta.3 selector constraints."""

    @model_validator(mode="after")
    def _validate_format_params(self) -> PackageRequest:
        if self.params is not None and self.format_kind is None:
            raise ValueError("params requires format_kind")
        if self.params is not None and self.format_kind == "image":
            if ("width" in self.params) != ("height" in self.params):
                raise ValueError("image params width and height must co-occur")
        return self


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
    overrides={
        "packages": (list[Package], Field()),
        # AdCP 3.2 removes the synchronous task-envelope status from this
        # schema arm. Keep it as a declared compatibility field so the
        # normalizer does not inject an unknown extra into adopter subclasses
        # that choose ``extra='forbid'``.
        "status": (Literal["completed"], Field(default="completed")),
    },
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
    overrides={
        "affected_packages": (Sequence[Package] | None, Field(default=None)),
        "status": (Literal["completed"], Field(default="completed")),
    },
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
