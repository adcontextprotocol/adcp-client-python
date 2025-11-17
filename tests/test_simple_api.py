"""Tests for Simple API."""

import pytest
from unittest.mock import AsyncMock, patch

from adcp import ADCPClient, ADCPSimpleError
from adcp.types import AgentConfig, Protocol, TaskResult, TaskStatus
from adcp.types.generated import (
    GetProductsResponse,
    ListAuthorizedPropertiesResponse,
    Product,
    Property,
    SyncCreativesResponse,
)


@pytest.fixture
def client():
    """Create test client."""
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )
    return ADCPClient(config)


@pytest.mark.asyncio
async def test_simple_api_property(client):
    """Test that .simple property exists and returns SimpleAPI."""
    simple = client.simple
    assert simple is not None
    assert simple._client is client
    # Property should return same instance
    assert client.simple is simple


@pytest.mark.asyncio
async def test_simple_get_products_success(client):
    """Test simple.get_products with successful completion."""
    # Mock response
    mock_response = GetProductsResponse(
        products=[
            Product(
                product_id="prod_1",
                name="Test Product",
                description="A test product",
                publisher_properties=[{"property_id": "prop_1"}],
                format_ids=["format_1"],
                delivery_type="guaranteed",
                pricing_options=[{"pricing_option_id": "price_1", "pricing_model": "cpm"}],
                delivery_measurement={"provider": "test"},
            )
        ]
    )

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=mock_response,
        success=True,
    )

    with patch.object(client.adapter, "call_tool", return_value=mock_result):
        result = await client.simple.get_products(brief="test campaign")

        # Should return response data directly
        assert isinstance(result, GetProductsResponse)
        assert len(result.products) == 1
        assert result.products[0].product_id == "prod_1"


@pytest.mark.asyncio
async def test_simple_list_authorized_properties_success(client):
    """Test simple.list_authorized_properties with successful completion."""
    mock_response = ListAuthorizedPropertiesResponse(
        publisher_domains=["test.com", "example.com"]
    )

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=mock_response,
        success=True,
    )

    with patch.object(client.adapter, "call_tool", return_value=mock_result):
        result = await client.simple.list_authorized_properties()

        assert isinstance(result, ListAuthorizedPropertiesResponse)
        assert len(result.publisher_domains) == 2
        assert "test.com" in result.publisher_domains


@pytest.mark.asyncio
async def test_simple_sync_creatives_success(client):
    """Test simple.sync_creatives with successful completion."""
    mock_response = SyncCreativesResponse(
        creatives=[{"creative_id": "creative_1", "status": "synced"}]
    )

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=mock_response,
        success=True,
    )

    with patch.object(client.adapter, "call_tool", return_value=mock_result):
        result = await client.simple.sync_creatives(
            media_buy_id="mb_123",
            creatives=[
                {
                    "creative_id": "creative_1",
                    "name": "Test Creative",
                    "format_id": "format_1",
                    "assets": {},
                }
            ],
        )

        assert isinstance(result, SyncCreativesResponse)
        assert len(result.creatives) == 1


@pytest.mark.asyncio
async def test_simple_api_with_kwargs(client):
    """Test that simple API accepts keyword arguments."""
    mock_response = GetProductsResponse(products=[])
    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=mock_response,
        success=True,
    )

    with patch.object(client.adapter, "call_tool", return_value=mock_result):
        # Should accept kwargs without creating request object manually
        result = await client.simple.get_products(
            brief="test",
            brand_manifest={"name": "Test Brand", "url": "https://test.com"},
            format_ids=["format_1", "format_2"],
        )

        assert isinstance(result, GetProductsResponse)


@pytest.mark.asyncio
async def test_simple_api_submitted_error(client):
    """Test that simple API raises error for async submitted tasks."""
    mock_result = TaskResult(
        status=TaskStatus.SUBMITTED,
        submitted={"webhook_url": "https://webhook.com", "operation_id": "op_123"},
    )

    with patch.object(client.adapter, "call_tool", return_value=mock_result):
        with pytest.raises(ADCPSimpleError, match="Task submitted for async processing"):
            await client.simple.get_products(brief="test")


@pytest.mark.asyncio
async def test_simple_api_needs_input_error(client):
    """Test that simple API raises error when agent needs input."""
    mock_result = TaskResult(
        status=TaskStatus.NEEDS_INPUT,
        needs_input={"message": "Please provide budget", "field": "budget"},
    )

    with patch.object(client.adapter, "call_tool", return_value=mock_result):
        with pytest.raises(ADCPSimpleError, match="Agent needs input"):
            await client.simple.get_products(brief="test")


@pytest.mark.asyncio
async def test_simple_api_failed_error(client):
    """Test that simple API raises error for failed tasks."""
    mock_result = TaskResult(
        status=TaskStatus.FAILED,
        error="Something went wrong",
        success=False,
    )

    with patch.object(client.adapter, "call_tool", return_value=mock_result):
        with pytest.raises(ADCPSimpleError, match="Task failed: Something went wrong"):
            await client.simple.get_products(brief="test")


@pytest.mark.asyncio
async def test_simple_api_direct_data_access(client):
    """Test that simple API provides direct data access without .data wrapper."""
    mock_response = GetProductsResponse(
        products=[
            Product(
                product_id="prod_1",
                name="Test Product",
                description="A test product",
                publisher_properties=[{"property_id": "prop_1"}],
                format_ids=["format_1"],
                delivery_type="guaranteed",
                pricing_options=[{"pricing_option_id": "price_1", "pricing_model": "cpm"}],
                delivery_measurement={"provider": "test"},
            )
        ]
    )

    mock_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data=mock_response,
        success=True,
    )

    with patch.object(client.adapter, "call_tool", return_value=mock_result):
        result = await client.simple.get_products(brief="test")

        # Direct access - no .data needed
        assert hasattr(result, "products")
        assert len(result.products) == 1
        assert result.products[0].name == "Test Product"
