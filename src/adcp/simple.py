"""Simplified API accessor for ADCPClient.

Provides an ergonomic API with:
- Kwargs instead of request objects
- Direct return values (no TaskResult unwrapping)
- Raises exceptions on errors

Usage:
    client = ADCPClient(config)

    # Standard API: full control
    result = await client.get_products(GetProductsRequest(brief="Coffee"))
    if result.success:
        print(result.data.products)

    # Simple API: ergonomic
    products = await client.simple.get_products(brief="Coffee")
    print(products.products)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adcp.exceptions import ADCPSimpleAPIError
from adcp.types.stable import (
    ActivateSignalRequest,
    ActivateSignalResponse,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
    ListAuthorizedPropertiesRequest,
    ListAuthorizedPropertiesResponse,
    ListCreativeFormatsRequest,
    ListCreativeFormatsResponse,
    ListCreativesRequest,
    ListCreativesResponse,
    PreviewCreativeRequest,
    PreviewCreativeResponse,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    SyncCreativesRequest,
    SyncCreativesResponse,
)

if TYPE_CHECKING:
    from adcp.client import ADCPClient


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
            **kwargs: Arguments for GetProductsRequest (brief, brand_manifest, etc.)

        Returns:
            GetProductsResponse directly (no TaskResult wrapper)

        Raises:
            ADCPSimpleAPIError: If request fails. Use standard API for detailed error handling.

        Example:
            products = await client.simple.get_products(
                brief='Coffee subscription service'
            )
            print(f"Found {len(products.products)} products")
        """
        request = GetProductsRequest(**kwargs)
        result = await self._client.get_products(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="get_products",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def list_creative_formats(
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
            formats = await client.simple.list_creative_formats()
            print(f"Found {len(formats.formats)} formats")
        """
        request = ListCreativeFormatsRequest(**kwargs)
        result = await self._client.list_creative_formats(request)
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
        request = PreviewCreativeRequest(**kwargs)
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
        request = SyncCreativesRequest(**kwargs)
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
        request = ListCreativesRequest(**kwargs)
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
        request = GetMediaBuyDeliveryRequest(**kwargs)
        result = await self._client.get_media_buy_delivery(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="get_media_buy_delivery",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data

    async def list_authorized_properties(
        self,
        **kwargs: Any,
    ) -> ListAuthorizedPropertiesResponse:
        """List authorized properties.

        Args:
            **kwargs: Arguments passed to ListAuthorizedPropertiesRequest

        Returns:
            ListAuthorizedPropertiesResponse

        Raises:
            Exception: If the request fails
        """
        request = ListAuthorizedPropertiesRequest(**kwargs)
        result = await self._client.list_authorized_properties(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="list_authorized_properties",
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
        request = GetSignalsRequest(**kwargs)
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
        request = ActivateSignalRequest(**kwargs)
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
        request = ProvidePerformanceFeedbackRequest(**kwargs)
        result = await self._client.provide_performance_feedback(request)
        if not result.success or not result.data:
            raise ADCPSimpleAPIError(
                operation="provide_performance_feedback",
                error_message=result.error,
                agent_id=self._client.agent_config.id,
            )
        return result.data
