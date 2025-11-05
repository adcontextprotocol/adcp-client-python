"""MCP protocol adapter using official Python MCP SDK."""

from typing import Any
from urllib.parse import urlparse

try:
    from mcp import ClientSession  # type: ignore[import-not-found]
    from mcp.client.sse import sse_client  # type: ignore[import-not-found]

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    ClientSession = None

from adcp.protocols.base import ProtocolAdapter
from adcp.types.core import TaskResult, TaskStatus


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
        """Get or create MCP client session with URL fallback handling."""
        if self._session is not None:
            return self._session

        # Parse the agent URI to determine transport type
        parsed = urlparse(self.agent_config.agent_uri)

        # Use SSE transport for HTTP/HTTPS endpoints
        if parsed.scheme in ("http", "https"):
            from contextlib import AsyncExitStack

            self._exit_stack = AsyncExitStack()

            # Create SSE client with authentication header
            headers = {}
            if self.agent_config.auth_token:
                # Support custom auth headers and types
                if self.agent_config.auth_type == "bearer":
                    headers[self.agent_config.auth_header] = f"Bearer {self.agent_config.auth_token}"
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
                    read, write = await self._exit_stack.enter_async_context(
                        sse_client(url, headers=headers)
                    )

                    self._session = await self._exit_stack.enter_async_context(
                        ClientSession(read, write)
                    )

                    # Initialize the session
                    await self._session.initialize()

                    return self._session
                except Exception as e:
                    last_error = e
                    # If this isn't the last URL to try, continue
                    if url != urls_to_try[-1]:
                        continue
                    # If this was the last URL, raise the error
                    raise RuntimeError(
                        f"Failed to connect to MCP agent. Tried URLs: {', '.join(urls_to_try)}. "
                        f"Last error: {str(last_error)}"
                    ) from last_error

            # This shouldn't be reached, but just in case
            raise RuntimeError(f"Failed to connect to MCP agent at {self.agent_config.agent_uri}")
        else:
            raise ValueError(f"Unsupported transport scheme: {parsed.scheme}")

    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> TaskResult[Any]:
        """Call a tool using MCP protocol."""
        try:
            session = await self._get_session()

            # Call the tool using MCP client session
            result = await session.call_tool(tool_name, params)

            # MCP tool results contain a list of content items
            # For AdCP, we expect the data in the content
            return TaskResult[Any](
                status=TaskStatus.COMPLETED,
                data=result.content,
                success=True,
            )

        except Exception as e:
            return TaskResult[Any](
                status=TaskStatus.FAILED,
                error=str(e),
                success=False,
            )

    async def list_tools(self) -> list[str]:
        """List available tools from MCP agent."""
        session = await self._get_session()
        result = await session.list_tools()
        return [tool.name for tool in result.tools]

    async def close(self) -> None:
        """Close the MCP session."""
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None
