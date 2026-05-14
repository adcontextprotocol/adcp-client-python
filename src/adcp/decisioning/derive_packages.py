"""Framework-level package derivation from proposal allocations.

When a buyer calls ``create_media_buy(proposal_id=..., total_budget=...)``
without supplying ``packages[]``, the spec lets the seller derive
packages from the committed proposal's ``allocations[]`` array via
percentage math. Every production adopter writes essentially the same
loop:

.. code-block:: python

    for allocation in proposal_payload["allocations"]:
        pkg_budget = total_budget.amount * (allocation["allocation_percentage"] / 100.0)
        packages.append(PackageRequest(
            product_id=allocation["product_id"],
            budget=pkg_budget,
            pricing_option_id=allocation["pricing_option_id"],
        ))

The framework auto-invokes :func:`derive_packages_from_proposal` from
:func:`adcp.decisioning.proposal_dispatch.maybe_hydrate_recipes_for_create_media_buy`
when ``req.packages`` is empty AND the proposal has ``allocations[]``,
so seller adapters see a normal ``create_media_buy`` with populated
packages — they never need to write the loop.

Adopters with custom derivation logic (auction pricing options needing
``bid_price``, multi-currency proposals, capability-overlap filtering)
override the behavior by implementing
:meth:`ProposalManager.derive_packages` on their manager subclass; the
framework dispatches there in preference to the built-in even-split
derivation.

Adopters with workflows outside the auto-injection path (admin-side
inspection tools, draft-preview UIs, off-path replay) can call this
helper directly.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from adcp.decisioning.types import AdcpError

logger = logging.getLogger("adcp.decisioning.derive_packages")

if TYPE_CHECKING:
    from adcp.decisioning.recipe import Recipe
    from adcp.types import PackageRequest


def derive_packages_from_proposal(
    proposal_payload: Mapping[str, Any],
    total_budget: Any,
    *,
    recipes: Mapping[str, Recipe] | None = None,
) -> list[PackageRequest]:
    """Derive a ``list[PackageRequest]`` from a committed proposal's allocations.

    Default even-percentage distribution per ``ProductAllocation``:

    * ``budget = total_budget.amount * allocation.allocation_percentage / 100``
    * ``product_id``, ``pricing_option_id`` copied from the allocation
    * ``start_time`` / ``end_time`` copied when present (per-flight
      scheduling per spec)
    * ``pacing``, ``targeting_overlay``, ``bid_price`` etc. are NOT
      derived — adopters whose proposals carry these per allocation
      should override :meth:`ProposalManager.derive_packages` and emit
      them explicitly.

    **Currency.** Per spec, ``PackageRequest.budget`` is in
    ``total_budget.currency`` (the media-buy-level unit). This helper
    treats ``allocation_percentage`` as a unit-less multiplier; it
    does not inspect or compare any currency carried by the proposal's
    products' pricing_options. Multi-currency adopters should override
    :meth:`ProposalManager.derive_packages` and apply their own FX
    conversion before emitting packages.

    :param proposal_payload: The committed proposal's wire payload
        (typically ``ProposalRecord.proposal_payload``). Must contain
        an ``allocations[]`` array; absent / empty allocations are
        treated as a buyer error and surfaced as ``INVALID_REQUEST``.
    :param total_budget: The ``TotalBudget`` (or dict-shaped equivalent
        with ``amount`` / ``currency`` keys) from the buyer's
        ``CreateMediaBuyRequest``. Must be non-None — the buyer must
        supply ``total_budget`` whenever they omit ``packages``.
    :param recipes: Unused by the built-in derivation. Threaded through
        so the signature matches :meth:`ProposalManager.derive_packages`
        for adopters who delegate to this helper from inside their
        override.

    :raises AdcpError: ``INVALID_REQUEST`` when the proposal payload
        lacks ``allocations[]``, when an allocation is missing required
        fields (``product_id``, ``pricing_option_id``,
        ``allocation_percentage``), or when ``total_budget`` is missing.
    """
    # Local imports — :class:`PackageRequest` lives in adcp.types and we
    # avoid a top-level circular when adcp.types reimports adcp helpers.
    from adcp.types import PackageRequest

    del recipes  # built-in derivation doesn't consult recipes

    if total_budget is None:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "create_media_buy(proposal_id=...) requires total_budget "
                "when packages are omitted; the publisher derives package "
                "budgets by applying the proposal's allocation_percentage "
                "values to total_budget.amount."
            ),
            recovery="correctable",
            field="total_budget",
        )

    budget_amount = _read_attr(total_budget, "amount")
    if budget_amount is None:
        raise AdcpError(
            "INVALID_REQUEST",
            message="total_budget.amount is required for package derivation.",
            recovery="correctable",
            field="total_budget.amount",
        )

    allocations = (
        proposal_payload.get("allocations") if isinstance(proposal_payload, Mapping) else None
    )
    if not allocations or not isinstance(allocations, list):
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "Cannot derive packages: the committed proposal carries no "
                "allocations[]. The buyer must supply packages[] explicitly "
                "or the seller must regenerate the proposal with allocations."
            ),
            recovery="terminal",
            field="proposal_id",
        )

    packages: list[PackageRequest] = []
    for idx, allocation in enumerate(allocations):
        product_id = _read_attr(allocation, "product_id")
        if not product_id:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(f"Cannot derive packages: allocations[{idx}] is missing " "product_id."),
                recovery="terminal",
                field=f"proposal.allocations[{idx}].product_id",
            )
        pct = _read_attr(allocation, "allocation_percentage")
        if pct is None:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    f"Cannot derive packages: allocations[{idx}] for product "
                    f"{product_id!r} is missing allocation_percentage."
                ),
                recovery="terminal",
                field=f"proposal.allocations[{idx}].allocation_percentage",
            )
        pricing_option_id = _read_attr(allocation, "pricing_option_id")
        if not pricing_option_id:
            # Seller-side gap, NOT buyer-correctable. ProductAllocation
            # `pricing_option_id` is optional on the wire (it's only a
            # "recommended" pricing option) but PackageRequest requires
            # one. The built-in even-percentage derivation has no way to
            # pick — only the seller knows whether the product is auction-
            # priced, has multiple options, etc. INTERNAL_ERROR signals
            # this to the buyer as a seller bug; the seller's options
            # are (a) populate allocation.pricing_option_id at proposal-
            # assembly time, (b) ensure products under the proposal have
            # exactly one pricing_options[] entry (framework auto-picks),
            # or (c) implement ProposalManager.derive_packages for
            # auction / multi-option semantics.
            logger.error(
                "Cannot derive packages from proposal allocation %d: "
                "product %r is missing pricing_option_id. Adopter must "
                "set allocation.pricing_option_id at proposal-assembly "
                "time, expose a single product.pricing_options entry, "
                "or implement ProposalManager.derive_packages. The "
                "buyer's create_media_buy will fail until this is fixed.",
                idx,
                product_id,
            )
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"Seller configuration error: proposal allocation for "
                    f"product {product_id!r} is missing pricing_option_id. "
                    "Contact the seller — this is not a buyer-correctable "
                    "input."
                ),
                recovery="terminal",
            )

        pkg_budget = float(budget_amount) * (float(pct) / 100.0)
        # Spec-defined per-allocation flight scheduling — when the
        # seller's proposal carries allocation.start_time/end_time,
        # propagate them to the derived package. ``ProductAllocation``
        # docs them as "allows publishers to propose per-flight
        # scheduling within a proposal." Dropping them silently would
        # erase seller intent.
        kwargs: dict[str, Any] = {
            "product_id": str(product_id),
            "budget": pkg_budget,
            "pricing_option_id": str(pricing_option_id),
        }
        start_time = _read_attr(allocation, "start_time")
        if start_time is not None:
            kwargs["start_time"] = start_time
        end_time = _read_attr(allocation, "end_time")
        if end_time is not None:
            kwargs["end_time"] = end_time
        packages.append(PackageRequest(**kwargs))
    return packages


def _read_attr(obj: Any, name: str) -> Any:
    """Read an attribute or mapping value, normalizing the two shapes.

    Mirrors the helper inside :mod:`adcp.decisioning.proposal_dispatch`;
    duplicated here to keep this module dependency-free.
    """
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


__all__ = ["derive_packages_from_proposal"]
