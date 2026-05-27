"""Account-mode primitives for sandbox-authority enforcement of
``comply_test_controller`` and other test-only surfaces.

The hard rule: under no circumstances should the comply test controller
(or any test-only surface) operate on a ``live``-mode account. The flag
must live on the resolved account, not on a process-level env var —
env vars are operator-error-prone proxies for what is fundamentally an
authority decision per principal.

Mirrors the JS-side ``src/lib/server/account-mode.ts`` for cross-language
parity. Phase 1 of the lifecycle-state-and-sandbox-authority proposal
ships the field, helpers, and dispatch gate; Phase 2 ships
:func:`get_mock_upstream_url` and :meth:`DecisioningPlatform.upstream_for`
so mock-mode accounts route HTTP requests at a per-tenant fixture URL
without touching adapter business logic.

See ``docs/proposals/lifecycle-state-and-sandbox-authority.md`` for the
full three-mode design.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from adcp.decisioning.types import Account, AdcpError

#: Three operationally distinct account modes:
#:
#:  - ``live``: production traffic. Adopter's upstream is truth.
#:    Test-only surfaces (comply controller, force_*, simulate_*) are denied.
#:  - ``sandbox``: adopter's own test account. Their code path runs against
#:    their test infrastructure. Test-only surfaces are allowed.
#:  - ``mock``: SDK-routed-to-mock-server. Adopter's code is bypassed; the
#:    SDK forwards to the mock upstream backend (Phase 2). Test-only
#:    surfaces are allowed.
#:
#: Default when unspecified: ``live``. A missing or unknown ``mode`` reads
#: as production, fail-closed for any test-only dispatch.
AccountMode = Literal["live", "sandbox", "mock"]


def get_account_mode(account: Any) -> AccountMode:
    """Read ``mode`` off any account-shaped value, with back-compat for
    the legacy ``sandbox: bool`` field.

    Returns the explicit mode if present; otherwise infers ``'sandbox'``
    from ``sandbox is True``; otherwise ``'live'``.

    Adopters that have not yet migrated to the ``mode`` field continue to
    work — ``account.sandbox is True`` reads as sandbox mode through this
    helper. New code should prefer ``mode`` directly.

    Unknown / unrecognized ``mode`` values fall through to ``'live'`` —
    we never silently admit on a misspelled mode string.
    """
    if account is None:
        return "live"
    mode = _attr(account, "mode")
    if mode == "live" or mode == "sandbox" or mode == "mock":
        return cast(AccountMode, mode)
    # Back-compat: legacy `sandbox: True` flag reads as `sandbox` mode.
    if _attr(account, "sandbox") is True:
        return "sandbox"
    return "live"


def is_sandbox_or_mock_account(account: Any) -> bool:
    """Predicate: is the account in a non-production mode that admits
    test-only surfaces (comply controller, force_*, simulate_*)?

    Returns ``True`` for ``mode in {'sandbox', 'mock'}`` (or legacy
    ``sandbox is True``); ``False`` for ``mode == 'live'`` or any account
    shape that doesn't carry the field.
    """
    mode = get_account_mode(account)
    return mode == "sandbox" or mode == "mock"


def assert_sandbox_account(
    account: Any,
    *,
    tool: str | None = None,
    message: str | None = None,
) -> None:
    """Raise ``AdcpError('PERMISSION_DENIED')`` if the account is not in
    a non-production mode. Use to gate dispatch of test-only surfaces.

    Fail-closed semantics:

    - ``account is None`` (no resolved account): raises.
    - ``mode == 'live'`` or unspecified + no ``sandbox is True``: raises.
    - ``mode in {'sandbox', 'mock'}`` (or legacy ``sandbox is True``):
      no-op, dispatch proceeds.

    The ``details`` payload carries ``{scope: 'sandbox-gate', tool?}``
    so dashboards can distinguish gate-rejections from other permission
    denials.

    **Resolver discipline.** The strength of this gate depends entirely
    on how the adopter's :meth:`AccountStore.resolve` constructs its
    return value. Resolvers MUST NOT spread untrusted input (request
    body, headers, ``ctx_metadata``, query params) into the resolved
    account — doing so lets a buyer self-promote to ``mode='sandbox'``
    and unlock test-only surfaces on a live principal. Source ``mode``
    (and ``sandbox``) from a trusted store keyed by the authenticated
    principal; never from request data.

    **opts.message must be a static string literal.** The message is
    echoed on the wire inside the error envelope. Interpolating
    user-controlled values into it creates a reflection sink (PII
    leakage, log injection, downstream HTML rendering). Pick from a
    fixed set of messages keyed by ``tool`` if you need variants.
    """
    if is_sandbox_or_mock_account(account):
        return
    details: dict[str, Any] = {
        "scope": "sandbox-gate",
        "reason": "sandbox-or-mock-required",
    }
    if tool is not None:
        details["tool"] = tool
    raise AdcpError(
        "PERMISSION_DENIED",
        message=message
        or (
            "Test-only surface requires a sandbox or mock account; "
            "resolved account is in live mode."
        ),
        recovery="terminal",
        details=details,
    )


def get_mock_upstream_url(account: Account[Any] | None) -> str | None:
    """Read ``account.metadata['mock_upstream_url']`` safely.

    Adopters populate this in :meth:`AccountStore.resolve` for
    ``mode='mock'`` accounts so the framework's
    :meth:`DecisioningPlatform.upstream_for` knows which mock-server
    fixture URL to point the adapter's :class:`UpstreamHttpClient` at.
    The mock-server is per-specialism (``bin/adcp.js mock-server
    <specialism>``); adopters or CI start it and supply the URL on the
    account.

    Returns ``None`` when:

    - ``account.metadata`` is not a :class:`Mapping`.
    - ``mock_upstream_url`` is absent.
    - ``mock_upstream_url`` is empty / falsy / not a string.

    The framework treats any of these as "no mock URL declared" and
    fails closed at :meth:`DecisioningPlatform.upstream_for` rather
    than silently routing to a live URL.
    """
    if account is None:
        return None
    metadata = getattr(account, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    url = metadata.get("mock_upstream_url")
    if not isinstance(url, str) or not url:
        return None
    return url


def _attr(obj: Any, name: str) -> Any:
    """Own-attribute read mirroring the JS-side ``Object.hasOwn`` posture.

    Python doesn't have prototype-pollution the same way JS does, but
    the equivalent risk is class-level attribute inheritance: an
    adopter who defines ``Account`` subclasses and accidentally sets a
    class-level ``mode = 'sandbox'`` would have every instance read as
    sandbox. We only consult instance state (``__dict__``) for known
    attribute carriers; for dataclass instances the field lives there.
    Falls back to ``getattr`` for dict-shaped accounts.

    Returns ``None`` when the attribute is absent — the caller treats
    that as "field not present" and falls through to the next check.
    """
    if isinstance(obj, dict):
        return obj.get(name)
    inst = getattr(obj, "__dict__", None)
    if isinstance(inst, dict) and name in inst:
        return inst[name]
    # Fallback: bare attribute read for objects without a writable
    # ``__dict__`` (slots, namedtuple, custom descriptors).
    return getattr(obj, name, None)
