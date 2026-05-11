"""``AdapterPair`` — the typed contract for legacy-version translators.

Each tool the framework supports on a legacy wire shape gets its own
:class:`AdapterPair`. The pair owns two translations:

* :attr:`adapt_request` — takes a payload validated against the legacy
  schema and returns a dict in the current (SDK-pinned) wire shape. The
  framework then runs current-schema validation + Pydantic ``model_validate``
  on the output, so a buggy translator surfaces as ``INVALID_REQUEST``
  with a field-level pointer.
* :attr:`normalize_response` — optional reverse direction: takes a
  current-shape response and rewrites it to the legacy shape the buyer
  expects to see. ``None`` means "no rewriting needed" (legacy and
  current shapes agree on the response side).

Mirrors ``src/lib/adapters/legacy/v2-5/types.ts`` in the TypeScript SDK.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AdapterPair:
    """Translation pair for one tool at one legacy version.

    Adapters live under ``adcp.compat.legacy.{version_key}.{tool_name}``
    and register themselves via :func:`adcp.compat.legacy.register_adapter`
    at import time. The dispatcher looks them up by
    ``(version_key, tool_name)`` once per request.

    Both callables run synchronously — they are pure transformations of
    in-memory dicts, no I/O. Heavier work (e.g., resolving format
    references) belongs in handlers, not adapters.
    """

    tool_name: str
    adapt_request: Callable[[dict[str, Any]], dict[str, Any]]
    normalize_response: Callable[[dict[str, Any]], dict[str, Any]] | None = None
