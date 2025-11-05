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

        mock_response = {
            "task": {"id": "task_123", "status": "completed"},
            "message": {
                "role": "assistant",
                "parts": [{"type": "text", "text": '{"result": "success"}'}],
            },
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            mock_http_response = MagicMock()
            mock_http_response.json.return_value = mock_response
            mock_http_response.raise_for_status = MagicMock()

            mock_client.post.return_value = mock_http_response

            result = await adapter.call_tool("get_products", {"brief": "test"})

            assert result.success is True
            assert result.status == TaskStatus.COMPLETED
            assert result.data == {"result": "success"}

    @pytest.mark.asyncio
    async def test_call_tool_failure(self, a2a_config):
        """Test failed tool call via A2A."""
        adapter = A2AAdapter(a2a_config)

        mock_response = {
            "task": {"id": "task_123", "status": "failed"},
            "message": {"role": "assistant", "parts": [{"type": "text", "text": "Error occurred"}]},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            mock_http_response = MagicMock()
            mock_http_response.json.return_value = mock_response
            mock_http_response.raise_for_status = MagicMock()

            mock_client.post.return_value = mock_http_response

            result = await adapter.call_tool("get_products", {"brief": "test"})

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

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            mock_http_response = MagicMock()
            mock_http_response.json.return_value = mock_agent_card
            mock_http_response.raise_for_status = MagicMock()

            mock_client.get.return_value = mock_http_response

            tools = await adapter.list_tools()

            assert len(tools) == 3
            assert "get_products" in tools
            assert "create_media_buy" in tools


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
