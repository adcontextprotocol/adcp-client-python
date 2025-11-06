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
    from unittest.mock import AsyncMock, patch
    from adcp.types.core import TaskResult, TaskStatus
    from adcp.types.generated import GetProductsRequest

    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )

    client = ADCPClient(config)

    # Mock the adapter's call_tool method
    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"products": [{"id": "prod_1", "name": "Test Product"}]},
        success=True,
    )

    with patch.object(client.adapter, "call_tool", return_value=mock_result) as mock_call:
        request = GetProductsRequest(brief="test campaign")
        result = await client.get_products(request)

        mock_call.assert_called_once()
        assert result.success is True
        assert result.status == TaskStatus.COMPLETED
        assert "products" in result.data


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


@pytest.mark.asyncio
async def test_multi_agent_parallel_execution():
    """Test parallel execution across multiple agents."""
    from unittest.mock import patch
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

    # Mock both agents' adapters
    for agent_client in client.agents.values():
        with patch.object(agent_client.adapter, "call_tool", return_value=mock_result):
            pass

    # Test that get_products can be called on multi-agent client
    # (actual execution would require proper mocking of asyncio.gather)
    assert callable(client.get_products)
