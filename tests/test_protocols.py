"""Tests for protocol adapters."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
            result = await adapter.call_tool("get_products", {"brief": "test"})

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
            result = await adapter.call_tool("get_products", {"brief": "test"})

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
        """Test successful tool call via MCP."""
        adapter = MCPAdapter(mcp_config)

        # Mock MCP session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = [{"type": "text", "text": "Success"}]
        mock_session.call_tool.return_value = mock_result

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter.call_tool("get_products", {"brief": "test"})

            # Verify MCP protocol details - tool name and arguments
            mock_session.call_tool.assert_called_once()
            call_args = mock_session.call_tool.call_args

            # Verify tool name and params are passed as positional args
            assert call_args[0][0] == "get_products"
            assert call_args[0][1] == {"brief": "test"}

            # Verify result parsing
            assert result.success is True
            assert result.status == TaskStatus.COMPLETED
            assert result.data == [{"type": "text", "text": "Success"}]

    @pytest.mark.asyncio
    async def test_call_tool_error(self, mcp_config):
        """Test tool call error via MCP."""
        adapter = MCPAdapter(mcp_config)

        mock_session = AsyncMock()
        mock_session.call_tool.side_effect = Exception("Connection failed")

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter.call_tool("get_products", {"brief": "test"})

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
