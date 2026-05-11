"""v2.5 → v3 adapter for ``preview_creative``.

* **Request shape:** identical between v2.5 and v3 — pass-through.
* **Response shape:** v2.5 emits ``output_id`` / ``output_role`` on each
  preview render; v3 renames these to ``render_id`` / ``role``. The
  normalizer accepts either set (preferring v3 names when both are
  present so a half-migrated server doesn't double-translate).

Handles both single-response ``{previews: [...]}`` and batch-response
``{results: [{success, response: {previews: [...]}}, ...]}`` shapes.

Direct port of ``src/lib/utils/preview-normalizer.ts`` /
``src/lib/adapters/legacy/v2-5/preview_creative.ts``.
"""

from __future__ import annotations

from typing import Any

from adcp.compat.legacy import register_adapter
from adcp.compat.legacy.types import AdapterPair


def _normalize_render(render: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a v2.5 preview render's ``output_id`` / ``output_role``
    fields to v3's ``render_id`` / ``role`` (preferring v3 when both
    present). All other fields pass through."""
    return {
        "render_id": render.get("render_id") or render.get("output_id") or "primary",
        "role": render.get("role") or render.get("output_role") or "primary",
        **{
            k: v
            for k, v in render.items()
            if k not in {"render_id", "role", "output_id", "output_role"}
        },
    }


def _normalize_preview(preview: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single preview object (rewrites every render)."""
    renders = preview.get("renders")
    if not isinstance(renders, list):
        return preview
    return {
        **preview,
        "renders": [_normalize_render(r) if isinstance(r, dict) else r for r in renders],
    }


def normalize_response(response: dict[str, Any]) -> dict[str, Any]:
    """Normalize a v2.5 ``preview_creative`` response.

    Two shapes are handled:

    * **Single:** ``{previews: [...], ...}`` — each preview's renders
      are normalized.
    * **Batch:** ``{results: [{success: bool, response: {previews: [...]}}, ...]}``
      — successful entries get their nested previews normalized;
      failures pass through.
    """
    previews = response.get("previews")
    if isinstance(previews, list):
        return {
            **response,
            "previews": [_normalize_preview(p) if isinstance(p, dict) else p for p in previews],
        }

    results = response.get("results")
    if isinstance(results, list):
        new_results: list[Any] = []
        for entry in results:
            if not isinstance(entry, dict):
                new_results.append(entry)
                continue
            if entry.get("success") and isinstance(entry.get("response"), dict):
                inner = entry["response"]
                inner_previews = inner.get("previews")
                if isinstance(inner_previews, list):
                    new_results.append(
                        {
                            **entry,
                            "response": {
                                **inner,
                                "previews": [
                                    _normalize_preview(p) if isinstance(p, dict) else p
                                    for p in inner_previews
                                ],
                            },
                        }
                    )
                    continue
            new_results.append(entry)
        return {**response, "results": new_results}

    return response


def _adapt_request_pass_through(payload: dict[str, Any]) -> dict[str, Any]:
    """``preview_creative`` requests are wire-identical between v2.5
    and v3. Shallow copy so downstream mutation can't reach the
    caller."""
    return dict(payload)


ADAPTER = AdapterPair(
    tool_name="preview_creative",
    adapt_request=_adapt_request_pass_through,
    normalize_response=normalize_response,
)
register_adapter("2.5", ADAPTER)
