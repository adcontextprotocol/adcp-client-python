"""v2.5 → v3 adapter for ``create_media_buy``.

Wire-shape deltas:

* ``brand_manifest`` (v2.5 URL string) → ``brand: {domain}`` (v3).
* Per-package ``creative_ids`` (v2.5) → ``creative_assignments`` (v3).
* Response packages get the reverse rewrite (``creative_assignments`` →
  ``creative_ids``, dropping v3-only ``weight``/``placement_ids``).

Buyer identity (``buyer_ref``) is preserved as-is; v3 tolerates the
field via ``additionalProperties`` and adopters that rely on
buyer-controlled idempotency keep their dedupe semantics.

Direct port (inverted) of
``src/lib/adapters/legacy/v2-5/create_media_buy.ts`` +
``src/lib/utils/creative-adapter.ts``.
"""

from __future__ import annotations

from typing import Any

from adcp.compat.legacy import register_adapter
from adcp.compat.legacy.types import AdapterPair
from adcp.compat.legacy.v2_5._media_buy_helpers import (
    adapt_brand_manifest_to_brand,
    adapt_package_request,
    normalize_media_buy_response,
)


def adapt_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a v2.5 ``create_media_buy`` request to v3 shape."""
    out = adapt_brand_manifest_to_brand(payload)

    packages = out.get("packages")
    if isinstance(packages, list):
        out["packages"] = [adapt_package_request(p) if isinstance(p, dict) else p for p in packages]

    return out


def is_legacy_shape(payload: dict[str, Any]) -> bool:
    """v2.5 ``create_media_buy`` markers:

    * top-level ``brand_manifest`` URL (v3 uses ``brand: {domain}``)
    * any package with ``creative_ids: list[str]`` (v3 uses
      ``creative_assignments: [{creative_id, ...}]``)
    """
    if "brand_manifest" in payload:
        return True
    packages = payload.get("packages")
    if isinstance(packages, list):
        for pkg in packages:
            if isinstance(pkg, dict) and isinstance(pkg.get("creative_ids"), list):
                return True
    return False


ADAPTER = AdapterPair(
    tool_name="create_media_buy",
    adapt_request=adapt_request,
    normalize_response=normalize_media_buy_response,
    is_legacy_shape=is_legacy_shape,
)
register_adapter("2.5", ADAPTER)
