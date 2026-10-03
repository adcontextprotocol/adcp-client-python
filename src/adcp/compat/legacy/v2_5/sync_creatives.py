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
from adcp.types._legacy_assets import coerce_legacy_asset, infer_asset_type

#: Canonical ``agent_url`` injected when wrapping a bare ``format_id``.
#: Mirrors ``adcp.server.spec_compat.CANONICAL_CREATIVE_AGENT_URL``.
_CANONICAL_CREATIVE_AGENT_URL = "https://creative.adcontextprotocol.org"


def _infer_asset_type(asset_key: str, asset: dict[str, Any]) -> str | None:
    """Delegate to the public legacy asset inference helper."""
    return infer_asset_type(asset_key, asset)


def _coerce_asset(asset_key: str, asset: dict[str, Any]) -> dict[str, Any]:
    """Delegate to the public, non-mutating legacy asset coercion helper."""
    return coerce_legacy_asset(asset_key, asset)


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


def is_legacy_shape(payload: dict[str, Any]) -> bool:
    """v2.5 ``sync_creatives`` has at least one creative whose
    ``format_id`` is a bare string (v3 always emits the structured
    ``{agent_url, id}`` form). Strong signal — v3 wouldn't emit this
    even with ``adcp_version`` omitted."""
    creatives = payload.get("creatives")
    if not isinstance(creatives, list):
        return False
    for creative in creatives:
        if isinstance(creative, dict) and isinstance(creative.get("format_id"), str):
            return True
    return False


ADAPTER = AdapterPair(
    tool_name="sync_creatives",
    adapt_request=adapt_request,
    is_legacy_shape=is_legacy_shape,
)
register_adapter("2.5", ADAPTER)
