"""Tests for the test helpers module."""

from __future__ import annotations

from adcp.client import ADCPClient, ADCPMultiAgentClient
from adcp.testing import (
    CREATIVE_AGENT_CONFIG,
    TEST_AGENT_A2A_CONFIG,
    TEST_AGENT_A2A_NO_AUTH_CONFIG,
    TEST_AGENT_MCP_CONFIG,
    TEST_AGENT_MCP_NO_AUTH_CONFIG,
    TEST_AGENT_TOKEN,
    create_test_agent,
    creative_agent,
    test_agent,
    test_agent_a2a,
    test_agent_a2a_no_auth,
    test_agent_client,
    test_agent_no_auth,
)
from adcp.types.core import Protocol


def test_exports_from_testing_module():
    """Test that all expected exports are available from testing module."""
    # These imports should work without errors
    from adcp.testing import (
        TEST_AGENT_A2A_CONFIG,
        TEST_AGENT_A2A_NO_AUTH_CONFIG,
        TEST_AGENT_MCP_CONFIG,
        TEST_AGENT_MCP_NO_AUTH_CONFIG,
        TEST_AGENT_TOKEN,
        create_test_agent,
        test_agent,
        test_agent_a2a,
        test_agent_a2a_no_auth,
        test_agent_client,
        test_agent_no_auth,
    )

    assert test_agent is not None
    assert test_agent_a2a is not None
    assert test_agent_no_auth is not None
    assert test_agent_a2a_no_auth is not None
    assert test_agent_client is not None
    assert callable(create_test_agent)
    assert isinstance(TEST_AGENT_TOKEN, str)
    assert TEST_AGENT_MCP_CONFIG is not None
    assert TEST_AGENT_A2A_CONFIG is not None
    assert TEST_AGENT_MCP_NO_AUTH_CONFIG is not None
    assert TEST_AGENT_A2A_NO_AUTH_CONFIG is not None


def test_test_agent_token():
    """Test that TEST_AGENT_TOKEN is a valid string."""
    assert isinstance(TEST_AGENT_TOKEN, str)
    assert len(TEST_AGENT_TOKEN) > 0
    assert TEST_AGENT_TOKEN == "1v8tAhASaUYYp4odoQ1PnMpdqNaMiTrCRqYo9OJp6IQ"


def test_mcp_config_structure():
    """Test TEST_AGENT_MCP_CONFIG has correct structure."""
    assert TEST_AGENT_MCP_CONFIG.id == "test-agent-mcp"
    assert TEST_AGENT_MCP_CONFIG.protocol == Protocol.MCP
    # AgentConfig validator strips trailing slashes for consistency
    assert TEST_AGENT_MCP_CONFIG.agent_uri == "https://test-agent.adcontextprotocol.org/mcp"
    assert TEST_AGENT_MCP_CONFIG.auth_token is not None


def test_a2a_config_structure():
    """Test TEST_AGENT_A2A_CONFIG has correct structure."""
    assert TEST_AGENT_A2A_CONFIG.id == "test-agent-a2a"
    assert TEST_AGENT_A2A_CONFIG.protocol == Protocol.A2A
    assert TEST_AGENT_A2A_CONFIG.agent_uri == "https://test-agent.adcontextprotocol.org"
    assert TEST_AGENT_A2A_CONFIG.auth_token is not None


def test_test_agent_is_adcp_client():
    """Test that test_agent is an ADCPClient instance."""
    assert isinstance(test_agent, ADCPClient)
    assert hasattr(test_agent, "get_products")
    assert hasattr(test_agent, "list_creative_formats")
    assert callable(test_agent.get_products)
    assert callable(test_agent.list_creative_formats)


def test_test_agent_a2a_is_adcp_client():
    """Test that test_agent_a2a is an ADCPClient instance."""
    assert isinstance(test_agent_a2a, ADCPClient)
    assert hasattr(test_agent_a2a, "get_products")
    assert hasattr(test_agent_a2a, "list_creative_formats")
    assert callable(test_agent_a2a.get_products)
    assert callable(test_agent_a2a.list_creative_formats)


def test_test_agent_client_is_multi_agent():
    """Test that test_agent_client is an ADCPMultiAgentClient instance."""
    assert isinstance(test_agent_client, ADCPMultiAgentClient)
    assert hasattr(test_agent_client, "agent")
    assert hasattr(test_agent_client, "agents")
    assert callable(test_agent_client.agent)
    assert len(test_agent_client.agent_ids) == 2


def test_test_agent_client_provides_access_to_both_agents():
    """Test that test_agent_client provides access to both MCP and A2A agents."""
    mcp_agent = test_agent_client.agent("test-agent-mcp")
    a2a_agent = test_agent_client.agent("test-agent-a2a")

    assert mcp_agent is not None
    assert a2a_agent is not None
    assert isinstance(mcp_agent, ADCPClient)
    assert isinstance(a2a_agent, ADCPClient)
    assert hasattr(mcp_agent, "get_products")
    assert hasattr(a2a_agent, "get_products")


def test_create_test_agent_default():
    """Test that create_test_agent creates valid config with defaults."""
    config = create_test_agent()

    assert config.id == "test-agent-mcp"
    assert config.protocol == Protocol.MCP
    assert config.auth_token is not None


def test_create_test_agent_with_overrides():
    """Test that create_test_agent allows overrides."""
    config = create_test_agent(
        id="custom-test-agent",
        timeout=60.0,
    )

    assert config.id == "custom-test-agent"
    assert config.timeout == 60.0
    assert config.protocol == Protocol.MCP  # unchanged
    assert config.auth_token is not None  # retained


def test_create_test_agent_protocol_override():
    """Test that create_test_agent allows protocol override."""
    config = create_test_agent(
        protocol=Protocol.A2A,
        agent_uri="https://test-agent.adcontextprotocol.org",
    )

    assert config.protocol == Protocol.A2A
    assert config.agent_uri == "https://test-agent.adcontextprotocol.org"


def test_test_agent_config_match():
    """Test that test_agent uses TEST_AGENT_MCP_CONFIG."""
    assert test_agent.agent_config.id == TEST_AGENT_MCP_CONFIG.id
    assert test_agent.agent_config.protocol == TEST_AGENT_MCP_CONFIG.protocol


def test_test_agent_a2a_config_match():
    """Test that test_agent_a2a uses TEST_AGENT_A2A_CONFIG."""
    assert test_agent_a2a.agent_config.id == TEST_AGENT_A2A_CONFIG.id
    assert test_agent_a2a.agent_config.protocol == TEST_AGENT_A2A_CONFIG.protocol


def test_agent_ids_in_test_agent_client():
    """Test that test_agent_client has correct agent IDs."""
    agent_ids = test_agent_client.agent_ids
    assert "test-agent-mcp" in agent_ids
    assert "test-agent-a2a" in agent_ids


def test_creative_agent_config_structure():
    """Test CREATIVE_AGENT_CONFIG has correct structure."""
    assert CREATIVE_AGENT_CONFIG.id == "creative-agent"
    assert CREATIVE_AGENT_CONFIG.protocol == Protocol.MCP
    assert CREATIVE_AGENT_CONFIG.agent_uri == "https://creative.adcontextprotocol.org/mcp"
    # Creative agent requires no authentication
    assert CREATIVE_AGENT_CONFIG.auth_token is None


def test_creative_agent_is_adcp_client():
    """Test that creative_agent is an ADCPClient instance."""
    assert isinstance(creative_agent, ADCPClient)
    assert hasattr(creative_agent, "preview_creative")
    assert hasattr(creative_agent, "list_creative_formats")
    assert callable(creative_agent.preview_creative)
    assert callable(creative_agent.list_creative_formats)


def test_creative_agent_config_match():
    """Test that creative_agent uses CREATIVE_AGENT_CONFIG."""
    assert creative_agent.agent_config.id == CREATIVE_AGENT_CONFIG.id
    assert creative_agent.agent_config.protocol == CREATIVE_AGENT_CONFIG.protocol


def test_mcp_no_auth_config_structure():
    """Test TEST_AGENT_MCP_NO_AUTH_CONFIG has correct structure."""
    assert TEST_AGENT_MCP_NO_AUTH_CONFIG.id == "test-agent-mcp-no-auth"
    assert TEST_AGENT_MCP_NO_AUTH_CONFIG.protocol == Protocol.MCP
    assert TEST_AGENT_MCP_NO_AUTH_CONFIG.agent_uri == "https://test-agent.adcontextprotocol.org/mcp"
    assert TEST_AGENT_MCP_NO_AUTH_CONFIG.auth_token is None


def test_a2a_no_auth_config_structure():
    """Test TEST_AGENT_A2A_NO_AUTH_CONFIG has correct structure."""
    assert TEST_AGENT_A2A_NO_AUTH_CONFIG.id == "test-agent-a2a-no-auth"
    assert TEST_AGENT_A2A_NO_AUTH_CONFIG.protocol == Protocol.A2A
    assert TEST_AGENT_A2A_NO_AUTH_CONFIG.agent_uri == "https://test-agent.adcontextprotocol.org"
    assert TEST_AGENT_A2A_NO_AUTH_CONFIG.auth_token is None


def test_test_agent_no_auth_is_adcp_client():
    """Test that test_agent_no_auth is an ADCPClient instance."""
    assert isinstance(test_agent_no_auth, ADCPClient)
    assert hasattr(test_agent_no_auth, "get_products")
    assert hasattr(test_agent_no_auth, "list_creative_formats")
    assert callable(test_agent_no_auth.get_products)
    assert callable(test_agent_no_auth.list_creative_formats)


def test_test_agent_a2a_no_auth_is_adcp_client():
    """Test that test_agent_a2a_no_auth is an ADCPClient instance."""
    assert isinstance(test_agent_a2a_no_auth, ADCPClient)
    assert hasattr(test_agent_a2a_no_auth, "get_products")
    assert hasattr(test_agent_a2a_no_auth, "list_creative_formats")
    assert callable(test_agent_a2a_no_auth.get_products)
    assert callable(test_agent_a2a_no_auth.list_creative_formats)


def test_test_agent_no_auth_config_match():
    """Test that test_agent_no_auth uses TEST_AGENT_MCP_NO_AUTH_CONFIG."""
    assert test_agent_no_auth.agent_config.id == TEST_AGENT_MCP_NO_AUTH_CONFIG.id
    assert test_agent_no_auth.agent_config.protocol == TEST_AGENT_MCP_NO_AUTH_CONFIG.protocol
    assert test_agent_no_auth.agent_config.auth_token is None


def test_test_agent_a2a_no_auth_config_match():
    """Test that test_agent_a2a_no_auth uses TEST_AGENT_A2A_NO_AUTH_CONFIG."""
    assert test_agent_a2a_no_auth.agent_config.id == TEST_AGENT_A2A_NO_AUTH_CONFIG.id
    assert test_agent_a2a_no_auth.agent_config.protocol == TEST_AGENT_A2A_NO_AUTH_CONFIG.protocol
    assert test_agent_a2a_no_auth.agent_config.auth_token is None
