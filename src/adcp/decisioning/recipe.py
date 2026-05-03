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
    from adcp.decisioning import Recipe

    class GAMRecipe(Recipe):
        recipe_kind: Literal["gam"] = "gam"
        line_item_template_id: str
        ad_unit_ids: list[str]
        key_value_targeting: dict[str, list[str]]

    class KevelRecipe(Recipe):
        recipe_kind: Literal["kevel"] = "kevel"
        flight_id: str
        zone_ids: list[str]

The kind tag enables router-by-recipe-kind dispatch in the
multi-decisioning case (one ProposalManager + many DecisioningPlatforms,
each handling a subset of recipe kinds). v1 doesn't yet wire that
routing — adopters using a single DecisioningPlatform attach recipes
freely without registry validation.

**Out of scope for v1** (deferred to subsequent PRs):

* ``capability_overlap`` declaration on the recipe (Layer 3 seam)
* Framework-side validation of buyer requests against
  capability_overlap before adapter code runs
* Recipe persistence through the buy lifecycle (hydration in
  ``create_media_buy`` / ``update_media_buy`` / ``get_delivery``)
* ``recipe_type: ClassVar`` on ``DecisioningPlatform`` for typed
  hydration

In v1, ``Product.implementation_config`` remains a plain
``dict[str, Any]`` from the framework's perspective. Adopters who want
typed recipes use ``MyRecipe(...).model_dump()`` to serialize and
``MyRecipe.model_validate(d)`` to deserialize on their own — no
framework participation yet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Recipe(BaseModel):
    """Base type for typed product implementation_config payloads.

    Subclasses declare a ``recipe_kind: Literal["<slug>"]`` field that
    identifies the adapter family (``"gam"``, ``"kevel"``, ``"meta"``,
    etc.). The base provides only the discriminator slot; adopters
    add the typed fields their adapter consumes at execute time.

    Adopter subclasses are pure Pydantic — round-trip via
    ``model_dump()`` to land in ``Product.implementation_config`` on
    the wire response, and ``model_validate(d)`` to rehydrate when
    an adopter receives the dict back at ``create_media_buy`` time.

    The base intentionally does NOT declare ``recipe_kind`` itself;
    each subclass MUST declare it as a ``Literal["..."]`` so static
    type checkers narrow correctly when the adopter pattern-matches
    on the kind tag.
    """

    model_config = ConfigDict(
        # Allow subclasses to add fields without re-declaring config.
        # Strict on extras at the recipe level — adopters who add
        # ad-hoc fields should declare them on their subclass.
        extra="forbid",
        frozen=False,
    )


__all__ = ["Recipe"]
