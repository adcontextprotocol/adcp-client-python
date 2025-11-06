from __future__ import annotations

"""MCP protocol adapter using official Python MCP SDK."""

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp import ClientSession

try:
    from mcp import ClientSession as _ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from adcp.exceptions import ADCPConnectionError, ADCPTimeoutError
from adcp.protocols.base import ProtocolAdapter
from adcp.types.core import DebugInfo, TaskResult, TaskStatus


class MCPAdapter(ProtocolAdapter):
    """Adapter for MCP protocol using official Python MCP SDK."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        if not MCP_AVAILABLE:
            raise ImportError(
                "MCP SDK not installed. Install with: pip install mcp (requires Python 3.10+)"
            )
        self._session: Any = None
        self._exit_stack: Any = None

    async def _get_session(self) -> ClientSession:
        """
        Get or create MCP client session with URL fallback handling.

        Raises:
            ADCPConnectionError: If connection to agent fails
        """
        if self._session is not None:
            return self._session  # type: ignore[return-value]

        logger.debug(f"Creating MCP session for agent {self.agent_config.id}")

        # Parse the agent URI to determine transport type
        parsed = urlparse(self.agent_config.agent_uri)

        # Use SSE transport for HTTP/HTTPS endpoints
        if parsed.scheme in ("http", "https"):
            self._exit_stack = AsyncExitStack()

            # Create SSE client with authentication header
            headers = {}
            if self.agent_config.auth_token:
                # Support custom auth headers and types
                if self.agent_config.auth_type == "bearer":
                    headers[self.agent_config.auth_header] = (
                        f"Bearer {self.agent_config.auth_token}"
                    )
                else:
                    headers[self.agent_config.auth_header] = self.agent_config.auth_token

            # Try the user's exact URL first
            urls_to_try = [self.agent_config.agent_uri]

            # If URL doesn't end with /mcp, also try with /mcp suffix
            if not self.agent_config.agent_uri.rstrip("/").endswith("/mcp"):
                base_uri = self.agent_config.agent_uri.rstrip("/")
                urls_to_try.append(f"{base_uri}/mcp")

            last_error = None
            for url in urls_to_try:
                try:
                    # Choose transport based on configuration
                    if self.agent_config.mcp_transport == "streamable_http":
                        # Use streamable HTTP transport (newer, bidirectional)
                        read, write, _get_session_id = await self._exit_stack.enter_async_context(
                            streamablehttp_client(
                                url, headers=headers, timeout=self.agent_config.timeout
                            )
                        )
                    else:
                        # Use SSE transport (legacy, but widely supported)
                        read, write = await self._exit_stack.enter_async_context(
                            sse_client(url, headers=headers)
                        )

                    self._session = await self._exit_stack.enter_async_context(
                        _ClientSession(read, write)
                    )

                    # Initialize the session
                    await self._session.initialize()

                    logger.info(
                        f"Connected to MCP agent {self.agent_config.id} at {url} "
                        f"using {self.agent_config.mcp_transport} transport"
                    )
                    if url != self.agent_config.agent_uri:
                        logger.info(
                            f"Note: Connected using fallback URL {url} "
                            f"(configured: {self.agent_config.agent_uri})"
                        )

                    return self._session  # type: ignore[return-value]
                except Exception as e:
                    last_error = e
                    # Clean up the exit stack on failure to avoid async scope issues
                    if self._exit_stack is not None:
                        old_stack = self._exit_stack
                        self._exit_stack = None  # Clear immediately to prevent reuse
                        self._session = None
                        try:
                            await old_stack.aclose()
                        except asyncio.CancelledError:
                            # Expected during shutdown
                            pass
                        except RuntimeError as cleanup_error:
                            # Known MCP SDK async cleanup issue
                            if (
                                "async context" in str(cleanup_error).lower()
                                or "cancel scope" in str(cleanup_error).lower()
                            ):
                                logger.debug(
                                    "Ignoring MCP SDK async context error during cleanup: "
                                    f"{cleanup_error}"
                                )
                            else:
                                logger.warning(
                                    f"Unexpected RuntimeError during cleanup: {cleanup_error}"
                                )
                        except Exception as cleanup_error:
                            # Unexpected cleanup errors should be logged
                            logger.warning(
                                f"Unexpected error during cleanup: {cleanup_error}", exc_info=True
                            )

                    # If this isn't the last URL to try, create a new exit stack and continue
                    if url != urls_to_try[-1]:
                        logger.debug(f"Retrying with next URL after error: {last_error}")
                        self._exit_stack = AsyncExitStack()
                        continue
                    # If this was the last URL, raise the error
                    logger.error(
                        f"Failed to connect to MCP agent {self.agent_config.id} using "
                        f"{self.agent_config.mcp_transport} transport. "
                        f"Tried URLs: {', '.join(urls_to_try)}"
                    )

                    # Classify error type for better exception handling
                    error_str = str(last_error).lower()
                    if "401" in error_str or "403" in error_str or "unauthorized" in error_str:
                        from adcp.exceptions import ADCPAuthenticationError

                        raise ADCPAuthenticationError(
                            f"Authentication failed: {last_error}",
                            agent_id=self.agent_config.id,
                            agent_uri=self.agent_config.agent_uri,
                        ) from last_error
                    elif "timeout" in error_str:
                        raise ADCPTimeoutError(
                            f"Connection timeout: {last_error}",
                            agent_id=self.agent_config.id,
                            agent_uri=self.agent_config.agent_uri,
                            timeout=self.agent_config.timeout,
                        ) from last_error
                    else:
                        raise ADCPConnectionError(
                            f"Failed to connect: {last_error}",
                            agent_id=self.agent_config.id,
                            agent_uri=self.agent_config.agent_uri,
                        ) from last_error

            # This shouldn't be reached, but just in case
            raise RuntimeError(f"Failed to connect to MCP agent at {self.agent_config.agent_uri}")
        else:
            raise ValueError(f"Unsupported transport scheme: {parsed.scheme}")

    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> TaskResult[Any]:
        """Call a tool using MCP protocol."""
        start_time = time.time() if self.agent_config.debug else None
        debug_info = None

        try:
            session = await self._get_session()

            if self.agent_config.debug:
                debug_request = {
                    "protocol": "MCP",
                    "tool": tool_name,
                    "params": params,
                    "transport": self.agent_config.mcp_transport,
                }

            # Call the tool using MCP client session
            result = await session.call_tool(tool_name, params)

            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response={
                        "content": result.content,
                        "is_error": result.isError if hasattr(result, "isError") else False,
                    },
                    duration_ms=duration_ms,
                )

            # MCP tool results contain a list of content items
            # For AdCP, we expect the data in the content
            return TaskResult[Any](
                status=TaskStatus.COMPLETED,
                data=result.content,
                success=True,
                debug_info=debug_info,
            )

        except Exception as e:
            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request if self.agent_config.debug else {},
                    response={"error": str(e)},
                    duration_ms=duration_ms,
                )
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=str(e),
                success=False,
                debug_info=debug_info,
            )

    async def list_tools(self) -> list[str]:
        """List available tools from MCP agent."""
        session = await self._get_session()
        result = await session.list_tools()
        return [tool.name for tool in result.tools]

    async def close(self) -> None:
        """Close the MCP session."""
        if self._exit_stack is not None:
            old_stack = self._exit_stack
            self._exit_stack = None
            self._session = None
            try:
                await old_stack.aclose()
            except (asyncio.CancelledError, RuntimeError):
                # Cleanup errors during shutdown are expected
                pass
            except Exception as e:
                logger.debug(f"Error during MCP session cleanup: {e}")
