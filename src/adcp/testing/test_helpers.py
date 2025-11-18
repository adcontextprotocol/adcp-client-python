"""Test agent helpers for easy examples and quick testing.

These provide pre-configured access to AdCP's public test agent.
"""

from __future__ import annotations

from typing import Any

from adcp.client import ADCPClient, ADCPMultiAgentClient
from adcp.types.core import AgentConfig, Protocol

# Public test agent auth token
# This token is public and rate-limited, for testing/examples only.
TEST_AGENT_TOKEN = "1v8tAhASaUYYp4odoQ1PnMpdqNaMiTrCRqYo9OJp6IQ"

# Public test agent configuration - MCP protocol
TEST_AGENT_MCP_CONFIG = AgentConfig(
    id="test-agent-mcp",
    agent_uri="https://test-agent.adcontextprotocol.org/mcp/",
    protocol=Protocol.MCP,
    auth_token=TEST_AGENT_TOKEN,
)

# Public test agent configuration - A2A protocol
TEST_AGENT_A2A_CONFIG = AgentConfig(
    id="test-agent-a2a",
    agent_uri="https://test-agent.adcontextprotocol.org",
    protocol=Protocol.A2A,
    auth_token=TEST_AGENT_TOKEN,
)

# Public test agent configuration (no auth) - MCP protocol
TEST_AGENT_MCP_NO_AUTH_CONFIG = AgentConfig(
    id="test-agent-mcp-no-auth",
    agent_uri="https://test-agent.adcontextprotocol.org/mcp/",
    protocol=Protocol.MCP,
)

# Public test agent configuration (no auth) - A2A protocol
TEST_AGENT_A2A_NO_AUTH_CONFIG = AgentConfig(
    id="test-agent-a2a-no-auth",
    agent_uri="https://test-agent.adcontextprotocol.org",
    protocol=Protocol.A2A,
)

# Reference creative agent configuration - MCP protocol
# No authentication required for the reference creative agent
CREATIVE_AGENT_CONFIG = AgentConfig(
    id="creative-agent",
    agent_uri="https://creative.adcontextprotocol.org/mcp",
    protocol=Protocol.MCP,
)


def _create_test_agent_client() -> ADCPClient:
    """Create pre-configured test agent client using MCP protocol.

    Returns:
        ADCPClient instance configured for the public test agent

    Note:
        This agent is rate-limited and intended for testing/examples only.
        The auth token is public and may be rotated without notice.
        DO NOT use in production applications.
    """
    return ADCPClient(TEST_AGENT_MCP_CONFIG)


def _create_test_agent_a2a_client() -> ADCPClient:
    """Create pre-configured test agent client using A2A protocol.

    Returns:
        ADCPClient instance configured for the public test agent

    Note:
        This agent is rate-limited and intended for testing/examples only.
        The auth token is public and may be rotated without notice.
        DO NOT use in production applications.
    """
    return ADCPClient(TEST_AGENT_A2A_CONFIG)


def _create_test_agent_no_auth_client() -> ADCPClient:
    """Create pre-configured test agent client (no auth) using MCP protocol.

    Returns:
        ADCPClient instance configured for the public test agent without authentication

    Note:
        This agent is rate-limited and intended for testing scenarios where no auth is provided.
        Useful for testing behavior differences between authenticated and unauthenticated requests.
        DO NOT use in production applications.
    """
    return ADCPClient(TEST_AGENT_MCP_NO_AUTH_CONFIG)


def _create_test_agent_a2a_no_auth_client() -> ADCPClient:
    """Create pre-configured test agent client (no auth) using A2A protocol.

    Returns:
        ADCPClient instance configured for the public test agent without authentication

    Note:
        This agent is rate-limited and intended for testing scenarios where no auth is provided.
        Useful for testing behavior differences between authenticated and unauthenticated requests.
        DO NOT use in production applications.
    """
    return ADCPClient(TEST_AGENT_A2A_NO_AUTH_CONFIG)


def _create_creative_agent_client() -> ADCPClient:
    """Create pre-configured creative agent client.

    Returns:
        ADCPClient instance configured for the reference creative agent

    Note:
        The reference creative agent is public and requires no authentication.
        It provides creative preview functionality for testing and examples.
    """
    return ADCPClient(CREATIVE_AGENT_CONFIG)


def _create_test_multi_agent_client() -> ADCPMultiAgentClient:
    """Create multi-agent client with both test agents configured.

    Returns:
        ADCPMultiAgentClient with both MCP and A2A test agents

    Note:
        This client is rate-limited and intended for testing/examples only.
        DO NOT use in production applications.
    """
    return ADCPMultiAgentClient([TEST_AGENT_MCP_CONFIG, TEST_AGENT_A2A_CONFIG])


# Pre-configured test agent client using MCP protocol.
# Ready to use for examples, documentation, and quick testing.
#
# Example:
#     ```python
#     from adcp.testing import test_agent
#
#     # Simple get_products call
#     result = await test_agent.get_products(
#         GetProductsRequest(
#             brief="Coffee subscription service for busy professionals",
#             promoted_offering="Premium monthly coffee deliveries"
#         )
#     )
#
#     if result.success:
#         print(f"Found {len(result.data.products)} products")
#     ```
#
# Note:
#     This agent is rate-limited and intended for testing/examples only.
#     The auth token is public and may be rotated without notice.
#     DO NOT use in production applications.
test_agent: ADCPClient = _create_test_agent_client()

# Pre-configured test agent client using A2A protocol.
# Identical functionality to test_agent but uses A2A instead of MCP.
#
# Example:
#     ```python
#     from adcp.testing import test_agent_a2a
#
#     result = await test_agent_a2a.get_products(
#         GetProductsRequest(
#             brief="Sustainable fashion brands",
#             promoted_offering="Eco-friendly clothing"
#         )
#     )
#     ```
#
# Note:
#     This agent is rate-limited and intended for testing/examples only.
#     The auth token is public and may be rotated without notice.
#     DO NOT use in production applications.
test_agent_a2a: ADCPClient = _create_test_agent_a2a_client()

# Pre-configured test agent client (no auth) using MCP protocol.
# Useful for testing scenarios where authentication is not provided,
# such as testing how agents handle unauthenticated requests or
# comparing behavior between authenticated and unauthenticated calls.
#
# Example:
#     ```python
#     from adcp.testing import test_agent_no_auth
#
#     # Test behavior without authentication
#     result = await test_agent_no_auth.get_products(
#         GetProductsRequest(
#             brief="Coffee subscription service",
#             promoted_offering="Premium monthly coffee"
#         )
#     )
#     ```
#
# Note:
#     This agent is rate-limited and intended for testing/examples only.
#     DO NOT use in production applications.
test_agent_no_auth: ADCPClient = _create_test_agent_no_auth_client()

# Pre-configured test agent client (no auth) using A2A protocol.
# Identical functionality to test_agent_no_auth but uses A2A instead of MCP.
#
# Example:
#     ```python
#     from adcp.testing import test_agent_a2a_no_auth
#
#     # Test A2A behavior without authentication
#     result = await test_agent_a2a_no_auth.get_products(
#         GetProductsRequest(
#             brief="Sustainable fashion brands",
#             promoted_offering="Eco-friendly clothing"
#         )
#     )
#     ```
#
# Note:
#     This agent is rate-limited and intended for testing/examples only.
#     DO NOT use in production applications.
test_agent_a2a_no_auth: ADCPClient = _create_test_agent_a2a_no_auth_client()

# Pre-configured reference creative agent.
# Provides creative preview functionality without authentication.
#
# Example:
#     ```python
#     from adcp.testing import creative_agent
#     from adcp.types._generated import PreviewCreativeRequest
#
#     result = await creative_agent.preview_creative(
#         PreviewCreativeRequest(
#             manifest={
#                 "format_id": "banner_300x250",
#                 "assets": {...}
#             }
#         )
#     )
#     ```
#
# Note:
#     The reference creative agent is public and requires no authentication.
#     Perfect for testing creative rendering and preview functionality.
creative_agent: ADCPClient = _create_creative_agent_client()

# Multi-agent client with both test agents configured.
# Useful for testing multi-agent patterns and protocol comparisons.
#
# Example:
#     ```python
#     from adcp.testing import test_agent_client
#
#     # Access individual agents
#     mcp_agent = test_agent_client.agent("test-agent-mcp")
#     a2a_agent = test_agent_client.agent("test-agent-a2a")
#
#     # Use for parallel operations
#     results = await test_agent_client.get_products(
#         GetProductsRequest(
#             brief="Premium coffee brands",
#             promoted_offering="Artisan coffee"
#         )
#     )
#     ```
#
# Note:
#     This client is rate-limited and intended for testing/examples only.
#     DO NOT use in production applications.
test_agent_client: ADCPMultiAgentClient = _create_test_multi_agent_client()


def create_test_agent(**overrides: Any) -> AgentConfig:
    """Create a custom test agent configuration.

    Useful when you need to modify the default test agent setup.

    Args:
        **overrides: Keyword arguments to override default config values

    Returns:
        Complete agent configuration

    Example:
        ```python
        from adcp.testing import create_test_agent
        from adcp.client import ADCPClient

        # Use default test agent with custom ID
        config = create_test_agent(id="my-test-agent")
        client = ADCPClient(config)
        ```

    Example:
        ```python
        # Use A2A protocol instead of MCP
        from adcp.types.core import Protocol

        config = create_test_agent(
            protocol=Protocol.A2A,
            agent_uri="https://test-agent.adcontextprotocol.org"
        )
        ```
    """
    base_config = TEST_AGENT_MCP_CONFIG.model_dump()
    base_config.update(overrides)
    return AgentConfig(**base_config)
