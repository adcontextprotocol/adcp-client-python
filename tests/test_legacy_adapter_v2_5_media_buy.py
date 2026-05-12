"""Tests for v2.5 ↔ v3 ``create_media_buy`` / ``update_media_buy`` adapters.

Both tools share the package shape (``creative_ids`` ↔
``creative_assignments``) and the response normalizer; ``create_media_buy``
additionally translates ``brand_manifest`` ↔ ``brand``.
"""

from __future__ import annotations

import logging
from typing import Any

from adcp.compat.legacy import get_legacy_adapter
from adcp.compat.legacy.v2_5 import _media_buy_helpers as helpers
from adcp.compat.legacy.v2_5 import create_media_buy as v2_5_cmb
from adcp.compat.legacy.v2_5 import update_media_buy as v2_5_umb

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_create_media_buy_adapter_registered() -> None:
    a = get_legacy_adapter("2.5", "create_media_buy")
    assert a is not None
    assert a.tool_name == "create_media_buy"
    assert a.normalize_response is not None


def test_update_media_buy_adapter_registered() -> None:
    a = get_legacy_adapter("2.5", "update_media_buy")
    assert a is not None
    assert a.tool_name == "update_media_buy"
    assert a.normalize_response is not None


# ---------------------------------------------------------------------------
# create_media_buy request: brand_manifest → brand
# ---------------------------------------------------------------------------


def test_create_media_buy_brand_manifest_becomes_brand_domain() -> None:
    out = v2_5_cmb.adapt_request({"brand_manifest": "https://acme.example.com/"})
    assert out["brand"] == {"domain": "acme.example.com"}
    assert "brand_manifest" not in out


def test_create_media_buy_brand_manifest_with_path_extracts_hostname() -> None:
    """Regression for #677: brand_manifest URL with a path must extract only
    the hostname so the result satisfies BrandReference.domain's regex."""
    out = v2_5_cmb.adapt_request({"brand_manifest": "https://acme.com/.well-known/brand.json"})
    assert out["brand"] == {"domain": "acme.com"}
    assert "brand_manifest" not in out


def test_create_media_buy_brand_manifest_with_port_drops_port() -> None:
    """Port numbers are stripped; only the hostname reaches brand.domain."""
    out = v2_5_cmb.adapt_request({"brand_manifest": "https://acme.com:8443/.well-known/brand.json"})
    assert out["brand"] == {"domain": "acme.com"}


def test_create_media_buy_brand_wins_over_manifest_when_both_present() -> None:
    out = v2_5_cmb.adapt_request(
        {
            "brand_manifest": "https://acme.example.com",
            "brand": {"domain": "explicit.example.com"},
        }
    )
    assert out["brand"] == {"domain": "explicit.example.com"}


def test_create_media_buy_no_brand_manifest_passes_through() -> None:
    out = v2_5_cmb.adapt_request({"buyer_ref": "br-1"})
    assert "brand" not in out
    assert "brand_manifest" not in out


# ---------------------------------------------------------------------------
# create_media_buy request: brand_manifest as inline object (#684)
# ---------------------------------------------------------------------------


def test_create_media_buy_brand_manifest_inline_object_with_url() -> None:
    """Inline BrandManifest object with url extracts hostname for brand.domain."""
    out = v2_5_cmb.adapt_request(
        {"brand_manifest": {"url": "https://acme.example.com", "name": "ACME Corp"}}
    )
    assert out["brand"] == {"domain": "acme.example.com"}
    assert "brand_manifest" not in out


def test_create_media_buy_brand_manifest_inline_object_url_with_path() -> None:
    out = v2_5_cmb.adapt_request(
        {
            "brand_manifest": {
                "url": "https://acme.com/.well-known/brand.json",
                "name": "ACME Corp",
            }
        }
    )
    assert out["brand"] == {"domain": "acme.com"}
    assert "brand_manifest" not in out


def test_create_media_buy_brand_manifest_inline_object_no_url_skips_brand() -> None:
    """Inline object without url (spec-valid) omits brand; no exception raised."""
    out = v2_5_cmb.adapt_request({"brand_manifest": {"name": "Great Value"}})
    assert "brand" not in out
    assert "brand_manifest" not in out


def test_create_media_buy_brand_manifest_inline_object_brand_wins_when_both_present() -> None:
    out = v2_5_cmb.adapt_request(
        {
            "brand_manifest": {"url": "https://acme.example.com", "name": "ACME Corp"},
            "brand": {"domain": "explicit.example.com"},
        }
    )
    assert out["brand"] == {"domain": "explicit.example.com"}
    assert "brand_manifest" not in out


# ---------------------------------------------------------------------------
# create_media_buy: brand_manifest non-standard-path warning (issue #683)
# ---------------------------------------------------------------------------

_MB_HELPERS_LOGGER = "adcp.compat.legacy.v2_5._media_buy_helpers"


def test_create_media_buy_cdn_url_warns(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """CDN brand_manifest with non-standard path emits a WARNING about the
    information loss so operators can debug downstream 404s."""
    import adcp.compat.legacy.v2_5._media_buy_helpers as _mbh

    monkeypatch.setattr(_mbh, "_brand_manifest_path_warned", set())
    with caplog.at_level(logging.WARNING, logger=_MB_HELPERS_LOGGER):
        out = v2_5_cmb.adapt_request(
            {"brand_manifest": "https://cdn.acmecorp.com/brand-manifest.json"}
        )
    assert out["brand"] == {"domain": "cdn.acmecorp.com"}
    records = [r for r in caplog.records if r.name == _MB_HELPERS_LOGGER]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "non-standard path" in msg
    assert "cdn.acmecorp.com" in msg


def test_create_media_buy_well_known_path_no_warning(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Standard /.well-known/brand.json path does NOT warn."""
    import adcp.compat.legacy.v2_5._media_buy_helpers as _mbh

    monkeypatch.setattr(_mbh, "_brand_manifest_path_warned", set())
    with caplog.at_level(logging.WARNING, logger=_MB_HELPERS_LOGGER):
        v2_5_cmb.adapt_request(
            {"brand_manifest": "https://acme.com/.well-known/brand.json"}
        )
    assert not any(r.name == _MB_HELPERS_LOGGER for r in caplog.records)


def test_create_media_buy_bare_domain_no_warning(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Bare domain brand_manifest (no scheme) does not false-positive."""
    import adcp.compat.legacy.v2_5._media_buy_helpers as _mbh

    monkeypatch.setattr(_mbh, "_brand_manifest_path_warned", set())
    with caplog.at_level(logging.WARNING, logger=_MB_HELPERS_LOGGER):
        v2_5_cmb.adapt_request({"brand_manifest": "acme.com"})
    assert not any(r.name == _MB_HELPERS_LOGGER for r in caplog.records)


def test_create_media_buy_cdn_url_warns_once_dedup(caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Same CDN URL repeated across requests only logs one warning (dedup)."""
    import adcp.compat.legacy.v2_5._media_buy_helpers as _mbh

    monkeypatch.setattr(_mbh, "_brand_manifest_path_warned", set())
    url = "https://cdn.acmecorp.com/brand-manifest.json"
    with caplog.at_level(logging.WARNING, logger=_MB_HELPERS_LOGGER):
        v2_5_cmb.adapt_request({"brand_manifest": url})
        v2_5_cmb.adapt_request({"brand_manifest": url})
    records = [r for r in caplog.records if r.name == _MB_HELPERS_LOGGER]
    assert len(records) == 1


# ---------------------------------------------------------------------------
# Package request: creative_ids → creative_assignments (both tools)
# ---------------------------------------------------------------------------


def test_package_creative_ids_become_assignments_on_create() -> None:
    out = v2_5_cmb.adapt_request(
        {
            "packages": [
                {"pkg_id": "p1", "creative_ids": ["c1", "c2"]},
            ]
        }
    )
    pkg = out["packages"][0]
    assert pkg["creative_assignments"] == [
        {"creative_id": "c1"},
        {"creative_id": "c2"},
    ]
    assert "creative_ids" not in pkg


def test_package_creative_ids_become_assignments_on_update() -> None:
    out = v2_5_umb.adapt_request(
        {
            "packages": [
                {"pkg_id": "p1", "creative_ids": ["c1"]},
            ]
        }
    )
    pkg = out["packages"][0]
    assert pkg["creative_assignments"] == [{"creative_id": "c1"}]
    assert "creative_ids" not in pkg


def test_package_empty_creative_ids_becomes_empty_assignments() -> None:
    out = v2_5_umb.adapt_request({"packages": [{"pkg_id": "p1", "creative_ids": []}]})
    assert out["packages"][0]["creative_assignments"] == []


def test_package_buyer_ref_preserved() -> None:
    """v3 tolerates ``buyer_ref`` via additionalProperties; preserve so
    adopters who key idempotency on it keep working."""
    out = v2_5_cmb.adapt_request({"packages": [{"pkg_id": "p1", "buyer_ref": "br-pkg"}]})
    assert out["packages"][0]["buyer_ref"] == "br-pkg"


def test_package_non_dict_entries_pass_through() -> None:
    out = v2_5_cmb.adapt_request({"packages": [None, "not_a_dict"]})
    assert out["packages"] == [None, "not_a_dict"]


def test_update_media_buy_no_brand_translation() -> None:
    """update_media_buy doesn't translate brand_manifest — verify it
    passes through unchanged (not silently dropped or rewritten)."""
    out = v2_5_umb.adapt_request({"brand_manifest": "https://example.com"})
    # Updates don't translate brand; if the buyer sends it, leave it.
    assert out["brand_manifest"] == "https://example.com"
    assert "brand" not in out


# ---------------------------------------------------------------------------
# Response normalization: creative_assignments → creative_ids (v3 → v2.5)
# ---------------------------------------------------------------------------


def _normalize(adapter: Any, response: dict[str, Any]) -> dict[str, Any]:
    return adapter.normalize_response(response)


def test_response_assignments_collapse_to_creative_ids() -> None:
    response = {
        "packages": [
            {
                "pkg_id": "p1",
                "creative_assignments": [
                    {"creative_id": "c1"},
                    {"creative_id": "c2"},
                ],
            }
        ]
    }
    out = _normalize(v2_5_cmb.ADAPTER, response)
    pkg = out["packages"][0]
    assert pkg["creative_ids"] == ["c1", "c2"]
    assert "creative_assignments" not in pkg


def test_response_weight_and_placement_ids_dropped() -> None:
    """v3-only fields ``weight`` and ``placement_ids`` are lossy in
    v2.5 — they're dropped silently. v2.5 buyers have no field to
    surface them on."""
    response = {
        "packages": [
            {
                "pkg_id": "p1",
                "creative_assignments": [
                    {"creative_id": "c1", "weight": 50, "placement_ids": ["p1"]},
                ],
            }
        ]
    }
    out = _normalize(v2_5_cmb.ADAPTER, response)
    assert out["packages"][0]["creative_ids"] == ["c1"]


def test_response_null_array_fields_coerced_to_absent() -> None:
    """Some servers emit explicit ``null`` for optional arrays; convert
    to absent so consumers can use safe membership checks."""
    response = {"packages": [{"pkg_id": "p1", "creative_assignments": None, "products": None}]}
    out = _normalize(v2_5_cmb.ADAPTER, response)
    pkg = out["packages"][0]
    assert "creative_assignments" not in pkg
    assert "products" not in pkg


def test_response_preserves_other_package_fields() -> None:
    response = {
        "packages": [
            {
                "pkg_id": "p1",
                "status": "active",
                "creative_assignments": [{"creative_id": "c1"}],
                "metadata": {"foo": "bar"},
            }
        ]
    }
    out = _normalize(v2_5_cmb.ADAPTER, response)
    pkg = out["packages"][0]
    assert pkg["status"] == "active"
    assert pkg["metadata"] == {"foo": "bar"}


def test_response_no_packages_passes_through() -> None:
    response = {"media_buy_id": "mb-1"}
    out = _normalize(v2_5_cmb.ADAPTER, response)
    assert out == response


def test_response_non_dict_packages_pass_through() -> None:
    response = {"packages": [None, "not_a_dict", {"pkg_id": "p1"}]}
    out = _normalize(v2_5_umb.ADAPTER, response)
    assert out["packages"] == [None, "not_a_dict", {"pkg_id": "p1"}]


def test_response_normalizer_shared_between_tools() -> None:
    """Both adapters use the same normalizer — verify they're literally
    the same function (no accidental divergent copies)."""
    assert v2_5_cmb.ADAPTER.normalize_response is helpers.normalize_media_buy_response
    assert v2_5_umb.ADAPTER.normalize_response is helpers.normalize_media_buy_response


# ---------------------------------------------------------------------------
# Mutation safety
# ---------------------------------------------------------------------------


def test_adapt_request_does_not_mutate_input() -> None:
    payload = {
        "brand_manifest": "https://example.com",
        "packages": [{"pkg_id": "p1", "creative_ids": ["c1"]}],
    }
    original = {
        "brand_manifest": "https://example.com",
        "packages": [{"pkg_id": "p1", "creative_ids": ["c1"]}],
    }
    v2_5_cmb.adapt_request(payload)
    assert payload == original
