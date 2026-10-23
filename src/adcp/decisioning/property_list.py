"""Property-list resolver and product intersection helper for get_products.

Capability-gated framework support for the ``property_list`` buyer-side filter
on ``get_products``.  When an adopter declares
``Features(property_list_filtering=True)`` on their
:class:`~adcp.decisioning.platform.DecisioningCapabilities`, the framework
automatically (using MCP by default):

1. Resolves the buyer's typed property list over MCP, including all pages.
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

from pydantic import RootModel

logger = logging.getLogger(__name__)


@runtime_checkable
class PropertyListFetcher(Protocol):
    """Adopter-supplied protocol for fetching a buyer's authorized property IDs.

    Custom fetchers may return publisher property IDs for existing integrations.
    The default :class:`adcp.property_lists.MCPPropertyListFetcher` resolves
    typed identifiers using the MCP ``get_property_list`` tool at ``agent_url``
    with the reference's bearer token, follows pagination, and caches until
    ``cache_valid_until``. No REST path convention is assumed.

    Custom network implementations MUST pin delivery to a validated public IP,
    disable redirects and environment proxies, and never log bearer tokens.
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
    fetcher: PropertyListFetcher | None = None,
) -> set[str]:
    """Resolve canonical identifier values (or legacy custom-fetcher property IDs).

    :param ref: ``PropertyList`` wire object (has ``agent_url``, ``list_id``,
        ``auth_token``).
    :param fetcher: Optional custom fetcher; defaults to a cached MCP resolver.
    :returns: Set of allowed canonical values. Use MCPPropertyListFetcher.resolve
        when identifier types are needed for matching.
    :raises AdcpError: ``recovery='transient'`` on any fetch failure.
        ``auth_token`` is never included in the error details.
    """
    from adcp.decisioning.types import AdcpError
    from adcp.property_lists import MCPPropertyListFetcher, _default_property_list_fetcher
    from adcp.webhooks import (
        WebhookDestinationPolicy,
        WebhookDestinationValidationError,
        validate_webhook_destination_url,
    )

    fetcher = fetcher if fetcher is not None else _default_property_list_fetcher()
    if isinstance(fetcher, MCPPropertyListFetcher):
        resolved = await fetcher.resolve(ref)
        return {identifier.value for identifier in resolved.identifiers}
    list_id: str = _field(ref, "list_id")
    agent_url: str = str(_field(ref, "agent_url"))
    auth_token: str | None = _field(ref, "auth_token")

    if not re.fullmatch(r"[A-Za-z0-9._~-]+", list_id):
        raise AdcpError(
            "INVALID_REQUEST",
            message="Property list_id must be a single URL-safe path segment",
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


def _prepare_compact_property_list_fields(
    params: Any, *, capability_enabled: bool, filter_mode: str
) -> tuple[Any, frozenset[str]]:
    """Ask the platform for inventory needed by SDK filtering before projection."""
    from adcp.property_lists import request_property_list

    fields = _field(params, "fields")
    if (
        filter_mode != "sdk"
        or not capability_enabled
        or request_property_list(params) is None
        or fields is None
    ):
        return params, frozenset()
    selected = list(fields.root)
    added = frozenset({"publisher_properties", "overlay_support"} - set(selected))
    if not added:
        return params, added
    enriched = type(fields).model_validate([*selected, *sorted(added)])
    return params.model_copy(update={"fields": enriched}), added


def _restore_compact_property_list_fields(response: Any, added: frozenset[str]) -> Any:
    """Hide fields requested internally while retaining platform safety receipts."""
    products = _field(response, "products")
    if not added or products is None:
        return response
    projected = [
        (
            {name: value for name, value in product.items() if name not in added}
            if isinstance(product, dict)
            else product.model_copy(update=dict.fromkeys(added))
        )
        for product in products
    ]
    if isinstance(response, dict):
        return {**response, "products": projected}
    return response.model_copy(update={"products": projected})


def _product_matches(product: Any, allowed: set[str]) -> bool:
    """Return True if the product should be included after property-list filtering."""
    overlay = _field(_field(product, "overlay_support"), "property_list")
    if isinstance(overlay, RootModel):
        overlay = overlay.root
    permissive: bool = bool(_field(product, "property_targeting_allowed", False)) or overlay is True
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
            # str(pid) already covers a scalar schema; only a RootModel wrapper
            # needs unwrapping (#1277).
            product_ids = {
                (pid.root if isinstance(pid, RootModel) else str(pid)) for pid in raw_ids
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
    filter_mode: Literal["sdk", "platform"] = "sdk",
) -> Any:
    """Post-adapter gate: apply property-list filtering to a get_products response.

    Called by the ``get_products`` handler shim after the platform method
    returns.  The gate is a no-op when either:

    * ``capability_enabled`` is False (adopter hasn't declared the feature).
    * ``params.property_list`` is absent (buyer didn't send a list reference).

    When ``fetcher`` is None, use the built-in MCP resolver.

    Platform mode returns the seller's response untouched, including its
    ``property_list_applied`` flag. SDK mode copies dict/model responses to
    avoid mutating the platform's return value.

    :raises AdcpError: ``recovery='transient'`` propagated from
        :func:`resolve_property_list` on fetch failure.
    """
    if filter_mode == "platform" or not capability_enabled:
        return response

    from adcp.property_lists import (
        MCPPropertyListFetcher,
        ProductPropertyPolicy,
        _default_property_list_fetcher,
        request_property_list,
    )

    property_list_ref = request_property_list(params)
    if property_list_ref is None:
        return response

    # Submitted/unchanged/error envelopes must keep their original semantics.
    if _field(response, "status") in {"submitted", "working", "input-required"}:
        return response
    outcome = _field(response, "outcome")
    if outcome in {"unchanged", "rejected"} or _field(response, "products") is None:
        return response
    fetcher = fetcher if fetcher is not None else _default_property_list_fetcher()
    products: list[Any] = list(_field(response, "products") or [])
    applied = True
    if isinstance(fetcher, MCPPropertyListFetcher):
        allowed_list = await fetcher.resolve(property_list_ref)
        policy = ProductPropertyPolicy(fetcher=fetcher)
        # Products with no declared property inventory cannot match a property
        # list. Exclude them, as the custom property-ID path already does.
        candidates = [product for product in products if _field(product, "publisher_properties")]
        assessments = await policy.assess(candidates, allowed_list, seller_url="")
        # Declared inventory that cannot be resolved remains a transient error.
        if any(item.status == "unjudgeable" for item in assessments):
            from adcp.decisioning.types import AdcpError

            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Product property expansion failed",
            )
        filtered = [
            product for product, item in zip(candidates, assessments) if item.status == "match"
        ]
    else:
        allowed = await resolve_property_list(property_list_ref, fetcher=fetcher)
        filtered = filter_products_by_property_list(products, allowed)

    update: dict[str, Any] = {"products": filtered}
    # Compact response schemas forbid arbitrary top-level fields. Report the
    # acknowledgement through ext; legacy discovery has a dedicated field.
    if outcome in {"listed", "products_available", "proposed"}:
        ext = _field(response, "ext") or {}
        if hasattr(ext, "model_dump"):
            ext = ext.model_dump(mode="json")
        update["ext"] = {**ext, "property_list_applied": applied}
        if not isinstance(response, dict):
            from adcp.types.domains.core.ext import ExtensionObject

            update["ext"] = ExtensionObject.model_validate(update["ext"])
    else:
        update["property_list_applied"] = applied

    if outcome in {"products_available", "proposed"} and len(filtered) != len(products):
        continuation = _field(response, "purchase_continuation")
        if (
            not filtered
            or outcome == "proposed"
            or _field(continuation, "kind") != "listed_purchase"
        ):
            # Proposals have immutable commercial terms and a bound digest;
            # legacy continuations bind an opaque observed payload. Neither
            # can be rewritten safely after removing one of its products.
            rejected = (
                dict(response)
                if isinstance(response, dict)
                else response.model_dump(mode="json", exclude_none=True)
            )
            rejected.update(
                outcome="rejected",
                reason="The available products do not satisfy the requested property list.",
                ext=(
                    update["ext"]
                    if isinstance(update["ext"], dict)
                    else update["ext"].model_dump(mode="json")
                ),
            )
            for name in ("products", "proposals", "purchase_continuation", "incomplete", "task_id"):
                rejected.pop(name, None)
            if isinstance(response, dict):
                return rejected
            from pydantic import TypeAdapter

            from adcp.types.domains.media_buy.request_proposals_response import (
                RequestProposalsResponse,
            )

            return TypeAdapter(RequestProposalsResponse).validate_python(rejected)
        product_ids = [str(_field(product, "product_id")) for product in filtered]
        update["purchase_continuation"] = (
            {**continuation, "product_ids": product_ids}
            if isinstance(continuation, dict)
            else continuation.model_copy(update={"product_ids": product_ids})
        )
    if isinstance(response, dict):
        return {**response, **update}
    return response.model_copy(update=update)


def validate_property_list_config(
    *,
    capability_enabled: bool,
    fetcher: PropertyListFetcher | None,
    filter_mode: Literal["sdk", "platform"] = "sdk",
) -> None:
    """Validate ownership mode; SDK mode has a built-in MCP fetcher."""
    if filter_mode not in {"sdk", "platform"}:
        raise ValueError("property_list_filter_mode must be 'sdk' or 'platform'")
    # A built-in MCP fetcher is available when no custom dependency is supplied.
    return


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
