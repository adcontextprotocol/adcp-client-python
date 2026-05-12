"""v2.5 → v3 adapter for ``list_creative_formats``.

* **Request shape:** identical between v2.5 and v3 — adapter passes the
  payload through unchanged.
* **Response shape:** v2.5 emits top-level ``width`` / ``height`` /
  ``dimensions`` on each format object. v3 moves these into a
  ``renders[]`` array of ``{render_id, role, dimensions}`` entries so a
  single format can declare multiple renders (companion ads, adaptive,
  device variants). ``normalize_response`` walks each
  ``response.formats[].`` and rewrites the v2.5 top-level shape into the
  v3 renders array when no renders are present.

Direct port of ``src/lib/utils/format-renders.ts`` /
``src/lib/adapters/legacy/v2-5/list_creative_formats.ts``.
"""

from __future__ import annotations

from typing import Any

from adcp.compat.legacy import register_adapter
from adcp.compat.legacy.types import AdapterPair


def _normalize_format_renders(format_obj: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a v2.5 format object's top-level dimensions as a v3 renders array.

    If ``renders`` is already present, returns the format unchanged
    (idempotent — handles formats that already emit v3 shape, e.g. a
    v2.5 server that's been partially migrated).
    """
    if isinstance(format_obj.get("renders"), list):
        return format_obj

    has_width = "width" in format_obj
    has_height = "height" in format_obj
    has_dimensions = "dimensions" in format_obj

    if not (has_width or has_height or has_dimensions):
        # No dimension info — template format. Leave unchanged so the
        # caller can inspect ``accepts_parameters`` etc.
        return format_obj

    dimensions: dict[str, int] | None
    if has_dimensions:
        dimensions = format_obj.get("dimensions")
    else:
        dimensions = {
            "width": format_obj.get("width", 0),
            "height": format_obj.get("height", 0),
        }

    new_format = {k: v for k, v in format_obj.items() if k not in {"width", "height", "dimensions"}}
    render: dict[str, Any] = {"render_id": "primary", "role": "primary"}
    if dimensions is not None:
        render["dimensions"] = dimensions
    new_format["renders"] = [render]
    return new_format


def normalize_response(response: dict[str, Any]) -> dict[str, Any]:
    """Normalize a v2.5 ``list_creative_formats`` response into v3 shape."""
    # Some agents omit the ``{formats: [...]}`` wrapper; tolerate that
    # by treating a top-level array as the formats list.
    if isinstance(response, list):
        return {"formats": [_normalize_format_renders(f) for f in response]}

    formats = response.get("formats")
    if not isinstance(formats, list):
        return response

    return {
        **response,
        "formats": [_normalize_format_renders(f) for f in formats],
    }


def _adapt_request_pass_through(payload: dict[str, Any]) -> dict[str, Any]:
    """``list_creative_formats`` requests are wire-identical between
    v2.5 and v3 — no translation needed. Return a shallow copy so
    downstream mutation can't reach the caller's dict (per AdapterPair
    contract)."""
    return dict(payload)


ADAPTER = AdapterPair(
    tool_name="list_creative_formats",
    adapt_request=_adapt_request_pass_through,
    normalize_response=normalize_response,
)
register_adapter("2.5", ADAPTER)
