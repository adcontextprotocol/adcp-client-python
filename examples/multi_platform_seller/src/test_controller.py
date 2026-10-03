"""Controller fixtures share the account store used by normal dispatch."""

from __future__ import annotations

from time import monotonic
from typing import Any

from adcp.decisioning import RequestContext
from adcp.server import ToolContext, current_tenant
from adcp.server.test_controller import TestControllerError, TestControllerStore
from examples.multi_platform_seller.src.account_store import MultiTenantAccountStore


class MultiTenantTestController(TestControllerStore):
    def __init__(self, accounts: MultiTenantAccountStore) -> None:
        self.accounts = accounts
        self._rejections: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

    async def seed_account(
        self,
        account_id: str,
        fixture: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        del context
        self.accounts.seed(account_id, fixture or {})
        return {"account_id": account_id}

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
        """Force one sandbox merchandising rejection, scoped to its caller."""
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
        if tenant is None:
            raise TestControllerError("PERMISSION_DENIED", "A tenant host is required")
        resolved = self.accounts.resolve(account)
        if resolved.mode != "sandbox":
            raise TestControllerError("FORBIDDEN", "Only sandbox accounts are controllable")
        principal = context.caller_identity if context is not None else None
        now = monotonic()
        self._rejections = {key: value for key, value in self._rejections.items() if value[0] > now}
        key = (tenant.id, str(resolved.id), principal or "public-demo")
        if key not in self._rejections and len(self._rejections) >= 256:
            raise TestControllerError("INVALID_STATE", "Too many pending directives")
        forced: dict[str, Any] = {"arm": "rejected", "reason": reason}
        if suggestions is not None:
            forced["suggestions"] = list(suggestions)
        self._rejections[key] = (now + 300, forced)
        return {"forced": forced}

    def take_rejection(self, ctx: RequestContext[Any]) -> dict[str, Any] | None:
        principal = ctx.auth_principal or "public-demo"
        key = (str(ctx.account.metadata["tenant_id"]), str(ctx.account.id), principal)
        directive = self._rejections.pop(key, None)
        if directive is None or directive[0] <= monotonic():
            return None
        return {"status": "rejected", **{k: v for k, v in directive[1].items() if k != "arm"}}
