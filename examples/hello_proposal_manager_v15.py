"""Hello-proposal-manager v1.5 — proposal-mode mock seller demo.

The v1.5 LOC budget validator. Demonstrates the full proposal lifecycle
behind a minimal-LOC adopter implementation:

* Concrete :class:`Recipe` subclass with typed
  :class:`CapabilityOverlap` declaration (D4).
* :class:`ProposalManager` with :meth:`get_products` /
  :meth:`refine_products` / :meth:`finalize_proposal` —
  ``finalize=True`` capability declared (D2).
* :class:`DecisioningPlatform` whose ``create_media_buy`` reads
  ``ctx.recipes[product_id]`` (D3 single-ledger hydration).
* :class:`PlatformRouter` wiring with ``proposal_stores=`` for the
  finalize-capable manager (D5 cross-store consistency).

End-state: an adopter writing a proposal-mode mock seller that passes
``proposal_finalize.yaml`` end-to-end through the SDK in this many
LOC. See ``docs/proposals/proposal-manager-v15-design.md`` §
"Storyboard pass criteria + adopter LOC budget" for the design's
target (~350 LOC).

Wire-level back-compat: this example does NOT change any wire shapes;
v1.5 makes the framework path that handles existing wire shapes
(``buying_mode='refine'``, ``refine[i].action='finalize'``,
``proposals[]``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from adcp.decisioning import (
    AdcpError,
    CapabilityOverlap,
    DecisioningCapabilities,
    DecisioningPlatform,
    FinalizeProposalRequest,
    FinalizeProposalSuccess,
    InMemoryProposalStore,
    PlatformRouter,
    ProposalCapabilities,
    Recipe,
    RequestContext,
    SalesPlatform,
    serve,
)
from adcp.decisioning.accounts import Account, AuthInfo
from adcp.decisioning.capabilities import (
    Account as CapabilitiesAccount,
)
from adcp.decisioning.capabilities import (
    Adcp,
    IdempotencySupported,
    MediaBuy,
)


# --------------------------------------------------------------------------
# Recipe — the typed contract between ProposalManager and DecisioningPlatform
# --------------------------------------------------------------------------


class HelloRecipe(Recipe):
    """Per-product implementation_config carried alongside each Product.

    Adopter pattern: declare :attr:`recipe_kind` as a Literal; add typed
    fields the adapter reads at execute time; declare
    :attr:`capability_overlap` to gate buyer requests pre-adapter.
    """

    recipe_kind: Literal["hello"] = "hello"
    line_item_template_id: str
    creative_placeholder_id: str
    # Lock pricing and delivery to the seller's product shape so the
    # framework rejects buyer requests outside this overlap before
    # the adapter sees them.
    capability_overlap: CapabilityOverlap | None = CapabilityOverlap(
        pricing_models=frozenset({"cpm"}),
        delivery_types=frozenset({"non_guaranteed"}),
    )


# --------------------------------------------------------------------------
# ProposalManager — the proposal-side surface
# --------------------------------------------------------------------------


_DRAFT_RECIPE = HelloRecipe(
    line_item_template_id="lit_demo_42",
    creative_placeholder_id="cph_demo_1",
)

# A canonical product the manager always returns. Mirrors the shape
# the proposal_finalize.yaml storyboard expects.
_BASE_PRODUCT_PAYLOAD: dict[str, Any] = {
    "product_id": "hello-cpm-default",
    "name": "Hello v1.5 demo product",
    "description": "Demo product wired with a Recipe + CapabilityOverlap.",
    "delivery_type": "non_guaranteed",
    "publisher_properties": [
        {"publisher_domain": "example.com", "selection_type": "all"},
    ],
    "format_ids": [
        {
            "agent_url": "https://creative.adcontextprotocol.org/",
            "id": "display_300x250",
        },
    ],
    "pricing_options": [
        {
            "pricing_option_id": "po-cpm-default",
            "pricing_model": "cpm",
            "floor_price": 5.0,
            "currency": "USD",
        },
    ],
    "reporting_capabilities": {
        "available_metrics": ["impressions"],
        "available_reporting_frequencies": ["daily"],
        "date_range_support": "date_range",
        "supports_webhooks": False,
        "expected_delay_minutes": 60,
        "timezone": "UTC",
    },
    "delivery_measurement": {"provider": "internal"},
    "implementation_config": _DRAFT_RECIPE.model_dump(),
}


def _draft_proposal_payload() -> dict[str, Any]:
    """The wire ``Proposal`` shape the manager returns alongside products."""
    return {
        "proposal_id": "p_demo_1",
        "proposal_status": "draft",
        "products": [_BASE_PRODUCT_PAYLOAD["product_id"]],
        "currency": "USD",
        "package_count": 1,
    }


class HelloProposalManager:
    """The v1.5 proposal-side surface.

    Three methods cover the full lifecycle:

    * :meth:`get_products` — initial brief / refine product discovery.
    * :meth:`refine_products` — iterative refine without finalize.
    * :meth:`finalize_proposal` — lock pricing + ``expires_at``.
    """

    capabilities = ProposalCapabilities(
        sales_specialism="sales-non-guaranteed",
        refine=True,
        finalize=True,
        # 60s grace window absorbs clock skew between the seller's
        # ``expires_at`` and the buyer's create_media_buy call.
        expires_at_grace_seconds=60,
    )

    async def get_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Initial product discovery — returns a single demo product
        carrying a HelloRecipe in ``implementation_config``."""
        del req, ctx
        return {
            "products": [_BASE_PRODUCT_PAYLOAD],
            "proposals": [_draft_proposal_payload()],
        }

    async def refine_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Refine iteration — same products + draft proposal. Adopter
        typically inspects ``req.refine`` and projects per-entry
        outcomes; this demo keeps the catalog stable so the storyboard
        can iterate."""
        del req, ctx
        return {
            "products": [_BASE_PRODUCT_PAYLOAD],
            "proposals": [_draft_proposal_payload()],
        }

    async def finalize_proposal(
        self,
        req: FinalizeProposalRequest,
        ctx: RequestContext[Any],
    ) -> FinalizeProposalSuccess:
        """Inline-commit finalize. Locks pricing on the draft proposal
        and emits a 24h ``expires_at`` hold window.

        Adopters whose finalize takes longer (HITL approval, external
        workflow) return ``ctx.handoff_to_task(self._async_finalize)``
        instead — the framework projects ``Submitted`` and the handoff
        completes via the standard TaskRegistry flow."""
        del ctx
        committed = dict(req.proposal_payload)
        committed["proposal_status"] = "committed"
        return FinalizeProposalSuccess(
            proposal=committed,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )


# --------------------------------------------------------------------------
# DecisioningPlatform — reads ctx.recipes; never writes
# --------------------------------------------------------------------------


class HelloDecisioningPlatform(DecisioningPlatform, SalesPlatform):
    """Execution-side platform. Reads :attr:`RequestContext.recipes`
    populated by the framework from the :class:`InMemoryProposalStore`
    on every post-acceptance dispatch path."""

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        adcp=Adcp(
            major_versions=[3],
            idempotency=IdempotencySupported(
                supported=True,
                replay_ttl_seconds=86400,
            ),
        ),
        account=CapabilitiesAccount(supported_billing=["operator"]),
        media_buy=MediaBuy(supported_pricing_models=["cpm"]),
    )
    accounts = None  # type: ignore[assignment]

    def get_products(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        """Fall-through for tenants without a ProposalManager — not used
        in this demo (HelloProposalManager handles get_products)."""
        del req, ctx
        return {"products": [_BASE_PRODUCT_PAYLOAD]}

    def create_media_buy(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Adopter pattern: read ``ctx.recipes[product_id]`` to get the
        typed recipe the framework hydrated from the ProposalStore.

        ``ctx.recipes`` is populated by the framework when the buyer
        called ``create_media_buy(proposal_id=...)`` — the framework
        looked up the proposal, validated expiry / capability overlap,
        and threaded the recipes into ``ctx``. The adapter doesn't
        rummage through the request envelope.
        """
        packages = getattr(req, "packages", None) or []
        # Pattern: typed downcast on the recipe to access adapter-private
        # fields without losing static-type safety.
        if packages and ctx.recipes:
            first_pkg = packages[0]
            product_id = (
                getattr(first_pkg, "product_id", None) or _BASE_PRODUCT_PAYLOAD["product_id"]
            )
            recipe = ctx.recipes.get(str(product_id))
            if recipe is not None and not isinstance(recipe, HelloRecipe):
                # Defensive — if we got a recipe of an unexpected kind,
                # surface as INTERNAL_ERROR rather than mistranslate.
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Expected HelloRecipe; got {type(recipe).__name__}. "
                        "Recipe-kind routing drift."
                    ),
                    recovery="terminal",
                )
        idem_key = getattr(req, "idempotency_key", "unknown")
        return {
            "media_buy_id": f"mb_demo_{idem_key}",
            "status": "active",
            "packages": [
                {
                    "package_id": f"pkg_demo_{i}",
                    "buyer_ref": getattr(p, "buyer_ref", None),
                    "product_id": getattr(p, "product_id", None),
                    "status": "active",
                }
                for i, p in enumerate(packages)
            ],
        }

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Same recipe-hydration pattern: ``ctx.recipes`` is hydrated
        from :meth:`ProposalStore.get_by_media_buy_id` (the reverse-
        index path) before this method runs."""
        del patch, ctx
        return {"media_buy_id": media_buy_id, "status": "active", "packages": []}

    def sync_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        del ctx
        creatives = getattr(req, "creatives", None) or []
        return {
            "creatives": [
                {
                    "creative_id": (
                        c.creative_id if hasattr(c, "creative_id") else c.get("creative_id")
                    ),
                    "approval_status": "approved",
                }
                for c in creatives
            ],
        }

    def get_media_buy_delivery(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        del ctx
        return {
            "media_buy_deliveries": [
                {
                    "media_buy_id": getattr(req, "media_buy_id", "mb_unknown"),
                    "totals": {"impressions": 0, "spend": 0.0},
                },
            ],
        }


# --------------------------------------------------------------------------
# AccountStore — single-tenant resolver
# --------------------------------------------------------------------------


class _SingleTenantAccounts:
    """Trivial AccountStore — every request resolves to the ``default``
    tenant. Adopters with multi-tenant deployments back this with a
    database lookup or read
    :func:`adcp.server.tenant_router.current_tenant`."""

    resolution = "explicit"

    def resolve(
        self,
        ref: dict[str, Any] | None = None,
        auth_info: AuthInfo | None = None,
    ) -> Account[Any]:
        ref = ref or {}
        return Account(
            id=str(ref.get("account_id", "acct_demo")),
            name="demo account",
            status="active",
            metadata={"tenant_id": "default"},
        )


# --------------------------------------------------------------------------
# Wiring — the LOC budget's load-bearing claim
# --------------------------------------------------------------------------


def build_router() -> PlatformRouter:
    """Construct the v1.5 router with finalize-capable wiring.

    Adopters call this from their own ``serve()`` entry point. The
    cross-store consistency check (D5) ensures finalize=True
    ProposalManagers always have a wired ProposalStore for the same
    tenant — adopters who forget see a hard error at construction
    rather than a silent prod foot-gun.
    """
    return PlatformRouter(
        accounts=_SingleTenantAccounts(),
        platforms={"default": HelloDecisioningPlatform()},
        proposal_managers={"default": HelloProposalManager()},
        proposal_stores={"default": InMemoryProposalStore()},
        capabilities=DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            adcp=Adcp(
                major_versions=[3],
                idempotency=IdempotencySupported(
                    supported=True,
                    replay_ttl_seconds=86400,
                ),
            ),
            account=CapabilitiesAccount(supported_billing=["operator"]),
            media_buy=MediaBuy(supported_pricing_models=["cpm"]),
        ),
    )


if __name__ == "__main__":
    serve(
        build_router(),
        name="hello-proposal-manager-v15",
        port=3002,
        auto_emit_completion_webhooks=False,
    )
