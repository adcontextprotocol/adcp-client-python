"""v2.5 → v3 adapter for ``get_products``.

This is the inverse of the JS SDK's ``adaptGetProductsRequestForV2`` /
``normalizeGetProductsResponse`` helpers — the JS direction is *client*
side (v3 client → v2 server). Server side flips both arrows:

* **Request (v2.5 → v3):** the buyer sent a v2.5 shape; rewrite to v3
  so the handler's typed code sees the canonical model.
* **Response (v3 → v2.5):** the handler produced a v3 shape; rewrite
  to the v2.5 shape the buyer expects on the wire.

Field deltas the adapter handles:

* **``brand_manifest`` (v2.5 URL string) ↔ ``brand`` (v3 BrandReference).**
  v2.5 sent ``brand_manifest: "https://example.com"``; v3 expects
  ``brand: {domain: "example.com"}``.
* **``promoted_offerings`` (v2.5 nested object) ↔ ``catalog`` (v3
  discriminated union).** v2.5's product-selectors / offerings nesting
  collapses to v3's ``catalog.type = 'product' | 'offering'`` shape.
* **Channels.** v2.5 used coarser buckets (``video``, ``audio``,
  ``native``, ``retail``); v3 splits them (``video`` → ``olv``+``ctv``,
  ``audio`` → ``streaming_audio``, etc.). The buyer-direction mapping
  fans out; the response-direction mapping collapses — both are
  inherently lossy when traffic mixes the buckets. The translation
  defers ambiguity to the canonical mappings documented in the JS
  SDK and falls through unknown channels unchanged.
* **Pricing options.** Per pricing option:
  ``rate`` ↔ ``fixed_price`` (and ``is_fixed`` discriminator gone in v3),
  ``price_guidance.floor`` ↔ ``floor_price`` (floor moves out of
  guidance). Percentile fields (``p25``/``p50``/``p75``/``p90``) stay
  in ``price_guidance`` in both versions.

Direct port (with direction inverted) of
``src/lib/utils/pricing-adapter.ts``.
"""

from __future__ import annotations

from typing import Any

from adcp.compat.legacy import register_adapter
from adcp.compat.legacy.types import AdapterPair
from adcp.compat.legacy.v2_5._url import strip_url_scheme

# v2.5 channel buckets to v3 channel slugs. Multi-mapped buckets resolve
# to all listed v3 channels; downstream consumers can narrow further via
# ``filters.channels`` if needed.
_V2_TO_V3_CHANNEL: dict[str, tuple[str, ...]] = {
    "video": ("olv", "ctv"),
    "audio": ("streaming_audio",),
    "native": ("display",),
    "retail": ("retail_media",),
}

# v3 channel slugs to v2.5 buckets. Inverse of ``_V2_TO_V3_CHANNEL``,
# collapsing many-to-one. Channels v3 added with no v2.5 equivalent map
# to themselves (the v2.5 buyer simply hasn't heard of them; pass the
# raw slug through so they can choose to ignore).
_V3_TO_V2_CHANNEL: dict[str, str] = {
    "olv": "video",
    "ctv": "video",
    "streaming_audio": "audio",
    "retail_media": "retail",
}


def _normalize_pricing_option(option: dict[str, Any]) -> dict[str, Any]:
    """v2.5 → v3 for one pricing option.

    Mirrors ``normalizePricingOption`` in the JS SDK.
    """
    # Already v3 shape — leave alone (idempotent on half-migrated input).
    if "fixed_price" in option or "floor_price" in option:
        if "rate" not in option and "is_fixed" not in option:
            return option

    rest = {k: v for k, v in option.items() if k not in {"rate", "is_fixed", "price_guidance"}}
    rate = option.get("rate")
    is_fixed = option.get("is_fixed")
    price_guidance = option.get("price_guidance")

    if rate is not None and (is_fixed is True or is_fixed is None):
        rest["fixed_price"] = rate

    if isinstance(price_guidance, dict):
        floor = price_guidance.get("floor")
        percentiles = {k: v for k, v in price_guidance.items() if k != "floor"}
        if floor is not None:
            rest["floor_price"] = floor
        if percentiles:
            rest["price_guidance"] = percentiles

    return rest


def _adapt_pricing_option_for_v2(option: dict[str, Any]) -> dict[str, Any]:
    """v3 → v2.5 for one pricing option.

    Mirrors ``adaptPricingOptionForV2`` in the JS SDK.
    """
    has_v3 = "fixed_price" in option or "floor_price" in option
    if not has_v3:
        return option

    rest = {
        k: v for k, v in option.items() if k not in {"fixed_price", "floor_price", "price_guidance"}
    }
    fixed_price = option.get("fixed_price")
    floor_price = option.get("floor_price")
    percentiles = option.get("price_guidance") or {}

    if fixed_price is not None:
        rest["rate"] = fixed_price
        rest["is_fixed"] = True
    else:
        rest["is_fixed"] = False
        if floor_price is not None or percentiles:
            v2_guidance = dict(percentiles)
            if floor_price is not None:
                v2_guidance["floor"] = floor_price
            rest["price_guidance"] = v2_guidance

    return rest


def adapt_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a v2.5 ``get_products`` request to v3 shape."""
    out = dict(payload)

    # brand_manifest (v2.5 URL string) → brand.domain (v3 BrandReference)
    brand_manifest = out.pop("brand_manifest", None)
    if isinstance(brand_manifest, str) and brand_manifest and "brand" not in out:
        out["brand"] = {"domain": strip_url_scheme(brand_manifest)}

    # promoted_offerings → catalog
    promoted = out.pop("promoted_offerings", None)
    if isinstance(promoted, dict) and "catalog" not in out:
        product_selectors = promoted.get("product_selectors")
        if isinstance(product_selectors, dict):
            catalog: dict[str, Any] = {"type": "product"}
            ps = product_selectors
            if "manifest_gtins" in ps:
                catalog["gtins"] = ps["manifest_gtins"]
            if "manifest_skus" in ps:
                catalog["ids"] = ps["manifest_skus"]
            if "manifest_tags" in ps:
                catalog["tags"] = ps["manifest_tags"]
            if "manifest_category" in ps:
                catalog["category"] = ps["manifest_category"]
            if "manifest_query" in ps:
                catalog["query"] = ps["manifest_query"]
            out["catalog"] = catalog
        elif "offerings" in promoted:
            out["catalog"] = {"type": "offering", "items": promoted["offerings"]}

    # Channel-bucket fan-out in filters.
    filters = out.get("filters")
    if isinstance(filters, dict) and isinstance(filters.get("channels"), list):
        expanded: list[str] = []
        seen: set[str] = set()
        for ch in filters["channels"]:
            if not isinstance(ch, str):
                continue
            mapped = _V2_TO_V3_CHANNEL.get(ch, (ch,))
            for slug in mapped:
                if slug not in seen:
                    seen.add(slug)
                    expanded.append(slug)
        out["filters"] = {**filters, "channels": expanded}

    return out


def _normalize_product_channels(product: dict[str, Any]) -> dict[str, Any]:
    """v3 → v2.5 channel collapse for a single product."""
    channels = product.get("channels")
    if not isinstance(channels, list):
        return product
    collapsed: list[str] = []
    seen: set[str] = set()
    for ch in channels:
        if not isinstance(ch, str):
            continue
        mapped = _V3_TO_V2_CHANNEL.get(ch, ch)
        if mapped not in seen:
            seen.add(mapped)
            collapsed.append(mapped)
    return {**product, "channels": collapsed}


def _normalize_product_pricing_v3_to_v2(product: dict[str, Any]) -> dict[str, Any]:
    """v3 → v2.5 pricing-option rewrite for a single product."""
    options = product.get("pricing_options")
    if not isinstance(options, list):
        return product
    return {
        **product,
        "pricing_options": [
            _adapt_pricing_option_for_v2(o) if isinstance(o, dict) else o for o in options
        ],
    }


def normalize_response(response: dict[str, Any]) -> dict[str, Any]:
    """Translate a v3 ``get_products`` response back to v2.5 shape.

    Mirrors ``adaptGetProductsResponseForV2`` (the inverse of the JS
    ``normalizeGetProductsResponse``). For each product:

    * Collapse v3 channel slugs to v2.5 buckets (lossy — ``olv`` and
      ``ctv`` both collapse to ``video``).
    * Rewrite each pricing option's ``fixed_price`` / ``floor_price``
      back to ``rate`` / ``price_guidance.floor`` with the ``is_fixed``
      discriminator restored.
    """
    products = response.get("products")
    if not isinstance(products, list):
        return response
    return {
        **response,
        "products": [
            (
                _normalize_product_pricing_v3_to_v2(_normalize_product_channels(p))
                if isinstance(p, dict)
                else p
            )
            for p in products
        ],
    }


def is_legacy_shape(payload: dict[str, Any]) -> bool:
    """v2.5 ``get_products`` carries either ``brand_manifest`` (URL
    string field that v3 doesn't have) or ``promoted_offerings``
    (nested object replaced by ``catalog`` in v3). Either is a
    strong signal."""
    return "brand_manifest" in payload or "promoted_offerings" in payload


ADAPTER = AdapterPair(
    tool_name="get_products",
    adapt_request=adapt_request,
    normalize_response=normalize_response,
    is_legacy_shape=is_legacy_shape,
)
register_adapter("2.5", ADAPTER)
