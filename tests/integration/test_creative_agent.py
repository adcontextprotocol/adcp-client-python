"""Integration tests for the creative agent at creative.adcontextprotocol.org."""

import pytest

from adcp import ADCPClient
from adcp.types import AgentConfig, ListCreativeFormatsRequest, Protocol


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creative_agent_list_formats():
    """Test the reference creative agent can list formats."""
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
async def test_creative_agent_connection_error_handling():
    """Test that connection errors are handled gracefully."""
    config = AgentConfig(
        id="invalid_agent",
        agent_uri="https://invalid.example.com/nonexistent",
        protocol=Protocol.MCP,
        timeout=2.0,  # Short timeout for faster test
    )

    # Should raise an exception for invalid endpoint
    with pytest.raises(Exception):
        async with ADCPClient(config) as client:
            request = ListCreativeFormatsRequest()
            await client.list_creative_formats(request)
