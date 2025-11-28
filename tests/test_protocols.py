"""Tests for protocol adapters."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import (
    AgentCard,
    Artifact,
    DataPart,
    Message,
    SendMessageSuccessResponse,
    Task,
    TaskState,
    TaskStatus as A2ATaskStatus,
    TextPart,
)

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


def create_mock_a2a_task(
    task_id: str = "task_123",
    context_id: str = "ctx_456",
    state: str = "completed",
    parts: list = None,
) -> Task:
    """Helper to create mock A2A Task responses."""
    if parts is None:
        parts = [TextPart(text="Default message"), DataPart(data={})]

    return Task(
        id=task_id,
        context_id=context_id,
        status=A2ATaskStatus(state=state),
        artifacts=[Artifact(artifact_id="artifact_1", parts=parts)],
    )


def create_mock_agent_card() -> AgentCard:
    """Helper to create mock AgentCard."""
    return AgentCard(
        name="test_agent",
        version="1.0.0",
        description="Test A2A agent",
        url="https://a2a.example.com",
        capabilities={"streaming": False},
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        skills=[],
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
        """Test successful tool call via A2A using canonical response format."""
        adapter = A2AAdapter(a2a_config)

        # Create A2A SDK Task response
        mock_task = create_mock_a2a_task(
            parts=[
                TextPart(text="Found 3 products matching criteria"),
                DataPart(data={"result": "success", "products": []}),
            ]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        # Mock the A2A client
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            # Verify the A2A client was called
            mock_a2a_client.send_message.assert_called_once()

            # Verify result parsing
            assert result.success is True
            assert result.status == TaskStatus.COMPLETED
            assert result.data == {"result": "success", "products": []}
            assert result.message == "Found 3 products matching criteria"
            assert result.metadata["task_id"] == "task_123"
            assert result.metadata["context_id"] == "ctx_456"

    @pytest.mark.asyncio
    async def test_call_tool_failure(self, a2a_config):
        """Test failed tool call via A2A using canonical response format."""
        adapter = A2AAdapter(a2a_config)

        # Protocol-level failure uses state: "failed" with TextPart for error message
        mock_task = create_mock_a2a_task(
            state="failed", parts=[TextPart(text="Authentication failed")]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            # Verify failure handling
            assert result.success is False
            assert result.status == TaskStatus.FAILED
            assert result.error == "Authentication failed"

    @pytest.mark.asyncio
    async def test_call_tool_with_task_errors(self, a2a_config):
        """Test completed task with task-level errors (not protocol failure)."""
        adapter = A2AAdapter(a2a_config)

        # Task completes but has partial failures in errors array
        mock_task = create_mock_a2a_task(
            parts=[
                TextPart(text="Media buy created with warnings"),
                DataPart(
                    data={
                        "media_buy_id": "mb_123",
                        "errors": [
                            {
                                "code": "APPROVAL_REQUIRED",
                                "message": "Budget exceeds threshold",
                                "severity": "warning",
                            }
                        ],
                    }
                ),
            ]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("create_media_buy", {"budget": 10000})

            assert result.status == TaskStatus.COMPLETED
            assert result.success is False  # Has task-level errors
            assert result.data["media_buy_id"] == "mb_123"
            assert len(result.data["errors"]) == 1

    @pytest.mark.asyncio
    async def test_call_tool_multiple_data_parts(self, a2a_config):
        """Test that last DataPart is authoritative when multiple exist."""
        adapter = A2AAdapter(a2a_config)

        # Simulates streaming scenario with intermediate + final DataParts
        mock_task = create_mock_a2a_task(
            parts=[
                DataPart(data={"status": "processing", "progress": 50}),
                DataPart(data={"status": "completed", "result": "final"}),
            ]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            assert result.success is True
            # Should use last DataPart, not first
            assert result.data == {"status": "completed", "result": "final"}

    @pytest.mark.asyncio
    async def test_call_tool_multiple_artifacts_uses_last(self, a2a_config):
        """Test that last artifact is used when multiple artifacts exist (streaming scenario)."""
        adapter = A2AAdapter(a2a_config)

        # Simulates streaming with multiple artifacts
        # A2A spec doesn't define artifact.status, so we use the last (most recent) one
        mock_task = Task(
            id="task_123",
            context_id="ctx_456",
            status=A2ATaskStatus(state="completed"),
            artifacts=[
                Artifact(
                    artifact_id="artifact_1",
                    parts=[
                        TextPart(text="Processing..."),
                        DataPart(data={"status": "working", "progress": 75}),
                    ],
                ),
                Artifact(
                    artifact_id="artifact_2",
                    parts=[
                        TextPart(text="Processing complete"),
                        DataPart(data={"status": "completed", "products": ["prod1"]}),
                    ],
                ),
                Artifact(
                    artifact_id="artifact_3",
                    parts=[
                        TextPart(text="Final result"),
                        DataPart(data={"status": "completed", "products": ["prod2"]}),
                    ],
                ),
            ],
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            assert result.success is True
            # Should use last artifact (most recent)
            assert result.data == {"status": "completed", "products": ["prod2"]}
            assert result.message == "Final result"

    @pytest.mark.asyncio
    async def test_call_tool_with_response_wrapper(self, a2a_config):
        """Test handling ADK-style response wrapper {"response": {...}}."""
        adapter = A2AAdapter(a2a_config)

        # ADK wraps the actual response in {"response": {...}}
        mock_task = create_mock_a2a_task(
            parts=[
                TextPart(text="Products retrieved"),
                DataPart(data={"response": {"products": [{"id": "prod1", "name": "TV Ad"}]}}),
            ]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            assert result.success is True
            # Should unwrap the "response" wrapper
            assert result.data == {"products": [{"id": "prod1", "name": "TV Ad"}]}
            assert result.message == "Products retrieved"

    @pytest.mark.asyncio
    async def test_call_tool_with_response_wrapper_and_metadata(self, a2a_config):
        """Test handling response wrapper with additional metadata keys."""
        adapter = A2AAdapter(a2a_config)

        # Some ADK responses have both "response" and other metadata
        mock_task = create_mock_a2a_task(
            parts=[
                TextPart(text="Products retrieved"),
                DataPart(
                    data={
                        "response": {"products": [{"id": "prod1"}]},
                        "metadata": {"cache_hit": True},
                    }
                ),
            ]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            assert result.success is True
            # Should still unwrap and return the "response" content
            assert result.data == {"products": [{"id": "prod1"}]}

    @pytest.mark.asyncio
    async def test_interim_response_working(self, a2a_config):
        """Test handling interim 'working' response without structured data."""
        adapter = A2AAdapter(a2a_config)

        mock_task = create_mock_a2a_task(
            state="working", parts=[TextPart(text="Processing your request...")]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            assert result.success is True
            assert result.status == TaskStatus.SUBMITTED
            # Interim responses don't need structured data
            assert result.data is None
            assert result.message == "Processing your request..."
            assert result.metadata["status"] == "working"

    @pytest.mark.asyncio
    async def test_interim_response_submitted(self, a2a_config):
        """Test handling interim 'submitted' response without structured data."""
        adapter = A2AAdapter(a2a_config)

        mock_task = create_mock_a2a_task(
            state="submitted", parts=[TextPart(text="Task submitted successfully")]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            assert result.success is True
            assert result.status == TaskStatus.SUBMITTED
            # Interim responses don't need structured data
            assert result.data is None
            assert result.message == "Task submitted successfully"
            assert result.metadata["status"] == "submitted"

    @pytest.mark.asyncio
    async def test_list_tools(self, a2a_config):
        """Test listing tools via A2A agent card."""
        adapter = A2AAdapter(a2a_config)

        # Use MagicMock to allow setting arbitrary attributes
        mock_agent_card = MagicMock()
        # Create skill mocks with .name attribute (not using name= parameter)
        skill1 = MagicMock()
        skill1.name = "get_products"
        skill2 = MagicMock()
        skill2.name = "create_media_buy"
        skill3 = MagicMock()
        skill3.name = "list_creative_formats"
        mock_agent_card.skills = [skill1, skill2, skill3]

        mock_a2a_client = AsyncMock()
        mock_a2a_client.get_card = AsyncMock(return_value=mock_agent_card)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            tools = await adapter.list_tools()

            # Verify get_card was called
            mock_a2a_client.get_card.assert_called_once()

            # Verify tool list parsing
            assert len(tools) == 3
            assert "get_products" in tools
            assert "create_media_buy" in tools
            assert "list_creative_formats" in tools

    @pytest.mark.asyncio
    async def test_get_agent_info(self, a2a_config):
        """Test getting agent info including AdCP extension metadata."""
        adapter = A2AAdapter(a2a_config)

        # Use MagicMock to allow setting arbitrary attributes including extensions
        mock_agent_card = MagicMock()
        mock_agent_card.name = "Test AdCP Agent"
        mock_agent_card.description = "Test agent for AdCP protocol"
        mock_agent_card.version = "1.0.0"
        # Create skill mocks with .name attribute (not using name= parameter)
        skill1 = MagicMock()
        skill1.name = "get_products"
        skill2 = MagicMock()
        skill2.name = "create_media_buy"
        mock_agent_card.skills = [skill1, skill2]
        mock_agent_card.extensions = {
            "adcp": {"adcp_version": "2.4.0", "protocols_supported": ["media_buy", "creative"]}
        }

        mock_a2a_client = AsyncMock()
        mock_a2a_client.get_card = AsyncMock(return_value=mock_agent_card)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            info = await adapter.get_agent_info()

            # Verify basic agent info
            assert info["name"] == "Test AdCP Agent"
            assert info["description"] == "Test agent for AdCP protocol"
            assert info["version"] == "1.0.0"
            assert info["protocol"] == "a2a"

            # Verify tools list
            assert len(info["tools"]) == 2
            assert "get_products" in info["tools"]
            assert "create_media_buy" in info["tools"]

            # Verify AdCP extension metadata
            assert info["adcp_version"] == "2.4.0"
            assert info["protocols_supported"] == ["media_buy", "creative"]

    @pytest.mark.asyncio
    async def test_get_agent_info_without_extensions(self, a2a_config):
        """Test getting agent info when AdCP extension is not present."""
        adapter = A2AAdapter(a2a_config)

        # Use MagicMock to allow setting arbitrary attributes
        mock_agent_card = MagicMock()
        mock_agent_card.name = "Basic Agent"
        # Create skill mock with .name attribute (not using name= parameter)
        skill1 = MagicMock()
        skill1.name = "get_products"
        mock_agent_card.skills = [skill1]
        mock_agent_card.extensions = None

        mock_a2a_client = AsyncMock()
        mock_a2a_client.get_card = AsyncMock(return_value=mock_agent_card)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            info = await adapter.get_agent_info()

            # Verify basic info is still available
            assert info["name"] == "Basic Agent"
            assert info["protocol"] == "a2a"
            assert "get_products" in info["tools"]

            # Verify AdCP extension fields are not present
            assert "adcp_version" not in info
            assert "protocols_supported" not in info


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

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        sys.version_info < (3, 11),
        reason="ExceptionGroup is only available in Python 3.11+",
    )
    async def test_cleanup_handles_exception_group(self, mcp_config):
        """Test that cleanup handles ExceptionGroup from task group failures."""
        from contextlib import AsyncExitStack

        import httpx

        adapter = MCPAdapter(mcp_config)

        # Create an ExceptionGroup like what anyio task groups raise
        http_error = httpx.HTTPStatusError(
            "Client error '405 Method Not Allowed' for url 'https://test.example.com'",
            request=MagicMock(),
            response=MagicMock(status_code=405),
        )
        exception_group = ExceptionGroup(  # type: ignore[name-defined]  # noqa: F821
            "unhandled errors in a TaskGroup", [http_error]
        )

        # Mock exit stack that raises ExceptionGroup on cleanup
        mock_exit_stack = AsyncMock(spec=AsyncExitStack)
        mock_exit_stack.aclose = AsyncMock(side_effect=exception_group)
        adapter._exit_stack = mock_exit_stack

        # cleanup should not raise despite the ExceptionGroup
        await adapter._cleanup_failed_connection("during test")

        # Verify cleanup was attempted and state is clean
        mock_exit_stack.aclose.assert_called_once()
        assert adapter._exit_stack is None
        assert adapter._session is None

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        sys.version_info < (3, 11),
        reason="ExceptionGroup is only available in Python 3.11+",
    )
    async def test_cleanup_handles_exception_group_with_cancelled_error(self, mcp_config):
        """Test that cleanup handles ExceptionGroup containing CancelledError."""
        import asyncio
        from contextlib import AsyncExitStack

        adapter = MCPAdapter(mcp_config)

        # Create a BaseExceptionGroup with CancelledError like what happens in the real error
        # In Python 3.11+, BaseExceptionGroup is used for BaseException subclasses
        cancelled_error = asyncio.CancelledError("Cancelled via cancel scope")
        if sys.version_info >= (3, 11):
            exception_group = BaseExceptionGroup(  # type: ignore[name-defined]  # noqa: F821
                "unhandled errors in a TaskGroup", [cancelled_error]
            )
        else:
            # Should not reach here due to skipif, but handle gracefully
            return

        # Mock exit stack that raises BaseExceptionGroup on cleanup
        mock_exit_stack = AsyncMock(spec=AsyncExitStack)
        mock_exit_stack.aclose = AsyncMock(side_effect=exception_group)
        adapter._exit_stack = mock_exit_stack

        # cleanup should not raise despite the BaseExceptionGroup with CancelledError
        await adapter._cleanup_failed_connection("during test")

        # Verify cleanup was attempted and state is clean
        mock_exit_stack.aclose.assert_called_once()
        assert adapter._exit_stack is None
        assert adapter._session is None

