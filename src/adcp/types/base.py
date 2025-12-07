from __future__ import annotations

"""Base model for AdCP types with spec-compliant serialization."""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

# Type alias to shorten long type annotations
MessageFormatter = Callable[[Any], str]


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


@_register_response_message("ListAuthorizedPropertiesResponse")
def _list_authorized_properties_message(self: Any) -> str:
    domains = getattr(self, "publisher_domains", None)
    if domains is None:
        return "No authorized properties found."
    count = len(domains)
    return f"Authorized to represent {count} publisher {_pluralize(count, 'domain')}."


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

    AdCP JSON schemas use additionalProperties: false and do not allow null
    for optional fields. Therefore, optional fields must be omitted entirely
    when not present (not sent as null).
    """

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        return super().model_dump_json(**kwargs)

    def summary(self) -> str:
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
