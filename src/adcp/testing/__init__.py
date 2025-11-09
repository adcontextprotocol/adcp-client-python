"""Test helpers for AdCP client library.

Provides pre-configured test agents for examples and quick testing.
"""

from __future__ import annotations

from adcp.testing.test_helpers import (
    TEST_AGENT_A2A_CONFIG,
    TEST_AGENT_MCP_CONFIG,
    TEST_AGENT_TOKEN,
    create_test_agent,
    test_agent,
    test_agent_a2a,
    test_agent_client,
)

__all__ = [
    "test_agent",
    "test_agent_a2a",
    "test_agent_client",
    "create_test_agent",
    "TEST_AGENT_TOKEN",
    "TEST_AGENT_MCP_CONFIG",
    "TEST_AGENT_A2A_CONFIG",
]
