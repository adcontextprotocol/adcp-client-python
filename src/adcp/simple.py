from __future__ import annotations

"""
Simple convenience API that matches JavaScript SDK ergonomics.

Provides:
- Keyword arguments instead of request objects
- Direct data access (result.products vs result.data.products)
- Async/await pattern
- Automatic error handling
"""

from typing import TYPE_CHECKING, Any

from adcp.types.core import TaskResult
from adcp.types.generated import (
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
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    SyncCreativesRequest,
    SyncCreativesResponse,
)

if TYPE_CHECKING:
    from adcp.client import ADCPClient


class ADCPSimpleError(Exception):
    """Error from simple API call."""

    pass


class SimpleAPI:
    """
    Simple API wrapper providing JavaScript-like ergonomics.

    Example:
        result = await client.simple.get_products(
            brief='Coffee brands targeting millennials',
            brand_manifest={'name': 'My Brand', 'url': 'https://...'}
        )
        print(f"Found {len(result.products)} products")
    """

    def __init__(self, client: ADCPClient):
        """Initialize simple API wrapper."""
        self._client = client

    async def get_products(self, **kwargs: Any) -> GetProductsResponse:
        """
        Get advertising products.

        Args:
            **kwargs: Keyword arguments matching GetProductsRequest fields
                - brief: str (optional) - Brief description of advertising needs
                - brand_manifest: dict (optional) - Brand information
                - brand_manifest_ref: dict (optional) - Reference to brand manifest
                - format_ids: list[str] (optional) - Preferred format IDs
                - publisher_properties: list[dict] (optional) - Target properties

        Returns:
            GetProductsResponse with direct access to .products

        Raises:
            ADCPSimpleError: If the request fails
        """
        request = GetProductsRequest(**kwargs)
        result: TaskResult[GetProductsResponse] = await self._client.get_products(request)
        return self._unwrap(result)

    async def list_creative_formats(self, **kwargs: Any) -> ListCreativeFormatsResponse:
        """
        List supported creative formats.

        Args:
            **kwargs: Keyword arguments matching ListCreativeFormatsRequest fields
                - brand_manifest: dict (optional) - Brand information for format filtering

        Returns:
            ListCreativeFormatsResponse with direct access to .formats

        Raises:
            ADCPSimpleError: If the request fails
        """
        request = ListCreativeFormatsRequest(**kwargs)
        result: TaskResult[ListCreativeFormatsResponse] = await self._client.list_creative_formats(
            request
        )
        return self._unwrap(result)

    async def sync_creatives(self, **kwargs: Any) -> SyncCreativesResponse:
        """
        Sync creatives to publisher's creative library.

        Args:
            **kwargs: Keyword arguments matching SyncCreativesRequest fields
                - media_buy_id: str - Media buy identifier
                - creatives: list[dict] - Creative assets to sync

        Returns:
            SyncCreativesResponse with direct access to sync results

        Raises:
            ADCPSimpleError: If the request fails
        """
        request = SyncCreativesRequest(**kwargs)
        result: TaskResult[SyncCreativesResponse] = await self._client.sync_creatives(request)
        return self._unwrap(result)

    async def list_creatives(self, **kwargs: Any) -> ListCreativesResponse:
        """
        List creatives in publisher's library.

        Args:
            **kwargs: Keyword arguments matching ListCreativesRequest fields
                - media_buy_id: str - Media buy identifier
                - tags: list[str] (optional) - Filter by tags
                - format_ids: list[str] (optional) - Filter by format IDs

        Returns:
            ListCreativesResponse with direct access to .creatives

        Raises:
            ADCPSimpleError: If the request fails
        """
        request = ListCreativesRequest(**kwargs)
        result: TaskResult[ListCreativesResponse] = await self._client.list_creatives(request)
        return self._unwrap(result)

    async def get_media_buy_delivery(self, **kwargs: Any) -> GetMediaBuyDeliveryResponse:
        """
        Get delivery metrics for a media buy.

        Args:
            **kwargs: Keyword arguments matching GetMediaBuyDeliveryRequest fields
                - media_buy_id: str - Media buy identifier
                - package_ids: list[str] (optional) - Filter by package IDs
                - start_date: str (optional) - Start date for metrics
                - end_date: str (optional) - End date for metrics

        Returns:
            GetMediaBuyDeliveryResponse with direct access to delivery metrics

        Raises:
            ADCPSimpleError: If the request fails
        """
        request = GetMediaBuyDeliveryRequest(**kwargs)
        result: TaskResult[
            GetMediaBuyDeliveryResponse
        ] = await self._client.get_media_buy_delivery(request)
        return self._unwrap(result)

    async def list_authorized_properties(self, **kwargs: Any) -> ListAuthorizedPropertiesResponse:
        """
        List authorized publisher properties.

        Args:
            **kwargs: Keyword arguments matching ListAuthorizedPropertiesRequest fields
                - channels: list[str] (optional) - Filter by channels

        Returns:
            ListAuthorizedPropertiesResponse with direct access to .properties

        Raises:
            ADCPSimpleError: If the request fails
        """
        request = ListAuthorizedPropertiesRequest(**kwargs)
        result: TaskResult[
            ListAuthorizedPropertiesResponse
        ] = await self._client.list_authorized_properties(request)
        return self._unwrap(result)

    async def get_signals(self, **kwargs: Any) -> GetSignalsResponse:
        """
        Get available first-party signals.

        Args:
            **kwargs: Keyword arguments matching GetSignalsRequest fields

        Returns:
            GetSignalsResponse with direct access to .signals

        Raises:
            ADCPSimpleError: If the request fails
        """
        request = GetSignalsRequest(**kwargs)
        result: TaskResult[GetSignalsResponse] = await self._client.get_signals(request)
        return self._unwrap(result)

    async def activate_signal(self, **kwargs: Any) -> ActivateSignalResponse:
        """
        Activate a first-party signal for targeting.

        Args:
            **kwargs: Keyword arguments matching ActivateSignalRequest fields
                - signal_id: str - Signal identifier to activate
                - package_id: str (optional) - Package to activate signal for

        Returns:
            ActivateSignalResponse with activation confirmation

        Raises:
            ADCPSimpleError: If the request fails
        """
        request = ActivateSignalRequest(**kwargs)
        result: TaskResult[ActivateSignalResponse] = await self._client.activate_signal(request)
        return self._unwrap(result)

    async def provide_performance_feedback(
        self, **kwargs: Any
    ) -> ProvidePerformanceFeedbackResponse:
        """
        Provide performance feedback to publisher.

        Args:
            **kwargs: Keyword arguments matching ProvidePerformanceFeedbackRequest fields
                - media_buy_id: str - Media buy identifier
                - package_id: str (optional) - Package identifier
                - feedback: dict - Performance feedback data

        Returns:
            ProvidePerformanceFeedbackResponse with feedback confirmation

        Raises:
            ADCPSimpleError: If the request fails
        """
        request = ProvidePerformanceFeedbackRequest(**kwargs)
        result: TaskResult[
            ProvidePerformanceFeedbackResponse
        ] = await self._client.provide_performance_feedback(request)
        return self._unwrap(result)

    def _unwrap(self, result: TaskResult[Any]) -> Any:
        """
        Unwrap TaskResult and return data directly.

        For completed tasks, returns the data.
        For async tasks (submitted), raises error with webhook info.
        For failed tasks, raises error with details.

        Args:
            result: TaskResult to unwrap

        Returns:
            The response data directly

        Raises:
            ADCPSimpleError: If task failed or is async
        """
        if result.status.value == "completed" and result.data is not None:
            return result.data

        if result.status.value == "submitted":
            raise ADCPSimpleError(
                f"Task submitted for async processing. "
                f"Webhook: {result.submitted.webhook_url if result.submitted else 'unknown'}"
            )

        if result.status.value == "needs_input":
            raise ADCPSimpleError(
                f"Agent needs input: {result.needs_input.message if result.needs_input else 'unknown'}"
            )

        if result.status.value == "failed":
            raise ADCPSimpleError(f"Task failed: {result.error or 'unknown error'}")

        raise ADCPSimpleError(f"Unexpected task status: {result.status.value}")
