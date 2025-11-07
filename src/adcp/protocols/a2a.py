from __future__ import annotations

"""A2A protocol adapter using HTTP client.

The official a2a-sdk is primarily for building A2A servers. For client functionality,
we implement the A2A protocol using HTTP requests as per the A2A specification.
"""

import logging
import time
from typing import Any
from uuid import uuid4

import httpx

from adcp.exceptions import (
    ADCPAuthenticationError,
    ADCPConnectionError,
    ADCPTimeoutError,
)
from adcp.protocols.base import ProtocolAdapter
from adcp.types.core import AgentConfig, DebugInfo, TaskResult, TaskStatus

logger = logging.getLogger(__name__)


class A2AAdapter(ProtocolAdapter):
    """Adapter for A2A protocol following the Agent2Agent specification."""

    def __init__(self, agent_config: AgentConfig):
        """Initialize A2A adapter with reusable HTTP client."""
        super().__init__(agent_config)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client with connection pooling."""
        if self._client is None:
            # Configure connection pooling for better performance
            limits = httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0,
            )
            self._client = httpx.AsyncClient(limits=limits)
            logger.debug(
                f"Created HTTP client with connection pooling for agent {self.agent_config.id}"
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client and clean up resources."""
        if self._client is not None:
            logger.debug(f"Closing A2A adapter client for agent {self.agent_config.id}")
            await self._client.aclose()
            self._client = None

    async def _call_a2a_tool(self, tool_name: str, params: dict[str, Any]) -> TaskResult[Any]:
        """
        Call a tool using A2A protocol.

        A2A uses a tasks/send endpoint to initiate tasks. The agent responds with
        task status and may require multiple roundtrips for completion.
        """
        start_time = time.time() if self.agent_config.debug else None
        client = await self._get_client()

        headers = {"Content-Type": "application/json"}

        if self.agent_config.auth_token:
            # Support custom auth headers and types
            if self.agent_config.auth_type == "bearer":
                headers[self.agent_config.auth_header] = f"Bearer {self.agent_config.auth_token}"
            else:
                headers[self.agent_config.auth_header] = self.agent_config.auth_token

        # Construct A2A message
        message = {
            "role": "user",
            "parts": [
                {
                    "type": "text",
                    "text": self._format_tool_request(tool_name, params),
                }
            ],
        }

        # A2A uses message/send endpoint
        url = f"{self.agent_config.agent_uri}/message/send"

        request_data = {
            "message": message,
            "context_id": str(uuid4()),
        }

        debug_info = None
        if self.agent_config.debug:
            debug_request = {
                "url": url,
                "method": "POST",
                "headers": {
                    k: (
                        v
                        if k.lower() not in ("authorization", self.agent_config.auth_header.lower())
                        else "***"
                    )
                    for k, v in headers.items()
                },
                "body": request_data,
            }

        try:
            response = await client.post(
                url,
                json=request_data,
                headers=headers,
                timeout=self.agent_config.timeout,
            )
            response.raise_for_status()

            data = response.json()

            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response={"status": response.status_code, "body": data},
                    duration_ms=duration_ms,
                )

            # Parse A2A response format
            # A2A tasks have lifecycle: submitted, working, completed, failed, input-required
            task_status = data.get("task", {}).get("status")

            if task_status in ("completed", "working"):
                # Extract the result from the response message
                result_data = self._extract_result(data)

                return TaskResult[Any](
                    status=TaskStatus.COMPLETED,
                    data=result_data,
                    success=True,
                    metadata={"task_id": data.get("task", {}).get("id")},
                    debug_info=debug_info,
                )
            elif task_status == "failed":
                return TaskResult[Any](
                    status=TaskStatus.FAILED,
                    error=data.get("message", {}).get("parts", [{}])[0].get("text", "Task failed"),
                    success=False,
                    debug_info=debug_info,
                )
            else:
                # Handle other states (submitted, input-required)
                return TaskResult[Any](
                    status=TaskStatus.SUBMITTED,
                    data=data,
                    success=True,
                    metadata={"task_id": data.get("task", {}).get("id")},
                    debug_info=debug_info,
                )

        except httpx.HTTPError as e:
            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response={"error": str(e)},
                    duration_ms=duration_ms,
                )
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=str(e),
                success=False,
                debug_info=debug_info,
            )

    def _format_tool_request(self, tool_name: str, params: dict[str, Any]) -> str:
        """Format tool request as natural language for A2A."""
        # For AdCP tools, we format as a structured request
        import json

        return f"Execute tool: {tool_name}\nParameters: {json.dumps(params, indent=2)}"

    def _extract_result(self, response_data: dict[str, Any]) -> Any:
        """Extract result data from A2A response."""
        # Try to extract structured data from response
        message = response_data.get("message", {})
        parts = message.get("parts", [])

        if not parts:
            return response_data

        # Return the first part's content
        first_part = parts[0]
        if first_part.get("type") == "text":
            # Try to parse as JSON if it looks like structured data
            text = first_part.get("text", "")
            try:
                import json

                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return text

        return first_part

    # ========================================================================
    # ADCP Protocol Methods
    # ========================================================================

    async def get_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get advertising products."""
        return await self._call_a2a_tool("get_products", params)

    async def list_creative_formats(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List supported creative formats."""
        return await self._call_a2a_tool("list_creative_formats", params)

    async def sync_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync creatives."""
        return await self._call_a2a_tool("sync_creatives", params)

    async def list_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List creatives."""
        return await self._call_a2a_tool("list_creatives", params)

    async def get_media_buy_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get media buy delivery."""
        return await self._call_a2a_tool("get_media_buy_delivery", params)

    async def list_authorized_properties(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List authorized properties."""
        return await self._call_a2a_tool("list_authorized_properties", params)

    async def get_signals(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get signals."""
        return await self._call_a2a_tool("get_signals", params)

    async def activate_signal(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Activate signal."""
        return await self._call_a2a_tool("activate_signal", params)

    async def provide_performance_feedback(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Provide performance feedback."""
        return await self._call_a2a_tool("provide_performance_feedback", params)

    async def preview_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Generate preview URLs for a creative manifest."""
        return await self._call_a2a_tool("preview_creative", params)

    async def list_tools(self) -> list[str]:
        """
        List available tools from A2A agent.

        Note: A2A doesn't have a standard tools/list endpoint. Agents expose
        their capabilities through the agent card. For AdCP, we rely on the
        standard AdCP tool set.
        """
        client = await self._get_client()

        headers = {"Content-Type": "application/json"}

        if self.agent_config.auth_token:
            # Support custom auth headers and types
            if self.agent_config.auth_type == "bearer":
                headers[self.agent_config.auth_header] = f"Bearer {self.agent_config.auth_token}"
            else:
                headers[self.agent_config.auth_header] = self.agent_config.auth_token

        # Try to fetch agent card from standard A2A location
        # A2A spec uses /.well-known/agent.json for agent card
        url = f"{self.agent_config.agent_uri}/.well-known/agent.json"

        logger.debug(f"Fetching A2A agent card for {self.agent_config.id} from {url}")

        try:
            response = await client.get(url, headers=headers, timeout=self.agent_config.timeout)
            response.raise_for_status()

            data = response.json()

            # Extract skills from agent card
            skills = data.get("skills", [])
            tool_names = [skill.get("name", "") for skill in skills if skill.get("name")]

            logger.info(f"Found {len(tool_names)} tools from A2A agent {self.agent_config.id}")
            return tool_names

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                logger.error(f"Authentication failed for A2A agent {self.agent_config.id}")
                raise ADCPAuthenticationError(
                    f"Authentication failed: HTTP {status_code}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e
            else:
                logger.error(f"HTTP {status_code} error fetching agent card: {e}")
                raise ADCPConnectionError(
                    f"Failed to fetch agent card: HTTP {status_code}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e
        except httpx.TimeoutException as e:
            logger.error(f"Timeout fetching agent card for {self.agent_config.id}")
            raise ADCPTimeoutError(
                f"Timeout fetching agent card: {e}",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
                timeout=self.agent_config.timeout,
            ) from e
        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching agent card: {e}")
            raise ADCPConnectionError(
                f"Failed to fetch agent card: {e}",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
            ) from e
