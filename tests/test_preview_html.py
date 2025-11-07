"""Tests for preview URL generation functionality."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol
from adcp.types.core import TaskResult, TaskStatus
from adcp.types.generated import (
    CreativeManifest,
    Format,
    FormatId,
    GetProductsRequest,
    GetProductsResponse,
    ListCreativeFormatsRequest,
    ListCreativeFormatsResponse,
    PreviewCreativeResponse,
    Product,
)
from adcp.utils.preview_cache import (
    PreviewURLGenerator,
    _create_sample_manifest_for_format,
    _create_sample_asset,
    add_preview_urls_to_formats,
    add_preview_urls_to_products,
)


def make_format_id(id_str: str) -> FormatId:
    """Helper to create FormatId objects for tests."""
    return FormatId(
        agent_url="https://creative.adcontextprotocol.org",
        id=id_str
    )


@pytest.mark.asyncio
async def test_preview_creative():
    """Test preview_creative method."""
    from adcp.types.generated import PreviewCreativeRequest

    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)

    format_id = make_format_id("display_300x250")
    manifest = CreativeManifest(
        format_id=format_id, assets={"image": "https://example.com/img.jpg"}
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"previews": []},  # Will be replaced by _parse_response mock
        success=True
    )

    # Parsed result from _parse_response
    mock_response_data = PreviewCreativeResponse(
        previews=[{"preview_url": "https://preview.example.com/abc123"}],
        expires_at="2025-12-01T00:00:00Z",
    )
    mock_parsed_result = TaskResult(status=TaskStatus.COMPLETED, data=mock_response_data, success=True)

    with patch.object(client.adapter, "preview_creative", return_value=mock_raw_result) as mock_call:
        with patch.object(client.adapter, "_parse_response", return_value=mock_parsed_result):
            request = PreviewCreativeRequest(format_id=format_id, creative_manifest=manifest)
            result = await client.preview_creative(request)

            assert result.success
            assert result.data
            assert len(result.data.previews) == 1
            assert result.data.previews[0]["preview_url"] == "https://preview.example.com/abc123"
            mock_call.assert_called_once()


@pytest.mark.asyncio
async def test_get_preview_data_for_manifest():
    """Test generating preview data for a manifest."""
    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)
    generator = PreviewURLGenerator(client)

    format_id = make_format_id("display_300x250")
    manifest = CreativeManifest(
        format_id=format_id, assets={"image": "https://example.com/img.jpg"}
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"previews": []},
        success=True
    )

    # Parsed result from _parse_response
    mock_preview_response = PreviewCreativeResponse(
        previews=[
            {"preview_url": "https://preview.example.com/abc123", "input": {"name": "Desktop"}}
        ],
        expires_at="2025-12-01T00:00:00Z",
    )
    mock_parsed_result = TaskResult(status=TaskStatus.COMPLETED, data=mock_preview_response, success=True)

    with patch.object(client.adapter, "preview_creative", return_value=mock_raw_result):
        with patch.object(client.adapter, "_parse_response", return_value=mock_parsed_result):
            result = await generator.get_preview_data_for_manifest(format_id, manifest)

            assert result is not None
            assert result["preview_url"] == "https://preview.example.com/abc123"
            assert result["expires_at"] == "2025-12-01T00:00:00Z"
            assert "input" in result


@pytest.mark.asyncio
async def test_preview_data_caching():
    """Test that preview data is cached."""
    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)
    generator = PreviewURLGenerator(client)

    format_id = make_format_id("display_300x250")
    manifest = CreativeManifest(
        format_id=format_id, assets={"image": "https://example.com/img.jpg"}
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"previews": []},
        success=True
    )

    # Parsed result from _parse_response
    mock_preview_response = PreviewCreativeResponse(
        previews=[{"preview_url": "https://preview.example.com/abc123"}],
        expires_at="2025-12-01T00:00:00Z",
    )
    mock_parsed_result = TaskResult(status=TaskStatus.COMPLETED, data=mock_preview_response, success=True)

    with patch.object(client.adapter, "preview_creative", return_value=mock_raw_result) as mock_call:
        with patch.object(client.adapter, "_parse_response", return_value=mock_parsed_result):
            result1 = await generator.get_preview_data_for_manifest(format_id, manifest)
            result2 = await generator.get_preview_data_for_manifest(format_id, manifest)

            assert result1 is not None
            assert result2 is not None
            assert result1["preview_url"] == result2["preview_url"]
            mock_call.assert_called_once()


@pytest.mark.asyncio
async def test_get_products_with_preview_urls():
    """Test get_products with fetch_previews parameter."""
    config = AgentConfig(
        id="publisher_agent",
        agent_uri="https://publisher.example.com",
        protocol=Protocol.MCP,
    )

    creative_config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)
    creative_client = ADCPClient(creative_config)

    format_id = make_format_id("display_300x250")
    product = Product(
        product_id="prod_1",
        name="Test Product",
        description="Test Description",
        publisher_properties=[],
        format_ids=[format_id],
        delivery_type="guaranteed",
        pricing_options=[{"id": "cpm_1", "type": "cpm", "price": 5.0}],
        delivery_measurement={"provider": "test"},
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"products": []},  # Will be replaced by _parse_response mock
        success=True
    )

    # Parsed result from _parse_response
    mock_products_response = GetProductsResponse(products=[product], errors=None)
    mock_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_products_response, success=True
    )

    # Raw preview result from creative adapter
    mock_preview_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"previews": []},
        success=True
    )

    # Parsed preview result
    mock_preview_response = PreviewCreativeResponse(
        previews=[{"preview_url": "https://preview.example.com/abc123"}],
        expires_at="2025-12-01T00:00:00Z",
    )
    mock_preview_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_preview_response, success=True
    )

    with patch.object(client.adapter, "get_products", return_value=mock_raw_result):
        with patch.object(client.adapter, "_parse_response", return_value=mock_parsed_result):
            with patch.object(creative_client.adapter, "preview_creative", return_value=mock_preview_raw_result):
                with patch.object(creative_client.adapter, "_parse_response", return_value=mock_preview_parsed_result):
                    request = GetProductsRequest(brief="test campaign")
                    result = await client.get_products(
                        request, fetch_previews=True, creative_agent_client=creative_client
                    )

                    assert result.success
                    assert "products_with_previews" in result.metadata
                    products_with_previews = result.metadata["products_with_previews"]
                    assert len(products_with_previews) == 1
                    assert "format_previews" in products_with_previews[0]
                    format_previews = products_with_previews[0]["format_previews"]
                    assert "display_300x250" in format_previews
                    assert "preview_url" in format_previews["display_300x250"]


@pytest.mark.asyncio
async def test_get_products_without_creative_client_raises_error():
    """Test that get_products raises ValueError when fetch_previews=True without creative client."""
    config = AgentConfig(
        id="publisher_agent",
        agent_uri="https://publisher.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)

    with pytest.raises(ValueError, match="creative_agent_client is required"):
        request = GetProductsRequest(brief="test campaign")
        await client.get_products(request, fetch_previews=True)


@pytest.mark.asyncio
async def test_list_creative_formats_with_preview_urls():
    """Test list_creative_formats with fetch_previews parameter."""
    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)

    format_id = make_format_id("display_300x250")
    fmt = Format(
        format_id=format_id,
        name="Display 300x250",
        description="Standard banner",
        type="display",
        assets_required=[{"asset_id": "image", "type": "image"}],
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"formats": []},  # Will be replaced by _parse_response mock
        success=True
    )

    # Parsed result from _parse_response
    mock_formats_response = ListCreativeFormatsResponse(formats=[fmt], errors=None)
    mock_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_formats_response, success=True
    )

    # Raw preview result from adapter
    mock_preview_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"previews": []},
        success=True
    )

    # Parsed preview result
    mock_preview_response = PreviewCreativeResponse(
        previews=[{"preview_url": "https://preview.example.com/abc123"}],
        expires_at="2025-12-01T00:00:00Z",
    )
    mock_preview_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_preview_response, success=True
    )

    with patch.object(client.adapter, "list_creative_formats", return_value=mock_raw_result):
        with patch.object(client.adapter, "_parse_response", side_effect=[mock_parsed_result, mock_preview_parsed_result]):
            with patch.object(client.adapter, "preview_creative", return_value=mock_preview_raw_result):
                request = ListCreativeFormatsRequest()
                result = await client.list_creative_formats(request, fetch_previews=True)

                assert result.success
                assert "formats_with_previews" in result.metadata
                formats_with_previews = result.metadata["formats_with_previews"]
                assert len(formats_with_previews) == 1
                assert "preview_data" in formats_with_previews[0]
                assert "preview_url" in formats_with_previews[0]["preview_data"]


def test_create_sample_asset():
    """Test sample asset creation."""
    assert "placeholder" in _create_sample_asset("image")
    assert ".mp4" in _create_sample_asset("video")
    assert "text" in _create_sample_asset("text").lower()
    assert "example.com" in _create_sample_asset("url")
    assert "<div>" in _create_sample_asset("html")


def test_create_sample_manifest_for_format():
    """Test creating sample manifest for a format."""
    format_id = make_format_id("display_300x250")
    fmt = Format(
        format_id=format_id,
        name="Display 300x250",
        description="Standard banner",
        type="display",
        assets_required=[
            {"asset_id": "image", "type": "image"},
            {"asset_id": "clickthrough_url", "type": "url"},
        ],
    )

    manifest = _create_sample_manifest_for_format(fmt)

    assert manifest is not None
    assert manifest.format_id == format_id
    assert "image" in manifest.assets
    assert "clickthrough_url" in manifest.assets


def test_create_sample_manifest_for_format_no_assets():
    """Test creating sample manifest for a format without assets."""
    format_id = make_format_id("display_300x250")
    fmt = Format(
        format_id=format_id,
        name="Display 300x250",
        description="Standard banner",
        type="display",
        assets_required=None,
    )

    manifest = _create_sample_manifest_for_format(fmt)
    assert manifest is None
