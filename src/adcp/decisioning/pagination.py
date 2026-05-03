"""Framework-managed cursor pagination for list responses.

When an adopter sets ``auto_paginate=True`` on :class:`DecisioningCapabilities`,
the handler intercepts ``get_products``, calls the adopter's implementation to
retrieve the full product set, and slices the result to the requested page.

.. warning:: **Adoption ceiling.**  ``auto_paginate=True`` requires the adopter's
   ``get_products`` to return the *complete* unfiltered product set on every call —
   the framework slices it. This pattern works well for in-memory and small-catalog
   sellers (≲10 k products). Adopters with DB-backed catalogs at production scale
   MUST handle cursor logic natively and leave ``auto_paginate=False`` (the default).
   Returning a 100 k-product list only to have the framework discard 99 950 rows is
   a silent production latency and memory spike that only manifests at scale.

The cursor is an **HMAC-SHA-256-signed** JSON envelope::

    base64url( JSON({ "p": JSON({"v":1,"o":<offset>,"qh":<filter-hash>}), "s":<hex-sig> }) )

The ``qh`` field is a SHA-256 fingerprint of the request parameters (excluding
``pagination`` itself), so if a buyer changes filters between pages the framework
returns ``INVALID_REQUEST`` with ``field="pagination.cursor"`` rather than silently
serving the wrong page slice.

**Secret management.** The HMAC key defaults to a per-process random secret (stable
within the process, different across restarts). Stale cursors from a prior process
return ``INVALID_REQUEST`` — this is the correct behaviour for stateless sellers.
Horizontally-scaled or stateless sellers that need cursors to survive process restarts
MUST set ``ADCP_PAGINATION_SECRET`` in the environment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from typing import Any

from adcp.decisioning.types import AdcpError

logger = logging.getLogger(__name__)

_CURSOR_VERSION = 1

# Per-process fallback secret. Generated once at import time; stable within a
# process, different across restarts. Stale cursors from prior processes fail
# the HMAC check and return INVALID_REQUEST — correct for stateless sellers.
_PROCESS_SECRET: bytes = os.urandom(32)


def _secret() -> bytes:
    """Return the active HMAC secret."""
    env = os.environ.get("ADCP_PAGINATION_SECRET")
    return env.encode() if env else _PROCESS_SECRET


def _sign(payload: bytes, secret: bytes) -> str:
    return hmac.new(secret, payload, digestmod="sha256").hexdigest()


def _encode_cursor(offset: int, query_hash: str, secret: bytes | None = None) -> str:
    """Return an opaque, HMAC-signed cursor string for the given page offset."""
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "o": offset, "qh": query_hash}, separators=(",", ":")
    ).encode()
    sig = _sign(payload, secret if secret is not None else _secret())
    envelope = json.dumps(
        {"p": payload.decode(), "s": sig}, separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(envelope).rstrip(b"=").decode()


def _decode_cursor(cursor: str, expected_query_hash: str, secret: bytes | None = None) -> int:
    """Decode *cursor* and return the page offset.

    :raises AdcpError: ``INVALID_REQUEST`` (``field="pagination.cursor"``,
        ``recovery="correctable"``) when the cursor is malformed, has an invalid
        HMAC signature, or embeds a query-hash that doesn't match
        *expected_query_hash* (filters changed between pages).
    """
    _bad = AdcpError(
        "INVALID_REQUEST",
        message=(
            "pagination.cursor is malformed or expired. Omit the cursor to restart from page 1."
        ),
        field="pagination.cursor",
        recovery="correctable",
    )
    try:
        # Restore stripped padding before decoding.
        padded = cursor + "=" * ((4 - len(cursor) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
        envelope = json.loads(raw)
        payload_str: str = envelope["p"]
        sig: str = envelope["s"]
    except Exception:
        raise _bad from None  # suppress decode-error chain; internal detail

    payload_bytes = payload_str.encode()
    expected_sig = _sign(payload_bytes, secret if secret is not None else _secret())
    if not hmac.compare_digest(sig, expected_sig):
        raise _bad from None

    try:
        inner = json.loads(payload_bytes)
        offset: int = int(inner["o"])
        qh: str = inner["qh"]
    except Exception:
        raise _bad from None

    if not hmac.compare_digest(qh, expected_query_hash):
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "pagination.cursor is stale: request filters changed since this cursor was "
                "issued. Omit the cursor to restart pagination with the new filter values."
            ),
            field="pagination.cursor",
            recovery="correctable",
            suggestion="Omit the cursor to start from the first page with the updated filters.",
        )

    return offset


def _query_hash(params: Any) -> str:
    """Compute a stable fingerprint of request params, excluding ``pagination``.

    Detects filter drift between pages. ``pagination`` is excluded so
    successive page requests with different cursors still match the same hash.
    """
    try:
        if hasattr(params, "model_dump"):
            d: Any = params.model_dump(mode="json", exclude={"pagination"})
        else:
            d = {k: v for k, v in dict(params).items() if k != "pagination"}
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        canonical = repr(params)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def apply_framework_pagination(
    response: Any,
    pagination: Any,
    query_hash: str,
    secret: bytes | None = None,
) -> Any:
    """Slice a full-list ``GetProductsResponse`` to the requested page.

    Called by the handler post-adapter when ``auto_paginate=True`` on
    :class:`~adcp.decisioning.platform.DecisioningCapabilities`.

    Short-circuits (returns *response* unchanged) when:

    * ``response.pagination`` is already populated — the adopter handled
      pagination natively; the framework must not overwrite it.
    * ``response.products`` is absent — unexpected shape; pass through and
      let wire validation surface the issue.

    Clamps ``max_results`` to ``[1, 100]`` before slicing.

    :param response: The adopter's full-list ``GetProductsResponse``.
    :param pagination: The wire ``Pagination`` request object
        (``max_results``, ``cursor``).
    :param query_hash: Filter fingerprint from :func:`_query_hash` on the
        original request. Used to anchor the cursor.
    :param secret: HMAC key override for testing and direct use. ``None`` uses
        :func:`_secret` (reads ``ADCP_PAGINATION_SECRET`` env var, falls back
        to the per-process random secret). The handler always passes ``None``
        — configure production secrets via the env var.
    :returns: A new ``GetProductsResponse`` with ``products`` sliced to the
        page and ``pagination`` populated, or the original *response* if the
        short-circuit fired.
    """
    # Short-circuit: adopter already populated pagination. Also short-circuits
    # when _invoke_platform_method returned a TaskHandoff projection dict
    # ({"task_id": ..., "status": "submitted"}) — dicts have no .products
    # attribute so the products-None branch below fires and passes through.
    if getattr(response, "pagination", None) is not None:
        return response

    products = getattr(response, "products", None)
    if products is None:
        return response

    # Decode cursor or start at offset 0.
    cursor_str = getattr(pagination, "cursor", None)
    if cursor_str:
        offset = _decode_cursor(cursor_str, query_hash, secret)
    else:
        offset = 0

    # Clamp max_results to wire schema bounds [1, 100].
    raw_max = getattr(pagination, "max_results", None)
    max_results = max(1, min(100, raw_max if raw_max is not None else 50))

    full_list = list(products)
    page = full_list[offset : offset + max_results]
    has_more = (offset + max_results) < len(full_list)
    next_cursor = (
        _encode_cursor(offset + max_results, query_hash, secret) if has_more else None
    )

    # Deferred: adcp.types imports adcp.decisioning.types; top-level import is circular.
    from adcp.types import PaginationResponse

    new_pagination = PaginationResponse(has_more=has_more, cursor=next_cursor)
    return response.model_copy(update={"products": page, "pagination": new_pagination})


__all__ = ["apply_framework_pagination"]
