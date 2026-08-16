"""Tests for ``create_tenant_store`` — the opinionated multi-tenant
:class:`AccountStore` builder with a baked-in per-entry tenant gate.

Mirrors the security semantics of the JS-side ``createTenantStore`` at
``packages/sdk/src/server/decisioning/tenant-store.ts``: cross-tenant
and unknown entries on ``upsert`` / ``sync_governance`` collapse to
``ACCOUNT_NOT_FOUND`` before reaching adopter callbacks. Fail-closed
when ``resolve_from_auth`` returns ``None``.

The gate methods (``upsert``, ``sync_governance``) are defined on the
class — adopters cannot monkey-patch instances to bypass isolation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from adcp.decisioning import (
    Account,
    AccountStore,
    AuthInfo,
    ResolveContext,
    SyncAccountsResultRow,
    SyncGovernanceEntry,
    SyncGovernanceResultRow,
    create_tenant_store,
)
from adcp.types import AccountReference

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Account directory keyed by tenant_id. Each tenant owns exactly one
# account; in a real adopter the per-account-per-tenant projection is
# more elaborate, but the gate semantics are identical.
ACCOUNTS: dict[str, Account] = {
    "t_pinnacle": Account(id="acc_pinnacle", name="Pinnacle"),
    "t_meridian": Account(id="acc_meridian", name="Meridian"),
}

# Operator → tenant routing. Adopter's resolve_by_ref typically
# encapsulates this lookup; the test surfaces it explicitly so
# fixtures stay readable.
OPERATOR_TO_TENANT: dict[str, str] = {
    "pinnacle.example": "t_pinnacle",
    "meridian.example": "t_meridian",
}

# Auth principal → tenant. Driven by ctx.auth_info.principal in the
# helper's resolve_from_auth callback.
PRINCIPAL_TO_TENANT: dict[str, str] = {
    "buyer@pinnacle": "t_pinnacle",
    "buyer@meridian": "t_meridian",
}


def _account_tenant_id(account: Account) -> str:
    """Project an Account back to its owning tenant id.

    Mirrors the JS ``tenantId`` callback. In the test fixture the
    projection is the inverse of ACCOUNTS — adopters typically read
    a denormalized field on their own Account model.
    """
    for tid, acc in ACCOUNTS.items():
        if acc.id == account.id:
            return tid
    raise KeyError(f"Account has no known tenant: {account.id!r}")


def _resolve_by_ref(ref: AccountReference | dict[str, Any], ctx: ResolveContext) -> Account | None:
    """Adopter's ref → Account lookup. Reads the (brand, operator) arm.

    Returns ``None`` for unknown operators (helper emits
    ``ACCOUNT_NOT_FOUND`` for that row).
    """
    del ctx  # ref-based lookup ignores ctx in this fixture
    operator = ref.get("operator") if isinstance(ref, dict) else getattr(ref, "operator", None)
    if operator is None:
        return None
    tid = OPERATOR_TO_TENANT.get(operator)
    return ACCOUNTS.get(tid) if tid else None


def _resolve_from_auth(ctx: ResolveContext) -> str | None:
    """Adopter's auth → tenant_id lookup. Returns ``None`` for
    unregistered principals (helper rejects every entry with
    ``PERMISSION_DENIED``)."""
    if ctx.auth_info is None or not ctx.auth_info.principal:
        return None
    return PRINCIPAL_TO_TENANT.get(ctx.auth_info.principal)


def _tenant_to_account(tenant_id: str) -> Account | None:
    """Adopter's tenant_id → Account projection. Used for Path-2
    (no-ref) resolution and for ``list``."""
    return ACCOUNTS.get(tenant_id)


def _ctx(principal: str | None) -> ResolveContext:
    """Construct a ResolveContext with the given principal. ``None``
    principal models an unauthenticated request — the fail-closed
    case (every entry rejected with PERMISSION_DENIED)."""
    if principal is None:
        return ResolveContext(auth_info=None)
    return ResolveContext(auth_info=AuthInfo(kind="api_key", principal=principal))


def _auth(principal: str | None) -> AuthInfo | None:
    """Construct an AuthInfo with the given principal — mirrors the
    dispatcher's ``accounts.resolve(ref_dict, auth_info=auth_info)``
    call shape (the Protocol takes ``auth_info``, not ``ctx``)."""
    if principal is None:
        return None
    return AuthInfo(kind="api_key", principal=principal)


def _ref(operator: str, brand: str = "acme.example") -> dict[str, Any]:
    """Build an operator-arm AccountReference as a dict (the wire shape)."""
    return {"brand": {"domain": brand}, "operator": operator}


def _run(coro: Any) -> Any:
    """Run an awaitable in a fresh event loop (pytest-asyncio not
    required for the tenant store's narrow sync surface)."""
    if asyncio.iscoroutine(coro):
        return asyncio.new_event_loop().run_until_complete(coro)
    return coro


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_protocol_conformance(self) -> None:
        """``_TenantStore`` must satisfy the runtime-checkable
        :class:`AccountStore` Protocol — the dispatcher relies on
        ``isinstance(store, AccountStore)`` and calls
        ``accounts.resolve(ref_dict, auth_info=auth_info)`` as a
        keyword argument."""
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        assert isinstance(store, AccountStore)

    def test_resolve_called_with_auth_info_kwarg(self) -> None:
        """Mirrors the dispatcher call shape exactly — ``auth_info``
        as a keyword. This is the call that breaks if ``resolve``
        keeps the old ``ctx=`` signature."""
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        acc = _run(store.resolve(_ref("pinnacle.example"), auth_info=_auth("buyer@pinnacle")))
        assert acc is not None
        assert acc.id == "acc_pinnacle"

    def test_same_tenant_ref_returns_account(self) -> None:
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        acc = _run(store.resolve(_ref("pinnacle.example"), _auth("buyer@pinnacle")))
        assert acc is not None
        assert acc.id == "acc_pinnacle"

    def test_cross_tenant_ref_returns_none(self) -> None:
        """Pinnacle credential, Meridian operator on the wire — the
        gate hides the existence of the cross-tenant account."""
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        acc = _run(store.resolve(_ref("meridian.example"), _auth("buyer@pinnacle")))
        assert acc is None

    def test_unknown_ref_returns_none(self) -> None:
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        acc = _run(store.resolve(_ref("unknown.example"), _auth("buyer@pinnacle")))
        assert acc is None

    def test_auth_has_no_tenant_with_ref_returns_none(self) -> None:
        """Unregistered principal cannot resolve any ref — the auth
        tenant is None, the equality check fails."""
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        acc = _run(store.resolve(_ref("pinnacle.example"), _auth("not-registered")))
        assert acc is None

    def test_no_ref_path2_returns_auth_tenant_account(self) -> None:
        """Path 2: no ref on the wire, derive account from auth."""
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        acc = _run(store.resolve(None, _auth("buyer@pinnacle")))
        assert acc is not None
        assert acc.id == "acc_pinnacle"

    def test_no_ref_unauthenticated_returns_none(self) -> None:
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        acc = _run(store.resolve(None, _auth(None)))
        assert acc is None

    def test_resolve_by_ref_raises_returns_none(self) -> None:
        """Per-request log-and-deny: an exception in adopter
        ``resolve_by_ref`` must surface as ``None``, not propagate
        out and 500 the calling tool."""

        def raising_resolve_by_ref(
            ref: AccountReference | dict[str, Any], ctx: ResolveContext
        ) -> Account | None:
            del ref, ctx
            raise RuntimeError("simulated DB outage")

        store = create_tenant_store(
            resolve_by_ref=raising_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        acc = _run(store.resolve(_ref("pinnacle.example"), _auth("buyer@pinnacle")))
        assert acc is None


# ---------------------------------------------------------------------------
# upsert (sync_accounts) — per-entry tenant-isolation gate
# ---------------------------------------------------------------------------


class TestUpsert:
    @staticmethod
    def _build_with_recorder() -> tuple[Any, list[dict[str, Any]]]:
        """Construct a store whose upsert_row records each invocation.

        Asserting on ``writes`` lets tests verify cross-tenant entries
        NEVER reach adopter code — the gate filters them upstream.
        """
        writes: list[dict[str, Any]] = []

        def upsert_row(row: dict[str, Any], ctx: ResolveContext) -> SyncAccountsResultRow:
            del ctx
            writes.append(row)
            return SyncAccountsResultRow(
                brand=row["brand"],
                operator=row["operator"],
                action="created",
                status="active",
            )

        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
            upsert_row=upsert_row,
        )
        return store, writes

    def test_in_tenant_entry_passes_through(self) -> None:
        store, writes = self._build_with_recorder()
        rows = _run(store.upsert([_ref("pinnacle.example")], _ctx("buyer@pinnacle")))
        assert len(writes) == 1
        assert rows[0].action == "created"
        assert rows[0].status == "active"

    def test_cross_tenant_entry_rejected_before_adopter_code(self) -> None:
        store, writes = self._build_with_recorder()
        rows = _run(store.upsert([_ref("pinnacle.example")], _ctx("buyer@meridian")))
        assert writes == [], "upsert_row MUST NOT run for cross-tenant entries"
        assert rows[0].action == "failed"
        assert rows[0].status == "rejected"
        assert rows[0].errors is not None
        assert rows[0].errors[0]["code"] == "ACCOUNT_NOT_FOUND"
        assert rows[0].errors[0]["recovery"] == "terminal"

    def test_unknown_ref_rejected_with_account_not_found(self) -> None:
        """Unknown and unauthorized refs share the existence-hiding result."""
        store, writes = self._build_with_recorder()
        rows = _run(store.upsert([_ref("unknown.example")], _ctx("buyer@pinnacle")))
        assert writes == []
        assert rows[0].errors is not None
        assert rows[0].errors[0]["code"] == "ACCOUNT_NOT_FOUND"

    def test_fail_closed_no_auth_rejects_every_entry(self) -> None:
        """Unregistered principal — every entry fails with
        PERMISSION_DENIED regardless of operator."""
        store, writes = self._build_with_recorder()
        rows = _run(
            store.upsert(
                [_ref("pinnacle.example"), _ref("meridian.example")],
                _ctx("not-registered"),
            )
        )
        assert writes == []
        assert len(rows) == 2
        for row in rows:
            assert row.errors is not None
            assert row.errors[0]["code"] == "PERMISSION_DENIED"

    def test_fail_closed_unauthenticated_rejects_every_entry(self) -> None:
        """ctx.auth_info is None — same fail-closed behavior."""
        store, writes = self._build_with_recorder()
        rows = _run(store.upsert([_ref("pinnacle.example")], _ctx(None)))
        assert writes == []
        assert rows[0].errors is not None
        assert rows[0].errors[0]["code"] == "PERMISSION_DENIED"

    def test_mixed_batch_partitions_correctly(self) -> None:
        """Only the in-tenant entry reaches adopter code; both probes fail alike."""
        store, writes = self._build_with_recorder()
        rows = _run(
            store.upsert(
                [
                    _ref("pinnacle.example", "a.example"),  # pass
                    _ref("meridian.example", "b.example"),  # cross-tenant
                    _ref("unknown.example", "c.example"),  # unknown
                ],
                _ctx("buyer@pinnacle"),
            )
        )
        assert len(writes) == 1, "only the in-tenant entry should reach upsert_row"
        assert rows[0].action == "created"
        assert rows[1].errors is not None
        assert rows[1].errors[0]["code"] == "ACCOUNT_NOT_FOUND"
        assert rows[2].errors is not None
        assert rows[2].errors[0]["code"] == "ACCOUNT_NOT_FOUND"

    def test_resolve_by_ref_raises_isolates_to_single_entry(self) -> None:
        """One bad row must not poison the batch. When
        ``resolve_by_ref`` raises for one entry, that entry surfaces
        as PERMISSION_DENIED while sibling entries pass through."""
        writes: list[dict[str, Any]] = []

        def upsert_row(row: dict[str, Any], ctx: ResolveContext) -> SyncAccountsResultRow:
            del ctx
            writes.append(row)
            return SyncAccountsResultRow(
                brand=row["brand"],
                operator=row["operator"],
                action="created",
                status="active",
            )

        def flaky_resolve_by_ref(
            ref: AccountReference | dict[str, Any], ctx: ResolveContext
        ) -> Account | None:
            operator = (
                ref.get("operator") if isinstance(ref, dict) else getattr(ref, "operator", None)
            )
            if operator == "boom.example":
                raise RuntimeError("simulated adopter failure")
            return _resolve_by_ref(ref, ctx)

        store = create_tenant_store(
            resolve_by_ref=flaky_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
            upsert_row=upsert_row,
        )
        rows = _run(
            store.upsert(
                [
                    _ref("pinnacle.example"),
                    _ref("boom.example"),
                    _ref("pinnacle.example", "other.example"),
                ],
                _ctx("buyer@pinnacle"),
            )
        )
        assert len(rows) == 3
        assert rows[0].action == "created"
        assert rows[1].action == "failed"
        assert rows[1].errors is not None
        assert rows[1].errors[0]["code"] == "PERMISSION_DENIED"
        assert rows[2].action == "created"
        # Two passing entries reached upsert_row; the raising one was
        # filtered upstream — adopter exception detail did not leak.
        assert len(writes) == 2

    def test_tenant_id_raises_isolates_to_single_entry(self) -> None:
        """``tenant_id(account)`` raising for one entry must not abort
        the batch — same per-entry isolation as ``resolve_by_ref``."""
        writes: list[dict[str, Any]] = []

        def upsert_row(row: dict[str, Any], ctx: ResolveContext) -> SyncAccountsResultRow:
            del ctx
            writes.append(row)
            return SyncAccountsResultRow(
                brand=row["brand"],
                operator=row["operator"],
                action="created",
                status="active",
            )

        def flaky_tenant_id(account: Account) -> str:
            if account.id == "acc_meridian":
                raise RuntimeError("simulated tenant lookup failure")
            return _account_tenant_id(account)

        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=flaky_tenant_id,
            tenant_to_account=_tenant_to_account,
            upsert_row=upsert_row,
        )
        rows = _run(
            store.upsert(
                [_ref("pinnacle.example"), _ref("meridian.example")],
                _ctx("buyer@pinnacle"),
            )
        )
        assert len(rows) == 2
        # In-tenant entry passes (and reaches adopter code).
        assert rows[0].action == "created"
        # Cross-tenant ref where tenant_id raised — surfaces as
        # PERMISSION_DENIED (the raise is treated as "we cannot
        # confirm the entry's tenant" → fail-closed deny).
        assert rows[1].errors is not None
        assert rows[1].errors[0]["code"] == "PERMISSION_DENIED"
        assert len(writes) == 1

    def test_no_upsert_row_hook_is_noop(self) -> None:
        """Without an adopter upsert_row, authorized rows still
        receive a wire-shaped success result; the helper provides a
        sensible default rather than 501-ing the whole batch."""
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        rows = _run(store.upsert([_ref("pinnacle.example")], _ctx("buyer@pinnacle")))
        # With no adopter hook, authorized rows pass with action='unchanged'
        # (no-op acknowledgment). Cross-tenant still rejects.
        assert rows[0].action == "unchanged"
        assert rows[0].errors is None


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_returns_only_same_tenant_account(self) -> None:
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        accounts = _run(store.list(ctx=_ctx("buyer@pinnacle")))
        assert len(accounts) == 1
        assert accounts[0].id == "acc_pinnacle"

    def test_unregistered_principal_returns_empty(self) -> None:
        """Fail-closed but quiet: empty list, not raise."""
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        accounts = _run(store.list(ctx=_ctx("not-registered")))
        assert accounts == []

    def test_unauthenticated_returns_empty(self) -> None:
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        accounts = _run(store.list(ctx=_ctx(None)))
        assert accounts == []

    def test_tenant_to_account_raises_returns_empty(self) -> None:
        """``list`` MUST NOT raise on a per-spec valid request — an
        adopter ``tenant_to_account`` exception must surface as ``[]``,
        not propagate. Same outcome as auth-None (fail-closed quiet)."""

        def raising_tenant_to_account(tenant_id: str) -> Account | None:
            del tenant_id
            raise RuntimeError("simulated DB outage")

        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=raising_tenant_to_account,
        )
        accounts = _run(store.list(ctx=_ctx("buyer@pinnacle")))
        assert accounts == []


# ---------------------------------------------------------------------------
# sync_governance — same per-entry gate as upsert
# ---------------------------------------------------------------------------


class TestSyncGovernance:
    @staticmethod
    def _build_with_recorder() -> tuple[Any, list[SyncGovernanceEntry]]:
        writes: list[SyncGovernanceEntry] = []

        def sync_governance_row(
            entry: SyncGovernanceEntry, ctx: ResolveContext
        ) -> SyncGovernanceResultRow:
            del ctx
            writes.append(entry)
            return SyncGovernanceResultRow(
                account=entry.account,
                status="synced",
                governance_agents=[{"url": a["url"]} for a in entry.governance_agents],
            )

        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
            sync_governance_row=sync_governance_row,
        )
        return store, writes

    def test_in_tenant_entry_passes_through(self) -> None:
        store, writes = self._build_with_recorder()
        entries = [
            SyncGovernanceEntry(
                account=_ref("pinnacle.example"),
                governance_agents=[{"url": "https://gov.example"}],
            )
        ]
        rows = _run(store.sync_governance(entries, _ctx("buyer@pinnacle")))
        assert len(writes) == 1
        assert rows[0].status == "synced"

    def test_cross_tenant_entry_hidden_as_account_not_found(self) -> None:
        store, writes = self._build_with_recorder()
        entries = [
            SyncGovernanceEntry(
                account=_ref("pinnacle.example"),
                governance_agents=[],
            )
        ]
        rows = _run(store.sync_governance(entries, _ctx("buyer@meridian")))
        assert writes == []
        assert rows[0].status == "failed"
        assert rows[0].errors is not None
        assert rows[0].errors[0]["code"] == "ACCOUNT_NOT_FOUND"

    def test_unknown_and_cross_tenant_entries_are_indistinguishable(self) -> None:
        """A caller cannot enumerate valid operators from governance errors."""
        cross_store, cross_writes = self._build_with_recorder()
        unknown_store = create_tenant_store(
            resolve_by_ref=lambda ref, ctx: None,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        entries = [
            SyncGovernanceEntry(
                account=_ref("pinnacle.example"),
                governance_agents=[],
            )
        ]

        cross_rows = _run(cross_store.sync_governance(entries, _ctx("buyer@meridian")))
        unknown_rows = _run(unknown_store.sync_governance(entries, _ctx("buyer@meridian")))

        assert cross_writes == []
        assert cross_rows[0].errors == unknown_rows[0].errors
        assert cross_rows[0].errors == [
            {
                "code": "ACCOUNT_NOT_FOUND",
                "message": "Unknown operator: pinnacle.example",
                "recovery": "terminal",
            }
        ]

    def test_fail_closed_no_auth_rejects_every_entry(self) -> None:
        store, writes = self._build_with_recorder()
        entries = [
            SyncGovernanceEntry(
                account=_ref("pinnacle.example"),
                governance_agents=[],
            ),
            SyncGovernanceEntry(
                account=_ref("meridian.example"),
                governance_agents=[],
            ),
        ]
        rows = _run(store.sync_governance(entries, _ctx("not-registered")))
        assert writes == []
        assert len(rows) == 2
        for row in rows:
            assert row.status == "failed"
            assert row.errors is not None
            assert row.errors[0]["code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Immutability — adopters cannot monkey-patch the gate methods
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_cannot_reassign_upsert(self) -> None:
        """The Python equivalent of JS's ``Object.defineProperty(...
        writable: false)``: gate methods live on the class. Instance
        attribute assignment fails because the class uses ``__slots__``
        — no per-instance ``__dict__``."""
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        with pytest.raises(AttributeError):
            store.upsert = lambda *_a, **_kw: []  # type: ignore[method-assign]

    def test_cannot_reassign_sync_governance(self) -> None:
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        with pytest.raises(AttributeError):
            store.sync_governance = lambda *_a, **_kw: []  # type: ignore[method-assign]

    def test_cannot_reassign_list(self) -> None:
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        with pytest.raises(AttributeError):
            store.list = lambda *_a, **_kw: []  # type: ignore[method-assign]

    def test_cannot_reassign_resolve(self) -> None:
        """Even ``resolve`` is locked — its tenant gate runs on
        Path-1 too, and a swapped resolve would bypass it."""
        store = create_tenant_store(
            resolve_by_ref=_resolve_by_ref,
            resolve_from_auth=_resolve_from_auth,
            tenant_id=_account_tenant_id,
            tenant_to_account=_tenant_to_account,
        )
        with pytest.raises(AttributeError):
            store.resolve = lambda *_a, **_kw: None  # type: ignore[method-assign]
