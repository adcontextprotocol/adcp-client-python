"""Adapters for buyers on the AdCP 2.5 wire shape.

Each submodule defines an :class:`AdapterPair` for one tool and
registers it under version ``"2.5"`` via
:func:`adcp.compat.legacy.register_adapter`. Importing this package
fires every registration at once.

Current coverage:

* ``sync_creatives`` — wraps bare ``format_id`` strings, infers
  ``asset_type`` discriminators, demotes mis-typed ``image`` assets
  to ``url`` when dimensions are missing.
* ``list_creative_formats`` — request pass-through; response rewrites
  v2.5 top-level ``width``/``height``/``dimensions`` into the v3
  ``renders: [{render_id, role, dimensions}]`` array.
* ``preview_creative`` — request pass-through; response renames v2.5
  ``output_id``/``output_role`` to v3 ``render_id``/``role`` on each
  preview render. Handles both single-response and batch-response
  shapes.

The remaining v2.5 tools (``get_products``, ``create_media_buy``,
``update_media_buy``) ship in Stage 5b — they rely on the pricing and
creative-adapter helpers which are substantial standalone ports.
"""

from __future__ import annotations

from adcp.compat.legacy.v2_5 import (  # noqa: F401
    get_products,
    list_creative_formats,
    preview_creative,
    sync_creatives,
)

__all__ = [
    "get_products",
    "list_creative_formats",
    "preview_creative",
    "sync_creatives",
]
