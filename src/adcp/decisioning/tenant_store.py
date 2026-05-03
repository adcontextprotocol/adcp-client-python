"""``create_tenant_store`` — opinionated multi-tenant :class:`AccountStore`
builder with a baked-in per-entry tenant-isolation gate.

Solves the recurring class of bug where adopters routing by wire-supplied
operator without cross-checking the auth principal could write across
tenants. The gate is enforced inside the framework: cross-tenant entries
on ``upsert`` / ``sync_governance`` are rejected with
``PERMISSION_DENIED`` before reaching adopter callbacks.

Mirrors the JS-side ``createTenantStore`` at
``packages/sdk/src/server/decisioning/tenant-store.ts`` (6.7). The
Python adaptation flattens the ``Tenant`` value to a string ``tenant_id``
since adopters typically denormalize the owning tenant onto the Account
itself; the security semantics are unchanged.

**Fail-closed.** When ``resolve_from_auth(ctx)`` returns ``None``
(unknown / unauthenticated principal):

* ``resolve`` returns ``None``
* ``upsert`` rejects every entry with ``PERMISSION_DENIED``
* ``sync_governance`` rejects every entry with ``PERMISSION_DENIED``
* ``list`` returns ``[]``

Don't fork this around to fail-open. Adopters who copied the prior
fail-open shape (``if home_tenant_id and tenant_id != home_tenant_id``)
silently disabled isolation when a credential lacked a tenant binding.

**Immutability.** The returned store's methods are defined on the class
(not assigned in ``__init__`` to a callable hook), and the class uses
``__slots__`` to forbid instance attribute assignment. An adopter who
writes ``store.upsert = custom_handler`` after construction gets an
:class:`AttributeError` instead of silently bypassing the gate. Adopters
with genuine custom needs compose at the method level (wrap the returned
store) or write a plain :class:`AccountStore` and own the gate.

``_TenantStore`` is intentionally not exported from
``adcp.decisioning.__init__``; only the :func:`create_tenant_store`
factory is public. Class-level monkey-patching is possible in pure
Python (no language-level final), but the leading-underscore +
non-export keep it out of adopter code paths.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Generic, cast

from typing_extensions import TypeVar

from adcp.decisioning.accounts import ResolveContext
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.types import (
    Account,
    SyncAccountsResultRow,
    SyncGovernanceEntry,
    SyncGovernanceResultRow,
)

if TYPE_CHECKING:
    from adcp.types import AccountReference

logger = logging.getLogger(__name__)

# Alias for the builtin ``list`` so annotations on the
# :meth:`_TenantStore.list` method (which shadows ``list`` inside the
# class body for forward-ref name resolution) keep referring to the
# generic-list type.
_BuiltinList = list

__all__ = ["create_tenant_store"]

#: Per-platform metadata generic. Defaults to ``dict[str, Any]`` —
#: matches :class:`Account[TMeta]`'s default.
TMeta = TypeVar("TMeta", default=dict[str, Any])

# Type aliases for the adopter callbacks. All callables may be sync OR
# async; the helper awaits at call time. AccountReference enters as
# either a Pydantic model or a raw dict (legacy callers) — typed as
# ``Any`` on the callable boundary to avoid forcing adopters into a
# specific arm of the discriminated union.
_ResolveByRef = Callable[
    [Any, ResolveContext],
    "Awaitable[Account[TMeta] | None] | Account[TMeta] | None",
]
_ResolveFromAuth = Callable[[ResolveContext], "Awaitable[str | None] | str | None"]
_TenantIdFn = Callable[["Account[TMeta]"], str]
_TenantToAccount = Callable[[str], "Awaitable[Account[TMeta] | None] | Account[TMeta] | None"]
_UpsertRow = Callable[
    [Any, ResolveContext],
    "Awaitable[SyncAccountsResultRow] | SyncAccountsResultRow",
]
_SyncGovernanceRow = Callable[
    [SyncGovernanceEntry, ResolveContext],
    "Awaitable[SyncGovernanceResultRow] | SyncGovernanceResultRow",
]


async def _await_maybe(value: Any) -> Any:
    """Resolve a value that may be a coroutine OR a plain return.

    Adopter callbacks are sync OR async; this shim keeps the helper's
    own dispatch uniform without forcing every adopter to write
    ``async def``.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def _ref_field(ref: Any, name: str) -> Any:
    """Read a field off an :class:`AccountReference` whether it arrived
    as a Pydantic model (RootModel proxy) or a raw dict.

    Mirrors the JS ``narrowAccountRef`` helper. The wire schema is a
    discriminated union of ``{account_id}`` and ``{brand, operator}``;
    upstream validation guarantees one arm is populated, so widening to
    an all-optional read is safe.
    """
    if ref is None:
        return None
    if isinstance(ref, dict):
        return ref.get(name)
    return getattr(ref, name, None)


def _account_not_found_message(ref: Any) -> str:
    account_id = _ref_field(ref, "account_id")
    if account_id:
        return f"Unknown account_id: {account_id}"
    operator = _ref_field(ref, "operator")
    if operator:
        return f"Unknown operator: {operator}"
    return "Unknown account reference"


def _permission_denied_message(ref: Any) -> str:
    operator = _ref_field(ref, "operator")
    account_id = _ref_field(ref, "account_id")
    subject = operator or account_id or "this account"
    return (
        f"Buyer agent has no authority over '{subject}' "
        "(tenant mismatch or auth principal not registered)."
    )


def _build_failed_sync_accounts_row(ref: Any, code: str, message: str) -> SyncAccountsResultRow:
    """Construct a wire-shaped failure row for ``sync_accounts``.

    The wire schema requires ``brand`` + ``operator`` on every row, so
    when the input ref is ``account_id``-only we synthesize ``'unknown'``
    placeholders — the buyer's actionable signal is ``errors[0].code``;
    ``brand`` / ``operator`` here are wire-required scaffolding, not
    authoritative echoes.
    """
    brand_field = _ref_field(ref, "brand") or {}
    if not isinstance(brand_field, dict):
        # Pydantic BrandReference — coerce via attribute access
        brand_dict = {"domain": getattr(brand_field, "domain", "unknown.example")}
    else:
        brand_dict = {"domain": brand_field.get("domain", "unknown.example")}
    operator = _ref_field(ref, "operator") or "unknown"
    account_id = _ref_field(ref, "account_id")
    return SyncAccountsResultRow(
        brand=brand_dict,
        operator=operator,
        action="failed",
        status="rejected",
        errors=[{"code": code, "message": message}],
        account_id=account_id if isinstance(account_id, str) else None,
    )


def _build_failed_sync_governance_row(
    entry: SyncGovernanceEntry, code: str, message: str
) -> SyncGovernanceResultRow:
    return SyncGovernanceResultRow(
        account=entry.account,
        status="failed",
        errors=[{"code": code, "message": message}],
    )


def _default_unchanged_row(ref: Any) -> SyncAccountsResultRow:
    """Build a no-op success row when adopter omits ``upsert_row``.

    Matches the wire shape with ``action='unchanged'`` so authorized
    entries don't surface as 501 / UNSUPPORTED_FEATURE just because
    the adopter has no persistence to perform.
    """
    brand_field = _ref_field(ref, "brand") or {}
    if not isinstance(brand_field, dict):
        brand_dict = {"domain": getattr(brand_field, "domain", "unknown.example")}
    else:
        brand_dict = {"domain": brand_field.get("domain", "unknown.example")}
    operator = _ref_field(ref, "operator") or "unknown"
    account_id = _ref_field(ref, "account_id")
    return SyncAccountsResultRow(
        brand=brand_dict,
        operator=operator,
        action="unchanged",
        status="active",
        account_id=account_id if isinstance(account_id, str) else None,
    )


class _TenantStore(Generic[TMeta]):
    """Concrete :class:`AccountStore` with per-entry tenant gate.

    Methods are defined on the class (not assigned in ``__init__``) so
    they can't be monkey-patched to bypass isolation. ``__slots__``
    forbids instance attribute assignment — adopters who try to
    override ``upsert`` get :class:`AttributeError`.
    """

    __slots__ = (
        "_resolve_by_ref",
        "_resolve_from_auth",
        "_tenant_id",
        "_tenant_to_account",
        "_upsert_row",
        "_sync_governance_row",
    )

    # Required for AccountStore Protocol structural matching. The
    # tenant-store helper covers all three resolution shapes, so the
    # most useful single literal is ``'explicit'`` (wire ref drives
    # lookup when present); Path-2 (auth-derived) is handled
    # transparently inside ``resolve``.
    resolution = "explicit"

    def __init__(
        self,
        *,
        resolve_by_ref: _ResolveByRef[TMeta],
        resolve_from_auth: _ResolveFromAuth,
        tenant_id: _TenantIdFn[TMeta],
        tenant_to_account: _TenantToAccount[TMeta],
        upsert_row: _UpsertRow | None = None,
        sync_governance_row: _SyncGovernanceRow | None = None,
    ) -> None:
        self._resolve_by_ref = resolve_by_ref
        self._resolve_from_auth = resolve_from_auth
        self._tenant_id = tenant_id
        self._tenant_to_account = tenant_to_account
        self._upsert_row = upsert_row
        self._sync_governance_row = sync_governance_row

    async def _auth_tenant(self, ctx: ResolveContext) -> str | None:
        """Compute the auth principal's tenant once per request."""
        return cast("str | None", await _await_maybe(self._resolve_from_auth(ctx)))

    async def resolve(
        self,
        ref: AccountReference | dict[str, Any] | None,
        auth_info: AuthInfo | None = None,
    ) -> Account[TMeta] | None:
        """Resolve a wire reference to the tenant-scoped Account.

        Signature matches the :class:`AccountStore` Protocol
        (``resolve(ref, auth_info=None)``); the dispatcher calls this
        as ``accounts.resolve(ref_dict, auth_info=auth_info)``. We
        synthesize a :class:`ResolveContext` internally so the adopter's
        ``resolve_by_ref`` callback continues to take ``(ref, ctx)`` —
        that keeps the adopter API uniform with ``upsert_row`` /
        ``sync_governance_row``.

        Two paths:

        * **Path 1** (ref provided): call ``resolve_by_ref(ref, ctx)``,
          then verify the resolved account's tenant matches the auth
          principal's tenant. Mismatch → return ``None`` (the gate
          hides the existence of cross-tenant accounts from the
          caller's perspective).

        * **Path 2** (ref is ``None``): derive tenant from auth, then
          project to Account via ``tenant_to_account``.

        Auth tenant ``None`` → ``None`` on either path. The framework
        treats ``None`` as ``ACCOUNT_NOT_FOUND`` for tools that require
        an account.
        """
        resolve_ctx = ResolveContext(auth_info=auth_info, tool_name="resolve")
        auth_tid = await self._auth_tenant(resolve_ctx)
        if auth_tid is None:
            return None

        if ref is None:
            return cast(
                "Account[TMeta] | None",
                await _await_maybe(self._tenant_to_account(auth_tid)),
            )

        try:
            account = cast(
                "Account[TMeta] | None",
                await _await_maybe(self._resolve_by_ref(ref, resolve_ctx)),
            )
        except Exception:
            # Per-request consistency with the per-entry isolation in
            # ``upsert`` / ``sync_governance``: log-and-deny rather
            # than 500-ing the calling tool. Adopter exception details
            # stay server-side (could carry stack/DB info).
            logger.warning(
                "tenant_store.resolve: resolve_by_ref raised; treating as ACCOUNT_NOT_FOUND",
                exc_info=True,
            )
            return None
        if account is None:
            return None
        if self._tenant_id(account) != auth_tid:
            return None
        return account

    async def upsert(
        self,
        refs: _BuiltinList[AccountReference | dict[str, Any]],
        ctx: ResolveContext | None = None,
    ) -> _BuiltinList[SyncAccountsResultRow]:
        """Per-entry tenant-isolation gate for ``sync_accounts``.

        For each ref:

        1. Compute the entry's tenant via ``resolve_by_ref``.
        2. Compare against the auth principal's tenant
           (``resolve_from_auth(ctx)``, computed once per call).
        3. Unknown ref → ``ACCOUNT_NOT_FOUND``.
        4. Auth tenant ``None`` OR auth tenant != entry tenant →
           ``PERMISSION_DENIED`` (fail-closed).
        5. Otherwise, dispatch to ``upsert_row`` (or no-op
           ``action='unchanged'`` if no hook).

        Sequential, not concurrent: adopter ``upsert_row`` callbacks
        commonly mutate shared tenant state; concurrent invocations
        against the same tenant are an entropy source the helper
        shouldn't introduce. Adopters who want parallel writes fan out
        inside their own callback.
        """
        resolve_ctx = ctx if ctx is not None else ResolveContext()
        auth_tid = await self._auth_tenant(resolve_ctx)

        rows: _BuiltinList[SyncAccountsResultRow] = []
        for ref in refs:
            try:
                entry_account = await _await_maybe(self._resolve_by_ref(ref, resolve_ctx))
            except Exception:
                # Per-entry isolation: one bad row must not poison the
                # batch. Log server-side; emit PERMISSION_DENIED on the
                # wire (don't leak adopter exception detail — could
                # carry stack/DB info).
                logger.warning(
                    "tenant_store.upsert: resolve_by_ref raised for entry; "
                    "rejecting with PERMISSION_DENIED",
                    exc_info=True,
                )
                rows.append(
                    _build_failed_sync_accounts_row(
                        ref, "PERMISSION_DENIED", _permission_denied_message(ref)
                    )
                )
                continue
            if entry_account is None:
                rows.append(
                    _build_failed_sync_accounts_row(
                        ref, "ACCOUNT_NOT_FOUND", _account_not_found_message(ref)
                    )
                )
                continue
            try:
                entry_tid = self._tenant_id(entry_account)
            except Exception:
                logger.warning(
                    "tenant_store.upsert: tenant_id raised for entry; "
                    "rejecting with PERMISSION_DENIED",
                    exc_info=True,
                )
                rows.append(
                    _build_failed_sync_accounts_row(
                        ref, "PERMISSION_DENIED", _permission_denied_message(ref)
                    )
                )
                continue
            if auth_tid is None or auth_tid != entry_tid:
                rows.append(
                    _build_failed_sync_accounts_row(
                        ref, "PERMISSION_DENIED", _permission_denied_message(ref)
                    )
                )
                continue
            if self._upsert_row is None:
                rows.append(_default_unchanged_row(ref))
            else:
                rows.append(await _await_maybe(self._upsert_row(ref, resolve_ctx)))
        return rows

    async def list(
        self,
        filter: dict[str, Any] | None = None,  # noqa: A002 — wire field name
        ctx: ResolveContext | None = None,
    ) -> _BuiltinList[Account[TMeta]]:
        """Return the accounts visible to the calling principal.

        Single-tenant projection: derive the auth tenant, project to
        an Account, return as a one-element list. Unauthenticated /
        unregistered → ``[]`` (fail-closed but quiet — ``list`` MUST
        NOT raise on a per-spec valid request).

        ``filter`` is accepted for AccountStore Protocol parity but
        not interpreted here — adopters needing filter / pagination
        compose by wrapping the returned store.
        """
        del filter  # accepted for Protocol parity; not interpreted
        resolve_ctx = ctx if ctx is not None else ResolveContext()
        auth_tid = await self._auth_tenant(resolve_ctx)
        if auth_tid is None:
            return []
        try:
            account = await _await_maybe(self._tenant_to_account(auth_tid))
        except Exception:
            # ``list`` MUST NOT raise on a per-spec valid request
            # (docstring contract). Fail-closed quiet — same outcome
            # as auth-None: the caller sees an empty list.
            logger.warning(
                "tenant_store.list: tenant_to_account raised; returning []",
                exc_info=True,
            )
            return []
        if account is None:
            return []
        return [account]

    async def sync_governance(
        self,
        entries: _BuiltinList[SyncGovernanceEntry],
        ctx: ResolveContext | None = None,
    ) -> _BuiltinList[SyncGovernanceResultRow]:
        """Per-entry tenant gate for ``sync_governance``. Same rules as
        :meth:`upsert`, shaped for the ``SyncGovernanceResultRow`` arm
        (``status='failed'`` with per-entry ``errors``)."""
        resolve_ctx = ctx if ctx is not None else ResolveContext()
        auth_tid = await self._auth_tenant(resolve_ctx)

        rows: _BuiltinList[SyncGovernanceResultRow] = []
        for entry in entries:
            try:
                entry_account = await _await_maybe(self._resolve_by_ref(entry.account, resolve_ctx))
            except Exception:
                logger.warning(
                    "tenant_store.sync_governance: resolve_by_ref raised for entry; "
                    "rejecting with PERMISSION_DENIED",
                    exc_info=True,
                )
                rows.append(
                    _build_failed_sync_governance_row(
                        entry,
                        "PERMISSION_DENIED",
                        _permission_denied_message(entry.account),
                    )
                )
                continue
            if entry_account is None:
                rows.append(
                    _build_failed_sync_governance_row(
                        entry,
                        "ACCOUNT_NOT_FOUND",
                        _account_not_found_message(entry.account),
                    )
                )
                continue
            try:
                entry_tid = self._tenant_id(entry_account)
            except Exception:
                logger.warning(
                    "tenant_store.sync_governance: tenant_id raised for entry; "
                    "rejecting with PERMISSION_DENIED",
                    exc_info=True,
                )
                rows.append(
                    _build_failed_sync_governance_row(
                        entry,
                        "PERMISSION_DENIED",
                        _permission_denied_message(entry.account),
                    )
                )
                continue
            if auth_tid is None or auth_tid != entry_tid:
                rows.append(
                    _build_failed_sync_governance_row(
                        entry,
                        "PERMISSION_DENIED",
                        _permission_denied_message(entry.account),
                    )
                )
                continue
            if self._sync_governance_row is None:
                rows.append(
                    SyncGovernanceResultRow(
                        account=entry.account,
                        status="synced",
                        governance_agents=list(entry.governance_agents),
                    )
                )
            else:
                rows.append(await _await_maybe(self._sync_governance_row(entry, resolve_ctx)))
        return rows


def create_tenant_store(
    *,
    resolve_by_ref: _ResolveByRef[TMeta],
    resolve_from_auth: _ResolveFromAuth,
    tenant_id: _TenantIdFn[TMeta],
    tenant_to_account: _TenantToAccount[TMeta],
    upsert_row: _UpsertRow | None = None,
    sync_governance_row: _SyncGovernanceRow | None = None,
) -> _TenantStore[TMeta]:
    """Build an :class:`AccountStore` whose ``resolve`` / ``upsert`` /
    ``list`` / ``sync_governance`` methods enforce tenant isolation.

    :param resolve_by_ref: ``(ref, ctx) -> Account | None``. Resolves a
        wire :class:`AccountReference` to the framework Account it
        points at — independent of who the caller is. Return ``None``
        if the ref is unknown (helper emits ``ACCOUNT_NOT_FOUND`` for
        that row). May be sync or async.
    :param resolve_from_auth: ``(ctx) -> tenant_id | None``. Derives the
        tenant from the auth principal. Return ``None`` if no principal
        is resolvable (no auth, principal not registered) — every entry
        on per-entry tools then fails ``PERMISSION_DENIED``
        (fail-closed).
    :param tenant_id: ``(account) -> str``. Stable identity for tenant-
        equality checks. The helper compares
        ``tenant_id(entry_account) == resolve_from_auth(ctx)`` to
        enforce isolation. A stable string id beats reference equality
        (Postgres-backed stores hand back fresh objects each fetch).
    :param tenant_to_account: ``(tenant_id) -> Account | None``. Project
        a tenant id to its Account. Used by Path-2 ``resolve``
        (no-ref tools) and by ``list``.
    :param upsert_row: Optional ``(ref, ctx) -> SyncAccountsResultRow``
        per-entry storage callback. Cross-tenant entries and unknown-ref
        entries NEVER reach this callback — the helper builds
        ``PERMISSION_DENIED`` / ``ACCOUNT_NOT_FOUND`` rows for those
        before invoking adopter code. Omit for adopters whose platform
        doesn't claim ``sync_accounts``; the helper returns
        ``action='unchanged'`` for authorized rows in that case.
    :param sync_governance_row: Optional ``(entry, ctx) ->
        SyncGovernanceResultRow``. Same gating rules as ``upsert_row``.
        Adopters persist the buyer's governance-agent binding here.

    :returns: An :class:`AccountStore`-shaped object whose gate methods
        are class-level (immutable per instance — ``__slots__`` forbids
        attribute assignment).

    Example::

        from adcp.decisioning import create_tenant_store

        store = create_tenant_store(
            resolve_by_ref=lambda ref, ctx: lookup_account_by_ref(ref),
            resolve_from_auth=lambda ctx: principal_to_tenant.get(
                ctx.auth_info.principal if ctx.auth_info else None
            ),
            tenant_id=lambda account: account.metadata["tenant_id"],
            tenant_to_account=lambda tid: tenants[tid].account,
            upsert_row=lambda ref, ctx: persist_account(ref),
        )
    """
    return _TenantStore(
        resolve_by_ref=resolve_by_ref,
        resolve_from_auth=resolve_from_auth,
        tenant_id=tenant_id,
        tenant_to_account=tenant_to_account,
        upsert_row=upsert_row,
        sync_governance_row=sync_governance_row,
    )
