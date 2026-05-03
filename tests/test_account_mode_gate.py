"""Phase 1 sandbox-authority gate for ``comply_test_controller``.

Port of JS PR #1453 (adcontextprotocol/adcp-client). The SDK enforces
that ``comply_test_controller`` only operates on accounts whose resolved
``mode`` is ``'sandbox'`` or ``'mock'``. Trust boundary is the resolved
account, NOT the wire — buyer-supplied ``account.sandbox: true`` is
ignored when the resolver returned a live account.

Test matrix:

- Resolver path (5): live denies; sandbox admits; mock admits; legacy
  ``sandbox=True`` admits; spoofed wire flag ignored on resolved live.
- Context.sandbox path (3): unresolved + ``context.sandbox`` admits;
  unresolved without it denies; resolved-as-live ignores context override.
- Env fallback (4): legacy admit; observed-live throws loudly; post-
  observation throws on env-only; ``list_scenarios`` exempt on live.

See ``docs/proposals/lifecycle-state-and-sandbox-authority.md``.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.decisioning.observed_modes import (
    _reset_observed_account_modes,
    has_observed_live_mode,
    record_resolved_account_mode,
)
from adcp.decisioning.types import Account
from adcp.server.test_controller import (
    TestControllerStore,
    _handle_test_controller,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _StubStore(TestControllerStore):
    """Minimal store that admits every scenario the gate lets through.

    Methods return a synthetic success payload — the gate's job is to
    short-circuit BEFORE the store runs, so we just need a no-op
    method body for any scenario the test fires.
    """

    async def force_account_status(
        self,
        account_id: str,
        status: str,
        **_: Any,
    ) -> dict[str, Any]:
        return {"previous_state": "active", "current_state": status}


@pytest.fixture(autouse=True)
def _isolate_observed_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with an empty observed-modes set + clean env."""
    monkeypatch.delenv("ADCP_SANDBOX", raising=False)
    monkeypatch.delenv("ADCP_ENV", raising=False)
    _reset_observed_account_modes()


def _store() -> _StubStore:
    return _StubStore()


def _force_status_params() -> dict[str, Any]:
    return {
        "scenario": "force_account_status",
        "params": {"account_id": "acc_1", "status": "disabled"},
    }


# ---------------------------------------------------------------------------
# Resolver path (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_live_denied() -> None:
    """Resolved ``mode='live'`` denies the controller."""

    def resolve(_ref: dict[str, Any] | None) -> Account:
        return Account(id="acc_1", mode="live", _mode_explicit=True)

    result = await _handle_test_controller(
        _store(),
        _force_status_params(),
        account_resolver=resolve,
    )
    assert result["success"] is False
    assert result["error"] == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_resolver_sandbox_admits() -> None:
    """Resolved ``mode='sandbox'`` admits the controller."""

    def resolve(_ref: dict[str, Any] | None) -> Account:
        return Account(id="acc_1", mode="sandbox", _mode_explicit=True)

    result = await _handle_test_controller(
        _store(),
        _force_status_params(),
        account_resolver=resolve,
    )
    assert result["success"] is True
    assert result["current_state"] == "disabled"


@pytest.mark.asyncio
async def test_resolver_mock_admits() -> None:
    """Resolved ``mode='mock'`` admits the controller."""

    def resolve(_ref: dict[str, Any] | None) -> Account:
        return Account(id="acc_1", mode="mock", _mode_explicit=True)

    result = await _handle_test_controller(
        _store(),
        _force_status_params(),
        account_resolver=resolve,
    )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_resolver_legacy_sandbox_flag_admits() -> None:
    """Account carrying legacy ``sandbox=True`` (no explicit mode) admits."""

    class _LegacyAccount:
        # Legacy adopter shape — pre-mode field. Carries ``sandbox`` only.
        id = "acc_1"
        sandbox = True
        mode = None  # tolerated; helper falls through to sandbox flag

    def resolve(_ref: dict[str, Any] | None) -> Any:
        return _LegacyAccount()

    result = await _handle_test_controller(
        _store(),
        _force_status_params(),
        account_resolver=resolve,
    )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_spoofed_wire_sandbox_ignored_when_resolver_returns_live() -> None:
    """Trust boundary: buyer-supplied ``account.sandbox: true`` MUST NOT
    override a resolved live account.

    The wire flag is the load-bearing security control — without
    resolver-trumps-wire, a buyer could self-promote any live account
    by stamping the flag. JS PR #1453 §"Spoofed wire flag" tests this
    invariant.
    """

    def resolve(_ref: dict[str, Any] | None) -> Account:
        return Account(id="acc_1", mode="live", _mode_explicit=True)

    params = _force_status_params()
    params["account"] = {"account_id": "acc_1", "sandbox": True}

    result = await _handle_test_controller(
        _store(),
        params,
        account_resolver=resolve,
    )
    assert result["success"] is False
    assert result["error"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Context.sandbox path (3 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_account_with_context_sandbox_admits() -> None:
    """When no account resolves AND ``context.sandbox=True``, admit.

    Capability-probe / conformance bootstrap path: requests that don't
    carry an account ref at all but signal sandbox intent through
    ``context.sandbox``. Mirrors today's ``isSandboxRequest`` semantics.
    """

    def resolve(_ref: dict[str, Any] | None) -> Account:
        raise RuntimeError("no account resolves for this request")

    params = _force_status_params()
    params["context"] = {"sandbox": True}

    result = await _handle_test_controller(
        _store(),
        params,
        account_resolver=resolve,
    )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_unresolved_account_without_context_sandbox_denied() -> None:
    """When no account resolves AND no sandbox signal, refuse."""

    def resolve(_ref: dict[str, Any] | None) -> Account:
        raise RuntimeError("no account resolves for this request")

    result = await _handle_test_controller(
        _store(),
        _force_status_params(),
        account_resolver=resolve,
    )
    assert result["success"] is False
    assert result["error"] == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_resolved_live_with_context_sandbox_denied() -> None:
    """``context.sandbox=True`` MUST NOT override a resolved live account.

    Same trust-boundary invariant as the wire-ref test: any buyer-
    supplied signal is ignored once the resolver names the account.
    """

    def resolve(_ref: dict[str, Any] | None) -> Account:
        return Account(id="acc_1", mode="live", _mode_explicit=True)

    params = _force_status_params()
    params["context"] = {"sandbox": True}

    result = await _handle_test_controller(
        _store(),
        params,
        account_resolver=resolve,
    )
    assert result["success"] is False
    assert result["error"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Env fallback (4 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_fallback_legacy_admit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ADCP_SANDBOX=1`` admits when no explicit live mode has been
    observed — the back-compat bridge for adopters who haven't migrated
    to ``mode``.
    """
    monkeypatch.setenv("ADCP_SANDBOX", "1")

    # No resolver wired — pure env fallback.
    result = await _handle_test_controller(
        _store(),
        _force_status_params(),
        account_resolver=None,
    )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_env_fallback_throws_on_observed_live_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ADCP_SANDBOX=1`` + observed explicit live = misconfig — refuse
    loudly.

    Operator-visible failure: silent denial would let the misconfig
    persist; a runtime error surfaces it on first comply call.
    """
    monkeypatch.setenv("ADCP_SANDBOX", "1")

    # Pre-stamp an explicit live observation (simulates an earlier
    # request having run through the dispatch resolve seam).
    record_resolved_account_mode(Account(id="acc_live", mode="live", _mode_explicit=True))
    assert has_observed_live_mode() is True

    with pytest.raises(RuntimeError, match=r"ADCP_SANDBOX=1.*live-mode account"):
        await _handle_test_controller(
            _store(),
            _force_status_params(),
            account_resolver=None,
        )


@pytest.mark.asyncio
async def test_env_only_path_with_observed_live_throws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-observation guard: env-only admit path on a request that
    DID resolve an account (sandbox-flag absent) but the process has
    seen an explicit live elsewhere.

    A resolver that returns ``mode='live'`` on the same call will hit
    the resolver-deny path before this guard fires. The guard is
    specifically for the env-only admission path.
    """
    monkeypatch.setenv("ADCP_SANDBOX", "1")

    # Seed observed-live by simulating a prior resolution.
    record_resolved_account_mode(Account(id="other", mode="live", _mode_explicit=True))

    # This call has no resolver and no wire-ref / context.sandbox —
    # would admit only via the env path. The guard trips.
    with pytest.raises(RuntimeError, match=r"ADCP_SANDBOX=1"):
        await _handle_test_controller(
            _store(),
            _force_status_params(),
            account_resolver=None,
        )


@pytest.mark.asyncio
async def test_list_scenarios_exempt_even_on_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_scenarios`` is a capability probe — exempt from the gate.

    Buyers need to discover what scenarios a server implements
    regardless of account mode; the probe doesn't mutate state.
    Mirrors JS PR #1453's exempt-list-scenarios behavior.
    """

    def resolve(_ref: dict[str, Any] | None) -> Account:
        return Account(id="acc_1", mode="live", _mode_explicit=True)

    result = await _handle_test_controller(
        _store(),
        {"scenario": "list_scenarios"},
        account_resolver=resolve,
    )
    assert result["success"] is True
    assert "scenarios" in result


# ---------------------------------------------------------------------------
# observed_modes unit test — explicit-vs-implicit distinction
# ---------------------------------------------------------------------------


def test_observed_modes_tracks_explicit_only() -> None:
    """Implicit-default live (resolver didn't populate mode) does NOT
    trip the observed-live guard. Only deliberate explicit mode values
    are recorded.
    """
    _reset_observed_account_modes()

    # Implicit default: resolver returns Account without setting mode.
    record_resolved_account_mode(Account(id="x"))
    assert has_observed_live_mode() is False

    # Explicit live: resolver deliberately stamps mode='live'.
    record_resolved_account_mode(Account(id="y", mode="live", _mode_explicit=True))
    assert has_observed_live_mode() is True


def test_observed_modes_distinguishes_explicit_sandbox() -> None:
    """Explicit non-live modes are also recorded but don't trip the
    live guard."""
    _reset_observed_account_modes()

    record_resolved_account_mode(Account(id="x", mode="sandbox", _mode_explicit=True))
    record_resolved_account_mode(Account(id="y", mode="mock", _mode_explicit=True))
    assert has_observed_live_mode() is False


def test_observed_modes_reset_refuses_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test seam refuses to clear when ``ADCP_ENV=production``."""
    monkeypatch.setenv("ADCP_ENV", "production")
    with pytest.raises(RuntimeError, match=r"refusing to clear"):
        _reset_observed_account_modes()


# ---------------------------------------------------------------------------
# Account.sandbox derived property — back-compat
# ---------------------------------------------------------------------------


def test_account_sandbox_property_derives_from_mode() -> None:
    """The ``sandbox`` derived property reads from ``mode`` for back-compat."""
    assert Account(id="x").sandbox is False
    assert Account(id="x", mode="sandbox").sandbox is True
    assert Account(id="x", mode="mock").sandbox is True
    assert Account(id="x", mode="live").sandbox is False
