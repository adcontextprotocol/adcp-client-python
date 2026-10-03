"""Opt-in commercial fixtures for the loopback-only translator sandbox.

The controller seeds accounts in the seller's actual SQL store. Product,
pricing, creative, and buy state remain owned by the upstream ad server;
unsupported upstream fixture operations are deliberately not advertised.
"""

from __future__ import annotations

from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select

from adcp.decisioning.context import AuthInfo
from adcp.server import ToolContext, current_tenant
from adcp.server.test_controller import TestControllerError, TestControllerStore

from .models import Account as AccountRow


class ReferenceFixtureController(TestControllerStore):
    """Seed caller-owned accounts with server-owned mock upstream routing."""

    def __init__(self, platform: Any, *, mock_upstream_url: str) -> None:
        url = urlsplit(mock_upstream_url)
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("reference fixture controller requires a loopback HTTP mock upstream")
        self.platform = platform
        self.mock_upstream_url = mock_upstream_url
        self._rejections: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
        platform._fixture_controller = self

    async def force_get_products_arm(
        self,
        arm: str,
        task_id: str | None = None,
        message: str | None = None,
        reason: str | None = None,
        suggestions: list[str] | None = None,
        *,
        account: dict[str, Any] | None = None,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Apply one caller-scoped sandbox merchandising rejection.

        This controls the seller's business rejection, not upstream catalog
        data or asynchronous tasks. Directives expire after five minutes.
        """
        if arm != "rejected" or task_id is not None or message is not None:
            raise TestControllerError("INVALID_PARAMS", "Only the rejected arm is supported")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 2000:
            raise TestControllerError("INVALID_PARAMS", "A sanitized rejection reason is required")
        if suggestions is not None and (
            not 1 <= len(suggestions) <= 20
            or any(not isinstance(item, str) or not 1 <= len(item) <= 1000 for item in suggestions)
        ):
            raise TestControllerError("INVALID_PARAMS", "Invalid rejection suggestions")
        tenant = current_tenant()
        auth_info = context.metadata.get("adcp.auth_info") if context is not None else None
        if tenant is None or not isinstance(auth_info, AuthInfo) or not auth_info.principal:
            raise TestControllerError("PERMISSION_DENIED", "An authenticated tenant is required")
        resolved = await self.platform.accounts.resolve(account, auth_info=auth_info)
        if (
            resolved.mode != "mock"
            or resolved.metadata.get("mock_upstream_url") != self.mock_upstream_url
        ):
            raise TestControllerError(
                "PERMISSION_DENIED", "Only the configured mock is controllable"
            )
        now = monotonic()
        self._rejections = {key: value for key, value in self._rejections.items() if value[0] > now}
        key = (tenant.id, str(resolved.id), auth_info.principal)
        if key not in self._rejections and len(self._rejections) >= 256:
            raise TestControllerError("INVALID_STATE", "Too many pending rejection directives")
        forced: dict[str, Any] = {"arm": "rejected", "reason": reason}
        if suggestions is not None:
            forced["suggestions"] = list(suggestions)
        self._rejections[key] = (now + 300, forced)
        return {"forced": forced}

    def take_rejection(self, ctx: Any) -> dict[str, Any] | None:
        """Consume exactly one directive for the resolved principal/account."""
        auth_info = ctx.auth_info
        if ctx.account is None or auth_info is None or not auth_info.principal:
            return None
        key = (str(ctx.account.metadata.get("tenant_id")), str(ctx.account.id), auth_info.principal)
        directive = self._rejections.pop(key, None)
        if directive is None or directive[0] <= monotonic():
            return None
        forced = directive[1]
        return {
            "status": "rejected",
            **{key: value for key, value in forced.items() if key != "arm"},
        }

    async def seed_account(
        self,
        account_id: str,
        fixture: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        tenant = current_tenant()
        auth_info = context.metadata.get("adcp.auth_info") if context is not None else None
        if tenant is None or not isinstance(auth_info, AuthInfo) or not auth_info.principal:
            raise TestControllerError("PERMISSION_DENIED", "An authenticated tenant is required")
        template = await self.platform.accounts.resolve(None, auth_info=auth_info)
        if (
            template.mode != "mock"
            or template.metadata.get("mock_upstream_url") != self.mock_upstream_url
        ):
            raise TestControllerError("PERMISSION_DENIED", "Only the configured mock is seedable")
        fixture = fixture or {}
        status = fixture.get("status", "active")
        if status not in {"active", "pending_approval", "suspended", "closed"}:
            raise TestControllerError("INVALID_PARAMS", "Unsupported account fixture status")
        billing = fixture.get("billing", "operator")
        if billing != "operator":
            raise TestControllerError("INVALID_PARAMS", "Reference fixtures use operator billing")
        sandbox = fixture.get("sandbox", True)
        if not isinstance(sandbox, bool):
            raise TestControllerError("INVALID_PARAMS", "sandbox must be a boolean")
        brand = fixture.get("brand")
        operator = fixture.get("operator")
        if not isinstance(brand, dict) or not isinstance(brand.get("domain"), str):
            raise TestControllerError("INVALID_PARAMS", "fixture.brand.domain is required")
        if not isinstance(operator, str) or not operator:
            raise TestControllerError("INVALID_PARAMS", "fixture.operator is required")
        # Routing never comes from fixture.ext: all fixture accounts use the
        # authenticated caller's provisioned network and advertiser upstream.
        routing = {
            "network_code": template.metadata["network_code"],
            "advertiser_id": template.metadata["advertiser_id"],
            "fixture_scope": {
                "brand": brand,
                "operator": operator,
                "operator_unit": fixture.get("operator_unit"),
                "currency": fixture.get("currency"),
                "timezone": fixture.get("timezone"),
            },
        }
        buyer_agent_id = template.metadata["buyer_agent_id"]
        async with self.platform._sessionmaker() as session, session.begin():
            result = await session.execute(
                select(AccountRow).where(
                    AccountRow.tenant_id == tenant.id,
                    AccountRow.account_id == account_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is not None and row.buyer_agent_id != buyer_agent_id:
                raise TestControllerError("NOT_FOUND", "Account fixture was not found")
            if row is None:
                row = AccountRow(
                    tenant_id=tenant.id,
                    buyer_agent_id=buyer_agent_id,
                    account_id=account_id,
                    name=str(fixture.get("name") or account_id),
                )
                session.add(row)
            row.status = status
            row.billing = billing
            row.sandbox = sandbox
            row.ext = routing
        return {"account_id": account_id}


__all__ = ["ReferenceFixtureController"]
