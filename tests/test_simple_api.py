"""Tests for the simplified API accessor."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from adcp.testing import test_agent
from adcp.types.core import TaskResult, TaskStatus
from adcp.types.generated import (
    GetProductsResponse,
    ListCreativeFormatsResponse,
    PreviewCreativeResponse,
    Product,
)


@pytest.mark.asyncio
async def test_get_products_simple_api():
    """Test client.simple.get_products with kwargs."""
    # Create mock response (using model_construct to bypass validation for test data)
    mock_product = Product.model_construct(
        product_id="prod_1",
        name="Test Product",
        description="A test product",
    )
    mock_response = GetProductsResponse.model_construct(products=[mock_product])
    mock_result = TaskResult[GetProductsResponse](
        status=TaskStatus.COMPLETED, data=mock_response, success=True
    )

    # Mock the client's get_products method
    with patch.object(test_agent, "get_products", new=AsyncMock(return_value=mock_result)):
        # Call simplified API with kwargs
        result = await test_agent.simple.get_products(brief="Coffee subscription service")

        # Verify it returns unwrapped data
        assert isinstance(result, GetProductsResponse)
        assert len(result.products) == 1
        assert result.products[0].product_id == "prod_1"

        # Verify the underlying call was made correctly
        test_agent.get_products.assert_called_once()
        call_args = test_agent.get_products.call_args[0][0]
        assert call_args.brief == "Coffee subscription service"


@pytest.mark.asyncio
async def test_get_products_simple_api_failure():
    """Test client.simple.get_products raises exception on failure."""
    # Create mock failure response
    mock_result = TaskResult[GetProductsResponse](
        status=TaskStatus.FAILED, data=None, success=False, error="Test error"
    )

    with patch.object(test_agent, "get_products", new=AsyncMock(return_value=mock_result)):
        # Should raise exception on failure
        with pytest.raises(Exception, match="get_products failed"):
            await test_agent.simple.get_products(brief="Test")


def test_simple_api_has_no_sync_methods():
    """Test that simple API only provides async methods.

    Users can wrap with asyncio.run() if they need sync behavior.
    """
    # Verify simple API doesn't have sync methods
    assert not hasattr(test_agent.simple, "get_products_sync")
    assert hasattr(test_agent.simple, "get_products")


@pytest.mark.asyncio
async def test_list_creative_formats_simple_api():
    """Test client.simple.list_creative_formats with kwargs."""
    from adcp.types.generated import Format

    # Create mock response (using model_construct to bypass validation for test data)
    mock_format = Format.model_construct(
        format_id={"id": "banner_300x250"},
        name="Banner 300x250",
        description="Standard banner",
    )
    mock_response = ListCreativeFormatsResponse.model_construct(formats=[mock_format])
    mock_result = TaskResult[ListCreativeFormatsResponse](
        status=TaskStatus.COMPLETED, data=mock_response, success=True
    )

    with patch.object(test_agent, "list_creative_formats", new=AsyncMock(return_value=mock_result)):
        # Call simplified API
        result = await test_agent.simple.list_creative_formats()

        # Verify it returns unwrapped data
        assert isinstance(result, ListCreativeFormatsResponse)
        assert len(result.formats) == 1
        assert result.formats[0].format_id["id"] == "banner_300x250"


def test_simple_api_exists_on_client():
    """Test that all clients have a .simple accessor."""
    from adcp.testing import creative_agent, test_agent_a2a

    # All clients should have .simple
    assert hasattr(test_agent, "simple")
    assert hasattr(test_agent_a2a, "simple")
    assert hasattr(creative_agent, "simple")

    # Should be SimpleAPI instance
    from adcp.simple import SimpleAPI

    assert isinstance(test_agent.simple, SimpleAPI)
    assert isinstance(test_agent_a2a.simple, SimpleAPI)
    assert isinstance(creative_agent.simple, SimpleAPI)


@pytest.mark.asyncio
async def test_preview_creative_simple_api():
    """Test client.simple.preview_creative."""
    from adcp.testing import creative_agent

    mock_response = PreviewCreativeResponse(
        previews=[{"url": "https://preview.example.com/123", "html": "<html>...</html>"}]
    )
    mock_result = TaskResult[PreviewCreativeResponse](
        status=TaskStatus.COMPLETED, data=mock_response, success=True
    )

    with patch.object(creative_agent, "preview_creative", new=AsyncMock(return_value=mock_result)):
        # Call simplified API
        result = await creative_agent.simple.preview_creative(
            manifest={"format_id": "banner_300x250", "assets": {}}
        )

        # Verify it returns unwrapped data
        assert isinstance(result, PreviewCreativeResponse)
        assert result.previews is not None
        assert len(result.previews) == 1


def test_simple_api_methods():
    """Test that SimpleAPI has all expected methods."""
    # Check all methods exist
    assert hasattr(test_agent.simple, "get_products")
    assert hasattr(test_agent.simple, "list_creative_formats")
    assert hasattr(test_agent.simple, "preview_creative")
    assert hasattr(test_agent.simple, "sync_creatives")
    assert hasattr(test_agent.simple, "list_creatives")
    assert hasattr(test_agent.simple, "get_media_buy_delivery")
    assert hasattr(test_agent.simple, "list_authorized_properties")
    assert hasattr(test_agent.simple, "get_signals")
    assert hasattr(test_agent.simple, "activate_signal")
    assert hasattr(test_agent.simple, "provide_performance_feedback")

    # Verify they're all async methods (not sync)
    import inspect

    assert inspect.iscoroutinefunction(test_agent.simple.get_products)
    assert inspect.iscoroutinefunction(test_agent.simple.list_creative_formats)
