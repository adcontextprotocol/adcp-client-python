from __future__ import annotations

"""A2A protocol adapter using the official a2a-sdk client."""

import logging
import time
from typing import Any
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, A2AClient
from a2a.types import (
    DataPart,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TextPart,
)

from adcp.exceptions import (
    ADCPAuthenticationError,
    ADCPConnectionError,
    ADCPTimeoutError,
)
from adcp.protocols.base import ProtocolAdapter
from adcp.types.core import AgentConfig, DebugInfo, TaskResult, TaskStatus

logger = logging.getLogger(__name__)


class A2AAdapter(ProtocolAdapter):
    """Adapter for A2A protocol using official a2a-sdk client."""

    def __init__(self, agent_config: AgentConfig):
        """Initialize A2A adapter with official A2A client."""
        super().__init__(agent_config)
        self._httpx_client: httpx.AsyncClient | None = None
        self._a2a_client: A2AClient | None = None

    async def _get_httpx_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client with connection pooling."""
        if self._httpx_client is None:
            limits = httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0,
            )

            headers = {}
            if self.agent_config.auth_token:
                if self.agent_config.auth_type == "bearer":
                    headers["Authorization"] = f"Bearer {self.agent_config.auth_token}"
                else:
                    headers[self.agent_config.auth_header] = self.agent_config.auth_token

            self._httpx_client = httpx.AsyncClient(
                limits=limits,
                headers=headers,
                timeout=self.agent_config.timeout,
            )
            logger.debug(
                f"Created HTTP client with connection pooling for agent {self.agent_config.id}"
            )
        return self._httpx_client

    async def _get_a2a_client(self) -> A2AClient:
        """Get or create the A2A client."""
        if self._a2a_client is None:
            httpx_client = await self._get_httpx_client()

            # Use A2ACardResolver to fetch the agent card
            card_resolver = A2ACardResolver(
                httpx_client=httpx_client,
                base_url=self.agent_config.agent_uri,
            )

            try:
                agent_card = await card_resolver.get_agent_card()
                logger.debug(f"Fetched agent card for {self.agent_config.id}")
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                if status_code in (401, 403):
                    raise ADCPAuthenticationError(
                        f"Authentication failed: HTTP {status_code}",
                        agent_id=self.agent_config.id,
                        agent_uri=self.agent_config.agent_uri,
                    ) from e
                else:
                    raise ADCPConnectionError(
                        f"Failed to fetch agent card: HTTP {status_code}",
                        agent_id=self.agent_config.id,
                        agent_uri=self.agent_config.agent_uri,
                    ) from e
            except httpx.TimeoutException as e:
                raise ADCPTimeoutError(
                    f"Timeout fetching agent card: {e}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                    timeout=self.agent_config.timeout,
                ) from e
            except httpx.HTTPError as e:
                raise ADCPConnectionError(
                    f"Failed to fetch agent card: {e}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e

            self._a2a_client = A2AClient(
                httpx_client=httpx_client,
                agent_card=agent_card,
            )
            logger.debug(f"Created A2A client for agent {self.agent_config.id}")

        return self._a2a_client

    async def close(self) -> None:
        """Close the HTTP client and clean up resources."""
        if self._httpx_client is not None:
            logger.debug(f"Closing A2A adapter client for agent {self.agent_config.id}")
            await self._httpx_client.aclose()
            self._httpx_client = None
            self._a2a_client = None

    async def _call_a2a_tool(
        self, tool_name: str, params: dict[str, Any], use_explicit_skill: bool = True
    ) -> TaskResult[Any]:
        """
        Call a tool using A2A protocol via official a2a-sdk client.

        Args:
            tool_name: Name of the skill/tool to invoke
            params: Parameters to pass to the skill
            use_explicit_skill: If True, use explicit skill invocation (deterministic).
                               If False, use natural language (flexible).

        The default is explicit skill invocation for predictable, repeatable behavior.
        See: https://docs.adcontextprotocol.org/docs/protocols/a2a-guide
        """
        start_time = time.time() if self.agent_config.debug else None
        a2a_client = await self._get_a2a_client()

        # Build A2A message
        message_id = str(uuid4())

        if use_explicit_skill:
            # Explicit skill invocation (deterministic)
            # Use DataPart with skill name and parameters
            data_part = DataPart(
                data={
                    "skill": tool_name,
                    "parameters": params,
                }
            )
            message = Message(
                message_id=message_id,
                role=Role.user,
                parts=[Part(root=data_part)],
            )
        else:
            # Natural language invocation (flexible)
            # Agent interprets intent from text
            text_part = TextPart(text=self._format_tool_request(tool_name, params))
            message = Message(
                message_id=message_id,
                role=Role.user,
                parts=[Part(root=text_part)],
            )

        # Build request params
        params_obj = MessageSendParams(message=message)

        # Build request
        request = SendMessageRequest(
            id=str(uuid4()),
            params=params_obj,
        )

        debug_info = None
        debug_request: dict[str, Any] = {}
        if self.agent_config.debug:
            debug_request = {
                "method": "send_message",
                "message_id": message_id,
                "tool": tool_name,
                "params": params,
            }

        try:
            # Use official A2A client
            sdk_response = await a2a_client.send_message(request)

            # SendMessageResponse is a RootModel union - unwrap it to get the actual response
            # (either JSONRPCSuccessResponse or JSONRPCErrorResponse)
            response = sdk_response.root if hasattr(sdk_response, "root") else sdk_response

            # Handle JSON-RPC error response
            if hasattr(response, "error"):
                error_msg = response.error.message if response.error.message else "Unknown error"
                if self.agent_config.debug and start_time:
                    duration_ms = (time.time() - start_time) * 1000
                    debug_info = DebugInfo(
                        request=debug_request,
                        response={"error": response.error.model_dump()},
                        duration_ms=duration_ms,
                    )
                return TaskResult[Any](
                    status=TaskStatus.FAILED,
                    error=error_msg,
                    success=False,
                    debug_info=debug_info,
                )

            # Handle success response
            if hasattr(response, "result"):
                result = response.result

                if self.agent_config.debug and start_time:
                    duration_ms = (time.time() - start_time) * 1000
                    debug_info = DebugInfo(
                        request=debug_request,
                        response={"result": result.model_dump()},
                        duration_ms=duration_ms,
                    )

                # Result can be either Task or Message
                if isinstance(result, Task):
                    return self._process_task_response(result, debug_info)
                else:
                    # Message response (shouldn't happen for send_message, but handle it)
                    agent_id = self.agent_config.id
                    logger.warning(f"Received Message instead of Task from A2A agent {agent_id}")
                    return TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=None,
                        message="Received message response",
                        success=True,
                        debug_info=debug_info,
                    )

            # Shouldn't reach here
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error="Invalid response from A2A client",
                success=False,
                debug_info=debug_info,
            )

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response={"error": str(e), "status_code": status_code},
                    duration_ms=duration_ms,
                )

            if status_code in (401, 403):
                error_msg = f"Authentication failed: HTTP {status_code}"
            else:
                error_msg = f"HTTP {status_code} error: {e}"

            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=error_msg,
                success=False,
                debug_info=debug_info,
            )
        except httpx.TimeoutException as e:
            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response={"error": str(e)},
                    duration_ms=duration_ms,
                )
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=f"Timeout: {e}",
                success=False,
                debug_info=debug_info,
            )
        except Exception as e:
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

    def _process_task_response(self, task: Task, debug_info: DebugInfo | None) -> TaskResult[Any]:
        """Process a Task response from A2A into our TaskResult format."""
        task_state = task.status.state

        if task_state == "completed":
            # Extract the result from the artifacts array
            result_data = self._extract_result_from_task(task)

            # Check for task-level errors in the payload
            errors = result_data.get("errors", []) if isinstance(result_data, dict) else []
            has_errors = bool(errors)

            return TaskResult[Any](
                status=TaskStatus.COMPLETED,
                data=result_data,
                message=self._extract_text_from_task(task),
                success=not has_errors,
                metadata={
                    "task_id": task.id,
                    "context_id": task.context_id,
                },
                debug_info=debug_info,
            )
        elif task_state == "failed":
            # Protocol-level failure - extract error message from TextPart
            error_msg = self._extract_text_from_task(task) or "Task failed"
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=error_msg,
                success=False,
                debug_info=debug_info,
            )
        else:
            # Handle all interim states (submitted, working, input-required, etc.)
            return TaskResult[Any](
                status=TaskStatus.SUBMITTED,
                data=None,  # Interim responses may not have structured AdCP content
                message=self._extract_text_from_task(task),
                success=True,
                metadata={
                    "task_id": task.id,
                    "context_id": task.context_id,
                    "status": task_state,
                },
                debug_info=debug_info,
            )

    def _format_tool_request(self, tool_name: str, params: dict[str, Any]) -> str:
        """Format tool request as natural language for A2A."""
        import json

        return f"Execute tool: {tool_name}\nParameters: {json.dumps(params, indent=2)}"

    def _extract_result_from_task(self, task: Task) -> Any:
        """
        Extract result data from A2A Task following canonical format.

        Per A2A response spec:
        - Responses MUST include at least one DataPart (kind: "data")
        - When multiple DataParts exist in an artifact, the last one is authoritative
        - When multiple artifacts exist, use the last one (most recent in streaming)
        - DataParts contain structured AdCP payload
        """
        if not task.artifacts:
            logger.warning("A2A Task missing required artifacts array")
            return {}

        # Use last artifact (most recent in streaming scenarios)
        target_artifact = task.artifacts[-1]

        if not target_artifact.parts:
            logger.warning("A2A Task artifact has no parts")
            return {}

        # Find all DataParts (kind: "data")
        # Note: Parts are wrapped in a Part union type, access via .root
        from a2a.types import DataPart

        data_parts = [p.root for p in target_artifact.parts if isinstance(p.root, DataPart)]

        if not data_parts:
            logger.warning("A2A Task missing required DataPart (kind: 'data')")
            return {}

        # Use last DataPart as authoritative (handles streaming scenarios within an artifact)
        last_data_part = data_parts[-1]
        data = last_data_part.data

        # Some A2A implementations (e.g., ADK) wrap the response in {"response": {...}}
        # Unwrap it to get the actual AdCP payload if present
        if isinstance(data, dict) and "response" in data:
            # If response is the only key, unwrap completely
            if len(data) == 1:
                return data["response"]
            # If there are other keys alongside response, prefer the wrapped content
            return data["response"]

        return data

    def _extract_text_from_task(self, task: Task) -> str | None:
        """Extract human-readable message from TextPart if present."""
        if not task.artifacts:
            return None

        # Use last artifact (most recent in streaming scenarios)
        target_artifact = task.artifacts[-1]

        # Find TextPart (kind: "text")
        # Note: Parts are wrapped in a Part union type, access via .root
        for part in target_artifact.parts:
            if isinstance(part.root, TextPart):
                return part.root.text

        return None

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

    async def create_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create media buy."""
        return await self._call_a2a_tool("create_media_buy", params)

    async def update_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update media buy."""
        return await self._call_a2a_tool("update_media_buy", params)

    async def build_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Build creative."""
        return await self._call_a2a_tool("build_creative", params)

    async def list_tools(self) -> list[str]:
        """
        List available tools from A2A agent.

        Uses A2A client which already fetched the agent card during initialization.
        """
        # Get the A2A client (which already fetched the agent card)
        a2a_client = await self._get_a2a_client()

        # Fetch the agent card using the official method
        try:
            agent_card = await a2a_client.get_card()

            # Extract skills from agent card
            tool_names = [skill.name for skill in agent_card.skills if skill.name]

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

    async def get_agent_info(self) -> dict[str, Any]:
        """
        Get agent information including AdCP extension metadata from A2A agent card.

        Uses A2A client's get_card() method to fetch the agent card and extracts:
        - Basic agent info (name, description, version)
        - AdCP extension (extensions.adcp.adcp_version, extensions.adcp.protocols_supported)
        - Available skills/tools

        Returns:
            Dictionary with agent metadata
        """
        # Get the A2A client (which already fetched the agent card)
        a2a_client = await self._get_a2a_client()

        logger.debug(f"Fetching A2A agent info for {self.agent_config.id}")

        try:
            agent_card = await a2a_client.get_card()

            # Extract basic info
            info: dict[str, Any] = {
                "name": agent_card.name,
                "description": agent_card.description,
                "version": agent_card.version,
                "protocol": "a2a",
            }

            # Extract skills/tools
            tool_names = [skill.name for skill in agent_card.skills if skill.name]
            if tool_names:
                info["tools"] = tool_names

            # Extract AdCP extension metadata
            # Note: AgentCard type doesn't include extensions in the SDK,
            # but it may be present at runtime
            extensions = getattr(agent_card, "extensions", None)
            if extensions:
                adcp_ext = extensions.get("adcp")
                if adcp_ext:
                    info["adcp_version"] = adcp_ext.get("adcp_version")
                    info["protocols_supported"] = adcp_ext.get("protocols_supported")

            logger.info(f"Retrieved agent info for {self.agent_config.id}")
            return info

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                raise ADCPAuthenticationError(
                    f"Authentication failed: HTTP {status_code}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e
            else:
                raise ADCPConnectionError(
                    f"Failed to fetch agent card: HTTP {status_code}",
                    agent_id=self.agent_config.id,
                    agent_uri=self.agent_config.agent_uri,
                ) from e
        except httpx.TimeoutException as e:
            raise ADCPTimeoutError(
                f"Timeout fetching agent card: {e}",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
                timeout=self.agent_config.timeout,
            ) from e
        except httpx.HTTPError as e:
            raise ADCPConnectionError(
                f"Failed to fetch agent card: {e}",
                agent_id=self.agent_config.id,
                agent_uri=self.agent_config.agent_uri,
            ) from e
