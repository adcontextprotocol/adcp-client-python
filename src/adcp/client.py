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

from adcp.exceptions import ADCPWebhookSignatureError
from adcp.protocols.a2a import A2AAdapter
from adcp.protocols.base import ProtocolAdapter
from adcp.protocols.mcp import MCPAdapter
from adcp.types.core import (
    Activity,
    ActivityType,
    AgentConfig,
    Protocol,
    TaskResult,
    TaskStatus,
)
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
from adcp.utils.operation_id import create_operation_id
from adcp.utils.response_parser import parse_json_or_text, parse_mcp_content

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
    ) -> TaskResult[GetProductsResponse]:
        """
        Get advertising products.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing GetProductsResponse
        """
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

        result = await self.adapter.call_tool("get_products", params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_products",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

    async def list_creative_formats(
        self,
        request: ListCreativeFormatsRequest,
    ) -> TaskResult[ListCreativeFormatsResponse]:
        """
        List supported creative formats.

        Args:
            request: Request parameters

        Returns:
            TaskResult containing ListCreativeFormatsResponse
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

        result = await self.adapter.call_tool("list_creative_formats", params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creative_formats",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        # Parse response data into structured type
        if result.success and result.data is not None:
            try:
                # Handle MCP content arrays
                if isinstance(result.data, list):
                    parsed_data = parse_mcp_content(result.data, ListCreativeFormatsResponse)
                else:
                    # Handle A2A or direct responses
                    parsed_data = parse_json_or_text(result.data, ListCreativeFormatsResponse)

                return TaskResult[ListCreativeFormatsResponse](
                    status=result.status,
                    data=parsed_data,
                    success=result.success,
                    error=result.error,
                    metadata=result.metadata,
                    debug_info=result.debug_info,
                )
            except ValueError as e:
                # Parsing failed - return error result
                logger.error(f"Failed to parse list_creative_formats response: {e}")
                return TaskResult[ListCreativeFormatsResponse](
                    status=TaskStatus.FAILED,
                    error=f"Failed to parse response: {e}",
                    success=False,
                    debug_info=result.debug_info,
                )

        return result  # type: ignore[return-value]

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

        result = await self.adapter.call_tool("sync_creatives", params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_creatives",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

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

        result = await self.adapter.call_tool("list_creatives", params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creatives",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

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

        result = await self.adapter.call_tool("get_media_buy_delivery", params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_delivery",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

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

        result = await self.adapter.call_tool("list_authorized_properties", params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_authorized_properties",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

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

        result = await self.adapter.call_tool("get_signals", params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_signals",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

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

        result = await self.adapter.call_tool("activate_signal", params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="activate_signal",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

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

        result = await self.adapter.call_tool("provide_performance_feedback", params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="provide_performance_feedback",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> TaskResult[Any]:
        """
        Call any tool on the agent.

        Args:
            tool_name: Name of the tool to call
            params: Tool parameters

        Returns:
            TaskResult with the response
        """
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type=tool_name,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool(tool_name, params)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type=tool_name,
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

    async def list_tools(self) -> list[str]:
        """
        List available tools from the agent.

        Returns:
            List of tool names
        """
        return await self.adapter.list_tools()

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

    async def handle_webhook(
        self,
        payload: dict[str, Any],
        signature: str | None = None,
    ) -> None:
        """
        Handle incoming webhook.

        Args:
            payload: Webhook payload
            signature: Webhook signature for verification

        Raises:
            ADCPWebhookSignatureError: If signature verification fails
        """
        if signature and not self._verify_webhook_signature(payload, signature):
            logger.warning(
                f"Webhook signature verification failed for agent {self.agent_config.id}"
            )
            raise ADCPWebhookSignatureError("Invalid webhook signature")

        operation_id = payload.get("operation_id", "unknown")
        task_type = payload.get("task_type", "unknown")

        self._emit_activity(
            Activity(
                type=ActivityType.WEBHOOK_RECEIVED,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type=task_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata={"payload": payload},
            )
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
