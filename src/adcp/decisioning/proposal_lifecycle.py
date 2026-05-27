"""Proposal-lifecycle framework helpers — the v1.5 intercept seam.

Sits parallel to :mod:`adcp.decisioning.refine` and
:mod:`adcp.decisioning.webhook_emit`: framework intercepts at a seam,
does its work, dispatches.

Public surface (the dispatch path imports these):

* :func:`enforce_proposal_expiry` — D7. Look up the committed proposal,
  validate it hasn't expired (state == COMMITTED, ``now <= expires_at +
  grace``), return the record.
* :func:`validate_capability_overlap` — D4. Walk a buyer's
  ``create_media_buy`` / ``update_media_buy`` request against each
  package's recipe ``capability_overlap`` and reject mismatches with
  ``INVALID_REQUEST``.
* :func:`validate_overlap_subset_of_wire` — D4 round-4. Validate at
  ``put_draft`` time that each recipe's ``capability_overlap`` axis is
  a subset of the corresponding wire-declared product capabilities.
  Mismatches raise ``INTERNAL_ERROR`` (adopter bug, not buyer bug).
* :func:`detect_finalize_action` — pull out a finalize-action refine
  entry from a ``GetProductsRequest``.
* :func:`finalize_succeeded_log` / :func:`finalize_handoff_log` /
  :func:`draft_persisted_log` / :func:`expired_log` /
  :func:`consumed_log` — structured log emit helpers per § Observability.

The clock is the process clock
(``datetime.now(timezone.utc)``). Multi-worker deployments accept the
NTP-sync responsibility for tight grace windows (per § D7).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from adcp.decisioning.proposal_store import (
    ProposalRecord,
    ProposalState,
    _await_maybe,
)
from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from adcp.decisioning.proposal_store import ProposalStore
    from adcp.decisioning.recipe import Recipe


logger = logging.getLogger("adcp.decisioning.proposal_lifecycle")


# ---------------------------------------------------------------------------
# D7 — expires_at enforcement
# ---------------------------------------------------------------------------


async def enforce_proposal_expiry(
    proposal_id: str,
    *,
    proposal_store: ProposalStore,
    expected_account_id: str,
    grace_seconds: int = 0,
    now: datetime | None = None,
) -> ProposalRecord:
    """Validate a proposal is committed and within its hold window.

    Three failure modes mapped to spec error codes:

    * Record not found OR cross-tenant → ``PROPOSAL_NOT_FOUND``
      (recovery=correctable). The buyer can request and finalize a fresh
      proposal_id. The dispatch path supplies
      ``expected_account_id`` from the authenticated principal so
      cross-tenant probes return the same error as missing IDs (no
      principal-enumeration via id probing).
    * Record present but state != COMMITTED → ``PROPOSAL_NOT_COMMITTED``
      (recovery=correctable). The buyer needs to call
      ``get_products(buying_mode='refine', refine=[{action:'finalize'}])``
      first.
    * Record committed but ``now > expires_at + grace`` →
      ``PROPOSAL_EXPIRED`` (recovery=terminal). The inventory hold
      window has lapsed; the buyer needs a fresh finalize.

    :param proposal_id: The buyer-supplied proposal_id from the
        ``create_media_buy`` request.
    :param proposal_store: The tenant's wired
        :class:`~adcp.decisioning.ProposalStore`.
    :param expected_account_id: Authenticated principal's account id
        — drives the cross-tenant safety check on
        :meth:`ProposalStore.get`.
    :param grace_seconds: Adopter-declared slack between
        ``expires_at`` and the hard rejection (read from
        :attr:`ProposalCapabilities.expires_at_grace_seconds`).
    :param now: Test injectable; defaults to
        ``datetime.now(timezone.utc)``.
    :returns: The :class:`~adcp.decisioning.ProposalRecord` for the
        committed proposal. Caller hydrates ``ctx.recipes`` from
        :attr:`ProposalRecord.recipes`.
    :raises AdcpError: as documented above.
    """
    raw = await _await_maybe(
        proposal_store.get(proposal_id, expected_account_id=expected_account_id)
    )
    record = cast("ProposalRecord | None", raw)
    if record is None:
        raise AdcpError(
            "PROPOSAL_NOT_FOUND",
            message=(
                f"Proposal {proposal_id!r} not found. The buyer must call "
                "get_products with buying_mode='refine' and "
                "refine=[{action:'finalize',...}] to obtain a committed "
                "proposal_id before referencing it on create_media_buy."
            ),
            recovery="correctable",
            field="proposal_id",
        )
    if record.state != ProposalState.COMMITTED:
        raise AdcpError(
            "PROPOSAL_NOT_COMMITTED",
            message=(
                f"Proposal {proposal_id!r} is in state "
                f"{record.state.value!r}; only committed proposals can "
                "be accepted via create_media_buy. Call get_products "
                "with buying_mode='refine' and action='finalize' first."
            ),
            recovery="correctable",
            field="proposal_id",
        )
    if record.expires_at is not None:
        current = now if now is not None else datetime.now(timezone.utc)
        deadline = record.expires_at + timedelta(seconds=grace_seconds)
        if current > deadline:
            expired_log(
                proposal_id=proposal_id,
                account_id=record.account_id,
                now=current,
                expires_at=record.expires_at,
                grace_seconds=grace_seconds,
            )
            raise AdcpError(
                "PROPOSAL_EXPIRED",
                message=(
                    f"Proposal {proposal_id!r} expired at "
                    f"{record.expires_at.isoformat()}; create_media_buy "
                    "must be called within the inventory hold window. "
                    "Call get_products with buying_mode='refine' and "
                    "action='finalize' to request a fresh hold."
                ),
                recovery="terminal",
                field="proposal_id",
            )
    return record


# ---------------------------------------------------------------------------
# D4 — capability-overlap validation
# ---------------------------------------------------------------------------


def validate_capability_overlap(
    *,
    packages: list[Any],
    recipes: Mapping[str, Recipe],
    field_path_prefix: str = "packages",
) -> None:
    """Pre-adapter validation seam: walk buyer's packages against
    each recipe's ``capability_overlap`` and reject mismatches.

    Called from the framework's ``create_media_buy`` and
    ``update_media_buy`` dispatch paths after recipes are hydrated
    from the :class:`~adcp.decisioning.ProposalStore`. Per D4, the
    framework owns this gate so every adopter doesn't write the same
    intersection logic.

    Validation axes (per :class:`~adcp.decisioning.CapabilityOverlap`):

    * ``pricing_models`` — checked against
      ``package.pricing_option_id`` resolved against the matching
      :class:`~adcp.types.PricingOption.pricing_model`. (Adopter
      passes pre-resolved pricing models via the
      ``_resolved_pricing_model`` per-package extra; the validation
      reads it.)
    * ``targeting_dimensions`` — checked against
      ``package.targeting_overlay`` keys.
    * ``delivery_types`` — checked against the matching product's
      delivery type (passed via ``_resolved_delivery_type``).
    * ``signal_types`` — checked against
      ``package.signal_type`` if present on the package.

    Per the design's None-vs-frozenset semantics: ``None`` skips the
    gate; an explicit ``frozenset`` (including the empty set) is
    enforced.

    :param packages: The buyer's request ``packages[]``. Each package
        carries (at minimum) ``product_id`` and the typed fields the
        validator inspects.
    :param recipes: ``product_id -> Recipe`` mapping hydrated from the
        :class:`~adcp.decisioning.ProposalStore`.
    :param field_path_prefix: Prefix for the ``field`` path on raised
        :class:`AdcpError`s. Defaults to ``"packages"``;
        ``update_media_buy`` callers pass a different prefix to match
        their wire shape.
    :raises AdcpError: ``INVALID_REQUEST`` with a ``field`` path
        pointing at the offending package element, ``recovery="terminal"``.
    """
    for i, package in enumerate(packages):
        product_id = _get_attr(package, "product_id")
        if product_id is None:
            # Package without product_id — skip; the framework's
            # request validation catches malformed packages elsewhere.
            continue
        recipe = recipes.get(str(product_id))
        if recipe is None or recipe.capability_overlap is None:
            continue
        overlap = recipe.capability_overlap

        if overlap.pricing_models is not None:
            requested = _get_attr(package, "_resolved_pricing_model") or _get_attr(
                package, "pricing_model"
            )
            if requested is not None and str(requested) not in overlap.pricing_models:
                raise AdcpError(
                    "INVALID_REQUEST",
                    message=(
                        f"Buyer requested pricing_model={str(requested)!r} on "
                        f"package {product_id!r}, but this product's recipe "
                        "declares "
                        f"capability_overlap.pricing_models="
                        f"{sorted(overlap.pricing_models)!r}. The seller did "
                        "not enable that pricing model for this product."
                    ),
                    recovery="terminal",
                    field=f"{field_path_prefix}[{i}].pricing_option_id",
                )

        if overlap.targeting_dimensions is not None:
            targeting_overlay = _get_attr(package, "targeting_overlay") or {}
            keys: set[str] = set()
            if isinstance(targeting_overlay, Mapping):
                keys = {str(k) for k in targeting_overlay.keys()}
            elif hasattr(targeting_overlay, "model_dump"):
                dumped = targeting_overlay.model_dump(exclude_none=True)
                if isinstance(dumped, dict):
                    keys = {str(k) for k in dumped.keys()}
            disallowed = keys - overlap.targeting_dimensions
            if disallowed:
                raise AdcpError(
                    "INVALID_REQUEST",
                    message=(
                        f"Buyer requested targeting dimensions "
                        f"{sorted(disallowed)!r} on package {product_id!r}, "
                        "but this product's recipe declares "
                        f"capability_overlap.targeting_dimensions="
                        f"{sorted(overlap.targeting_dimensions)!r}. The "
                        "seller did not enable those targeting dimensions "
                        "for this product."
                    ),
                    recovery="terminal",
                    field=f"{field_path_prefix}[{i}].targeting_overlay",
                )

        if overlap.delivery_types is not None:
            delivery = _get_attr(package, "_resolved_delivery_type") or _get_attr(
                package, "delivery_type"
            )
            if delivery is not None and str(delivery) not in overlap.delivery_types:
                raise AdcpError(
                    "INVALID_REQUEST",
                    message=(
                        f"Buyer requested delivery_type={str(delivery)!r} on "
                        f"package {product_id!r}, but this product's recipe "
                        "declares "
                        f"capability_overlap.delivery_types="
                        f"{sorted(overlap.delivery_types)!r}."
                    ),
                    recovery="terminal",
                    field=f"{field_path_prefix}[{i}].delivery_type",
                )

        if overlap.signal_types is not None:
            signal_type = _get_attr(package, "signal_type")
            if signal_type is not None and str(signal_type) not in overlap.signal_types:
                raise AdcpError(
                    "INVALID_REQUEST",
                    message=(
                        f"Buyer requested signal_type={str(signal_type)!r} on "
                        f"package {product_id!r}, but this product's recipe "
                        "declares "
                        f"capability_overlap.signal_types="
                        f"{sorted(overlap.signal_types)!r}."
                    ),
                    recovery="terminal",
                    field=f"{field_path_prefix}[{i}].signal_type",
                )


def validate_overlap_subset_of_wire(
    *,
    recipes: Mapping[str, Recipe],
    products: list[Any],
) -> None:
    """Validate ``recipe.capability_overlap`` is a subset of the
    matching product's wire-declared capabilities.

    Called at ``put_draft`` time. Mismatches raise ``INTERNAL_ERROR``
    — this is an adopter bug (the manager declared an overlap claiming
    capabilities the wire shape doesn't advertise), not a buyer bug.
    Catches the case where the buyer pre-flights against the wire,
    sees ``cpm`` only, but the adopter's overlap has ``cpcv`` enabled
    via stale config.

    Per § D4 round-4 dual-sourcing concern: the wire declares the full
    set of options on
    ``Product.pricing_options[*].pricing_model``; the recipe declares
    a subset. Drift is an adopter bug.

    :param recipes: ``product_id -> Recipe`` mapping returned alongside
        the products on get_products / refine_products.
    :param products: The list of wire :class:`~adcp.types.Product`
        objects from the response. Iterated to extract per-product
        wire capability sets.
    :raises AdcpError: ``INTERNAL_ERROR`` when an overlap axis exceeds
        the wire-declared capabilities for that product.
    """
    products_by_id = {}
    for product in products:
        product_id = _get_attr(product, "product_id")
        if product_id is not None:
            products_by_id[str(product_id)] = product

    for product_id, recipe in recipes.items():
        if recipe.capability_overlap is None:
            continue
        product = products_by_id.get(product_id)
        if product is None:
            # Recipe declared for a product not in the response —
            # adopter bug, but not strictly a wire-overlap drift.
            # Skip; the dispatch path catches missing products
            # elsewhere.
            continue
        overlap = recipe.capability_overlap

        if overlap.pricing_models is not None:
            wire_pricing = _wire_pricing_models(product)
            extras = overlap.pricing_models - wire_pricing
            if extras:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Recipe for product {product_id!r} declares "
                        f"capability_overlap.pricing_models="
                        f"{sorted(overlap.pricing_models)!r} including "
                        f"{sorted(extras)!r}, but the wire product only "
                        f"advertises {sorted(wire_pricing)!r}. The recipe's "
                        "overlap must be a subset of the wire-declared "
                        "capabilities; adopter declaration is inconsistent "
                        "with the product shape."
                    ),
                    recovery="terminal",
                )

        if overlap.delivery_types is not None:
            wire_delivery = _wire_delivery_types(product)
            extras = overlap.delivery_types - wire_delivery
            if extras and wire_delivery:
                # Only enforce when the wire declares something —
                # products lacking a delivery_type field shouldn't
                # trip the gate (the wire validation catches that).
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Recipe for product {product_id!r} declares "
                        f"capability_overlap.delivery_types="
                        f"{sorted(overlap.delivery_types)!r} including "
                        f"{sorted(extras)!r}, but the wire product only "
                        f"advertises {sorted(wire_delivery)!r}."
                    ),
                    recovery="terminal",
                )


def _wire_pricing_models(product: Any) -> frozenset[str]:
    """Extract the set of ``pricing_model`` values from a wire Product."""
    pricing_options = _get_attr(product, "pricing_options") or []
    out: set[str] = set()
    for opt in pricing_options:
        pricing_model = _get_attr(opt, "pricing_model")
        if pricing_model is not None:
            # Pydantic enum or string both supported.
            out.add(str(getattr(pricing_model, "value", pricing_model)))
    return frozenset(out)


def _wire_delivery_types(product: Any) -> frozenset[str]:
    """Extract ``delivery_type`` from a wire Product as a singleton set."""
    delivery_type = _get_attr(product, "delivery_type")
    if delivery_type is None:
        return frozenset()
    return frozenset({str(getattr(delivery_type, "value", delivery_type))})


def _get_attr(obj: Any, name: str) -> Any:
    """Read ``name`` off either an attribute or a mapping value.

    The framework receives both Pydantic-model packages and dict-shaped
    packages from various dispatch paths; this helper normalizes the
    two so the validators don't branch on shape.
    """
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


# ---------------------------------------------------------------------------
# Finalize action detection
# ---------------------------------------------------------------------------


def detect_finalize_action(req: Any) -> tuple[int, str, str | None] | None:
    """Return ``(index, proposal_id, ask)`` for the first finalize-action
    refine entry on a ``GetProductsRequest``, or ``None`` if no
    finalize entry exists.

    The index points at the entry's position in ``refine[]`` so the
    framework can produce indexed wire field paths
    (``refine[3].proposal_id``) on rejection — buyers parsing the
    error get a precise pointer rather than a wildcard.

    Per the spec, ``buying_mode='refine'`` carries a ``refine[]`` array
    of entries. Each entry has a ``scope`` (``request`` / ``product``
    / ``proposal``) and an optional ``action`` (``include`` / ``omit``
    / ``finalize``). v1.5 only intercepts ``proposal``-scoped entries
    with ``action='finalize'``.

    The framework processes ONE finalize entry per request; if the
    buyer sends multiple finalize entries, only the first is
    processed (rest fall through to the standard refine path). v1.5
    doesn't support multi-finalize; v1.6+ may extend.

    :returns: ``(proposal_id, ask)`` from the first finalize entry, or
        ``None`` when no entry has ``action='finalize'``.
    """
    refine = _get_attr(req, "refine")
    if not refine:
        return None
    for index, entry in enumerate(refine):
        # Refine entries are :class:`Refine` RootModels; unwrap.
        inner = getattr(entry, "root", entry)
        scope = _get_attr(inner, "scope")
        # Coerce enum to value
        scope_str = str(getattr(scope, "value", scope)) if scope is not None else None
        action = _get_attr(inner, "action")
        action_str = str(getattr(action, "value", action)) if action is not None else None
        if scope_str == "proposal" and action_str == "finalize":
            proposal_id = _get_attr(inner, "proposal_id")
            ask = _get_attr(inner, "ask")
            if proposal_id is not None:
                return (index, str(proposal_id), str(ask) if ask is not None else None)
    return None


# ---------------------------------------------------------------------------
# Structured logging — § Observability
# ---------------------------------------------------------------------------


def draft_persisted_log(
    *,
    proposal_id: str,
    account_id: str,
    recipes_count: int,
) -> None:
    """``proposal.draft_persisted`` event."""
    logger.info(
        "proposal.draft_persisted",
        extra={
            "event": "proposal.draft_persisted",
            "proposal_id": proposal_id,
            "account_id": account_id,
            "recipes_count": recipes_count,
        },
    )


def finalize_succeeded_log(
    *,
    proposal_id: str,
    account_id: str,
    expires_at: datetime,
    path: str,
) -> None:
    """``proposal.finalized`` event. ``path`` is ``"inline"`` or ``"handoff"``."""
    logger.info(
        "proposal.finalized",
        extra={
            "event": "proposal.finalized",
            "proposal_id": proposal_id,
            "account_id": account_id,
            "expires_at": expires_at.isoformat(),
            "path": path,
        },
    )


# Alias for callers that explicitly want the handoff-path log.
def finalize_handoff_log(
    *,
    proposal_id: str,
    account_id: str,
    expires_at: datetime,
) -> None:
    finalize_succeeded_log(
        proposal_id=proposal_id,
        account_id=account_id,
        expires_at=expires_at,
        path="handoff",
    )


def expired_log(
    *,
    proposal_id: str,
    account_id: str,
    now: datetime,
    expires_at: datetime,
    grace_seconds: int,
) -> None:
    """``proposal.expired`` event."""
    logger.info(
        "proposal.expired",
        extra={
            "event": "proposal.expired",
            "proposal_id": proposal_id,
            "account_id": account_id,
            "now": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "grace_seconds": grace_seconds,
        },
    )


def consumed_log(
    *,
    proposal_id: str,
    account_id: str,
    media_buy_id: str,
) -> None:
    """``proposal.consumed`` event."""
    logger.info(
        "proposal.consumed",
        extra={
            "event": "proposal.consumed",
            "proposal_id": proposal_id,
            "account_id": account_id,
            "media_buy_id": media_buy_id,
        },
    )


__all__ = [
    "consumed_log",
    "detect_finalize_action",
    "draft_persisted_log",
    "enforce_proposal_expiry",
    "expired_log",
    "finalize_handoff_log",
    "finalize_succeeded_log",
    "validate_capability_overlap",
    "validate_overlap_subset_of_wire",
]
