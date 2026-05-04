"""Salesagent → SDK projection: BuyerAgentRegistry + AccountStore.

These two stores are how the SDK runtime consults salesagent's existing
Principal + Account tables without modifying the salesagent schema.

The projections:
- Principal.access_token → BuyerAgentRegistry.resolve(auth_info=bearer_token)
- Account → AccountStore.resolve(ref, auth_info)
- AgentAccountAccess junction → access scoping in AccountStore

Salesagent schema citations (read-only against the salesagent main repo):
- Principal: src/core/database/models.py:533
- Account:   src/core/database/models.py:798
- AgentAccountAccess: src/core/database/models.py:868
- AdapterConfig (for HITL flag): src/core/database/models.py:1085
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning.context import RequestContext
from adcp.decisioning.types import Account as SDKAccount
from adcp.decisioning.types import AdcpError, AuthInfo, BuyerAgent

# Salesagent imports — present in the salesagent venv at deploy time.
# Annotated as TYPE_CHECKING-style so this file lints/imports cleanly
# inside adcp-client-python's tree without salesagent installed.
try:
    from sqlalchemy import select  # type: ignore[import-not-found]

    from src.core.database.database_session import (  # type: ignore[import-not-found]
        get_db_session,
    )
    from src.core.database.models import (  # type: ignore[import-not-found]
        Account as SAAccount,
    )
    from src.core.database.models import (  # type: ignore[import-not-found]
        AgentAccountAccess,
        Principal,
    )

    SALESAGENT_AVAILABLE = True
except ImportError:  # pragma: no cover — only runs in adcp-client-python's tree
    SALESAGENT_AVAILABLE = False


# ---------------------------------------------------------------------------
# BuyerAgentRegistry — projects Principal.access_token → BuyerAgent
# ---------------------------------------------------------------------------


class SalesagentBuyerAgentRegistry:
    """Resolve a verified BuyerAgent from a salesagent Principal row.

    Salesagent uses bearer tokens stored in `Principal.access_token`
    (unique, indexed). The SDK's BuyerAgentRegistry contract takes an
    AuthInfo and returns a BuyerAgent or raises.

    No oauth_client_id, key_hash, or signing-key columns on Principal —
    bearer-token auth only (per the schema audit in PR #506).
    """

    def resolve(self, auth_info: AuthInfo) -> BuyerAgent:
        """Verify the bearer token against the principals table.

        :raises AdcpError: AUTH_REQUIRED if no token supplied,
            PERMISSION_DENIED if token doesn't match a Principal row.
        """
        if not SALESAGENT_AVAILABLE:
            raise RuntimeError(
                "SalesagentBuyerAgentRegistry requires salesagent imports. "
                "Deploy via salesagent fork; this module is a reference "
                "implementation in adcp-client-python."
            )

        token = self._extract_bearer(auth_info)
        if token is None:
            raise AdcpError(
                code="AUTH_REQUIRED",
                message="Bearer token required for principal resolution.",
                recovery="terminal",
            )

        with get_db_session() as session:
            stmt = select(Principal).filter_by(access_token=token)
            principal = session.scalars(stmt).first()

        if principal is None:
            raise AdcpError(
                code="PERMISSION_DENIED",
                message="Bearer token does not match a known principal.",
                recovery="terminal",
            )

        return BuyerAgent(
            id=principal.principal_id,
            tenant_id=principal.tenant_id,
            name=principal.name,
            # Adapter-specific identities — Principal.platform_mappings
            # carries e.g. {"google_ad_manager": "advertiser_12345"}
            metadata={
                "tenant_id": principal.tenant_id,
                "platform_mappings": principal.platform_mappings,
            },
        )

    @staticmethod
    def _extract_bearer(auth_info: AuthInfo) -> str | None:
        """Pull a bearer token out of the AuthInfo header dict.

        Adapters might send `Authorization: Bearer <token>` or
        `X-AdCP-Token: <token>` — accept both, prefer Authorization.
        """
        headers = getattr(auth_info, "headers", {}) or {}
        auth_header = headers.get("Authorization") or headers.get("authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return headers.get("X-AdCP-Token") or headers.get("x-adcp-token")


# ---------------------------------------------------------------------------
# AccountStore — projects Account row → SDK Account
# ---------------------------------------------------------------------------


class SalesagentAccountStore:
    """Resolve a salesagent Account row into the SDK's Account shape.

    Salesagent's Account table is already AdCP-shaped (15+ columns
    project directly to wire fields per PR #506 schema audit), so the
    projection is mostly a typed copy plus access-control via the
    AgentAccountAccess junction.
    """

    def __init__(self, *, experiment_tenant_ids: set[str] | None = None) -> None:
        """:param experiment_tenant_ids: Override Account.sandbox=True for
        these tenants when projecting to ``Account.mode='sandbox'``. Used
        to flip an experiment tenant into sandbox mode without touching
        production rows. Default ``None`` (use the column verbatim).
        """
        self._experiment_tenant_ids = experiment_tenant_ids or set()

    async def resolve(self, ref: Any, auth_info: AuthInfo) -> SDKAccount[dict[str, Any]]:
        """Look up the Account row + verify the BuyerAgent has access."""
        if not SALESAGENT_AVAILABLE:
            raise RuntimeError("SalesagentAccountStore requires salesagent imports.")

        tenant_id = self._extract_tenant_id(ref)
        account_id = self._extract_account_id(ref)
        principal_id = self._extract_principal_id(auth_info)

        with get_db_session() as session:
            # Verify access via AgentAccountAccess junction
            access_stmt = select(AgentAccountAccess).filter_by(
                tenant_id=tenant_id,
                principal_id=principal_id,
                account_id=account_id,
            )
            if session.scalars(access_stmt).first() is None:
                raise AdcpError(
                    code="PERMISSION_DENIED",
                    message="Caller is not authorized for this account.",
                    recovery="correctable",
                )

            # Fetch the Account row
            acct_stmt = select(SAAccount).filter_by(tenant_id=tenant_id, account_id=account_id)
            sa_account = session.scalars(acct_stmt).first()

        if sa_account is None:
            raise AdcpError(
                code="NOT_FOUND",
                message=f"Account {account_id} not found.",
                recovery="terminal",
            )

        # Project sandbox: bool → mode: 'live' | 'sandbox'
        # (No 'mock' mode in salesagent's schema; experiment tenant override
        # is the only way to flip mock for the side-car test.)
        if tenant_id in self._experiment_tenant_ids and sa_account.sandbox:
            mode: str = "sandbox"
        elif sa_account.sandbox:
            mode = "sandbox"
        else:
            mode = "live"

        return SDKAccount(
            id=sa_account.account_id,
            mode=mode,  # type: ignore[arg-type]  # SDK Literal type
            metadata={
                "tenant_id": sa_account.tenant_id,
                "principal_id": sa_account.principal_id,
                "platform_mappings": sa_account.platform_mappings,
                "advertiser": sa_account.advertiser,
                "operator": sa_account.operator,
                "billing": sa_account.billing,
                "rate_card": sa_account.rate_card,
                "payment_terms": sa_account.payment_terms,
                "account_scope": sa_account.account_scope,
                "status": sa_account.status,
                # governance_agents on Account is List[GovernanceAgent] — the
                # governance-aware-seller config per PR #489 §3.7.
                "governance_agents": sa_account.governance_agents,
            },
        )

    @staticmethod
    def _extract_tenant_id(ref: Any) -> str:
        """The wire account ref includes a tenant_id; salesagent's schema
        is multi-tenant (tenant_id, account_id) composite key.
        """
        if hasattr(ref, "tenant_id"):
            return ref.tenant_id
        if isinstance(ref, dict):
            return ref["tenant_id"]
        raise ValueError(f"Cannot extract tenant_id from ref: {ref!r}")

    @staticmethod
    def _extract_account_id(ref: Any) -> str:
        if hasattr(ref, "account_id"):
            return ref.account_id
        if isinstance(ref, dict):
            return ref["account_id"]
        raise ValueError(f"Cannot extract account_id from ref: {ref!r}")

    @staticmethod
    def _extract_principal_id(auth_info: AuthInfo) -> str:
        """Resolved BuyerAgent.id is the salesagent Principal.principal_id.
        Pulled from auth_info.principal_id which the framework populates
        after BuyerAgentRegistry.resolve.
        """
        principal_id = getattr(auth_info, "principal_id", None)
        if principal_id is None:
            raise ValueError("auth_info missing principal_id (BuyerAgent not resolved?)")
        return principal_id


# ---------------------------------------------------------------------------
# Helper: fetch AdapterConfig.gam_manual_approval_required for HITL gate
# ---------------------------------------------------------------------------


async def fetch_gam_manual_approval_required(ctx: RequestContext[Any]) -> bool:
    """Read the tenant-scoped HITL flag.

    Salesagent stores this on AdapterConfig (tenant-level), not Account
    (account-level). The before-hook reads it via tenant_id from ctx.
    """
    if not SALESAGENT_AVAILABLE:
        raise RuntimeError("Requires salesagent imports.")

    from src.core.database.models import (  # type: ignore[import-not-found]
        AdapterConfig,
    )

    tenant_id = ctx.account.metadata.get("tenant_id")
    if not tenant_id:
        return False

    with get_db_session() as session:
        stmt = select(AdapterConfig).filter_by(tenant_id=tenant_id)
        config = session.scalars(stmt).first()

    return bool(config and config.gam_manual_approval_required)
