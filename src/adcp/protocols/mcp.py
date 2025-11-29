from __future__ import annotations

"""MCP protocol adapter using official Python MCP SDK."""

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

# ExceptionGroup and BaseExceptionGroup are available in Python 3.11+
# In 3.11+, they're built-in types. For 3.10, we need to handle their absence.
try:
    _ExceptionGroup: type[BaseException] | None = ExceptionGroup  # type: ignore[name-defined]
    _BaseExceptionGroup: type[BaseException] | None = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:
    # Python 3.10 - ExceptionGroup doesn't exist
    _ExceptionGroup = None
    _BaseExceptionGroup = None

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

try:
    from httpx import HTTPStatusError

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    HTTPStatusError = None  # type: ignore[assignment, misc]

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

    async def _cleanup_failed_connection(self, context: str) -> None:
        """
        Clean up resources after a failed connection attempt.

        This method handles cleanup without raising exceptions to avoid
        masking the original connection error.

        Args:
            context: Description of the context for logging (e.g., "during connection attempt")
        """
        if self._exit_stack is not None:
            old_stack = self._exit_stack
            self._exit_stack = None
            self._session = None
            try:
                await old_stack.aclose()
            except BaseException as cleanup_error:
                # Handle all cleanup errors including ExceptionGroup
                # Re-raise KeyboardInterrupt and SystemExit immediately
                if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
                    raise

                if isinstance(cleanup_error, asyncio.CancelledError):
                    logger.debug(f"MCP session cleanup cancelled {context}")
                    return

                # Handle ExceptionGroup/BaseExceptionGroup from task group failures (Python 3.11+)
                # ExceptionGroup: for Exception subclasses (e.g., HTTPStatusError)
                # BaseExceptionGroup: for BaseException subclasses (e.g., CancelledError)
                # We need both because CancelledError is a BaseException, not an Exception
                is_exception_group = (
                    _ExceptionGroup is not None and isinstance(cleanup_error, _ExceptionGroup)
                ) or (
                    _BaseExceptionGroup is not None
                    and isinstance(cleanup_error, _BaseExceptionGroup)
                )

                if is_exception_group:
                    # Check if all exceptions in the group are CancelledError
                    # If so, treat the entire group as a cancellation
                    all_cancelled = all(
                        isinstance(exc, asyncio.CancelledError)
                        for exc in cleanup_error.exceptions  # type: ignore[attr-defined]
                    )
                    if all_cancelled:
                        logger.debug(f"MCP session cleanup cancelled {context}")
                        return

                    # Mixed group: skip CancelledErrors and log real errors
                    exceptions = cleanup_error.exceptions  # type: ignore[attr-defined]
                    cancelled_errors = [
                        exc for exc in exceptions if isinstance(exc, asyncio.CancelledError)
                    ]
                    cancelled_count = len(cancelled_errors)
                    if cancelled_count > 0:
                        logger.debug(
                            f"Skipping {cancelled_count} CancelledError(s) "
                            f"in mixed exception group {context}"
                        )

                    # Log each non-cancelled exception individually
                    for exc in exceptions:
                        if not isinstance(exc, asyncio.CancelledError):
                            self._log_cleanup_error(exc, context)
                else:
                    self._log_cleanup_error(cleanup_error, context)

    def _log_cleanup_error(self, exc: BaseException, context: str) -> None:
        """Log a cleanup error without raising."""
        # Check for known cleanup error patterns from httpx/anyio
        exc_str = str(exc).lower()

        # Common cleanup errors that are expected when connection fails
        is_known_cleanup_error = (
            isinstance(exc, RuntimeError)
            and ("cancel scope" in exc_str or "async context" in exc_str)
        ) or (
            # HTTP errors during cleanup (if httpx is available)
            HTTPX_AVAILABLE and HTTPStatusError is not None and isinstance(exc, HTTPStatusError)
        )

        if is_known_cleanup_error:
            # Expected cleanup errors - log at debug level without stack trace
            logger.debug(f"Ignoring expected cleanup error {context}: {exc}")
        else:
            # Truly unexpected cleanup errors - log at warning with full context
            logger.warning(f"Unexpected error during cleanup {context}: {exc}", exc_info=True)

    async def _get_session(self) -> ClientSession:
        """
        Get or create MCP client session with URL fallback handling.

        Raises:
            ADCPConnectionError: If connection to agent fails
        """
        if self._session is not None:
            return self._session  # type: ignore[no-any-return]

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

                    return self._session  # type: ignore[no-any-return]
                except BaseException as e:
                    # Catch BaseException to handle CancelledError from failed initialization
                    # Re-raise KeyboardInterrupt and SystemExit immediately
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
                    last_error = e
                    # Clean up the exit stack on failure to avoid resource leaks
                    await self._cleanup_failed_connection("during connection attempt")

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

    def _serialize_mcp_content(self, content: list[Any]) -> list[dict[str, Any]]:
        """
        Convert MCP SDK content objects to plain dicts.

        The MCP SDK returns Pydantic objects (TextContent, ImageContent, etc.)
        but the rest of the ADCP client expects protocol-agnostic dicts.
        This method handles the translation at the protocol boundary.

        Args:
            content: List of MCP content items (may be dicts or Pydantic objects)

        Returns:
            List of plain dicts representing the content
        """
        result = []
        for item in content:
            # Already a dict, pass through
            if isinstance(item, dict):
                result.append(item)
            # Pydantic v2 model with model_dump()
            elif hasattr(item, "model_dump"):
                result.append(item.model_dump())
            # Pydantic v1 model with dict()
            elif hasattr(item, "dict") and callable(item.dict):
                result.append(item.dict())
            # Fallback: try to access __dict__
            elif hasattr(item, "__dict__"):
                result.append(dict(item.__dict__))
            # Last resort: serialize as unknown type
            else:
                logger.warning(f"Unknown MCP content type: {type(item)}, serializing as string")
                result.append({"type": "unknown", "data": str(item)})
        return result

    async def _call_mcp_tool(self, tool_name: str, params: dict[str, Any]) -> TaskResult[Any]:
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

            # Check if this is an error response
            is_error = hasattr(result, "isError") and result.isError

            # Extract human-readable message from content
            message_text = None
            if hasattr(result, "content") and result.content:
                serialized_content = self._serialize_mcp_content(result.content)
                if isinstance(serialized_content, list):
                    for item in serialized_content:
                        is_text = isinstance(item, dict) and item.get("type") == "text"
                        if is_text and item.get("text"):
                            message_text = item["text"]
                            break

            # Handle error responses
            if is_error:
                # For error responses, structuredContent is optional
                # Use the error message from content as the error
                error_message = message_text or "Tool execution failed"
                if self.agent_config.debug and start_time:
                    duration_ms = (time.time() - start_time) * 1000
                    debug_info = DebugInfo(
                        request=debug_request,
                        response={
                            "error": error_message,
                            "is_error": True,
                        },
                        duration_ms=duration_ms,
                    )
                return TaskResult[Any](
                    status=TaskStatus.FAILED,
                    error=error_message,
                    success=False,
                    debug_info=debug_info,
                )

            # For successful responses, structuredContent is required
            if not hasattr(result, "structuredContent") or result.structuredContent is None:
                raise ValueError(
                    f"MCP tool {tool_name} did not return structuredContent. "
                    f"This SDK requires MCP tools to provide structured responses "
                    f"for successful calls. "
                    f"Got content: {result.content if hasattr(result, 'content') else 'none'}"
                )

            # Extract the structured data (required for success)
            data_to_return = result.structuredContent

            if self.agent_config.debug and start_time:
                duration_ms = (time.time() - start_time) * 1000
                debug_info = DebugInfo(
                    request=debug_request,
                    response={
                        "data": data_to_return,
                        "message": message_text,
                        "is_error": False,
                    },
                    duration_ms=duration_ms,
                )

            # Return both the structured data and the human-readable message
            return TaskResult[Any](
                status=TaskStatus.COMPLETED,
                data=data_to_return,
                message=message_text,
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

    # ========================================================================
    # ADCP Protocol Methods
    # ========================================================================

    async def get_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get advertising products."""
        return await self._call_mcp_tool("get_products", params)

    async def list_creative_formats(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List supported creative formats."""
        return await self._call_mcp_tool("list_creative_formats", params)

    async def sync_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync creatives."""
        return await self._call_mcp_tool("sync_creatives", params)

    async def list_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List creatives."""
        return await self._call_mcp_tool("list_creatives", params)

    async def get_media_buy_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get media buy delivery."""
        return await self._call_mcp_tool("get_media_buy_delivery", params)

    async def list_authorized_properties(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List authorized properties."""
        return await self._call_mcp_tool("list_authorized_properties", params)

    async def get_signals(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get signals."""
        return await self._call_mcp_tool("get_signals", params)

    async def activate_signal(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Activate signal."""
        return await self._call_mcp_tool("activate_signal", params)

    async def provide_performance_feedback(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Provide performance feedback."""
        return await self._call_mcp_tool("provide_performance_feedback", params)

    async def preview_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Generate preview URLs for a creative manifest."""
        return await self._call_mcp_tool("preview_creative", params)

    async def create_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create media buy."""
        return await self._call_mcp_tool("create_media_buy", params)

    async def update_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update media buy."""
        return await self._call_mcp_tool("update_media_buy", params)

    async def build_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Build creative."""
        return await self._call_mcp_tool("build_creative", params)

    async def list_tools(self) -> list[str]:
        """List available tools from MCP agent."""
        session = await self._get_session()
        result = await session.list_tools()
        return [tool.name for tool in result.tools]

    async def get_agent_info(self) -> dict[str, Any]:
        """
        Get agent information including AdCP extension metadata from MCP server.

        MCP servers may expose metadata through:
        - Server capabilities exposed during initialization
        - extensions.adcp in server info (if supported)
        - Tool list

        Returns:
            Dictionary with agent metadata
        """
        session = await self._get_session()

        # Extract basic MCP server info
        info: dict[str, Any] = {
            "name": getattr(session, "server_name", None),
            "version": getattr(session, "server_version", None),
            "protocol": "mcp",
        }

        # Get available tools
        try:
            tools_result = await session.list_tools()
            tool_names = [tool.name for tool in tools_result.tools]
            if tool_names:
                info["tools"] = tool_names
        except Exception as e:
            logger.warning(f"Failed to list tools for {self.agent_config.id}: {e}")

        # Try to extract AdCP extension metadata from server capabilities
        # MCP servers may expose this in their initialization response
        if hasattr(session, "_server_capabilities"):
            capabilities = session._server_capabilities
            if isinstance(capabilities, dict):
                extensions = capabilities.get("extensions", {})
                adcp_ext = extensions.get("adcp", {})
                if adcp_ext:
                    info["adcp_version"] = adcp_ext.get("adcp_version")
                    info["protocols_supported"] = adcp_ext.get("protocols_supported")

        logger.info(f"Retrieved agent info for {self.agent_config.id}")
        return info

    async def close(self) -> None:
        """Close the MCP session and clean up resources."""
        await self._cleanup_failed_connection("during close")
