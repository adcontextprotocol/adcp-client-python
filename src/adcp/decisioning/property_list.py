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
query) retain ``Features(property_list_filtering=True)`` and pass
``property_list_filter_mode="platform"`` to the server builder. They own both
filtering and the ``property_list_applied`` response flag; the SDK does not fetch
or filter in that mode.

Reference pattern: :mod:`adcp.decisioning.webhook_emit` (capability-gated
post-adapter side effect).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


@runtime_checkable
class PropertyListFetcher(Protocol):
    """Adopter-supplied protocol for fetching a buyer's authorized property IDs.

    The framework calls :meth:`fetch` when ``property_list_filtering`` is
    declared in capabilities and the request carries a ``PropertyList``
    reference.  Adopters plug in their own HTTP client — the framework ships
    no hidden HTTP dependency.

    Implementations MUST pin delivery to the validated IP, disable redirects
    and environment proxies, and URL-encode ``list_id``. A safe httpx pattern::

        class MyFetcher:
            async def fetch(
                self,
                agent_url: str,
                list_id: str,
                *,
                auth_token: str | None = None,
            ) -> list[str]:
                url = f"{agent_url.rstrip('/')}/property-lists/{quote(list_id, safe='')}"
                transport = build_async_ip_pinned_transport(url)
                headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
                async with httpx.AsyncClient(
                    transport=transport,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    resp = await client.get(url, headers=headers)
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
    from adcp.webhooks import (
        WebhookDestinationPolicy,
        WebhookDestinationValidationError,
        validate_webhook_destination_url,
    )

    list_id: str = ref.list_id
    agent_url: str = str(ref.agent_url)
    auth_token: str | None = getattr(ref, "auth_token", None)

    if not re.fullmatch(r"[A-Za-z0-9._~-]+", list_id):
        raise AdcpError(
            "INVALID_REQUEST",
            message="Property list_id must be a single URL-safe path segment",
            recovery="correctable",
            details={"list_id": list_id},
        )

    try:
        validation = validate_webhook_destination_url(
            agent_url,
            policy=WebhookDestinationPolicy.production(),
            field="property_list.agent_url",
        )
    except WebhookDestinationValidationError as exc:
        raise AdcpError(
            "INVALID_REQUEST",
            message="Property list agent_url failed destination policy",
            recovery="correctable",
            details={"reason": exc.reason},
        ) from None

    parsed = urlsplit(validation.original_url)
    safe_origin = f"{parsed.scheme}://{parsed.hostname or ''}"
    if parsed.port is not None:
        safe_origin += f":{parsed.port}"

    try:
        ids = await fetcher.fetch(validation.original_url, list_id, auth_token=auth_token)
        return set(ids)
    except Exception as exc:
        # Exception text may carry auth_token or credential-shaped upstream
        # values. Log only the class and deliberately omit the exception chain.
        logger.warning(
            "[adcp.property_list] fetch failed for list_id=%r agent_origin=%r (%s)",
            list_id,
            safe_origin,
            type(exc).__name__,
        )
        raise AdcpError(
            "SERVICE_UNAVAILABLE",
            message=(
                f"Property list fetch failed for list_id={list_id!r} "
                f"from agent_origin={safe_origin!r}"
            ),
            recovery="transient",
            details={"list_id": list_id, "agent_origin": safe_origin},
        ) from None


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


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a wire dict or a model."""
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _product_matches(product: Any, allowed: set[str]) -> bool:
    """Return True if the product should be included after property-list filtering."""
    permissive: bool = bool(_field(product, "property_targeting_allowed", False))
    pub_props: list[Any] = list(_field(product, "publisher_properties") or [])
    product_id: str = str(_field(product, "product_id", "?"))

    for pp_wrapper in pub_props:
        # PublisherProperties is a RootModel; unwrap to the discriminated variant.
        pp = _field(pp_wrapper, "root", pp_wrapper)
        st = _field(pp, "selection_type")

        if st == "all":
            logger.debug(
                "[adcp.property_list] product %r: selection_type='all' → include",
                product_id,
            )
            return True

        if st == "by_id":
            raw_ids: list[Any] = list(_field(pp, "property_ids") or [])
            product_ids = {(pid.root if hasattr(pid, "root") else str(pid)) for pid in raw_ids}
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
    filter_mode: Literal["sdk", "platform"] = "sdk",
) -> Any:
    """Post-adapter gate: apply property-list filtering to a get_products response.

    Called by the ``get_products`` handler shim after the platform method
    returns.  The gate is a no-op when either:

    * ``capability_enabled`` is False (adopter hasn't declared the feature).
    * ``params.property_list`` is absent (buyer didn't send a list reference).

    When ``fetcher`` is None but the gate would otherwise fire, a WARNING is
    emitted and the response is returned unmodified (defense-in-depth;
    :func:`validate_property_list_config` should have caught this at boot).

    Platform mode returns the seller's response untouched, including its
    ``property_list_applied`` flag. SDK mode copies dict/model responses to
    avoid mutating the platform's return value.

    :raises AdcpError: ``recovery='transient'`` propagated from
        :func:`resolve_property_list` on fetch failure.
    """
    if filter_mode == "platform" or not capability_enabled:
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
    products: list[Any] = list(_field(response, "products") or [])
    filtered = filter_products_by_property_list(products, allowed)

    update = {"products": filtered, "property_list_applied": True}
    if isinstance(response, dict):
        return {**response, **update}
    return response.model_copy(update=update)


def validate_property_list_config(
    *,
    capability_enabled: bool,
    fetcher: PropertyListFetcher | None,
    filter_mode: Literal["sdk", "platform"] = "sdk",
) -> None:
    """Require a fetcher for SDK-owned filtering; platform mode needs none.

    Mirrors :func:`~adcp.decisioning.webhook_emit.validate_webhook_sender_for_platform`:
    a declared capability without the required runtime dependency would silently
    skip filtering at request time — a buyer who sends ``property_list`` would
    receive unfiltered products with ``property_list_applied`` absent or False,
    mismatching what the seller's ``get_adcp_capabilities`` advertised.

    :raises AdcpError: ``recovery='terminal'`` when misconfigured.
    """
    if filter_mode not in {"sdk", "platform"}:
        raise ValueError("property_list_filter_mode must be 'sdk' or 'platform'")
    if filter_mode == "platform" or not capability_enabled:
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
            'or pass property_list_filter_mode="platform" to own filtering '
            "and the property_list_applied flag in your platform."
        ),
        recovery="terminal",
        details={"missing": "property_list_fetcher"},
    )


def property_list_capability_enabled(platform: Any) -> bool:
    """Return True if ``platform.capabilities.media_buy.features.property_list_filtering`` is set.

    Centralises the three-level ``getattr`` chain used in both ``handler.py``
    and ``serve.py`` so they can't drift apart.
    """
    media_buy = getattr(getattr(platform, "capabilities", None), "media_buy", None)
    features = getattr(media_buy, "features", None)
    return bool(getattr(features, "property_list_filtering", False))


__all__ = [
    "PropertyListFetcher",
    "filter_products_by_property_list",
    "maybe_apply_property_list_filter",
    "property_list_capability_enabled",
    "resolve_property_list",
    "validate_property_list_config",
]
