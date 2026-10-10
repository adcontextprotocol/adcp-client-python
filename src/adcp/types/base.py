from __future__ import annotations

"""Base model for structural AdCP application types and wire serialization.

Generated Pydantic models provide typed construction, common field
constraints, and spec-shaped serialization. Complete protocol conformance is
defined by the bundled canonical JSON Schemas; validate at wire boundaries
with :mod:`adcp.validation`. In particular, code generation cannot represent
every JSON Schema conditional or ``x-adcp-validation`` behavioral rule.
"""

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from pydantic import (
    AfterValidator,
    AnyUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    SerializerFunctionWrapHandler,
    StrictInt,
    StrictStr,
    TypeAdapter,
    WithJsonSchema,
    model_serializer,
)
from pydantic_core import PydanticSerializationError

from adcp._deferred_adapters import deferred_adapter
from adcp.types._geo_place_keys import GeoPlaceSystemKey as GeoPlaceSystemKey
from adcp.types._scalar import as_json_schema_integer

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


def _resolve_extra_policy() -> Literal["ignore", "forbid"]:
    """Choose the ``extra`` policy for :class:`AdCPBaseModel`.

    ``ignore`` (default) silently drops unknown fields, preserving
    forward compatibility when newer spec versions add fields. This is
    the production-safe default — a client on spec N sending to a
    server on spec N+1 keeps working.

    ``forbid`` raises on unknown fields so CI catches the silent-drop
    case that production-safe defaults obscure. Most useful during
    upgrades: right after a major spec revision, run tests with
    ``ADCP_STRICT_VALIDATION=1`` to surface every place the upgrade
    dropped a renamed field, then ship with the flag unset for the
    forward-compat default.

    Values accepted: ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (any
    case). Anything else — including empty string and ``"0"`` — keeps
    the ``ignore`` default.
    """
    raw = os.environ.get("ADCP_STRICT_VALIDATION", "").strip().lower()
    return "forbid" if raw in {"1", "true", "yes", "on"} else "ignore"


_EXTRA_POLICY: Literal["ignore", "forbid"] = _resolve_extra_policy()


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
#: points the format-reference ``agent_url`` here.
WireUrl = Annotated[
    StrictStr,
    AfterValidator(_require_absolute_url),
    WithJsonSchema({"type": "string", "format": "uri"}),
]


class AdCPBaseModel(BaseModel):
    """Base model for AdCP types with spec-compliant serialization.

    Defaults to ``extra='ignore'`` so unknown fields from newer spec
    versions are silently dropped rather than causing validation
    errors. Generated types whose schemas set
    ``additionalProperties: true`` override this with ``extra='allow'``
    in their own ``model_config``.

    Set ``ADCP_STRICT_VALIDATION=1`` in the environment (``"1"``,
    ``"true"``, ``"yes"``, ``"on"`` are accepted) to flip the default
    to ``extra='forbid'``. Use this during spec upgrades to catch
    silently-dropped renamed fields in tests. See :func:`_resolve_extra_policy`.

    .. important::
       The env var is resolved **once at module import time**. Set it
       in your shell or CI environment **before** ``import adcp`` runs
       — mutating ``os.environ["ADCP_STRICT_VALIDATION"]`` after the
       first ``adcp`` import has no effect on already-imported model
       classes (they captured the policy at class-body evaluation).

    Consumers who want per-model strict validation can override
    ``model_config`` on their subclass.
    """

    # ``defer_build=True`` skips building each model's pydantic-core
    # validator/serializer at class-definition time. With ~700 generated model
    # modules, eager builds dominate ``import adcp`` memory; deferring means each
    # model's core schema is built lazily on first validate/serialize, so only
    # the handful of models actually used are paid for.
    model_config = ConfigDict(extra=_EXTRA_POLICY, defer_build=True)

    # Pydantic derives serialization JSON Schema from the model fields only when
    # this serializer's return is unannotated. Even ``-> Any`` erases the shape.
    @model_serializer(mode="wrap")
    def _notification_config_wire_defaults(  # type: ignore[no-untyped-def]
        self, handler: SerializerFunctionWrapHandler
    ):
        """Retain the unrelated product default only for product subscriptions.

        Reporting capability defaults and tier validation live in their own
        generated models; this wrapper does not mask reporting attributes.
        """
        value = handler(self)
        if not isinstance(value, dict):
            return value
        # Bundled schemas can rename or inline the same wire shape. Match its
        # declared fields rather than generated class/module names; supplied
        # values still come exclusively from this instance's fields-set.
        fields = type(self).model_fields
        if {"subscriber_id", "url", "event_types", "product_payload_view"} <= fields.keys():
            events = getattr(self, "event_types", ())
            if "product_payload_view" not in self.model_fields_set and not any(
                str(getattr(e, "value", e)).startswith("product.") for e in events
            ):
                value.pop("product_payload_view", None)
        return value

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
        try:
            return super().model_dump(**kwargs)
        except (TypeError, PydanticSerializationError) as exc:
            if "MockValSer" not in str(exc):
                raise
            _build_deferred_serializers(self, set())
            return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        if "serialize_as_any" not in kwargs:
            kwargs["serialize_as_any"] = True
        try:
            return super().model_dump_json(**kwargs)
        except (TypeError, PydanticSerializationError) as exc:
            if "MockValSer" not in str(exc):
                raise
            _build_deferred_serializers(self, set())
            return super().model_dump_json(**kwargs)

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
