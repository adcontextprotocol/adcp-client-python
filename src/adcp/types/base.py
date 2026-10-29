from __future__ import annotations

"""Base model for structural AdCP application types and wire serialization.

Generated Pydantic models provide typed construction, common field
constraints, and spec-shaped serialization. Complete protocol conformance is
defined by the bundled canonical JSON Schemas; validate at wire boundaries
with :mod:`adcp.validation`. In particular, code generation cannot represent
every JSON Schema conditional or ``x-adcp-validation`` behavioral rule.
"""

import copy
import os
import warnings
from collections.abc import Callable
from contextvars import ContextVar
from functools import lru_cache
from types import NoneType, UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Final,
    Generic,
    Literal,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
)

from pydantic import (
    AfterValidator,
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    GetCoreSchemaHandler,
    RootModel,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    StrictInt,
    StrictStr,
    TypeAdapter,
    WithJsonSchema,
    model_serializer,
)
from pydantic._internal._mock_val_ser import MockValSer, set_model_mocks
from pydantic.fields import FieldInfo
from pydantic_core import PydanticSerializationError, PydanticUndefined, core_schema

from adcp._deferred_adapters import deferred_adapter
from adcp.types._geo_place_keys import GeoPlaceSystemKey as GeoPlaceSystemKey
from adcp.types._scalar import as_json_schema_integer

# Re-exported for the generated modules, beside ``WireUrl`` (#1384): the
# wire-preserving types ``scripts/post_generate_fixes.py`` points format fields
# at. To a type checker each IS the pydantic type it replaces -- a generated
# ``last_updated`` reads as ``datetime``, a ``url`` as ``AnyUrl`` -- so an
# adopter's ``assert_type``, assignment or constructor call type-checks exactly
# as it did; at runtime the value is the subclass that remembers its wire text.
# ``wire_text`` is the typed way to read that text.
from adcp.types._wire import WireEmail as WireEmail
from adcp.types._wire import wire_text as wire_text

if TYPE_CHECKING:
    import uuid as _uuid
    from typing import TypeAlias

    from pydantic import AwareDatetime as _AwareDatetime

    WireDatetime: TypeAlias = _AwareDatetime
    WireAnyUrl: TypeAlias = AnyUrl
    WireUuid: TypeAlias = _uuid.UUID
else:
    from adcp.types._wire import WireAnyUrl as WireAnyUrl
    from adcp.types._wire import WireDatetime as WireDatetime
    from adcp.types._wire import WireUuid as WireUuid

if TYPE_CHECKING:
    from typing_extensions import Self

    # Annotation-only: resolving these at runtime would import the generated tree, which
    # imports this module. ``from __future__ import annotations`` keeps them as strings.
    # Taken by their public domain path rather than out of ``_generated``: the domain
    # modules mirror the schema tree, so these names do not renumber on a regeneration
    # the way the generated module's do. A ``TYPE_CHECKING`` block never executes, so
    # the layering rule costs nothing here.
    from adcp.types.domains.core.account_ref import AccountReference1, AccountReference2
    from adcp.types.domains.core.canonical_account_ref import CanonicalAccountReference
    from adcp.types.domains.core.context import ContextObject
    from adcp.types.domains.core.error import Error
    from adcp.types.domains.core.push_notification_config import PushNotificationConfig
    from adcp.types.domains.enums.task_status import TaskStatus

# Type alias to shorten long type annotations
MessageFormatter = Callable[[Any], str]


def _resolve_extra_policy() -> Literal["allow", "forbid"]:
    """Choose the ``extra`` policy for :class:`AdCPBaseModel`.

    ``allow`` (default) keeps an unknown field in ``model_extra`` and writes
    it back on dump. The generator sets ``extra='forbid'`` on every class
    whose schema object is closed (``additionalProperties: false``) and
    ``extra='allow'`` where the schema says ``true``; a class whose schema
    says nothing inherits this default, which is JSON Schema's own reading of
    an absent ``additionalProperties``. A document that carried a property the
    pinned model does not declare -- a field a newer spec added, a sibling
    arm's property the schema admits -- therefore survives parse → dump
    (#1614). SDK 9 dropped it (``ignore``).

    ``forbid`` raises on unknown fields so a test run catches a stray keyword
    argument or a renamed field. Set ``ADCP_STRICT_VALIDATION=1`` in CI during
    upgrades, then ship with the flag unset.

    Values accepted: ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (any
    case). Anything else — including empty string and ``"0"`` — keeps
    the ``allow`` default.
    """
    raw = os.environ.get("ADCP_STRICT_VALIDATION", "").strip().lower()
    return "forbid" if raw in {"1", "true", "yes", "on"} else "allow"


_EXTRA_POLICY: Literal["allow", "forbid"] = _resolve_extra_policy()


def _build_deferred_serializers(value: Any, seen: set[int]) -> None:
    """Force lazy core-schema builds for every model instance in a graph.

    With ``defer_build=True`` a model class used only as a nested field keeps a
    placeholder serializer until it is first built. ``serialize_as_any=True``
    (set by :meth:`AdCPBaseModel.model_dump`) makes pydantic-core dispatch
    serialization to each nested instance's *own* class serializer, a path that
    does not trigger the lazy build. This walks the instance graph and rebuilds
    any class whose core schema is still deferred, so only the model classes
    that actually appear in serialized payloads get built — preserving the
    import-time memory saving while keeping serialization correct.
    """
    if isinstance(value, BaseModel):
        ident = id(value)
        if ident in seen:
            return
        seen.add(ident)
        cls = type(value)
        if not cls.__pydantic_complete__:
            cls.model_rebuild(force=False)
        for field_value in value.__dict__.values():
            _build_deferred_serializers(field_value, seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _build_deferred_serializers(item, seen)
    elif isinstance(value, dict):
        for item in value.values():
            _build_deferred_serializers(item, seen)


class _DeferredValSer(MockValSer[Any]):
    """pydantic's rebuild-on-access placeholder, extended to instance attribute access.

    ``MockValSer`` rebuilds the model when one of its *methods* is accessed,
    which covers ``Model(...)`` and ``instance.model_dump()``. pydantic-core
    reaches a serializer another way: when it serializes a model instance it
    meets by inference — an ``Any``-typed field, ``serialize_as_any``, a
    plain-pydantic parent — it reads ``instance.__pydantic_serializer__`` and
    downcasts, which a placeholder fails with ``TypeError: 'MockValSer' object
    is not an instance of 'SchemaSerializer'`` (#1310, #1339). A placeholder
    that is also a descriptor builds the model on that instance-level read and
    hands back the real object. Class-level reads still return the placeholder,
    which is what pydantic's own ``isinstance(..., MockValSer)`` checks expect.
    """

    __slots__ = ("_attr",)

    def __init__(self, mock: MockValSer[Any], attr: str) -> None:
        super().__init__(
            mock._error_message,
            code=mock._code,
            val_or_ser="validator" if attr == "__pydantic_validator__" else "serializer",
            attempt_rebuild=mock._attempt_rebuild,
        )
        self._attr = attr

    def __get__(self, instance: object, owner: type[BaseModel] | None = None) -> Any:
        if instance is None or owner is None:
            return self
        # An empty parent namespace: the model's own module and
        # ``__pydantic_parent_namespace__`` resolve everything a generated or
        # adopter class declares at module level. A class that still needs the
        # caller's locals keeps the placeholder, and pydantic's frame-walking
        # rebuild runs on the next method access as before.
        if owner.model_rebuild(raise_errors=False, _parent_namespace_depth=0) is False:
            return self
        return getattr(owner, self._attr)


def _install_deferred_valsers(cls: type[BaseModel]) -> None:
    """Swap pydantic's placeholders on ``cls`` for ones that build on instance access."""
    for attr in ("__pydantic_validator__", "__pydantic_serializer__"):
        mock = cls.__dict__.get(attr)
        if isinstance(mock, MockValSer) and not isinstance(mock, _DeferredValSer):
            setattr(cls, attr, _DeferredValSer(mock, attr))


def defer_model_rebuild(model: type[BaseModel]) -> None:
    """Discard ``model``'s built core schema so pydantic rebuilds it on first use.

    The import-time patchers (``_ergonomic``, ``_forward_compat``,
    ``canonical_creative``) edit ``model_fields`` in place and then need the
    core schema re-derived. ``model_rebuild(force=True)`` does that
    immediately: it walks the model's whole nested subtree into a fresh
    pydantic-core validator and serializer, and pydantic does not share the
    schemas of nested ``defer_build`` models between parents, so each forced
    rebuild of a request or response root costs tens of MiB and hundreds of
    milliseconds — at import, for models the process may never use (#1694).

    This helper instead puts the model back into the state ``defer_build=True``
    leaves a freshly declared class in: the built artefacts are replaced by
    pydantic's rebuild-on-access placeholders, and the next validation,
    serialization or schema request builds the schema from the patched fields.
    The outcome is identical to a forced rebuild; only the moment differs.
    :func:`adcp.testing.build_all_models` still realizes every model eagerly
    for callers (freezegun) that need the build to happen before patching
    ``datetime``.

    The placeholders are pydantic's own (``pydantic._internal._mock_val_ser``);
    the pinned ``pydantic<2.14`` range is what makes relying on them safe, and
    ``tests/test_lazy_rebuild.py`` grades the contract.
    """
    model.__pydantic_complete__ = False
    for attr in ("__pydantic_core_schema__", "__pydantic_validator__", "__pydantic_serializer__"):
        if attr in model.__dict__ and not isinstance(getattr(model, attr), MockValSer):
            delattr(model, attr)
    set_model_mocks(model)
    _install_deferred_valsers(model)


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """Return singular or plural form based on count."""
    if count == 1:
        return singular
    return plural if plural else f"{singular}s"


# Registry of human-readable message formatters for response types.
# Key is the class name, value is a callable that takes the instance and returns a message.
_RESPONSE_MESSAGE_REGISTRY: dict[str, MessageFormatter] = {}


def _register_response_message(cls_name: str) -> Callable[[MessageFormatter], MessageFormatter]:
    """Decorator to register a message formatter for a response type."""

    def decorator(func: MessageFormatter) -> MessageFormatter:
        _RESPONSE_MESSAGE_REGISTRY[cls_name] = func
        return func

    return decorator


# Response message formatters
@_register_response_message("GetProductsResponse")
def _get_products_message(self: Any) -> str:
    products = getattr(self, "products", None)
    if products is None or len(products) == 0:
        return "No products matched your requirements."
    count = len(products)
    return f"Found {count} {_pluralize(count, 'product')} matching your requirements."


@_register_response_message("ListCreativeFormatsResponse")
def _list_creative_formats_message(self: Any) -> str:
    formats = getattr(self, "formats", None)
    if formats is None:
        return "No creative formats found."
    count = len(formats)
    return f"Found {count} supported creative {_pluralize(count, 'format')}."


@_register_response_message("GetSignalsResponse")
def _get_signals_message(self: Any) -> str:
    signals = getattr(self, "signals", None)
    if signals is None:
        return "No signals found."
    count = len(signals)
    return f"Found {count} {_pluralize(count, 'signal')} available for targeting."


@_register_response_message("ListCreativesResponse")
def _list_creatives_message(self: Any) -> str:
    creatives = getattr(self, "creatives", None)
    if creatives is None:
        return "No creatives found."
    count = len(creatives)
    return f"Found {count} {_pluralize(count, 'creative')} in the system."


@_register_response_message("CreateMediaBuyResponse1")
def _create_media_buy_success_message(self: Any) -> str:
    media_buy_id = getattr(self, "media_buy_id", None)
    packages = getattr(self, "packages", None)
    package_count = len(packages) if packages else 0
    return (
        f"Media buy {media_buy_id} created with "
        f"{package_count} {_pluralize(package_count, 'package')}."
    )


@_register_response_message("CreateMediaBuyResponse2")
def _create_media_buy_error_message(self: Any) -> str:
    errors = getattr(self, "errors", None)
    error_count = len(errors) if errors else 0
    return f"Media buy creation failed with {error_count} {_pluralize(error_count, 'error')}."


@_register_response_message("UpdateMediaBuyResponse1")
def _update_media_buy_success_message(self: Any) -> str:
    media_buy_id = getattr(self, "media_buy_id", None)
    return f"Media buy {media_buy_id} updated successfully."


@_register_response_message("UpdateMediaBuyResponse2")
def _update_media_buy_error_message(self: Any) -> str:
    errors = getattr(self, "errors", None)
    error_count = len(errors) if errors else 0
    return f"Media buy update failed with {error_count} {_pluralize(error_count, 'error')}."


@_register_response_message("SyncCreativesResponse1")
def _sync_creatives_success_message(self: Any) -> str:
    creatives = getattr(self, "creatives", None)
    creative_count = len(creatives) if creatives else 0
    return f"Synced {creative_count} {_pluralize(creative_count, 'creative')} successfully."


@_register_response_message("SyncCreativesResponse2")
def _sync_creatives_error_message(self: Any) -> str:
    errors = getattr(self, "errors", None)
    error_count = len(errors) if errors else 0
    return f"Creative sync failed with {error_count} {_pluralize(error_count, 'error')}."


@_register_response_message("ActivateSignalResponse1")
def _activate_signal_success_message(self: Any) -> str:
    return "Signal activated successfully."


@_register_response_message("ActivateSignalResponse2")
def _activate_signal_error_message(self: Any) -> str:
    errors = getattr(self, "errors", None)
    error_count = len(errors) if errors else 0
    return f"Signal activation failed with {error_count} {_pluralize(error_count, 'error')}."


@_register_response_message("PreviewCreativeResponse1")
def _preview_creative_single_message(self: Any) -> str:
    previews = getattr(self, "previews", None)
    preview_count = len(previews) if previews else 0
    return f"Generated {preview_count} {_pluralize(preview_count, 'preview')}."


@_register_response_message("PreviewCreativeResponse2")
def _preview_creative_batch_message(self: Any) -> str:
    results = getattr(self, "results", None)
    result_count = len(results) if results else 0
    return f"Generated previews for {result_count} {_pluralize(result_count, 'manifest')}."


@_register_response_message("BuildCreativeResponse1")
def _build_creative_success_message(self: Any) -> str:
    return "Creative built successfully."


@_register_response_message("BuildCreativeResponse2")
def _build_creative_error_message(self: Any) -> str:
    errors = getattr(self, "errors", None)
    error_count = len(errors) if errors else 0
    return f"Creative build failed with {error_count} {_pluralize(error_count, 'error')}."


@_register_response_message("GetMediaBuyDeliveryResponse")
def _get_media_buy_delivery_message(self: Any) -> str:
    deliveries = getattr(self, "media_buy_deliveries", None)
    if deliveries is None:
        return "No delivery data available."
    count = len(deliveries)
    return f"Retrieved delivery data for {count} media {_pluralize(count, 'buy', 'buys')}."


@_register_response_message("ProvidePerformanceFeedbackResponse1")
def _provide_performance_feedback_success_message(self: Any) -> str:
    return "Performance feedback recorded successfully."


@_register_response_message("ProvidePerformanceFeedbackResponse2")
def _provide_performance_feedback_error_message(self: Any) -> str:
    errors = getattr(self, "errors", None)
    error_count = len(errors) if errors else 0
    return (
        f"Performance feedback recording failed with "
        f"{error_count} {_pluralize(error_count, 'error')}."
    )


#: The annotation for a schema's ``type: integer``. Accepts an ``int`` and a
#: float with no fractional part; refuses a fractional float, a bool and a
#: numeric string, which is exactly what the bundled validator does for the
#: same field. The generator marks every such field and
#: ``scripts/post_generate_fixes.py`` points the marker here.
SchemaInt = Annotated[StrictInt, BeforeValidator(as_json_schema_integer)]


def _require_absolute_url(value: str) -> str:
    """Validate ``value`` as a URL and return it unchanged.

    ``AnyUrl`` normalizes what it parses — ``https://creative.adcontextprotocol.org``
    comes back with a trailing slash — and a format reference's ``agent_url``
    is hashed byte for byte into the ``migrated_…`` option ID (#1384). The
    wire string is the identity, so it is validated with the same parser the
    other ``format: uri`` fields use and then kept as sent.
    """
    _url_adapter().validate_python(value)
    return value


@deferred_adapter
def _url_adapter() -> TypeAdapter[AnyUrl]:
    return TypeAdapter(AnyUrl)


#: A ``format: uri`` string whose bytes are part of an identity. Validated as a
#: URL, carried as the ``str`` the wire delivered. ``scripts/post_generate_fixes.py``
#: points the format-reference ``agent_url`` here. Its siblings for a field that
#: keeps the parsed type -- :class:`WireAnyUrl`, :class:`WireDatetime`,
#: :class:`WireUuid` -- and the ``str``-typed :data:`WireEmail` live in
#: :mod:`adcp.types._wire` and are re-exported here for the generated modules.
WireUrl = Annotated[
    StrictStr,
    AfterValidator(_require_absolute_url),
    WithJsonSchema({"type": "string", "format": "uri"}),
]


def _without_nullable(schema: Any) -> core_schema.CoreSchema:
    """The schema with its ``nullable`` wrapper removed, if it has one.

    The wrapper is usually outermost. A field another annotation already
    rebuilt — the targeting-overlay bridge builds its own ``json_or_python``
    with validator functions around the type — carries it further in, and
    only the JSON side of such a schema matters here.
    """
    kind = schema["type"]
    if kind == "nullable":
        return cast(core_schema.CoreSchema, schema["schema"])
    if kind == "json-or-python":
        return cast(
            core_schema.CoreSchema,
            {**schema, "json_schema": _without_nullable(schema["json_schema"])},
        )
    if kind in ("function-before", "function-after", "function-wrap", "json", "default"):
        stripped = {**schema, "schema": _without_nullable(schema["schema"])}
        # A validator function may publish a separate input schema; the JSON
        # Schema is generated from that one when it is present.
        if "json_schema_input_schema" in stripped:
            stripped["json_schema_input_schema"] = _without_nullable(
                stripped["json_schema_input_schema"]
            )
        return cast(core_schema.CoreSchema, stripped)
    return cast(core_schema.CoreSchema, schema)


def _is_none(value: Any) -> bool:
    """``exclude_if`` for a :data:`NoExplicitNull` field: ``None`` is "not supplied"."""
    return value is None


class _RefuseJsonNull:
    """The validation half of :data:`NoExplicitNull`: a JSON ``null`` is refused.

    Builds a ``json_or_python`` core schema: the JSON side is the type without
    its null arm, so a document carrying ``null`` fails with the type's own
    error (``list_type``, ``model_type``, ...) exactly as a required field
    would, and ``model_json_schema()`` publishes no null arm; the Python side
    keeps ``T | None``, so ``Model(field=None)`` and ``model_validate`` on a
    dict still read ``None`` as the SDK's spelling of "not supplied". The
    ``= None`` default is not validated, so omission reads back as ``None``
    either way. No Python function sits in the validation path, so nested
    validation keeps pydantic's native JSON handling (duplicate-key
    diagnostics, error order).

    ``ADCP_STRICT_VALIDATION`` — the switch that also forbids unknown
    fields — makes the Python side refuse ``None`` too, for suites that want
    to catch a caller spelling absence as an explicit ``None``.
    """

    __slots__ = ()

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        nullable = handler(source)
        strict = _without_nullable(nullable)
        return core_schema.json_or_python_schema(
            json_schema=strict,
            python_schema=strict if _EXTRA_POLICY == "forbid" else nullable,
        )

    def __repr__(self) -> str:
        return "NoExplicitNull"

    # One marker, however a ``FieldInfo`` carrying it is copied: a canonical
    # redeclaration takes ``copy.deepcopy`` of the generated field
    # (``canonical_creative._inherit``), and a test asks ``NoExplicitNull in
    # field.metadata``.
    def __copy__(self) -> _RefuseJsonNull:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _RefuseJsonNull:
        return self

    def __reduce__(self) -> str:
        return "_REFUSE_JSON_NULL"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _RefuseJsonNull)

    def __hash__(self) -> int:
        return hash(_RefuseJsonNull)


_REFUSE_JSON_NULL = _RefuseJsonNull()


#: Marks an optional property whose schema does not admit ``null``: omit it or
#: supply a value. A JSON ``null`` is refused on input, and a ``None`` is left
#: off the output from any parent.
#:
#: A JSON Schema property outside ``required`` whose type does not admit
#: ``null`` permits exactly one way of not supplying it — leaving it out. The
#: generator renders every optional property as ``T | None = None``, and that
#: annotation is the right Python surface: the attribute reads as ``None`` when
#: the property was omitted. What the annotation got wrong was *input*: it also
#: admitted an explicit ``null``, which the schema — and the SDK's own
#: JSON-Schema arm, :func:`adcp.validation.validate_request` — rejects. This
#: marker closes that gap without changing the annotation.
#:
#: The marker has two halves, because the two jobs live at different levels of
#: pydantic:
#:
#: * **Validation** (:class:`_RefuseJsonNull`, carried in ``metadata``): the
#:   JSON side of the field's core schema has no null arm, so a document
#:   carrying ``null`` is refused and the published JSON Schema states what the
#:   bundled schema states. A Python ``None`` stays accepted.
#: * **Serialization** (the ``FieldInfo`` itself, ``exclude_if=_is_none``): a
#:   ``None`` is left off the output *by the field*, in pydantic-core, whoever
#:   drives the dump. ``AdCPBaseModel.model_dump`` defaults
#:   ``exclude_none=True``, but a plain pydantic parent's serializer never calls
#:   that override, so before #1587 a nested AdCP model wrote ``"ext": null``
#:   that the same model then refused to read back. The per-field exclusion is
#:   what makes ``model_validate_json(model_dump_json())`` round-trip from any
#:   parent: a direct field, a list, a dict, a ``RootModel`` over a union. It
#:   also applies under an explicit ``exclude_none=False``, which is always
#:   safe: ``null`` is never a valid value for a marked property, so there is
#:   nothing to keep. A property whose schema *does* admit ``null`` is generated
#:   without the marker and keeps emitting ``null`` under ``exclude_none=False``.
#:
#: Pydantic merges a ``FieldInfo`` found in ``Annotated`` metadata into the
#: field (``exclude_if`` lands on the field, the inner marker lands in
#: ``field.metadata``), so one ``NoExplicitNull`` beside the generated
#: ``Field(...)`` is all a field needs, and ``copy.deepcopy`` of the merged
#: ``FieldInfo`` (``canonical_creative._inherit``) carries both halves. The
#: marker itself is therefore never in ``field.metadata``; ask
#: :func:`is_no_explicit_null`, which reads the exclusion by identity. Not a
#: subclass: pydantic declares ``FieldInfo`` final.
#:
#: The generator marks every such property from the schema
#: (``scripts/generate_types.py``, ``mark_no_explicit_null_properties``) and
#: ``scripts/post_generate_fixes.py`` points the mark here. See #1547 and #1587.
NoExplicitNull: Final[FieldInfo] = FieldInfo(exclude_if=_is_none)
NoExplicitNull.metadata.append(_REFUSE_JSON_NULL)


def inherit_field(source: type[BaseModel], name: str, *, required: bool = False) -> Any:
    """A copy of ``source``'s ``name`` field, for a redeclaration that retypes it.

    A hand-written subclass that narrows an inherited field's *annotation*
    (``list[Format]`` for the generated ``list[FormatOption]``) still wants
    everything else the generated field carries: its description, its
    constraints (``min_length``, ``max_length``), its default, and the
    never-null marker (:data:`NoExplicitNull`) that leaves an unset ``None``
    off the wire from any parent (#1591). A bare ``Field(...)`` on the
    redeclaration silently drops all of them -- which is how a nested dump of
    a canonical ``Product`` came to write ``"placements": null`` (#1620), the
    same class of defect as #1548 and #1579.

    Assign the result on the right-hand side of the annotated redeclaration.
    ``required=True`` drops the default and the never-null marker, for a
    subclass that makes an optional generated field mandatory: a required
    field has no "omitted" for the marker to spell. The return type is
    ``Any`` because a ``FieldInfo`` is what pydantic expects there -- the same
    reason ``Field()`` itself is ``Any``.
    """
    field = copy.deepcopy(source.model_fields[name])
    if required:
        field.default = PydanticUndefined
        field.default_factory = None
        # The marker belongs to an optional property (omit it, or supply a
        # value); a required field has no "omitted" to spell, and the tree-wide
        # invariant is that every marked field is optional.
        field.metadata = [
            item
            for item in field.metadata
            if item is not _REFUSE_JSON_NULL and item is not OmitUnsetDefault
        ]
        if field.exclude_if is NoExplicitNull.exclude_if:
            field.exclude_if = None
    return field


def is_no_explicit_null(field: FieldInfo) -> bool:
    """Whether ``field`` carries :data:`NoExplicitNull`.

    Identity, not equality: the marker is merged into the field, so what the
    field keeps is its ``exclude_if`` (and, in ``metadata``, its validation
    half). A redeclaration that copies the generated ``FieldInfo`` keeps both.
    """
    return field.exclude_if is NoExplicitNull.exclude_if


class _OmitUnsetDefault:
    """Marks a schema-optional property that carries a schema ``default``.

    The generator writes the schema default into the field (``sandbox: bool =
    False``), which is the right Python surface: the attribute reads as the
    default when the property was omitted. On the wire, though, "omitted" and
    "present with the default" are different bytes, and a digest or echo over
    the document tells them apart (#1613). A field carrying this marker is
    left off the default dump unless it is in ``model_fields_set`` -- the wire
    set it, or the caller did. A schema-required property with a default
    (and a generator-injected discriminator default) carries no marker and is
    emitted either way.

    Carried in ``Annotated`` metadata; pydantic keeps unknown metadata on
    ``FieldInfo.metadata``. One marker however the field is copied, like
    :class:`_RefuseJsonNull`.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "OmitUnsetDefault"

    def __copy__(self) -> _OmitUnsetDefault:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _OmitUnsetDefault:
        return self

    def __reduce__(self) -> str:
        return "OmitUnsetDefault"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _OmitUnsetDefault)

    def __hash__(self) -> int:
        return hash(_OmitUnsetDefault)


#: The marker the generator appends to a schema-optional property with a
#: schema ``default`` (``scripts/generate_types.py`` marks it,
#: ``scripts/post_generate_fixes.py`` points it here). See
#: :class:`_OmitUnsetDefault`.
OmitUnsetDefault: Final = _OmitUnsetDefault()


def is_omit_unset_default(field: FieldInfo) -> bool:
    """Whether ``field`` carries :data:`OmitUnsetDefault`."""
    return any(item is OmitUnsetDefault for item in field.metadata)


@lru_cache(maxsize=4096)
def omit_unset_default_fields(model: type[BaseModel]) -> frozenset[str]:
    """The fields of ``model`` the default dump leaves off unless they were set."""
    return frozenset(
        name for name, field in model.model_fields.items() if is_omit_unset_default(field)
    )


#: Set for the duration of a dump the caller asked for with
#: ``exclude_unset=False``: every field is emitted, defaults included, which is
#: the SDK 9 output.
_EMIT_UNSET_DEFAULTS: ContextVar[bool] = ContextVar("adcp_emit_unset_defaults", default=False)


def _admits_none(annotation: Any) -> bool:
    """Whether ``annotation`` accepts ``None`` as a value."""
    if annotation is Any or annotation is NoneType:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        return _admits_none(get_args(annotation)[0])
    if origin is Union or origin is UnionType:
        return any(_admits_none(arm) for arm in get_args(annotation))
    return False


@lru_cache(maxsize=4096)
def null_admitting_fields(model: type[BaseModel]) -> frozenset[str]:
    """The fields of ``model`` whose schema admits an explicit ``null``.

    A generated optional property is ``T | None = None``; when its schema does
    not admit ``null`` it also carries :data:`NoExplicitNull`, which refuses a
    JSON ``null`` and leaves a ``None`` off every dump. The fields left over --
    an annotation that admits ``None`` and no marker -- are the ones a wire
    ``null`` is a legal value for, and the ones :class:`AdCPBaseModel`
    re-emits a received ``null`` on (#1615).
    """
    return frozenset(
        name
        for name, field in model.model_fields.items()
        if not is_no_explicit_null(field) and _admits_none(field.annotation)
    )


def _is_required_key(model: type[BaseModel], key: str) -> bool:
    """Whether ``key`` (Python name or alias) names a required field, or no field at all."""
    name = _field_name_for_key(model, key)
    if name is None:
        return True  # an unknown key is left for pydantic (extras, or a refusal)
    return model.model_fields[name].is_required()


def _field_name_for_key(model: type[BaseModel], key: str) -> str | None:
    """The field ``key`` names, whether ``key`` is the Python name or the alias."""
    fields = model.model_fields
    if key in fields:
        return key
    for name, field in fields.items():
        if field.alias == key or field.validation_alias == key:
            return name
    return None


def _keep_opaque_nulls(
    value: dict[str, Any], extra: dict[str, Any], info: SerializationInfo
) -> dict[str, Any]:
    """Put back the ``null`` values ``exclude_none`` dropped from an opaque object.

    A model that declares no fields and allows extras is an opaque JSON object:
    ``core/context.json`` (``ContextObject``), which every response echoes
    unchanged, and ``core/ext.json``. ``exclude_none`` -- the
    :meth:`AdCPBaseModel.model_dump` default -- exists to leave *unset
    optional fields* off the wire, but it also dropped the object's own
    ``null`` values, so a buyer's ``{"a": null, "b": 1}`` came back as
    ``{"b": 1}``. An opaque object has no unset fields, so every key it holds
    is data. Keys the caller excluded by name stay excluded; nested values are
    plain JSON, which pydantic already dumps with their nulls.
    """
    include, exclude = info.include, info.exclude
    kept: dict[str, Any] = {}
    for key, item in extra.items():
        if key in value:
            kept[key] = value[key]
        elif (
            item is None
            and (include is None or key in include)
            and (exclude is None or key not in exclude)
        ):
            kept[key] = None
    return kept


def _put_back_null_extras(
    value: dict[str, Any], extra: dict[str, Any], info: SerializationInfo
) -> None:
    """Re-emit an unknown key whose value is ``null`` on an open model.

    ``exclude_none`` drops it with the ``None`` fields; an unknown key is a
    value the document carried, and the schema that admits it admits its
    ``null`` (#1614).
    """
    include, exclude = info.include, info.exclude
    for key, item in extra.items():
        if (
            item is None
            and key not in value
            and (include is None or key in include)
            and (exclude is None or key not in exclude)
        ):
            value[key] = None


class AdCPBaseModel(BaseModel):
    """Base model for AdCP types with spec-compliant serialization.

    Defaults to ``extra='allow'``: an unknown field from a newer spec
    version, or a sibling arm's property the schema admits, is kept in
    ``model_extra`` and written back on dump instead of being dropped
    (#1614). Generated types whose schemas close the object
    (``additionalProperties: false``) set ``extra='forbid'`` in their own
    ``model_config``; the rest follow the schema's reading of an absent
    ``additionalProperties``, which is open.

    Set ``ADCP_STRICT_VALIDATION=1`` in the environment (``"1"``,
    ``"true"``, ``"yes"``, ``"on"`` are accepted) to flip the default
    to ``extra='forbid'``. Use this in tests to catch a stray keyword
    argument or a renamed field. See :func:`_resolve_extra_policy`.

    .. important::
       The env var is resolved **once at module import time**. Set it
       in your shell or CI environment **before** ``import adcp`` runs
       — mutating ``os.environ["ADCP_STRICT_VALIDATION"]`` after the
       first ``adcp`` import has no effect on already-imported model
       classes (they captured the policy at class-body evaluation).

    Consumers who want per-model strict validation can override
    ``model_config`` on their subclass.

    Dumps write wire names: a field the generator aliased because its wire
    key is not a Python identifier (``field_schema`` for ``$schema``,
    ``pass_`` for ``pass``) is emitted under the wire key by ``model_dump()``
    and ``model_dump_json()`` with no ``by_alias`` argument. Pass
    ``by_alias=False`` for the Python names.
    """

    # ``defer_build=True`` skips building each model's pydantic-core
    # validator/serializer at class-definition time. With ~700 generated model
    # modules, eager builds dominate ``import adcp`` memory; deferring means each
    # model's core schema is built lazily on first validate/serialize, so only
    # the handful of models actually used are paid for.
    # ``serialize_by_alias``: a dump writes the wire name of an aliased field
    # (``$schema``, ``pass``, ``from``, ``media-buy``, ``_truncation``, ``2xl``)
    # without the caller passing ``by_alias=True``, so ``model_dump()`` is the
    # document and not a Python-named rendering of it (#1615). Construction
    # accepts either spelling (``validate_by_name`` alongside the default
    # ``validate_by_alias``).
    model_config = ConfigDict(
        extra=_EXTRA_POLICY,
        defer_build=True,
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=True,
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        # The deferred build's placeholders, made to build on instance access
        # as well (see ``_DeferredValSer``). A subclass that overrides this
        # hook must call ``super()``.
        _install_deferred_valsers(cls)

    # Pydantic derives serialization JSON Schema from the model fields only when
    # this serializer's return is unannotated. Even ``-> Any`` erases the shape.
    @model_serializer(mode="wrap")
    def _notification_config_wire_defaults(  # type: ignore[no-untyped-def]
        self, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ):
        """Retain the unrelated product default only for product subscriptions.

        Reporting capability defaults and tier validation live in their own
        generated models; this wrapper does not mask reporting attributes.

        Also keeps an opaque object's ``null`` values (see
        :func:`_keep_opaque_nulls`).
        """
        return self._finish_wire_dump(handler(self), info)

    def _finish_wire_dump(self, value: Any, info: SerializationInfo) -> Any:
        """The wire-fidelity pass every AdCP model serializer ends with.

        A subclass that declares its own wrap serializer replaces this
        class's, so it calls this on ``handler(self)`` itself
        (``canonical_creative._serialize_canonical_model``).
        """
        if not isinstance(value, dict):
            return value
        cls = type(self)
        extra = self.__pydantic_extra__
        if extra and info.exclude_none:
            if not cls.model_fields:
                return _keep_opaque_nulls(value, extra, info)
            _put_back_null_extras(value, extra, info)
        if not info.exclude_unset and not _EMIT_UNSET_DEFAULTS.get():
            self._drop_unset_defaults(value, info)
        if info.exclude_none:
            self._put_back_explicit_nulls(value, info)
        # Bundled schemas can rename or inline the same wire shape. Match its
        # declared fields rather than generated class/module names; supplied
        # values still come exclusively from this instance's fields-set.
        fields = cls.model_fields
        if {"subscriber_id", "url", "event_types", "product_payload_view"} <= fields.keys():
            events = getattr(self, "event_types", ())
            product_event = any(str(getattr(e, "value", e)).startswith("product.") for e in events)
            if "product_payload_view" not in self.model_fields_set:
                if not product_event:
                    value.pop("product_payload_view", None)
                elif "product_payload_view" not in value:
                    # A product subscription declares its payload view even when
                    # the caller left it at the default (the one schema default
                    # the SDK keeps emitting unset; see ``_drop_unset_defaults``).
                    view = getattr(self, "product_payload_view")
                    value["product_payload_view"] = (
                        getattr(view, "value", view) if info.mode == "json" else view
                    )
        return value

    # -- Explicit nulls (#1615) --------------------------------------------
    #
    # On the Python side ``None`` means "not supplied": ``Model(x=None)`` and
    # ``Model()`` are the same document. On the wire an explicit ``null`` on a
    # property whose schema admits it is a value, and a digest or echo over the
    # document includes it. The two meet in ``model_fields_set``: a field the
    # wire set to ``null`` is set, with value ``None``; an optional field a
    # Python caller passed as ``None`` is left out here, so the dump can emit ``null`` for
    # exactly the fields that were received as (or deliberately given as, via a
    # dict document) ``null``. ``model_validate``, ``model_validate_json`` and
    # nested validation never pass through ``__init__``, so what they set stays
    # set. A class that defines its own ``__init__`` makes pydantic call it
    # during validation as well, so it must not declare a null-admitting field
    # (``tests/test_explicit_nulls.py`` guards that).

    def __init__(self, /, **data: Any) -> None:
        # A class with its own ``__init__`` is also constructed through it by
        # validation, where a ``None`` is a document value; such a class keeps
        # pydantic's reading of a ``None`` keyword argument (and, by
        # ``tests/test_explicit_nulls.py``, has no null-admitting field).
        if None in data.values() and not type(self).__pydantic_custom_init__:
            # A ``None`` keyword argument for an optional field is the same as
            # leaving it out, so it is left out before validation runs: a
            # validator that asks ``model_fields_set`` whether a field is
            # present (a root required-group rule) gets the answer the wire
            # would. A required field keeps its ``None``: that is a value, and
            # ``null`` on the wire where the schema admits it.
            cls = type(self)
            data = {
                key: item
                for key, item in data.items()
                if item is not None or _is_required_key(cls, key)
            }
        super().__init__(**data)

    # Pydantic calls a model's ``__init__`` from inside validation only when it
    # is not the base one; this override has base semantics.
    cast(Any, __init__).__pydantic_base_init__ = True

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if value is None and name in type(self).model_fields:
            self.__pydantic_fields_set__.discard(name)

    def model_copy(self, *, update: Any = None, deep: bool = False) -> Self:
        copied = super().model_copy(update=update, deep=deep)
        if update and None in update.values():
            copied._unset_none_keys(update)
        return copied

    def _unset_none_keys(self, data: Any) -> None:
        cls = type(self)
        for key, item in data.items():
            if item is None:
                name = _field_name_for_key(cls, key)
                if name is not None:
                    self.__pydantic_fields_set__.discard(name)

    def _drop_unset_defaults(self, value: dict[str, Any], info: SerializationInfo) -> None:
        """Leave off a schema-optional default the document (or caller) did not set."""
        cls = type(self)
        marked = omit_unset_default_fields(cls)
        if not marked:
            return
        # pydantic reports ``None`` here when the call passed no ``by_alias``.
        by_alias: bool | None = info.by_alias
        if by_alias is None:
            by_alias = bool(cls.model_config.get("serialize_by_alias"))
        fields_set = self.__pydantic_fields_set__
        for name in marked:
            if name in fields_set:
                continue
            field = cls.model_fields[name]
            key = (field.serialization_alias or field.alias or name) if by_alias else name
            value.pop(key, None)

    def _put_back_explicit_nulls(self, value: dict[str, Any], info: SerializationInfo) -> None:
        """Re-emit ``null`` for a set, null-admitting field that ``exclude_none`` dropped."""
        cls = type(self)
        admitting = null_admitting_fields(cls)
        if not admitting:
            return
        include, exclude = info.include, info.exclude
        # pydantic reports ``None`` here when the call passed no ``by_alias``.
        by_alias: bool | None = info.by_alias
        if by_alias is None:
            by_alias = bool(cls.model_config.get("serialize_by_alias"))
        for name in self.__pydantic_fields_set__:
            if name not in admitting or self.__dict__.get(name) is not None:
                continue
            field = cls.model_fields[name]
            if field.exclude:
                continue
            key = (field.serialization_alias or field.alias or name) if by_alias else name
            if key in value:
                continue
            if include is not None and name not in include and key not in include:
                continue
            if exclude is not None and (name in exclude or key in exclude):
                continue
            value[key] = None

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        # ``serialize_as_any=True`` makes Pydantic dispatch on the runtime type of
        # nested values rather than the declared schema, so subclass
        # ``@model_serializer`` overrides fire from a base-typed parent field. Combined
        # with ``Field(exclude=True)`` on internal fields (which already works at every
        # nesting depth), this removes the parent-side ``model_dump`` boilerplate that
        # adopters previously needed to write per response type. See
        # docs/extending-types.md.
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        if "serialize_as_any" not in kwargs:
            kwargs["serialize_as_any"] = True
        token = _EMIT_UNSET_DEFAULTS.set(kwargs.get("exclude_unset", None) is False)
        try:
            return super().model_dump(**kwargs)
        except (TypeError, PydanticSerializationError) as exc:
            if "MockValSer" not in str(exc):
                raise
            _build_deferred_serializers(self, set())
            return super().model_dump(**kwargs)
        finally:
            _EMIT_UNSET_DEFAULTS.reset(token)

    def model_dump_as_received(self, **kwargs: Any) -> dict[str, Any]:
        """Deprecated: ``model_dump(mode="json")`` is the document now.

        In SDK 9 the default dump emitted every field, so a schema default the
        wire omitted came back, an admitted explicit ``null`` was dropped and
        an aliased key was written under its Python name; this method was the
        recipe that recovered the bytes (#1613, #1615). In SDK 10 the default
        dump leaves an unset schema default off, re-emits a received ``null``
        and writes wire names, so the two are the same document. This alias
        stays through 10.x and is removed in 11.
        """
        warnings.warn(
            "model_dump_as_received() is deprecated: model_dump(mode='json') now dumps the "
            "document as received. It will be removed in SDK 11.",
            DeprecationWarning,
            stacklevel=2,
        )
        kwargs.setdefault("mode", "json")
        return self.model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        if "serialize_as_any" not in kwargs:
            kwargs["serialize_as_any"] = True
        token = _EMIT_UNSET_DEFAULTS.set(kwargs.get("exclude_unset", None) is False)
        try:
            return super().model_dump_json(**kwargs)
        except (TypeError, PydanticSerializationError) as exc:
            if "MockValSer" not in str(exc):
                raise
            _build_deferred_serializers(self, set())
            return super().model_dump_json(**kwargs)
        finally:
            _EMIT_UNSET_DEFAULTS.reset(token)

    def model_summary(self) -> str:
        """Human-readable summary for protocol responses.

        Returns a standardized human-readable message suitable for MCP tool
        results, A2A task communications, and REST API responses.

        For types without a registered formatter, returns a generic message
        with the class name.
        """
        formatter = _RESPONSE_MESSAGE_REGISTRY.get(self.__class__.__name__)
        if formatter:
            return formatter(self)
        return f"{self.__class__.__name__} response"


_RootT = TypeVar("_RootT")


class AdCPRootModel(RootModel[_RootT], Generic[_RootT]):
    """The ``RootModel`` base for generated composing roots.

    A generated schema root that is a union of object models, or a wrapper
    another union discriminates against, keeps its ``RootModel``
    (``post_generate_fixes.unwrap_rootmodel_unions``). Pydantic's own
    ``RootModel.model_dump`` emits every ``None`` of the arm it holds, so the
    wrapper's dump differed from the arm's; this base gives it
    :class:`AdCPBaseModel`'s defaults (``exclude_none``, ``serialize_as_any``)
    so a root dumps the document its arm would (#1613).

    It also parametrizes under the SDK's deferred-build policy. ``class
    X(RootModel[A | B])`` builds the union's validator at the ``RootModel[...]``
    expression, before ``X`` or any config it declares exists; a composing root
    such as ``adagents.AdcpAgentsAuthorization`` walked most of its domain into
    pydantic-core at import that way (#1694). Parametrizing this class inherits
    ``defer_build=True`` into ``AdCPRootModel[A | B]`` and into ``X``, so the
    build waits for first use like every other generated model's.
    """

    model_config = ConfigDict(defer_build=True)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        _install_deferred_valsers(cls)

    def model_dump(self, **kwargs: Any) -> Any:
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        if "serialize_as_any" not in kwargs:
            kwargs["serialize_as_any"] = True
        token = _EMIT_UNSET_DEFAULTS.set(kwargs.get("exclude_unset", None) is False)
        try:
            return super().model_dump(**kwargs)
        except (TypeError, PydanticSerializationError) as exc:
            if "MockValSer" not in str(exc):
                raise
            _build_deferred_serializers(self, set())
            return super().model_dump(**kwargs)
        finally:
            _EMIT_UNSET_DEFAULTS.reset(token)

    def model_dump_json(self, **kwargs: Any) -> str:
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        if "serialize_as_any" not in kwargs:
            kwargs["serialize_as_any"] = True
        token = _EMIT_UNSET_DEFAULTS.set(kwargs.get("exclude_unset", None) is False)
        try:
            return super().model_dump_json(**kwargs)
        except (TypeError, PydanticSerializationError) as exc:
            if "MockValSer" not in str(exc):
                raise
            _build_deferred_serializers(self, set())
            return super().model_dump_json(**kwargs)
        finally:
            _EMIT_UNSET_DEFAULTS.reset(token)


class RegistryBaseModel(BaseModel):
    """Base model for registry API types.

    Uses ``extra='allow'`` so that new fields from the registry API
    are preserved rather than dropped. This differs from AdCPBaseModel
    which defaults to ``extra='ignore'`` for protocol types.
    """

    model_config = ConfigDict(extra="allow")


def _version_envelope() -> type[BaseModel]:
    """The generated ``AdcpVersionEnvelope`` class, imported on first use.

    Deferred because every generated module imports this one: a module-level import
    of the generated tree here would be a cycle.
    """
    from adcp.types.domains.core.version_envelope import AdcpVersionEnvelope

    return AdcpVersionEnvelope


def _protocol_envelope() -> type[BaseModel]:
    """The generated ``ProtocolEnvelope`` class, imported on first use (see above)."""
    from adcp.types.domains.core.protocol_envelope import ProtocolEnvelope

    return ProtocolEnvelope


class _AdcpMessage:
    """What an AdCP task request and an AdCP task response have in common.

    A MARKER: it declares no fields and is not a ``BaseModel``. A field here would land
    on every task message's wire type -- ``adcp_version`` would appear on
    ``ValidateInputRequest``, whose schema declares no version fields at all, inventing
    a spec field. So the version pins are read through accessors, which answer from the
    instance dict and return ``None`` for a tool that declares nothing.

    METHODS, not properties: pydantic does not let a model field shadow a property of
    the same name. The value is stored and ``model_dump`` shows it, but attribute access
    returns the property -- so every tool that declares a ``context`` would read ``None``,
    silently. The production precedent for the whole shape is the Prebid Sales Agent's
    ``BuyerRequest`` mixin (``src/core/schemas/_base.py``).

    The three field classifiers answer "which stratum does this field belong to" from
    the MRO rather than a per-class table: fields stay flat, exactly as on the wire, and
    provenance is which ancestor declared them. A class with no envelope ancestry
    answers the empty set -- degraded, but it cannot be wrong, and the answer changes by
    itself when the schema starts composing the envelope.
    """

    if TYPE_CHECKING:
        # DECLARED, not inherited. Every concrete AdCP message class is a pydantic model
        # by construction of the marker-insertion pass, so these members always exist --
        # and declaring them is what makes ``type[AdcpRequest]`` usable in a typed
        # registry without a cast. Inheriting ``BaseModel`` to say the same thing would
        # make the marker a model in its own right, which is exactly what it must not be.
        @classmethod
        def model_validate(cls, obj: Any, **kwargs: Any) -> Self: ...

        def model_dump(self, **kwargs: Any) -> dict[str, Any]: ...

        def model_dump_json(self, **kwargs: Any) -> str: ...

    @classmethod
    def _stratum_fields(cls, ancestor: type[BaseModel]) -> frozenset[str]:
        """The fields this class declares that ``ancestor`` also declares.

        Empty unless this class actually descends from ``ancestor``: a schema that
        inlines ``adcp_version`` rather than composing the envelope shares the name but
        not the stratum, and the intersection alone would read it as composed.
        """
        if not issubclass(cls, ancestor):
            return frozenset()
        own = cast("type[BaseModel]", cls)
        return frozenset(own.model_fields) & frozenset(ancestor.model_fields)

    @classmethod
    def version_fields(cls) -> frozenset[str]:
        """The version-envelope fields on this message (empty without the ancestry)."""
        return cls._stratum_fields(_version_envelope())

    @classmethod
    def protocol_fields(cls) -> frozenset[str]:
        """The protocol-envelope fields on this message (empty without the ancestry).

        A ``status`` an arm narrows to a ``Literal`` is still the envelope's ``status``,
        and a body-level ``context`` is still the envelope's ``context`` -- the spec says
        the envelope declaration is authoritative -- so matching by name is the tie-break,
        not an approximation of one. ``errors[]`` is a different field from ``adcp_error``
        by design and stays payload.
        """
        return cls._stratum_fields(_protocol_envelope())

    @classmethod
    def payload_fields(cls) -> frozenset[str]:
        """The tool's own fields: everything neither envelope declared.

        Excludes the version pins, which ARE payload on the wire ("this envelope is part
        of the payload itself"), because a caller composing a DataPart or an audit record
        wants tool arguments only. A caller who wants both unions the two sets.
        """
        own = cast("type[BaseModel]", cls)
        return frozenset(own.model_fields) - cls.version_fields() - cls.protocol_fields()


class AdcpRequest(_AdcpMessage):
    """The request message of a task in the pinned bundle's task registry.

    A consumer holding one can resolve its account, decide at-most-once, echo its
    context and negotiate version -- the whole transport-boundary job -- before knowing
    which tool it is. ``issubclass(model, AdcpRequest)`` is the registration-time proof
    that a model is spec-derived rather than a hand-written parallel: a field test passes
    for a forged model, descent does not.

    Each accessor returns the field's value, or ``None`` when this tool's schema declares
    no such field. Only 49 of the 87 request schemas declare an ``account`` and only 43 an
    ``idempotency_key``, so asking the request is what replaces
    ``getattr(req, "account", None)`` against ``Any`` at the boundary.
    """

    def get_account(
        self,
    ) -> AccountReference1 | AccountReference2 | CanonicalAccountReference | None:
        """The account this request names, or None when its schema declares none.

        The union is what the generated ``account`` fields actually hold, measured: 44
        request classes type it as the two ``core/account-ref.json`` arms directly (the
        ``AccountReference`` RootModel is unwrapped at the field by
        ``expose_account_reference_union_fields``, so naming the wrapper here would be a
        type no value ever has), and 8 as the ``CanonicalAccountReference`` root. The one
        inline declarer, ``compliance/comply-test-controller-request.json``, answers with
        its own generated ``Account`` through the same instance-dict read.
        """
        return self.__dict__.get("account")

    def get_idempotency_key(self) -> str | None:
        """The at-most-once key this request carries, or None when its schema declares none.

        Presence is the honest structural signal for write-versus-read: a read is
        idempotent by construction and takes no key. ``x-mutates-state`` is not a
        substitute -- it covers 44 of 87 schemas and contradicts the key in both
        directions on 2 schemas each way.
        """
        return self.__dict__.get("idempotency_key")

    def get_context(self) -> ContextObject | None:
        """The buyer's opaque ``context``, echoed unchanged onto whatever leaves."""
        return self.__dict__.get("context")

    def get_push_notification_config(self) -> PushNotificationConfig | None:
        """The webhook configuration this request asks for, or None (19 of 87 declare it)."""
        return self.__dict__.get("push_notification_config")

    def get_adcp_version(self) -> str | None:
        """The release this buyer pins, or None when it pinned none.

        Answers from the instance dict, so it still answers for a schema that inlines the
        field without composing the envelope -- the one case ``version_fields()`` reports
        empty.
        """
        return self.__dict__.get("adcp_version")

    def get_adcp_major_version(self) -> int | None:
        """The major this buyer pins (deprecated through 3.x), or None."""
        return self.__dict__.get("adcp_major_version")


class AdcpResponse(_AdcpMessage):
    """The response message of a task in the pinned bundle's task registry.

    A consumer holding one can route on task state, pick up an async ``task_id``, split
    envelope from payload and log uniformly, before knowing which tool answered. Which
    makes one generic poll-to-terminal loop possible for all 77 tasks, where today each
    arm has no common type at all.
    """

    def get_status(self) -> TaskStatus | None:
        """The AdCP task state the seller asserted, or None.

        None only for the 10 task responses whose schemas do not compose
        ``core/protocol-envelope.json``, against the envelope's own "REQUIRED on every
        task response envelope" -- a residue this SDK cannot invent its way out of.
        """
        return self.__dict__.get("status")

    def get_task_id(self) -> str | None:
        """The async operation identifier, present when the task needs polling."""
        return self.__dict__.get("task_id")

    def get_context(self) -> ContextObject | None:
        """The caller's ``context``, echoed back byte-for-byte."""
        return self.__dict__.get("context")

    def get_adcp_error(self) -> Error | None:
        """The envelope-level typed error for a fatal task failure, or None.

        The payload's ``errors[]`` array is a different field by design and is read from
        the concrete arm; the two MUST be treated as distinct by name.
        """
        return self.__dict__.get("adcp_error")

    def get_message(self) -> str | None:
        """The human-readable summary of the result, or None."""
        return self.__dict__.get("message")

    def get_replayed(self) -> bool | None:
        """True when this answer came from the idempotency cache.

        ``False`` when the response declares the field and was executed fresh, ``None``
        when the tool's schema does not declare it -- absence stays distinguishable from
        a negative answer.
        """
        return self.__dict__.get("replayed")
