"""Opt-in demand-grant gate, after commercial identity and before account side effects."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from adcp.decisioning.registry import BuyerAgent
from adcp.decisioning.types import Account, AdcpError
from adcp.signing.trust import TrustGrantEvaluator


@dataclass(frozen=True)
class TrustGrantClaim:
    brand_domain: str
    account_operator: str
    brand_id: str | None = None
    countries: tuple[str, ...] = ()
    scope: str = "adcp:media_buying"


GrantClaimResolver = Callable[
    [Account[Any], BuyerAgent], TrustGrantClaim | Awaitable[TrustGrantClaim]
]
OperationGrantClaimResolver = Callable[
    [Account[Any], BuyerAgent, str], TrustGrantClaim | Awaitable[TrustGrantClaim]
]

# R6 assigns media buying to buys and their account provisioning. Other
# domains have no universal operation-to-scope mapping: sellers choose the
# required scope explicitly rather than inheriting media-buying authority.
_MEDIA_BUY_DEFAULT_OPERATIONS = frozenset(
    {
        "sync_accounts",
        "get_products",
        "list_products",
        "request_proposals",
        "refine_proposals",
        "decline_proposals",
        "buy_products",
        "accept_proposal",
        "create_media_buy",
        "update_media_buy",
        "control_media_buy",
        "get_media_buys",
        "get_media_buy_delivery",
        "get_reporting_status",
        "sync_reporting_receipts",
        "provide_performance_feedback",
        "sync_creatives",
        "list_creatives",
        "list_creative_formats",
    }
)


@dataclass(frozen=True)
class TrustGrantGate:
    """Extract existing-account authority from seller-owned state.

    Natural-key sync_accounts declarations are checked directly before upsert.
    Existing accounts use extract_claim, so a caller cannot omit the brand to
    bypass grant checking. Requires a BuyerAgentRegistry in the composition root.
    """

    evaluator: TrustGrantEvaluator
    extract_claim: GrantClaimResolver
    extract_operation_claim: OperationGrantClaimResolver | None = None

    async def require_claim(
        self,
        claim: TrustGrantClaim,
        agent: BuyerAgent | None,
        *,
        signing_profile: Literal["web-bot-auth", "adcp/request-signing/v1"] = "web-bot-auth",
    ) -> None:
        if agent is None:
            # No established commercial identity: omit details under the oracle clamp.
            raise AdcpError(
                "PERMISSION_DENIED", message="Buyer agent is not authorized", recovery="correctable"
            )
        result = await self.evaluator.check(
            agent_origin=agent.agent_url,
            brand_domain=claim.brand_domain,
            account_operator=claim.account_operator,
            brand_id=claim.brand_id,
            countries=claim.countries,
            scope=claim.scope,
            signing_profile=signing_profile,
        )
        result.require()

    async def require_account(
        self,
        account: Account[Any],
        agent: BuyerAgent | None,
        *,
        signing_profile: Literal["web-bot-auth", "adcp/request-signing/v1"] = "web-bot-auth",
        operation: str | None = None,
    ) -> None:
        if agent is None:
            await self.require_claim(TrustGrantClaim("", ""), None)
            return
        claim = (
            self.extract_operation_claim(account, agent, operation)
            if self.extract_operation_claim is not None and operation is not None
            else self.extract_claim(account, agent)
        )
        if inspect.isawaitable(claim):
            claim = await claim
        if (
            operation is not None
            and operation not in _MEDIA_BUY_DEFAULT_OPERATIONS
            and claim.scope == "adcp:media_buying"
        ):
            raise AdcpError(
                "PERMISSION_DENIED",
                message="Operation requires an explicit grant scope",
                details={"scope": "agent", "reason": "grant_absent"},
                recovery="correctable",
            )
        await self.require_claim(claim, agent, signing_profile=signing_profile)

    async def require_declaration(
        self,
        declaration: Mapping[str, Any],
        agent: BuyerAgent | None,
        *,
        signing_profile: Literal["web-bot-auth", "adcp/request-signing/v1"] = "web-bot-auth",
    ) -> None:
        brand, operator = declaration.get("brand"), declaration.get("operator")
        if (
            not isinstance(brand, Mapping)
            or not isinstance(brand.get("domain"), str)
            or not isinstance(operator, str)
        ):
            if agent is None:
                await self.require_claim(TrustGrantClaim("", ""), None)
            raise AdcpError(
                "PERMISSION_DENIED",
                message="Account declaration has no grant authority",
                details={"scope": "agent", "reason": "grant_absent"},
                recovery="correctable",
            )
        await self.require_claim(
            TrustGrantClaim(
                brand["domain"],
                operator,
                brand.get("brand_id"),
                tuple(brand.get("countries") or ()),
            ),
            agent,
            signing_profile=signing_profile,
        )


__all__ = [
    "GrantClaimResolver",
    "OperationGrantClaimResolver",
    "TrustGrantClaim",
    "TrustGrantGate",
]
