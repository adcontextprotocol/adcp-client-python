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

    model_config = ConfigDict(extra=_EXTRA_POLICY)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        # NOTE: Pydantic v2 uses a Rust-backed serializer that does NOT call Python-level
        # model_dump() overrides on nested child instances. If a child class overrides
        # model_dump() for custom serialization logic, that override will not fire when
        # the child is serialized as part of a parent model_dump() call. Use
        # Field(exclude=True) for field-level exclusion (works at all nesting depths) or
        # @model_serializer for custom output logic. See docs/extending-types.md.
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
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
