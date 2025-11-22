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
    WebhookPayload,
)
from adcp.types.core import (
    Activity,
    ActivityType,
    AgentConfig,
    Protocol,
    TaskResult,
    TaskStatus,
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

    def _verify_webhook_signature(self, payload: dict[str, Any], signature: str) -> bool:
        """
        Verify HMAC-SHA256 signature of webhook payload.

        Args:
            payload: Webhook payload dict
            signature: Signature to verify

        Returns:
            True if signature is valid, False otherwise
        """
        if not self.webhook_secret:
            return True

        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        expected_signature = hmac.new(
            self.webhook_secret.encode("utf-8"), payload_bytes, hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected_signature)

    def _parse_webhook_result(self, webhook: WebhookPayload) -> TaskResult[Any]:
        """
        Parse webhook payload into typed TaskResult based on task_type.

        Args:
            webhook: Validated webhook payload

        Returns:
            TaskResult with task-specific typed response data
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

        if webhook.status == GeneratedTaskStatus.completed and webhook.result is not None:
            response_type = response_type_map.get(webhook.task_type.value)
            if response_type:
                try:
                    parsed_result: Any = parse_json_or_text(webhook.result, response_type)
                    return TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=parsed_result,
                        success=True,
                        metadata={
                            "task_id": webhook.task_id,
                            "operation_id": webhook.operation_id,
                            "timestamp": webhook.timestamp,
                            "message": webhook.message,
                        },
                    )
                except ValueError as e:
                    logger.warning(f"Failed to parse webhook result: {e}")
                    # Fall through to untyped result

        # Handle failed, input-required, or unparseable results
        # Convert webhook status to core TaskStatus enum
        # Map generated enum values to core enum values
        status_map = {
            GeneratedTaskStatus.completed: TaskStatus.COMPLETED,
            GeneratedTaskStatus.submitted: TaskStatus.SUBMITTED,
            GeneratedTaskStatus.working: TaskStatus.WORKING,
            GeneratedTaskStatus.failed: TaskStatus.FAILED,
            GeneratedTaskStatus.input_required: TaskStatus.NEEDS_INPUT,
        }
        task_status = status_map.get(webhook.status, TaskStatus.FAILED)

        return TaskResult[Any](
            status=task_status,
            data=webhook.result,
            success=webhook.status == GeneratedTaskStatus.completed,
            error=webhook.error if isinstance(webhook.error, str) else None,
            metadata={
                "task_id": webhook.task_id,
                "operation_id": webhook.operation_id,
                "timestamp": webhook.timestamp,
                "message": webhook.message,
                "context_id": webhook.context_id,
                "progress": webhook.progress,
            },
        )

    async def handle_webhook(
        self,
        payload: dict[str, Any],
        signature: str | None = None,
    ) -> TaskResult[Any]:
        """
        Handle incoming webhook and return typed result.

        This method:
        1. Verifies webhook signature (if provided)
        2. Validates payload against WebhookPayload schema
        3. Parses task-specific result data into typed response
        4. Emits activity for monitoring

        Args:
            payload: Webhook payload dict
            signature: Optional HMAC-SHA256 signature for verification

        Returns:
            TaskResult with parsed task-specific response data

        Raises:
            ADCPWebhookSignatureError: If signature verification fails
            ValidationError: If payload doesn't match WebhookPayload schema

        Example:
            >>> result = await client.handle_webhook(payload, signature)
            >>> if result.success and isinstance(result.data, GetProductsResponse):
            >>>     print(f"Found {len(result.data.products)} products")
        """
        # Verify signature before processing
        if signature and not self._verify_webhook_signature(payload, signature):
            logger.warning(
                f"Webhook signature verification failed for agent {self.agent_config.id}"
            )
            raise ADCPWebhookSignatureError("Invalid webhook signature")

        # Validate and parse webhook payload
        webhook = WebhookPayload.model_validate(payload)

        # Emit activity for monitoring
        self._emit_activity(
            Activity(
                type=ActivityType.WEBHOOK_RECEIVED,
                operation_id=webhook.operation_id or "unknown",
                agent_id=self.agent_config.id,
                task_type=webhook.task_type.value,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={"payload": payload},
            )
        )

        # Parse and return typed result
        return self._parse_webhook_result(webhook)


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
