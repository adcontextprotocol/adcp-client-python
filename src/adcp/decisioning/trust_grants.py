"""Opt-in demand-grant gate, after commercial identity and before account side effects."""

from __future__ import annotations

import inspect
import threading
from _thread import LockType
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from adcp.decisioning.registry import BuyerAgent
from adcp.decisioning.types import Account, AdcpError
from adcp.signing.canonical import canonical_origin
from adcp.signing.etld import registrable_domain
from adcp.signing.trust import GrantAuthorization, TrustGrantEvaluator


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

_ClaimKey = tuple[str, str | None, str | None, str, str | None, frozenset[str]]


@dataclass
class _DenialHistory:
    denials: dict[_ClaimKey, tuple[int, GrantAuthorization]] = field(default_factory=dict)
    positives: OrderedDict[_ClaimKey, GrantAuthorization] = field(default_factory=OrderedDict)
    claims: dict[_ClaimKey, int] = field(default_factory=dict)
    operator_counts: dict[str, int] = field(default_factory=dict)
    revision: int = 0
    lock: LockType = field(default_factory=threading.Lock)


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


def _cached_binding_may_continue(
    operation: str | None,
    params: Mapping[str, Any] | None,
    spend_committing: bool | None,
) -> bool:
    """Use the transport's spend decision, with conservative direct-call defaults."""
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
    max_denials: int = 4096
    max_claims_per_operator: int = 128
    _history: _DenialHistory = field(
        default_factory=_DenialHistory, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (
            type(self.max_denials) is not int
            or self.max_denials <= 0
            or type(self.max_claims_per_operator) is not int
            or self.max_claims_per_operator <= 0
        ):
            raise ValueError("claim admission limits must be positive integers")

    async def require_claim(
        self,
        claim: TrustGrantClaim,
        agent: BuyerAgent | None,
        *,
        signing_profile: Literal["web-bot-auth", "adcp/request-signing/v1"] = "web-bot-auth",
        allow_cached_binding_on_outage: bool = False,
    ) -> None:
        if agent is None:
            await self._require_admitted_claim(
                claim,
                agent,
                signing_profile=signing_profile,
                allow_cached_binding_on_outage=allow_cached_binding_on_outage,
            )
            return
        operator = registrable_domain(agent.agent_url)
        if operator is None:
            raise AdcpError("PERMISSION_DENIED", message="Buyer agent is not authorized")
        key: _ClaimKey = (
            canonical_origin(agent.agent_url),
            registrable_domain(claim.brand_domain),
            registrable_domain(claim.account_operator),
            claim.scope,
            claim.brand_id,
            frozenset(claim.countries),
        )
        with self._history.lock:
            if key not in self._history.claims:
                if (
                    len(self._history.claims) >= self.max_denials
                    or self._history.operator_counts.get(operator, 0)
                    >= self.max_claims_per_operator
                ):
                    raise AdcpError(
                        "SERVICE_UNAVAILABLE",
                        message="Claim history admission capacity exceeded",
                    )
                self._history.claims[key] = 0
                self._history.operator_counts[operator] = (
                    self._history.operator_counts.get(operator, 0) + 1
                )
            self._history.claims[key] += 1
        try:
            await self._require_admitted_claim(
                claim,
                agent,
                signing_profile=signing_profile,
                allow_cached_binding_on_outage=allow_cached_binding_on_outage,
            )
        finally:
            with self._history.lock:
                self._history.claims[key] -= 1
                if (
                    self._history.claims[key] == 0
                    and key not in self._history.positives
                    and key not in self._history.denials
                ):
                    del self._history.claims[key]
                    self._history.operator_counts[operator] -= 1
                    if not self._history.operator_counts[operator]:
                        del self._history.operator_counts[operator]

    async def _require_admitted_claim(
        self,
        claim: TrustGrantClaim,
        agent: BuyerAgent | None,
        *,
        signing_profile: Literal["web-bot-auth", "adcp/request-signing/v1"] = "web-bot-auth",
        allow_cached_binding_on_outage: bool = False,
    ) -> None:
        if agent is None:
            # No established commercial identity: omit details under the oracle clamp.
            raise AdcpError("PERMISSION_DENIED", message="Buyer agent is not authorized")
        key: _ClaimKey = (
            canonical_origin(agent.agent_url),
            registrable_domain(claim.brand_domain),
            registrable_domain(claim.account_operator),
            claim.scope,
            claim.brand_id,
            frozenset(claim.countries),
        )
        with self._history.lock:
            started_revision = self._history.revision
        result = await self.evaluator.check(
            agent_origin=agent.agent_url,
            brand_domain=claim.brand_domain,
            account_operator=claim.account_operator,
            brand_id=claim.brand_id,
            countries=claim.countries,
            scope=claim.scope,
            signing_profile=signing_profile,
        )
        continuity_deadline: float | None = None
        if result.error_code == "SERVICE_UNAVAILABLE" and allow_cached_binding_on_outage:
            with self._history.lock:
                positive = self._history.positives.get(key)
                if (
                    key not in self._history.denials
                    and positive is not None
                    and positive.outage_expires_at is not None
                    and positive.outage_expires_at > self.evaluator.current_time()
                ):
                    continuity_deadline = positive.outage_expires_at
            if continuity_deadline is not None:
                # A positive claim alone is insufficient: another request may
                # have read a newer owner withdrawal or delisting. Revalidate
                # every link against the latest retained record snapshots.
                result = await self.evaluator.check(
                    agent_origin=agent.agent_url,
                    brand_domain=claim.brand_domain,
                    account_operator=claim.account_operator,
                    brand_id=claim.brand_id,
                    countries=claim.countries,
                    scope=claim.scope,
                    signing_profile=signing_profile,
                    allow_stale_on_outage=True,
                )
        with self._history.lock:
            denied = self._history.denials.get(key)
            if result.authorized:
                if denied is not None and denied[0] > started_revision:
                    # An older in-flight approval cannot undo a newer denial.
                    result = denied[1]
                elif continuity_deadline is not None:
                    if (
                        result.outage_expires_at is None
                        or min(continuity_deadline, result.outage_expires_at)
                        <= self.evaluator.current_time()
                    ):
                        result = GrantAuthorization(
                            False,
                            error_code="SERVICE_UNAVAILABLE",
                            include_grant_absent_details=False,
                        )
                    # An outage read must never refresh the retained proof.
                elif (
                    result.expires_at is not None
                    and result.expires_at <= self.evaluator.current_time()
                ):
                    result = GrantAuthorization(
                        False, error_code="SERVICE_UNAVAILABLE", include_grant_absent_details=False
                    )
                else:
                    self._history.denials.pop(key, None)
                    if result.outage_expires_at is not None:
                        self._history.positives[key] = result
                        self._history.positives.move_to_end(key)
            elif result.error_code == "PERMISSION_DENIED":
                self._history.positives.pop(key, None)
                self._history.revision += 1
                self._history.denials[key] = (self._history.revision, result)
            elif result.error_code == "SERVICE_UNAVAILABLE":
                if denied is not None:
                    result = denied[1]
        result.require()

    async def require_account(
        self,
        account: Account[Any],
        agent: BuyerAgent | None,
        *,
        signing_profile: Literal["web-bot-auth", "adcp/request-signing/v1"] = "web-bot-auth",
        operation: str | None = None,
        bound_wba_identity: bool = False,
        operation_params: Mapping[str, Any] | None = None,
        spend_committing: bool | None = None,
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
            )
        await self.require_claim(
            claim,
            agent,
            signing_profile=signing_profile,
            allow_cached_binding_on_outage=(
                bound_wba_identity
                and signing_profile == "web-bot-auth"
                and _cached_binding_may_continue(operation, operation_params, spend_committing)
            ),
        )

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
