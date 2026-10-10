"""Canonical-first creative models for the Python SDK public API.

The generated protocol models intentionally remain wire-faithful through the
AdCP 3.x transition and therefore contain legacy named-format identity.  They
are exposed from :mod:`adcp.types.legacy`.  This module provides the primary
application-facing models: legacy identity is absent from their declared
fields, JSON Schema, and serialized output at every nesting depth.

``format_kind`` is a ``str``, not the generated enum — a deliberate override
-------------------------------------------------------------------------

``core/canonical-format-kind.json`` declares a **closed** 16-member ``enum``
and, in the same file, states as normative:

    Consumer SDKs MUST treat this enum as **open** at parse time: an unknown
    ``format_kind`` value MUST be retained as-is on the in-memory object (not
    silently dropped or rewritten to ``"custom"``) and MUST NOT cause the
    surrounding payload to fail validation. ... The producer-side enum stays
    closed (sellers MUST NOT mint ad-hoc ``format_kind`` values ...); the
    consumer-side enum stays open for forward compatibility.

**The schema already knows the rule is directional, and then encodes it as a
single closed enum, which cannot carry that.** So this override implements what
the schema says rather than contradicting it: every reference to that schema
generates a bare ``str`` — ``OPEN_VOCABULARY_SCHEMAS`` in
``scripts/generate_types.py``, one schema-level transform rather than a
widening at each call site.

**Consumer models retain unknown format kinds, and that is deliberate.** A
seller supports some set of format kinds; that set is the seller's, not this
library's and not the pinned enum's. It can be larger — the seller handles a
kind promoted in a spec newer than the pin — or smaller, four of the sixteen.
A pinned SDK cannot tell "a kind the seller invented" from "a kind defined
after my pin": both are simply "not in my 16". Refusing the second to prevent
the first would make this SDK's version a ceiling on what the protocol permits,
which is the same defect as the closed enum with the enforcement moved into a
validator. The producer-side MUST is a seller's obligation; this library gives
it the vocabulary and :func:`is_canonical_format_kind` to meet it, and leaves
the decision where the knowledge is.

The ``ProductFormatDeclaration`` authoring class is a ``Format`` narrowed by
the root rules of ``core/product-format-declaration.json`` — the six
cross-field ``allOf`` clauses and the sixteen-branch ``format_kind``/``params``
``oneOf``, both read from the bundled schema. Use ``Format`` to parse consumer
declarations with future kinds, ``LegacyProductFormatDeclaration`` for the
generated branch union, and the opt-in ``CanonicalFormatKindStr`` annotation to
restrict an adopter boundary.

So the vocabulary is not discarded, it is **relocated**:
:class:`CanonicalFormatKind` stays a first-class export, used for comparison
(``creative.format_kind == CanonicalFormatKind.image`` — it is a ``StrEnum``,
so that holds against a plain string field and no adopter writes a literal) and
for membership through :func:`is_canonical_format_kind`, whose vocabulary is a
parameter. What changed is only that the field no longer refuses the
seventeenth value a newer server sends. That is the registry pattern the
sibling fields already use: ``format_shape`` and ``asset_group_id`` are plain
strings governed by versioned registries whose own governance says
non-canonical values stay valid and validators may warn.

The one cost is that the generated models no longer agree with the bundled
schema for this one field. That is declared, by name and with this reason, as a
single entry in ``tests/conformance/_schema_parity.py``'s ``DECLARED`` —
``format_kind_is_an_open_vocabulary_by_decision``. The parity rule itself is
not relaxed.

Upstream ask: `adcontextprotocol/adcp#7929
<https://github.com/adcontextprotocol/adcp/issues/7929>`_. If it lands — the
schema stating the directional rule in a form that can carry it, the way
``format_shape`` is governed — delete the transform, delete this section, and
the generated models go back to agreeing with their schema.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Protocol, TypeVar, cast

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    WithJsonSchema,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import GenerateJsonSchema
from pydantic_core import CoreSchema

from adcp.types._product_format_declaration import (
    PRODUCT_FORMAT_DECLARATION_SCHEMA,
    check_declaration_rules,
)
from adcp.types.base import AdCPBaseModel
from adcp.types.domains.core.canonical_format_kind import CanonicalFormatKind
from adcp.types.domains.core.creative_asset import CreativeAsset as _CanonicalCreativeWire
from adcp.types.domains.core.creative_filters import CreativeFilters as _LegacyCreativeFilters
from adcp.types.domains.core.creative_manifest import (
    CreativeManifest as _CanonicalCreativeManifestWire,
)
from adcp.types.domains.core.creative_variant import CreativeVariant as _LegacyCreativeVariant
from adcp.types.domains.core.package import Package as _LegacyPackage
from adcp.types.domains.core.placement import Placement as _LegacyPlacement
from adcp.types.domains.core.platform_extension_ref import PlatformExtensionReference
from adcp.types.domains.core.pricing_option import PricingOption as _LegacyPricingOption
from adcp.types.domains.core.product import Product as _LegacyProduct
from adcp.types.domains.core.product_filters import ProductFilters as _LegacyProductFilters
from adcp.types.domains.core.product_format_declaration import SellerPreference
from adcp.types.domains.creative.get_creative_delivery_response import (
    Creative as _LegacyDeliveryCreative,
)
from adcp.types.domains.creative.get_creative_delivery_response import (
    GetCreativeDeliveryResponse as _LegacyGetCreativeDeliveryResponse,
)
from adcp.types.domains.creative.list_creatives_request import (
    ListCreativesRequest as _LegacyListCreativesRequest,
)
from adcp.types.domains.creative.list_creatives_response import (
    Creatives1 as _CanonicalListedCreative,
)
from adcp.types.domains.creative.list_creatives_response import (
    ListCreativesResponse as _LegacyListCreativesResponse,
)
from adcp.types.domains.creative.sync_creatives_request import (
    SyncCreativesRequest as _LegacySyncCreativesRequest,
)
from adcp.types.domains.enums.channels import MediaChannel
from adcp.types.domains.media_buy.create_media_buy_request import (
    CreateMediaBuyRequest as _LegacyCreateMediaBuyRequest,
)
from adcp.types.domains.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse1 as _LegacyCreateMediaBuyResponse1,
)
from adcp.types.domains.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse2 as _LegacyCreateMediaBuyResponse2,
)
from adcp.types.domains.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse3 as _LegacyCreateMediaBuyResponse3,
)
from adcp.types.domains.media_buy.get_media_buy_delivery_response import (
    GetMediaBuyDeliveryResponse as _LegacyGetMediaBuyDeliveryResponse,
)
from adcp.types.domains.media_buy.get_media_buys_response import (
    GetMediaBuysResponse as _LegacyGetMediaBuysResponse,
)
from adcp.types.domains.media_buy.get_media_buys_response import (
    MediaBuy as _LegacyMediaBuy,
)
from adcp.types.domains.media_buy.get_media_buys_response import (
    Package as _LegacyMediaBuyPackage,
)
from adcp.types.domains.media_buy.get_products_request import (
    GetProductsRequest as _LegacyGetProductsRequest,
)
from adcp.types.domains.media_buy.get_products_response import (
    GetProductsResponse as _LegacyGetProductsResponse,
)
from adcp.types.domains.media_buy.package_request import (
    PackageRequest as _LegacyPackageRequest,
)
from adcp.types.domains.media_buy.package_update import PackageUpdate as _LegacyPackageUpdate
from adcp.types.domains.media_buy.update_media_buy_request import (
    UpdateMediaBuyRequest as _LegacyUpdateMediaBuyRequest,
)
from adcp.types.domains.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse1 as _LegacyUpdateMediaBuyResponse1,
)
from adcp.types.domains.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse2 as _LegacyUpdateMediaBuyResponse2,
)
from adcp.types.domains.media_buy.update_media_buy_response import (
    UpdateMediaBuyResponse3 as _LegacyUpdateMediaBuyResponse3,
)
from adcp.types.legacy import LegacyFormatId
from adcp.types.media_buy_status_helpers import (
    MEDIA_BUY_LEGACY_STATUS_VALUES,
    unwrap_enum_value,
)


def is_canonical_format_kind(
    value: object,
    vocabulary: Iterable[str] = CanonicalFormatKind,
) -> bool:
    """Is *value* one of *vocabulary*'s format kinds?

    The vocabulary defaults to :class:`CanonicalFormatKind`, the sixteen kinds
    the pinned AdCP bundle declares — but it is a PARAMETER, because the set
    that matters is the seller's, not this SDK's. A seller may support a kind
    promoted in a spec newer than the pin, or only four of the sixteen, and
    neither is expressible by anything this library knows.

    **Consumer models never call this for you.** Their ``format_kind`` is a
    ``str`` on the way out and on the way back, and they retain future values.
    That is deliberate: a pinned library cannot tell "a kind the seller
    invented" from "a kind defined after my pin", so refusing the second to
    prevent the first would make this SDK's version a ceiling on what the
    protocol permits. "I accept the request and then tell you I cannot process
    this creative" is a seller's answer, not a type error.

    This function is the sanctioned way to be strict, where the caller knows
    which spec version its counterpart speaks::

        from adcp.types import is_canonical_format_kind

        if not is_canonical_format_kind(creative.format_kind):
            route_as_declared_but_unsupported(creative)

        if not is_canonical_format_kind(manifest.format_kind, MY_SUPPORTED_KINDS):
            reject_with_unsupported_format(manifest)

    Comparison needs no helper: ``CanonicalFormatKind`` is a ``StrEnum``, so
    ``creative.format_kind == CanonicalFormatKind.image`` holds against a plain
    string field and an adopter never writes a literal.
    """

    return isinstance(value, str) and any(value == kind for kind in vocabulary)


def require_canonical_format_kind(
    vocabulary: Iterable[str] = CanonicalFormatKind,
) -> Callable[[str], str]:
    """Build an opt-in string validator for an adopter's format vocabulary.

    Use it with ``AfterValidator`` or ``field_validator`` on your own boundary
    models. The vocabulary is captured once, so generators and mutable inputs
    cannot change the validator after construction. SDK model fields remain
    open strings; this helper does not change their validation.

    For nullable or plural fields, compose the annotated string with ``None``
    or ``list``. Values outside the vocabulary raise ``ValueError``; valid
    values are returned unchanged.
    """
    kinds = tuple(vocabulary)

    def validate(value: str) -> str:
        if not is_canonical_format_kind(value, kinds):
            raise ValueError(f"Unknown canonical format kind: {value!r}")
        return value

    return validate


CanonicalFormatKindStr = Annotated[str, AfterValidator(require_canonical_format_kind())]
"""Opt-in string annotation restricted to the pinned canonical vocabulary.

Use ``CanonicalFormatKindStr | None`` or ``list[CanonicalFormatKindStr]`` for
optional and plural adopter fields. Use ``require_canonical_format_kind`` to
select a different seller vocabulary.
"""


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

if TYPE_CHECKING:
    from adcp.types.domains.core.format_id import FormatReferenceStructuredObject

    # The three shapes the generated parents give legacy creative identity.
    # Each canonical model that inherits one of those fields redeclares it in a
    # ``TYPE_CHECKING`` block, so a type checker's synthesized ``__init__``
    # agrees with the runtime: ``__pydantic_init_subclass__`` removes the field
    # and ``_reject_legacy_creative_identity`` refuses the key, and ``init=False``
    # is what makes the keyword unavailable statically as well. The annotations
    # mirror the generated declaration exactly — a drift in either direction
    # makes the redeclaration an incompatible override, which mypy and pyright
    # refuse by name. ``test_canonical_removed_fields.py`` pins that every
    # removed field has such a redeclaration.
    _RemovedFormatId = FormatReferenceStructuredObject | None
    _RemovedFormatIds = list[FormatReferenceStructuredObject] | None
    _RemovedFormatIdSequence = Sequence[FormatReferenceStructuredObject] | None


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


class _JsonSchemaHandlerWithGenerator(Protocol):
    """The concrete handler pydantic passes in carries the active generator.

    ``GetJsonSchemaHandler`` is the documented protocol and does not declare
    ``generate_json_schema``; the object pydantic actually supplies does, and
    reaching it is how a nested definition registry gets sanitized.
    """

    generate_json_schema: GenerateJsonSchema


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

    sanitized: dict[str, Any] = _sanitize_schema_node(copy.deepcopy(schema))
    return sanitized


def _serialize_canonical_model(
    self: CanonicalBoundaryModel,
    handler: SerializerFunctionWrapHandler,
) -> Any:
    """Enforce the boundary for nested and TypeAdapter serialization too."""

    # ``self`` is whatever pydantic is serializing at this position, which is not
    # always a model: a field annotated with a canonical model can hold a plain
    # dict (a mismatched value pydantic serializes with a warning). Read the
    # capability off the type defensively, because ``dict`` does not carry it.
    return strip_legacy_creative_identity(
        handler(self),
        _format_scope=getattr(type(self), "__adcp_format_declaration_scope__", False),
    )


class CanonicalBoundaryModel(AdCPBaseModel):
    """Base class enforcing the primary canonical runtime boundary.

    Every canonical model is a real subclass of the generated wire model it
    refines, so each one inherits the generated model's fields, its injected
    ``model_validator``s, and its envelope ancestry. Two concerns that used to
    be re-attached per class by ``create_model`` are therefore declared once,
    here, and inherited:

    * ``_serialize_canonical`` — the wrap serializer that strips legacy creative
      identity from nested and ``TypeAdapter`` serialization.
    * ``__pydantic_init_subclass__`` — the one declared field removal. Legacy
      creative identity must be absent from a canonical model's *declared*
      fields, which is the single thing inheritance alone cannot express; the
      rule reads the same :func:`is_legacy_creative_identity_key` predicate that
      governs the input validator, the schema sanitizer and the serializer, so
      there is one strip predicate for all four.

    ``CanonicalBoundaryModel`` is listed LAST among a canonical model's bases.
    Pydantic merges ``model_config`` across bases left to right, so the
    right-most base wins; the generated wire model inherits
    :class:`AdCPBaseModel`'s ``extra`` policy and would otherwise override this
    class's ``extra="allow"`` and start dropping caller-supplied extension keys.
    """

    model_config = ConfigDict(extra="allow", defer_build=True)
    __adcp_canonical_creative_model__: ClassVar[bool] = True

    # Whether this model is a format declaration, and so may carry a root
    # ``v1_format_ref`` and owns the format-scoped legacy-identity strip. It is
    # a declared, INHERITED capability rather than a ``cls.__name__ ==
    # "Format"`` comparison, which no subclass of ``Format`` could satisfy —
    # a subclass would silently lose both behaviours.
    __adcp_format_declaration_scope__: ClassVar[bool] = False

    _serialize_canonical = model_serializer(mode="wrap")(_serialize_canonical_model)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Remove inherited legacy creative identity from the declared fields.

        Each removed name is then bound as a plain class attribute holding
        ``None`` — which is the removal's own truth, and which the inherited
        validators need. A generated model can carry an injected validator that
        READS the removed field: ``list-creatives-response.json``'s
        ``_validate_format_reference_xor`` evaluates
        ``(self.format_id is None) == (self.format_kind is None)``. Inheritance
        is the point of this layer, so that validator now runs on the canonical
        model, and with the field merely deleted it raised ``AttributeError``.
        With the name reading ``None`` the XOR reduces to exactly the invariant
        the canonical model should hold — ``format_kind`` must be set — which is
        the same thing the canonical declaration states by making it required.

        Binding happens AFTER class creation, so pydantic never considers the
        name a field candidate; ``model_fields``, the JSON schema and the wire
        are all unaffected. Measured scope: one such validator, on one model.
        """

        removed = [name for name in cls.model_fields if is_legacy_creative_identity_key(name)]
        for name in removed:
            del cls.model_fields[name]
            cls.__annotations__.pop(name, None)
            setattr(cls, name, None)
        if removed:
            cls.model_rebuild(force=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_creative_identity(cls, value: Any) -> Any:
        found = _legacy_creative_identity_path(
            value,
            allow_root_v1_ref=cls.__adcp_format_declaration_scope__,
            format_scope=cls.__adcp_format_declaration_scope__,
        )
        if found is not None:
            raise ValueError(
                f"{found} contains legacy creative identity; use an explicit Legacy* model"
            )
        return value

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("serialize_as_any", False)
        stripped: dict[str, Any] = strip_legacy_creative_identity(
            super().model_dump(**kwargs),
            _format_scope=type(self).__adcp_format_declaration_scope__,
        )
        return stripped

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("serialize_as_any", False)
        raw = super().model_dump_json(**kwargs)
        clean = strip_legacy_creative_identity(
            json.loads(raw),
            _format_scope=type(self).__adcp_format_declaration_scope__,
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
        generator = cast(_JsonSchemaHandlerWithGenerator, handler).generate_json_schema
        for key, definition in list(generator.definitions.items()):
            generator.definitions[key] = sanitize_canonical_schema(definition)
        return schema


def _inherit(source: type[AdCPBaseModel], name: str) -> Any:
    """Return a copy of ``source``'s ``name`` field for a retyped redeclaration.

    A canonical model that narrows an inherited field's *annotation* still wants
    the generated field's constraints, description and default. Redeclaring with
    this as the assigned value keeps every one of them, where a bare ``Field()``
    would silently drop ``min_length`` and friends. The return type is ``Any``
    because a ``FieldInfo`` is what pydantic expects on the right-hand side of an
    annotated field declaration — the same reason ``Field()`` itself is ``Any``.
    """

    return copy.deepcopy(source.model_fields[name])


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

    __adcp_format_declaration_scope__: ClassVar[bool] = True

    # Set by a subclass that enforces a bundled root schema. ``None`` keeps the
    # permissive reader behaviour every other ``Format`` consumer relies on;
    # naming a schema makes the root rules authoritative, which includes the
    # ``allOf`` clause forbidding ``capability_id`` outright.
    _root_schema_name: ClassVar[str | None] = None

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
    format_kind: str
    params: dict[str, Any]

    _legacy_format_refs: list[LegacyFormatId] = PrivateAttr(default_factory=list)

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
        if (
            "capability_id" in data
            and "format_option_id" not in data
            and type(self)._root_schema_name is None
        ):
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
        if self.format_kind == CanonicalFormatKind.custom.value:
            if not self.format_shape:
                raise ValueError("custom formats require format_shape")
            if self.format_schema is None:
                raise ValueError("custom formats require format_schema")
        elif self.format_shape is not None or self.format_schema is not None:
            raise ValueError("format_shape and format_schema are only valid for custom formats")
        return self


class ProductFormatDeclaration(Format):
    """A product-bound canonical declaration, graded against its root schema.

    The AdCP 3.2 authoring type. It is a :class:`Format` — so every projection
    entry point in :mod:`adcp.canonical_formats` accepts it, and
    ``legacy_format_refs`` / ``params_as`` stay reachable — narrowed by the root
    rules of ``core/product-format-declaration.json``: the six cross-field
    ``allOf`` clauses and the sixteen-branch ``format_kind``/``params``
    ``oneOf``. Those are read from the bundled schema on every validation
    rather than restated here, so the schema stays the only statement of them.
    """

    # ``revalidate_instances`` stays at its ``"never"`` default deliberately.
    # Setting ``"always"`` hands this validator a dict of every declared field,
    # unset ones included as ``None`` — which destroys the presence the root
    # rules test. ``format_shape: None`` would then read as present and
    # allOf[2]'s ``else`` branch would refuse every non-custom declaration.
    # Presence therefore comes from the buyer's own document, or from
    # ``exclude_unset`` when a model is revalidated across classes.
    _root_schema_name: ClassVar[str | None] = PRODUCT_FORMAT_DECLARATION_SCHEMA

    @model_validator(mode="before")
    @classmethod
    def _enforce_root_schema_rules(cls, data: Any) -> Any:
        schema_name = cls._root_schema_name
        if schema_name is None:  # pragma: no cover - set on this class
            return data
        return check_declaration_rules(data, schema_name=schema_name)


class Placement(_LegacyPlacement, CanonicalBoundaryModel):
    """Canonical placement; ``format_options`` are canonical declarations."""

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_ids: _RemovedFormatIdSequence = Field(default=None, init=False)

    format_options: list[Format] | None = Field(default=None, min_length=1)  # type: ignore[assignment]


class Product(_LegacyProduct, CanonicalBoundaryModel):
    """Canonical product; formats, placements and pricing are canonical."""

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_ids: _RemovedFormatIds = Field(default=None, init=False)

    format_options: list[Format] = Field(  # type: ignore[assignment]
        min_length=1, description="Canonical creative formats accepted by this product."
    )
    placements: list[Placement] | None = Field(default=None, min_length=1)  # type: ignore[assignment]
    pricing_options: list[CanonicalPricingOption] = Field(min_length=1)


class CreativeAsset(_CanonicalCreativeWire, CanonicalBoundaryModel):
    """Canonical creative asset; the format kind is required, not optional.

    The kind is narrowed to required and nothing else: it stays ``str`` and the
    model refuses no value. A buyer SENDS a creative asset, and the
    producer-side "sellers MUST NOT mint ad-hoc kinds" rule is the sender's
    obligation, not something a pinned library can tell from a kind defined
    after its pin — :func:`is_canonical_format_kind` is how a caller meets it.
    """

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_id: _RemovedFormatId = Field(default=None, init=False)

    format_kind: str


class Creative(_CanonicalListedCreative, CanonicalBoundaryModel):
    """Canonical listed creative; the format kind is required, not optional.

    A listed creative is a row a seller RETURNS, so the kind is required but
    never confined: a kind a newer seller emits is retained as-is, which is
    what ``core/canonical-format-kind.json`` requires of a consumer.
    """

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_id: _RemovedFormatId = Field(default=None, init=False)

    format_kind: str


class CreativeManifest(_CanonicalCreativeManifestWire, CanonicalBoundaryModel):
    """Canonical manifest accepting the SDK's public standalone asset models.

    The 3.2 aggregate asset-union schema currently generates structurally
    duplicate Pydantic classes. Convert public ``ImageContent``/``UrlContent``
    (and peers) back to their wire dictionaries before the aggregate union
    validates them. This keeps the public constructors composable without
    relaxing the on-wire discriminator checks.
    """

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_id: _RemovedFormatId = Field(default=None, init=False)

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


class CreativeVariant(_LegacyCreativeVariant, CanonicalBoundaryModel):
    """Canonical creative variant whose manifest is the canonical manifest."""

    manifest: CreativeManifest | None = None


class _DeliveryCreativeManifest(_CanonicalCreativeManifestWire, CanonicalBoundaryModel):
    """The buyer's manifest, read back off a delivery row.

    It redeclares nothing about the format-kind vocabulary. ``format_kind`` is
    ``str`` here and on :class:`CreativeManifest`, neither refuses a value, and
    the ``_StrictFormatKind`` mixin that used to be the only difference between
    the two is deleted. **This class is not a tolerant variant of anything.**

    **The one reason it survives** is ``_normalize_readback`` below, and
    specifically its first line: the canonical models are real SUBCLASSES of
    the generated wire models they refine, so a delivery row carrying a
    manifest INSTANCE of the generated wire class is a PARENT instance, which
    pydantic refuses for a field typed as the subclass. Dumping it first is
    what lets a caller compose a delivery response out of the models it already
    holds.

    Measured, not reasoned: deleting both delivery classes and typing
    ``DeliveryCreative.variants`` as ``list[CreativeVariant]`` turns the
    readback suites 6 red, of which exactly ONE is lost behavior —

        Input should be a valid dictionary or instance of CreativeManifest
        [type=model_type, input_type=CreativeManifest]

    on ``test_delivery_accepts_known_input_models[manifest-CreativeManifest1-canonical]``
    and its ``variant`` twin. The other four grade the two-class split itself
    (``test_delivery_manifest_cannot_bypass_strict_input[instance-...]``, its
    variant twin, ``test_delivery_only_types_are_not_top_level_exports`` and
    ``test_unknown_nested_manifest_kind_round_trips_in_delivery_readback``) and
    would be restated, not lost. Keeping one type here therefore costs
    parent-instance acceptance at the delivery boundary, which is a readback
    concern and has nothing to do with the vocabulary.
    """

    @model_validator(mode="before")
    @classmethod
    def _normalize_readback(cls, data: Any) -> Any:
        if isinstance(data, AdCPBaseModel) and not isinstance(data, cls):
            data = data.model_dump(mode="python")
        # Pydantic binds the validator to a descriptor proxy on the class.
        normalize = cast(Callable[[Any], Any], CreativeManifest._normalize_standalone_assets)
        return normalize(data)


class _DeliveryCreativeVariant(_LegacyCreativeVariant, CanonicalBoundaryModel):
    """A delivery row carrying the read-back manifest.

    Survives for the same measured reason as
    :class:`_DeliveryCreativeManifest`: ``_normalize_readback`` accepts a
    PARENT instance — a variant of the generated wire class — which a field
    typed as the canonical subclass refuses. Nothing here is about the
    format-kind vocabulary.
    """

    manifest: _DeliveryCreativeManifest | None = _inherit(_LegacyCreativeVariant, "manifest")

    @model_validator(mode="before")
    @classmethod
    def _normalize_readback(cls, data: Any) -> Any:
        if isinstance(data, AdCPBaseModel) and not isinstance(data, cls):
            return data.model_dump(mode="python")
        return data


class DeliveryCreative(_LegacyDeliveryCreative, CanonicalBoundaryModel):
    """Canonical served creative; variants are the read-back delivery rows."""

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_id: _RemovedFormatId = Field(default=None, init=False)

    variants: list[_DeliveryCreativeVariant] = _inherit(  # type: ignore[assignment]
        _LegacyDeliveryCreative, "variants"
    )


class CreativeFilters(_LegacyCreativeFilters, CanonicalBoundaryModel):
    """Canonical creative filters; legacy identity selection is unavailable."""

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_ids: _RemovedFormatIds = Field(default=None, init=False)


class ProductFilters(_LegacyProductFilters, CanonicalBoundaryModel):
    """Canonical product filters; legacy identity selection is unavailable."""

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_ids: _RemovedFormatIds = Field(default=None, init=False)


class PackageRequest(_LegacyPackageRequest, CanonicalBoundaryModel):
    """Canonical package request preserving beta.3 selector constraints."""

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_ids: _RemovedFormatIds = Field(default=None, init=False)

    creatives: list[CreativeAsset] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_format_params(self) -> PackageRequest:
        if self.params is not None and self.format_kind is None:
            raise ValueError("params requires format_kind")
        if self.params is not None and self.format_kind == "image":
            if ("width" in self.params) != ("height" in self.params):
                raise ValueError("image params width and height must co-occur")
        return self


class PackageUpdate(_LegacyPackageUpdate, CanonicalBoundaryModel):
    """Canonical package update; creatives are canonical assets."""

    creatives: list[CreativeAsset] | None = Field(default=None, min_length=1)  # type: ignore[assignment]


class Package(_LegacyPackage, CanonicalBoundaryModel):
    """Canonical package; legacy format identity is absent."""

    if TYPE_CHECKING:  # the removed fields, hidden from the constructor too
        format_ids: _RemovedFormatIds = Field(default=None, init=False)
        format_ids_pending: _RemovedFormatIds = Field(default=None, init=False)
        format_ids_to_provide: _RemovedFormatIds = Field(default=None, init=False)


class GetProductsRequest(_LegacyGetProductsRequest, CanonicalBoundaryModel):
    """Canonical discovery request with legacy response-field selection rejected."""

    filters: ProductFilters | None = None

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


class GetProductsResponse(_LegacyGetProductsResponse, CanonicalBoundaryModel):
    """Canonical discovery response; products are canonical products."""

    products: list[Product] | None = None  # type: ignore[assignment]


class CreateMediaBuyRequest(_LegacyCreateMediaBuyRequest, CanonicalBoundaryModel):
    """Canonical create request; packages are canonical package requests."""

    packages: list[PackageRequest] | None = None


class UpdateMediaBuyRequest(_LegacyUpdateMediaBuyRequest, CanonicalBoundaryModel):
    """Canonical update request; both package lists are canonical."""

    packages: list[PackageUpdate] | None = None
    new_packages: list[PackageRequest] | None = Field(  # type: ignore[assignment]
        default=None, min_length=1
    )


class CreateMediaBuyResponse1(_LegacyCreateMediaBuyResponse1, CanonicalBoundaryModel):
    """Canonical create response preserving the 3.x legacy-status normalizer."""

    packages: list[Package]  # type: ignore[assignment]

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


class CreateMediaBuyResponse2(_LegacyCreateMediaBuyResponse2, CanonicalBoundaryModel):
    """Canonical create-media-buy error arm."""


class CreateMediaBuyResponse3(_LegacyCreateMediaBuyResponse3, CanonicalBoundaryModel):
    """Canonical create-media-buy submitted arm."""


CreateMediaBuyResponse = CreateMediaBuyResponse1 | CreateMediaBuyResponse2 | CreateMediaBuyResponse3


class UpdateMediaBuyResponse1(_LegacyUpdateMediaBuyResponse1, CanonicalBoundaryModel):
    """Canonical update response preserving the 3.x legacy-status normalizer."""

    affected_packages: Sequence[Package] | None = None

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


class UpdateMediaBuyResponse2(_LegacyUpdateMediaBuyResponse2, CanonicalBoundaryModel):
    """Canonical update-media-buy error arm."""


class UpdateMediaBuyResponse3(_LegacyUpdateMediaBuyResponse3, CanonicalBoundaryModel):
    """Canonical update-media-buy submitted arm."""


UpdateMediaBuyResponse = UpdateMediaBuyResponse1 | UpdateMediaBuyResponse2 | UpdateMediaBuyResponse3


class SyncCreativesRequest(_LegacySyncCreativesRequest, CanonicalBoundaryModel):
    """Canonical creative sync request; creatives are canonical assets."""

    creatives: list[CreativeAsset] = Field(min_length=1)  # type: ignore[assignment]


class ListCreativesRequest(_LegacyListCreativesRequest, CanonicalBoundaryModel):
    """Canonical creative read request with legacy field selection rejected."""

    filters: CreativeFilters | None = None

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


class ListCreativesResponse(_LegacyListCreativesResponse, CanonicalBoundaryModel):
    """Canonical creative listing; rows are canonical listed creatives."""

    creatives: list[Creative]


class MediaBuyPackage(_LegacyMediaBuyPackage, CanonicalBoundaryModel):
    """Canonical media-buy package row; legacy format identity is absent."""

    if TYPE_CHECKING:  # the removed fields, hidden from the constructor too
        format_ids: _RemovedFormatIds = Field(default=None, init=False)
        format_ids_pending: _RemovedFormatIds = Field(default=None, init=False)
        format_ids_to_provide: _RemovedFormatIds = Field(default=None, init=False)


class MediaBuy(_LegacyMediaBuy, CanonicalBoundaryModel):
    """Canonical media buy; packages are canonical package rows."""

    packages: Sequence[MediaBuyPackage]


class GetMediaBuysResponse(_LegacyGetMediaBuysResponse, CanonicalBoundaryModel):
    """Canonical media-buy listing; rows are canonical media buys."""

    media_buys: Sequence[MediaBuy]


class GetMediaBuyDeliveryResponse(_LegacyGetMediaBuyDeliveryResponse, CanonicalBoundaryModel):
    """Canonical media-buy delivery response."""


class GetCreativeDeliveryResponse(_LegacyGetCreativeDeliveryResponse, CanonicalBoundaryModel):
    """Canonical creative delivery response; rows are the read-back delivery creatives."""

    creatives: Sequence[DeliveryCreative]


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
    "is_canonical_format_kind",
    "CanonicalFormatKindStr",
    "require_canonical_format_kind",
    "is_legacy_creative_identity_key",
    "sanitize_canonical_schema",
    "strip_legacy_creative_identity",
]
