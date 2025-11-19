"""Helper utilities for generating creative preview URLs for grid rendering."""

# mypy: disable-error-code="arg-type,attr-defined,call-arg,unused-ignore,union-attr"

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adcp.client import ADCPClient
    from adcp.types.stable import CreativeManifest, Format, FormatId, Product

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
        from adcp.types.aliases import PreviewCreativeFormatRequest

        cache_key = _make_manifest_cache_key(format_id, manifest.model_dump(exclude_none=True))

        if cache_key in self._preview_cache:
            return self._preview_cache[cache_key]

        try:
            request = PreviewCreativeFormatRequest(
                request_type="single",
                format_id=format_id,
                creative_manifest=manifest,
            )
            result = await self.creative_agent_client.preview_creative(request)

            if result.success and result.data and result.data.previews:
                preview = result.data.previews[0]
                first_render = preview.renders[0] if preview.renders else None

                if first_render:
                    has_url = hasattr(first_render, "preview_url")
                    preview_url = str(first_render.preview_url) if has_url else None
                    preview_data = {
                        "preview_id": preview.preview_id,
                        "preview_url": preview_url,
                        "preview_html": getattr(first_render, "preview_html", None),
                        "render_id": first_render.render_id,
                        "input": preview.input.model_dump(),
                        "expires_at": str(result.data.expires_at),
                    }

                    self._preview_cache[cache_key] = preview_data
                    return preview_data

        except Exception as e:
            logger.warning(f"Failed to generate preview for format {format_id}: {e}", exc_info=True)

        return None

    async def get_preview_data_batch(
        self,
        requests: list[tuple[FormatId, CreativeManifest]],
        output_format: str = "url",
    ) -> list[dict[str, Any] | None]:
        """
        Generate preview data for multiple manifests in one API call (batch mode).

        This is 5-10x faster than individual requests for multiple previews.

        Args:
            requests: List of (format_id, manifest) tuples to preview
            output_format: "url" for iframe URLs, "html" for direct embedding

        Returns:
            List of preview data dicts (or None for failures), in same order as requests
        """
        from adcp.types.stable import PreviewCreativeRequest

        if not requests:
            return []

        # Check cache first
        cache_keys = [
            _make_manifest_cache_key(fid, manifest.model_dump(exclude_none=True))
            for fid, manifest in requests
        ]

        # Separate cached vs uncached requests
        uncached_indices: list[int] = []
        uncached_requests: list[dict[str, Any]] = []
        results: list[dict[str, Any] | None] = [None] * len(requests)

        for idx, (cache_key, (format_id, manifest)) in enumerate(zip(cache_keys, requests)):
            if cache_key in self._preview_cache:
                results[idx] = self._preview_cache[cache_key]
            else:
                uncached_indices.append(idx)
                fid_dict = format_id.model_dump() if hasattr(format_id, "model_dump") else format_id
                uncached_requests.append(
                    {
                        "format_id": fid_dict,
                        "creative_manifest": manifest.model_dump(exclude_none=True),
                    }
                )

        # If everything was cached, return early
        if not uncached_requests:
            return results

        # Make batch API call for uncached items
        try:
            # Batch requests in chunks of 50 (API limit)
            batch_size = 50
            for chunk_start in range(0, len(uncached_requests), batch_size):
                chunk_end = min(chunk_start + batch_size, len(uncached_requests))
                chunk_requests = uncached_requests[chunk_start:chunk_end]
                chunk_indices = uncached_indices[chunk_start:chunk_end]

                batch_request = PreviewCreativeRequest(
                    requests=chunk_requests,
                    output_format=output_format,  # type: ignore[arg-type]
                    context=None,
                )
                result = await self.creative_agent_client.preview_creative(batch_request)

                if result.success and result.data and result.data.results:
                    # Process batch results
                    for result_idx, batch_result in enumerate(result.data.results):
                        original_idx = chunk_indices[result_idx]
                        cache_key = cache_keys[original_idx]

                        if batch_result.get("success") and batch_result.get("response"):
                            response = batch_result["response"]
                            if response.get("previews"):
                                preview = response["previews"][0]
                                renders = preview.get("renders", [])
                                first_render = renders[0] if renders else {}
                                preview_data = {
                                    "preview_id": preview.get("preview_id"),
                                    "preview_url": first_render.get("preview_url"),
                                    "preview_html": first_render.get("preview_html"),
                                    "render_id": first_render.get("render_id"),
                                    "input": preview.get("input", {}),
                                    "expires_at": response.get("expires_at"),
                                }
                                # Cache and store
                                self._preview_cache[cache_key] = preview_data
                                results[original_idx] = preview_data
                        else:
                            # Request failed
                            error = batch_result.get("error", {})
                            logger.warning(
                                f"Batch preview failed for request {original_idx}: "
                                f"{error.get('message', 'Unknown error')}"
                            )

        except Exception as e:
            logger.warning(f"Batch preview generation failed: {e}", exc_info=True)

        return results


async def add_preview_urls_to_formats(
    formats: list[Format],
    creative_agent_client: ADCPClient,
    use_batch: bool = True,
    output_format: str = "url",
) -> list[dict[str, Any]]:
    """
    Add preview URLs to each format by generating sample manifests.

    Uses batch API for 5-10x better performance when previewing multiple formats.

    Args:
        formats: List of Format objects
        creative_agent_client: Client for the creative agent
        use_batch: If True, use batch API (default). Set False to use individual requests.
        output_format: "url" for iframe URLs, "html" for direct embedding

    Returns:
        List of format dicts with added preview_data fields
    """
    if not formats:
        return []

    generator = PreviewURLGenerator(creative_agent_client)

    # Prepare all requests
    format_requests = []
    for fmt in formats:
        sample_manifest = _create_sample_manifest_for_format(fmt)
        if sample_manifest:
            format_requests.append((fmt, sample_manifest))

    if not format_requests:
        return [fmt.model_dump(exclude_none=True) for fmt in formats]

    # Use batch API if requested and we have multiple formats
    if use_batch and len(format_requests) > 1:
        # Batch mode - much faster!
        batch_requests = [(fmt.format_id, manifest) for fmt, manifest in format_requests]
        preview_data_list = await generator.get_preview_data_batch(
            batch_requests, output_format=output_format
        )

        # Merge preview data back with formats
        result = []
        preview_idx = 0
        for fmt in formats:
            format_dict = fmt.model_dump(exclude_none=True)
            # Check if this format had a manifest
            if preview_idx < len(format_requests) and format_requests[preview_idx][0] == fmt:
                preview_data = preview_data_list[preview_idx]
                if preview_data:
                    format_dict["preview_data"] = preview_data
                preview_idx += 1
            result.append(format_dict)
        return result
    else:
        # Fallback to individual requests (for single format or when batch disabled)
        import asyncio

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

        return await asyncio.gather(*[process_format(fmt) for fmt in formats])


async def add_preview_urls_to_products(
    products: list[Product],
    creative_agent_client: ADCPClient,
    use_batch: bool = True,
    output_format: str = "url",
) -> list[dict[str, Any]]:
    """
    Add preview URLs to products for their supported formats.

    Uses batch API for 5-10x better performance when previewing many product formats.

    Args:
        products: List of Product objects
        creative_agent_client: Client for the creative agent
        use_batch: If True, use batch API (default). Set False to use individual requests.
        output_format: "url" for iframe URLs, "html" for direct embedding

    Returns:
        List of product dicts with added format_previews field
    """
    if not products:
        return []

    generator = PreviewURLGenerator(creative_agent_client)

    # Collect all unique format_id + manifest combinations across all products
    all_requests: list[tuple[Product, FormatId, CreativeManifest]] = []
    for product in products:
        for format_id in product.format_ids:
            sample_manifest = _create_sample_manifest_for_format_id(format_id, product)
            if sample_manifest:
                all_requests.append((product, format_id, sample_manifest))

    if not all_requests:
        return [p.model_dump(exclude_none=True) for p in products]

    # Use batch API if requested and we have multiple requests
    if use_batch and len(all_requests) > 1:
        # Batch mode - much faster!
        batch_requests = [(format_id, manifest) for _, format_id, manifest in all_requests]
        preview_data_list = await generator.get_preview_data_batch(
            batch_requests, output_format=output_format
        )

        # Map results back to products
        # Build a mapping from product_id -> format_id -> preview_data
        product_previews: dict[str, dict[str, dict[str, Any]]] = {}
        for (product, format_id, _), preview_data in zip(all_requests, preview_data_list):
            if preview_data:
                if product.product_id not in product_previews:
                    product_previews[product.product_id] = {}
                product_previews[product.product_id][format_id.id] = preview_data

        # Add preview data to products
        result = []
        for product in products:
            product_dict = product.model_dump(exclude_none=True)
            if product.product_id in product_previews:
                product_dict["format_previews"] = product_previews[product.product_id]
            result.append(product_dict)
        return result
    else:
        # Fallback to individual requests (for single product/format or when batch disabled)
        import asyncio

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
                        return (format_id.id, preview_data)
                except Exception as e:
                    logger.warning(
                        f"Failed to generate preview for product {product.product_id}, "
                        f"format {format_id}: {e}"
                    )
                return (format_id.id, None)

            format_tasks = [process_format(fid) for fid in product.format_ids]
            format_results = await asyncio.gather(*format_tasks)
            format_previews = {fid: data for fid, data in format_results if data is not None}

            if format_previews:
                product_dict["format_previews"] = format_previews

            return product_dict

        return await asyncio.gather(*[process_product(product) for product in products])


def _create_sample_manifest_for_format(fmt: Format) -> CreativeManifest | None:
    """
    Create a sample manifest for a format.

    Args:
        fmt: Format object

    Returns:
        Sample CreativeManifest, or None if unable to create one
    """
    from adcp.types.stable import CreativeManifest

    if not fmt.assets_required:
        return None

    assets: dict[str, Any] = {}

    for asset in fmt.assets_required:
        if isinstance(asset, dict):
            asset_id = asset.get("asset_id")
            asset_type = asset.get("asset_type")

            if asset_id:
                assets[asset_id] = _create_sample_asset(asset_type)
        else:
            # Handle Pydantic model
            asset_id = asset.asset_id
            has_value = hasattr(asset.asset_type, "value")
            asset_type = asset.asset_type.value if has_value else str(asset.asset_type)
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
    from adcp.types.stable import CreativeManifest, ImageAsset, UrlAsset

    assets = {
        "primary_asset": ImageAsset(url="https://example.com/sample-image.jpg"),
        "clickthrough_url": UrlAsset(url="https://example.com"),
    }

    return CreativeManifest(format_id=format_id, promoted_offering=product.name, assets=assets)


def _create_sample_asset(asset_type: str | None) -> Any:
    """
    Create a sample asset value based on asset type.

    Args:
        asset_type: Type of asset (image, video, text, url, etc.)

    Returns:
        Sample asset object (Pydantic model)
    """
    from adcp.types.stable import (
        HtmlAsset,
        ImageAsset,
        TextAsset,
        UrlAsset,
        VideoAsset,
    )

    if asset_type == "image":
        return ImageAsset(url="https://via.placeholder.com/300x250.png")
    elif asset_type == "video":
        return VideoAsset(url="https://example.com/sample-video.mp4")
    elif asset_type == "text":
        return TextAsset(content="Sample advertising text")
    elif asset_type == "url":
        return UrlAsset(url="https://example.com")
    elif asset_type == "html":
        return HtmlAsset(content="<div>Sample HTML</div>")
    else:
        # Default to URL asset for unknown types
        return UrlAsset(url="https://example.com/sample-asset")
