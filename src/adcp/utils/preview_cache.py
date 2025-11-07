"""Helper utilities for generating creative preview URLs for grid rendering."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adcp.client import ADCPClient
    from adcp.types.generated import CreativeManifest, Format, FormatId, Product

logger = logging.getLogger(__name__)


def _make_manifest_cache_key(format_id: FormatId | str, manifest_dict: dict[str, Any]) -> str:
    """
    Create a cache key for a format_id and manifest.

    Args:
        format_id: Format identifier (FormatId object or string)
        manifest_dict: Creative manifest dict

    Returns:
        Cache key string
    """
    # Convert FormatId to string representation
    if isinstance(format_id, str):
        format_id_str = format_id
    else:
        # FormatId is a Pydantic model with agent_url and id
        format_id_str = f"{format_id.agent_url}:{format_id.id}"

    manifest_str = str(sorted(manifest_dict.items()))
    combined = f"{format_id_str}:{manifest_str}"
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


class PreviewURLGenerator:
    """Helper class for generating preview URLs from creative agents."""

    def __init__(self, creative_agent_client: ADCPClient):
        """
        Initialize preview URL generator.

        Args:
            creative_agent_client: ADCPClient configured to talk to a creative agent
        """
        self.creative_agent_client = creative_agent_client
        self._preview_cache: dict[str, dict[str, Any]] = {}

    async def get_preview_data_for_manifest(
        self, format_id: FormatId, manifest: CreativeManifest
    ) -> dict[str, Any] | None:
        """
        Generate preview data for a creative manifest.

        Returns preview data with URLs suitable for embedding in
        <rendered-creative> web components or iframes.

        Args:
            format_id: Format identifier
            manifest: Creative manifest

        Returns:
            Preview data with preview_url and metadata, or None if generation fails
        """
        from adcp.types.generated import PreviewCreativeRequest

        cache_key = _make_manifest_cache_key(format_id, manifest.model_dump(exclude_none=True))

        if cache_key in self._preview_cache:
            return self._preview_cache[cache_key]

        try:
            request = PreviewCreativeRequest(
                format_id=format_id, creative_manifest=manifest, inputs=None, template_id=None
            )
            result = await self.creative_agent_client.preview_creative(request)

            if result.success and result.data and result.data.previews:
                preview = result.data.previews[0]
                preview_data = {
                    "preview_url": preview.get("preview_url"),
                    "input": preview.get("input", {}),
                    "expires_at": result.data.expires_at,
                }

                if preview.get("renders"):
                    preview_data["renders"] = preview["renders"]

                self._preview_cache[cache_key] = preview_data
                return preview_data

        except Exception as e:
            logger.warning(f"Failed to generate preview for format {format_id}: {e}", exc_info=True)

        return None


async def add_preview_urls_to_formats(
    formats: list[Format], creative_agent_client: ADCPClient
) -> list[dict[str, Any]]:
    """
    Add preview URLs to each format by generating sample manifests.

    Returns formats with preview_data containing URLs for web component embedding.
    Preview generation is done in parallel for better performance.

    Note: This makes N API calls (one per format). A batch preview_creatives
    endpoint would be more efficient. See PROTOCOL_SUGGESTIONS.md for details.

    Args:
        formats: List of Format objects
        creative_agent_client: Client for the creative agent

    Returns:
        List of format dicts with added preview_data fields
    """
    import asyncio

    generator = PreviewURLGenerator(creative_agent_client)

    async def process_format(fmt: Format) -> dict[str, Any]:
        """Process a single format and add preview data."""
        format_dict = fmt.model_dump(exclude_none=True)

        try:
            sample_manifest = _create_sample_manifest_for_format(fmt)
            if sample_manifest:
                preview_data = await generator.get_preview_data_for_manifest(
                    fmt.format_id, sample_manifest
                )
                if preview_data:
                    format_dict["preview_data"] = preview_data
        except Exception as e:
            logger.warning(f"Failed to add preview data for format {fmt.format_id}: {e}")

        return format_dict

    # Process all formats in parallel
    return await asyncio.gather(*[process_format(fmt) for fmt in formats])


async def add_preview_urls_to_products(
    products: list[Product], creative_agent_client: ADCPClient
) -> list[dict[str, Any]]:
    """
    Add preview URLs to products for their supported formats.

    Returns products with format_previews containing URLs for web component embedding.
    Preview generation is done in parallel for better performance.

    Note: This makes N API calls (one per format across all products). A batch
    preview_creatives endpoint would be more efficient. See PROTOCOL_SUGGESTIONS.md.

    Args:
        products: List of Product objects
        creative_agent_client: Client for the creative agent

    Returns:
        List of product dicts with added format_previews field
    """
    import asyncio

    generator = PreviewURLGenerator(creative_agent_client)

    async def process_product(product: Product) -> dict[str, Any]:
        """Process a single product and add preview data for all its formats."""
        product_dict = product.model_dump(exclude_none=True)

        async def process_format(format_id: FormatId) -> tuple[str, dict[str, Any] | None]:
            """Process a single format for this product."""
            try:
                sample_manifest = _create_sample_manifest_for_format_id(format_id, product)
                if sample_manifest:
                    preview_data = await generator.get_preview_data_for_manifest(
                        format_id, sample_manifest
                    )
                    # Use just the id field as the key for easier lookup
                    return (format_id.id, preview_data)
            except Exception as e:
                logger.warning(
                    f"Failed to generate preview for product {product.product_id}, "
                    f"format {format_id}: {e}"
                )
            return (format_id.id, None)

        # Process all formats for this product in parallel
        format_results = await asyncio.gather(*[process_format(fid) for fid in product.format_ids])

        # Build format_previews dict from results
        format_previews = {fid: data for fid, data in format_results if data is not None}

        if format_previews:
            product_dict["format_previews"] = format_previews

        return product_dict

    # Process all products in parallel
    return await asyncio.gather(*[process_product(product) for product in products])


def _create_sample_manifest_for_format(fmt: Format) -> CreativeManifest | None:
    """
    Create a sample manifest for a format.

    Args:
        fmt: Format object

    Returns:
        Sample CreativeManifest, or None if unable to create one
    """
    from adcp.types.generated import CreativeManifest

    if not fmt.assets_required:
        return None

    assets: dict[str, Any] = {}

    for asset in fmt.assets_required:
        if isinstance(asset, dict):
            asset_id = asset.get("asset_id")
            asset_type = asset.get("type")

            if asset_id:
                assets[asset_id] = _create_sample_asset(asset_type)

    if not assets:
        return None

    return CreativeManifest(format_id=fmt.format_id, assets=assets, promoted_offering=None)


def _create_sample_manifest_for_format_id(
    format_id: FormatId, product: Product
) -> CreativeManifest | None:
    """
    Create a sample manifest for a format ID referenced by a product.

    Args:
        format_id: Format identifier
        product: Product that references this format

    Returns:
        Sample CreativeManifest with placeholder assets
    """
    from adcp.types.generated import CreativeManifest

    assets = {
        "primary_asset": "https://example.com/sample-image.jpg",
        "clickthrough_url": "https://example.com",
    }

    return CreativeManifest(format_id=format_id, promoted_offering=product.name, assets=assets)


def _create_sample_asset(asset_type: str | None) -> Any:
    """
    Create a sample asset value based on asset type.

    Args:
        asset_type: Type of asset (image, video, text, url, etc.)

    Returns:
        Sample asset value
    """
    if asset_type == "image":
        return "https://via.placeholder.com/300x250.png"
    elif asset_type == "video":
        return "https://example.com/sample-video.mp4"
    elif asset_type == "text":
        return "Sample advertising text"
    elif asset_type == "url":
        return "https://example.com"
    elif asset_type == "html":
        return "<div>Sample HTML</div>"
    else:
        return "https://example.com/sample-asset"
