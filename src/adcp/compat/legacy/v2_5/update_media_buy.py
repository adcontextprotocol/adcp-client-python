"""v2.5 → v3 adapter for ``update_media_buy``.

Same wire-shape deltas as ``create_media_buy`` for the package shape
(``creative_ids`` ↔ ``creative_assignments``), but no
``brand_manifest`` translation — updates don't carry brand info.

Response shape matches ``create_media_buy``: package
``creative_assignments`` collapse back to ``creative_ids`` for the
v2.5 buyer.

Direct port (inverted) of
``src/lib/adapters/legacy/v2-5/update_media_buy.ts`` +
``src/lib/utils/creative-adapter.ts``.
"""

from __future__ import annotations

from typing import Any

from adcp.compat.legacy import register_adapter
from adcp.compat.legacy.types import AdapterPair
from adcp.compat.legacy.v2_5._media_buy_helpers import (
    adapt_package_request,
    normalize_media_buy_response,
)


def adapt_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a v2.5 ``update_media_buy`` request to v3 shape."""
    out = dict(payload)

    packages = out.get("packages")
    if isinstance(packages, list):
        out["packages"] = [adapt_package_request(p) if isinstance(p, dict) else p for p in packages]

    return out


ADAPTER = AdapterPair(
    tool_name="update_media_buy",
    adapt_request=adapt_request,
    normalize_response=normalize_media_buy_response,
)
register_adapter("2.5", ADAPTER)
