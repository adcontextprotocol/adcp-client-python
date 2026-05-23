"""time_budget deadline wrapper for get_products.

When ``GetProductsRequest.time_budget`` is set, the framework installs an
``asyncio.wait_for`` deadline around the adopter's ``get_products`` call and
projects the wire-compliant ``incomplete[]`` shape on exhaustion.

Design principles
-----------------
* **Non-intrusive.** The wrapper lives in ``handler.py``'s ``get_products``
  shim, not inside ``_invoke_platform_method``. That keeps the shared
  dispatcher clean and the concern local — the same posture as
  ``webhook_emit.py`` adding behaviour at the call seam.

* **Cancel-safe.** ``get_products`` is a pure read — no ``TaskRegistry``
  allocations occur inside ``_invoke_platform_method`` for this tool.
  ``asyncio.CancelledError`` (a ``BaseException`` on Python 3.8+) propagates
  past the ``except Exception`` in ``_invoke_platform_method`` cleanly. This
  invariant MUST be preserved if ``get_products`` ever gains registry work.

* **Thread-pool warning for sync adopters.** When a sync adopter runs via
  ``loop.run_in_executor`` and ``asyncio.wait_for`` fires, the asyncio side
  moves on but the underlying thread continues until its blocking call
  returns. No Python mechanism can interrupt a running thread. The pool
  slot is occupied for the full duration; on a short-budget burst against a
  slow sync adopter this can exhaust the pool. Async adopters are
  unaffected. Adopters who need to co-operate with deadline cancellation
  should implement the ``IncrementalGetProducts`` protocol or migrate to an
  async ``get_products``.

* **``campaign`` unit → no SDK-managed deadline.** ``unit='campaign'`` means
  "the seller has the full campaign flight to respond" — this is a
  semantically distinct signal (not the same as omitting ``time_budget``
  entirely). The SDK does NOT install a deadline for campaign-scoped
  requests; the raw ``time_budget`` value still reaches the adopter via
  ``params`` so they can honour it at the application level if desired.

* **``_invoke_platform_method`` MUST NOT catch TimeoutError.** The
  ``except Exception`` in ``dispatch._invoke_platform_method`` catches
  ``asyncio.TimeoutError`` (MRO: TimeoutError → OSError → Exception) on
  Python < 3.11. The ``wait_for`` is placed in ``handler.get_products``
  *outside* ``_invoke_platform_method``'s try/except to ensure
  ``TimeoutError`` reaches the shim-level handler. If a future refactor
  moves the deadline inside ``_invoke_platform_method``, the ``except
  Exception`` at ``dispatch.py:1134`` must explicitly re-raise
  ``asyncio.TimeoutError`` before the generic handler fires.

Optional incremental protocol
------------------------------
``IncrementalGetProducts`` is an opt-in upgrade. When the adopter implements
it alongside ``get_products``, the framework (in a follow-up) routes
``get_products`` calls through the streaming path, collecting partial batches
until the deadline and projecting any unfinished scopes to ``incomplete[]``.
Without it, a timeout produces ``products: []`` + ``incomplete[{scope:
'products'}]``.

Expose the Protocol now so adopters and code generators can reference it via
``from adcp.decisioning import IncrementalGetProducts``; the dispatch routing
for the streaming path ships when the second adopter adopts the wire shape.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.types import GetProductsRequest

logger = logging.getLogger(__name__)

# ---- Unit conversion ----

_UNIT_TO_SECONDS: dict[str, float] = {
    "seconds": 1.0,
    "minutes": 60.0,
    "hours": 3600.0,
    "days": 86400.0,
}


def resolve_time_budget(time_budget: Any) -> float | None:
    """Convert ``GetProductsRequest.time_budget`` to a seconds deadline.

    Returns ``None`` when:
    - ``time_budget`` is ``None`` (field absent) — no deadline.
    - ``unit == 'campaign'`` — seller decides timing; SDK does not install a
      deadline. The raw ``time_budget`` value still reaches the adopter via
      ``params``.

    For all other units (seconds / minutes / hours / days), returns
    ``interval * unit_seconds`` as a positive ``float``.
    """
    if time_budget is None:
        return None

    unit = getattr(time_budget, "unit", None)
    if unit is None:
        # Tolerate plain dicts (test fixtures, future schema variants).
        unit = time_budget.get("unit") if isinstance(time_budget, dict) else None
    if unit is None:
        return None

    # Normalise enum to string.
    unit_str = unit.value if hasattr(unit, "value") else str(unit)

    if unit_str == "campaign":
        # Semantically distinct from "omitted" — the seller has the full
        # campaign flight. Log at DEBUG so adopters see the explicit skip.
        logger.debug(
            "[adcp.decisioning] time_budget unit='campaign' — "
            "no SDK-managed deadline; adopter decides timing."
        )
        return None

    factor = _UNIT_TO_SECONDS.get(unit_str)
    if factor is None:
        logger.warning(
            "[adcp.decisioning] Unrecognised time_budget unit %r — " "treating as no deadline.",
            unit_str,
        )
        return None

    interval = getattr(time_budget, "interval", None)
    if interval is None and isinstance(time_budget, dict):
        interval = time_budget.get("interval")
    if not isinstance(interval, int) or interval < 1:
        logger.warning(
            "[adcp.decisioning] Invalid time_budget interval %r — " "treating as no deadline.",
            interval,
        )
        return None

    return float(interval) * factor


def project_incomplete_response(*, interval: int, unit: str) -> dict[str, Any]:
    """Build the wire-compliant timeout response dict.

    Returns ``{"products": [], "incomplete": [{scope, description, estimated_wait}]}``
    with ``incomplete`` containing at least one entry (``min_length=1`` on
    the wire schema). Uses a raw dict to stay above the import-layering
    boundary (only the whitelist in ``tests/test_import_layering.py`` may
    import from ``adcp.types._generated``).

    ``scope`` is ``"products"`` — the spec's "not all inventory sources were
    searched" scope, which is accurate for a full-search timeout. When the
    deadline fires before ``_invoke_platform_method`` returns, the seller
    genuinely does not know whether pricing / forecast / proposals would have
    been attempted; ``scope='products'`` is the minimal correct signal. A
    follow-up that ships the incremental protocol can enumerate additional
    scopes when the adopter provides partial data.

    ``estimated_wait`` is omitted (``None``) when the plain adopter path is
    used, since the framework has no visibility into how much longer the call
    would have taken.
    """
    description = (
        f"time_budget exhausted ({interval} {unit}); "
        "return the best results achievable within the budget. "
        "Retry with a larger time_budget to receive complete results."
    )
    return {
        "products": [],
        "incomplete": [
            {
                "scope": "products",
                "description": description,
                "estimated_wait": None,
            }
        ],
    }


# ---- Optional incremental protocol ----


class ProductsCheckpoint:
    """Accumulates partial ``GetProductsResponse`` batches during streaming.

    Passed to ``IncrementalGetProducts.get_products_incremental`` so the
    framework can collect whatever batches the adopter yields before the
    deadline. The framework reads ``checkpoint.batches`` after timeout to
    project ``products`` and ``incomplete[]``.

    Adopters do not instantiate this directly — the framework creates it and
    passes it in.
    """

    def __init__(self) -> None:
        self.batches: list[dict[str, Any]] = []

    def add_batch(self, batch: dict[str, Any]) -> None:
        """Record a partial response batch."""
        self.batches.append(batch)


@runtime_checkable
class IncrementalGetProducts(Protocol):
    """Optional upgrade protocol for streaming partial get_products results.

    **Status: Protocol declaration only.** The dispatch routing for this
    path ships in a follow-up to issue #495. Implementing this Protocol
    today has no runtime effect — the framework still calls the plain
    ``get_products`` method. Declare it now so adopters and code generators
    can reference the type; re-enable via the follow-up PR once the dispatch
    branch is wired.

    When the dispatch path lands: the framework routes ``get_products``
    calls through this streaming path instead of the plain method. Batches
    are collected until the ``time_budget`` deadline; remaining scopes are
    projected to ``incomplete[]``.

    Until then, a ``time_budget`` timeout returns
    ``products: []`` + ``incomplete: [{scope: 'products'}]``.

    Usage::

        from adcp.decisioning import IncrementalGetProducts, ProductsCheckpoint
        from adcp.types import GetProductsRequest
        from adcp.decisioning import RequestContext
        from typing import AsyncIterator

        class MySeller(DecisioningPlatform, IncrementalGetProducts):
            async def get_products_incremental(
                self,
                req: GetProductsRequest,
                ctx: RequestContext,
                checkpoint: ProductsCheckpoint,
            ) -> AsyncIterator[dict]:
                for batch in self._stream_products(req):
                    checkpoint.add_batch(batch)
                    yield batch

    Note: ``get_products_incremental`` MUST be an ``async def`` that yields —
    i.e., an async generator function. The framework detects it via
    ``asyncio.isasyncgenfunction``, not ``asyncio.iscoroutinefunction``. If
    your data source is synchronous, wrap the yield in an ``async def`` body
    rather than returning a sync generator.

    ``campaign`` unit: if ``req.time_budget.unit == 'campaign'``, the
    framework does not install a deadline; this method is called the same as
    the plain path, and the adopter may yield indefinitely (within the
    campaign flight window).
    """

    async def get_products_incremental(
        self,
        req: GetProductsRequest,
        ctx: RequestContext,
        checkpoint: ProductsCheckpoint,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield partial product batches until complete or deadline fires."""
        ...


__all__ = [
    "IncrementalGetProducts",
    "ProductsCheckpoint",
    "project_incomplete_response",
    "resolve_time_budget",
]
