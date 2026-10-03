"""Multi-tenant account store — resolves wire account refs to accounts
that carry ``metadata['tenant_id']`` for :class:`PlatformRouter` dispatch.

Two resolution paths share the same store:

1. **Subdomain-routed** (production-ish): the ASGI middleware
   :class:`adcp.server.SubdomainTenantMiddleware` extracts the tenant
   from the ``Host`` header and stashes it on the
   :func:`adcp.server.current_tenant` contextvar. The store reads that
   contextvar to stamp the tenant id onto the resolved account.
2. **Explicit ref** (storyboard / dev): the wire request carries
   ``account.account_id`` like ``tenant-a:acct_demo``. The store splits
   on ``:`` and uses the prefix as the tenant id.

In production adopters typically use one path or the other. The
example accepts both so the storyboard runner (which sends explicit
account refs) and a subdomain-aware buyer (which doesn't) both work
against the same boot.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Literal

from adcp.decisioning import AdcpError
from adcp.decisioning.accounts import AccountStore, ResolveContext
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.types import Account
from adcp.server import current_tenant


class MultiTenantAccountStore:
    """:class:`AccountStore` impl that wires tenant routing to
    :class:`PlatformRouter`.

    :param tenants: The set of recognized tenant ids. Resolution refuses
        anything outside this set with ``ACCOUNT_NOT_FOUND``.
    """

    resolution: Literal["explicit"] = "explicit"

    def __init__(self, *, tenants: frozenset[str]) -> None:
        if not tenants:
            raise ValueError("MultiTenantAccountStore requires non-empty tenants")
        self._tenants = tenants
        self._lock = threading.RLock()
        self._accounts: dict[tuple[str, str], Account[dict[str, Any]]] = {}
        self._natural_ids: dict[tuple[str, str, str, str, bool], str] = {}

    def resolve(
        self,
        ref: dict[str, Any] | None = None,
        auth_info: AuthInfo | None = None,
    ) -> Account[dict[str, Any]]:
        """Resolve a wire ref + auth context to a tenant-scoped Account.

        Resolution order:

        1. Subdomain-set contextvar (set by
           :class:`SubdomainTenantMiddleware`) — production path.
        2. Account ref prefix ``tenant-a:acct_demo`` — storyboard/dev.
        3. Reject with ``ACCOUNT_NOT_FOUND``.
        """
        tenant_id = self._tenant_from_subdomain() or self._tenant_from_ref(ref)
        if tenant_id is None or tenant_id not in self._tenants:
            raise AdcpError(
                "ACCOUNT_NOT_FOUND",
                message=(
                    "Could not resolve a tenant for this request. Either "
                    "send via the tenant subdomain (e.g. "
                    "tenant-a.localhost) or pass account.account_id with "
                    "a 'tenant-x:' prefix. Recognized tenants: "
                    f"{sorted(self._tenants)}."
                ),
                recovery="terminal",
                field="account",
            )

        ref = ref or {}
        account_id = ref.get("account_id")
        with self._lock:
            if not account_id:
                account_id = self._natural_ids.get(self._natural_key(tenant_id, ref))
            if account_id and (stored := self._accounts.get((tenant_id, str(account_id)))):
                return stored
        if not account_id:
            account_id = f"{tenant_id}:default"

        return Account(
            id=account_id,
            metadata={"tenant_id": tenant_id},
            auth_info=_auth_info_to_dict(auth_info),
            mode="sandbox",
            _mode_explicit=True,
        )

    def seed(self, account_id: str, fixture: dict[str, Any]) -> None:
        """Persist a controller fixture in the tenant selected by the host."""
        tenant_id = self._tenant_from_subdomain()
        if tenant_id is None or tenant_id not in self._tenants:
            raise AdcpError("ACCOUNT_NOT_FOUND", message="Account seeding requires a tenant host")
        with self._lock:
            self._accounts[(tenant_id, account_id)] = Account(
                id=account_id,
                name=str(fixture.get("name") or f"{tenant_id} fixture account"),
                status=str(fixture.get("status") or "active"),
                metadata={"tenant_id": tenant_id, "fixture": dict(fixture)},
                mode="sandbox" if fixture.get("sandbox", True) else "live",
                _mode_explicit=True,
            )
            self._natural_ids[self._natural_key(tenant_id, fixture)] = account_id

    @staticmethod
    def _natural_key(tenant_id: str, ref: dict[str, Any]) -> tuple[str, str, str, str, bool]:
        brand = ref.get("brand") or {}
        unit = ref.get("operator_unit") or {}
        return (
            tenant_id,
            str(brand.get("domain", "")),
            str(ref.get("operator", "")),
            str(unit.get("id", "")),
            bool(ref.get("sandbox", True)),
        )

    def list(
        self,
        filter: dict[str, Any] | None = None,
        ctx: ResolveContext | None = None,
    ) -> list[Account[dict[str, Any]]]:
        """Discover the current tenant's demo account without exposing siblings."""
        del ctx
        tenant_id = self._tenant_from_subdomain()
        if tenant_id is None or tenant_id not in self._tenants:
            return []
        default = Account(
            id=f"{tenant_id}:default",
            name=f"{tenant_id} demo account",
            metadata={"tenant_id": tenant_id},
            mode="sandbox",
            _mode_explicit=True,
        )
        with self._lock:
            accounts = [
                account for (tenant, _), account in self._accounts.items() if tenant == tenant_id
            ]
        accounts = [default, *accounts]
        status = (filter or {}).get("status")
        if status is not None:
            statuses = status if isinstance(status, list) else [status]
            accounts = [account for account in accounts if account.status in statuses]
        sandbox = (filter or {}).get("sandbox")
        if sandbox is not None:
            accounts = [account for account in accounts if account.sandbox == sandbox]
        return accounts

    # ----- internals --------------------------------------------------

    def _tenant_from_subdomain(self) -> str | None:
        tenant = current_tenant()
        if tenant is None:
            return None
        return tenant.id

    def _tenant_from_ref(self, ref: dict[str, Any] | None) -> str | None:
        if not ref:
            return None
        account_id = ref.get("account_id") if isinstance(ref, dict) else None
        if not account_id or not isinstance(account_id, str):
            return None
        if ":" not in account_id:
            # Storyboard convention is ``tenant:rest``. Bare account ids
            # without a prefix don't carry tenant information.
            return None
        prefix, _ = account_id.split(":", 1)
        return prefix


def _auth_info_to_dict(auth_info: AuthInfo | None) -> dict[str, Any] | None:
    if auth_info is None:
        return None
    return {
        "kind": auth_info.kind,
        "key_id": auth_info.key_id,
        "principal": auth_info.principal,
        "scopes": list(auth_info.scopes),
    }


# Static-type assertion: the store satisfies the AccountStore Protocol.
# TYPE_CHECKING-only so the assertion has zero runtime cost and never
# exposes a dummy "_assertion" tenant via the registry. Mypy still
# reads it; runtime callers use isinstance via the Protocol's
# @runtime_checkable decorator.
if TYPE_CHECKING:
    _ASSERT: AccountStore[dict[str, Any]] = MultiTenantAccountStore(
        tenants=frozenset({"_assertion"})
    )


__all__ = ["MultiTenantAccountStore"]
