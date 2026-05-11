"""v2.5 → v3 adapter for ``sync_creatives``.

Three wire-shape changes between v2.5 and v3:

1. **``format_id`` shape.** v2.5 buyers sent a bare string
   (``"display_300x250"``); v3 requires the structured form
   ``{"agent_url": "...", "id": "display_300x250"}``. We inject the
   canonical creative-agent URL (the AdCP standard registry) when the
   value is a string.
2. **``asset_type`` discriminator.** v3 requires every asset to declare
   its type explicitly. v2.5 relied on the asset key as the type hint
   (``{"image": {...}}``) or on field presence (``url`` + dims →
   image). The adapter infers the discriminator using the same rules
   the spec documents for backwards compatibility.
3. **``image`` → ``url`` demotion.** v3's image variant requires
   ``width`` and ``height``. A v2.5 asset typed as ``image`` but
   missing dims is semantically a URL reference; demote rather than
   reject.

Each rule is reversible in principle, but :attr:`AdapterPair.normalize_response`
is left ``None`` here — the response shape didn't change between v2.5
and v3 for ``sync_creatives``.

Direct port of ``src/lib/adapters/legacy/v2-5/sync_creatives.ts`` /
``src/lib/utils/sync-creatives-adapter.ts``.
"""

from __future__ import annotations

from typing import Any

from adcp.compat.legacy import register_adapter
from adcp.compat.legacy.types import AdapterPair

#: Canonical ``agent_url`` injected when wrapping a bare ``format_id``.
#: Mirrors ``adcp.server.spec_compat.CANONICAL_CREATIVE_AGENT_URL``.
_CANONICAL_CREATIVE_AGENT_URL = "https://creative.adcontextprotocol.org"

# Asset type discriminators the spec defines. Used by the key-based
# inference path — only exact key matches resolve to a type. Substring
# matches (``hero_image``) are intentionally excluded; they're asset
# IDs, not type hints.
_KNOWN_ASSET_TYPES: frozenset[str] = frozenset(
    {
        "image",
        "video",
        "audio",
        "vast",
        "text",
        "url",
        "html",
        "javascript",
        "webhook",
        "css",
        "daast",
        "markdown",
        "brief",
        "catalog",
    }
)


def _infer_asset_type(asset_key: str, asset: dict[str, Any]) -> str | None:
    """Infer ``asset_type`` from key + field presence. ``None`` if ambiguous."""
    if asset_key in _KNOWN_ASSET_TYPES:
        return asset_key
    if "url" in asset:
        if "width" in asset and "height" in asset:
            return "image"
        return "url"
    if "content" in asset:
        return "text"
    return None


def _coerce_asset(asset_key: str, asset: dict[str, Any]) -> dict[str, Any]:
    """Apply the v2.5 → v3 coercions to a single asset dict."""
    out = dict(asset)
    if "asset_type" not in out:
        inferred = _infer_asset_type(asset_key, out)
        if inferred is not None:
            out["asset_type"] = inferred

    # ``image`` without both dims → demote to ``url`` (only when ``url``
    # is actually present; otherwise the asset is structurally invalid
    # either way, and current-schema validation reports it).
    if out.get("asset_type") == "image" and not ("width" in out and "height" in out):
        if "url" in out:
            out.pop("width", None)
            out.pop("height", None)
            out["asset_type"] = "url"
    return out


def adapt_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a v2.5 ``sync_creatives`` request to v3 shape.

    Returns a new dict — callers can rely on the original being
    untouched (idempotency under retry).
    """
    out = dict(payload)
    creatives = out.get("creatives")
    if not isinstance(creatives, list):
        return out

    new_creatives: list[Any] = []
    for creative in creatives:
        if not isinstance(creative, dict):
            new_creatives.append(creative)
            continue

        new_creative = dict(creative)

        # Hook 1: bare format_id string → structured.
        fid = new_creative.get("format_id")
        if isinstance(fid, str):
            new_creative["format_id"] = {
                "agent_url": _CANONICAL_CREATIVE_AGENT_URL,
                "id": fid,
            }

        # Hooks 2 + 3: per-asset coercions.
        assets = new_creative.get("assets")
        if isinstance(assets, dict):
            new_creative["assets"] = {
                key: _coerce_asset(key, value) if isinstance(value, dict) else value
                for key, value in assets.items()
            }

        new_creatives.append(new_creative)

    out["creatives"] = new_creatives
    return out


ADAPTER = AdapterPair(tool_name="sync_creatives", adapt_request=adapt_request)
register_adapter("2.5", ADAPTER)
