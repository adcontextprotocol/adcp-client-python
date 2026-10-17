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
from functools import lru_cache
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Protocol,
    TypeAlias,
    TypeVar,
    cast,
    get_args,
)

from pydantic import (
    AfterValidator,
    AnyUrl,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    ModelWrapValidatorHandler,
    PlainSerializer,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    WithJsonSchema,
    WrapSerializer,
    WrapValidator,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import GenerateJsonSchema
from pydantic_core import CoreSchema

from adcp.types import _conditions
from adcp.types._product_format_declaration import (
    PRODUCT_FORMAT_DECLARATION_SCHEMA,
    check_declaration_rules,
)
from adcp.types._targeting_compat import (
    TargetingOverlayMutationInMaterializedJson,
    mutation_overlay_field,
)
from adcp.types.base import AdCPBaseModel
from adcp.types.domains.core.canonical_format_kind import CanonicalFormatKind
from adcp.types.domains.core.creative_asset import (
    # The canonical asset IS the root's canonical-format-kind branch: that branch
    # requires ``format_kind`` and types the deprecated ``format_id`` as
    # ``None``, which is precisely what this layer used to add by hand.
    CreativeAssetV2CreativeCanonicalFormatKind as _CanonicalCreativeWire,
)
from adcp.types.domains.core.creative_filters import CreativeFilters as _LegacyCreativeFilters
from adcp.types.domains.core.creative_manifest import (
    # The ROOT, not its canonical branch. core/creative-manifest.json requires
    # only ['assets'] and then states a true XOR -- oneOf[0] 'Named-format
    # manifest' requires format_id with not:{required:[format_kind]}, oneOf[1]
    # 'Canonical-format manifest' requires format_kind with
    # not:{required:[format_id]} -- so the root requires NEITHER key. Binding the
    # canonical branch imported that branch's required ``format_kind`` onto the
    # public name, which is the requiredness regression on this branch
    # (salesagent-8qela.69): optional upstream, required here.
    #
    # BOTH manifest refinements below bind this: the public input manifest and the
    # readback manifest on a delivery row. The pin reaches both through the same
    # ``$ref`` to this root -- core/creative-variant.json for the input manifest,
    # core/get-creative-delivery-response.json for the readback one -- so neither
    # may narrow to one arm. The readback name carried the canonical arm until
    # ``_DeliveryCreativeManifest`` below, and that is the SAME requiredness
    # regression, one name over.
    CreativeManifest as _CreativeManifestRoot,
)
from adcp.types.domains.core.creative_variant import CreativeVariant as _LegacyCreativeVariant
from adcp.types.domains.core.macro_resolution_capability import MacroProcessingCapability
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
    # The generated base and one class per branch. The root's branches partition
    # by requiredness, so there is no single wire class to refine: two branches
    # require ``packages`` and the third forbids it.
    CreateMediaBuyRequest as _LegacyCreateMediaBuyBase,
)
from adcp.types.domains.media_buy.create_media_buy_request import (
    CreateMediaBuyRequestCommittedProposalExecution as _LegacyCreateMediaBuyProposal,
)
from adcp.types.domains.media_buy.create_media_buy_request import (
    CreateMediaBuyRequestExplicitPackagesWithFixedAllocation as _LegacyCreateMediaBuyFixed,
)
from adcp.types.domains.media_buy.create_media_buy_request import (
    CreateMediaBuyRequestExplicitPackagesWithSellerOptimizedAllocation as _LegacyCMBSellerOptimized,
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

    if isinstance(value, AdCPBaseModel):
        value = _model_boundary_values(value)
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower().endswith(_CREDENTIAL_SHAPED_KEY_SUFFIXES):
                return f"{path}.{key}" if path else str(key)
            if not isinstance(nested, (dict, list, tuple, AdCPBaseModel)):
                continue
            nested_path = f"{path}.{key}" if path else str(key)
            found = _walk_for_credential_keys(nested, path=nested_path)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            found = _walk_for_credential_keys(nested, path=f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _has_boundary_serialization_metadata(annotation: Any) -> bool:
    """Find functional serializers, including ones inside container annotations."""
    if isinstance(annotation, (PlainSerializer, WrapSerializer)):
        return True
    if hasattr(annotation, "__get_pydantic_core_schema__"):
        is_adcp_model = isinstance(annotation, type) and issubclass(annotation, AdCPBaseModel)
        if not is_adcp_model and getattr(annotation, "__module__", "").split(".")[0] != "pydantic":
            return True
    return any(_has_boundary_serialization_metadata(item) for item in get_args(annotation))


@lru_cache(maxsize=1024)
def _direct_boundary_fields(cls: type[AdCPBaseModel]) -> frozenset[str] | None:
    """Cache the fields of models whose stored values match their default dump."""
    if cls.model_dump not in (AdCPBaseModel.model_dump, CanonicalBoundaryModel.model_dump):
        return None
    if getattr(cls.__get_pydantic_core_schema__, "__func__", None) is not getattr(
        AdCPBaseModel.__get_pydantic_core_schema__, "__func__", None
    ):
        return None
    decorators = cls.__pydantic_decorators__
    if decorators.computed_fields or decorators.field_serializers:
        return None
    stock_serializers = (
        AdCPBaseModel._notification_config_wire_defaults,
        _serialize_canonical_model,
    )
    if any(item.func not in stock_serializers for item in decorators.model_serializers.values()):
        return None
    if cls.model_config.get("serialize_by_alias"):
        return None
    # This stock serializer conditionally drops a field based on sibling values.
    if {"subscriber_id", "url", "event_types", "product_payload_view"} <= cls.model_fields.keys():
        return None
    fields = set()
    for name, field in cls.model_fields.items():
        if field.exclude:
            continue
        if field.exclude_if is not None or _has_boundary_serialization_metadata(field.annotation):
            return None
        if any(_has_boundary_serialization_metadata(item) for item in field.metadata):
            return None
        fields.add(name)
    return frozenset(fields)


def _is_direct_boundary_value(value: Any, active: set[int] | None = None) -> bool:
    """Other models and Python objects need Pydantic's conversion before scanning."""
    if value is None or isinstance(value, (str, int, float, bool, AnyUrl, AdCPBaseModel)):
        return True
    if not isinstance(value, (dict, list, tuple)):
        return False
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        return False
    active.add(identity)
    try:
        items = value.values() if isinstance(value, dict) else value
        return all(_is_direct_boundary_value(item, active) for item in items)
    finally:
        active.remove(identity)


def _model_boundary_values(value: AdCPBaseModel) -> Any:
    """Read ordinary fields directly and preserve custom serialization behavior.

    Canonical instances can be constructed or mutated without validation, so
    they still need scanning. Private compatibility state is deliberately not
    part of the public boundary.
    """
    fields = _direct_boundary_fields(type(value))
    if fields is None:
        return value.model_dump(mode="python")
    # Match AdCPBaseModel.model_dump's default omission of null model fields;
    # null values inside open dictionaries still retain their keys and checks.
    values = {
        **{
            name: item
            for name, item in value.__dict__.items()
            if name in fields and item is not None
        },
        **{
            name: item
            for name, item in (value.__pydantic_extra__ or {}).items()
            if item is not None
        },
    }
    if not all(_is_direct_boundary_value(item) for item in values.values()):
        return value.model_dump(mode="python")
    return values


class _JsonSchemaHandlerWithGenerator(Protocol):
    """The concrete handler pydantic passes in carries the active generator.

    ``GetJsonSchemaHandler`` is the documented protocol and does not declare
    ``generate_json_schema``; the object pydantic actually supplies does, and
    reaching it is how a nested definition registry gets sanitized.
    """

    generate_json_schema: GenerateJsonSchema


def is_legacy_creative_identity_key(key: object) -> bool:
    """Return whether *key* names legacy creative routing identity."""

    return isinstance(key, str) and _is_legacy_creative_identity_string(key)


@lru_cache(maxsize=1024)
def _is_legacy_creative_identity_string(key: str) -> bool:
    return bool(_LEGACY_IDENTITY_KEY.search(key))


def _is_format_scoped_agent_tuple(value: dict[Any, Any], *, path: str) -> bool:
    """Recognize legacy format owners, including tuples with extension keys."""

    if "agent_url" not in value or "id" not in value:
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
            key: (
                strip_legacy_creative_identity(item, _path=f"{_path}.{key}")
                if isinstance(item, (dict, list, tuple))
                else item
            )
            for key, item in value.items()
            if not is_legacy_creative_identity_key(key)
            and not (legacy_tuple and key == "agent_url")
        }
    if isinstance(value, list):
        return [
            (
                strip_legacy_creative_identity(item, _path=f"{_path}[{index}]")
                if isinstance(item, (dict, list, tuple))
                else item
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return tuple(
            (
                strip_legacy_creative_identity(item, _path=f"{_path}[{index}]")
                if isinstance(item, (dict, list, tuple))
                else item
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
        value = _model_boundary_values(value)
    if isinstance(value, dict):
        if _is_format_scoped_agent_tuple(value, path=path):
            return f"{path}.agent_url"
        for key, nested in value.items():
            if is_legacy_creative_identity_key(key):
                if allow_root_v1_ref and path == "$" and key == "v1_format_ref":
                    continue
                return f"{path}.{key}"
            if not isinstance(nested, (dict, list, tuple, AdCPBaseModel)):
                continue
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


def _shape_forbids_top_level_key(cls: type, found: str) -> bool:
    """Does *cls*'s own shape already forbid the top-level key *found* names?

    ``found`` is a path from :func:`_legacy_creative_identity_path`, so a
    top-level key is exactly ``$.<key>`` -- anything deeper carries a further
    ``.`` or a ``[``, and only the path-reading refusal answers for those.

    ``_ABSENT`` is the generated declaration of the keys a shape forbids,
    compiled from the schema's branch and inherited by a canonical model that
    binds that branch. It is read rather than re-derived from field types: the
    canonical layer DELETES a removed field from its own ``model_fields``, so the
    resolved-annotation route cannot see it on this class at all.
    """

    key = found[2:] if found.startswith("$.") else ""
    if not key or "." in key or "[" in key:
        return False
    return key in getattr(cls, "_ABSENT", frozenset())


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

    # Each serializer remains its own boundary: a custom serializer can start
    # an independent TypeAdapter dump and embed that result as a JSON string,
    # which an enclosing cleanup pass cannot inspect.
    return strip_legacy_creative_identity(handler(self))


@lru_cache(maxsize=128)
def _uses_canonical_serializer(model: type[AdCPBaseModel]) -> bool:
    """Whether Pydantic selected our inherited canonical wrap serializer."""
    if getattr(model.__get_pydantic_core_schema__, "__func__", None) is not getattr(
        AdCPBaseModel.__get_pydantic_core_schema__, "__func__", None
    ):
        return False
    serializers = model.__pydantic_decorators__.model_serializers
    return (
        bool(serializers)
        and next(reversed(serializers.values())).func is _serialize_canonical_model
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

    @model_validator(mode="wrap")
    @classmethod
    def _reject_legacy_creative_identity(
        cls, value: Any, handler: ModelWrapValidatorHandler[Any]
    ) -> Any:
        """Refuse a removed creative-identity key, naming the migration.

        WRAP rather than BEFORE, and the kind is what makes this reachable. A
        generated branch that FORBIDS the same key refuses it too, through
        ``_c.absent``, with the generic sentence every forbidden key gets:
        "format_id is not accepted by this request shape". That sentence states
        the refusal and not the remedy, and a buyer told a field is unusable
        needs to be told what to use instead.

        Both were ``mode="before"``, and the generated one won. Measured: a
        same-named override keeps the slot where the name was FIRST collected
        walking the MRO, so declaring this on the most derived class does not
        move it -- the generated validator still ran first and this one never
        saw the key. A wrap validator wraps the whole inner validation,
        including every before-validator a subclass declares, so precedence
        here comes from the validator's KIND and not from a collection order
        nobody controls. Measured on both: wrap-on-the-base wins, and keeps
        winning through a further refinement.

        The two refusals are not redundant and neither is deleted. This one
        reads a PATH, so it catches a legacy identity nested anywhere in the
        document; ``_ABSENT`` is top-level keys only. The generic refusal still
        answers for every other key a shape forbids.

        BUT IT DOES NOT ANSWER FOR A TOP-LEVEL KEY THIS SHAPE ITSELF FORBIDS,
        and the paragraph above is why that had to change. Where both apply the
        two refusals ARE redundant, and preempting the shape's own one costs the
        buyer the only thing either refusal delivers on the wire:

            canonical CreativeAsset, this validator winning
                issues[0].keyword  'validation'   -- NOT a JSON Schema keyword
                issues[0].message  'a declared constraint on this field rejected
                                    the value'
            the shape's own refusal
                issues[0].keyword  'not'          -- a Draft-7 keyword
                issues[0].message  'not composition failed'

        The remedy sentence below reaches NEITHER envelope. It is stripped on
        purpose: ``_OPAQUE_TYPES`` in ``adcp.validation.pydantic_errors`` replaces
        the message of any validator-raised ``ValueError``, because this SDK's
        validators interpolate caller values into those sentences. So the
        justification in the first paragraph -- "a buyer told a field is unusable
        needs to be told what to use instead" -- is unsatisfied by construction
        (salesagent-8qela.34), and what the preemption actually trades is a
        vocabulary keyword for an invented one, against
        ``schemas/cache/3.2/core/error.json``, which requires ``issues[].keyword``
        and says it is "drawn from the JSON Schema vocabulary ... do not invent
        custom values here".

        So the shape's refusal OWNS a top-level key the shape forbids. Measured
        over all fifteen (canonical model, removed name) pairs: exactly one --
        ``CreativeAsset.format_id`` -- has a shape that also forbids the name, so
        the other fourteen are unaffected and still get the path-reading refusal
        below. Keeping the remedy AND the keyword needs this validator to raise a
        structured error instead of a bare ``ValueError``; that is
        salesagent-8qela.106 and is deliberately not done here.
        """
        found = _legacy_creative_identity_path(
            value,
            allow_root_v1_ref=cls.__adcp_format_declaration_scope__,
            format_scope=cls.__adcp_format_declaration_scope__,
        )
        if found is not None and not _shape_forbids_top_level_key(cls, found):
            raise ValueError(
                f"{found} contains legacy creative identity; use an explicit Legacy* model"
            )
        return handler(value)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("serialize_as_any", False)
        value = super().model_dump(**kwargs)
        if _uses_canonical_serializer(type(self)):
            return value
        # A subclass can replace the inherited serializer. Keep the public
        # method's boundary for its output without rescanning ordinary dumps.
        cleaned: dict[str, Any] = strip_legacy_creative_identity(value)
        return cleaned

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("serialize_as_any", False)
        raw = super().model_dump_json(**kwargs)
        if _uses_canonical_serializer(type(self)):
            return raw
        clean = strip_legacy_creative_identity(json.loads(raw))
        indent = kwargs.get("indent")
        return json.dumps(
            clean,
            ensure_ascii=kwargs.get("ensure_ascii", False),
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
    # Declared so the spec's ``translation_target.token`` is validated as a
    # macro capability instead of being screened as a credential-shaped extra.
    macro_resolution_capabilities: list[MacroProcessingCapability] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Binding format-option processing capabilities for exact macro dialect "
            "identities, semantics, operations, actors, contexts, and encodings."
        ),
    )

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

    # Optional, because core/product.json requires it in no branch unconditionally:
    # the root's ``required`` is [product_id, name, description, publisher_properties,
    # delivery_type, pricing_options, reporting_capabilities], and ``format_options``
    # is required only inside the ``anyOf`` branch "Canonical-format product". An
    # anyOf branch's requirement is not the root's, so narrowing the ITEM type here
    # is right and dropping ``| None`` was not -- it made the other branch
    # unconstructible. ``placements`` two lines down is the same narrowing done
    # correctly, and ``pricing_options`` is required because the ROOT requires it.
    format_options: list[Format] | None = Field(  # type: ignore[assignment]
        default=None,
        min_length=1,
        description="Canonical creative formats accepted by this product.",
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
        # Annotated ``None``, not ``_RemovedFormatId``. This class binds the
        # canonical-format BRANCH, which types ``format_id`` as ``None``;
        # ``_RemovedFormatId`` is ``FormatReferenceStructuredObject | None`` and so
        # would WIDEN the parent, which mypy refuses and which pyright names as an
        # override error. A redeclaration restates the parent's annotation -- here
        # that is ``None`` -- and ``init=False`` is what removes the keyword from the
        # ``__init__`` both checkers synthesize through ``dataclass_transform``.
        #
        # ``None`` is the only spelling available here and it is the right one. mypy
        # refuses the alternative outright -- 'NoneType should not be used as a type,
        # please use None instead [nonetype-type]' -- so a gate that wanted
        # ``NoneType`` written in the source would have demanded a different spelling
        # of the same annotation than the type checker allows.
        # test_the_removed_field_is_hidden_from_the_type_checker therefore compares the
        # two sides as pydantic RESOLVES them rather than as they are written: both go
        # through a throwaway field, which is where ``None`` becomes ``NoneType``.
        # See salesagent-8qela.70.
        format_id: None = Field(default=None, init=False)

    # Nothing is declared here. The branch this subclasses requires ``format_kind``
    # and types ``format_id`` as ``None``, so the narrowing and the removal this
    # class used to spell by hand are the branch's own shape -- and the ``None``
    # annotation is stronger than the hidden-from-the-constructor trick it
    # replaces, because it is a type error at every call rather than only the
    # ones a type checker sees through ``init=False``.


class Creative(_CanonicalListedCreative, CanonicalBoundaryModel):
    """Canonical listed creative; the format kind is required, not optional.

    A listed creative is a row a seller RETURNS, so the kind is required but
    never confined: a kind a newer seller emits is retained as-is, which is
    what ``core/canonical-format-kind.json`` requires of a consumer.
    """

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_id: _RemovedFormatId = Field(default=None, init=False)

    format_kind: str


class CreativeManifest(_CreativeManifestRoot, CanonicalBoundaryModel):
    """Canonical manifest accepting the SDK's public standalone asset models.

    Bound to the generated ROOT, which is what the public name means: the root
    admits either branch and requires neither key. The branch classes stay
    reachable as branches and nothing about them changes.

    Safe only because salesagent-8qela.76 landed first. The root's XOR lives in
    its core schema, which a subclass does not inherit, so rebinding alone used
    to make this class ACCEPT a manifest carrying neither key -- a document the
    pin refuses. With the compiled check now on the shared base the subclass
    inherits it, and the measured table is: canonical ACCEPTS, neither REFUSES,
    both REFUSES.

    ``format_id`` is still removed at runtime by
    ``CanonicalBoundaryModel.__pydantic_init_subclass__``, so the deprecated
    named-format branch remains unconstructible here. That is INHERITED, true at
    upstream too, and tracked as salesagent-8qela.70 -- deliberately not bundled.

    The 3.2 aggregate asset-union schema currently generates structurally
    duplicate Pydantic classes. Convert public ``ImageContent``/``UrlContent``
    (and peers) back to their wire dictionaries before the aggregate union
    validates them. This keeps the public constructors composable without
    relaxing the on-wire discriminator checks.
    """

    if TYPE_CHECKING:  # the removed field, hidden from the constructor too
        format_id: _RemovedFormatId = Field(default=None, init=False)

    # ``format_id`` is typed ``None`` by the branch; see :class:`CreativeAsset`.

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


class _DeliveryCreativeManifest(_CreativeManifestRoot, CanonicalBoundaryModel):
    """The buyer's manifest, read back off a delivery row.

    **It binds the ROOT, the same node :class:`CreativeManifest` binds.**
    ``core/get-creative-delivery-response.json`` reaches this field through
    ``$ref .../core/creative-manifest.json``, and ``core/creative-variant.json``
    reaches the INPUT manifest through the same ``$ref``. One schema node refined
    twice, so the two refinements owe a document the same verdict.

    They did not give it. This class used to bind
    ``CreativeManifestCanonicalFormatManifest`` — oneOf[1] — where ``format_kind``
    is ``required``, while :class:`CreativeManifest` binds the root, whose
    ``required`` is ``['assets']``. Measured, the disagreement was in two places
    and neither was the vocabulary:

    * STATICALLY, ``format_kind`` read ``str`` here and ``str | None`` there.
      That is the ``mypy --strict`` error ``creative_asset_binding.py:82``
      carried, and the ratchet in ``tests/test_type_check_fixtures.py`` that
      held it is retired with this change.
    * IN THE REFUSAL, on ``{"assets": {}}``: the root-bound class names the
      root's combinator (``type='oneOf'``), the arm-bound one reported a
      ``missing`` field and never reached the combinator.
      ``core/error.json`` requires an issue to carry a JSON Schema keyword and
      ``missing`` is not one.

    Binding the root RELAXES NOTHING at runtime. The root's ``oneOf`` is enforced
    by the root's own conjunct validator, not by
    :class:`~adcp.types._branch_dispatch.BranchDispatchMixin`'s branch union —
    the mixin hands the union only to the class that DECLARES ``_branch_models``,
    and a refinement declared here does not. Measured against the input manifest
    on all three documents: canonical arm accepted by both, neither-arm refused
    ``oneOf`` by both, both-arms refused ``value_error`` by both
    (``test_readback_manifest_grades_a_document_exactly_as_the_input_manifest``).

    The one behavior it does move is on
    ``test_delivery_manifest_cannot_bypass_strict_input``: an instance of this
    class is an instance of the root and of NEITHER arm, so revalidating the
    INSTANCE through the generated root's union is refused as ``model_type``
    where it used to be taken. That refusal is the obligation #1241 asked for,
    and it now holds uniformly on all four (response model x input class) pairs
    instead of three — a strengthening, which is why the test's conditional
    collapsed rather than being relaxed.

    It redeclares nothing about the format-kind vocabulary. ``format_kind`` is
    ``str | None`` here and on :class:`CreativeManifest`, inherited from the root
    in both cases, neither refuses a value, and the ``_StrictFormatKind`` mixin
    that used to be the only difference between the two is deleted. **This class
    is not a tolerant variant of anything.**

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

    variants: list[_DeliveryCreativeVariant] = _inherit(_LegacyDeliveryCreative, "variants")


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
    # The generated parent carries ``TargetingOverlayMutation``. A canonical model
    # materializes JSON in its before validator, so this subtree re-enters core JSON
    # validation to keep native error codes and extra/missing order -- which is the
    # only difference between the two aliases.
    targeting_overlay: TargetingOverlayMutationInMaterializedJson = mutation_overlay_field(
        _LegacyPackageRequest
    )

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
    # See PackageRequest.targeting_overlay above.
    targeting_overlay: TargetingOverlayMutationInMaterializedJson = mutation_overlay_field(
        _LegacyPackageUpdate
    )


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


class CreateMediaBuyRequest(_LegacyCreateMediaBuyBase, CanonicalBoundaryModel):
    """Canonical create request; packages are canonical package requests.

    This class carries the branches' core schema, so parsing it -- by name,
    nested in another model, or through a ``TypeAdapter`` -- returns the
    canonical branch the document satisfies.

    ``packages`` is narrowed at the BASE's requiredness, not at a branch's. A
    base cannot carry one requiredness for a field whose requiredness is what
    distinguishes the branches, so each branch re-declares the narrowing at its
    own. The generated branch is listed FIRST in each branch's bases, so the
    branch's requiredness wins by MRO position, and ``CanonicalBoundaryModel``
    is listed LAST so ``model_config`` resolves from the canonical side.
    """

    packages: Sequence[PackageRequest] | None = None

    @classmethod
    def _branch_models(cls) -> tuple[type, ...]:
        # Re-declared, which is what moves the dispatch from the GENERATED
        # branches to the canonical ones: the shared hook asks whether a class
        # declares this method, so the generated base of the same name keeps its
        # own branches and this one gets these. Inheriting it instead left the
        # method reporting the generated branches while the schema was built
        # from the canonical ones -- two answers to one question.
        return (
            CreateMediaBuyRequestExplicitPackagesWithFixedAllocation,
            CreateMediaBuyRequestExplicitPackagesWithSellerOptimizedAllocation,
            CreateMediaBuyRequestCommittedProposalExecution,
        )

    @classmethod
    def _branch_combinator(cls) -> str:
        return "anyOf"


class CreateMediaBuyRequestExplicitPackagesWithFixedAllocation(
    _LegacyCreateMediaBuyFixed, CreateMediaBuyRequest
):
    """Canonical fixed-allocation create request; packages are canonical.

    ``packages`` is re-declared to carry the CANONICAL item type. Its
    requiredness comes from the generated branch by MRO position, not from this
    line -- dropping the line keeps the field required and loses the narrowing.
    """

    packages: Sequence[PackageRequest]


class CreateMediaBuyRequestExplicitPackagesWithSellerOptimizedAllocation(
    _LegacyCMBSellerOptimized, CreateMediaBuyRequest
):
    """Canonical seller-optimized create request; packages are canonical."""

    packages: Sequence[PackageRequest]


class CreateMediaBuyRequestCommittedProposalExecution(
    _LegacyCreateMediaBuyProposal, CreateMediaBuyRequest
):
    """Canonical proposal-execution create request.

    ``packages`` is re-declared as ``None`` because this branch FORBIDS it. The
    canonical base narrows the field for the branches that permit it, and that
    narrowing and this ban are incompatible declarations inherited from two
    bases -- so the ban is restated here, where it is true, rather than silenced.
    Re-declaring it as a canonical list would re-widen what the branch bans.
    """

    packages: None = None


#: The canonical create request's branches. Annotate this to make a type checker
#: force exhaustive narrowing; annotate CreateMediaBuyRequest for the same
#: strictness at runtime under one name.
CreateMediaBuyRequestBranches: TypeAlias = Annotated[
    CreateMediaBuyRequestExplicitPackagesWithFixedAllocation
    | CreateMediaBuyRequestExplicitPackagesWithSellerOptimizedAllocation
    | CreateMediaBuyRequestCommittedProposalExecution,
    WrapValidator(_conditions.name_the_combinator("anyOf", CreateMediaBuyRequest._branch_models())),
]

# No module-level ``CreateMediaBuyRequestBranchesAdapter``: a ``TypeAdapter``
# builds its validator AT IMPORT, so it captures schema state once and shares it
# with every test in the process, which is what
# ``test_sdk_has_no_module_level_type_adapter_construction`` forbids. This file
# is hand-written and carried the same declaration the generator emitted; both
# are gone. ``CreateMediaBuyRequestBranches`` above is the public name and stays
# reachable, and a caller who wants an adapter writes
# ``TypeAdapter(CreateMediaBuyRequestBranches)``.

# CreateMediaBuyRequest is declared above its branches, so _branch_models
# cannot resolve them while the class body is compiled. The rebuild installs
# the branches' schema -- the same two-step every other root with branches uses.
CreateMediaBuyRequest.model_rebuild(force=True)


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
    """Canonical create-media-buy error branch."""


class CreateMediaBuyResponse3(_LegacyCreateMediaBuyResponse3, CanonicalBoundaryModel):
    """Canonical create-media-buy submitted branch."""


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
    """Canonical update-media-buy error branch."""


class UpdateMediaBuyResponse3(_LegacyUpdateMediaBuyResponse3, CanonicalBoundaryModel):
    """Canonical update-media-buy submitted branch."""


UpdateMediaBuyResponse = UpdateMediaBuyResponse1 | UpdateMediaBuyResponse2 | UpdateMediaBuyResponse3


class SyncCreativesRequest(_LegacySyncCreativesRequest, CanonicalBoundaryModel):
    """Canonical creative sync request; creatives are canonical assets."""

    # Optional, matching ``PackageRequest.creatives`` above and the generated parent:
    # creative/sync-creatives-request.json requires only [idempotency_key, account] at
    # the root and ``creatives`` only inside an untitled ``anyOf`` branch. Requiring it
    # here made the SDK refuse a schema-valid sync_creatives request that takes the
    # other branch -- a request model, so this was a refusal and not a typing
    # inconvenience. Narrowing the item type to the canonical asset is the part that
    # was correct.
    creatives: list[CreativeAsset] | None = Field(  # type: ignore[assignment]
        default=None, min_length=1
    )


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
    # One entry per branch: the create request is a union now, and each branch is a
    # canonical model in its own right -- every property this tuple is graded
    # for has to hold of each of them, not of the alias over them.
    CreateMediaBuyRequestExplicitPackagesWithFixedAllocation,
    CreateMediaBuyRequestExplicitPackagesWithSellerOptimizedAllocation,
    CreateMediaBuyRequestCommittedProposalExecution,
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
    "CreateMediaBuyRequestBranches",
    "CreateMediaBuyRequestCommittedProposalExecution",
    "CreateMediaBuyRequestExplicitPackagesWithFixedAllocation",
    "CreateMediaBuyRequestExplicitPackagesWithSellerOptimizedAllocation",
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
