"""Built-in spec-compatibility hooks for pre-v3 / pre-4.4 buyers.

Wire into ``serve(pre_validation_hooks=spec_compat_hooks())`` to accept the
long tail of legacy clients without writing boilerplate in every adopter repo.
Adopters who want strict-4.4-only rejection of pre-v3/pre-4.4 buyers leave
this off and let the validator reject malformed requests.

The hooks in this module are intentionally pure-Python with no transport or
framework dependencies so they compose cleanly with adopter-specific hooks::

    from adcp.server import spec_compat_hooks, serve

    # Standalone:
    serve(handler, pre_validation_hooks=spec_compat_hooks())

    # Combined with adopter-specific hooks:
    serve(handler, pre_validation_hooks={
        **spec_compat_hooks(),
        "create_media_buy": my_custom_hook,
    })

    # Selective opt-out (skip sync_creatives coercions entirely):
    serve(handler, pre_validation_hooks=spec_compat_hooks(exclude={"sync_creatives"}))

    # Private creative-format registry:
    serve(handler, pre_validation_hooks=spec_compat_hooks(
        creative_agent_url="https://creative.example.com"
    ))
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Collection
from typing import Any, TypeAlias

PreValidationHooks: TypeAlias = dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]]
"""Type alias for the ``pre_validation_hooks`` parameter of ``serve()``."""

CANONICAL_CREATIVE_AGENT_URL = "https://creative.adcontextprotocol.org"
"""Canonical ``agent_url`` for the AdCP standard creative-format registry.

Injected when wrapping a bare ``format_id`` string from a pre-4.4 buyer.
Override via ``spec_compat_hooks(creative_agent_url=...)`` for private
creative-format registries.
"""

_VALID_HOOK_NAMES: frozenset[str] = frozenset({"get_products", "sync_creatives"})
"""Tool names ``spec_compat_hooks()`` returns hooks for.

Exposed for ``exclude=`` validation — a typo like ``exclude={"sync_creative"}``
emits a :class:`UserWarning` instead of silently leaving the hook active.
"""

# Exact set of asset_type discriminator values the spec defines.
# Used by the asset_type inference heuristic — only exact key matches count.
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


def _hook_get_products(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    """Default ``buying_mode`` to ``'brief'`` when omitted.

    Spec text: *"Sellers receiving requests from pre-v3 clients without
    buying_mode SHOULD default to 'brief'."*
    """
    if "buying_mode" not in args:
        return {**args, "buying_mode": "brief"}
    return args


def _infer_asset_type(asset_key: str, asset: dict[str, Any]) -> str | None:
    """Infer ``asset_type`` for a pre-4.4 asset dict that omits the discriminator.

    Resolution order:
    1. Exact key match against ``_KNOWN_ASSET_TYPES`` (pre-4.4 convention of
       using the type name as the asset key, e.g. ``{"image": {...}}``.
       Substring / partial matches are intentionally excluded — keys like
       ``"hero_image"`` are asset IDs, not type hints.
    2. Field-presence heuristics:
       - ``url`` + ``width`` + ``height`` present → ``"image"``
       - ``url`` present without dims → ``"url"``
       - ``content`` present → ``"text"``
    3. Returns ``None`` when inference is ambiguous — the dict is left
       unchanged and schema validation will report the missing discriminator.
    """
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
    """Apply pre-4.4 coercions to a single raw asset dict.

    Mutates a copy, not the original.  Applies hooks in order so that
    hook-3 (infer asset_type) feeds into hook-4 (image → url demotion).
    """
    # Hook 3: infer asset_type when missing.
    if "asset_type" not in asset:
        inferred = _infer_asset_type(asset_key, asset)
        if inferred is not None:
            asset = {**asset, "asset_type": inferred}

    # Hook 4: demote asset_type='image' → 'url' when dims are absent.
    # Only fires when url is present — if url is also absent the asset is
    # structurally unusable either way; leave it for schema validation to
    # report rather than silently changing the type to something equally invalid.
    if asset.get("asset_type") == "image" and not ("width" in asset and "height" in asset):
        if "url" in asset:
            asset = {k: v for k, v in asset.items() if k not in ("width", "height")}
            asset = {**asset, "asset_type": "url"}

    return asset


def _make_sync_creatives_hook(
    agent_url: str,
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Return a composed sync_creatives hook (hooks 2 + 3 + 4)."""

    def hook(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
        creatives = args.get("creatives")
        if not isinstance(creatives, list):
            return args

        new_creatives: list[Any] = []
        changed = False

        for creative in creatives:
            if not isinstance(creative, dict):
                new_creatives.append(creative)
                continue

            creative = dict(creative)

            # Hook 2: wrap bare format_id string as {agent_url, id}.
            format_id = creative.get("format_id")
            if isinstance(format_id, str):
                creative["format_id"] = {"agent_url": agent_url, "id": format_id}
                changed = True

            # Hooks 3+4: coerce individual assets.
            assets = creative.get("assets")
            if isinstance(assets, dict):
                new_assets: dict[str, Any] = {}
                for key, asset_val in assets.items():
                    if isinstance(asset_val, dict):
                        coerced = _coerce_asset(key, asset_val)
                        new_assets[key] = coerced
                        if coerced is not asset_val:
                            changed = True
                    else:
                        new_assets[key] = asset_val
                creative["assets"] = new_assets

            new_creatives.append(creative)

        if not changed:
            return args
        return {**args, "creatives": new_creatives}

    return hook


def spec_compat_hooks(
    *,
    exclude: Collection[str] | None = None,
    creative_agent_url: str = CANONICAL_CREATIVE_AGENT_URL,
) -> PreValidationHooks:
    """Return built-in spec-compat hooks for pre-v3 / pre-4.4 buyers.

    Hooks included (all opt-outable via ``exclude``):

    ``get_products``
        Defaults ``buying_mode`` to ``'brief'`` when the field is absent.
        Spec: *"Sellers receiving requests from pre-v3 clients without
        buying_mode SHOULD default to 'brief'."*

    ``sync_creatives``
        Three bundled coercions, always applied together:

        1. **format_id string → structured** — wraps a bare string
           ``format_id`` as ``{"agent_url": ..., "id": ...}`` using
           ``creative_agent_url`` as the registry URL (pre-4.4 buyers sent a
           plain string; 4.4+ requires the structured form).
        2. **asset_type inference** — infers the missing ``asset_type``
           discriminator from the asset key (exact match only — ``"image"``
           resolves to ``asset_type='image'``; ``"hero_image"`` does not) or
           from field presence (``url``+dims → ``image``; ``url`` → ``url``;
           ``content`` → ``text``).  Leaves ambiguous assets unchanged.
        3. **image → url demotion** — when ``asset_type='image'`` but
           ``width``/``height`` are absent, demotes to ``asset_type='url'``
           (4.4 image variant requires both dims).  Only fires when ``url``
           is present; structurally invalid assets (no ``url`` either) are
           left for schema validation to report.

        Adopters who need granular control over the three sub-behaviors
        should copy the relevant logic from
        ``adcp.server.spec_compat._coerce_asset`` / ``_hook_get_products``
        rather than trying to layer hooks — ``pre_validation_hooks`` allows
        only one callable per tool name.

    Args:
        exclude: Tool names to exclude from the returned dict. Names not in
            :data:`_VALID_HOOK_NAMES` raise a :class:`UserWarning` (a typo
            like ``exclude={"sync_creative"}`` would otherwise silently leave
            the hook active). Example: ``exclude={"sync_creatives"}`` returns
            only the ``get_products`` hook.
        creative_agent_url: Registry URL injected when wrapping a bare
            ``format_id`` string.  Defaults to
            ``CANONICAL_CREATIVE_AGENT_URL``.  Override for private
            creative-format registries.

    Returns:
        A :data:`PreValidationHooks` dict ready to pass as
        ``serve(pre_validation_hooks=...)``.
    """
    excluded: frozenset[str] = frozenset(exclude) if exclude else frozenset()
    unknown = excluded - _VALID_HOOK_NAMES
    if unknown:
        warnings.warn(
            "spec_compat_hooks(exclude=...) received unknown hook name(s): "
            f"{sorted(unknown)}. Valid names: {sorted(_VALID_HOOK_NAMES)}. "
            "Typos here are silent — verify the name matches the tool you "
            "intended to opt out.",
            UserWarning,
            stacklevel=2,
        )
    hooks: PreValidationHooks = {}

    if "get_products" not in excluded:
        hooks["get_products"] = _hook_get_products

    if "sync_creatives" not in excluded:
        hooks["sync_creatives"] = _make_sync_creatives_hook(creative_agent_url)

    return hooks
