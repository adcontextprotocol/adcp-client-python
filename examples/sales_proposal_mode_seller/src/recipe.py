"""Concrete :class:`Recipe` subclass for the proposal-mode mock seller.

Adopter pattern: declare a ``recipe_kind`` Literal as discriminator, add
typed fields the adapter reads at execute time, declare
``capability_overlap`` to gate buyer requests pre-adapter.

The framework's :class:`InMemoryProposalStore` carries this typed
recipe through the proposal lifecycle; the adapter casts back to this
class on ``create_media_buy`` / ``update_media_buy`` /
``get_media_buy_delivery``.
"""

from __future__ import annotations

from typing import Literal

from adcp.decisioning import CapabilityOverlap, Recipe


class ProposalModeRecipe(Recipe):
    """Per-product implementation_config for the proposal-mode demo.

    :attr:`line_item_template_id`: synthetic placeholder for the
        adapter's underlying booking system. Real adopters carry the
        platform-specific identifiers their adapter needs (GAM line-item
        template id, Kevel zone id, etc.).
    :attr:`floor_cpm`: locked-in CPM for the committed proposal. The
        finalize transition is what locks this; refine iterations may
        update it.
    :attr:`capability_overlap`: framework-validated subset of wire
        capabilities. Buyer requests outside this overlap are rejected
        before the adapter sees them.
    """

    recipe_kind: Literal["proposal_mode"] = "proposal_mode"
    line_item_template_id: str
    floor_cpm: float
    capability_overlap: CapabilityOverlap | None = CapabilityOverlap(
        pricing_models=frozenset({"cpm"}),
        delivery_types=frozenset({"non_guaranteed"}),
    )
