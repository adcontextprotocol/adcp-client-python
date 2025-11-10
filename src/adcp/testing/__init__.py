"""Test helpers for AdCP client library.

Provides pre-configured test agents for examples and quick testing.

All test agents include a `.simple` accessor for ergonomic usage:

- **Standard API** (client methods): Full TaskResult with error handling
- **Simple API** (client.simple methods): Direct returns, raises on error

Example:
    # Standard API - explicit control
    result = await test_agent.get_products(GetProductsRequest(brief='Coffee'))
    if result.success:
        print(result.data.products)

    # Simple API - ergonomic
    products = await test_agent.simple.get_products(brief='Coffee')
    print(products.products)
"""

from __future__ import annotations

from adcp.testing.test_helpers import (
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

__all__ = [
    "test_agent",
    "test_agent_a2a",
    "test_agent_no_auth",
    "test_agent_a2a_no_auth",
    "creative_agent",
    "test_agent_client",
    "create_test_agent",
    "TEST_AGENT_TOKEN",
    "TEST_AGENT_MCP_CONFIG",
    "TEST_AGENT_A2A_CONFIG",
    "TEST_AGENT_MCP_NO_AUTH_CONFIG",
    "TEST_AGENT_A2A_NO_AUTH_CONFIG",
    "CREATIVE_AGENT_CONFIG",
]
