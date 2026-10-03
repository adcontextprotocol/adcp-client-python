"""Boot the sales-proposal-mode mock seller.

Wires:

* :class:`ProposalModeProposalManager` declaring ``finalize=True``.
* :class:`ProposalModeDecisioningPlatform` reading ``ctx.recipes``.
* In-memory proposal store via :func:`create_dev_proposal_store` —
  emits a ``UserWarning`` so the dev-mode posture is visible at boot.
* :class:`PlatformRouter` over both with cross-store consistency check.

This is the storyboard adopter — the proof that the design works
end-to-end. Run::

    python -m examples.sales_proposal_mode_seller.src.app

Then exercise via::

    adcp storyboard run http://127.0.0.1:3003/mcp media_buy_seller \\
        --json --allow-http
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    PlatformRouter,
    create_dev_proposal_store,
    serve,
)
from adcp.decisioning.accounts import AccountStore
from adcp.decisioning.capabilities import Account as CapabilitiesAccount
from adcp.decisioning.capabilities import (
    Adcp,
    IdempotencySupported,
    MediaBuy,
    SupportedProtocol,
)
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.types import Account
from adcp.server import IdempotencyStore, MemoryBackend, ToolContext
from examples.sales_proposal_mode_seller.src.platform import (
    ProposalModeDecisioningPlatform,
)
from examples.sales_proposal_mode_seller.src.proposal_manager import (
    ProposalModeProposalManager,
)

PORT = int(os.environ.get("ADCP_PORT") or os.environ.get("PORT") or 3003)


class ProposalModeRouter(PlatformRouter):
    """Replay complete dispatch results, including intercepted finalization."""

    # Idempotency is wired at the transport dispatch boundary below, so it
    # covers the framework's finalize interception as well as platform calls.
    _adcp_idempotency_external = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._idempotency: dict[str, IdempotencyStore] = {}

    async def idempotency_middleware(
        self,
        skill_name: str,
        params: dict[str, Any],
        context: ToolContext,
        call_next: Callable[[], Awaitable[Any]],
    ) -> Any:
        # This unauthenticated, single-tenant demo has one public principal.
        # A production deployment must supply its authenticated principal;
        # retain that identity whenever transport authentication provides it.
        scope = replace(
            context,
            caller_identity=context.caller_identity or "proposal-mode-demo",
            tenant_id=context.tenant_id or "default",
        )

        async def dispatch(params: Any, context: Any = None) -> Any:
            del params, context
            return await call_next()

        dispatch.__name__ = skill_name
        store = self._idempotency.setdefault(
            skill_name, IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)
        )
        return await store.wrap(dispatch)(params=params, context=scope)


class _SingleTenantAccounts:
    """Minimal :class:`AccountStore` + :class:`AccountStoreUpsert` — every
    request resolves to the single ``default`` tenant. ``sync_accounts``
    is implemented (the storyboard runner needs it for stateful chain
    bootstrapping)."""

    resolution = "explicit"

    def resolve(
        self,
        ref: dict[str, Any] | None = None,
        auth_info: AuthInfo | None = None,
    ) -> Account[dict[str, Any]]:
        del auth_info
        ref = ref or {}
        operator = (ref or {}).get("operator") if isinstance(ref, dict) else None
        account_id = (ref or {}).get("account_id") if isinstance(ref, dict) else None
        resolved_id = str(account_id or f"acct_{operator or 'demo'}".replace(".", "_"))
        return Account(
            id=resolved_id,
            metadata={"tenant_id": "default"},
        )

    def upsert(
        self,
        refs: list[Any],
        ctx: Any = None,
    ) -> list[dict[str, Any]]:
        """``sync_accounts`` API. Storyboards call this first to seed
        the stateful account chain. Returns one result row per ref in
        the wire shape per ``schemas/cache/account/sync-accounts-response.json``
        — the framework wraps the list as ``{"accounts": [...]}``."""
        del ctx
        rows: list[dict[str, Any]] = []
        for ref in refs:
            if hasattr(ref, "model_dump"):
                ref_dict = ref.model_dump(mode="json", exclude_none=True)
            else:
                ref_dict = dict(ref) if isinstance(ref, dict) else {}
            operator = ref_dict.get("operator", "demo")
            brand = ref_dict.get("brand") or {}
            domain = (
                brand.get("domain") if isinstance(brand, dict) else getattr(brand, "domain", None)
            )
            account_id = f"acct_{operator}".replace(".", "_")
            rows.append(
                {
                    "account_id": account_id,
                    "name": f"Account for {domain or operator}",
                    "brand": {"domain": domain or "demo.example"},
                    "operator": operator,
                    "action": "created",
                    "status": "active",
                    "billing": "operator",
                }
            )
        return rows


def build_router() -> ProposalModeRouter:
    """Construct the v1.5 router with finalize-capable wiring."""
    accounts: AccountStore[Any] = _SingleTenantAccounts()  # type: ignore[assignment]
    return ProposalModeRouter(
        accounts=accounts,
        platforms={"default": ProposalModeDecisioningPlatform()},
        proposal_managers={"default": ProposalModeProposalManager()},
        # ``create_dev_proposal_store`` emits a ``UserWarning`` at boot.
        # Production deployments wire a durable backing instead.
        proposal_stores={"default": create_dev_proposal_store()},
        capabilities=DecisioningCapabilities(
            specialisms=["sales-non-guaranteed", "sales-proposal-mode"],
            adcp=Adcp(
                major_versions=[3],
                idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
            ),
            account=CapabilitiesAccount(supported_billing=["operator"]),
            media_buy=MediaBuy(
                supported_pricing_models=["cpm"],
                supports_proposals=True,
                buying_modes=["brief", "refine"],
            ),
            supported_protocols=[SupportedProtocol.media_buy],
        ),
    )


if __name__ == "__main__":
    router = build_router()
    serve(
        router,
        name="sales-proposal-mode-seller",
        port=PORT,
        auto_emit_completion_webhooks=False,
        middleware=[router.idempotency_middleware],
    )
