"""Unit tests for adcp.decisioning core types.

Covers:

* :class:`TaskHandoff` type-identity dispatch (rejects subclasses)
* :class:`AdcpError` wire projection
* :class:`Account` generic shape + auth_info threading
* :class:`SingletonAccounts` per-principal idempotency scoping (the
  buyer-to-buyer leak regression)
* :class:`ExplicitAccounts` and :class:`FromAuthAccounts` resolver shapes
* :class:`AccountStore` Protocol structural matching
* :class:`DecisioningPlatform` subclass attribute contract
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.decisioning import (
    Account,
    AccountStore,
    AdcpError,
    AuthInfo,
    DecisioningCapabilities,
    DecisioningPlatform,
    ExplicitAccounts,
    FromAuthAccounts,
    SingletonAccounts,
    TaskHandoff,
)
from adcp.decisioning.types import is_task_handoff

# ---- TaskHandoff ----


def test_task_handoff_type_identity() -> None:
    """``type(obj) is TaskHandoff`` must be the dispatch check —
    ``isinstance`` would let adopter subclasses trigger the handoff
    path silently."""

    def fn(_ctx: Any) -> str:
        return "done"

    h = TaskHandoff(fn)
    assert type(h) is TaskHandoff
    assert is_task_handoff(h) is True
    # A plain dict is never a handoff.
    assert is_task_handoff({"status": "submitted"}) is False
    # A buyer-supplied request body cannot reach this type.
    assert is_task_handoff(None) is False


def test_task_handoff_subclass_rejected_at_dispatch() -> None:
    """Adopter subclasses of TaskHandoff are NOT recognized as handoffs.
    Documented as a deliberate non-feature — subclassing is unsupported
    and silently produces the sync-return path."""

    class AdopterSubclass(TaskHandoff[str]):
        pass

    sub = AdopterSubclass(lambda _ctx: "done")
    assert type(sub) is AdopterSubclass
    assert is_task_handoff(sub) is False, (
        "Adopter subclass of TaskHandoff was treated as a handoff at "
        "dispatch — type-identity check is broken; the framework would "
        "now dispatch adopter-subclass instances through the handoff "
        "path, which is not the documented contract"
    )


def test_task_handoff_repr_does_not_leak_fn() -> None:
    """``__repr__`` returns a sealed marker so a debug helper or error
    traceback can't auto-render the closure body."""

    def fn(_ctx: Any) -> str:
        return "secret"

    h = TaskHandoff(fn)
    assert repr(h) == "TaskHandoff(<sealed>)"
    assert "secret" not in repr(h)


# ---- AdcpError ----


def test_adcp_error_wire_projection() -> None:
    """``to_wire()`` produces the AdCP structured-error envelope with
    only the fields that were populated. Optional fields stay omitted."""
    err = AdcpError(
        "BUDGET_TOO_LOW",
        message="total_budget below floor (0.50 CPM × 1000 imp)",
        recovery="correctable",
        field="total_budget",
        suggestion="Increase budget to at least $0.50",
    )
    assert err.to_wire() == {
        "code": "BUDGET_TOO_LOW",
        "message": "total_budget below floor (0.50 CPM × 1000 imp)",
        "recovery": "correctable",
        "field": "total_budget",
        "suggestion": "Increase budget to at least $0.50",
    }


def test_adcp_error_minimum_fields() -> None:
    """Code-only error projects to the minimum envelope. ``recovery``
    defaults to ``'terminal'`` (do-not-retry)."""
    err = AdcpError("INVALID_REQUEST")
    assert err.to_wire() == {
        "code": "INVALID_REQUEST",
        "message": "INVALID_REQUEST",
        "recovery": "terminal",
    }


def test_adcp_error_str_includes_code_and_recovery() -> None:
    """Default ``__str__`` surfaces ``code`` + ``recovery`` so log
    lines and error tracebacks carry both at a glance."""
    err = AdcpError("BUDGET_TOO_LOW", message="too low", recovery="correctable")
    assert str(err) == "AdcpError[BUDGET_TOO_LOW / correctable]: too low"


def test_adcp_error_with_details() -> None:
    """Multi-error preflight: ``details={'errors': [...]}`` survives the
    wire projection so buyers can read every rejected field at once."""
    err = AdcpError(
        "INVALID_REQUEST",
        message="multiple validation failures",
        recovery="correctable",
        details={
            "errors": [
                {"code": "BUDGET_TOO_LOW", "field": "total_budget"},
                {"code": "INVALID_REQUEST", "field": "package[0].targeting"},
            ]
        },
    )
    wire = err.to_wire()
    assert "details" in wire
    assert wire["details"]["errors"][0]["code"] == "BUDGET_TOO_LOW"


# ---- Account ----


def test_account_default_metadata_is_empty_dict() -> None:
    """Adopters who don't define typed metadata get an empty dict —
    no ``cast`` required to construct."""
    acct = Account(id="acme_42")
    assert acct.id == "acme_42"
    assert acct.metadata == {}
    assert acct.status == "active"


# ---- SingletonAccounts (the buyer-to-buyer leak regression) ----


def test_singleton_per_principal_scoping() -> None:
    """The buyer-to-buyer cache-leak regression: SingletonAccounts MUST
    synthesize per-principal IDs so two distinct buyers don't share an
    idempotency cache. Without per-principal synthesis, buyer A's
    ``response_payload`` would surface to buyer B on UUID collision —
    a confidentiality leak."""
    sa = SingletonAccounts(account_id="training-agent")
    a = sa.resolve(None, AuthInfo(kind="signed_request", principal="buyer-a"))
    b = sa.resolve(None, AuthInfo(kind="signed_request", principal="buyer-b"))
    assert a.id == "training-agent:buyer-a"
    assert b.id == "training-agent:buyer-b"
    assert a.id != b.id


def test_singleton_anonymous_fallback() -> None:
    """Unauthenticated dev/CI fixtures get ``:anonymous`` so the
    resolver doesn't fail closed in test environments. Production
    deployments with auth never hit this branch."""
    sa = SingletonAccounts(account_id="dev")
    acct = sa.resolve(None, None)
    assert acct.id == "dev:anonymous"


def test_singleton_threads_auth_info() -> None:
    """``Account.auth_info`` carries the verified principal info so
    platform methods can read scopes / key_id without re-parsing
    transport headers."""
    sa = SingletonAccounts(account_id="hello")
    auth = AuthInfo(
        kind="signed_request",
        key_id="kid-1",
        principal="buyer-a",
        scopes=["read", "write"],
    )
    acct = sa.resolve(None, auth)
    assert acct.auth_info == {
        "kind": "signed_request",
        "key_id": "kid-1",
        "principal": "buyer-a",
        "scopes": ["read", "write"],
    }


def test_singleton_rejects_empty_account_id() -> None:
    """``account_id`` must be a non-empty string — fail-fast at
    construction beats fail-mysteriously at first request."""
    with pytest.raises(ValueError, match="non-empty account_id"):
        SingletonAccounts(account_id="")


# ---- ExplicitAccounts ----


def test_explicit_accounts_resolves_via_loader() -> None:
    """``ExplicitAccounts`` reads ``ref['account_id']`` and routes
    through the adopter's loader."""
    loaded: list[str] = []

    def loader(account_id: str) -> Account[Any]:
        loaded.append(account_id)
        return Account(id=account_id, name=f"Acme {account_id}")

    store = ExplicitAccounts(loader=loader)
    acct = store.resolve({"account_id": "acme_42"})
    assert isinstance(acct, Account)
    assert acct.id == "acme_42"
    assert loaded == ["acme_42"]


def test_explicit_accounts_missing_ref_raises() -> None:
    """Missing/empty ``ref`` produces ``ACCOUNT_NOT_FOUND`` with the
    field path set to ``account.account_id`` so buyers know where the
    ref should go."""

    def loader(_account_id: str) -> Account[Any]:
        raise AssertionError("loader should not be called on missing ref")

    store = ExplicitAccounts(loader=loader)
    with pytest.raises(AdcpError) as exc_info:
        store.resolve(None)
    assert exc_info.value.code == "ACCOUNT_NOT_FOUND"
    assert exc_info.value.field == "account.account_id"
    assert exc_info.value.recovery == "terminal"


# ---- FromAuthAccounts ----


def test_from_auth_resolves_via_principal() -> None:
    """``FromAuthAccounts`` reads ``auth_info.principal`` and ignores
    the wire ref. The auth principal IS the account holder."""

    def loader(principal: str) -> Account[Any]:
        return Account(id=f"acct_for_{principal}")

    store = FromAuthAccounts(loader=loader)
    acct = store.resolve(ref=None, auth_info=AuthInfo(kind="bearer", principal="buyer-a"))
    assert isinstance(acct, Account)
    assert acct.id == "acct_for_buyer-a"


def test_from_auth_missing_principal_raises() -> None:
    """``FromAuthAccounts`` without ``auth_info`` raises
    ``AUTH_INVALID`` — the resolver can't synthesize an account from
    nothing."""

    def loader(_principal: str) -> Account[Any]:
        raise AssertionError("loader should not be called without auth")

    store = FromAuthAccounts(loader=loader)
    with pytest.raises(AdcpError) as exc_info:
        store.resolve(None, None)
    assert exc_info.value.code == "AUTH_INVALID"


# ---- AccountStore Protocol structural matching ----


def test_account_store_protocol_runtime_checkable() -> None:
    """All three reference impls satisfy the Protocol structurally
    (they have ``resolution: str`` and ``resolve(ref, auth_info)``).
    Adopters writing custom stores get the same structural check."""
    assert isinstance(SingletonAccounts(account_id="x"), AccountStore)
    assert isinstance(ExplicitAccounts(loader=lambda _x: Account(id="y")), AccountStore)
    assert isinstance(FromAuthAccounts(loader=lambda _x: Account(id="z")), AccountStore)


def test_account_store_resolution_literal() -> None:
    """``resolution`` is a structural literal the framework reads at
    server boot for ``validate_platform`` checks."""
    assert SingletonAccounts(account_id="x").resolution == "singleton"
    assert ExplicitAccounts(loader=lambda _x: Account(id="y")).resolution == "explicit"
    assert FromAuthAccounts(loader=lambda _x: Account(id="z")).resolution == "from_auth"


# ---- DecisioningPlatform contract ----


def test_decisioning_platform_subclass_attributes() -> None:
    """A subclass declares ``capabilities`` + ``accounts``. The base
    leaves them unset (None) so ``validate_platform`` at server boot
    can fail-fast on platforms that forgot."""

    class HelloSeller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="hello")

    s = HelloSeller()
    assert s.capabilities.specialisms == ["sales-non-guaranteed"]
    assert s.accounts.resolution == "singleton"
