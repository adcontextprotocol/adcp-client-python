"""Content Standards protocol handler.

Provides a base class for implementing Content Standards agents.
Non-Content-Standards operations return 'not supported' via the base class.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Generic

from pydantic import ValidationError

from adcp.server.base import ADCPHandler, NotImplementedResponse, TContext, invalid_request
from adcp.types import (
    CalibrateContentRequest,
    CalibrateContentResponse,
    CreateContentStandardsRequest,
    CreateContentStandardsResponse,
    GetContentStandardsRequest,
    GetContentStandardsResponse,
    GetMediaBuyArtifactsRequest,
    GetMediaBuyArtifactsResponse,
    ListContentStandardsRequest,
    ListContentStandardsResponse,
    UpdateContentStandardsRequest,
    UpdateContentStandardsResponse,
    ValidateContentDeliveryRequest,
    ValidateContentDeliveryResponse,
)


class ContentStandardsHandler(ADCPHandler[TContext], Generic[TContext]):
    """Handler for Content Standards protocol.

    Subclass this to implement a Content Standards agent. All Content Standards
    operations must be implemented via the handle_* methods.
    The public methods (create_content_standards, etc.) handle validation and
    error handling automatically.

    Non-Content-Standards operations (get_products, create_media_buy, etc.)
    return 'not supported' via the base class.

    Example:
        class MyContentStandardsHandler(ContentStandardsHandler):
            async def handle_create_content_standards(
                self,
                request: CreateContentStandardsRequest,
                context: TContext | None = None
            ) -> CreateContentStandardsResponse:
                # Your implementation
                return CreateContentStandardsResponse(...)
    """

    _agent_type: str = "Content Standards agents"

    # ========================================================================
    # Content Standards Operations - Override base class with validation
    # ========================================================================

    async def create_content_standards(
        self,
        params: CreateContentStandardsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> CreateContentStandardsResponse | NotImplementedResponse:
        """Create content standards configuration.

        Validates params and delegates to handle_create_content_standards.
        """
        try:
            request = CreateContentStandardsRequest.model_validate(params)
        except ValidationError as e:
            return invalid_request(e, payload=params)
        return await self.handle_create_content_standards(request, context)

    async def get_content_standards(
        self,
        params: GetContentStandardsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> GetContentStandardsResponse | NotImplementedResponse:
        """Get content standards configuration.

        Validates params and delegates to handle_get_content_standards.
        """
        try:
            request = GetContentStandardsRequest.model_validate(params)
        except ValidationError as e:
            return invalid_request(e, payload=params)
        return await self.handle_get_content_standards(request, context)

    async def list_content_standards(
        self,
        params: ListContentStandardsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> ListContentStandardsResponse | NotImplementedResponse:
        """List content standards configurations.

        Validates params and delegates to handle_list_content_standards.
        """
        try:
            request = ListContentStandardsRequest.model_validate(params)
        except ValidationError as e:
            return invalid_request(e, payload=params)
        return await self.handle_list_content_standards(request, context)

    async def update_content_standards(
        self,
        params: UpdateContentStandardsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> UpdateContentStandardsResponse | NotImplementedResponse:
        """Update content standards configuration.

        Validates params and delegates to handle_update_content_standards.
        """
        try:
            request = UpdateContentStandardsRequest.model_validate(params)
        except ValidationError as e:
            return invalid_request(e, payload=params)
        return await self.handle_update_content_standards(request, context)

    async def calibrate_content(
        self,
        params: CalibrateContentRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> CalibrateContentResponse | NotImplementedResponse:
        """Calibrate content against standards.

        Validates params and delegates to handle_calibrate_content.
        """
        try:
            request = CalibrateContentRequest.model_validate(params)
        except ValidationError as e:
            return invalid_request(e, payload=params)
        return await self.handle_calibrate_content(request, context)

    async def validate_content_delivery(
        self,
        params: ValidateContentDeliveryRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> ValidateContentDeliveryResponse | NotImplementedResponse:
        """Validate content delivery against standards.

        Validates params and delegates to handle_validate_content_delivery.
        """
        try:
            request = ValidateContentDeliveryRequest.model_validate(params)
        except ValidationError as e:
            return invalid_request(e, payload=params)
        return await self.handle_validate_content_delivery(request, context)

    async def get_media_buy_artifacts(
        self,
        params: GetMediaBuyArtifactsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> GetMediaBuyArtifactsResponse | NotImplementedResponse:
        """Get artifacts associated with a media buy.

        Validates params and delegates to handle_get_media_buy_artifacts.
        """
        try:
            request = GetMediaBuyArtifactsRequest.model_validate(params)
        except ValidationError as e:
            return invalid_request(e, payload=params)
        return await self.handle_get_media_buy_artifacts(request, context)

    # ========================================================================
    # Abstract handlers - Implement these in subclasses
    # ========================================================================

    @abstractmethod
    async def handle_create_content_standards(
        self,
        request: CreateContentStandardsRequest,
        context: TContext | None = None,
    ) -> CreateContentStandardsResponse:
        """Handle create content standards request."""
        ...

    @abstractmethod
    async def handle_get_content_standards(
        self,
        request: GetContentStandardsRequest,
        context: TContext | None = None,
    ) -> GetContentStandardsResponse:
        """Handle get content standards request."""
        ...

    @abstractmethod
    async def handle_list_content_standards(
        self,
        request: ListContentStandardsRequest,
        context: TContext | None = None,
    ) -> ListContentStandardsResponse:
        """Handle list content standards request."""
        ...

    @abstractmethod
    async def handle_update_content_standards(
        self,
        request: UpdateContentStandardsRequest,
        context: TContext | None = None,
    ) -> UpdateContentStandardsResponse:
        """Handle update content standards request."""
        ...

    @abstractmethod
    async def handle_calibrate_content(
        self,
        request: CalibrateContentRequest,
        context: TContext | None = None,
    ) -> CalibrateContentResponse:
        """Handle calibrate content request."""
        ...

    @abstractmethod
    async def handle_validate_content_delivery(
        self,
        request: ValidateContentDeliveryRequest,
        context: TContext | None = None,
    ) -> ValidateContentDeliveryResponse:
        """Handle validate content delivery request."""
        ...

    @abstractmethod
    async def handle_get_media_buy_artifacts(
        self,
        request: GetMediaBuyArtifactsRequest,
        context: TContext | None = None,
    ) -> GetMediaBuyArtifactsResponse:
        """Handle get media buy artifacts request."""
        ...
