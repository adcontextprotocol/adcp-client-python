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

from typing import TYPE_CHECKING, Any, Literal

from adcp.decisioning import AdcpError
from adcp.decisioning.accounts import AccountStore
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

        account_id = (ref or {}).get("account_id") if isinstance(ref, dict) else None
        if not account_id:
            account_id = f"{tenant_id}:default"

        return Account(
            id=account_id,
            metadata={"tenant_id": tenant_id},
            auth_info=_auth_info_to_dict(auth_info),
        )

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
