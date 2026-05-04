"""GAMDecisioningPlatform — wraps salesagent's `_impl` functions.

The wrap discipline: call salesagent's existing transport-agnostic
`_impl` functions; don't re-implement principal resolution, tenant
config, currency validation, signal lookup, audit logging, workflow
row creation, or webhook scheduling. Those all live below `_impl`
in salesagent's existing stack.

`_impl` seam citations (verified in PR #506 Step 0.2):
- _create_media_buy_impl (media_buy_create.py:1270, async)
- _update_media_buy_impl (media_buy_update.py:117, sync)
- _get_products_impl (products.py:145, async)
- _get_media_buy_delivery_impl (media_buy_delivery.py:67, sync)
- _sync_creatives_impl (creatives/_sync.py:29, sync)
"""

from __future__ import annotations

from typing import Any, ClassVar

from adcp.decisioning.context import RequestContext
from adcp.decisioning.platform import DecisioningPlatform
from adcp.decisioning.specialisms import SalesPlatform
from adcp.decisioning.types import DecisioningCapabilities
from adcp.types import (
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetProductsRequest,
    GetProductsResponse,
    SyncCreativesRequest,
    SyncCreativesResponse,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
)

# Salesagent imports — present in deploy environment
try:
    from src.core.tools.creatives._sync import (  # type: ignore[import-not-found]
        _sync_creatives_impl,
    )
    from src.core.tools.media_buy_create import (  # type: ignore[import-not-found]
        _create_media_buy_impl,
    )
    from src.core.tools.media_buy_delivery import (  # type: ignore[import-not-found]
        _get_media_buy_delivery_impl,
    )
    from src.core.tools.media_buy_update import (  # type: ignore[import-not-found]
        _update_media_buy_impl,
    )
    from src.core.tools.products import (  # type: ignore[import-not-found]
        _get_products_impl,
    )
    from src.services.dynamic_products import (  # type: ignore[import-not-found]
        generate_variants_for_brief,
    )

    SALESAGENT_AVAILABLE = True
except ImportError:
    SALESAGENT_AVAILABLE = False


class GAMPlatform(DecisioningPlatform, SalesPlatform):
    """Wraps salesagent's _impl functions for the side-car experiment.

    Per the #502 revision: framework types the recipe contract via
    `recipe_type`; the adopter (this class) populates ctx.recipes
    from the salesagent Product table.
    """

    upstream_url: ClassVar[str | None] = "https://googleads.googleapis.com/v202405"

    capabilities: ClassVar[DecisioningCapabilities] = DecisioningCapabilities(
        specialisms=["sales-guaranteed", "sales-non-guaranteed"],
    )

    # Recipe shape for typed dispatch — see examples/recipe_falsification/
    # The actual GAMRecipe Pydantic class lives there (Phase 1B harness).
    # In the side-car deployment, it's imported from examples.recipe_falsification.
    # recipe_type: ClassVar[type[BaseModel]] = GAMRecipe

    async def get_products(
        self, req: GetProductsRequest, ctx: RequestContext[Any]
    ) -> GetProductsResponse:
        """Wrap _get_products_impl — handles both static catalog + dynamic variants.

        Per Phase 1A: static products come from the Product table directly;
        signal-driven variants are persistent Product rows generated via
        generate_variants_for_brief (which writes to the DB as a side
        effect of brief processing).
        """
        if not SALESAGENT_AVAILABLE:
            raise RuntimeError("Requires salesagent imports.")

        identity = self._build_resolved_identity(ctx)
        return await _get_products_impl(req, identity)

    async def create_media_buy(
        self, req: CreateMediaBuyRequest, ctx: RequestContext[Any]
    ) -> CreateMediaBuyResponse:
        """Wrap _create_media_buy_impl — preserves HITL gate semantics.

        The HITL `before` hook (see hitl_gate.py) writes the WorkflowStep
        row and short-circuits with status='pending_approval' when
        AdapterConfig.gam_manual_approval_required is set. This wrapper
        runs only when the gate falls through — either no approval
        needed, or _already_approved sentinel set on the request by
        execute_approved_media_buy after admin approval.
        """
        if not SALESAGENT_AVAILABLE:
            raise RuntimeError("Requires salesagent imports.")

        identity = self._build_resolved_identity(ctx)

        # Pull push_notification_config from ctx if buyer registered one.
        # F12 auto-emit will fire on success regardless — this is just
        # the per-request config the impl needs for any interim webhooks.
        push_config = self._extract_push_notification_config(req)

        result = await _create_media_buy_impl(
            req=req,
            push_notification_config=push_config,
            identity=identity,
            context_id=getattr(ctx, "context_id", None),
        )
        # _create_media_buy_impl returns CreateMediaBuyResult (a wrapper
        # with .response and .status); project to wire response.
        return result.response  # type: ignore[union-attr]

    async def update_media_buy(
        self, req: UpdateMediaBuyRequest, ctx: RequestContext[Any]
    ) -> UpdateMediaBuyResponse:
        """Wrap _update_media_buy_impl (sync; SDK wraps in awaitable)."""
        if not SALESAGENT_AVAILABLE:
            raise RuntimeError("Requires salesagent imports.")

        identity = self._build_resolved_identity(ctx)
        result = _update_media_buy_impl(
            req=req,
            identity=identity,
            context_id=getattr(ctx, "context_id", None),
        )
        # Returns Success | Error union — let it bubble to wire response.
        return result  # type: ignore[return-value]

    async def get_media_buy_delivery(
        self, req: GetMediaBuyDeliveryRequest, ctx: RequestContext[Any]
    ) -> GetMediaBuyDeliveryResponse:
        """Wrap _get_media_buy_delivery_impl (sync)."""
        if not SALESAGENT_AVAILABLE:
            raise RuntimeError("Requires salesagent imports.")

        identity = self._build_resolved_identity(ctx)
        return _get_media_buy_delivery_impl(req=req, identity=identity)

    async def sync_creatives(
        self, req: SyncCreativesRequest, ctx: RequestContext[Any]
    ) -> SyncCreativesResponse:
        """Wrap _sync_creatives_impl. The HITL gate maps the GAM-internal
        operation name `add_creative_assets` → AdCP-wire `sync_creatives`
        for approval check (see hitl_gate.py)."""
        if not SALESAGENT_AVAILABLE:
            raise RuntimeError("Requires salesagent imports.")

        identity = self._build_resolved_identity(ctx)
        return _sync_creatives_impl(
            creatives=req.creatives,
            assignments=req.assignments,
            creative_ids=req.creative_ids,
            delete_missing=req.delete_missing or False,
            dry_run=req.dry_run or False,
            validation_mode=req.validation_mode or "strict",
            push_notification_config=self._extract_push_notification_config(req),
            context=getattr(req, "context", None),
            identity=identity,
        )

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    def _build_resolved_identity(self, ctx: RequestContext[Any]) -> Any:
        """Project SDK ctx (BuyerAgent + Account) → salesagent's
        ResolvedIdentity dataclass.

        Salesagent's _impl functions expect a ResolvedIdentity with:
        - principal_id: str
        - tenant: dict[str, Any]
        - testing_context: AdCPTestContext | None
        """
        from src.core.resolved_identity import (  # type: ignore[import-not-found]
            ResolvedIdentity,
        )

        return ResolvedIdentity(
            principal_id=ctx.account.metadata.get("principal_id"),
            tenant={
                "tenant_id": ctx.account.metadata["tenant_id"],
                # Other tenant fields the _impl might consult — pulled
                # from the salesagent tenant config_loader at request time.
            },
            testing_context=None,
        )

    @staticmethod
    def _extract_push_notification_config(req: Any) -> dict[str, Any] | None:
        """Pull push_notification_config dict from request if present."""
        config = getattr(req, "push_notification_config", None)
        if config is None:
            return None
        if hasattr(config, "model_dump"):
            return config.model_dump()
        if isinstance(config, dict):
            return config
        return None
