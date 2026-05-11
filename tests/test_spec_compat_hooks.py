"""Tests for spec_compat_hooks() — built-in pre-v3 / pre-4.4 buyer compat.

Coverage:
- Hook 1 (get_products): buying_mode default
- Hook 2 (sync_creatives): format_id string → structured
- Hook 3 (sync_creatives): asset_type inference (key exact-match + field presence)
- Hook 4 (sync_creatives): image → url demotion when dims missing
- Composition rules: exclude, custom creative_agent_url, default usage
- Context-echo safety: hook must not be called if creatives is absent or wrong type
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.server.spec_compat import (
    CANONICAL_CREATIVE_AGENT_URL,
    PreValidationHooks,
    _coerce_asset,
    _hook_get_products,
    _infer_asset_type,
    _make_sync_creatives_hook,
    spec_compat_hooks,
)


# ---------------------------------------------------------------------------
# Hook 1: get_products — buying_mode default
# ---------------------------------------------------------------------------


def test_get_products_defaults_buying_mode_when_absent() -> None:
    result = _hook_get_products("get_products", {})
    assert result["buying_mode"] == "brief"


def test_get_products_preserves_existing_buying_mode() -> None:
    result = _hook_get_products("get_products", {"buying_mode": "refine"})
    assert result["buying_mode"] == "refine"


def test_get_products_preserves_other_fields() -> None:
    result = _hook_get_products("get_products", {"filters": {"category": "display"}})
    assert result["filters"] == {"category": "display"}
    assert result["buying_mode"] == "brief"


def test_get_products_returns_new_dict_when_modified() -> None:
    args: dict[str, Any] = {}
    result = _hook_get_products("get_products", args)
    assert result is not args


def test_get_products_returns_same_dict_when_unchanged() -> None:
    args: dict[str, Any] = {"buying_mode": "brief"}
    result = _hook_get_products("get_products", args)
    assert result is args


# ---------------------------------------------------------------------------
# Hook 2: format_id string → structured
# ---------------------------------------------------------------------------


_SYNC_HOOK = _make_sync_creatives_hook(CANONICAL_CREATIVE_AGENT_URL)
_MINIMAL_CREATIVE = {"creative_id": "c1", "name": "Test", "assets": {}}


def _run_sync(args: dict[str, Any]) -> dict[str, Any]:
    return _SYNC_HOOK("sync_creatives", args)


def test_format_id_string_wrapped_as_structured() -> None:
    creative = {**_MINIMAL_CREATIVE, "format_id": "display_static"}
    result = _run_sync({"creatives": [creative]})
    fid = result["creatives"][0]["format_id"]
    assert fid == {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display_static"}


def test_format_id_structured_left_unchanged() -> None:
    fid = {"agent_url": "https://example.com", "id": "my_format"}
    creative = {**_MINIMAL_CREATIVE, "format_id": fid}
    result = _run_sync({"creatives": [creative]})
    assert result["creatives"][0]["format_id"] == fid


def test_format_id_uses_custom_agent_url() -> None:
    hook = _make_sync_creatives_hook("https://creative.example.com")
    creative = {**_MINIMAL_CREATIVE, "format_id": "banner"}
    result = hook("sync_creatives", {"creatives": [creative]})
    assert result["creatives"][0]["format_id"]["agent_url"] == "https://creative.example.com"


# ---------------------------------------------------------------------------
# Hook 3: asset_type inference
# ---------------------------------------------------------------------------


def test_infer_asset_type_exact_key_match() -> None:
    for name in ("image", "video", "audio", "text", "url", "html"):
        assert _infer_asset_type(name, {}) == name


def test_infer_asset_type_no_substring_match() -> None:
    # "hero_image" contains "image" but is not an exact type name — must not infer
    assert _infer_asset_type("hero_image", {}) is None
    assert _infer_asset_type("video_thumbnail", {}) is None
    assert _infer_asset_type("cta_url", {}) is None


def test_infer_asset_type_url_plus_dims_gives_image() -> None:
    asset = {"url": "https://cdn.example.com/img.jpg", "width": 300, "height": 250}
    assert _infer_asset_type("banner", asset) == "image"


def test_infer_asset_type_url_without_dims_gives_url() -> None:
    asset = {"url": "https://cdn.example.com/page"}
    assert _infer_asset_type("landing", asset) == "url"


def test_infer_asset_type_content_gives_text() -> None:
    asset = {"content": "Hello world"}
    assert _infer_asset_type("copy", asset) == "text"


def test_infer_asset_type_unknown_ambiguous_returns_none() -> None:
    assert _infer_asset_type("mystery", {}) is None


def test_coerce_asset_infers_and_sets_asset_type() -> None:
    asset = {"url": "https://cdn.example.com/img.jpg", "width": 300, "height": 250}
    result = _coerce_asset("banner", asset)
    assert result["asset_type"] == "image"


def test_coerce_asset_does_not_overwrite_existing_asset_type() -> None:
    asset = {"asset_type": "video", "url": "https://cdn.example.com/video.mp4"}
    result = _coerce_asset("clip", asset)
    assert result["asset_type"] == "video"


def test_sync_hook_infers_asset_type_for_exact_key() -> None:
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"image": {"url": "https://cdn.example.com/img.jpg", "width": 300, "height": 250}},
    }
    result = _run_sync({"creatives": [creative]})
    assert result["creatives"][0]["assets"]["image"]["asset_type"] == "image"


def test_sync_hook_leaves_ambiguous_asset_unchanged() -> None:
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"mystery_slot": {"some_field": "value"}},
    }
    result = _run_sync({"creatives": [creative]})
    asset = result["creatives"][0]["assets"]["mystery_slot"]
    assert "asset_type" not in asset


# ---------------------------------------------------------------------------
# Hook 4: image → url demotion
# ---------------------------------------------------------------------------


def test_coerce_asset_demotes_image_to_url_when_dims_missing() -> None:
    asset = {"asset_type": "image", "url": "https://cdn.example.com/img.jpg"}
    result = _coerce_asset("banner", asset)
    assert result["asset_type"] == "url"
    assert result["url"] == "https://cdn.example.com/img.jpg"


def test_coerce_asset_image_not_demoted_when_dims_present() -> None:
    asset = {"asset_type": "image", "url": "https://cdn.example.com/img.jpg", "width": 300, "height": 250}
    result = _coerce_asset("banner", asset)
    assert result["asset_type"] == "image"
    assert "width" in result


def test_coerce_asset_image_not_demoted_when_url_also_absent() -> None:
    # Structurally invalid — no url, no dims. Leave unchanged; let schema validation report it.
    asset = {"asset_type": "image"}
    result = _coerce_asset("banner", asset)
    assert result["asset_type"] == "image"
    assert "url" not in result


def test_coerce_asset_demotes_image_strips_partial_dims() -> None:
    # Lone width without height — still demotes and strips the stray dim.
    asset = {"asset_type": "image", "url": "https://cdn.example.com/img.jpg", "width": 300}
    result = _coerce_asset("banner", asset)
    assert result["asset_type"] == "url"
    assert "width" not in result
    assert result["url"] == "https://cdn.example.com/img.jpg"


def test_sync_hook_demotes_image_to_url_via_inference_then_demotion() -> None:
    # Key "image" → infer asset_type="image" (hook 3) → dims absent, url present → demote (hook 4)
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"image": {"url": "https://cdn.example.com/img.jpg"}},
    }
    result = _run_sync({"creatives": [creative]})
    assert result["creatives"][0]["assets"]["image"]["asset_type"] == "url"


# ---------------------------------------------------------------------------
# spec_compat_hooks() factory
# ---------------------------------------------------------------------------


def test_spec_compat_hooks_returns_both_hooks_by_default() -> None:
    hooks = spec_compat_hooks()
    assert "get_products" in hooks
    assert "sync_creatives" in hooks


def test_spec_compat_hooks_exclude_get_products() -> None:
    hooks = spec_compat_hooks(exclude={"get_products"})
    assert "get_products" not in hooks
    assert "sync_creatives" in hooks


def test_spec_compat_hooks_exclude_sync_creatives() -> None:
    hooks = spec_compat_hooks(exclude={"sync_creatives"})
    assert "get_products" in hooks
    assert "sync_creatives" not in hooks


def test_spec_compat_hooks_exclude_all() -> None:
    hooks = spec_compat_hooks(exclude={"get_products", "sync_creatives"})
    assert hooks == {}


def test_spec_compat_hooks_unknown_exclude_silently_ignored() -> None:
    hooks = spec_compat_hooks(exclude={"nonexistent_tool"})
    assert "get_products" in hooks
    assert "sync_creatives" in hooks


def test_spec_compat_hooks_custom_agent_url_threaded() -> None:
    hooks = spec_compat_hooks(creative_agent_url="https://creative.example.com")
    sync_hook = hooks["sync_creatives"]
    creative = {**_MINIMAL_CREATIVE, "format_id": "banner"}
    result = sync_hook("sync_creatives", {"creatives": [creative]})
    assert result["creatives"][0]["format_id"]["agent_url"] == "https://creative.example.com"


def test_spec_compat_hooks_spreads_with_adopter_hooks() -> None:
    my_hook = lambda n, a: a  # noqa: E731
    merged = {**spec_compat_hooks(), "create_media_buy": my_hook}
    assert "get_products" in merged
    assert "sync_creatives" in merged
    assert merged["create_media_buy"] is my_hook


def test_spec_compat_hooks_return_type_is_pre_validation_hooks() -> None:
    hooks: PreValidationHooks = spec_compat_hooks()
    assert isinstance(hooks, dict)
    for key, val in hooks.items():
        assert isinstance(key, str)
        assert callable(val)


# ---------------------------------------------------------------------------
# Safety: no-op when args structure is unexpected
# ---------------------------------------------------------------------------


def test_sync_hook_no_creatives_key_returns_args_unchanged() -> None:
    args = {"something_else": 42}
    result = _run_sync(args)
    assert result is args


def test_sync_hook_creatives_not_list_returns_args_unchanged() -> None:
    args = {"creatives": "not_a_list"}
    result = _run_sync(args)
    assert result is args


def test_sync_hook_no_changes_returns_original_dict() -> None:
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"banner": {"asset_type": "image", "url": "https://cdn.example.com/img.jpg", "width": 300, "height": 250}},
    }
    args = {"creatives": [creative]}
    result = _run_sync(args)
    assert result is args


# ---------------------------------------------------------------------------
# Integration: hook wired through create_tool_caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spec_compat_hooks_wired_via_create_tool_caller() -> None:
    from typing import Any

    from adcp.server.base import ADCPHandler, ToolContext
    from adcp.server.mcp_tools import create_tool_caller

    received: list[Any] = []

    class _Handler(ADCPHandler[Any]):
        async def get_products(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            received.append(params)
            return {"products": []}

    hooks = spec_compat_hooks()
    caller = create_tool_caller(_Handler(), "get_products", pre_validation_hook=hooks["get_products"])
    await caller({})  # no buying_mode sent
    assert received[0].get("buying_mode") == "brief"
