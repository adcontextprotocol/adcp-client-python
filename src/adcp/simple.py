"""Simplified API accessor for ADCPClient.

Provides an ergonomic API with:
- Kwargs instead of request objects
- Direct return values (no TaskResult unwrapping)
- Raises exceptions on errors

Usage:
    client = ADCPClient(config)

    # Standard API: full control
    result = await client.get_products(GetProductsRequest(brief="Coffee", buying_mode="brief"))
    if result.success:
        print(result.data.products)

    # Simple API: ergonomic
    products = await client.simple.get_products(brief="Coffee", buying_mode="brief")
    print(products.products)
"""

from __future__ import annotations

from types import UnionType
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from adcp.exceptions import ADCPSimpleAPIError
from adcp.types import (
    ActivateSignalRequest,
    ActivateSignalResponse,
    BuildCreativeRequest,
    BuildCreativeResponse,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    GetCreativeDeliveryRequest,
    GetCreativeDeliveryResponse,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsDiscoveryRequest,
    GetSignalsLookupRequest,
    GetSignalsResponse,
    ListAccountsRequest,
    ListAccountsResponse,
    ListCreativesRequest,
    ListCreativesResponse,
    LogEventRequest,
    LogEventResponse,
    PreviewCreativeRequest,
    PreviewCreativeResponse,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    SyncAccountsRequest,
    SyncAccountsResponse,
    SyncCreativesRequest,
    SyncCreativesResponse,
    SyncEventSourcesRequest,
    SyncEventSourcesResponse,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
)
from adcp.types.legacy import (
    LegacyListCreativeFormatsRequest as ListCreativeFormatsRequest,
)
from adcp.types.legacy import (
    LegacyListCreativeFormatsResponse as ListCreativeFormatsResponse,
)

if TYPE_CHECKING:
    from adcp.client import ADCPClient


_adapter_cache: dict[type | UnionType, TypeAdapter[Any]] = {}


def _get_adapter(tp: type | UnionType) -> TypeAdapter[Any]:
    """Get or create a cached TypeAdapter for the given type.

    Uses a plain dict rather than lru_cache because UnionType is not
    hashable in all Python versions for lru_cache, but works as a dict key.
    """
    try:
        return _adapter_cache[tp]
    except KeyError:
        adapter: TypeAdapter[Any] = TypeAdapter(tp)
        _adapter_cache[tp] = adapter
        return adapter


def _make_request(request_type: type | UnionType, kwargs: dict[str, Any]) -> Any:
    """Create a request instance, handling both classes and Union type aliases."""
    if isinstance(request_type, UnionType):
        return _get_adapter(request_type).validate_python(kwargs)
    return request_type(**kwargs)


class SimpleAPI:
    """Simplified API accessor for ergonomic usage.

    Provides kwargs-based methods that return unwrapped response data
    and raise exceptions on errors.

    This is intended for:
    - Quick prototyping and testing
    - Documentation and examples
    - Simple scripts and notebooks

    For production code with complex error handling, use the standard
    client API which returns TaskResult wrappers.
    """

    def __init__(self, client: ADCPClient):
        """Initialize simple API accessor.

        Args:
            client: The ADCPClient instance to wrap
        """
        self._client = client

    async def get_products(
        self,
        **kwargs: Any,
    ) -> GetProductsResponse:
        """Get advertising products (simplified).

        This is a convenience wrapper around client.get_products() that:
        - Accepts kwargs instead of GetProductsRequest
        - Returns unwrapped GetProductsResponse
        - Raises ADCPSimpleAPIError on failures

        For full control over error handling, use client.get_products() instead.

        Args:
            **kwargs: Arguments for GetProductsRequest (brief, brand, etc.)

        Returns:
            GetProductsResponse directly (no TaskResult wrapper)

        Raises:
            ADCPSimpleAPIError: If request fails. Use standard API for detailed error handling.

        Example:
            products = await client.simple.get_products(
                brief='Coffee subscription service',
                buying_mode='brief',
            )
            print(f"Found {len(products.products)} products")
        """
        request = _make_request(GetProductsRequest, kwargs)
        result = await self._client.get_products(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="get_products",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def list_creative_formats_legacy(
        self,
        **kwargs: Any,
    ) -> ListCreativeFormatsResponse:
        """List supported creative formats.

        Args:
            **kwargs: Arguments passed to ListCreativeFormatsRequest

        Returns:
            ListCreativeFormatsResponse with formats list

        Raises:
            Exception: If the request fails

        Example:
            formats = await client.simple.list_creative_formats_legacy()
            print(f"Found {len(formats.formats)} formats")
        """
        request = _make_request(ListCreativeFormatsRequest, kwargs)
        result = await self._client.list_creative_formats_legacy(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="list_creative_formats",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def preview_creative(
        self,
        **kwargs: Any,
    ) -> PreviewCreativeResponse:
        """Preview creative manifest.

        Args:
            **kwargs: Arguments passed to PreviewCreativeRequest

        Returns:
            PreviewCreativeResponse with preview data

        Raises:
            Exception: If the request fails

        Example:
            preview = await client.simple.preview_creative(
                manifest={'format_id': 'banner_300x250', 'assets': {...}}
            )
            print(f"Preview: {preview.previews[0]}")
        """
        request = _make_request(PreviewCreativeRequest, kwargs)
        result = await self._client.preview_creative(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="preview_creative",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def sync_creatives(
        self,
        **kwargs: Any,
    ) -> SyncCreativesResponse:
        """Sync creatives.

        Args:
            **kwargs: Arguments passed to SyncCreativesRequest

        Returns:
            SyncCreativesResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(SyncCreativesRequest, kwargs)
        result = await self._client.sync_creatives(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="sync_creatives",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def list_creatives(
        self,
        **kwargs: Any,
    ) -> ListCreativesResponse:
        """List creatives.

        Args:
            **kwargs: Arguments passed to ListCreativesRequest

        Returns:
            ListCreativesResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(ListCreativesRequest, kwargs)
        result = await self._client.list_creatives(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="list_creatives",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def get_media_buy_delivery(
        self,
        **kwargs: Any,
    ) -> GetMediaBuyDeliveryResponse:
        """Get media buy delivery.

        Args:
            **kwargs: Arguments passed to GetMediaBuyDeliveryRequest

        Returns:
            GetMediaBuyDeliveryResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(GetMediaBuyDeliveryRequest, kwargs)
        result = await self._client.get_media_buy_delivery(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="get_media_buy_delivery",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def get_signals(
        self,
        **kwargs: Any,
    ) -> GetSignalsResponse:
        """Get signals.

        Args:
            **kwargs: Arguments passed to GetSignalsRequest

        Returns:
            GetSignalsResponse

        Raises:
            Exception: If the request fails
        """
        request: GetSignalsDiscoveryRequest | GetSignalsLookupRequest
        if "signal_ids" in kwargs:
            request = GetSignalsLookupRequest.model_validate(kwargs)
        else:
            request = GetSignalsDiscoveryRequest.model_validate(kwargs)
        result = await self._client.get_signals(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="get_signals",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def activate_signal(
        self,
        **kwargs: Any,
    ) -> ActivateSignalResponse:
        """Activate signal.

        Args:
            **kwargs: Arguments passed to ActivateSignalRequest

        Returns:
            ActivateSignalResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(ActivateSignalRequest, kwargs)
        result = await self._client.activate_signal(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="activate_signal",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def provide_performance_feedback(
        self,
        **kwargs: Any,
    ) -> ProvidePerformanceFeedbackResponse:
        """Provide performance feedback.

        Args:
            **kwargs: Arguments passed to ProvidePerformanceFeedbackRequest

        Returns:
            ProvidePerformanceFeedbackResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(ProvidePerformanceFeedbackRequest, kwargs)
        result = await self._client.provide_performance_feedback(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="provide_performance_feedback",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def create_media_buy(
        self,
        **kwargs: Any,
    ) -> CreateMediaBuyResponse:
        """Create media buy.

        Args:
            **kwargs: Arguments passed to CreateMediaBuyRequest

        Returns:
            CreateMediaBuyResponse

        Raises:
            Exception: If the request fails

        Example:
            media_buy = await client.simple.create_media_buy(
                brand=brand,
                packages=[package_request],
                publisher_properties=properties
            )
            print(f"Created media buy: {media_buy.media_buy_id}")
        """
        request = _make_request(CreateMediaBuyRequest, kwargs)
        result = await self._client.create_media_buy(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="create_media_buy",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def update_media_buy(
        self,
        **kwargs: Any,
    ) -> UpdateMediaBuyResponse:
        """Update media buy.

        Args:
            **kwargs: Arguments passed to UpdateMediaBuyRequest

        Returns:
            UpdateMediaBuyResponse

        Raises:
            Exception: If the request fails

        Example:
            updated = await client.simple.update_media_buy(
                media_buy_id="mb_123",
                packages=[updated_package]
            )
            print(f"Updated media buy: {updated.media_buy_id}")
        """
        request = _make_request(UpdateMediaBuyRequest, kwargs)
        result = await self._client.update_media_buy(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="update_media_buy",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def build_creative(
        self,
        **kwargs: Any,
    ) -> BuildCreativeResponse:
        """Build creative.

        Args:
            **kwargs: Arguments passed to BuildCreativeRequest

        Returns:
            BuildCreativeResponse

        Raises:
            Exception: If the request fails

        Example:
            creative = await client.simple.build_creative(
                manifest=creative_manifest,
                target_format_id="vast_2.0",
                inputs={"duration": 30}
            )
            print(f"Built creative: {creative.assets[0].url}")
        """
        request = _make_request(BuildCreativeRequest, kwargs)
        result = await self._client.build_creative(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="build_creative",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def list_accounts(
        self,
        **kwargs: Any,
    ) -> ListAccountsResponse:
        """List accounts.

        Args:
            **kwargs: Arguments passed to ListAccountsRequest

        Returns:
            ListAccountsResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(ListAccountsRequest, kwargs)
        result = await self._client.list_accounts(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="list_accounts",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def sync_accounts(
        self,
        **kwargs: Any,
    ) -> SyncAccountsResponse:
        """Sync accounts.

        Args:
            **kwargs: Arguments passed to SyncAccountsRequest

        Returns:
            SyncAccountsResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(SyncAccountsRequest, kwargs)
        result = await self._client.sync_accounts(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="sync_accounts",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def log_event(
        self,
        **kwargs: Any,
    ) -> LogEventResponse:
        """Log event.

        Args:
            **kwargs: Arguments passed to LogEventRequest

        Returns:
            LogEventResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(LogEventRequest, kwargs)
        result = await self._client.log_event(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="log_event",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def sync_event_sources(
        self,
        **kwargs: Any,
    ) -> SyncEventSourcesResponse:
        """Sync event sources.

        Args:
            **kwargs: Arguments passed to SyncEventSourcesRequest

        Returns:
            SyncEventSourcesResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(SyncEventSourcesRequest, kwargs)
        result = await self._client.sync_event_sources(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="sync_event_sources",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def get_creative_delivery(
        self,
        **kwargs: Any,
    ) -> GetCreativeDeliveryResponse:
        """Get creative delivery.

        Args:
            **kwargs: Arguments passed to GetCreativeDeliveryRequest

        Returns:
            GetCreativeDeliveryResponse

        Raises:
            Exception: If the request fails
        """
        request = _make_request(GetCreativeDeliveryRequest, kwargs)
        result = await self._client.get_creative_delivery(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="get_creative_delivery",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data
