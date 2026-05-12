"""Tests for the v2.5 ↔ v3 ``get_products`` adapter."""

from __future__ import annotations

import logging
from typing import Any

from adcp.compat.legacy import get_legacy_adapter
from adcp.compat.legacy.v2_5 import get_products as v2_5_gp


def _adapt(payload: dict[str, Any]) -> dict[str, Any]:
    return v2_5_gp.ADAPTER.adapt_request(payload)


def _normalize(response: Any) -> Any:
    assert v2_5_gp.ADAPTER.normalize_response is not None
    return v2_5_gp.ADAPTER.normalize_response(response)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_adapter_registered_for_v2_5() -> None:
    adapter = get_legacy_adapter("2.5", "get_products")
    assert adapter is not None
    assert adapter.tool_name == "get_products"
    assert adapter.normalize_response is not None


def test_adapt_request_does_not_mutate_input() -> None:
    payload = {"brand_manifest": "https://example.com", "brief": "Q4"}
    original = dict(payload)
    _adapt(payload)
    assert payload == original


# ---------------------------------------------------------------------------
# Request: brand_manifest → brand
# ---------------------------------------------------------------------------


def test_brand_manifest_url_becomes_brand_domain() -> None:
    out = _adapt({"brand_manifest": "https://acme.example.com"})
    assert out["brand"] == {"domain": "acme.example.com"}
    assert "brand_manifest" not in out


def test_brand_manifest_strips_http_scheme() -> None:
    out = _adapt({"brand_manifest": "http://acme.example.com/"})
    assert out["brand"] == {"domain": "acme.example.com"}


def test_brand_manifest_strips_trailing_slash() -> None:
    out = _adapt({"brand_manifest": "https://acme.example.com///"})
    assert out["brand"] == {"domain": "acme.example.com"}


def test_brand_manifest_with_path_extracts_hostname() -> None:
    """Regression for #677: brand_manifest URL with a path must extract only
    the hostname so the result satisfies BrandReference.domain's regex."""
    out = _adapt({"brand_manifest": "https://acme.com/.well-known/brand.json"})
    assert out["brand"] == {"domain": "acme.com"}
    assert "brand_manifest" not in out


def test_brand_manifest_with_port_drops_port() -> None:
    """Port numbers are stripped; only the hostname reaches brand.domain."""
    out = _adapt({"brand_manifest": "https://acme.com:8443/.well-known/brand.json"})
    assert out["brand"] == {"domain": "acme.com"}


def test_brand_field_takes_precedence_over_manifest() -> None:
    """Half-migrated buyer sends both — keep the v3 field."""
    out = _adapt(
        {
            "brand_manifest": "https://acme.example.com",
            "brand": {"domain": "explicit.example.com"},
        }
    )
    assert out["brand"] == {"domain": "explicit.example.com"}
    assert "brand_manifest" not in out


# ---------------------------------------------------------------------------
# Request: brand_manifest as inline BrandManifest object (#684)
# ---------------------------------------------------------------------------


def test_brand_manifest_inline_object_with_url_becomes_brand_domain() -> None:
    """Inline BrandManifest object with url extracts hostname for brand.domain."""
    out = _adapt(
        {
            "brand_manifest": {
                "url": "https://acme.example.com",
                "name": "ACME Corp",
            }
        }
    )
    assert out["brand"] == {"domain": "acme.example.com"}
    assert "brand_manifest" not in out


def test_brand_manifest_inline_object_url_with_path_extracts_hostname() -> None:
    """Inline object url may be a full URL with path — only hostname survives."""
    out = _adapt(
        {
            "brand_manifest": {
                "url": "https://acme.com/.well-known/brand.json",
                "name": "ACME Corp",
            }
        }
    )
    assert out["brand"] == {"domain": "acme.com"}
    assert "brand_manifest" not in out


def test_brand_manifest_inline_object_no_url_skips_brand() -> None:
    """Inline object without url has no derivable hostname; brand is omitted.

    The BrandManifest schema marks url as optional (only name is required).
    The adapter skips brand and lets v3 validation decide — it does not raise.
    """
    out = _adapt({"brand_manifest": {"name": "Great Value"}})
    assert "brand" not in out
    assert "brand_manifest" not in out


def test_brand_manifest_inline_object_brand_wins_when_both_present() -> None:
    """Half-migrated buyer sends explicit brand alongside inline object — keep brand."""
    out = _adapt(
        {
            "brand_manifest": {"url": "https://acme.example.com", "name": "ACME Corp"},
            "brand": {"domain": "explicit.example.com"},
        }
    )
    assert out["brand"] == {"domain": "explicit.example.com"}
    assert "brand_manifest" not in out


# ---------------------------------------------------------------------------
# Request: brand_manifest non-standard-path warning (issue #683)
# ---------------------------------------------------------------------------

_GP_LOGGER = "adcp.compat.legacy.v2_5.get_products"


def test_brand_manifest_cdn_url_warns(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """CDN brand_manifest with non-standard path emits a WARNING about the
    information loss so operators can debug downstream 404s."""
    import adcp.compat.legacy.v2_5.get_products as _gp

    monkeypatch.setattr(_gp, "_brand_manifest_path_warned", set())
    with caplog.at_level(logging.WARNING, logger=_GP_LOGGER):
        out = _adapt({"brand_manifest": "https://cdn.acmecorp.com/brand-manifest.json"})
    assert out["brand"] == {"domain": "cdn.acmecorp.com"}
    records = [r for r in caplog.records if r.name == _GP_LOGGER]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "non-standard path" in msg
    assert "cdn.acmecorp.com" in msg


def test_brand_manifest_well_known_path_no_warning(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Standard /.well-known/brand.json path does NOT warn — v3 derives the
    same URL that the buyer originally pointed at."""
    import adcp.compat.legacy.v2_5.get_products as _gp

    monkeypatch.setattr(_gp, "_brand_manifest_path_warned", set())
    with caplog.at_level(logging.WARNING, logger=_GP_LOGGER):
        _adapt({"brand_manifest": "https://acme.com/.well-known/brand.json"})
    assert not any(r.name == _GP_LOGGER for r in caplog.records)


def test_brand_manifest_bare_domain_no_warning(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Bare domain brand_manifest (no scheme) does not false-positive — urlparse
    returns hostname=None for schemeless strings so we skip the path check."""
    import adcp.compat.legacy.v2_5.get_products as _gp

    monkeypatch.setattr(_gp, "_brand_manifest_path_warned", set())
    with caplog.at_level(logging.WARNING, logger=_GP_LOGGER):
        _adapt({"brand_manifest": "acme.com"})
    assert not any(r.name == _GP_LOGGER for r in caplog.records)


def test_brand_manifest_cdn_url_warns_once_dedup(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Same CDN URL repeated across requests only logs one warning (dedup)."""
    import adcp.compat.legacy.v2_5.get_products as _gp

    monkeypatch.setattr(_gp, "_brand_manifest_path_warned", set())
    url = "https://cdn.acmecorp.com/brand-manifest.json"
    with caplog.at_level(logging.WARNING, logger=_GP_LOGGER):
        _adapt({"brand_manifest": url})
        _adapt({"brand_manifest": url})
    records = [r for r in caplog.records if r.name == _GP_LOGGER]
    assert len(records) == 1


# ---------------------------------------------------------------------------
# Request: promoted_offerings → catalog
# ---------------------------------------------------------------------------


def test_promoted_offerings_product_selectors_become_catalog_product() -> None:
    out = _adapt(
        {
            "promoted_offerings": {
                "product_selectors": {
                    "manifest_gtins": ["123"],
                    "manifest_skus": ["sku-1"],
                    "manifest_tags": ["holiday"],
                    "manifest_category": "apparel",
                    "manifest_query": "blue jacket",
                }
            }
        }
    )
    assert out["catalog"] == {
        "type": "product",
        "gtins": ["123"],
        "ids": ["sku-1"],
        "tags": ["holiday"],
        "category": "apparel",
        "query": "blue jacket",
    }
    assert "promoted_offerings" not in out


def test_promoted_offerings_offerings_become_catalog_offering() -> None:
    out = _adapt({"promoted_offerings": {"offerings": [{"name": "x"}, {"name": "y"}]}})
    assert out["catalog"] == {
        "type": "offering",
        "items": [{"name": "x"}, {"name": "y"}],
    }


# ---------------------------------------------------------------------------
# Request: channel bucket fan-out
# ---------------------------------------------------------------------------


def test_channels_video_fans_out_to_olv_and_ctv() -> None:
    out = _adapt({"filters": {"channels": ["video"]}})
    assert out["filters"]["channels"] == ["olv", "ctv"]


def test_channels_audio_maps_to_streaming_audio() -> None:
    out = _adapt({"filters": {"channels": ["audio"]}})
    assert out["filters"]["channels"] == ["streaming_audio"]


def test_channels_unknown_pass_through() -> None:
    out = _adapt({"filters": {"channels": ["display", "future_channel"]}})
    assert out["filters"]["channels"] == ["display", "future_channel"]


def test_channels_dedup_after_expansion() -> None:
    """If a buyer already includes olv/ctv alongside video, expand
    without duplicating."""
    out = _adapt({"filters": {"channels": ["video", "olv"]}})
    assert out["filters"]["channels"] == ["olv", "ctv"]


def test_filters_other_fields_preserved() -> None:
    out = _adapt({"filters": {"channels": ["video"], "format_ids": ["x"]}})
    assert out["filters"]["channels"] == ["olv", "ctv"]
    assert out["filters"]["format_ids"] == ["x"]


# ---------------------------------------------------------------------------
# Response: pricing v3 → v2.5
# ---------------------------------------------------------------------------


def test_response_fixed_price_becomes_rate_and_is_fixed_true() -> None:
    response = {
        "products": [
            {
                "product_id": "p1",
                "pricing_options": [
                    {
                        "pricing_option_id": "po1",
                        "pricing_model": "cpm",
                        "currency": "USD",
                        "fixed_price": 10.0,
                    }
                ],
            }
        ]
    }
    out = _normalize(response)
    opt = out["products"][0]["pricing_options"][0]
    assert opt["rate"] == 10.0
    assert opt["is_fixed"] is True
    assert "fixed_price" not in opt


def test_response_floor_price_moves_into_price_guidance() -> None:
    response = {
        "products": [
            {
                "product_id": "p1",
                "pricing_options": [
                    {
                        "pricing_option_id": "po1",
                        "pricing_model": "cpm",
                        "currency": "USD",
                        "floor_price": 5.0,
                        "price_guidance": {"p50": 7.0},
                    }
                ],
            }
        ]
    }
    out = _normalize(response)
    opt = out["products"][0]["pricing_options"][0]
    assert opt["is_fixed"] is False
    assert opt["price_guidance"] == {"p50": 7.0, "floor": 5.0}
    assert "floor_price" not in opt


def test_response_channels_collapse_to_v2_buckets() -> None:
    response = {"products": [{"product_id": "p1", "channels": ["olv", "ctv", "streaming_audio"]}]}
    out = _normalize(response)
    # olv + ctv both collapse to "video" — deduped.
    assert out["products"][0]["channels"] == ["video", "audio"]


def test_response_unknown_channels_pass_through() -> None:
    response = {"products": [{"product_id": "p1", "channels": ["display", "future"]}]}
    out = _normalize(response)
    assert out["products"][0]["channels"] == ["display", "future"]


def test_response_passes_through_when_no_products() -> None:
    response = {"errors": [{"code": "boom"}]}
    out = _normalize(response)
    assert out == response


def test_response_preserves_non_pricing_fields() -> None:
    response = {
        "products": [
            {
                "product_id": "p1",
                "name": "Holiday Display",
                "channels": ["olv"],
                "pricing_options": [
                    {
                        "pricing_option_id": "po1",
                        "pricing_model": "cpm",
                        "currency": "USD",
                        "fixed_price": 8.0,
                        "min_spend_per_package": 1000,
                    }
                ],
            }
        ]
    }
    out = _normalize(response)
    product = out["products"][0]
    assert product["name"] == "Holiday Display"
    assert product["pricing_options"][0]["min_spend_per_package"] == 1000


# ---------------------------------------------------------------------------
# Pricing-option idempotency (v3-already-shape input)
# ---------------------------------------------------------------------------


def test_normalize_pricing_option_idempotent_on_v3_already_shape() -> None:
    """If a server (or half-migrated buyer) emits v3-shaped pricing in a
    v2.5 envelope, don't double-translate."""
    option = {
        "pricing_option_id": "po1",
        "pricing_model": "cpm",
        "currency": "USD",
        "fixed_price": 10.0,
    }
    out = v2_5_gp._normalize_pricing_option(option)
    assert out["fixed_price"] == 10.0
    assert "rate" not in out
    assert "is_fixed" not in out


def test_normalize_pricing_option_mixed_v2_and_v3_keys_v2_wins() -> None:
    """When both v2 (``rate``) and v3 (``fixed_price``) fields are present —
    the v2.5→v3 normalizer's job is to translate v2 input, so v2 wins.
    Pin the precedence so a future refactor doesn't quietly flip it."""
    option = {
        "pricing_option_id": "po1",
        "pricing_model": "cpm",
        "currency": "USD",
        "rate": 5.0,  # v2 wire shape
        "is_fixed": True,
        "fixed_price": 10.0,  # v3 field also present (server bug or migration race)
    }
    out = v2_5_gp._normalize_pricing_option(option)
    # v2 ``rate`` wins — the function exists to normalize FROM v2.
    assert out["fixed_price"] == 5.0
    assert "rate" not in out
    assert "is_fixed" not in out
    assert "rate" not in out
    assert "is_fixed" not in out
