"""Tests for protocol adapters."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a import types as pb
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

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


# Spec-string -> protobuf TaskState enum value. Tests exercise the adapter
# using the 0.3-style lowercase strings a human test author reads in the
# A2A spec; the helper translates to the 1.0 proto enum at construction
# time so both the adapter and the fixture agree on a single source of
# truth for state identity.
_STATE_TO_PB: dict[str, "pb.TaskState.ValueType"] = {
    "completed": pb.TaskState.TASK_STATE_COMPLETED,
    "failed": pb.TaskState.TASK_STATE_FAILED,
    "working": pb.TaskState.TASK_STATE_WORKING,
    "submitted": pb.TaskState.TASK_STATE_SUBMITTED,
    "input-required": pb.TaskState.TASK_STATE_INPUT_REQUIRED,
    "input_required": pb.TaskState.TASK_STATE_INPUT_REQUIRED,
    "auth-required": pb.TaskState.TASK_STATE_AUTH_REQUIRED,
    "auth_required": pb.TaskState.TASK_STATE_AUTH_REQUIRED,
    "canceled": pb.TaskState.TASK_STATE_CANCELED,
    "rejected": pb.TaskState.TASK_STATE_REJECTED,
    "unknown": pb.TaskState.TASK_STATE_UNSPECIFIED,
}


def TextPart(text: str) -> pb.Part:  # noqa: N802 (0.3 fixture shim)
    """Construct a Part carrying a ``text`` oneof (fixture shim for 1.0)."""
    return pb.Part(text=text)


def DataPart(data: dict) -> pb.Part:  # noqa: N802 (0.3 fixture shim)
    """Construct a Part carrying a ``data`` oneof (fixture shim for 1.0)."""
    value = Value()
    ParseDict(data, value)
    return pb.Part(data=value)


def create_mock_a2a_task(
    task_id: str = "task_123",
    context_id: str = "ctx_456",
    state: str = "completed",
    parts: list | None = None,
) -> pb.Task:
    """Helper to create mock A2A Task responses."""
    if parts is None:
        parts = [TextPart(text="Default message"), DataPart(data={})]

    return pb.Task(
        id=task_id,
        context_id=context_id,
        status=pb.TaskStatus(state=_STATE_TO_PB[state]),
        artifacts=[pb.Artifact(artifact_id="artifact_1", parts=parts)],
    )


def _wrap_task_in_stream(task: pb.Task) -> pb.StreamResponse:
    """Wrap a Task in a StreamResponse envelope (matches BaseClient shape)."""
    event = pb.StreamResponse()
    event.task.CopyFrom(task)
    return event


def _send_message_stream(*tasks: pb.Task):
    """Return an async iterator factory that yields tasks as StreamResponses."""
    events = [_wrap_task_in_stream(t) for t in tasks]

    async def _gen(request, *, context=None):
        for event in events:
            yield event

    return _gen


class _SendMessageSuccessAdapter:
    """Adapter that mimics the 0.3 ``SendMessageSuccessResponse`` container.

    Tests were written against the 0.3 ``send_message`` return shape; in
    1.0 the client yields ``StreamResponse`` events. This wrapper keeps
    the old test assertions readable by producing the same constructor
    signature (``result=task_proto``) while the patched
    :meth:`A2AAdapter._send_and_aggregate` unwraps it into the 1.0 shape.
    """

    def __init__(self, result: pb.Task) -> None:
        self.result = result


def SendMessageSuccessResponse(result: pb.Task) -> _SendMessageSuccessAdapter:  # noqa: N802
    # Factory named to match the 0.3 class the tests mock.
    return _SendMessageSuccessAdapter(result)


class _ClientMock:
    """Mock a2a-sdk ``Client`` whose ``send_message`` returns a
    :class:`_SendMessageSuccessAdapter` — matching the 0.3 return-value
    pattern the existing tests use.

    The 1.0 adapter drains ``client.send_message()`` as an async iterator
    via :meth:`A2AAdapter._send_and_aggregate`. To keep the tests readable
    without churning every call site, we patch ``_send_and_aggregate`` to
    shortcut straight to the mock's return value and repackage it as a
    :class:`StreamResponse`. Tests inspect ``client.send_message.call_args``
    exactly as they did against the 0.3 client.
    """

    def __init__(self) -> None:
        self.send_message = AsyncMock()


def _build_mock_client() -> _ClientMock:
    return _ClientMock()


async def _fake_send_and_aggregate(self, client, request):
    """Shortcut replacement for :meth:`A2AAdapter._send_and_aggregate`.

    Reads the mocked ``client.send_message`` return value — which in the
    tests is a ``_SendMessageSuccessAdapter`` or plain ``pb.Task`` — and
    packages it as the :class:`pb.StreamResponse` the real adapter would
    pull off the wire.
    """
    response = await client.send_message(request)
    if hasattr(response, "result"):
        task = response.result
    else:
        task = response
    event = pb.StreamResponse()
    event.task.CopyFrom(task)
    return event


@pytest.fixture(autouse=True)
def _patch_send_and_aggregate(monkeypatch):
    """Auto-apply the ``_send_and_aggregate`` shortcut for every test.

    Keeps the mock surface tests use (``client.send_message`` returns
    ``SendMessageSuccessResponse(result=task)``) wired to the 1.0 adapter
    without forcing every test to construct an async iterator by hand.
    """
    from adcp.protocols import a2a as _a2a_mod

    monkeypatch.setattr(_a2a_mod.A2AAdapter, "_send_and_aggregate", _fake_send_and_aggregate)


def create_mock_agent_card() -> pb.AgentCard:
    """Helper to create mock AgentCard."""
    return pb.AgentCard(
        name="test_agent",
        version="1.0.0",
        description="Test A2A agent",
        supported_interfaces=[
            pb.AgentInterface(
                url="https://a2a.example.com",
                protocol_binding="JSONRPC",
                protocol_version="0.3",
            )
        ],
        capabilities=pb.AgentCapabilities(streaming=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
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

        # Simulates streaming scenario with intermediate + final DataParts.
        # The final DataPart must be a spec-compliant get_products response
        # so the adapter's strict post-receive validation passes — this
        # test is exercising DataPart-merge ordering, not tolerant parsing.
        mock_task = create_mock_a2a_task(
            parts=[
                DataPart(data={"status": "processing", "progress": 50}),
                DataPart(data={"products": []}),
            ]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            assert result.success is True
            # Should use last DataPart, not first
            assert result.data == {"products": []}

    @pytest.mark.asyncio
    async def test_call_tool_multiple_artifacts_uses_last(self, a2a_config):
        """Test that last artifact is used when multiple artifacts exist (streaming scenario)."""
        adapter = A2AAdapter(a2a_config)

        # Simulates streaming with multiple artifacts. A2A spec doesn't
        # define artifact.status, so the adapter picks the last (most
        # recent) one. The last artifact's DataPart must be a spec-
        # compliant get_products response so strict post-receive
        # validation passes — empty products[] keeps the test focused
        # on artifact-ordering semantics, not schema drift.
        mock_task = pb.Task(
            id="task_123",
            context_id="ctx_456",
            status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_COMPLETED),
            artifacts=[
                pb.Artifact(
                    artifact_id="artifact_1",
                    parts=[
                        TextPart(text="Processing..."),
                        DataPart(data={"status": "working", "progress": 75}),
                    ],
                ),
                pb.Artifact(
                    artifact_id="artifact_2",
                    parts=[
                        TextPart(text="Processing complete"),
                        DataPart(data={"products": []}),
                    ],
                ),
                pb.Artifact(
                    artifact_id="artifact_3",
                    parts=[
                        TextPart(text="Final result"),
                        DataPart(data={"products": []}),
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
            assert result.data == {"products": []}
            assert result.message == "Final result"

    @pytest.mark.asyncio
    async def test_call_tool_with_response_wrapper(self, a2a_config):
        """Test handling ADK-style response wrapper {"response": {...}}."""
        adapter = A2AAdapter(a2a_config)

        # ADK wraps the actual response in {"response": {...}}. Empty
        # products[] keeps the unwrapped payload spec-compliant.
        mock_task = create_mock_a2a_task(
            parts=[
                TextPart(text="Products retrieved"),
                DataPart(data={"response": {"products": []}}),
            ]
        )
        mock_response = SendMessageSuccessResponse(result=mock_task)

        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("get_products", {"brief": "test"})

            assert result.success is True
            # Should unwrap the "response" wrapper
            assert result.data == {"products": []}
            assert result.message == "Products retrieved"

    @pytest.mark.asyncio
    async def test_call_tool_with_response_wrapper_and_metadata(self, a2a_config):
        """Test handling response wrapper with additional metadata keys."""
        adapter = A2AAdapter(a2a_config)

        # Some ADK responses have both "response" and other metadata. Keep
        # the wrapped payload spec-compliant (empty products[]) so the
        # unwrap path is the only thing under test.
        mock_task = create_mock_a2a_task(
            parts=[
                TextPart(text="Products retrieved"),
                DataPart(
                    data={
                        "response": {"products": []},
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
            assert result.data == {"products": []}

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

        # A2ACardResolver populates ``_cached_agent_card`` inside
        # ``_get_a2a_client``; when we patch that method we need to
        # pre-seed the cache so ``list_tools`` finds the card.
        adapter._cached_agent_card = pb.AgentCard(
            name="agent",
            version="1.0.0",
            skills=[
                pb.AgentSkill(id="get_products", name="get_products"),
                pb.AgentSkill(id="create_media_buy", name="create_media_buy"),
                pb.AgentSkill(id="list_creative_formats", name="list_creative_formats"),
            ],
        )

        mock_a2a_client = AsyncMock()

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            tools = await adapter.list_tools()

            # Verify tool list parsing
            assert len(tools) == 3
            assert "get_products" in tools
            assert "create_media_buy" in tools
            assert "list_creative_formats" in tools

    @pytest.mark.asyncio
    async def test_get_agent_info(self, a2a_config):
        """Test getting agent info from an A2A agent card.

        The 1.0 protobuf :class:`AgentCard` doesn't have a generic
        ``extensions`` field; AdCP metadata advertising is expected to
        move into the skills list or the agent-card documentation URL
        in a future spec bump. For now the adapter just surfaces the
        basic card fields (name/description/version/tools) and no
        longer attempts to read an ``extensions`` map.
        """
        adapter = A2AAdapter(a2a_config)

        adapter._cached_agent_card = pb.AgentCard(
            name="Test AdCP Agent",
            description="Test agent for AdCP protocol",
            version="1.0.0",
            skills=[
                pb.AgentSkill(id="get_products", name="get_products"),
                pb.AgentSkill(id="create_media_buy", name="create_media_buy"),
            ],
        )

        mock_a2a_client = AsyncMock()

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

            # Proto AgentCard has no extensions field; adcp_* keys must be absent.
            assert "adcp_version" not in info
            assert "protocols_supported" not in info

    @pytest.mark.asyncio
    async def test_get_agent_info_without_extensions(self, a2a_config):
        """Test getting agent info when AdCP extension is not present."""
        adapter = A2AAdapter(a2a_config)
        adapter._cached_agent_card = pb.AgentCard(
            name="Basic Agent",
            skills=[pb.AgentSkill(id="get_products", name="get_products")],
        )

        mock_a2a_client = AsyncMock()

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            info = await adapter.get_agent_info()

            # Verify basic info is still available
            assert info["name"] == "Basic Agent"
            assert info["protocol"] == "a2a"
            assert "get_products" in info["tools"]

            # Verify AdCP extension fields are not present
            assert "adcp_version" not in info
            assert "protocols_supported" not in info


class TestA2AContextId:
    """Tests for A2A contextId auto-retain, inject, and reset.

    Covers the multi-turn conversation story: first send carries
    context_id=None, server assigns one, adapter echoes it on every
    subsequent turn. Callers can seed the id at construction (resume /
    self-named sessions) or clear it to start a fresh conversation.
    """

    @staticmethod
    def _captured_context_id(mock_send_message: AsyncMock) -> str | None:
        """Pull the ``Message.context_id`` off the captured send call.

        The adapter wraps the outbound ``Message`` in ``MessageSendParams``
        inside a ``SendMessageRequest`` — drill through to the message.
        """
        request = mock_send_message.call_args[0][0]
        # In 1.0 the message sits directly on SendMessageRequest. Empty
        # string means "no context_id was echoed" (proto string fields
        # default to empty); expose None so assertions read naturally.
        return request.message.context_id or None

    @pytest.mark.asyncio
    async def test_first_call_sends_no_context_id_and_captures_server_assigned(self, a2a_config):
        """First turn: no context yet → server assigns → adapter stores it."""
        adapter = A2AAdapter(a2a_config)
        assert adapter.context_id is None

        mock_task = create_mock_a2a_task(context_id="server-assigned-abc")
        mock_response = SendMessageSuccessResponse(result=mock_task)
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(return_value=mock_response)

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("get_products", {})

        assert self._captured_context_id(mock_a2a_client.send_message) is None
        assert adapter.context_id == "server-assigned-abc"

    @pytest.mark.asyncio
    async def test_subsequent_call_echoes_retained_context_id(self, a2a_config):
        """Second turn: adapter sends the context_id captured on turn one."""
        adapter = A2AAdapter(a2a_config)

        first_task = create_mock_a2a_task(context_id="ctx-session-1")
        second_task = create_mock_a2a_task(context_id="ctx-session-1")
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            side_effect=[
                SendMessageSuccessResponse(result=first_task),
                SendMessageSuccessResponse(result=second_task),
            ]
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("get_products", {})
            await adapter._call_a2a_tool("create_media_buy", {})

        second_call = mock_a2a_client.send_message.call_args_list[1]
        assert second_call[0][0].message.context_id == "ctx-session-1"
        assert adapter.context_id == "ctx-session-1"

    @pytest.mark.asyncio
    async def test_set_context_id_is_used_on_next_send(self, a2a_config):
        """Seeded context_id is sent on the very next call (resume use case)."""
        adapter = A2AAdapter(a2a_config)
        adapter.set_context_id("resumed-from-redis")

        mock_task = create_mock_a2a_task(context_id="resumed-from-redis")
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=mock_task)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("get_products", {})

        assert self._captured_context_id(mock_a2a_client.send_message) == "resumed-from-redis"

    @pytest.mark.asyncio
    async def test_clearing_context_id_starts_fresh_conversation(self, a2a_config):
        """set_context_id(None) clears; next send carries no context_id."""
        adapter = A2AAdapter(a2a_config)
        adapter._context_id = "old-ctx"

        mock_task = create_mock_a2a_task(context_id="new-server-ctx")
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=mock_task)
        )

        adapter.set_context_id(None)
        assert adapter.context_id is None

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("get_products", {})

        assert self._captured_context_id(mock_a2a_client.send_message) is None
        # After response we retain whatever the server assigned.
        assert adapter.context_id == "new-server-ctx"

    @staticmethod
    def _captured_task_id(mock_send_message: AsyncMock, call_index: int = 0) -> str | None:
        """Pull the ``Message.task_id`` off a specific captured send call."""
        request = mock_send_message.call_args_list[call_index][0][0]
        return request.message.task_id or None

    @pytest.mark.asyncio
    async def test_task_id_retained_when_state_is_input_required(self, a2a_config):
        """Non-terminal state (input-required) → task_id echoed on next send
        so the server resumes the same task rather than orphaning it."""
        adapter = A2AAdapter(a2a_config)

        hitl_task = create_mock_a2a_task(
            task_id="task-hitl-1",
            context_id="ctx-abc",
            state="input-required",
            parts=[TextPart(text="Need approval")],
        )
        resume_task = create_mock_a2a_task(
            task_id="task-hitl-1",
            context_id="ctx-abc",
            state="completed",
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            side_effect=[
                SendMessageSuccessResponse(result=hitl_task),
                SendMessageSuccessResponse(result=resume_task),
            ]
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("create_media_buy", {})
            assert adapter.active_task_id == "task-hitl-1"

            await adapter._call_a2a_tool("create_media_buy", {"approval": "yes"})

        assert self._captured_task_id(mock_a2a_client.send_message, 0) is None
        assert self._captured_task_id(mock_a2a_client.send_message, 1) == "task-hitl-1"
        # Terminal state clears the pending task.
        assert adapter.active_task_id is None

    @pytest.mark.asyncio
    async def test_task_id_cleared_on_completed_state(self, a2a_config):
        """Terminal state → subsequent call starts a new task under the
        same context (task_id=None on send, context_id retained)."""
        adapter = A2AAdapter(a2a_config)

        first_task = create_mock_a2a_task(
            task_id="task-get-products",
            context_id="ctx-session",
            state="completed",
        )
        second_task = create_mock_a2a_task(
            task_id="task-create-media-buy",
            context_id="ctx-session",
            state="completed",
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            side_effect=[
                SendMessageSuccessResponse(result=first_task),
                SendMessageSuccessResponse(result=second_task),
            ]
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("get_products", {})
            assert adapter.active_task_id is None

            await adapter._call_a2a_tool("create_media_buy", {})

        assert self._captured_task_id(mock_a2a_client.send_message, 1) is None
        second_call = mock_a2a_client.send_message.call_args_list[1]
        assert second_call[0][0].message.context_id == "ctx-session"

    @pytest.mark.asyncio
    async def test_task_id_cleared_on_failed_state(self, a2a_config):
        """Failure is terminal too — pending task_id must clear."""
        adapter = A2AAdapter(a2a_config)

        failed = create_mock_a2a_task(
            task_id="task-failed",
            context_id="ctx",
            state="failed",
            parts=[TextPart(text="server error")],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=failed)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("get_products", {})

        assert adapter.active_task_id is None

    @pytest.mark.asyncio
    async def test_task_id_retained_on_working_state(self, a2a_config):
        """'working' is also non-terminal — adapter must retain task_id
        so clients polling / resuming land on the right task."""
        adapter = A2AAdapter(a2a_config)

        working = create_mock_a2a_task(
            task_id="task-in-progress",
            context_id="ctx",
            state="working",
            parts=[TextPart(text="processing...")],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=working)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("create_media_buy", {})

        assert adapter.active_task_id == "task-in-progress"

    @pytest.mark.asyncio
    async def test_set_context_id_clears_pending_task(self, a2a_config):
        """Switching context discards any in-flight task — a new
        conversation shouldn't try to resume a task from the old one."""
        adapter = A2AAdapter(a2a_config)
        adapter._context_id = "old-ctx"
        adapter._active_task_id = "old-task"

        adapter.set_context_id("new-ctx")

        assert adapter.context_id == "new-ctx"
        assert adapter.active_task_id is None

    @pytest.mark.asyncio
    async def test_server_rebinding_context_id_is_honored(self, a2a_config):
        """If the server returns a different context_id than we proposed,
        we adopt the server's value — servers are authoritative on context
        assignment even when the buyer self-named the session."""
        adapter = A2AAdapter(a2a_config)
        adapter.set_context_id("buyer-proposed")

        mock_task = create_mock_a2a_task(context_id="server-overrode")
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=mock_task)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("get_products", {})

        assert self._captured_context_id(mock_a2a_client.send_message) == "buyer-proposed"
        assert adapter.context_id == "server-overrode"

    @pytest.mark.asyncio
    async def test_task_id_retained_on_submitted_state(self, a2a_config):
        """'submitted' is non-terminal — server has accepted the task but
        not started processing. Adapter must retain task_id so the next
        call lands on the same queued task instead of stacking a duplicate.
        """
        adapter = A2AAdapter(a2a_config)

        submitted = create_mock_a2a_task(
            task_id="task-queued",
            context_id="ctx",
            state="submitted",
            parts=[TextPart(text="accepted")],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=submitted)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("create_media_buy", {})

        assert adapter.active_task_id == "task-queued"

    @pytest.mark.asyncio
    async def test_task_id_retained_on_auth_required_state(self, a2a_config):
        """'auth-required' is non-terminal — server is blocked pending
        buyer-side auth. Adapter must retain task_id so the resubmit with
        credentials lands on the same task."""
        adapter = A2AAdapter(a2a_config)

        auth_required = create_mock_a2a_task(
            task_id="task-needs-auth",
            context_id="ctx",
            state="auth-required",
            parts=[TextPart(text="authenticate and retry")],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=auth_required)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("create_media_buy", {})

        assert adapter.active_task_id == "task-needs-auth"

    @pytest.mark.asyncio
    async def test_task_id_cleared_on_canceled_state(self, a2a_config):
        """'canceled' is terminal — adapter must clear task_id so the
        next call starts fresh instead of echoing a dead task."""
        adapter = A2AAdapter(a2a_config)

        canceled = create_mock_a2a_task(
            task_id="task-canceled",
            context_id="ctx",
            state="canceled",
            parts=[TextPart(text="canceled by buyer")],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=canceled)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("create_media_buy", {})

        assert adapter.active_task_id is None

    @pytest.mark.asyncio
    async def test_task_id_cleared_on_rejected_state(self, a2a_config):
        """'rejected' is terminal — adapter must clear task_id."""
        adapter = A2AAdapter(a2a_config)

        rejected = create_mock_a2a_task(
            task_id="task-rejected",
            context_id="ctx",
            state="rejected",
            parts=[TextPart(text="rejected by agent")],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=rejected)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("create_media_buy", {})

        assert adapter.active_task_id is None

    @pytest.mark.asyncio
    async def test_task_id_cleared_on_unknown_state(self, a2a_config):
        """'unknown' is treated as terminal — don't cling to a task in
        an undefined state. Adapter should clear and warn."""
        adapter = A2AAdapter(a2a_config)

        unknown = create_mock_a2a_task(
            task_id="task-mystery",
            context_id="ctx",
            state="unknown",
            parts=[TextPart(text="???")],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=unknown)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("create_media_buy", {})

        assert adapter.active_task_id is None

    @pytest.mark.asyncio
    async def test_rejected_task_result_content(self, a2a_config):
        """TASK_STATE_REJECTED — adapter returns SUBMITTED status with 'rejected'
        in metadata and the TextPart message. REJECTED is terminal (task_id is
        cleared) but routes through the non-COMPLETED else-branch in
        _process_task_response, so data=None."""
        adapter = A2AAdapter(a2a_config)

        rejected = create_mock_a2a_task(
            task_id="task-rejected-content",
            context_id="ctx-rej",
            state="rejected",
            parts=[TextPart(text="policy violation: brand safety")],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=rejected)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("create_media_buy", {})

        assert result.status == TaskStatus.SUBMITTED
        assert result.data is None
        assert result.message == "policy violation: brand safety"
        assert result.metadata is not None
        assert result.metadata["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_rejected_task_adcp_error_datapart_not_extracted(self, a2a_config):
        """TASK_STATE_REJECTED with a DataPart carrying adcp_error — the
        DataPart is silently dropped because _process_task_response only
        calls _extract_result_from_task for COMPLETED tasks.  This test
        documents the current gap: callers cannot read structured error
        detail from a rejected task's artifact without a separate fix."""
        adapter = A2AAdapter(a2a_config)

        rejected = create_mock_a2a_task(
            task_id="task-rejected-err",
            context_id="ctx-rej-err",
            state="rejected",
            parts=[
                DataPart(data={"adcp_error": {"code": "POLICY_VIOLATION", "message": "rejected"}}),
                TextPart(text="rejected by server"),
            ],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=rejected)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("create_media_buy", {})

        assert result.status == TaskStatus.SUBMITTED
        # Gap: adcp_error DataPart is not extracted for non-COMPLETED states.
        # A future fix should surface structured error detail here.
        assert result.data is None
        assert result.message == "rejected by server"
        assert result.metadata is not None
        assert result.metadata["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_auth_required_task_result_content(self, a2a_config):
        """TASK_STATE_AUTH_REQUIRED — non-terminal state. Adapter returns
        SUBMITTED status with 'auth-required' in metadata and the challenge
        message from the TextPart.  Callers should surface this to trigger
        an auth flow before re-submitting."""
        adapter = A2AAdapter(a2a_config)

        auth_task = create_mock_a2a_task(
            task_id="task-auth-content",
            context_id="ctx-auth",
            state="auth-required",
            parts=[TextPart(text="OAuth required: redirect to https://auth.example.com")],
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=auth_task)
        )

        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            result = await adapter._call_a2a_tool("create_media_buy", {})

        assert result.status == TaskStatus.SUBMITTED
        assert result.data is None
        assert result.message == "OAuth required: redirect to https://auth.example.com"
        assert result.metadata is not None
        assert result.metadata["status"] == "auth-required"
        assert result.metadata["task_id"] == "task-auth-content"
        assert result.metadata["context_id"] == "ctx-auth"

    @pytest.mark.asyncio
    async def test_state_not_committed_when_post_processing_raises(self, a2a_config):
        """If _process_task_response raises, the adapter must NOT advance
        its state — otherwise a retry echoes a task_id the caller never
        saw a response for. Uses IdempotencyConflictError because it's in
        the adapter's allow-list to propagate (most exceptions get caught
        and converted to TaskResult(FAILED), but typed idempotency errors
        bubble out). Either way the invariant is the same: pre-call state
        must survive a raise from post-processing.
        """
        from adcp.exceptions import IdempotencyConflictError

        adapter = A2AAdapter(a2a_config)
        adapter._context_id = "prior-ctx"
        adapter._active_task_id = "prior-task"

        response_task = create_mock_a2a_task(
            task_id="server-new-task",
            context_id="server-new-ctx",
            state="input-required",
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=response_task)
        )

        boom = IdempotencyConflictError("create_media_buy", errors=[])
        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            with patch.object(adapter, "_process_task_response", side_effect=boom):
                with pytest.raises(IdempotencyConflictError):
                    await adapter._call_a2a_tool("create_media_buy", {})

        assert adapter.context_id == "prior-ctx"
        assert adapter.active_task_id == "prior-task"

    @pytest.mark.asyncio
    async def test_state_not_committed_when_exception_converts_to_failed(self, a2a_config):
        """Mirror of the IdempotencyConflictError test for the generic
        exception path. Most exceptions in post-processing get caught by
        the broad ``except Exception`` at the end of _call_a2a_tool and
        converted to ``TaskResult(FAILED)`` — the caller never sees the
        exception, but the adapter still must not have advanced state,
        or the next call echoes a task_id the caller never saw succeed.
        """
        adapter = A2AAdapter(a2a_config)
        adapter._context_id = "prior-ctx"
        adapter._active_task_id = "prior-task"

        response_task = create_mock_a2a_task(
            task_id="server-new-task",
            context_id="server-new-ctx",
            state="input-required",
        )
        mock_a2a_client = AsyncMock()
        mock_a2a_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=response_task)
        )

        boom = RuntimeError("post-processing blew up")
        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            with patch.object(adapter, "_process_task_response", side_effect=boom):
                result = await adapter._call_a2a_tool("create_media_buy", {})

        assert result.status == TaskStatus.FAILED
        assert adapter.context_id == "prior-ctx"
        assert adapter.active_task_id == "prior-task"


class TestA2AProtocolVersions:
    """Tests for the ``a2a_protocol_versions`` introspection property."""

    def test_returns_none_before_card_fetch(self, a2a_config):
        """Until an operation fetches the AgentCard, the list is unknown —
        not empty. Callers need to distinguish 'not yet known' from
        'peer advertises nothing'."""
        adapter = A2AAdapter(a2a_config)
        assert adapter.a2a_protocol_versions is None

    def test_sorted_from_cached_card(self, a2a_config):
        """After a card is cached the property returns the sorted set
        of advertised ``protocol_version`` strings."""
        adapter = A2AAdapter(a2a_config)
        card = pb.AgentCard(
            name="dual",
            supported_interfaces=[
                pb.AgentInterface(
                    url="http://x", protocol_binding="JSONRPC", protocol_version="1.0"
                ),
                pb.AgentInterface(
                    url="http://x", protocol_binding="JSONRPC", protocol_version="0.3"
                ),
            ],
        )
        adapter._cached_agent_card = card
        assert adapter.a2a_protocol_versions == ["0.3", "1.0"]

    def test_empty_list_when_peer_advertises_none(self, a2a_config):
        """Peer advertises a card but no ``supported_interfaces`` — list
        is empty (not None), distinct from 'card not yet fetched'."""
        adapter = A2AAdapter(a2a_config)
        adapter._cached_agent_card = pb.AgentCard(name="bare")
        assert adapter.a2a_protocol_versions == []

    def test_client_property_returns_none_on_non_a2a(self, mcp_config):
        """The ADCPClient-level wrapper returns ``None`` on MCP
        clients so generic code can probe without branching."""
        from adcp.client import ADCPClient

        client = ADCPClient(mcp_config)
        assert client.a2a_protocol_versions is None

    def test_client_property_forwards_adapter_state(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config)
        assert isinstance(client.adapter, A2AAdapter)
        # Seed the cache directly; the property reads straight through.
        client.adapter._cached_agent_card = pb.AgentCard(
            name="x",
            supported_interfaces=[
                pb.AgentInterface(
                    url="http://x", protocol_binding="JSONRPC", protocol_version="0.3"
                ),
            ],
        )
        assert client.a2a_protocol_versions == ["0.3"]

    def test_force_a2a_version_rejects_on_non_a2a(self, mcp_config):
        """The pin only makes sense for A2A; MCP callers shouldn't be
        able to pass it and have it silently no-op."""
        from adcp.client import ADCPClient

        with pytest.raises(TypeError, match="only supported for A2A"):
            ADCPClient(mcp_config, force_a2a_version="0.3")

    def test_force_a2a_version_plumbs_to_adapter(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config, force_a2a_version="0.3")
        assert isinstance(client.adapter, A2AAdapter)
        assert client.adapter._force_a2a_version == "0.3"

    def test_force_a2a_version_defaults_to_none(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config)
        assert isinstance(client.adapter, A2AAdapter)
        assert client.adapter._force_a2a_version is None


class TestADCPClientContextId:
    """Tests for the ADCPClient-level contextId surface."""

    def test_constructor_seeds_context_id_on_a2a_client(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config, context_id="seeded-ctx")
        assert client.context_id == "seeded-ctx"
        assert isinstance(client.adapter, A2AAdapter)
        assert client.adapter.context_id == "seeded-ctx"

    def test_context_id_property_defaults_to_none(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config)
        assert client.context_id is None

    def test_reset_context_clears(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config, context_id="will-clear")
        client.reset_context()
        assert client.context_id is None

    def test_reset_context_with_new_id(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config)
        client.reset_context("fresh-named-session")
        assert client.context_id == "fresh-named-session"

    def test_constructor_rejects_context_id_on_non_a2a(self, mcp_config):
        from adcp.client import ADCPClient

        with pytest.raises(TypeError, match="only supported for A2A"):
            ADCPClient(mcp_config, context_id="nope")

    def test_reset_context_rejects_on_non_a2a(self, mcp_config):
        from adcp.client import ADCPClient

        client = ADCPClient(mcp_config)
        with pytest.raises(TypeError, match="only supported for A2A"):
            client.reset_context("anything")

    def test_context_id_property_returns_none_on_non_a2a(self, mcp_config):
        from adcp.client import ADCPClient

        client = ADCPClient(mcp_config)
        assert client.context_id is None

    def test_active_task_id_property_exposes_adapter_state(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config)
        assert client.active_task_id is None
        assert isinstance(client.adapter, A2AAdapter)
        client.adapter._active_task_id = "task-mid-flight"
        assert client.active_task_id == "task-mid-flight"

    def test_active_task_id_returns_none_on_non_a2a(self, mcp_config):
        from adcp.client import ADCPClient

        client = ADCPClient(mcp_config)
        assert client.active_task_id is None

    def test_empty_string_context_id_is_not_seeded(self, a2a_config):
        """``context_id=""`` from ``os.getenv(...) or ""`` patterns must
        not silently seed an empty id on the wire."""
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config, context_id="")
        assert client.context_id is None

    def test_empty_string_context_id_ok_on_non_a2a(self, mcp_config):
        """Empty context_id should be treated as 'not provided' on any
        protocol — no TypeError on MCP, same as passing None."""
        from adcp.client import ADCPClient

        client = ADCPClient(mcp_config, context_id="")
        assert client.context_id is None

    def test_checkpoint_returns_all_fields(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient(a2a_config, context_id="ctx-123")
        assert isinstance(client.adapter, A2AAdapter)
        client.adapter._active_task_id = "task-in-flight"

        state = client.checkpoint()
        assert state == {
            "agent_id": a2a_config.id,
            "context_id": "ctx-123",
            "active_task_id": "task-in-flight",
        }

    def test_checkpoint_on_non_a2a_carries_agent_id_and_nones(self, mcp_config):
        from adcp.client import ADCPClient

        client = ADCPClient(mcp_config)
        assert client.checkpoint() == {
            "agent_id": mcp_config.id,
            "context_id": None,
            "active_task_id": None,
        }

    def test_from_checkpoint_restores_both_ids(self, a2a_config):
        """Full resume requires both ids — persisting only context_id
        orphans the pending task server-side."""
        from adcp.client import ADCPClient

        state = {
            "agent_id": a2a_config.id,
            "context_id": "ctx-resume",
            "active_task_id": "task-hitl",
        }
        client = ADCPClient.from_checkpoint(a2a_config, state)

        assert client.context_id == "ctx-resume"
        assert client.active_task_id == "task-hitl"

    def test_from_checkpoint_with_empty_state_is_fresh_client(self, a2a_config):
        from adcp.client import ADCPClient

        client = ADCPClient.from_checkpoint(a2a_config, {})
        assert client.context_id is None
        assert client.active_task_id is None

    def test_from_checkpoint_roundtrips(self, a2a_config):
        from adcp.client import ADCPClient

        original = ADCPClient(a2a_config, context_id="ctx-orig")
        assert isinstance(original.adapter, A2AAdapter)
        original.adapter._active_task_id = "task-orig"

        restored = ADCPClient.from_checkpoint(a2a_config, original.checkpoint())
        assert restored.context_id == original.context_id
        assert restored.active_task_id == original.active_task_id

    def test_from_checkpoint_rejects_mismatched_agent_id(self, a2a_config):
        """A checkpoint minted for Agent A must not be restored onto
        Agent B — that would leak Agent A's opaque session ids to a
        different vendor on the next message."""
        from adcp.client import ADCPClient

        state = {
            "agent_id": "other-agent",
            "context_id": "ctx-from-other",
            "active_task_id": "task-from-other",
        }
        with pytest.raises(ValueError, match="minted for agent"):
            ADCPClient.from_checkpoint(a2a_config, state)

    def test_from_checkpoint_raises_on_non_a2a_with_active_task(self, mcp_config):
        """Silently dropping active_task_id on a non-A2A restore would
        mask bugs — raise instead."""
        from adcp.client import ADCPClient

        state = {
            "agent_id": mcp_config.id,
            "context_id": None,
            "active_task_id": "task-x",
        }
        with pytest.raises(TypeError, match="active_task_id"):
            ADCPClient.from_checkpoint(mcp_config, state)

    def test_from_checkpoint_empty_on_mcp_is_fine(self, mcp_config):
        """Empty/None checkpoint must round-trip on any protocol."""
        from adcp.client import ADCPClient

        state = {
            "agent_id": mcp_config.id,
            "context_id": None,
            "active_task_id": None,
        }
        client = ADCPClient.from_checkpoint(mcp_config, state)
        assert client.context_id is None
        assert client.active_task_id is None


class TestMCPAdapter:
    """Tests for MCP protocol adapter."""

    @pytest.mark.asyncio
    async def test_call_tool_success(self, mcp_config):
        """Test successful tool call via MCP with proper structuredContent."""
        adapter = MCPAdapter(mcp_config)

        # Mock MCP session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        # Mock MCP result with structuredContent (required for AdCP). Empty
        # products[] keeps the payload spec-compliant without having to
        # enumerate every required product field.
        mock_result.content = [{"type": "text", "text": "Success"}]
        mock_result.structuredContent = {"products": []}
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
            assert result.data == {"products": []}

    @pytest.mark.asyncio
    async def test_call_tool_with_structured_content(self, mcp_config):
        """Test successful tool call via MCP with structuredContent field."""
        adapter = MCPAdapter(mcp_config)

        # Mock MCP session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        # Mock MCP result with structuredContent (preferred over content).
        # Empty formats[] is spec-compliant without enumerating every
        # required Format field.
        mock_result.content = [{"type": "text", "text": "Found 0 creative formats"}]
        mock_result.structuredContent = {"formats": []}
        mock_result.isError = False
        mock_session.call_tool.return_value = mock_result

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter._call_mcp_tool("list_creative_formats", {})

            # Verify result uses structuredContent, not content array
            assert result.success is True
            assert result.status == TaskStatus.COMPLETED
            assert result.data == {"formats": []}
            # Verify message extraction from content array
            assert result.message == "Found 0 creative formats"

    @pytest.mark.asyncio
    async def test_call_tool_no_structured_adcp_data(self, mcp_config):
        """Tool call fails when neither structuredContent nor text-JSON yields AdCP data.

        Per AdCP spec §MCP Response Extraction: a response with plain text (not
        JSON) and no structuredContent returns no structured data; the SDK
        reports this as a FAILED TaskResult with a diagnostic message.
        """
        adapter = MCPAdapter(mcp_config)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        # Response has plain text content, no JSON, no structuredContent.
        mock_result.content = [{"type": "text", "text": "Success"}]
        mock_result.structuredContent = None
        mock_result.isError = False
        mock_session.call_tool.return_value = mock_result

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter._call_mcp_tool("get_products", {"brief": "test"})

            assert result.success is False
            assert result.status == TaskStatus.FAILED
            assert "no structured AdCP data" in result.error

    @pytest.mark.asyncio
    async def test_call_tool_text_json_fallback(self, mcp_config):
        """Text-only JSON content is extracted per spec when structuredContent absent."""
        adapter = MCPAdapter(mcp_config)

        mock_session = AsyncMock()
        mock_result = MagicMock()
        # Reference-agent shape: JSON inside TextContent, no structuredContent.
        mock_result.content = [{"type": "text", "text": '{"status":"completed","products":[]}'}]
        mock_result.structuredContent = None
        mock_result.isError = False
        mock_session.call_tool.return_value = mock_result

        with patch.object(adapter, "_get_session", return_value=mock_session):
            result = await adapter._call_mcp_tool("get_products", {"brief": "test"})

            assert result.success is True
            assert result.status == TaskStatus.COMPLETED
            assert result.data == {"status": "completed", "products": []}

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
