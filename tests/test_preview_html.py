"""Tests for preview URL generation functionality."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from adcp import ADCPClient
from adcp.types import (
    AgentConfig,
    Format,
    GetProductsRequest,
    GetProductsResponse,
    ImageContent,
    Product,
    Protocol,
)
from adcp.types._generated import (
    CreativeManifest,
)
from adcp.types.core import TaskResult, TaskStatus
from adcp.types.legacy import (
    LegacyFormat,
    LegacyListCreativeFormatsRequest,
    LegacyListCreativeFormatsResponse,
    LegacyPreviewCreativeBatchResponse,
    LegacyPreviewCreativeRequest,
    LegacyPreviewCreativeResponse1,
)
from adcp.types.legacy import (
    LegacyFormatId as FormatId,
)
from adcp.utils.preview_cache import (
    PreviewURLGenerator,
    _create_sample_asset,
    _create_sample_manifest_for_format,
    _preview_render_data,
)
from tests.conftest import validate_union


def make_format_id(id_str: str) -> FormatId:
    """Helper to create FormatId objects for tests."""
    return FormatId(agent_url="https://creative.adcontextprotocol.org", id=id_str)


def test_preview_render_data_preserves_metadata_and_requires_isolation():
    data = _preview_render_data(
        {
            "render_id": "render-1",
            "preview_url": "https://preview.example/render-1",
            "preview_html": "<script>parent.postMessage('unsafe', '*')</script>",
            "embedding": {"recommended_sandbox": "", "csp_policy": "default-src 'none'"},
            "renderer": {
                "renderer_id": "renderer-1",
                "version": "1.0.0",
                "export": "render",
                "rendering_origin": "agent_approximation",
                "tracking_suppressed": True,
            },
        }
    )

    assert data["embedding"]["recommended_sandbox"] == ""
    assert data["renderer"]["renderer_id"] == "renderer-1"
    assert data["rendering_policy"] == {
        "sandbox": "",
        "caller_restrictive_csp_required": True,
        "provider_metadata_advisory": True,
        "preview_url_container": "cross_origin_iframe",
        "preview_html_container": "iframe_srcdoc",
    }


def test_preview_cache_is_bounded_and_honors_expiry():
    generator = PreviewURLGenerator(object(), max_cache_entries=1)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    expired = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    assert generator._store_preview("first", {"expires_at": future, "preview_html": "one"})
    assert generator._store_preview("second", {"expires_at": future, "preview_html": "two"})
    assert generator._get_cached("first") is None
    assert generator._get_cached("second") is not None
    assert not generator._store_preview("expired", {"expires_at": expired})


def test_preview_cache_rejects_oversized_data():
    generator = PreviewURLGenerator(object(), max_preview_bytes=64)

    assert not generator._store_preview("large", {"preview_html": "x" * 128})
    assert generator._get_cached("large") is None


def _batch_preview_response(output_format: str, count: int = 1):
    results = []
    for index in range(count):
        render = {
            "render_id": f"render-{index}",
            "role": "primary",
            "output_format": output_format,
        }
        if output_format == "url":
            render["preview_url"] = f"https://preview.example/{index}"
        else:
            render["preview_html"] = f"<p>preview {index}</p>"
        results.append(
            {
                "success": True,
                "creative_id": f"creative-{index}",
                "response": {
                    "expires_at": "2099-12-01T00:00:00Z",
                    "previews": [
                        {
                            "preview_id": f"preview-{index}",
                            "input": {"name": "Default"},
                            "renders": [render],
                        }
                    ],
                },
            }
        )
    return LegacyPreviewCreativeBatchResponse(response_type="batch", results=results)


@pytest.mark.asyncio
async def test_batch_preview_dispatches_multiple_items():
    preview_call = AsyncMock(
        return_value=TaskResult(
            status=TaskStatus.COMPLETED,
            data=_batch_preview_response("url", count=2),
            success=True,
        )
    )
    generator = PreviewURLGenerator(SimpleNamespace(preview_creative_legacy=preview_call))
    requests = []
    for index in range(2):
        format_id = make_format_id(f"display-{index}")
        manifest = CreativeManifest(format_id=format_id, assets={})
        requests.append((format_id, manifest))

    results = await generator.get_preview_data_batch(requests, output_format="url")

    assert [result["preview_url"] for result in results if result] == [
        "https://preview.example/0",
        "https://preview.example/1",
    ]
    request = preview_call.await_args.args[0]
    assert request.request_type == "batch"
    assert len(request.requests) == 2


@pytest.mark.asyncio
async def test_batch_preview_cache_separates_output_formats():
    preview_call = AsyncMock(
        side_effect=[
            TaskResult(
                status=TaskStatus.COMPLETED,
                data=_batch_preview_response("url"),
                success=True,
            ),
            TaskResult(
                status=TaskStatus.COMPLETED,
                data=_batch_preview_response("html"),
                success=True,
            ),
        ]
    )
    generator = PreviewURLGenerator(SimpleNamespace(preview_creative_legacy=preview_call))
    format_id = make_format_id("display")
    manifest = CreativeManifest(format_id=format_id, assets={})

    url_result = await generator.get_preview_data_batch(
        [(format_id, manifest)], output_format="url"
    )
    html_result = await generator.get_preview_data_batch(
        [(format_id, manifest)], output_format="html"
    )

    assert url_result[0] and url_result[0]["preview_url"] == "https://preview.example/0"
    assert html_result[0] and html_result[0]["preview_html"] == "<p>preview 0</p>"
    assert preview_call.await_count == 2


@pytest.mark.asyncio
async def test_preview_creative():
    """Test the explicit legacy preview method."""

    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.example.com",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)

    format_id = make_format_id("display_300x250")
    manifest = CreativeManifest(
        format_id=format_id,
        assets={
            "image": ImageContent(
                url="https://example.com/img.jpg",
                width=300,
                height=250,
            )
        },
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"previews": []},  # Will be replaced by _parse_response mock
        success=True,
    )

    # Parsed result from _parse_response
    mock_response_data = LegacyPreviewCreativeResponse1(
        response_type="single",
        expires_at="2099-12-01T00:00:00Z",
        previews=[
            {
                "preview_id": "prev-1",
                "input": {"name": "Default"},
                "renders": [
                    {
                        "render_id": "render-1",
                        "role": "primary",
                        "output_format": "url",
                        "preview_url": "https://preview.example.com/abc123",
                    }
                ],
            }
        ],
    )
    mock_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_response_data, success=True
    )

    with patch.object(
        client.adapter, "preview_creative", return_value=mock_raw_result
    ) as mock_call:
        with patch.object(client.adapter, "_parse_response", return_value=mock_parsed_result):
            request = LegacyPreviewCreativeRequest(
                request_type="single",
                format_id=format_id,
                creative_manifest=manifest,
            )
            result = await client.preview_creative_legacy(request)

            assert result.success
            assert result.data
            assert len(result.data.previews) == 1
            # PreviewRender is a RootModel - access .root for the actual variant data
            render = result.data.previews[0].renders[0].root
            assert str(render.preview_url) == "https://preview.example.com/abc123"
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
        format_id=format_id,
        assets={
            "image": ImageContent(
                url="https://example.com/img.jpg",
                width=300,
                height=250,
            )
        },
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(status=TaskStatus.COMPLETED, data={"previews": []}, success=True)

    # Parsed result from _parse_response
    mock_preview_response = LegacyPreviewCreativeResponse1(
        response_type="single",
        expires_at="2099-12-01T00:00:00Z",
        previews=[
            {
                "preview_id": "preview-1",
                "input": {"name": "Desktop"},
                "renders": [
                    {
                        "render_id": "render-1",
                        "role": "primary",
                        "output_format": "url",
                        "preview_url": "https://preview.example.com/abc123",
                    }
                ],
            }
        ],
    )
    mock_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_preview_response, success=True
    )

    with patch.object(client.adapter, "preview_creative", return_value=mock_raw_result):
        with patch.object(client.adapter, "_parse_response", return_value=mock_parsed_result):
            result = await generator.get_preview_data_for_manifest(format_id, manifest)

            assert result is not None
            assert result["preview_url"] == "https://preview.example.com/abc123"
            assert "2099-12-01" in result["expires_at"]  # Check date is present (format may vary)
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
        format_id=format_id,
        assets={
            "image": ImageContent(
                url="https://example.com/img.jpg",
                width=300,
                height=250,
            )
        },
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(status=TaskStatus.COMPLETED, data={"previews": []}, success=True)

    # Parsed result from _parse_response
    mock_preview_response = LegacyPreviewCreativeResponse1(
        response_type="single",
        expires_at="2099-12-01T00:00:00Z",
        previews=[
            {
                "preview_id": "prev-1",
                "input": {"name": "Default"},
                "renders": [
                    {
                        "render_id": "render-1",
                        "role": "primary",
                        "output_format": "url",
                        "preview_url": "https://preview.example.com/abc123",
                    }
                ],
            }
        ],
    )
    mock_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_preview_response, success=True
    )

    with patch.object(
        client.adapter, "preview_creative", return_value=mock_raw_result
    ) as mock_call:
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

    product = Product(
        product_id="prod_1",
        name="Test Product",
        description="Test Description",
        publisher_properties=[{"publisher_domain": "example.com", "selection_type": "all"}],
        delivery_type="guaranteed",
        pricing_options=[
            {
                "currency": "USD",
                "pricing_option_id": "cpm_1",
                "fixed_price": 5.0,
                "pricing_model": "cpm",
            }
        ],
        reporting_capabilities={
            "available_metrics": [],
            "available_reporting_frequencies": ["daily"],
            "date_range_support": "date_range",
            "expected_delay_minutes": 60,
            "supports_webhooks": False,
            "timezone": "UTC",
        },
        format_options=[
            Format(
                format_option_id="display_300x250",
                format_kind="image",
                params={
                    "width": 300,
                    "height": 250,
                    "slots": [
                        {
                            "asset_id": "image",
                            "asset_type": "image",
                            "item_type": "individual",
                            "required": True,
                        }
                    ],
                },
            )
        ],
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"products": []},  # Will be replaced by _parse_response mock
        success=True,
    )

    # Parsed result from _parse_response
    mock_products_response = GetProductsResponse(products=[product], errors=None)
    mock_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_products_response, success=True
    )

    # Raw preview result from creative adapter
    mock_preview_raw_result = TaskResult(
        status=TaskStatus.COMPLETED, data={"previews": []}, success=True
    )

    # Parsed preview result
    mock_preview_response = LegacyPreviewCreativeResponse1(
        response_type="single",
        expires_at="2099-12-01T00:00:00Z",
        previews=[
            {
                "preview_id": "prev-1",
                "input": {"name": "Default"},
                "renders": [
                    {
                        "render_id": "render-1",
                        "role": "primary",
                        "output_format": "url",
                        "preview_url": "https://preview.example.com/abc123",
                    }
                ],
            }
        ],
    )
    mock_preview_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_preview_response, success=True
    )

    with patch.object(client.adapter, "get_products", return_value=mock_raw_result):
        with patch.object(client.adapter, "_parse_response", return_value=mock_parsed_result):
            with patch.object(
                creative_client.adapter,
                "preview_creative",
                return_value=mock_preview_raw_result,
            ):
                with patch.object(
                    creative_client.adapter,
                    "_parse_response",
                    return_value=mock_preview_parsed_result,
                ):
                    request = validate_union(
                        GetProductsRequest, {"buying_mode": "brief", "brief": "test campaign"}
                    )
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
        request = validate_union(
            GetProductsRequest, {"buying_mode": "brief", "brief": "test campaign"}
        )
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
    fmt = LegacyFormat(
        format_id=format_id,
        name="Display 300x250",
        description="Standard banner",
        type="display",
        assets=[
            {
                "asset_id": "image",
                "asset_type": "image",
                "item_type": "individual",
                "required": True,
            }
        ],
    )

    # Raw result from adapter (unparsed)
    mock_raw_result = TaskResult(
        status=TaskStatus.COMPLETED,
        data={"formats": []},  # Will be replaced by _parse_response mock
        success=True,
    )

    # Parsed result from _parse_response
    mock_formats_response = LegacyListCreativeFormatsResponse(formats=[fmt], errors=None)
    mock_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_formats_response, success=True
    )

    # Raw preview result from adapter
    mock_preview_raw_result = TaskResult(
        status=TaskStatus.COMPLETED, data={"previews": []}, success=True
    )

    # Parsed preview result
    mock_preview_response = LegacyPreviewCreativeResponse1(
        response_type="single",
        expires_at="2099-12-01T00:00:00Z",
        previews=[
            {
                "preview_id": "prev-1",
                "input": {"name": "Default"},
                "renders": [
                    {
                        "render_id": "render-1",
                        "role": "primary",
                        "output_format": "url",
                        "preview_url": "https://preview.example.com/abc123",
                    }
                ],
            }
        ],
    )
    mock_preview_parsed_result = TaskResult(
        status=TaskStatus.COMPLETED, data=mock_preview_response, success=True
    )

    with patch.object(client.adapter, "list_creative_formats", return_value=mock_raw_result):
        with patch.object(
            client.adapter,
            "_parse_response",
            side_effect=[mock_parsed_result, mock_preview_parsed_result],
        ):
            with patch.object(
                client.adapter,
                "preview_creative",
                return_value=mock_preview_raw_result,
            ):
                request = LegacyListCreativeFormatsRequest()
                result = await client.list_creative_formats_legacy(request, fetch_previews=True)

                assert result.success
                assert "formats_with_previews" in result.metadata
                formats_with_previews = result.metadata["formats_with_previews"]
                assert len(formats_with_previews) == 1
                assert "preview_data" in formats_with_previews[0]
                assert "preview_url" in formats_with_previews[0]["preview_data"]


def test_create_sample_asset():
    """Test sample asset creation."""
    from adcp.types import HtmlContent, TextContent, UrlContent, VideoContent

    image_asset = _create_sample_asset("image")
    assert isinstance(image_asset, ImageContent)
    assert "placeholder" in str(image_asset.url)

    video_asset = _create_sample_asset("video")
    assert isinstance(video_asset, VideoContent)
    assert ".mp4" in str(video_asset.url)

    text_asset = _create_sample_asset("text")
    assert isinstance(text_asset, TextContent)
    assert "text" in text_asset.content.lower()

    url_asset = _create_sample_asset("url")
    assert isinstance(url_asset, UrlContent)
    assert "example.com" in str(url_asset.url)

    html_asset = _create_sample_asset("html")
    assert isinstance(html_asset, HtmlContent)
    assert "<div>" in html_asset.content


def test_create_sample_manifest_for_format():
    """Test creating sample manifest for a format."""
    format_id = make_format_id("display_300x250")
    fmt = LegacyFormat(
        format_id=format_id,
        name="Display 300x250",
        description="Standard banner",
        type="display",
        assets=[
            {
                "asset_id": "image",
                "asset_type": "image",
                "item_type": "individual",
                "required": True,
            },
            {
                "asset_id": "clickthrough_url",
                "asset_type": "url",
                "item_type": "individual",
                "required": True,
            },
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
    fmt = LegacyFormat(
        format_id=format_id,
        name="Display 300x250",
        description="Standard banner",
        type="display",
        assets=None,
    )

    manifest = _create_sample_manifest_for_format(fmt)
    assert manifest is None


# New tests for v2.6+ assets field


def test_create_sample_manifest_for_format_with_new_assets_field():
    """Test creating sample manifest using new assets field (v2.6+)."""
    format_id = make_format_id("display_300x250")
    fmt = LegacyFormat(
        format_id=format_id,
        name="Display 300x250",
        description="Standard banner",
        type="display",
        assets=[
            {
                "asset_id": "banner_image",
                "asset_type": "image",
                "item_type": "individual",
                "required": True,
            },
            {
                "asset_id": "logo",
                "asset_type": "image",
                "item_type": "individual",
                "required": False,
            },
            {
                "asset_id": "cta_url",
                "asset_type": "url",
                "item_type": "individual",
                "required": True,
            },
        ],
    )

    manifest = _create_sample_manifest_for_format(fmt)
    assert manifest is not None
    # Only required assets should be in the sample manifest
    assert "banner_image" in manifest.assets
    assert "cta_url" in manifest.assets
    # Optional asset should NOT be included
    assert "logo" not in manifest.assets


def test_create_sample_manifest_uses_assets_field():
    """Test that sample manifest uses the assets field."""
    format_id = make_format_id("display_300x250")
    fmt = LegacyFormat(
        format_id=format_id,
        name="Display 300x250",
        description="Standard banner",
        type="display",
        assets=[
            {
                "asset_id": "hero_image",
                "asset_type": "image",
                "item_type": "individual",
                "required": True,
            },
        ],
    )

    manifest = _create_sample_manifest_for_format(fmt)
    assert manifest is not None
    assert "hero_image" in manifest.assets
