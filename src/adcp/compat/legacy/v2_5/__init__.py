"""Adapters for buyers on the AdCP 2.5 wire shape.

Each submodule defines an :class:`AdapterPair` for one tool and
registers it under version ``"2.5"`` via
:func:`adcp.compat.legacy.register_adapter`. Importing this package
fires every registration at once.

Current coverage (full v2.5 tool catalog):

* ``sync_creatives`` — bare ``format_id`` strings → structured;
  ``asset_type`` inference; ``image``-without-dims → ``url``.
* ``list_creative_formats`` — request pass-through; response rewrites
  v2.5 top-level dimensions into the v3 ``renders[]`` array.
* ``preview_creative`` — request pass-through; response renames
  ``output_id``/``output_role`` to v3 ``render_id``/``role``.
* ``get_products`` — ``brand_manifest`` ↔ ``brand``,
  ``promoted_offerings`` ↔ ``catalog``, channel-bucket ↔ slug
  translation, pricing-option ``rate``/``is_fixed`` ↔ ``fixed_price`` /
  ``price_guidance.floor`` ↔ ``floor_price``.
* ``create_media_buy`` — ``brand_manifest`` ↔ ``brand``, package
  ``creative_ids`` ↔ ``creative_assignments``. Response collapses v3
  assignment objects back to v2.5 ID lists (``weight`` /
  ``placement_ids`` dropped — v2.5 buyers can't act on them).
* ``update_media_buy`` — same package translation as
  ``create_media_buy`` (no ``brand_manifest`` on updates).
"""

from __future__ import annotations

from adcp.compat.legacy.v2_5 import (  # noqa: F401
    create_media_buy,
    get_products,
    list_creative_formats,
    preview_creative,
    sync_creatives,
    update_media_buy,
)

__all__ = [
    "create_media_buy",
    "get_products",
    "list_creative_formats",
    "preview_creative",
    "sync_creatives",
    "update_media_buy",
]
