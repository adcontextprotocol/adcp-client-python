"""Recipe — discriminated-union base for typed product implementation_config.

The recipe is the contract between :class:`ProposalManager` and
:class:`DecisioningPlatform` (see
``docs/proposals/product-architecture.md`` § "How recipes are shared
between the two platforms"). A ProposalManager assembles products and
attaches a recipe to each; a DecisioningPlatform later consumes the
recipe at ``create_media_buy`` time to translate to its upstream API.

**The recipe is never on the wire.** It rides inside
``Product.implementation_config`` (an opaque-to-buyer dict). Buyers
treat it as a black box; the framework persists it through the proposal
lifecycle so the executing DecisioningPlatform sees a stable view.

v1 carries one declared field — :attr:`recipe_kind`. Adopters subclass
this base and add their own typed fields:

.. code-block:: python

    from typing import Literal
    from adcp.decisioning import CapabilityOverlap, Recipe

    class GAMRecipe(Recipe):
        recipe_kind: Literal["gam"] = "gam"
        line_item_template_id: str
        ad_unit_ids: list[str]
        capability_overlap: CapabilityOverlap | None = CapabilityOverlap(
            pricing_models=frozenset({"cpm"}),
            delivery_types=frozenset({"guaranteed"}),
        )

    class KevelRecipe(Recipe):
        recipe_kind: Literal["kevel"] = "kevel"
        flight_id: str
        zone_ids: list[str]

The kind tag enables router-by-recipe-kind dispatch in the
multi-decisioning case (one ProposalManager + many DecisioningPlatforms,
each handling a subset of recipe kinds). v1 doesn't yet wire that
routing — adopters using a single DecisioningPlatform attach recipes
freely without registry validation.

**v1.5 adds :attr:`capability_overlap`** (per
``docs/proposals/proposal-manager-v15-design.md`` § D4): a typed
declaration of which wire capabilities the buyer can configure on this
product. The framework validates buyer requests against the overlap
before adapter code runs (see
:mod:`adcp.decisioning.proposal_lifecycle`).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class CapabilityOverlap:
    """Per-product subset of wire capability flags.

    Buyer requests asking for capabilities outside this overlap are
    rejected by the framework before the adapter sees them (see
    :func:`adcp.decisioning.proposal_lifecycle.validate_capability_overlap`).

    Each field is ``None | frozenset[str]``:

    * ``None`` → framework does not gate this axis (legacy / open).
    * ``frozenset`` → buyer choices must be subsets of this set.
      ``frozenset()`` (empty) means deny-all on this axis.

    The None vs empty-set distinction matches Python set intuition:
    "no constraint" is None; "allowed set is empty" is ``frozenset()``.

    **Why no extras dict?** v1.5 deliberately omits an
    ``extras: dict[str, ...]`` escape hatch (per § D4). Adopters with
    novel gating needs subclass :class:`CapabilityOverlap` and add
    typed fields:

    .. code-block:: python

        @dataclass(frozen=True)
        class GAMCapabilityOverlap(CapabilityOverlap):
            line_item_priorities: frozenset[int] | None = None
            forecast_modes: frozenset[str] | None = None

    The subclass leaves a paper trail; a dict bag does not. If the new
    axis turns out to be widely useful, it lands as a typed field on
    :class:`CapabilityOverlap` upstream rather than as an
    undocumented key in a shared dict.

    :param pricing_models: Subset of wire ``pricing_models`` the buyer
        can choose. Validated against the matching
        :attr:`PricingOption.pricing_model` on the buyer's package.
    :param targeting_dimensions: Subset of wire targeting dimensions
        (``geo``, ``device_type``, ``language``, etc.). Validated
        against the keys present on the buyer's ``targeting_overlay``.
    :param delivery_types: Subset of ``{guaranteed, non_guaranteed}``
        the product offers.
    :param signal_types: If the seller integrates signals, which
        signal types this product accepts. ``frozenset()`` means the
        seller explicitly refuses all signals on this product;
        ``None`` means no framework gate.
    """

    pricing_models: frozenset[str] | None = None
    targeting_dimensions: frozenset[str] | None = None
    delivery_types: frozenset[str] | None = None
    signal_types: frozenset[str] | None = None


class Recipe(BaseModel):
    """Base type for typed product implementation_config payloads.

    Subclasses declare a ``recipe_kind: Literal["<slug>"]`` field that
    identifies the adapter family (``"gam"``, ``"kevel"``, ``"meta"``,
    etc.). The base provides only the discriminator slot;
    adopters add the typed fields their adapter consumes at execute
    time, plus an optional :attr:`capability_overlap` for
    framework-gated buyer-request validation (per v1.5 § D4).

    Adopter subclasses are pure Pydantic — round-trip via
    ``model_dump()`` to land in ``Product.implementation_config`` on
    the wire response, and ``model_validate(d)`` to rehydrate when
    an adopter receives the dict back at ``create_media_buy`` time.

    The base intentionally does NOT declare ``recipe_kind`` itself;
    each subclass MUST declare it as a ``Literal["..."]`` so static
    type checkers narrow correctly when the adopter pattern-matches
    on the kind tag.

    :param capability_overlap: Optional typed declaration of which
        wire capabilities the buyer can configure on this product.
        ``None`` (default) means no framework gating — the v1
        behaviour. An explicit :class:`CapabilityOverlap` activates
        the v1.5 validation seam.
    """

    model_config = ConfigDict(
        # Allow subclasses to add fields without re-declaring config.
        # Strict on extras at the recipe level — adopters who add
        # ad-hoc fields should declare them on their subclass.
        # Allow ``frozenset`` / ``CapabilityOverlap`` to round-trip via
        # ``arbitrary_types_allowed``: Pydantic's stock JSON schema
        # generation doesn't have a frozenset codec but adopters use
        # ``model_dump(mode='python')`` to keep the typed shape, and
        # the field is opaque to the wire (rides on
        # ``implementation_config``).
        extra="forbid",
        frozen=False,
        arbitrary_types_allowed=True,
    )

    capability_overlap: CapabilityOverlap | None = None


__all__ = ["CapabilityOverlap", "Recipe"]
