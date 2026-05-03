"""Property-list resolver and product intersection helper for get_products.

Capability-gated framework support for the ``property_list`` buyer-side filter
on ``get_products``.  When an adopter declares
``Features(property_list_filtering=True)`` on their
:class:`~adcp.decisioning.platform.DecisioningCapabilities` and supplies a
:class:`PropertyListFetcher`, the framework automatically:

1. Fetches the buyer's authorized property IDs from ``request.property_list.agent_url``.
2. Applies :func:`filter_products_by_property_list` to the platform's response.
3. Sets ``response.property_list_applied = True`` on the returned envelope.

Adopters who want to apply the filter themselves (e.g., pushed down into a DB
query) set ``Features(property_list_filtering=False)`` and call these helpers
directly inside their ``get_products`` implementation.

Reference pattern: :mod:`adcp.decisioning.webhook_emit` (capability-gated
post-adapter side effect).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class PropertyListFetcher(Protocol):
    """Adopter-supplied protocol for fetching a buyer's authorized property IDs.

    The framework calls :meth:`fetch` when ``property_list_filtering`` is
    declared in capabilities and the request carries a ``PropertyList``
    reference.  Adopters plug in their own HTTP client — the framework ships
    no hidden HTTP dependency.

    Typical implementation::

        class MyFetcher:
            def __init__(self, client: httpx.AsyncClient) -> None:
                self._client = client

            async def fetch(
                self,
                agent_url: str,
                list_id: str,
                *,
                auth_token: str | None = None,
            ) -> list[str]:
                headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
                resp = await self._client.get(
                    f"{agent_url}/property-lists/{list_id}",
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()["property_ids"]

    Wire the fetcher via::

        create_adcp_server_from_platform(platform, property_list_fetcher=MyFetcher(client))
    """

    async def fetch(
        self,
        agent_url: str,
        list_id: str,
        *,
        auth_token: str | None = None,
    ) -> list[str]:
        """Fetch and return the list of allowed property_id strings.

        :param agent_url: Buyer agent URL from the wire ``PropertyList`` reference.
        :param list_id: Property list identifier.
        :param auth_token: Optional JWT/bearer token.  Never log this value.
        :returns: List of allowed property_id strings (``^[a-z0-9_]+$`` format).
        :raises Exception: Any exception; the framework wraps it in
            :class:`~adcp.decisioning.types.AdcpError` with ``recovery='transient'``.
        """
        ...


async def resolve_property_list(
    ref: Any,
    *,
    fetcher: PropertyListFetcher,
) -> set[str]:
    """Fetch the buyer's authorized property IDs from the agent at ``ref.agent_url``.

    :param ref: ``PropertyList`` wire object (has ``agent_url``, ``list_id``,
        ``auth_token``).
    :param fetcher: Adopter-supplied :class:`PropertyListFetcher`.
    :returns: Set of allowed property_id strings.
    :raises AdcpError: ``recovery='transient'`` on any fetch failure.
        ``auth_token`` is never included in the error details.
    """
    from adcp.decisioning.types import AdcpError

    list_id: str = ref.list_id
    agent_url: str = str(ref.agent_url)
    auth_token: str | None = getattr(ref, "auth_token", None)

    try:
        ids = await fetcher.fetch(agent_url, list_id, auth_token=auth_token)
        return set(ids)
    except Exception as exc:
        raise AdcpError(
            "SERVICE_UNAVAILABLE",
            message=(
                f"Property list fetch failed for list_id={list_id!r} "
                f"from agent_url={agent_url!r}: {exc}"
            ),
            recovery="transient",
            details={"list_id": list_id, "agent_url": agent_url},
        ) from exc


def filter_products_by_property_list(
    products: list[Any],
    allowed_property_ids: set[str],
) -> list[Any]:
    """Filter a product list to those matching the buyer's authorized property IDs.

    Respects ``publisher_properties.selection_type``:

    * ``'all'`` — product covers all publisher properties; always included.
    * ``'by_id'`` — intersect the product's ``property_ids`` with
      ``allowed_property_ids``.
    * ``'by_tag'`` — property tags cannot be matched against a property ID list;
      this entry does not contribute to inclusion.

    Respects ``product.property_targeting_allowed``:

    * ``False`` (default, "all or nothing") — the product's full set of
      ``by_id`` property IDs must be a subset of ``allowed_property_ids``.
    * ``True`` (permissive) — any non-empty intersection is sufficient.

    A product is included if ANY of its ``publisher_properties`` entries passes
    the filter.  This models the semantics of a product covering inventory from
    multiple publishers: if ANY publisher's inventory is in the buyer's allowed
    set, the product is relevant.

    :param products: Products from the platform's ``get_products`` response.
    :param allowed_property_ids: Set of property_id strings the buyer is
        authorized to spend on (result of :func:`resolve_property_list`).
    :returns: Filtered product list; original order preserved.
    """
    return [p for p in products if _product_matches(p, allowed_property_ids)]


def _product_matches(product: Any, allowed: set[str]) -> bool:
    """Return True if the product should be included after property-list filtering."""
    permissive: bool = bool(getattr(product, "property_targeting_allowed", False))
    pub_props: list[Any] = list(getattr(product, "publisher_properties", None) or [])
    product_id: str = str(getattr(product, "product_id", "?"))

    for pp_wrapper in pub_props:
        # PublisherProperties is a RootModel; unwrap to the discriminated variant.
        pp = getattr(pp_wrapper, "root", pp_wrapper)
        st = getattr(pp, "selection_type", None)

        if st == "all":
            logger.debug(
                "[adcp.property_list] product %r: selection_type='all' → include",
                product_id,
            )
            return True

        if st == "by_id":
            raw_ids: list[Any] = list(getattr(pp, "property_ids", None) or [])
            product_ids = {
                (pid.root if hasattr(pid, "root") else str(pid)) for pid in raw_ids
            }
            if permissive:
                if product_ids & allowed:
                    logger.debug(
                        "[adcp.property_list] product %r: by_id permissive"
                        " — intersection found → include",
                        product_id,
                    )
                    return True
            else:
                if product_ids and product_ids.issubset(allowed):
                    logger.debug(
                        "[adcp.property_list] product %r: by_id strict"
                        " — all IDs in allowed set → include",
                        product_id,
                    )
                    return True
            logger.debug(
                "[adcp.property_list] product %r: by_id %s — no match → continue",
                product_id,
                "permissive" if permissive else "strict",
            )

        elif st == "by_tag":
            # Tag-based selection cannot be matched against property ID lists.
            logger.debug(
                "[adcp.property_list] product %r: selection_type='by_tag'"
                " → cannot match IDs, skip entry",
                product_id,
            )

    logger.debug(
        "[adcp.property_list] product %r: no publisher_properties entry passed → exclude",
        product_id,
    )
    return False


async def maybe_apply_property_list_filter(
    *,
    params: Any,
    response: Any,
    fetcher: PropertyListFetcher | None,
    capability_enabled: bool,
) -> Any:
    """Post-adapter gate: apply property-list filtering to a get_products response.

    Called by the ``get_products`` handler shim after the platform method
    returns.  The gate is a no-op when either:

    * ``capability_enabled`` is False (adopter hasn't declared the feature).
    * ``params.property_list`` is absent (buyer didn't send a list reference).

    When ``fetcher`` is None but the gate would otherwise fire, a WARNING is
    emitted and the response is returned unmodified (defense-in-depth;
    :func:`validate_property_list_config` should have caught this at boot).

    Uses :meth:`model_copy` to avoid mutating the platform's return value.

    :raises AdcpError: ``recovery='transient'`` propagated from
        :func:`resolve_property_list` on fetch failure.
    """
    if not capability_enabled:
        return response

    property_list_ref = getattr(params, "property_list", None)
    if property_list_ref is None:
        return response

    if fetcher is None:
        logger.warning(
            "[adcp.property_list] property_list_filtering capability is declared "
            "and the request carries a property_list reference, but no "
            "PropertyListFetcher was wired — filter skipped. Pass "
            "property_list_fetcher= to "
            "adcp.decisioning.serve.create_adcp_server_from_platform."
        )
        return response

    allowed = await resolve_property_list(property_list_ref, fetcher=fetcher)
    products: list[Any] = list(getattr(response, "products", None) or [])
    filtered = filter_products_by_property_list(products, allowed)

    return response.model_copy(
        update={"products": filtered, "property_list_applied": True}
    )


def validate_property_list_config(
    *,
    capability_enabled: bool,
    fetcher: PropertyListFetcher | None,
) -> None:
    """Boot-time fail-fast: raise when property_list_filtering=True but no fetcher.

    Mirrors :func:`~adcp.decisioning.webhook_emit.validate_webhook_sender_for_platform`:
    a declared capability without the required runtime dependency would silently
    skip filtering at request time — a buyer who sends ``property_list`` would
    receive unfiltered products with ``property_list_applied`` absent or False,
    mismatching what the seller's ``get_adcp_capabilities`` advertised.

    :raises AdcpError: ``recovery='terminal'`` when misconfigured.
    """
    if not capability_enabled:
        return
    if fetcher is not None:
        return

    from adcp.decisioning.types import AdcpError

    raise AdcpError(
        "INVALID_REQUEST",
        message=(
            "Features.property_list_filtering=True is declared in capabilities "
            "but no PropertyListFetcher was wired. Buyers who send "
            "property_list on get_products requests would have their list "
            "filter silently skipped. Pass property_list_fetcher= to "
            "adcp.decisioning.serve.create_adcp_server_from_platform, "
            "or set Features(property_list_filtering=False) to opt out."
        ),
        recovery="terminal",
        details={"missing": "property_list_fetcher"},
    )


__all__ = [
    "PropertyListFetcher",
    "filter_products_by_property_list",
    "maybe_apply_property_list_filter",
    "resolve_property_list",
    "validate_property_list_config",
]
