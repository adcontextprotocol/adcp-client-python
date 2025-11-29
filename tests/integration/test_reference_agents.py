"""Integration tests for reference AdCP agents.

Tests against live reference agents to ensure the SDK works correctly:
- test-agent.adcontextprotocol.org (A2A and MCP)
- creative.adcontextprotocol.org (MCP only)
"""

import pytest

from adcp import ADCPClient
from adcp.types import (
    AgentConfig,
    GetProductsRequest,
    ListCreativeFormatsRequest,
    Protocol,
)


class TestTestAgent:
    """Integration tests for test-agent.adcontextprotocol.org.

    This agent supports both A2A and MCP protocols for testing.
    """

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_products_mcp(self):
        """Test getting products via MCP protocol."""
        config = AgentConfig(
            id="test_agent_mcp",
            agent_uri="https://test-agent.adcontextprotocol.org",
            protocol=Protocol.MCP,
        )

        async with ADCPClient(config) as client:
            request = GetProductsRequest(brief="Coffee brands")
            result = await client.get_products(request)

            assert result.success, f"Failed to get products: {result.error}"
            assert result.data is not None, "Expected data in response"
            assert hasattr(result.data, "products"), "Expected products in data"
            assert len(result.data.products) > 0, "Expected at least one product"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_products_a2a(self):
        """Test getting products via A2A protocol."""
        config = AgentConfig(
            id="test_agent_a2a",
            agent_uri="https://test-agent.adcontextprotocol.org",
            protocol=Protocol.A2A,
        )

        async with ADCPClient(config) as client:
            request = GetProductsRequest(brief="Coffee brands")
            result = await client.get_products(request)

            assert result.success, f"Failed to get products: {result.error}"
            assert result.data is not None, "Expected data in response"
            assert hasattr(result.data, "products"), "Expected products in data"
            assert len(result.data.products) > 0, "Expected at least one product"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_protocol_equivalence(self):
        """Test that both protocols return similar results for the same request."""
        brief = "Television advertising"
        request = GetProductsRequest(brief=brief)

        # Test with MCP
        mcp_config = AgentConfig(
            id="test_agent_mcp",
            agent_uri="https://test-agent.adcontextprotocol.org",
            protocol=Protocol.MCP,
        )
        async with ADCPClient(mcp_config) as client:
            mcp_result = await client.get_products(request)

        # Test with A2A
        a2a_config = AgentConfig(
            id="test_agent_a2a",
            agent_uri="https://test-agent.adcontextprotocol.org",
            protocol=Protocol.A2A,
        )
        async with ADCPClient(a2a_config) as client:
            a2a_result = await client.get_products(request)

        # Both should succeed
        assert mcp_result.success, f"MCP failed: {mcp_result.error}"
        assert a2a_result.success, f"A2A failed: {a2a_result.error}"

        # Both should have products
        assert mcp_result.data is not None
        assert a2a_result.data is not None
        assert len(mcp_result.data.products) > 0
        assert len(a2a_result.data.products) > 0

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_simple_api_mcp(self):
        """Test simple API with MCP protocol."""
        config = AgentConfig(
            id="test_agent_mcp",
            agent_uri="https://test-agent.adcontextprotocol.org",
            protocol=Protocol.MCP,
        )

        async with ADCPClient(config) as client:
            # Simple API doesn't require wrapping in request object
            result = await client.simple.get_products(brief="Digital advertising")

            assert result is not None
            assert hasattr(result, "products")
            assert len(result.products) > 0

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_simple_api_a2a(self):
        """Test simple API with A2A protocol."""
        config = AgentConfig(
            id="test_agent_a2a",
            agent_uri="https://test-agent.adcontextprotocol.org",
            protocol=Protocol.A2A,
        )

        async with ADCPClient(config) as client:
            # Simple API doesn't require wrapping in request object
            result = await client.simple.get_products(brief="Digital advertising")

            assert result is not None
            assert hasattr(result, "products")
            assert len(result.products) > 0

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_error_handling_mcp(self):
        """Test error handling with invalid endpoint (MCP)."""
        config = AgentConfig(
            id="invalid_agent",
            agent_uri="https://invalid.example.com/nonexistent",
            protocol=Protocol.MCP,
            timeout=2.0,  # Short timeout
        )

        # Should raise an exception for invalid endpoint
        with pytest.raises(Exception):
            async with ADCPClient(config) as client:
                request = GetProductsRequest(brief="test")
                await client.get_products(request)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_error_handling_a2a(self):
        """Test error handling with invalid endpoint (A2A)."""
        config = AgentConfig(
            id="invalid_agent",
            agent_uri="https://invalid.example.com/nonexistent",
            protocol=Protocol.A2A,
            timeout=2.0,  # Short timeout
        )

        # Should raise an exception for invalid endpoint
        with pytest.raises(Exception):
            async with ADCPClient(config) as client:
                request = GetProductsRequest(brief="test")
                await client.get_products(request)


class TestCreativeAgent:
    """Integration tests for creative.adcontextprotocol.org.

    This agent only supports MCP protocol.
    """

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_list_creative_formats(self):
        """Test listing creative formats."""
        config = AgentConfig(
            id="creative_agent",
            agent_uri="https://creative.adcontextprotocol.org",
            protocol=Protocol.MCP,
        )

        async with ADCPClient(config) as client:
            request = ListCreativeFormatsRequest()
            result = await client.list_creative_formats(request)

            assert result.success, f"Failed to list formats: {result.error}"
            assert result.data is not None, "Expected data in response"
            assert hasattr(result.data, "formats"), "Expected formats in data"
            assert len(result.data.formats) > 0, "Expected at least one format"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_error_handling(self):
        """Test error handling with invalid endpoint."""
        config = AgentConfig(
            id="invalid_creative",
            agent_uri="https://invalid.example.com/nonexistent",
            protocol=Protocol.MCP,
            timeout=2.0,  # Short timeout
        )

        # Should raise an exception for invalid endpoint
        with pytest.raises(Exception):
            async with ADCPClient(config) as client:
                request = ListCreativeFormatsRequest()
                await client.list_creative_formats(request)
