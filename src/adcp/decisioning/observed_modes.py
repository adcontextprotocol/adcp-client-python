"""Process-scoped tracker of explicit ``Account.mode`` values returned
from :meth:`AccountStore.resolve` during framework-side comply-controller
dispatch.

Used by the sandbox gate's deprecated env-fallback path (``ADCP_SANDBOX=1``)
to fail closed when a process has resolved any explicit live-mode account.

Rationale: the env-fallback exists for back-compat with adopters that
have not yet adopted the per-account ``mode`` field. If those adopters
have ALSO begun returning explicit ``mode='live'`` from their resolver,
the env var is a misconfiguration — leaving it set would re-open the
gate for live principals after the resolver was meant to close it.

Implicit-default ``live`` (resolver returns no mode field, so the
dataclass default ``'live'`` applies) is NOT observed here — those
adopters are exactly who the env-fallback bridge exists for. Only the
deliberate explicit mode (resolver populated ``mode='...'`` and stamped
``_mode_explicit=True``) trips the guard.

Mirrors the JS-side
``src/lib/server/decisioning/runtime/observed-modes.ts``.
"""

from __future__ import annotations

import os
from typing import Any

from adcp.decisioning.account_mode import AccountMode

_observed: set[AccountMode] = set()


def record_resolved_account_mode(account: Any) -> None:
    """Record an account returned from :meth:`AccountStore.resolve`.

    Only counts EXPLICIT mode values — those where the resolver
    deliberately populated ``mode`` (signaled via the
    ``_mode_explicit=True`` flag on the framework's :class:`Account`
    dataclass). Adopters whose resolvers don't yet stamp ``mode`` keep
    working with the env-fallback bridge: their accounts read as
    implicit live, which doesn't trip this guard.

    No-op when ``account`` is ``None`` / not an account-shaped value /
    lacks an explicit mode flag / mode is not a known string. The set
    only collects deliberate, resolver-stamped mode values.
    """
    if account is None:
        return
    # Only count explicit modes — implicit-default live (legacy adopter
    # whose resolver didn't populate mode) is the back-compat target the
    # env-fallback exists for, not a misconfiguration.
    if not getattr(account, "_mode_explicit", False):
        return
    mode = getattr(account, "mode", None)
    if mode in ("live", "sandbox", "mock"):
        _observed.add(mode)


def has_observed_live_mode() -> bool:
    """``True`` when this process has observed at least one explicit
    ``mode='live'`` account from :meth:`AccountStore.resolve`.

    The sandbox-gate env-fallback consults this to decide whether
    ``ADCP_SANDBOX=1`` is a safe legacy bridge or a misconfiguration.
    """
    return "live" in _observed


def _reset_observed_account_modes() -> None:
    """Test seam — reset the observed-modes set between test cases.

    Refuses to clear when ``ADCP_ENV`` is anything that looks like
    production. The observed-modes set is intentionally process-scoped
    — clearing it in production would re-arm the env-fallback admit
    path for live principals already seen, defeating the whole point
    of the fail-closed guard.

    Not part of the public API. Adopter code MUST NOT call this.
    """
    env = os.environ.get("ADCP_ENV", "").strip().lower()
    if env in {"prod", "production"}:
        raise RuntimeError(
            f"_reset_observed_account_modes is a test seam; refusing to clear "
            f"observed account modes in ADCP_ENV={env!r}. The set is process-"
            f"scoped to keep the comply controller's env-fallback fail-closed "
            f"guard armed once a live account has been seen."
        )
    _observed.clear()
