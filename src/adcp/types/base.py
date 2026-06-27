from __future__ import annotations

"""Base model for AdCP types with spec-compliant serialization."""

import os
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

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
        except TypeError as exc:
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
        except TypeError as exc:
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
