"""Tests for ADCPClient."""

import pytest

from adcp import ADCPClient, ADCPMultiAgentClient
from adcp.types import AgentConfig, Protocol


def test_agent_config_creation():
    """Test creating agent configuration."""
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    assert config.id == "test_agent"
    assert config.agent_uri == "https://test.example.com"
    assert config.protocol == Protocol.A2A


def test_client_creation():
    """Test creating ADCP client."""
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    assert client.agent_config == config


def test_multi_agent_client_creation():
    """Test creating multi-agent client."""
    agents = [
        AgentConfig(
            id="agent1",
            agent_uri="https://agent1.example.com",
            protocol=Protocol.A2A,
        ),
        AgentConfig(
            id="agent2",
            agent_uri="https://agent2.example.com",
            protocol=Protocol.MCP,
        ),
    ]

    client = ADCPMultiAgentClient(agents)

    assert len(client.agents) == 2
    assert "agent1" in client.agent_ids
    assert "agent2" in client.agent_ids


def test_webhook_url_generation():
    """Test webhook URL generation."""
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(
        config,
        webhook_url_template="https://myapp.com/webhook/{task_type}/{agent_id}/{operation_id}",
    )

    url = client.get_webhook_url("get_products", "op_123")

    assert url == "https://myapp.com/webhook/get_products/test_agent/op_123"


@pytest.mark.asyncio
async def test_get_products():
    """Test get_products method with mock adapter."""
    from unittest.mock import patch

    from adcp.types._generated import GetProductsRequest, GetProductsResponse
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    # Mock both the adapter method and parsing
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"products": []},  # Simple data for adapter
        success=True,
    )

    mock_parsed_result = TaskResult[GetProductsResponse](
        status=TaskStatus.COMPLETED,
        data=GetProductsResponse(products=[]),  # Properly typed result
        success=True,
    )

    with (
        patch.object(client.adapter, "get_products", return_value=mock_raw_result) as mock_get,
        patch.object(
            client.adapter, "_parse_response", return_value=mock_parsed_result
        ) as mock_parse,
    ):
        request = GetProductsRequest(brief="test campaign")
        result = await client.get_products(request)

        # Verify adapter method was called
        mock_get.assert_called_once_with({"brief": "test campaign"})
        # Verify parsing was called with correct type
        mock_parse.assert_called_once_with(mock_raw_result, GetProductsResponse)
        # Verify final result
        assert result.success is True
        assert result.status == TaskStatus.COMPLETED
        assert isinstance(result.data, GetProductsResponse)


@pytest.mark.asyncio
async def test_all_client_methods():
    """Test that all AdCP tool methods exist and are callable."""
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    # Verify all required methods exist
    assert hasattr(client, "get_products")
    assert hasattr(client, "list_creative_formats")
    assert hasattr(client, "sync_creatives")
    assert hasattr(client, "list_creatives")
    assert hasattr(client, "get_media_buy_delivery")
    assert hasattr(client, "list_authorized_properties")
    assert hasattr(client, "get_signals")
    assert hasattr(client, "activate_signal")
    assert hasattr(client, "provide_performance_feedback")


@pytest.mark.parametrize(
    "method_name,request_class,request_data",
    [
        ("get_products", "GetProductsRequest", {}),
        ("list_creative_formats", "ListCreativeFormatsRequest", {}),
        ("sync_creatives", "SyncCreativesRequest", {"creatives": []}),
        ("list_creatives", "ListCreativesRequest", {}),
        ("get_media_buy_delivery", "GetMediaBuyDeliveryRequest", {}),
        ("list_authorized_properties", "ListAuthorizedPropertiesRequest", {}),
        (
            "get_signals",
            "GetSignalsRequest",
            {
                "signal_spec": "test",
                "deliver_to": {
                    "countries": ["US"],
                    "destinations": [{"type": "platform", "platform": "test"}],
                },
            },
        ),
        (
            "activate_signal",
            "ActivateSignalRequest",
            {
                "signal_agent_segment_id": "test",
                "destinations": [{"type": "platform", "platform": "test"}],
            },
        ),
        (
            "provide_performance_feedback",
            "ProvidePerformanceFeedbackRequest",
            {
                "media_buy_id": "test",
                "measurement_period": {
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-31T23:59:59Z",
                },
                "performance_index": 0.5,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_method_calls_correct_tool_name(method_name, request_class, request_data):
    """Test that each method calls the correct adapter method.

    This test ensures client methods call the matching adapter method
    (e.g., client.get_products calls adapter.get_products).
    """
    from unittest.mock import patch

    import adcp.types._generated as gen
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    # Create request instance with required fields
    request_cls = getattr(gen, request_class)
    request = request_cls(**request_data)

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={},
        success=True,
    )

    # Mock the specific adapter method (not call_tool)
    with patch.object(client.adapter, method_name, return_value=mock_result) as mock_method:
        method = getattr(client, method_name)
        await method(request)

        # Verify adapter method was called
        mock_method.assert_called_once()


@pytest.mark.asyncio
async def test_multi_agent_parallel_execution():
    """Test parallel execution across multiple agents."""
    from unittest.mock import patch

    from adcp.types._generated import GetProductsRequest
    from adcp.types.core import TaskResult, TaskStatus

    agents = [
        AgentConfig(
            id="agent1",
            agent_uri="https://agent1.example.com",
            protocol=Protocol.A2A,
        ),
        AgentConfig(
            id="agent2",
            agent_uri="https://agent2.example.com",
            protocol=Protocol.MCP,
        ),
    ]

    client = ADCPMultiAgentClient(agents)

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"products": []},
        success=True,
    )

    # Mock both agents' adapters - keep context active during execution
    with (
        patch.object(
            client.agents["agent1"].adapter, "get_products", return_value=mock_result
        ) as mock1,
        patch.object(
            client.agents["agent2"].adapter, "get_products", return_value=mock_result
        ) as mock2,
    ):
        request = GetProductsRequest(brief="test")
        results = await client.get_products(request)

        # Verify both agents' get_products method was called
        mock1.assert_called_once_with({"brief": "test"})
        mock2.assert_called_once_with({"brief": "test"})

        # Verify results from both agents
        assert len(results) == 2
        assert all(r.success for r in results)


@pytest.mark.asyncio
async def test_list_creative_formats_parses_mcp_response():
    """Test that list_creative_formats parses MCP content array into structured response."""
    import json
    from unittest.mock import patch

    from adcp.types._generated import ListCreativeFormatsRequest, ListCreativeFormatsResponse
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)

    # Mock MCP response with content array containing JSON
    formats_data = {
        "formats": [
            {
                "format_id": {"agent_url": "https://creative.example.com", "id": "banner_300x250"},
                "name": "Medium Rectangle",
                "type": "display",
            },
            {
                "format_id": {"agent_url": "https://creative.example.com", "id": "video_16x9"},
                "name": "Video 16:9",
                "type": "video",
            },
        ]
    }

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=[{"type": "text", "text": json.dumps(formats_data)}],  # MCP content array
        success=True,
    )

    with patch.object(client.adapter, "list_creative_formats", return_value=mock_result):
        request = ListCreativeFormatsRequest()
        result = await client.list_creative_formats(request)

        # Verify response is parsed into structured type
        assert result.success is True
        assert isinstance(result.data, ListCreativeFormatsResponse)
        assert len(result.data.formats) == 2
        assert result.data.formats[0].name == "Medium Rectangle"
        assert result.data.formats[1].name == "Video 16:9"


@pytest.mark.asyncio
async def test_list_creative_formats_parses_a2a_response():
    """Test that list_creative_formats parses A2A dict response into structured response."""
    from unittest.mock import patch

    from adcp.types._generated import ListCreativeFormatsRequest, ListCreativeFormatsResponse
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    # Mock A2A response with direct dict data
    formats_data = {
        "formats": [
            {
                "format_id": {"agent_url": "https://creative.example.com", "id": "native_feed"},
                "name": "Native Feed Ad",
                "type": "native",
            }
        ]
    }

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=formats_data,  # Direct dict from A2A
        success=True,
    )

    with patch.object(client.adapter, "list_creative_formats", return_value=mock_result):
        request = ListCreativeFormatsRequest()
        result = await client.list_creative_formats(request)

        # Verify response is parsed into structured type
        assert result.success is True
        assert isinstance(result.data, ListCreativeFormatsResponse)
        assert len(result.data.formats) == 1
        assert result.data.formats[0].name == "Native Feed Ad"


@pytest.mark.asyncio
async def test_list_creative_formats_handles_invalid_response():
    """Test that list_creative_formats handles invalid response gracefully."""
    from unittest.mock import patch

    from adcp.types._generated import ListCreativeFormatsRequest
    from adcp.types.core import TaskResult, TaskStatus

    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)

    # Mock invalid response (text instead of structured data)
    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=[{"type": "text", "text": "Found 42 creative formats"}],  # Invalid: not JSON
        success=True,
    )

    with patch.object(client.adapter, "list_creative_formats", return_value=mock_result):
        request = ListCreativeFormatsRequest()
        result = await client.list_creative_formats(request)

        # Verify error is returned
        assert result.success is False
        assert result.status == TaskStatus.FAILED
        assert "Failed to parse response" in result.error


@pytest.mark.asyncio
async def test_client_context_manager():
    """Test that ADCPClient works as an async context manager."""
    from unittest.mock import AsyncMock, patch

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.MCP,
    )

    # Mock the close method to verify it gets called
    with patch.object(ADCPClient, "close", new_callable=AsyncMock) as mock_close:
        async with ADCPClient(config) as client:
            assert client.agent_config == config

        # Verify close was called on context exit
        mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_multi_agent_context_manager():
    """Test that ADCPMultiAgentClient works as an async context manager."""
    from unittest.mock import AsyncMock, patch

    agents = [
        AgentConfig(
            id="agent1",
            agent_uri="https://agent1.example.com",
            protocol=Protocol.A2A,
        ),
        AgentConfig(
            id="agent2",
            agent_uri="https://agent2.example.com",
            protocol=Protocol.MCP,
        ),
    ]

    # Mock the close method to verify it gets called
    with patch.object(ADCPMultiAgentClient, "close", new_callable=AsyncMock) as mock_close:
        async with ADCPMultiAgentClient(agents) as client:
            assert len(client.agents) == 2

        # Verify close was called on context exit
        mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_client_context_manager_with_exception():
    """Test that ADCPClient properly closes even when an exception occurs."""
    from unittest.mock import AsyncMock, patch

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.MCP,
    )

    # Mock the close method to verify it gets called
    with patch.object(ADCPClient, "close", new_callable=AsyncMock) as mock_close:
        try:
            async with ADCPClient(config) as client:
                assert client.agent_config == config
                raise ValueError("Test exception")
        except ValueError:
            pass  # Expected

        # Verify close was called even after exception
        mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_multi_agent_close_handles_adapter_failures():
    """Test that multi-agent close handles individual adapter failures gracefully."""
    from unittest.mock import AsyncMock, patch

    agents = [
        AgentConfig(
            id="agent1",
            agent_uri="https://agent1.example.com",
            protocol=Protocol.A2A,
        ),
        AgentConfig(
            id="agent2",
            agent_uri="https://agent2.example.com",
            protocol=Protocol.MCP,
        ),
    ]

    client = ADCPMultiAgentClient(agents)

    # Mock one adapter to fail during close
    mock_close_success = AsyncMock()
    mock_close_failure = AsyncMock(side_effect=RuntimeError("Cleanup error"))

    with (
        patch.object(client.agents["agent1"].adapter, "close", mock_close_success),
        patch.object(client.agents["agent2"].adapter, "close", mock_close_failure),
    ):
        # Should not raise despite one adapter failing
        await client.close()

        # Verify both adapters had close called
        mock_close_success.assert_called_once()
        mock_close_failure.assert_called_once()
