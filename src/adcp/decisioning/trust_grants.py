"""Opt-in demand-grant gate, after commercial identity and before account side effects."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from adcp.decisioning.registry import BuyerAgent
from adcp.decisioning.types import Account, AdcpError
from adcp.signing.etld import registrable_domain
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


def _is_non_spend(
    operation: str | None,
    params: Mapping[str, Any] | None,
    spend_committing: bool | None,
) -> bool:
    """Whether ``operation`` commits no spend, so it may continue during an outage.

    Uses the transport's spend decision, with conservative direct-call
    defaults: provisioning and unknown operations count as spend.
    """
    if operation is None or operation == "sync_accounts":
        return False
    if spend_committing is not None:
        return not spend_committing
    from adcp.server.mcp_tools import ADCP_TOOL_DEFINITIONS

    if not any(tool["name"] == operation for tool in ADCP_TOOL_DEFINITIONS):
        return False
    if operation in {"update_media_buy", "control_media_buy"} and params is None:
        return False
    from adcp.server.signed_requests import is_wba_spend_committing

    return not is_wba_spend_committing(operation, params)


def _grant_absent(message: str) -> AdcpError:
    return AdcpError(
        "PERMISSION_DENIED",
        message=message,
        details={"scope": "agent", "reason": "grant_absent"},
        recovery="correctable",
    )


def _seller_claim_unavailable() -> AdcpError:
    # PERMISSION_DENIED without details: grant_absent is reserved for a claim
    # that no grant covers.
    return AdcpError(
        "PERMISSION_DENIED",
        message="Account has no resolvable grant claim for this operation",
        recovery="correctable",
    )


def _unestablished() -> AdcpError:
    # No established commercial identity: omit details under the oracle clamp.
    return AdcpError(
        "PERMISSION_DENIED", message="Buyer agent is not authorized", recovery="correctable"
    )


@dataclass(frozen=True)
class TrustGrantGate:
    """Apply demand-grant evaluation before account side effects.

    Natural-key ``sync_accounts`` declarations are checked directly before
    upsert. Existing accounts use ``extract_claim`` (or
    ``extract_operation_claim``), reading seller-owned state, so a caller
    cannot omit the brand to bypass grant checking; an extractor that fails,
    or yields a claim without a brand or operator, fails closed. Requires a
    BuyerAgentRegistry in the composition root.

    The gate keeps no claim history of its own. The trust-record evaluator's
    last record read is authoritative: a withdrawn grant stays withdrawn
    through any later outage, and a non-spend claim continues on the last
    records read only within the evaluator's outage-continuation bound.
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
        non_spend: bool = False,
        wba_revocation: str | None = None,
    ) -> None:
        """Authorize ``claim`` for ``agent``; raise the AdCP error otherwise.

        ``non_spend`` marks a claim for an operation that commits no spend:
        only such a claim continues on the last records read during an
        outage. ``wba_revocation`` is the verified signer's revocation state:
        a stale list fails spend closed and an unavailable store fails every
        claim, with the retryable ``SERVICE_UNAVAILABLE``.
        """
        if agent is None:
            raise _unestablished()
        try:
            operator = registrable_domain(agent.agent_url)
        except ValueError:
            operator = None
        if operator is None:
            raise _unestablished()
        if signing_profile == "web-bot-auth" and (
            wba_revocation == "unavailable" or (wba_revocation == "stale" and not non_spend)
        ):
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message="Origin revocation status is temporarily unavailable",
                recovery="transient",
            )
        result = await self.evaluator.check(
            agent_origin=agent.agent_url,
            brand_domain=claim.brand_domain,
            account_operator=claim.account_operator,
            brand_id=claim.brand_id,
            countries=claim.countries,
            scope=claim.scope,
            signing_profile=signing_profile,
            allow_stale_on_outage=non_spend,
        )
        result.require()

    async def require_account(
        self,
        account: Account[Any],
        agent: BuyerAgent | None,
        *,
        signing_profile: Literal["web-bot-auth", "adcp/request-signing/v1"] = "web-bot-auth",
        operation: str | None = None,
        operation_params: Mapping[str, Any] | None = None,
        spend_committing: bool | None = None,
        wba_revocation: str | None = None,
    ) -> None:
        if agent is None:
            raise _unestablished()
        try:
            claim = (
                self.extract_operation_claim(account, agent, operation)
                if self.extract_operation_claim is not None and operation is not None
                else self.extract_claim(account, agent)
            )
            if inspect.isawaitable(claim):
                claim = await claim
        except AdcpError:
            raise
        except Exception as exc:
            # Missing seller-owned brand/operator state never authorizes. It is
            # a seller-side failure, not "no grant covers the claim".
            raise _seller_claim_unavailable() from exc
        if not isinstance(claim, TrustGrantClaim):
            raise _seller_claim_unavailable()
        if (
            operation is not None
            and operation not in _MEDIA_BUY_DEFAULT_OPERATIONS
            and claim.scope == "adcp:media_buying"
        ):
            # The seller did not choose a scope for this operation.
            raise _seller_claim_unavailable()
        await self.require_claim(
            claim,
            agent,
            signing_profile=signing_profile,
            non_spend=_is_non_spend(operation, operation_params, spend_committing),
            wba_revocation=wba_revocation,
        )

    async def require_declaration(
        self,
        declaration: Mapping[str, Any],
        agent: BuyerAgent | None,
        *,
        signing_profile: Literal["web-bot-auth", "adcp/request-signing/v1"] = "web-bot-auth",
        wba_revocation: str | None = None,
    ) -> None:
        if agent is None:
            raise _unestablished()
        brand, operator = declaration.get("brand"), declaration.get("operator")
        if (
            not isinstance(brand, Mapping)
            or not isinstance(brand.get("domain"), str)
            or not isinstance(operator, str)
        ):
            raise _grant_absent("Account declaration has no grant authority")
        # Provisioning always requires fresh authority.
        await self.require_claim(
            TrustGrantClaim(
                brand["domain"],
                operator,
                brand.get("brand_id"),
                tuple(brand.get("countries") or ()),
            ),
            agent,
            signing_profile=signing_profile,
            wba_revocation=wba_revocation,
        )


__all__ = [
    "GrantClaimResolver",
    "OperationGrantClaimResolver",
    "TrustGrantClaim",
    "TrustGrantGate",
]
