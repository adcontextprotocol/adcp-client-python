"""Adapters for buyers on the AdCP 2.5 wire shape.

Each submodule defines an :class:`AdapterPair` for one tool and
registers it under version ``"2.5"`` via
:func:`adcp.compat.legacy.register_adapter`. Importing this package
fires every registration at once.

Current coverage:

* ``sync_creatives`` — wraps bare ``format_id`` strings, infers
  ``asset_type`` discriminators, demotes mis-typed ``image`` assets
  to ``url`` when dimensions are missing. The three coercions match
  the spec text on the v3 ``sync_creatives`` schema's pre-v3
  compatibility section.

The full v2.5 catalog (``get_products``, ``create_media_buy``,
``update_media_buy``, ``list_creative_formats``, ``preview_creative``)
ports incrementally — each tool ships as its own commit so reviewers
can audit translations one at a time.
"""

from __future__ import annotations

from adcp.compat.legacy.v2_5 import sync_creatives  # noqa: F401

__all__ = ["sync_creatives"]
