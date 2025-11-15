"""Tests for protocol adapters."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adcp.protocols.a2a import A2AAdapter
from adcp.protocols.mcp import MCPAdapter
from adcp.types.core import AgentConfig, Protocol, TaskStatus


@pytest.fixture
def a2a_config():
    """Create A2A agent config for testing."""
    return AgentConfig(
        id="test_a2a_agent",
        agent_uri="https://a2a.example.com",
        protocol=Protocol.A2A,
        auth_token="test_token",
    )


@pytest.fixture
def mcp_config():
    """Create MCP agent config for testing."""
    return AgentConfig(
        id="test_mcp_agent",
        agent_uri="https://mcp.example.com",
        protocol=Protocol.MCP,
        auth_token="test_token",
    )


class TestA2AAdapter:
    """Tests for A2A protocol adapter."""

    @pytest.mark.asyncio
    async def test_call_tool_success(self, a2a_config):
        """Test successful tool call via A2A."""
        adapter = A2AAdapter(a2a_config)

        mock_response_data = {
            "task": {"id": "task_123", "status": "completed"},
            "message": {
                "role": "assistant",
                "parts": [{"type": "text", "text": '{"result": "success"}'}],
            },
        }

        mock_client = AsyncMock()
        mock_http_response = MagicMock()
        mock_http_response.json = MagicMock(return_value=mock_response_data)
        mock_http_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_http_response)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            # Verify the adapter logic - check HTTP request details
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args

            # Verify URL includes /message/send endpoint
            assert call_args[0][0] == "https://a2a.example.com/message/send"

            # Verify headers include auth token (default auth_type is "token", not "bearer")
            headers = call_args[1]["headers"]
            assert "x-adcp-auth" in headers
            assert headers["x-adcp-auth"] == "test_token"

            # Verify request body structure matches A2A spec
            json_body = call_args[1]["json"]
            assert "message" in json_body
            assert json_body["message"]["role"] == "user"
            assert "parts" in json_body["message"]
            assert "context_id" in json_body

            # Verify result parsing
            assert result.success is True
            assert result.status == TaskStatus.COMPLETED
            assert result.data == {"result": "success"}

    @pytest.mark.asyncio
    async def test_call_tool_failure(self, a2a_config):
        """Test failed tool call via A2A."""
        adapter = A2AAdapter(a2a_config)

        mock_response_data = {
            "task": {"id": "task_123", "status": "failed"},
            "message": {"role": "assistant", "parts": [{"type": "text", "text": "Error occurred"}]},
        }

        mock_client = AsyncMock()
        mock_http_response = MagicMock()
        mock_http_response.json = MagicMock(return_value=mock_response_data)
        mock_http_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_http_response)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            # Verify HTTP request was made with correct parameters
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert call_args[0][0] == "https://a2a.example.com/message/send"
            assert call_args[1]["headers"]["x-adcp-auth"] == "test_token"
            assert "message" in call_args[1]["json"]

            # Verify failure handling
            assert result.success is False
            assert result.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_list_tools(self, a2a_config):
        """Test listing tools via A2A agent card."""
        adapter = A2AAdapter(a2a_config)

        mock_agent_card = {
            "skills": [
                {"name": "get_products"},
                {"name": "create_media_buy"},
                {"name": "list_creative_formats"},
            ]
        }

        mock_client = AsyncMock()
        mock_http_response = MagicMock()
        mock_http_response.json = MagicMock(return_value=mock_agent_card)
        mock_http_response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_http_response)

        with patch.object(adapter, "_get_client", return_value=mock_client):
            tools = await adapter.list_tools()

            # Verify agent card URL construction (A2A spec uses agent.json)
            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            expected_url = "https://a2a.example.com/.well-known/agent.json"
            assert call_args[0][0] == expected_url

            # Verify auth headers are included (default auth_type is "token")
            headers = call_args[1]["headers"]
            assert "x-adcp-auth" in headers
            assert headers["x-adcp-auth"] == "test_token"

            # Verify tool list parsing
            assert len(tools) == 3
            assert "get_products" in tools
            assert "create_media_buy" in tools
            assert "list_creative_formats" in tools


class TestMCPAdapter:
    """Tests for MCP protocol adapter."""

    @pytest.mark.asyncio
    async def test_call_tool_success(self, mcp_config):
        """Test successful tool call via MCP with proper structuredContent."""
        adapter = MCPAdapter(mcp_config)

        # Mock MCP session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        # Mock MCP result with structuredContent (required for AdCP)
        mock_result.content = [{"type": "text", "text": "Success"}]
        mock_result.structuredContent = {"products": [{"id": "prod1"}]}
        mock_result.isError = False
        mock_session.call_tool.return_value = mock_result

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter._call_mcp_tool("get_products", {"brief": "test"})

            # Verify MCP protocol details - tool name and arguments
            mock_session.call_tool.assert_called_once()
            call_args = mock_session.call_tool.call_args

            # Verify tool name and params are passed as positional args
            assert call_args[0][0] == "get_products"
            assert call_args[0][1] == {"brief": "test"}

            # Verify result uses structuredContent
            assert result.success is True
            assert result.status == TaskStatus.COMPLETED
            assert result.data == {"products": [{"id": "prod1"}]}

    @pytest.mark.asyncio
    async def test_call_tool_with_structured_content(self, mcp_config):
        """Test successful tool call via MCP with structuredContent field."""
        adapter = MCPAdapter(mcp_config)

        # Mock MCP session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        # Mock MCP result with structuredContent (preferred over content)
        mock_result.content = [{"type": "text", "text": "Found 42 creative formats"}]
        mock_result.structuredContent = {"formats": [{"id": "format1"}, {"id": "format2"}]}
        mock_result.isError = False
        mock_session.call_tool.return_value = mock_result

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter._call_mcp_tool("list_creative_formats", {})

            # Verify result uses structuredContent, not content array
            assert result.success is True
            assert result.status == TaskStatus.COMPLETED
            assert result.data == {"formats": [{"id": "format1"}, {"id": "format2"}]}
            # Verify message extraction from content array
            assert result.message == "Found 42 creative formats"

    @pytest.mark.asyncio
    async def test_call_tool_missing_structured_content(self, mcp_config):
        """Test tool call fails when structuredContent is missing on successful response."""
        adapter = MCPAdapter(mcp_config)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        # Mock MCP result WITHOUT structuredContent and isError=False (invalid)
        mock_result.content = [{"type": "text", "text": "Success"}]
        mock_result.structuredContent = None
        mock_result.isError = False
        mock_session.call_tool.return_value = mock_result

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter._call_mcp_tool("get_products", {"brief": "test"})

            # Verify error handling for missing structuredContent on success
            assert result.success is False
            assert result.status == TaskStatus.FAILED
            assert "did not return structuredContent" in result.error

    @pytest.mark.asyncio
    async def test_call_tool_error_without_structured_content(self, mcp_config):
        """Test tool call handles error responses without structuredContent gracefully."""
        adapter = MCPAdapter(mcp_config)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        # Mock MCP error response WITHOUT structuredContent (valid for errors)
        mock_result.content = [
            {"type": "text", "text": "brand_manifest must provide brand information"}
        ]
        mock_result.structuredContent = None
        mock_result.isError = True
        mock_session.call_tool.return_value = mock_result

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter._call_mcp_tool("get_products", {"brief": "test"})

            # Verify error is handled gracefully
            assert result.success is False
            assert result.status == TaskStatus.FAILED
            assert result.error == "brand_manifest must provide brand information"

    @pytest.mark.asyncio
    async def test_call_tool_error(self, mcp_config):
        """Test tool call error via MCP."""
        adapter = MCPAdapter(mcp_config)

        mock_session = AsyncMock()
        mock_session.call_tool.side_effect = Exception("Connection failed")

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter._call_mcp_tool("get_products", {"brief": "test"})

            # Verify call_tool was attempted with correct parameters (positional args)
            mock_session.call_tool.assert_called_once()
            call_args = mock_session.call_tool.call_args
            assert call_args[0][0] == "get_products"
            assert call_args[0][1] == {"brief": "test"}

            # Verify error handling
            assert result.success is False
            assert result.status == TaskStatus.FAILED
            assert "Connection failed" in result.error

    @pytest.mark.asyncio
    async def test_list_tools(self, mcp_config):
        """Test listing tools via MCP."""
        adapter = MCPAdapter(mcp_config)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_tool1 = MagicMock()
        mock_tool1.name = "get_products"
        mock_tool2 = MagicMock()
        mock_tool2.name = "create_media_buy"
        mock_result.tools = [mock_tool1, mock_tool2]
        mock_session.list_tools.return_value = mock_result

        with patch.object(adapter, "_get_session", return_value=mock_session):
            tools = await adapter.list_tools()

            # Verify list_tools was called on the session
            mock_session.list_tools.assert_called_once()

            # Verify adapter correctly extracts tool names from MCP response
            assert len(tools) == 2
            assert "get_products" in tools
            assert "create_media_buy" in tools

    @pytest.mark.asyncio
    async def test_close_session(self, mcp_config):
        """Test closing MCP session."""
        adapter = MCPAdapter(mcp_config)

        mock_exit_stack = AsyncMock()
        adapter._exit_stack = mock_exit_stack

        await adapter.close()

        mock_exit_stack.aclose.assert_called_once()
        assert adapter._exit_stack is None
        assert adapter._session is None

    def test_serialize_mcp_content_with_dicts(self, mcp_config):
        """Test serializing MCP content that's already dicts."""
        adapter = MCPAdapter(mcp_config)

        content = [
            {"type": "text", "text": "Hello"},
            {"type": "resource", "uri": "file://test.txt"},
        ]

        result = adapter._serialize_mcp_content(content)

        assert result == content  # Pass through unchanged
        assert len(result) == 2

    def test_serialize_mcp_content_with_pydantic_v2(self, mcp_config):
        """Test serializing MCP content with Pydantic v2 objects."""
        from pydantic import BaseModel

        adapter = MCPAdapter(mcp_config)

        class MockTextContent(BaseModel):
            type: str
            text: str

        content = [
            MockTextContent(type="text", text="Pydantic v2"),
        ]

        result = adapter._serialize_mcp_content(content)

        assert len(result) == 1
        assert result[0] == {"type": "text", "text": "Pydantic v2"}
        assert isinstance(result[0], dict)

    def test_serialize_mcp_content_mixed(self, mcp_config):
        """Test serializing mixed MCP content (dicts and Pydantic objects)."""
        from pydantic import BaseModel

        adapter = MCPAdapter(mcp_config)

        class MockTextContent(BaseModel):
            type: str
            text: str

        content = [
            {"type": "text", "text": "Plain dict"},
            MockTextContent(type="text", text="Pydantic object"),
        ]

        result = adapter._serialize_mcp_content(content)

        assert len(result) == 2
        assert result[0] == {"type": "text", "text": "Plain dict"}
        assert result[1] == {"type": "text", "text": "Pydantic object"}
        assert all(isinstance(item, dict) for item in result)

    @pytest.mark.asyncio
    async def test_connection_failure_cleanup(self, mcp_config):
        """Test that connection failures clean up resources properly."""
        from contextlib import AsyncExitStack

        import httpcore

        adapter = MCPAdapter(mcp_config)

        # Mock the exit stack to simulate connection failure
        mock_exit_stack = AsyncMock(spec=AsyncExitStack)
        mock_exit_stack.enter_async_context = AsyncMock(
            side_effect=httpcore.ConnectError("Connection refused")
        )
        # Simulate the anyio cleanup error that occurs in production
        mock_exit_stack.aclose = AsyncMock(
            side_effect=RuntimeError("Attempted to exit cancel scope in a different task")
        )

        with patch("adcp.protocols.mcp.AsyncExitStack", return_value=mock_exit_stack):
            # Try to get session - should fail but cleanup gracefully
            try:
                await adapter._get_session()
            except Exception:
                pass  # Expected to fail

            # Verify cleanup was attempted
            mock_exit_stack.aclose.assert_called()

        # Verify adapter state is clean after failed connection
        assert adapter._exit_stack is None
        assert adapter._session is None

    @pytest.mark.asyncio
    async def test_close_with_runtime_error(self, mcp_config):
        """Test that close() handles RuntimeError from anyio cleanup gracefully."""
        from contextlib import AsyncExitStack

        adapter = MCPAdapter(mcp_config)

        # Set up a mock exit stack that raises RuntimeError on cleanup
        mock_exit_stack = AsyncMock(spec=AsyncExitStack)
        mock_exit_stack.aclose = AsyncMock(
            side_effect=RuntimeError("Attempted to exit cancel scope in a different task")
        )
        adapter._exit_stack = mock_exit_stack

        # close() should not raise despite the RuntimeError
        await adapter.close()

        # Verify cleanup was attempted and state is clean
        mock_exit_stack.aclose.assert_called_once()
        assert adapter._exit_stack is None
        assert adapter._session is None

    @pytest.mark.asyncio
    async def test_close_with_cancellation(self, mcp_config):
        """Test that close() handles CancelledError during cleanup."""
        import asyncio
        from contextlib import AsyncExitStack

        adapter = MCPAdapter(mcp_config)

        # Set up a mock exit stack that raises CancelledError
        mock_exit_stack = AsyncMock(spec=AsyncExitStack)
        mock_exit_stack.aclose = AsyncMock(side_effect=asyncio.CancelledError())
        adapter._exit_stack = mock_exit_stack

        # close() should not raise despite the CancelledError
        await adapter.close()

        # Verify cleanup was attempted and state is clean
        mock_exit_stack.aclose.assert_called_once()
        assert adapter._exit_stack is None
        assert adapter._session is None

    @pytest.mark.asyncio
    async def test_multiple_connection_attempts_with_cleanup_failures(self, mcp_config):
        """Test that multiple connection attempts handle cleanup failures properly."""
        from contextlib import AsyncExitStack

        adapter = MCPAdapter(mcp_config)

        # Mock exit stack creation and cleanup
        call_count = 0

        def create_mock_exit_stack():
            nonlocal call_count
            call_count += 1
            mock_stack = AsyncMock(spec=AsyncExitStack)
            mock_stack.enter_async_context = AsyncMock(
                side_effect=ConnectionError(f"Connection attempt {call_count} failed")
            )
            mock_stack.aclose = AsyncMock(
                side_effect=RuntimeError("Cancel scope error") if call_count == 1 else None
            )
            return mock_stack

        with patch("adcp.protocols.mcp.AsyncExitStack", side_effect=create_mock_exit_stack):
            # Try to get session - should fail after trying all URLs
            try:
                await adapter._get_session()
            except Exception:
                pass  # Expected to fail

        # Verify multiple connection attempts were made (original URL + /mcp suffix)
        assert call_count >= 1

        # Verify adapter state is clean after all failed attempts
        assert adapter._exit_stack is None
        assert adapter._session is None
