"""Tests for spec_compat_hooks() — built-in pre-v3 / pre-4.4 buyer compat.

Tests exercise the public ``spec_compat_hooks()`` factory only. End-to-end
hook calls are validated against the generated Pydantic schemas
(:class:`adcp.types.GetProductsRequest`,
:class:`adcp.types.SyncCreativesRequest`) to prove the coerced output is
actually 4.4-spec-valid, not just structurally plausible.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.server.spec_compat import (
    CANONICAL_CREATIVE_AGENT_URL,
    PreValidationHooks,
    spec_compat_hooks,
)
from adcp.types import GetProductsRequest, SyncCreativesRequest

_MINIMAL_CREATIVE: dict[str, Any] = {"creative_id": "c1", "name": "Test", "assets": {}}


def _gp(hooks: PreValidationHooks, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke the get_products hook through the public factory dict."""
    return hooks["get_products"]("get_products", args)


def _sc(hooks: PreValidationHooks, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke the sync_creatives hook through the public factory dict."""
    return hooks["sync_creatives"]("sync_creatives", args)


def _wrap_sync_payload(creatives: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a creative list in the minimal SyncCreativesRequest envelope.

    Used by model_validate() proofs — every required envelope field
    (account, idempotency_key, adcp_major_version) is filled with the
    minimum-valid value so the test asserts on the creative shape, not
    the envelope.
    """
    return {
        "adcp_major_version": 4,
        "account": {"account_id": "acc-1"},
        "idempotency_key": "idem-1234567890abcdef",
        "creatives": creatives,
    }


# ---------------------------------------------------------------------------
# Hook 1: get_products — buying_mode default
# ---------------------------------------------------------------------------


def test_get_products_defaults_buying_mode_when_absent() -> None:
    hooks = spec_compat_hooks()
    result = _gp(hooks, {"brief": "test"})
    assert result["buying_mode"] == "brief"


def test_get_products_preserves_existing_buying_mode() -> None:
    hooks = spec_compat_hooks()
    result = _gp(hooks, {"buying_mode": "refine", "refine": {}})
    assert result["buying_mode"] == "refine"


def test_get_products_preserves_other_fields() -> None:
    hooks = spec_compat_hooks()
    result = _gp(hooks, {"filters": {"category": "display"}})
    assert result["filters"] == {"category": "display"}
    assert result["buying_mode"] == "brief"


def test_get_products_returns_same_dict_when_unchanged() -> None:
    hooks = spec_compat_hooks()
    args: dict[str, Any] = {"buying_mode": "brief", "brief": "x"}
    result = _gp(hooks, args)
    assert result is args


# ---------------------------------------------------------------------------
# Hook 2: sync_creatives — format_id string → structured
# ---------------------------------------------------------------------------


def test_format_id_string_wrapped_as_structured() -> None:
    hooks = spec_compat_hooks()
    creative = {**_MINIMAL_CREATIVE, "format_id": "display_static"}
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["format_id"] == {
        "agent_url": CANONICAL_CREATIVE_AGENT_URL,
        "id": "display_static",
    }


def test_format_id_structured_left_unchanged() -> None:
    hooks = spec_compat_hooks()
    fid = {"agent_url": "https://example.com", "id": "my_format"}
    creative = {**_MINIMAL_CREATIVE, "format_id": fid}
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["format_id"] == fid


def test_format_id_uses_custom_agent_url() -> None:
    hooks = spec_compat_hooks(creative_agent_url="https://creative.example.com")
    creative = {**_MINIMAL_CREATIVE, "format_id": "banner"}
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["format_id"]["agent_url"] == "https://creative.example.com"


# ---------------------------------------------------------------------------
# Hook 3: sync_creatives — asset_type inference (end-to-end through the hook)
# ---------------------------------------------------------------------------


def test_asset_type_inferred_from_exact_key() -> None:
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {
            "image": {
                "url": "https://cdn.example.com/img.jpg",
                "width": 300,
                "height": 250,
            },
        },
    }
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["assets"]["image"]["asset_type"] == "image"


def test_asset_type_inferred_for_each_known_type() -> None:
    # Inference is exact key-match only, so any spec-defined type name
    # used as the asset key should infer that asset_type. Provide minimal
    # fields per type so the inference fires from the key, not field presence.
    hooks = spec_compat_hooks()
    fid = {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"}
    assets = {
        "video": {"url": "https://cdn.example.com/v.mp4"},
        "audio": {"url": "https://cdn.example.com/a.mp3"},
        "text": {"content": "Hello"},
        "html": {"content": "<div/>"},
    }
    creative = {**_MINIMAL_CREATIVE, "format_id": fid, "assets": assets}
    result = _sc(hooks, {"creatives": [creative]})
    out_assets = result["creatives"][0]["assets"]
    assert out_assets["video"]["asset_type"] == "video"
    assert out_assets["audio"]["asset_type"] == "audio"
    assert out_assets["text"]["asset_type"] == "text"
    assert out_assets["html"]["asset_type"] == "html"


def test_asset_type_no_substring_inference() -> None:
    # "hero_image" contains "image" but is not an exact type name — the hook
    # must not infer "image" from a substring match. Hook leaves it unchanged
    # so schema validation can report the missing discriminator.
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"hero_image": {"some_field": "value"}},
    }
    result = _sc(hooks, {"creatives": [creative]})
    assert "asset_type" not in result["creatives"][0]["assets"]["hero_image"]


def test_asset_type_inferred_from_url_plus_dims() -> None:
    # Unknown key, but url+width+height → image.
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {
            "banner_300x250": {
                "url": "https://cdn.example.com/i.jpg",
                "width": 300,
                "height": 250,
            },
        },
    }
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["assets"]["banner_300x250"]["asset_type"] == "image"


def test_asset_type_inferred_from_url_without_dims_gives_url() -> None:
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"landing": {"url": "https://example.com/page"}},
    }
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["assets"]["landing"]["asset_type"] == "url"


def test_asset_type_inferred_from_content_gives_text() -> None:
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"copy": {"content": "Hello world"}},
    }
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["assets"]["copy"]["asset_type"] == "text"


def test_asset_type_ambiguous_left_unchanged() -> None:
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"mystery_slot": {"some_field": "value"}},
    }
    result = _sc(hooks, {"creatives": [creative]})
    assert "asset_type" not in result["creatives"][0]["assets"]["mystery_slot"]


def test_asset_type_existing_not_overwritten() -> None:
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {
            "clip": {"asset_type": "video", "url": "https://cdn.example.com/v.mp4"},
        },
    }
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["assets"]["clip"]["asset_type"] == "video"


# ---------------------------------------------------------------------------
# Hook 4: sync_creatives — image → url demotion when dims missing
# ---------------------------------------------------------------------------


def test_image_demoted_to_url_when_dims_missing() -> None:
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {
            "banner": {
                "asset_type": "image",
                "url": "https://cdn.example.com/i.jpg",
            },
        },
    }
    result = _sc(hooks, {"creatives": [creative]})
    asset = result["creatives"][0]["assets"]["banner"]
    assert asset["asset_type"] == "url"
    assert asset["url"] == "https://cdn.example.com/i.jpg"


def test_image_not_demoted_when_dims_present() -> None:
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {
            "banner": {
                "asset_type": "image",
                "url": "https://cdn.example.com/i.jpg",
                "width": 300,
                "height": 250,
            },
        },
    }
    result = _sc(hooks, {"creatives": [creative]})
    asset = result["creatives"][0]["assets"]["banner"]
    assert asset["asset_type"] == "image"
    assert asset["width"] == 300


def test_image_not_demoted_when_url_absent() -> None:
    # Structurally invalid (no url, no dims). Hook leaves it unchanged so
    # schema validation reports the missing fields.
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"broken": {"asset_type": "image"}},
    }
    result = _sc(hooks, {"creatives": [creative]})
    asset = result["creatives"][0]["assets"]["broken"]
    assert asset["asset_type"] == "image"
    assert "url" not in asset


def test_image_demotion_strips_partial_dims() -> None:
    # Lone width without height — still demote and strip the stray dim.
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {
            "banner": {
                "asset_type": "image",
                "url": "https://cdn.example.com/i.jpg",
                "width": 300,
            },
        },
    }
    result = _sc(hooks, {"creatives": [creative]})
    asset = result["creatives"][0]["assets"]["banner"]
    assert asset["asset_type"] == "url"
    assert "width" not in asset


def test_image_demoted_via_inference_then_demotion() -> None:
    # Key "image" → infer asset_type='image' (hook 3) → dims absent, url
    # present → demote to 'url' (hook 4). Composition test.
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {"image": {"url": "https://cdn.example.com/i.jpg"}},
    }
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["assets"]["image"]["asset_type"] == "url"


# ---------------------------------------------------------------------------
# Schema-validation proof: hook output passes generated Pydantic models.
# These tests are the contract that spec_compat_hooks() produces 4.4-valid
# requests, not just dicts that *look* right.
# ---------------------------------------------------------------------------


def test_get_products_hook_output_passes_pydantic_validation() -> None:
    """Pre-v3 buyer payload (no buying_mode) is 4.4-valid after the hook."""
    hooks = spec_compat_hooks()
    # Pre-v3 buyer omits buying_mode; pairs with a 'brief' string for brief mode.
    pre_v3_payload = {"adcp_major_version": 4, "brief": "Sponsorship Q4"}
    coerced = _gp(hooks, pre_v3_payload)
    # Must not raise: the hook produced a valid GetProductsRequest payload.
    model = GetProductsRequest.model_validate(coerced)
    assert model.buying_mode is not None
    assert model.buying_mode.value == "brief"


def test_sync_creatives_hook_output_passes_pydantic_validation_format_id_wrap() -> None:
    """Pre-4.4 buyer (bare format_id string) coerces to a 4.4-valid request."""
    hooks = spec_compat_hooks()
    pre_44_creative = {
        "creative_id": "c1",
        "name": "Banner",
        "format_id": "display_300x250",  # bare string — pre-4.4 shape
        "assets": {
            "image": {
                "asset_type": "image",
                "url": "https://cdn.example.com/i.jpg",
                "width": 300,
                "height": 250,
            },
        },
    }
    payload = _wrap_sync_payload([pre_44_creative])
    coerced = _sc(hooks, payload)
    model = SyncCreativesRequest.model_validate(coerced)
    # The wrapped format_id surfaced on the validated model.
    coerced_creatives = coerced["creatives"]
    assert coerced_creatives[0]["format_id"] == {
        "agent_url": CANONICAL_CREATIVE_AGENT_URL,
        "id": "display_300x250",
    }
    # Round-trip back to dict survives validation, confirming the discriminator
    # picked the structured FormatId variant.
    assert model.creatives is not None


def test_sync_creatives_hook_output_passes_pydantic_validation_inference_path() -> None:
    """Pre-4.4 buyer (missing asset_type, key-name as type hint) → 4.4-valid."""
    hooks = spec_compat_hooks()
    pre_44_creative = {
        "creative_id": "c1",
        "name": "Banner",
        "format_id": "display_300x250",
        "assets": {
            # No asset_type discriminator; key 'image' is the type hint.
            "image": {
                "url": "https://cdn.example.com/i.jpg",
                "width": 300,
                "height": 250,
            },
        },
    }
    payload = _wrap_sync_payload([pre_44_creative])
    coerced = _sc(hooks, payload)
    SyncCreativesRequest.model_validate(coerced)


def test_sync_creatives_hook_output_passes_pydantic_validation_demotion_path() -> None:
    """Pre-4.4 'image' asset without dims demotes to 'url' and validates."""
    hooks = spec_compat_hooks()
    pre_44_creative = {
        "creative_id": "c1",
        "name": "Landing",
        "format_id": "display_static",
        "assets": {
            # Key 'image' → asset_type='image' (inference) → no dims → demote
            # to 'url'. Output should be url-typed.
            "image": {"url": "https://example.com/landing"},
        },
    }
    payload = _wrap_sync_payload([pre_44_creative])
    coerced = _sc(hooks, payload)
    SyncCreativesRequest.model_validate(coerced)
    assert coerced["creatives"][0]["assets"]["image"]["asset_type"] == "url"


# ---------------------------------------------------------------------------
# Factory: exclude / custom agent_url / type / unknown-exclude warning
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


def test_spec_compat_hooks_unknown_exclude_emits_warning() -> None:
    with pytest.warns(UserWarning, match="unknown hook name"):
        hooks = spec_compat_hooks(exclude={"sync_creative"})  # typo
    # Both real hooks remain — typo did not silently opt out.
    assert "get_products" in hooks
    assert "sync_creatives" in hooks


def test_spec_compat_hooks_unknown_exclude_alongside_real_name() -> None:
    # Mixed valid + invalid: warn on the typo, still opt out the real one.
    with pytest.warns(UserWarning, match=r"\['nonexistent'\]"):
        hooks = spec_compat_hooks(exclude={"sync_creatives", "nonexistent"})
    assert "sync_creatives" not in hooks
    assert "get_products" in hooks


def test_spec_compat_hooks_custom_agent_url_threaded() -> None:
    hooks = spec_compat_hooks(creative_agent_url="https://creative.example.com")
    creative = {**_MINIMAL_CREATIVE, "format_id": "banner"}
    result = _sc(hooks, {"creatives": [creative]})
    assert result["creatives"][0]["format_id"]["agent_url"] == "https://creative.example.com"


def test_spec_compat_hooks_spreads_with_adopter_hooks() -> None:
    def my_hook(name: str, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
        return args

    merged: PreValidationHooks = {**spec_compat_hooks(), "create_media_buy": my_hook}
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
# Safety: weird-args no-op
# ---------------------------------------------------------------------------


def test_sync_hook_no_creatives_key_returns_args_unchanged() -> None:
    hooks = spec_compat_hooks()
    args: dict[str, Any] = {"something_else": 42}
    result = _sc(hooks, args)
    assert result is args


def test_sync_hook_creatives_not_list_returns_args_unchanged() -> None:
    hooks = spec_compat_hooks()
    args: dict[str, Any] = {"creatives": "not_a_list"}
    result = _sc(hooks, args)
    assert result is args


def test_sync_hook_no_changes_returns_original_dict() -> None:
    hooks = spec_compat_hooks()
    creative = {
        **_MINIMAL_CREATIVE,
        "format_id": {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display"},
        "assets": {
            "banner": {
                "asset_type": "image",
                "url": "https://cdn.example.com/i.jpg",
                "width": 300,
                "height": 250,
            },
        },
    }
    args = {"creatives": [creative]}
    result = _sc(hooks, args)
    assert result is args


# ---------------------------------------------------------------------------
# Integration: hooks wired through create_tool_caller (the path serve() uses).
# These exercise the full dispatcher path: handler receives the coerced
# params after the hook has run, validating the wire contract end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tool_caller_get_products_applies_buying_mode_default() -> None:
    from adcp.server.base import ADCPHandler, ToolContext
    from adcp.server.mcp_tools import create_tool_caller

    received: list[dict[str, Any]] = []

    class _Handler(ADCPHandler[Any]):
        async def get_products(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            received.append(params)
            return {"products": []}

    hooks = spec_compat_hooks()
    caller = create_tool_caller(
        _Handler(),
        "get_products",
        pre_validation_hook=hooks["get_products"],
    )
    await caller({"brief": "Q4 sponsorship"})  # no buying_mode sent
    assert received[0]["buying_mode"] == "brief"
    assert received[0]["brief"] == "Q4 sponsorship"


@pytest.mark.asyncio
async def test_create_tool_caller_sync_creatives_wraps_format_id_string() -> None:
    from adcp.server.base import ADCPHandler, ToolContext
    from adcp.server.mcp_tools import create_tool_caller

    received: list[dict[str, Any]] = []

    class _Handler(ADCPHandler[Any]):
        async def sync_creatives(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            received.append(params)
            return {"creatives": []}

    hooks = spec_compat_hooks()
    caller = create_tool_caller(
        _Handler(),
        "sync_creatives",
        pre_validation_hook=hooks["sync_creatives"],
    )
    pre_44_payload = _wrap_sync_payload(
        [
            {
                "creative_id": "c1",
                "name": "Banner",
                "format_id": "display_300x250",  # bare string
                "assets": {
                    "image": {
                        "asset_type": "image",
                        "url": "https://cdn.example.com/i.jpg",
                        "width": 300,
                        "height": 250,
                    },
                },
            },
        ],
    )
    await caller(pre_44_payload)
    assert received[0]["creatives"][0]["format_id"] == {
        "agent_url": CANONICAL_CREATIVE_AGENT_URL,
        "id": "display_300x250",
    }


@pytest.mark.asyncio
async def test_create_tool_caller_sync_creatives_infers_asset_type() -> None:
    from adcp.server.base import ADCPHandler, ToolContext
    from adcp.server.mcp_tools import create_tool_caller

    received: list[dict[str, Any]] = []

    class _Handler(ADCPHandler[Any]):
        async def sync_creatives(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            received.append(params)
            return {"creatives": []}

    hooks = spec_compat_hooks()
    caller = create_tool_caller(
        _Handler(),
        "sync_creatives",
        pre_validation_hook=hooks["sync_creatives"],
    )
    pre_44_payload = _wrap_sync_payload(
        [
            {
                "creative_id": "c1",
                "name": "Banner",
                "format_id": {
                    "agent_url": CANONICAL_CREATIVE_AGENT_URL,
                    "id": "display",
                },
                "assets": {
                    # Missing asset_type; key 'image' is the type hint.
                    "image": {
                        "url": "https://cdn.example.com/i.jpg",
                        "width": 300,
                        "height": 250,
                    },
                },
            },
        ],
    )
    await caller(pre_44_payload)
    assert received[0]["creatives"][0]["assets"]["image"]["asset_type"] == "image"


@pytest.mark.asyncio
async def test_create_tool_caller_sync_creatives_demotes_image_to_url() -> None:
    from adcp.server.base import ADCPHandler, ToolContext
    from adcp.server.mcp_tools import create_tool_caller

    received: list[dict[str, Any]] = []

    class _Handler(ADCPHandler[Any]):
        async def sync_creatives(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            received.append(params)
            return {"creatives": []}

    hooks = spec_compat_hooks()
    caller = create_tool_caller(
        _Handler(),
        "sync_creatives",
        pre_validation_hook=hooks["sync_creatives"],
    )
    pre_44_payload = _wrap_sync_payload(
        [
            {
                "creative_id": "c1",
                "name": "Landing",
                "format_id": {
                    "agent_url": CANONICAL_CREATIVE_AGENT_URL,
                    "id": "display",
                },
                "assets": {
                    # asset_type='image' but no dims → demote to 'url'.
                    "image": {
                        "asset_type": "image",
                        "url": "https://example.com/landing",
                    },
                },
            },
        ],
    )
    await caller(pre_44_payload)
    asset = received[0]["creatives"][0]["assets"]["image"]
    assert asset["asset_type"] == "url"
    assert asset["url"] == "https://example.com/landing"
