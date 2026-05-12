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

    Contract every adapter must hold:

    * **Sync + pure.** Both callables run synchronously and produce a
      new dict — they MUST NOT mutate their input (callers rely on the
      original being intact for retries, logging, and idempotency
      tracking). Tests in
      ``tests/test_legacy_adapter_registry.py::test_v2_5_adapter_does_not_mutate_input``
      assert this for shipped adapters; new adapters should add the
      equivalent check.
    * **No I/O.** Heavier work (resolving format references, calling
      upstream services) belongs in handlers, not adapters.
    * **Exception mapping.** A raise inside ``adapt_request`` surfaces
      to the buyer as :class:`adcp.exceptions.ADCPTaskError` with code
      ``INVALID_REQUEST`` (translation = buyer-correctable, per spec).
      A raise inside ``normalize_response`` surfaces as
      ``INTERNAL_ERROR`` (the handler produced a valid response that
      the adapter can't rewrite — SDK bug, not buyer bug).
    """

    tool_name: str
    adapt_request: Callable[[dict[str, Any]], dict[str, Any]]
    normalize_response: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Optional shape probe used by the dispatcher when the buyer didn't
    # send an ``adcp_version`` / ``adcp_major_version`` envelope (real v2.5
    # buyers can't — the field didn't exist in the v2.5 schema). The
    # probe should return ``True`` only on strong, unambiguous v2.5
    # markers — fields that exist in v2.5 but NOT in v3 (e.g.
    # ``brand_manifest``, ``creative_ids`` in packages, bare-string
    # ``format_id``). False positives downgrade a real v3 buyer to v2.5
    # validation, which is the worst outcome; bias conservatively. Tools
    # with pass-through requests (``list_creative_formats``,
    # ``preview_creative``) leave this ``None`` because their request
    # shape is identical across versions.
    is_legacy_shape: Callable[[dict[str, Any]], bool] | None = None
