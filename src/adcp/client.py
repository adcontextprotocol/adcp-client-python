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
from uuid import uuid4

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
)

logger = logging.getLogger(__name__)


def create_operation_id() -> str:
    """Generate a unique operation ID."""
    return f"op_{uuid4().hex[:12]}"


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

    async def get_products(self, brief: str, **kwargs: Any) -> TaskResult[Any]:
        """Get advertising products."""
        operation_id = create_operation_id()
        params = {"brief": brief, **kwargs}

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

    async def list_creative_formats(self, **kwargs: Any) -> TaskResult[Any]:
        """List supported creative formats."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creative_formats",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("list_creative_formats", kwargs)

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

        return result

    async def create_media_buy(self, **kwargs: Any) -> TaskResult[Any]:
        """Create a new media buy."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_media_buy",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("create_media_buy", kwargs)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="create_media_buy",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

    async def update_media_buy(self, **kwargs: Any) -> TaskResult[Any]:
        """Update an existing media buy."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_media_buy",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("update_media_buy", kwargs)

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_RESPONSE,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="update_media_buy",
                status=result.status,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        return result

    async def sync_creatives(self, **kwargs: Any) -> TaskResult[Any]:
        """Synchronize creatives with the agent."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="sync_creatives",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("sync_creatives", kwargs)

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

    async def list_creatives(self, **kwargs: Any) -> TaskResult[Any]:
        """List creatives for a media buy."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_creatives",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("list_creatives", kwargs)

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

    async def get_media_buy_delivery(self, **kwargs: Any) -> TaskResult[Any]:
        """Get delivery metrics for a media buy."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_media_buy_delivery",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("get_media_buy_delivery", kwargs)

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

    async def list_authorized_properties(self, **kwargs: Any) -> TaskResult[Any]:
        """List properties this agent is authorized to sell."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="list_authorized_properties",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("list_authorized_properties", kwargs)

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

    async def get_signals(self, **kwargs: Any) -> TaskResult[Any]:
        """Get available signals for targeting."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="get_signals",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("get_signals", kwargs)

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

    async def activate_signal(self, **kwargs: Any) -> TaskResult[Any]:
        """Activate a signal for use in campaigns."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="activate_signal",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("activate_signal", kwargs)

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

    async def provide_performance_feedback(self, **kwargs: Any) -> TaskResult[Any]:
        """Provide performance feedback for a campaign."""
        operation_id = create_operation_id()

        self._emit_activity(
            Activity(
                type=ActivityType.PROTOCOL_REQUEST,
                operation_id=operation_id,
                agent_id=self.agent_config.id,
                task_type="provide_performance_feedback",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        result = await self.adapter.call_tool("provide_performance_feedback", kwargs)

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

    async def __aenter__(self) -> "ADCPClient":
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
            logger.warning(f"Webhook signature verification failed for agent {self.agent_config.id}")
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

    async def get_products(self, brief: str, **kwargs: Any) -> list[TaskResult[Any]]:
        """Execute get_products across all agents in parallel."""
        import asyncio

        tasks = [agent.get_products(brief, **kwargs) for agent in self.agents.values()]
        return await asyncio.gather(*tasks)

    @classmethod
    def from_env(cls) -> "ADCPMultiAgentClient":
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
