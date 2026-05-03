"""ProductConfigStore — pluggable implementation_config lookup for create_media_buy.

Seller adapters (GAM, Kevel, Xandr, Prebid Server, …) attach seller-side
configuration to each ``Product`` under ``Product.ext.implementation_config``
by convention.  Every adopter that implements ``create_media_buy`` writes the
same boilerplate: pull the packages off the request, collect the product_ids,
look up the per-product config from a store, hand it to the adapter alongside
the wire request.

This module lifts that loop into the framework.  The adopter wires a
``ProductConfigStore`` once at server construction; the framework calls
``lookup_implementation_configs`` before invoking the platform method and
injects the result as ``configs=`` kwarg.  Adopters who declare ``configs`` in
their ``create_media_buy`` signature receive the resolved dict automatically.
Adopters who don't wire a store receive ``configs={}`` (current behaviour,
fully backward-compatible).

Reference pattern: ``adcp.decisioning.webhook_emit`` — same "framework
intercepts at the handler seam before the platform method fires" design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext


@runtime_checkable
class ProductConfigStore(Protocol):
    """Adopter-supplied lookup for seller-side product configuration.

    The framework calls ``lookup_implementation_configs`` once per
    ``create_media_buy`` request, before invoking the platform method.
    Adopters return a dict keyed by ``product_id``; the framework injects
    it as ``configs=`` on the platform method call.

    If the store returns a partial dict (some product_ids missing), the
    adopter handles it — the framework does not fabricate entries for missing
    ids.  If the store raises, the framework surfaces
    ``SERVICE_UNAVAILABLE`` with ``recovery='transient'`` to the buyer.

    The ``ctx`` parameter carries the resolved ``RequestContext``, including
    ``ctx.account`` for multi-tenant stores that need tenant isolation.
    """

    async def lookup_implementation_configs(
        self,
        product_ids: list[str],
        ctx: RequestContext[Any],
    ) -> dict[str, dict[str, Any]]:
        """Return seller-side config keyed by product_id.

        :param product_ids: Deduplicated list of product ids from
            ``CreateMediaBuyRequest.packages[*].product_id``.  Empty when
            the request uses ``proposal_id`` without an explicit
            ``packages`` array.
        :param ctx: Resolved request context — use ``ctx.account`` for
            tenant scoping.
        :returns: Dict mapping each product_id to its config dict.  Missing
            keys mean "no config for this product"; the framework passes the
            partial dict to the adopter unchanged.
        """
        ...


__all__ = ["ProductConfigStore"]
