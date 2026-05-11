"""Tests for the legacy-adapter registry and the v2.5 sync_creatives adapter."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.compat.legacy import (
    LEGACY_ADAPTER_VERSIONS,
    AdapterPair,
    _reset_registry_for_tests,
    get_legacy_adapter,
    list_legacy_adapter_tools,
    register_adapter,
)
from adcp.compat.legacy.v2_5 import sync_creatives as v2_5_sync_creatives

# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------


def test_legacy_adapter_versions_contains_2_5() -> None:
    assert "2.5" in LEGACY_ADAPTER_VERSIONS


def test_get_legacy_adapter_returns_registered_pair() -> None:
    adapter = get_legacy_adapter("2.5", "sync_creatives")
    assert adapter is not None
    assert adapter.tool_name == "sync_creatives"
    assert callable(adapter.adapt_request)


def test_get_legacy_adapter_unknown_tool_returns_none() -> None:
    assert get_legacy_adapter("2.5", "no_such_tool") is None


def test_list_legacy_adapter_tools_lists_v2_5_tools() -> None:
    tools = list_legacy_adapter_tools("2.5")
    assert "sync_creatives" in tools


def test_register_adapter_rejects_unknown_version() -> None:
    pair = AdapterPair(tool_name="x", adapt_request=lambda p: p)
    with pytest.raises(ValueError, match="not in LEGACY_ADAPTER_VERSIONS"):
        register_adapter("9.9", pair)


def test_register_adapter_rejects_overwrite() -> None:
    """Re-registering a different pair for the same key raises."""
    _reset_registry_for_tests()
    # Reload the v2.5 registrations so other tests don't see an empty registry.
    try:
        # Trigger v2.5 load via the public surface.
        get_legacy_adapter("2.5", "sync_creatives")

        rogue = AdapterPair(
            tool_name="sync_creatives",
            adapt_request=lambda p: {**p, "rogue": True},
        )
        with pytest.raises(ValueError, match="already registered"):
            register_adapter("2.5", rogue)
    finally:
        _reset_registry_for_tests()


def test_register_adapter_idempotent_on_same_pair() -> None:
    """Re-registering the same pair object is a no-op (import retry safe)."""
    pair = get_legacy_adapter("2.5", "sync_creatives")
    assert pair is not None
    register_adapter("2.5", pair)  # must not raise


# ---------------------------------------------------------------------------
# v2.5 sync_creatives adapter — wire-shape coercions
# ---------------------------------------------------------------------------


_CANONICAL_URL = "https://creative.adcontextprotocol.org"
_MIN_CREATIVE: dict[str, Any] = {"creative_id": "c1", "name": "Test"}


def _adapt(payload: dict[str, Any]) -> dict[str, Any]:
    return v2_5_sync_creatives.adapt_request(payload)


def test_v2_5_format_id_string_wrapped_as_structured() -> None:
    payload = {"creatives": [{**_MIN_CREATIVE, "format_id": "display_300x250"}]}
    out = _adapt(payload)
    assert out["creatives"][0]["format_id"] == {
        "agent_url": _CANONICAL_URL,
        "id": "display_300x250",
    }


def test_v2_5_format_id_structured_left_unchanged() -> None:
    structured = {"agent_url": "https://example.com", "id": "x"}
    payload = {"creatives": [{**_MIN_CREATIVE, "format_id": structured}]}
    out = _adapt(payload)
    assert out["creatives"][0]["format_id"] == structured


def test_v2_5_asset_type_inferred_from_key() -> None:
    payload = {
        "creatives": [
            {
                **_MIN_CREATIVE,
                "format_id": {"agent_url": _CANONICAL_URL, "id": "x"},
                "assets": {
                    "video": {"url": "https://cdn.example.com/v.mp4"},
                    "text": {"content": "Hello"},
                },
            }
        ]
    }
    out = _adapt(payload)
    assets = out["creatives"][0]["assets"]
    assert assets["video"]["asset_type"] == "video"
    assert assets["text"]["asset_type"] == "text"


def test_v2_5_image_with_dims_kept_as_image() -> None:
    payload = {
        "creatives": [
            {
                **_MIN_CREATIVE,
                "format_id": {"agent_url": _CANONICAL_URL, "id": "x"},
                "assets": {
                    "banner": {
                        "asset_type": "image",
                        "url": "https://cdn.example.com/i.jpg",
                        "width": 300,
                        "height": 250,
                    }
                },
            }
        ]
    }
    out = _adapt(payload)
    assert out["creatives"][0]["assets"]["banner"]["asset_type"] == "image"


def test_v2_5_image_without_dims_demoted_to_url() -> None:
    payload = {
        "creatives": [
            {
                **_MIN_CREATIVE,
                "format_id": {"agent_url": _CANONICAL_URL, "id": "x"},
                "assets": {
                    "banner": {
                        "asset_type": "image",
                        "url": "https://cdn.example.com/i.jpg",
                    }
                },
            }
        ]
    }
    out = _adapt(payload)
    asset = out["creatives"][0]["assets"]["banner"]
    assert asset["asset_type"] == "url"
    assert "width" not in asset


def test_v2_5_image_partial_dims_demoted_strips_orphan_dim() -> None:
    payload = {
        "creatives": [
            {
                **_MIN_CREATIVE,
                "format_id": {"agent_url": _CANONICAL_URL, "id": "x"},
                "assets": {
                    "banner": {
                        "asset_type": "image",
                        "url": "https://cdn.example.com/i.jpg",
                        "width": 300,
                    }
                },
            }
        ]
    }
    out = _adapt(payload)
    asset = out["creatives"][0]["assets"]["banner"]
    assert asset["asset_type"] == "url"
    assert "width" not in asset


def test_v2_5_image_without_url_left_invalid() -> None:
    """No url, no dims → structurally unusable, leave for current-schema
    validation to reject."""
    payload = {
        "creatives": [
            {
                **_MIN_CREATIVE,
                "format_id": {"agent_url": _CANONICAL_URL, "id": "x"},
                "assets": {"banner": {"asset_type": "image"}},
            }
        ]
    }
    out = _adapt(payload)
    asset = out["creatives"][0]["assets"]["banner"]
    assert asset["asset_type"] == "image"  # unchanged
    assert "url" not in asset


def test_v2_5_no_substring_key_inference() -> None:
    """``hero_image`` contains 'image' but is not the type — leave unchanged."""
    payload = {
        "creatives": [
            {
                **_MIN_CREATIVE,
                "format_id": {"agent_url": _CANONICAL_URL, "id": "x"},
                "assets": {"hero_image": {"some_field": "value"}},
            }
        ]
    }
    out = _adapt(payload)
    assert "asset_type" not in out["creatives"][0]["assets"]["hero_image"]


def test_v2_5_adapter_does_not_mutate_input() -> None:
    """Idempotency under retry: callers can re-adapt the same payload."""
    payload = {"creatives": [{**_MIN_CREATIVE, "format_id": "display"}]}
    original = {
        "creatives": [{**_MIN_CREATIVE, "format_id": "display"}],
    }
    _adapt(payload)
    assert payload == original


def test_v2_5_adapter_handles_missing_creatives_key() -> None:
    out = _adapt({"adcp_major_version": 2})
    assert out == {"adcp_major_version": 2}


def test_v2_5_adapter_handles_non_list_creatives() -> None:
    out = _adapt({"creatives": "not_a_list"})
    assert out == {"creatives": "not_a_list"}


def test_v2_5_adapter_normalize_response_unset() -> None:
    """v2.5 ``sync_creatives`` doesn't need a reverse rewrite — the
    response shape is unchanged. Adapter declares this by leaving
    ``normalize_response=None``.
    """
    adapter = get_legacy_adapter("2.5", "sync_creatives")
    assert adapter is not None
    assert adapter.normalize_response is None
