from __future__ import annotations

"""Main client classes for AdCP."""

import hashlib
import hmac
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from a2a.types import Task, TaskStatusUpdateEvent
from pydantic import BaseModel

from adcp.exceptions import ADCPWebhookSignatureError
from adcp.protocols.a2a import A2AAdapter
from adcp.protocols.base import ProtocolAdapter
from adcp.protocols.mcp import MCPAdapter
from adcp.types import (
    ActivateSignalRequest,
    ActivateSignalResponse,
    BuildCreativeRequest,
    BuildCreativeResponse,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    GeneratedTaskStatus,
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
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
)
from adcp.types.core import (
    Activity,
    ActivityType,
    AgentConfig,
    Protocol,
    TaskResult,
    TaskStatus,
)
from adcp.types.generated_poc.content_standards.calibrate_content_request import (
    CalibrateContentRequest,
)
from adcp.types.generated_poc.content_standards.calibrate_content_response import (
    CalibrateContentResponse,
)

# V3 Content Standards types
from adcp.types.generated_poc.content_standards.create_content_standards_request import (
    CreateContentStandardsRequest,
)
from adcp.types.generated_poc.content_standards.create_content_standards_response import (
    CreateContentStandardsResponse,
)
from adcp.types.generated_poc.content_standards.get_content_standards_request import (
    GetContentStandardsRequest,
)
from adcp.types.generated_poc.content_standards.get_content_standards_response import (
    GetContentStandardsResponse,
)
from adcp.types.generated_poc.content_standards.get_media_buy_artifacts_request import (
    GetMediaBuyArtifactsRequest,
)
from adcp.types.generated_poc.content_standards.get_media_buy_artifacts_response import (
    GetMediaBuyArtifactsResponse,
)
from adcp.types.generated_poc.content_standards.list_content_standards_request import (
    ListContentStandardsRequest,
)
from adcp.types.generated_poc.content_standards.list_content_standards_response import (
    ListContentStandardsResponse,
)
from adcp.types.generated_poc.content_standards.update_content_standards_request import (
    UpdateContentStandardsRequest,
)
from adcp.types.generated_poc.content_standards.update_content_standards_response import (
    UpdateContentStandardsResponse,
)
from adcp.types.generated_poc.content_standards.validate_content_delivery_request import (
    ValidateContentDeliveryRequest,
)
from adcp.types.generated_poc.content_standards.validate_content_delivery_response import (
    ValidateContentDeliveryResponse,
)
from adcp.types.generated_poc.core.async_response_data import AdcpAsyncResponseData

# V3 Governance (Property Lists) types
from adcp.types.generated_poc.property.create_property_list_request import (
    CreatePropertyListRequest,
)
from adcp.types.generated_poc.property.create_property_list_response import (
    CreatePropertyListResponse,
)
from adcp.types.generated_poc.property.delete_property_list_request import (
    DeletePropertyListRequest,
)
from adcp.types.generated_poc.property.delete_property_list_response import (
    DeletePropertyListResponse,
)
from adcp.types.generated_poc.property.get_property_list_request import (
    GetPropertyListRequest,
)
from adcp.types.generated_poc.property.get_property_list_response import (
    GetPropertyListResponse,
)
from adcp.types.generated_poc.property.list_property_lists_request import (
    ListPropertyListsRequest,
)
from adcp.types.generated_poc.property.list_property_lists_response import (
    ListPropertyListsResponse,
)
from adcp.types.generated_poc.property.update_property_list_request import (
    UpdatePropertyListRequest,
)
from adcp.types.generated_poc.property.update_property_list_response import (
    UpdatePropertyListResponse,
)

# V3 Protocol Discovery types
from adcp.types.generated_poc.protocol.get_adcp_capabilities_request import (
    GetAdcpCapabilitiesRequest,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    GetAdcpCapabilitiesResponse,
)

# V3 Sponsored Intelligence types
from adcp.types.generated_poc.sponsored_intelligence.si_get_offering_request import (
    SiGetOfferingRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_get_offering_response import (
    SiGetOfferingResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_initiate_session_request import (
    SiInitiateSessionRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_initiate_session_response import (
    SiInitiateSessionResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_send_message_request import (
    SiSendMessageRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_send_message_response import (
    SiSendMessageResponse,
)
from adcp.types.generated_poc.sponsored_intelligence.si_terminate_session_request import (
    SiTerminateSessionRequest,
)
from adcp.types.generated_poc.sponsored_intelligence.si_terminate_session_response import (
    SiTerminateSessionResponse,
)
from adcp.utils.operation_id import create_operation_id

logger = logging.getLogger(__name__)


class ADCPClient:
    """Client for interacting with a single AdCP agent."""

    def __init__(
        self,
        agent_config: AgentConfig,
        webhook_url_template: str | None = None,
        webhook_secret: str | None = None,
        on_activity: Callable[[Activity], None] | None = None,
    ):
        """
        Initialize ADCP client for a single agent.

        Args:
            agent_config: Agent configuration
            webhook_url_template: Template for webhook URLs with {agent_id},
                {task_type}, {operation_id}
            webhook_secret: Secret for webhook signature verification
            on_activity: Callback for activity events
        """
        self.agent_config = agent_config
        self.webhook_url_template = webhook_url_template
        self.webhook_secret = webhook_secret
        self.on_activity = on_activity

        # Initialize protocol adapter
        self.adapter: ProtocolAdapter
        if agent_config.protocol == Protocol.A2A:
            self.adapter = A2AAdapter(agent_config)
        elif agent_config.protocol == Protocol.MCP:
            self.adapter = MCPAdapter(agent_config)
        else:
            raise ValueError(f"Unsupported protocol: {agent_config.protocol}")

        # Initialize simple API accessor (lazy import to avoid circular dependency)
        from adcp.simple import SimpleAPI

        self.simple = SimpleAPI(self)

    def get_webhook_url(self, task_type: str, operation_id: str) -> str:
        """Generate webhook URL for a task."""
        if not self.webhook_url_template:
            raise ValueError("webhook_url_template not configured")

        return self.webhook_url_template.format(
            agent_id=self.agent_config.id,
            task_type=task_type,
            operation_id=operation_id,
        )

    def _emit_activity(self, activity: Activity) -> None:
        """Emit activity event."""
        if self.on_activity:
            self.on_activity(activity)

    async def get_products(
        self,
        request: GetProductsRequest,
        fetch_previews: bool = False,
        preview_output_format: str = "url",
        creative_agent_client: ADCPClient | None = None,
    ) -> TaskResult[GetProductsResponse]:
        """
        Get advertising products.

        Args:
            request: Request parameters
            fetch_previews: If True, generate preview URLs for each product's formats
                (uses batch API for 5-10x performance improvement)
            preview_output_format: "url" for iframe URLs (default), "html" for direct
                embedding (2-3x faster, no iframe overhead)
            creative_agent_client: Client for creative agent (required if
                fetch_previews=True)

        Returns:
            TaskResult containing GetProductsResponse with optional preview URLs in metadata

        Raises:
            ValueError: If fetch_previews=True but creative_agent_client is not provided
        """
        if fetch_previews and not creative_agent_client:
            raise ValueError("creative_agent_client is required when fetch_previews=True")

        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_products",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_products(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_products",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result: TaskResult[GetProductsResponse] = self.adapter._parse_response(
            raw_result, GetProductsResponse
        )

        if fetch_previews and result.success and result.data and creative_agent_client:
            from adcp.utils.preview_cache import add_preview_urls_to_products

            products_with_previews = await add_preview_urls_to_products(
                result.data.products,
                creative_agent_client,
                use_batch=True,
                output_format=preview_output_format,
            )
            result.metadata = result.metadata or {}
            result.metadata["products_with_previews"] = products_with_previews

        return result

    async def list_creative_formats(
        self,
        request: ListCreativeFormatsRequest,
        fetch_previews: bool = False,
        preview_output_format: str = "url",
    ) -> TaskResult[ListCreativeFormatsResponse]:
        """
        List supported creative formats.

        Args:
            request: Request parameters
            fetch_previews: If True, generate preview URLs for each format using
                sample manifests (uses batch API for 5-10x performance improvement)
            preview_output_format: "url" for iframe URLs (default), "html" for direct
                embedding (2-3x faster, no iframe overhead)

        Returns:
            TaskResult containing ListCreativeFormatsResponse with optional preview URLs in metadata
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creative_formats",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_creative_formats(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creative_formats",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result: TaskResult[ListCreativeFormatsResponse] = self.adapter._parse_response(
            raw_result, ListCreativeFormatsResponse
        )

        if fetch_previews and result.success and result.data:
            from adcp.utils.preview_cache import add_preview_urls_to_formats

            formats_with_previews = await add_preview_urls_to_formats(
                result.data.formats,
                self,
                use_batch=True,
                output_format=preview_output_format,
            )
            result.metadata = result.metadata or {}
            result.metadata["formats_with_previews"] = formats_with_previews

        return result

    async def preview_creative(
        self,
        request: PreviewCreativeRequest,
    ) -> TaskResult[PreviewCreativeResponse]:
        """
        Generate preview of a creative manifest.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing PreviewCreativeResponse with preview URLs
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="preview_creative",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.preview_creative(params)  # type: ignore[attr-defined]

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="preview_creative",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, PreviewCreativeResponse)

    async def sync_creatives(
        self,
        request: SyncCreativesRequest,
    ) -> TaskResult[SyncCreativesResponse]:
        """
        Sync Creatives.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing SyncCreativesResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_creatives",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.sync_creatives(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_creatives",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SyncCreativesResponse)

    async def list_creatives(
        self,
        request: ListCreativesRequest,
    ) -> TaskResult[ListCreativesResponse]:
        """
        List Creatives.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ListCreativesResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creatives",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_creatives(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creatives",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListCreativesResponse)

    async def get_media_buy_delivery(
        self,
        request: GetMediaBuyDeliveryRequest,
    ) -> TaskResult[GetMediaBuyDeliveryResponse]:
        """
        Get Media Buy Delivery.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing GetMediaBuyDeliveryResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_delivery",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_media_buy_delivery(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_delivery",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetMediaBuyDeliveryResponse)

    async def list_authorized_properties(
        self,
        request: ListAuthorizedPropertiesRequest,
    ) -> TaskResult[ListAuthorizedPropertiesResponse]:
        """
        List Authorized Properties.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ListAuthorizedPropertiesResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_authorized_properties",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_authorized_properties(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_authorized_properties",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListAuthorizedPropertiesResponse)

    async def get_signals(
        self,
        request: GetSignalsRequest,
    ) -> TaskResult[GetSignalsResponse]:
        """
        Get Signals.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing GetSignalsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_signals",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_signals(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_signals",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetSignalsResponse)

    async def activate_signal(
        self,
        request: ActivateSignalRequest,
    ) -> TaskResult[ActivateSignalResponse]:
        """
        Activate Signal.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ActivateSignalResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="activate_signal",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.activate_signal(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="activate_signal",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ActivateSignalResponse)

    async def provide_performance_feedback(
        self,
        request: ProvidePerformanceFeedbackRequest,
    ) -> TaskResult[ProvidePerformanceFeedbackResponse]:
        """
        Provide Performance Feedback.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ProvidePerformanceFeedbackResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="provide_performance_feedback",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.provide_performance_feedback(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="provide_performance_feedback",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ProvidePerformanceFeedbackResponse)

    async def create_media_buy(
        self,
        request: CreateMediaBuyRequest,
    ) -> TaskResult[CreateMediaBuyResponse]:
        """
        Create a new media buy reservation.

        Requests the agent to reserve inventory for a campaign. The agent returns a
        media_buy_id that tracks this reservation and can be used for updates.

        Args:
            request: Media buy creation parameters including:
                - brand_manifest: Advertiser brand information and creative assets
                - packages: List of package requests specifying desired inventory
                - publisher_properties: Target properties for ad placement
                - budget: Optional budget constraints
                - start_date/end_date: Campaign flight dates

        Returns:
            TaskResult containing CreateMediaBuyResponse with:
                - media_buy_id: Unique identifier for this reservation
                - status: Current state of the media buy
                - packages: Confirmed package details
                - Additional platform-specific metadata

        Example:
            >>> from adcp import ADCPClient, CreateMediaBuyRequest
            >>> client = ADCPClient(agent_config)
            >>> request = CreateMediaBuyRequest(
            ...     brand_manifest=brand,
            ...     packages=[package_request],
            ...     publisher_properties=properties
            ... )
            >>> result = await client.create_media_buy(request)
            >>> if result.success:
            ...     media_buy_id = result.data.media_buy_id
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_media_buy",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.create_media_buy(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_media_buy",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, CreateMediaBuyResponse)

    async def update_media_buy(
        self,
        request: UpdateMediaBuyRequest,
    ) -> TaskResult[UpdateMediaBuyResponse]:
        """
        Update an existing media buy reservation.

        Modifies a previously created media buy by updating packages or publisher
        properties. The update operation uses discriminated unions to specify what
        to change - either package details or targeting properties.

        Args:
            request: Media buy update parameters including:
                - media_buy_id: Identifier from create_media_buy response
                - updates: Discriminated union specifying update type:
                    * UpdateMediaBuyPackagesRequest: Modify package selections
                    * UpdateMediaBuyPropertiesRequest: Change targeting properties

        Returns:
            TaskResult containing UpdateMediaBuyResponse with:
                - media_buy_id: The updated media buy identifier
                - status: Updated state of the media buy
                - packages: Updated package configurations
                - Additional platform-specific metadata

        Example:
            >>> from adcp import ADCPClient, UpdateMediaBuyPackagesRequest
            >>> client = ADCPClient(agent_config)
            >>> request = UpdateMediaBuyPackagesRequest(
            ...     media_buy_id="mb_123",
            ...     packages=[updated_package]
            ... )
            >>> result = await client.update_media_buy(request)
            >>> if result.success:
            ...     updated_packages = result.data.packages
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_media_buy",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.update_media_buy(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_media_buy",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, UpdateMediaBuyResponse)

    async def build_creative(
        self,
        request: BuildCreativeRequest,
    ) -> TaskResult[BuildCreativeResponse]:
        """
        Generate production-ready creative assets.

        Requests the creative agent to build final deliverable assets in the target
        format (e.g., VAST, DAAST, HTML5). This is typically called after previewing
        and approving a creative manifest.

        Args:
            request: Creative build parameters including:
                - manifest: Creative manifest with brand info and content
                - target_format_id: Desired output format identifier
                - inputs: Optional user-provided inputs for template variables
                - deployment: Platform or agent deployment configuration

        Returns:
            TaskResult containing BuildCreativeResponse with:
                - assets: Production-ready creative files (URLs or inline content)
                - format_id: The generated format identifier
                - manifest: The creative manifest used for generation
                - metadata: Additional platform-specific details

        Example:
            >>> from adcp import ADCPClient, BuildCreativeRequest
            >>> client = ADCPClient(agent_config)
            >>> request = BuildCreativeRequest(
            ...     manifest=creative_manifest,
            ...     target_format_id="vast_2.0",
            ...     inputs={"duration": 30}
            ... )
            >>> result = await client.build_creative(request)
            >>> if result.success:
            ...     vast_url = result.data.assets[0].url
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="build_creative",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.build_creative(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="build_creative",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, BuildCreativeResponse)

    # ========================================================================
    # V3 Protocol Methods - Protocol Discovery
    # ========================================================================

    async def get_adcp_capabilities(
        self,
        request: GetAdcpCapabilitiesRequest,
    ) -> TaskResult[GetAdcpCapabilitiesResponse]:
        """
        Get AdCP capabilities from the agent.

        Queries the agent's supported AdCP features, protocol versions, and
        domain-specific capabilities (media_buy, signals, sponsored_intelligence).

        Args:
            request: Request parameters including optional protocol filters

        Returns:
            TaskResult containing GetAdcpCapabilitiesResponse with:
                - adcp: Core protocol version information
                - supported_protocols: List of supported domain protocols
                - media_buy: Media buy capabilities (if supported)
                - sponsored_intelligence: SI capabilities (if supported)
                - signals: Signals capabilities (if supported)
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_adcp_capabilities",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_adcp_capabilities(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_adcp_capabilities",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetAdcpCapabilitiesResponse)

    # ========================================================================
    # V3 Protocol Methods - Content Standards
    # ========================================================================

    async def create_content_standards(
        self,
        request: CreateContentStandardsRequest,
    ) -> TaskResult[CreateContentStandardsResponse]:
        """
        Create a new content standards configuration.

        Defines acceptable content contexts for ad placement using natural
        language policy and optional calibration exemplars.

        Args:
            request: Request parameters including policy and scope

        Returns:
            TaskResult containing CreateContentStandardsResponse with standards_id
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_content_standards",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.create_content_standards(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_content_standards",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, CreateContentStandardsResponse)

    async def get_content_standards(
        self,
        request: GetContentStandardsRequest,
    ) -> TaskResult[GetContentStandardsResponse]:
        """
        Get a content standards configuration by ID.

        Args:
            request: Request parameters including standards_id

        Returns:
            TaskResult containing GetContentStandardsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_content_standards",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_content_standards(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_content_standards",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetContentStandardsResponse)

    async def list_content_standards(
        self,
        request: ListContentStandardsRequest,
    ) -> TaskResult[ListContentStandardsResponse]:
        """
        List content standards configurations.

        Args:
            request: Request parameters including optional filters

        Returns:
            TaskResult containing ListContentStandardsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_content_standards",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_content_standards(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_content_standards",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListContentStandardsResponse)

    async def update_content_standards(
        self,
        request: UpdateContentStandardsRequest,
    ) -> TaskResult[UpdateContentStandardsResponse]:
        """
        Update a content standards configuration.

        Args:
            request: Request parameters including standards_id and updates

        Returns:
            TaskResult containing UpdateContentStandardsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_content_standards",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.update_content_standards(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_content_standards",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, UpdateContentStandardsResponse)

    async def calibrate_content(
        self,
        request: CalibrateContentRequest,
    ) -> TaskResult[CalibrateContentResponse]:
        """
        Calibrate content against standards.

        Evaluates content (artifact or URL) against configured standards to
        determine suitability for ad placement.

        Args:
            request: Request parameters including content to evaluate

        Returns:
            TaskResult containing CalibrateContentResponse with verdict
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="calibrate_content",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.calibrate_content(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="calibrate_content",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, CalibrateContentResponse)

    async def validate_content_delivery(
        self,
        request: ValidateContentDeliveryRequest,
    ) -> TaskResult[ValidateContentDeliveryResponse]:
        """
        Validate content delivery against standards.

        Validates that ad delivery records comply with content standards.

        Args:
            request: Request parameters including delivery records

        Returns:
            TaskResult containing ValidateContentDeliveryResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="validate_content_delivery",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.validate_content_delivery(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="validate_content_delivery",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ValidateContentDeliveryResponse)

    async def get_media_buy_artifacts(
        self,
        request: GetMediaBuyArtifactsRequest,
    ) -> TaskResult[GetMediaBuyArtifactsResponse]:
        """
        Get artifacts associated with a media buy.

        Retrieves content artifacts where ads were delivered for a media buy.

        Args:
            request: Request parameters including media_buy_id

        Returns:
            TaskResult containing GetMediaBuyArtifactsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_artifacts",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_media_buy_artifacts(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_artifacts",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetMediaBuyArtifactsResponse)

    # ========================================================================
    # V3 Protocol Methods - Sponsored Intelligence
    # ========================================================================

    async def si_get_offering(
        self,
        request: SiGetOfferingRequest,
    ) -> TaskResult[SiGetOfferingResponse]:
        """
        Get sponsored intelligence offering.

        Retrieves product/service offerings that can be presented in a
        sponsored intelligence session.

        Args:
            request: Request parameters including brand context

        Returns:
            TaskResult containing SiGetOfferingResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_get_offering",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.si_get_offering(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_get_offering",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SiGetOfferingResponse)

    async def si_initiate_session(
        self,
        request: SiInitiateSessionRequest,
    ) -> TaskResult[SiInitiateSessionResponse]:
        """
        Initiate a sponsored intelligence session.

        Starts a conversational brand experience session with a user.

        Args:
            request: Request parameters including identity and context

        Returns:
            TaskResult containing SiInitiateSessionResponse with session_id
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_initiate_session",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.si_initiate_session(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_initiate_session",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SiInitiateSessionResponse)

    async def si_send_message(
        self,
        request: SiSendMessageRequest,
    ) -> TaskResult[SiSendMessageResponse]:
        """
        Send a message in a sponsored intelligence session.

        Continues the conversation in an active SI session.

        Args:
            request: Request parameters including session_id and message

        Returns:
            TaskResult containing SiSendMessageResponse with brand response
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_send_message",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.si_send_message(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_send_message",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SiSendMessageResponse)

    async def si_terminate_session(
        self,
        request: SiTerminateSessionRequest,
    ) -> TaskResult[SiTerminateSessionResponse]:
        """
        Terminate a sponsored intelligence session.

        Ends an active SI session, optionally with follow-up actions.

        Args:
            request: Request parameters including session_id and termination context

        Returns:
            TaskResult containing SiTerminateSessionResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_terminate_session",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.si_terminate_session(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="si_terminate_session",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, SiTerminateSessionResponse)

    # ========================================================================
    # V3 Governance (Property Lists) Methods
    # ========================================================================

    async def create_property_list(
        self,
        request: CreatePropertyListRequest,
    ) -> TaskResult[CreatePropertyListResponse]:
        """
        Create a property list for governance filtering.

        Property lists define dynamic sets of properties based on filters,
        brand manifests, and feature requirements.

        Args:
            request: Request parameters for creating the property list

        Returns:
            TaskResult containing CreatePropertyListResponse with list_id
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_property_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.create_property_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_property_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, CreatePropertyListResponse)

    async def get_property_list(
        self,
        request: GetPropertyListRequest,
    ) -> TaskResult[GetPropertyListResponse]:
        """
        Get a property list with optional resolution.

        When resolve=true, returns the list of resolved property identifiers.
        Use this to get the actual properties that match the list's filters.

        Args:
            request: Request parameters including list_id and resolve flag

        Returns:
            TaskResult containing GetPropertyListResponse with identifiers
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_property_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.get_property_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_property_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, GetPropertyListResponse)

    async def list_property_lists(
        self,
        request: ListPropertyListsRequest,
    ) -> TaskResult[ListPropertyListsResponse]:
        """
        List property lists owned by a principal.

        Retrieves metadata for all property lists, optionally filtered
        by principal or pagination parameters.

        Args:
            request: Request parameters with optional filtering

        Returns:
            TaskResult containing ListPropertyListsResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_property_lists",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.list_property_lists(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_property_lists",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, ListPropertyListsResponse)

    async def update_property_list(
        self,
        request: UpdatePropertyListRequest,
    ) -> TaskResult[UpdatePropertyListResponse]:
        """
        Update a property list.

        Modifies the filters, brand manifest, or other parameters
        of an existing property list.

        Args:
            request: Request parameters with list_id and updates

        Returns:
            TaskResult containing UpdatePropertyListResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_property_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.update_property_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_property_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, UpdatePropertyListResponse)

    async def delete_property_list(
        self,
        request: DeletePropertyListRequest,
    ) -> TaskResult[DeletePropertyListResponse]:
        """
        Delete a property list.

        Removes a property list. Any active subscriptions to this list
        will be terminated.

        Args:
            request: Request parameters with list_id

        Returns:
            TaskResult containing DeletePropertyListResponse
        """
        operation_id = create_operation_id()
        params = request.model_dump(exclude_none=True)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="delete_property_list",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        raw_result = await self.adapter.delete_property_list(params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="delete_property_list",
                status=raw_result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return self.adapter._parse_response(raw_result, DeletePropertyListResponse)

    async def list_tools(self) -> list[str]:
        """
        List available tools from the agent.

        Returns:
            List of tool names
        """
        return await self.adapter.list_tools()

    async def get_info(self) -> dict[str, Any]:
        """
        Get agent information including AdCP extension metadata.

        Returns agent card information including:
        - Agent name, description, version
        - Protocol type (mcp or a2a)
        - AdCP version (from extensions.adcp.adcp_version)
        - Supported protocols (from extensions.adcp.protocols_supported)
        - Available tools/skills

        Returns:
            Dictionary with agent metadata
        """
        return await self.adapter.get_agent_info()

    async def close(self) -> None:
        """Close the adapter and clean up resources."""
        if hasattr(self.adapter, "close"):
            logger.debug(f"Closing adapter for agent {self.agent_config.id}")
            await self.adapter.close()

    async def __aenter__(self) -> ADCPClient:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    def _verify_webhook_signature(
        self, payload: dict[str, Any], signature: str, timestamp: str
    ) -> bool:
        """
        Verify HMAC-SHA256 signature of webhook payload.

        The verification algorithm matches get_adcp_signed_headers_for_webhook:
        1. Constructs message as "{timestamp}.{json_payload}"
        2. JSON-serializes payload with compact separators
        3. UTF-8 encodes the message
        4. HMAC-SHA256 signs with the shared secret
        5. Compares against the provided signature (with "sha256=" prefix stripped)

        Args:
            payload: Webhook payload dict
            signature: Signature to verify (with or without "sha256=" prefix)
            timestamp: ISO 8601 timestamp from X-AdCP-Timestamp header

        Returns:
            True if signature is valid, False otherwise
        """
        if not self.webhook_secret:
            return True

        # Strip "sha256=" prefix if present
        if signature.startswith("sha256="):
            signature = signature[7:]

        # Serialize payload to JSON with consistent formatting (matches signing)
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=False).encode("utf-8")

        # Construct signed message: timestamp.payload (matches get_adcp_signed_headers_for_webhook)
        signed_message = f"{timestamp}.{payload_bytes.decode('utf-8')}"

        # Generate expected signature
        expected_signature = hmac.new(
            self.webhook_secret.encode("utf-8"), signed_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected_signature)

    def _parse_webhook_result(
        self,
        task_id: str,
        task_type: str,
        operation_id: str,
        status: GeneratedTaskStatus,
        result: Any,
        timestamp: datetime | str,
        message: str | None,
        context_id: str | None,
    ) -> TaskResult[AdcpAsyncResponseData]:
        """
        Parse webhook data into typed TaskResult based on task_type.

        Args:
            task_id: Unique identifier for this task
            task_type: Task type from application routing (e.g., "get_products")
            operation_id: Operation identifier from application routing
            status: Current task status
            result: Task-specific payload (AdCP response data)
            timestamp: ISO 8601 timestamp when webhook was generated
            message: Human-readable summary of task state
            context_id: Session/conversation identifier

        Returns:
            TaskResult with task-specific typed response data

        Note:
            This method works with both MCP and A2A protocols by accepting
            protocol-agnostic parameters rather than protocol-specific objects.
        """
        from adcp.utils.response_parser import parse_json_or_text

        # Map task types to their response types (using string literals, not enum)
        # Note: Some response types are Union types (e.g., ActivateSignalResponse = Success | Error)
        response_type_map: dict[str, type[BaseModel] | Any] = {
            "get_products": GetProductsResponse,
            "list_creative_formats": ListCreativeFormatsResponse,
            "sync_creatives": SyncCreativesResponse,  # Union type
            "list_creatives": ListCreativesResponse,
            "get_media_buy_delivery": GetMediaBuyDeliveryResponse,
            "list_authorized_properties": ListAuthorizedPropertiesResponse,
            "get_signals": GetSignalsResponse,
            "activate_signal": ActivateSignalResponse,  # Union type
            "provide_performance_feedback": ProvidePerformanceFeedbackResponse,
        }

        # Handle completed tasks with result parsing
        if status == GeneratedTaskStatus.completed and result is not None:
            response_type = response_type_map.get(task_type)
            if response_type:
                try:
                    parsed_result: Any = parse_json_or_text(result, response_type)
                    return TaskResult[AdcpAsyncResponseData](
                        status=TaskStatus.COMPLETED,
                        data=parsed_result,
                        success=True,
                        metadata={
                            "task_id": task_id,
                            "operation_id": operation_id,
                            "timestamp": timestamp,
                            "message": message,
                        },
                    )
                except ValueError as e:
                    logger.warning(f"Failed to parse webhook result: {e}")
                    # Fall through to untyped result

        # Handle failed, input-required, or unparseable results
        # Convert status to core TaskStatus enum
        status_map = {
            GeneratedTaskStatus.completed: TaskStatus.COMPLETED,
            GeneratedTaskStatus.submitted: TaskStatus.SUBMITTED,
            GeneratedTaskStatus.working: TaskStatus.WORKING,
            GeneratedTaskStatus.failed: TaskStatus.FAILED,
            GeneratedTaskStatus.input_required: TaskStatus.NEEDS_INPUT,
        }
        task_status = status_map.get(status, TaskStatus.FAILED)

        # Extract error message from result.errors if present
        error_message: str | None = None
        if result is not None and hasattr(result, "errors"):
            errors = getattr(result, "errors", None)
            if errors and len(errors) > 0:
                first_error = errors[0]
                if hasattr(first_error, "message"):
                    error_message = first_error.message

        return TaskResult[AdcpAsyncResponseData](
            status=task_status,
            data=result,
            success=status == GeneratedTaskStatus.completed,
            error=error_message,
            metadata={
                "task_id": task_id,
                "operation_id": operation_id,
                "timestamp": timestamp,
                "message": message,
                "context_id": context_id,
            },
        )

    async def _handle_mcp_webhook(
        self,
        payload: dict[str, Any],
        task_type: str,
        operation_id: str,
        signature: str | None,
        timestamp: str | None = None,
    ) -> TaskResult[AdcpAsyncResponseData]:
        """
        Handle MCP webhook delivered via HTTP POST.

        Args:
            payload: Webhook payload dict
            task_type: Task type from application routing
            operation_id: Operation identifier from application routing
            signature: Optional HMAC-SHA256 signature for verification (X-AdCP-Signature header)
            timestamp: Optional timestamp for signature verification (X-AdCP-Timestamp header)

        Returns:
            TaskResult with parsed task-specific response data

        Raises:
            ADCPWebhookSignatureError: If signature verification fails
            ValidationError: If payload doesn't match McpWebhookPayload schema
        """
        from adcp.types.generated_poc.core.mcp_webhook_payload import McpWebhookPayload

        # Verify signature before processing (requires both signature and timestamp)
        if (
            signature
            and timestamp
            and not self._verify_webhook_signature(payload, signature, timestamp)
        ):
            logger.warning(
                f"Webhook signature verification failed for agent {self.agent_config.id}"
            )
            raise ADCPWebhookSignatureError("Invalid webhook signature")

        # Validate and parse MCP webhook payload
        webhook = McpWebhookPayload.model_validate(payload)

        # Emit activity for monitoring
        self._emit_activity(
            Activity(
                type=ActivityType.WEBHOOK_RECEIVED,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type=task_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={"payload": payload, "protocol": "mcp"},
            )
        )

        # Extract fields and parse result
        return self._parse_webhook_result(
            task_id=webhook.task_id,
            task_type=task_type,
            operation_id=operation_id,
            status=webhook.status,
            result=webhook.result,
            timestamp=webhook.timestamp,
            message=webhook.message,
            context_id=webhook.context_id,
        )

    async def _handle_a2a_webhook(
        self, payload: Task | TaskStatusUpdateEvent, task_type: str, operation_id: str
    ) -> TaskResult[AdcpAsyncResponseData]:
        """
        Handle A2A webhook delivered through Task or TaskStatusUpdateEvent.

        Per A2A specification:
        - Terminated statuses (completed, failed): Payload is Task with artifacts[].parts[]
        - Intermediate statuses (working, input-required, submitted):
        Payload is TaskStatusUpdateEvent with status.message.parts[]

        Args:
            payload: A2A Task or TaskStatusUpdateEvent object
            task_type: Task type from application routing
            operation_id: Operation identifier from application routing

        Returns:
            TaskResult with parsed task-specific response data

        Note:
            Signature verification is NOT applicable for A2A webhooks
            as they arrive through authenticated A2A connections, not HTTP.
        """
        from a2a.types import DataPart, TextPart

        adcp_data: Any = None
        text_message: str | None = None
        task_id: str
        context_id: str | None
        status_state: str
        timestamp: datetime | str

        # Type detection and extraction based on payload type
        if isinstance(payload, TaskStatusUpdateEvent):
            # Intermediate status: Extract from status.message.parts[]
            task_id = payload.task_id
            context_id = payload.context_id
            status_state = payload.status.state if payload.status else "failed"
            timestamp = (
                payload.status.timestamp
                if payload.status and payload.status.timestamp
                else datetime.now(timezone.utc)
            )

            # Extract from status.message.parts[]
            if payload.status and payload.status.message and payload.status.message.parts:
                # Extract DataPart for structured AdCP payload
                data_parts = [
                    p.root for p in payload.status.message.parts if isinstance(p.root, DataPart)
                ]
                if data_parts:
                    # Use last DataPart as authoritative
                    last_data_part = data_parts[-1]
                    adcp_data = last_data_part.data

                    # Unwrap {"response": {...}} wrapper if present (ADK pattern)
                    if isinstance(adcp_data, dict) and "response" in adcp_data:
                        if len(adcp_data) == 1:
                            adcp_data = adcp_data["response"]
                        else:
                            adcp_data = adcp_data["response"]

                # Extract TextPart for human-readable message
                for part in payload.status.message.parts:
                    if isinstance(part.root, TextPart):
                        text_message = part.root.text
                        break

        else:
            # Terminated status (Task): Extract from artifacts[].parts[]
            task_id = payload.id
            context_id = payload.context_id
            status_state = payload.status.state if payload.status else "failed"
            timestamp = (
                payload.status.timestamp
                if payload.status and payload.status.timestamp
                else datetime.now(timezone.utc)
            )

            # Extract from task.artifacts[].parts[]
            # Following A2A spec: use last artifact, last DataPart is authoritative
            if payload.artifacts:
                # Use last artifact (most recent in streaming scenarios)
                target_artifact = payload.artifacts[-1]

                if target_artifact.parts:
                    # Extract DataPart for structured AdCP payload
                    data_parts = [
                        p.root for p in target_artifact.parts if isinstance(p.root, DataPart)
                    ]
                    if data_parts:
                        # Use last DataPart as authoritative
                        last_data_part = data_parts[-1]
                        adcp_data = last_data_part.data

                        # Unwrap {"response": {...}} wrapper if present (ADK pattern)
                        if isinstance(adcp_data, dict) and "response" in adcp_data:
                            if len(adcp_data) == 1:
                                adcp_data = adcp_data["response"]
                            else:
                                adcp_data = adcp_data["response"]

                    # Extract TextPart for human-readable message
                    for part in target_artifact.parts:
                        if isinstance(part.root, TextPart):
                            text_message = part.root.text
                            break

        # Map A2A status.state to GeneratedTaskStatus enum
        status_map = {
            "completed": GeneratedTaskStatus.completed,
            "submitted": GeneratedTaskStatus.submitted,
            "working": GeneratedTaskStatus.working,
            "failed": GeneratedTaskStatus.failed,
            "input-required": GeneratedTaskStatus.input_required,
            "input_required": GeneratedTaskStatus.input_required,  # Handle both formats
        }
        mapped_status = status_map.get(status_state, GeneratedTaskStatus.failed)

        # Emit activity for monitoring
        self._emit_activity(
            Activity(
                type=ActivityType.WEBHOOK_RECEIVED,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type=task_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={
                    "task_id": task_id,
                    "protocol": "a2a",
                    "payload_type": (
                        "TaskStatusUpdateEvent"
                        if isinstance(payload, TaskStatusUpdateEvent)
                        else "Task"
                    ),
                },
            )
        )

        # Parse and return typed result by passing extracted fields directly
        return self._parse_webhook_result(
            task_id=task_id,
            task_type=task_type,
            operation_id=operation_id,
            status=mapped_status,
            result=adcp_data,
            timestamp=timestamp,
            message=text_message,
            context_id=context_id,
        )

    async def handle_webhook(
        self,
        payload: dict[str, Any] | Task | TaskStatusUpdateEvent,
        task_type: str,
        operation_id: str,
        signature: str | None = None,
        timestamp: str | None = None,
    ) -> TaskResult[AdcpAsyncResponseData]:
        """
        Handle incoming webhook and return typed result.

        This method provides a unified interface for handling webhooks from both
        MCP and A2A protocols:

        - MCP Webhooks: HTTP POST with dict payload, optional HMAC signature
        - A2A Webhooks: Task or TaskStatusUpdateEvent objects based on status

        The method automatically detects the protocol type and routes to the
        appropriate handler. Both protocols return a consistent TaskResult
        structure with typed AdCP response data.

        Args:
            payload: Webhook payload - one of:
                - dict[str, Any]: MCP webhook payload from HTTP POST
                - Task: A2A webhook for terminated statuses (completed, failed)
                - TaskStatusUpdateEvent: A2A webhook for intermediate statuses
                  (working, input-required, submitted)
            task_type: Task type from application routing (e.g., "get_products").
                Applications should extract this from URL routing pattern:
                /webhook/{task_type}/{agent_id}/{operation_id}
            operation_id: Operation identifier from application routing.
                Used to correlate webhook notifications with original task submission.
            signature: Optional HMAC-SHA256 signature for MCP webhook verification
                (X-AdCP-Signature header). Ignored for A2A webhooks.
            timestamp: Optional timestamp for MCP webhook signature verification
                (X-AdCP-Timestamp header). Required when signature is provided.

        Returns:
            TaskResult with parsed task-specific response data. The structure
            is identical regardless of protocol.

        Raises:
            ADCPWebhookSignatureError: If MCP signature verification fails
            ValidationError: If MCP payload doesn't match WebhookPayload schema

        Note:
            task_type and operation_id were deprecated from the webhook payload
            per AdCP specification. Applications must extract these from URL
            routing and pass them explicitly.

        Examples:
            MCP webhook (HTTP endpoint):
            >>> @app.post("/webhook/{task_type}/{agent_id}/{operation_id}")
            >>> async def webhook_handler(task_type: str, operation_id: str, request: Request):
            >>>     payload = await request.json()
            >>>     signature = request.headers.get("X-AdCP-Signature")
            >>>     timestamp = request.headers.get("X-AdCP-Timestamp")
            >>>     result = await client.handle_webhook(
            >>>         payload, task_type, operation_id, signature, timestamp
            >>>     )
            >>>     if result.success:
            >>>         print(f"Task completed: {result.data}")

            A2A webhook with Task (terminated status):
            >>> async def on_task_completed(task: Task):
            >>>     # Extract task_type and operation_id from your app's task tracking
            >>>     task_type = your_task_registry.get_type(task.id)
            >>>     operation_id = your_task_registry.get_operation_id(task.id)
            >>>     result = await client.handle_webhook(
            >>>         task, task_type, operation_id
            >>>     )
            >>>     if result.success:
            >>>         print(f"Task completed: {result.data}")

            A2A webhook with TaskStatusUpdateEvent (intermediate status):
            >>> async def on_task_update(event: TaskStatusUpdateEvent):
            >>>     # Extract task_type and operation_id from your app's task tracking
            >>>     task_type = your_task_registry.get_type(event.task_id)
            >>>     operation_id = your_task_registry.get_operation_id(event.task_id)
            >>>     result = await client.handle_webhook(
            >>>         event, task_type, operation_id
            >>>     )
            >>>     if result.status == GeneratedTaskStatus.working:
            >>>         print(f"Task still working: {result.metadata.get('message')}")
        """
        # Detect protocol type and route to appropriate handler
        if isinstance(payload, (Task, TaskStatusUpdateEvent)):
            # A2A webhook (Task or TaskStatusUpdateEvent)
            return await self._handle_a2a_webhook(payload, task_type, operation_id)
        else:
            # MCP webhook (dict payload)
            return await self._handle_mcp_webhook(
                payload, task_type, operation_id, signature, timestamp
            )


class ADCPMultiAgentClient:
    """Client for managing multiple AdCP agents."""

    def __init__(
        self,
        agents: list[AgentConfig],
        webhook_url_template: str | None = None,
        webhook_secret: str | None = None,
        on_activity: Callable[[Activity], None] | None = None,
        handlers: dict[str, Callable[..., Any]] | None = None,
    ):
        """
        Initialize multi-agent client.

        Args:
            agents: List of agent configurations
            webhook_url_template: Template for webhook URLs
            webhook_secret: Secret for webhook verification
            on_activity: Callback for activity events
            handlers: Task completion handlers
        """
        self.agents = {
            agent.id: ADCPClient(
                agent,
                webhook_url_template=webhook_url_template,
                webhook_secret=webhook_secret,
                on_activity=on_activity,
            )
            for agent in agents
        }
        self.handlers = handlers or {}

    def agent(self, agent_id: str) -> ADCPClient:
        """Get client for specific agent."""
        if agent_id not in self.agents:
            raise ValueError(f"Agent not found: {agent_id}")
        return self.agents[agent_id]

    @property
    def agent_ids(self) -> list[str]:
        """Get list of agent IDs."""
        return list(self.agents.keys())

    async def close(self) -> None:
        """Close all agent clients and clean up resources."""
        import asyncio

        logger.debug("Closing all agent clients in multi-agent client")
        close_tasks = [client.close() for client in self.agents.values()]
        await asyncio.gather(*close_tasks, return_exceptions=True)

    async def __aenter__(self) -> ADCPMultiAgentClient:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def get_products(
        self,
        request: GetProductsRequest,
    ) -> list[TaskResult[GetProductsResponse]]:
        """
        Execute get_products across all agents in parallel.

        Args:
            request: Request parameters

        Returns:
            List of TaskResults containing GetProductsResponse for each agent
        """
        import asyncio

        tasks = [agent.get_products(request) for agent in self.agents.values()]
        return await asyncio.gather(*tasks)

    @classmethod
    def from_env(cls) -> ADCPMultiAgentClient:
        """Create client from environment variables."""
        agents_json = os.getenv("ADCP_AGENTS")
        if not agents_json:
            raise ValueError("ADCP_AGENTS environment variable not set")

        agents_data = json.loads(agents_json)
        agents = [AgentConfig(**agent) for agent in agents_data]

        return cls(
            agents=agents,
            webhook_url_template=os.getenv("WEBHOOK_URL_TEMPLATE"),
            webhook_secret=os.getenv("WEBHOOK_SECRET"),
        )
