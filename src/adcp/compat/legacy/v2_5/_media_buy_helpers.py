"""Shared v2.5 ↔ v3 helpers for ``create_media_buy`` and ``update_media_buy``.

These two tools share most of the wire-shape deltas (packages,
``brand_manifest``, ``buyer_ref``), so the per-tool adapter modules
re-export the same primitives from here.

Direct port (with direction inverted) of
``src/lib/utils/creative-adapter.ts``. The JS direction is *client*
side (v3 → v2). Our server-side direction is v2 → v3 for requests and
v3 → v2 for responses.
"""

from __future__ import annotations

from typing import Any

from adcp.compat.legacy.v2_5._url import (
    extract_brand_domain,
    warn_brand_manifest_path_lossy,
)


def adapt_brand_manifest_to_brand(payload: dict[str, Any]) -> dict[str, Any]:
    """Rewrite v2.5 ``brand_manifest`` (URL string or inline BrandManifest
    object) to v3 ``brand`` ``{domain: ...}``.  Caller-supplied ``brand``
    wins when both fields are present (half-migrated buyer).

    v2.5 ``brand-manifest-ref.json`` is a oneOf: either a URL string or an
    inline BrandManifest object.  For the inline case, the ``url`` field is
    optional; when absent there is no derivable hostname, so ``brand`` is
    omitted and v3 validation decides whether to reject.

    Uses ``extract_brand_domain`` to isolate the hostname from full URLs
    (e.g. ``"https://acme.com/.well-known/brand.json"`` → ``"acme.com"``)
    so the result satisfies ``BrandReference.domain``'s hostname-only regex.
    ``warn_brand_manifest_path_lossy`` surfaces a one-time warning when the
    original URL's path won't be reconstructed by v3 sellers — applied
    uniformly to both the URL-string and inline-object branches.
    """
    out = dict(payload)
    manifest = out.pop("brand_manifest", None)
    if isinstance(manifest, str) and manifest and "brand" not in out:
        domain = extract_brand_domain(manifest)
        warn_brand_manifest_path_lossy(manifest, domain)
        out["brand"] = {"domain": domain}
    elif isinstance(manifest, dict) and "brand" not in out:
        url = manifest.get("url")
        if isinstance(url, str) and url.strip():
            domain = extract_brand_domain(url)
            warn_brand_manifest_path_lossy(url, domain)
            out["brand"] = {"domain": domain}
    return out


def adapt_package_request(pkg: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a v2.5 package request to v3 shape.

    Translations:

    * ``creative_ids: list[str]`` → ``creative_assignments: list[{creative_id}]``.
      v2.5 packages reference creatives by ID alone; v3 wraps each in an
      assignment object so future ``weight`` / ``placement_ids`` fields
      can attach. The v2.5 → v3 path can't synthesize those (the buyer
      never sent them), so we just lift the ID.
    * ``buyer_ref`` survives unchanged. v3 doesn't model it on packages
      explicitly but tolerates ``additionalProperties`` per the spec, so
      passing it through preserves the buyer's idempotency-by-name
      semantics if their handler expects it.
    """
    out = dict(pkg)
    creative_ids = out.get("creative_ids")
    if isinstance(creative_ids, list):
        out.pop("creative_ids", None)
        out["creative_assignments"] = [
            {"creative_id": cid} for cid in creative_ids if isinstance(cid, str)
        ]
    return out


def normalize_package_response(pkg: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a v3 package response back to v2.5 shape for legacy buyers.

    Translations:

    * ``creative_assignments: [{creative_id, weight?, placement_ids?}]``
      → ``creative_ids: [creative_id, ...]``. **Lossy** — ``weight`` and
      ``placement_ids`` have no v2.5 equivalent and are dropped. v2.5
      buyers can't act on them, so silent collapse is acceptable.
    * Null arrays (``creative_assignments: null``, ``creative_ids: null``,
      ``products: null``) are coerced to absent fields. Some servers emit
      explicit ``null`` for optional arrays; downstream consumers expect
      either absent or a real list.
    """
    cleaned = dict(pkg)
    for field in ("creative_assignments", "creative_ids", "products"):
        if cleaned.get(field) is None and field in cleaned:
            cleaned.pop(field, None)

    assignments = cleaned.get("creative_assignments")
    if isinstance(assignments, list):
        creative_ids: list[str] = []
        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            cid = assignment.get("creative_id")
            if isinstance(cid, str):
                creative_ids.append(cid)
        cleaned.pop("creative_assignments", None)
        cleaned["creative_ids"] = creative_ids

    return cleaned


def normalize_media_buy_response(response: dict[str, Any]) -> dict[str, Any]:
    """Apply :func:`normalize_package_response` to every package in a
    media-buy response. Pass-through when ``packages`` is absent (error
    responses, terminal envelopes)."""
    packages = response.get("packages")
    if not isinstance(packages, list):
        return response
    return {
        **response,
        "packages": [normalize_package_response(p) if isinstance(p, dict) else p for p in packages],
    }
