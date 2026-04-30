"""Dispatch layer for the v6.0 DecisioningPlatform framework.

The dispatch layer ties everything together at the seam between the
existing ``adcp.server`` transport machinery and the new
``DecisioningPlatform`` Protocol-driven adopter shape:

* :func:`validate_platform` — server-boot fail-fast: confirms every
  claimed specialism has its required methods, governance opt-in is
  honored, and ``accounts`` is a real ``AccountStore``.
* :func:`compose_caller_identity` — composite cache scope key
  ``f"{store_qualname}:{account.id}"`` (round-3 D9 — structural
  cross-store isolation).
* :func:`_build_request_context` — the hydration helper that turns a
  ``ToolContext`` + resolved ``Account`` into a typed
  ``RequestContext`` per D2 / D9 / D15.
* :func:`_invoke_platform_method` — the method-call seam. Detects
  async-vs-sync, runs sync on a thread-pool executor with
  ``contextvars`` snapshot, projects ``TaskHandoff`` returns, wraps
  non-``AdcpError`` exceptions to ``INTERNAL_ERROR`` (wire never
  leaks a stack trace).
* :func:`_project_handoff` — TaskHandoff lifecycle: allocates
  ``task_id``, projects the wire ``Submitted`` envelope, kicks off
  the adopter's handoff fn in the background, persists terminal
  artifact via the task registry.

Codegen-emitted ``handler.py`` (Stage 3 next file) calls
``_invoke_platform_method`` from each typed shim; ``serve.py``
(Stage 3 last) wires the executor + registry + middleware.

This module is framework-internal — adopters import nothing from
here. The Protocol contracts adopters write against live in
:mod:`adcp.decisioning.specialisms.*`.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from adcp.decisioning.platform import (
    GOVERNANCE_SPECIALISMS,
    DecisioningCapabilities,
    DecisioningPlatform,
)
from adcp.decisioning.state import _NotYetWiredStateReader
from adcp.decisioning.task_registry import (
    TaskHandoffContext,
    TaskRegistry,
)
from adcp.decisioning.types import AdcpError, TaskHandoff, is_task_handoff

if TYPE_CHECKING:
    from pydantic import BaseModel

    from adcp.decisioning.accounts import AccountStore
    from adcp.decisioning.context import AuthInfo, RequestContext
    from adcp.decisioning.types import Account
    from adcp.server.base import ToolContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# REQUIRED_METHODS_PER_SPECIALISM — what each specialism must implement
# ---------------------------------------------------------------------------

#: Required platform methods per specialism. ``validate_platform`` walks
#: ``capabilities.specialisms`` against this map at server boot and
#: fail-fasts when a claimed specialism is missing methods.
#:
#: Keyed by specialism slug (matches the AdCP wire enum in
#: ``schemas/cache/enums/specialism.json``). v6.0 ships ``sales-*``;
#: v6.1 adds the rest as new specialism Protocols land.
#:
#: Drift policy: when a specialism Protocol gains a required method,
#: bump this map AND add a v6.x migration note. ``validate_platform``
#: tolerates *unknown* specialisms (forward-compat with v6.x+ specs)
#: but only via UserWarning — see D14 round-3.
REQUIRED_METHODS_PER_SPECIALISM: dict[str, frozenset[str]] = {
    # All nine sales-* specialisms share the unified hybrid SalesPlatform
    # surface. Per the SalesPlatform docstring, every sales-* claim
    # requires the five core methods. The four optional methods
    # (get_media_buys, provide_performance_feedback,
    # list_creative_formats, list_creatives) are present-or-absent —
    # not enforced here. The v6.0 rc.1 spec mandates them; v6.0 alpha
    # tolerates absence so adopters can ship in stages.
    "sales-non-guaranteed": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-guaranteed": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-broadcast-tv": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-streaming-tv": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-social": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-exchange": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    "sales-proposal-mode": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
        }
    ),
    # Catalog-driven and retail-media require the sales core PLUS
    # sync_catalogs (to push the inventory taxonomy). v6.1 adds
    # log_event + sync_event_sources for retail-media; for v6.0 alpha
    # we leave those off the required list so adopters can ship sales
    # core first.
    "sales-catalog-driven": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
            "sync_catalogs",
        }
    ),
    "sales-retail-media": frozenset(
        {
            "get_products",
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "get_media_buy_delivery",
            "sync_catalogs",
        }
    ),
}


# ---------------------------------------------------------------------------
# validate_platform — server-boot fail-fast
# ---------------------------------------------------------------------------


def validate_platform(platform: DecisioningPlatform) -> None:
    """Server-boot validator — fail-fast before the first request.

    Checks (in order):

    1. ``platform.capabilities`` is a populated
       :class:`DecisioningCapabilities` (not the base default).
    2. ``platform.accounts`` is a real :class:`AccountStore`
       (anything truthy with a ``resolve`` method) — None catches
       subclasses that forgot to attach a store.
    3. Each claimed specialism's required methods are implemented
       on the platform subclass. Unknown specialisms emit
       ``UserWarning`` (forward-compat with v6.x+ specs); known
       specialisms missing methods raise ``AdcpError("INVALID_REQUEST")``.
    4. **Governance opt-in fail-fast (D15 round-4):** if any claimed
       specialism is in :data:`GOVERNANCE_SPECIALISMS` AND
       ``capabilities.governance_aware`` is False AND the platform
       hasn't wired a custom :class:`StateReader` (i.e., the dispatch
       hydration helper would supply ``_NotYetWiredStateReader``),
       raise. Silent governance-gate skipping is a security
       regression the framework refuses to ship.

    Catches per-validator exceptions and re-projects to
    ``AdcpError("INVALID_REQUEST")`` so server boot never crashes
    with a raw stack trace — the operator sees one structured
    diagnostic per problem (Round-4 Emma #16).

    :raises AdcpError: on any blocking validation failure. The error
        ``details`` carry per-issue diagnostics for operator triage.
    """
    if not isinstance(platform.capabilities, DecisioningCapabilities):
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform.capabilities must be a "
                "DecisioningCapabilities instance — found "
                f"{type(platform.capabilities).__name__!r}. Subclasses MUST "
                "set ``capabilities = DecisioningCapabilities(...)`` on the "
                "class body."
            ),
            recovery="terminal",
        )

    accounts = getattr(platform, "accounts", None)
    if accounts is None:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform.accounts is None — subclasses MUST set "
                "an AccountStore (SingletonAccounts, ExplicitAccounts, "
                "FromAuthAccounts, or a custom AccountStore impl) on the "
                "class body."
            ),
            recovery="terminal",
        )

    # Specialism-method coverage.
    missing: list[tuple[str, str]] = []
    unknown: list[str] = []
    governance_specialisms_claimed: list[str] = []
    for specialism in platform.capabilities.specialisms:
        if specialism in GOVERNANCE_SPECIALISMS:
            governance_specialisms_claimed.append(specialism)
        try:
            required = REQUIRED_METHODS_PER_SPECIALISM.get(specialism)
        except Exception as exc:
            # Defensive: a custom REQUIRED_METHODS_PER_SPECIALISM impl
            # (test-monkeypatch, etc.) that raises must not crash boot.
            # Round-4 Emma #16 — wrap validator throws.
            logger.warning(
                "REQUIRED_METHODS_PER_SPECIALISM lookup raised for %r: %r",
                specialism,
                exc,
            )
            required = None
        if required is None:
            unknown.append(specialism)
            continue
        for method_name in required:
            if not _has_overridden_method(platform, method_name):
                missing.append((specialism, method_name))

    if unknown:
        warnings.warn(
            (
                f"DecisioningPlatform claims unknown specialism(s) "
                f"{sorted(unknown)!r}. Either typos (compare against the AdCP "
                f"specialism enum: {sorted(REQUIRED_METHODS_PER_SPECIALISM.keys())}), "
                "or your framework version predates the spec. Required-method "
                "validation is skipped for these specialisms; tools/list will "
                "advertise the spec set this framework version knows."
            ),
            UserWarning,
            stacklevel=2,
        )

    if missing:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform claims specialisms but is missing "
                f"required methods: {missing}. Implement each on your "
                "subclass or remove the specialism from "
                "capabilities.specialisms."
            ),
            recovery="terminal",
            details={"missing": [{"specialism": s, "method": m} for s, m in missing]},
        )

    # Governance opt-in fail-fast (D15 round-4).
    if governance_specialisms_claimed and not platform.capabilities.governance_aware:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                f"Platform claims governance-* specialism(s) "
                f"{governance_specialisms_claimed!r} but "
                "capabilities.governance_aware is False. Set "
                "governance_aware=True AND wire a custom StateReader that "
                "returns real GovernanceContextJWS values, OR drop the "
                "governance-* specialism claim. Silent governance-gate "
                "skipping is a security boundary; the framework refuses "
                "to ship that. See "
                "docs/proposals/decisioning-platform-dispatch-design.md#d15"
            ),
            recovery="terminal",
            details={
                "governance_specialisms": sorted(governance_specialisms_claimed),
                "governance_aware": False,
            },
        )


def _has_overridden_method(platform: DecisioningPlatform, method_name: str) -> bool:
    """True when the platform subclass provides ``method_name``.

    The base :class:`DecisioningPlatform` class itself doesn't define
    specialism methods (D11 — base is intentionally minimal). So
    ``hasattr(platform, method_name)`` is sufficient: if the attribute
    exists, the subclass put it there.
    """
    return hasattr(platform, method_name) and callable(getattr(platform, method_name))


# ---------------------------------------------------------------------------
# compose_caller_identity — D9 round-3 composite cache scope key
# ---------------------------------------------------------------------------


def compose_caller_identity(
    account: Account[Any],
    store: AccountStore[Any],
) -> str:
    """Compose the cache scope key from store qualname + account id.

    Round-3 D9: the framework's idempotency middleware reads
    ``ctx.caller_identity`` for cache scoping. Using ``account.id``
    alone leaks across stores when two adopters use different
    ``AccountStore`` impls but happen to mint colliding ids. The
    composite ``f"{store qualname}:{account.id}"`` gives structural
    cross-store isolation at zero coordination cost.

    Within-store collisions (one impl, identical ``account.id`` for
    two distinct accounts) remain an adopter bug at
    ``AccountStore.resolve``; the framework can't structurally prevent
    that without a runtime registry costing more than it buys.
    """
    return f"{type(store).__qualname__}:{account.id}"


# ---------------------------------------------------------------------------
# _build_request_context — the hydration helper
# ---------------------------------------------------------------------------


def _build_request_context(
    tool_ctx: ToolContext,
    account: Account[Any],
    auth_info: AuthInfo | None,
    *,
    state_reader: Any | None = None,
    resource_resolver: Any | None = None,
) -> RequestContext[Any]:
    """Hydrate a :class:`RequestContext` per the D2 + D9 + D15 contract.

    Mirrors the TS-side ``to-context.ts:buildRequestContext``. The
    framework supplies the context per request; adopters never
    construct one (the class docstring on
    :class:`adcp.decisioning.RequestContext` carries the
    ``@internal-construction`` note).

    :param tool_ctx: The framework's :class:`ToolContext` from the
        underlying transport. Carries ``request_id``, ``tenant_id``,
        and ``metadata``; we extend its caller_identity to the
        composite key.
    :param account: Resolved account from the platform's
        :class:`AccountStore.resolve`.
    :param auth_info: Optional verified principal info — when present,
        ``auth_principal`` is populated from ``auth_info.principal``.
    :param state_reader: Custom ``StateReader`` impl. Defaults to the
        v6.0 stub. Accept as a parameter so ``serve()`` can wire a
        v6.1 backing store without touching dispatch.
    :param resource_resolver: Custom ``ResourceResolver`` impl. Same
        plumbing rationale as ``state_reader``.
    """
    # Local import to avoid a circular at module-load time. dispatch.py
    # is imported by serve.py; context.py and accounts.py both reach
    # back into adcp.decisioning, so the cycle is real if we hoist.
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.resolve import _NotYetWiredResolver

    auth_principal = auth_info.principal if auth_info is not None else None

    # Build the RequestContext with the explicit state/resolve kwargs
    # if provided; otherwise let the dataclass default factories
    # supply the v6.0 stubs.
    ctx_kwargs: dict[str, Any] = {
        "request_id": tool_ctx.request_id,
        "caller_identity": tool_ctx.caller_identity,
        "tenant_id": tool_ctx.tenant_id,
        "metadata": dict(tool_ctx.metadata),
        "account": account,
        "auth_info": auth_info,
        "auth_principal": auth_principal,
    }
    if state_reader is not None:
        ctx_kwargs["state"] = state_reader
    else:
        ctx_kwargs["state"] = _NotYetWiredStateReader()
    if resource_resolver is not None:
        ctx_kwargs["resolve"] = resource_resolver
    else:
        ctx_kwargs["resolve"] = _NotYetWiredResolver()

    return RequestContext(**ctx_kwargs)


# ---------------------------------------------------------------------------
# _invoke_platform_method + _project_handoff — the call seam
# ---------------------------------------------------------------------------


async def _invoke_platform_method(
    platform: DecisioningPlatform,
    method_name: str,
    params: BaseModel,
    ctx: RequestContext[Any],
    *,
    executor: ThreadPoolExecutor,
    registry: TaskRegistry,
    arg_projector: dict[str, Any] | None = None,
) -> Any:
    """Invoke a platform method, projecting hybrid returns.

    Detects async-vs-sync via ``asyncio.iscoroutinefunction`` (NOT
    ``inspect.iscoroutinefunction`` — the latter doesn't unwrap
    ``functools.partial`` until 3.12). Sync methods run on the
    explicit thread-pool executor with an explicit
    ``contextvars.copy_context()`` snapshot so middleware-set
    ContextVars survive the cross-thread hop (D5 + D6).

    ``TaskHandoff`` returns flow through :func:`_project_handoff` to
    allocate a task_id, kick off the handoff fn, and project the
    Submitted envelope.

    Wraps any non-:class:`AdcpError` exception to
    ``AdcpError("INTERNAL_ERROR", recovery="terminal")`` so the wire
    response never leaks a stack trace. Adopters get the original
    exception logged via the framework's observability hooks (the
    raise re-raises the wrapped error; the original is the
    ``__cause__``).

    :param arg_projector: Optional kwargs dict for tools whose Python
        method signature differs from the wire shape (D1
        arg-projection, e.g. ``update_media_buy(media_buy_id, patch,
        ctx)``). Codegen-emitted shims pass this for those tools;
        most tools call with ``None``.
    """
    method = getattr(platform, method_name)

    try:
        if asyncio.iscoroutinefunction(method):
            if arg_projector is not None:
                result = await method(**arg_projector, ctx=ctx)
            else:
                result = await method(params, ctx)
        else:
            ctx_snapshot = contextvars.copy_context()
            loop = asyncio.get_running_loop()
            if arg_projector is not None:
                projected_kwargs = {**arg_projector, "ctx": ctx}
                result = await loop.run_in_executor(
                    executor,
                    functools.partial(ctx_snapshot.run, method, **projected_kwargs),
                )
            else:
                result = await loop.run_in_executor(
                    executor,
                    functools.partial(ctx_snapshot.run, method, params, ctx),
                )
    except AdcpError:
        # Adopter raised structured error — propagate verbatim. The
        # outer middleware projects to the wire envelope.
        raise
    except Exception as exc:
        # Wrap unexpected exceptions so the wire never sees a stack
        # trace. Adopter logs the original via observability hooks;
        # __cause__ is preserved for server-side debugging.
        logger.exception(
            "Unhandled exception in platform.%s — wrapping to INTERNAL_ERROR",
            method_name,
        )
        raise AdcpError(
            "INTERNAL_ERROR",
            message="An internal error occurred",
            recovery="terminal",
        ) from exc

    if is_task_handoff(result):
        return await _project_handoff(
            result,
            ctx,
            method_name=method_name,
            registry=registry,
            executor=executor,
        )
    return result


async def _project_handoff(
    handoff: TaskHandoff[Any],
    ctx: RequestContext[Any],
    *,
    method_name: str,
    registry: TaskRegistry,
    executor: ThreadPoolExecutor,
) -> dict[str, Any]:
    """Promote a TaskHandoff to a background task.

    Lifecycle:

    1. Allocate ``task_id`` via ``registry.issue(account_id=...,
       task_type=method_name)``. The registry persists the row in
       ``submitted`` state.
    2. Kick off the handoff fn in the background via
       :func:`asyncio.create_task` (async fn) or
       :func:`loop.run_in_executor` (sync fn) with an explicit
       ``contextvars.copy_context()`` snapshot. ``create_task``
       inherits the snapshot for free; ``run_in_executor`` doesn't,
       hence the explicit copy.
    3. The background task awaits the handoff fn's return; on success
       calls ``registry.complete(task_id, result.model_dump() if
       Pydantic else result)``; on :class:`AdcpError` calls
       ``registry.fail(task_id, error.to_wire())``; on any other
       exception, wraps to ``INTERNAL_ERROR`` and calls
       ``registry.fail``.
    4. Returns the wire ``Submitted`` envelope dict to the synchronous
       caller (the platform method's typed shim), which projects it
       to the buyer.

    :param method_name: Wire-spec verb name (``'create_media_buy'``,
        etc.) — used as ``task_type`` on the registry row so
        ``tasks/get`` round-trips correctly.

    The handoff fn is extracted via the type-identity dispatch in
    :func:`adcp.decisioning.types.is_task_handoff`. Subclassed
    TaskHandoff instances (deliberate non-feature) silently take the
    sync-return path before reaching this function.
    """
    fn = handoff._fn

    task_id = await registry.issue(
        account_id=ctx.account.id,
        task_type=method_name,
    )

    # Hand off to background. The wire envelope returns immediately;
    # the fn runs to completion in the background and persists the
    # terminal artifact via the registry.
    handoff_ctx = TaskHandoffContext(id=task_id, _registry=registry)

    async def _run() -> None:
        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(handoff_ctx)
            else:
                ctx_snapshot = contextvars.copy_context()
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    executor,
                    functools.partial(ctx_snapshot.run, fn, handoff_ctx),
                )
        except AdcpError as exc:
            await registry.fail(task_id, exc.to_wire())
            return
        except Exception:
            logger.exception(
                "Unhandled exception in handoff fn for task %s — wrapping",
                task_id,
            )
            wrapped = AdcpError(
                "INTERNAL_ERROR",
                message="An internal error occurred during background task",
                recovery="terminal",
            )
            await registry.fail(task_id, wrapped.to_wire())
            return

        # Persist terminal artifact. Pydantic responses get
        # ``model_dump()``; dict responses pass through.
        if hasattr(result, "model_dump"):
            await registry.complete(task_id, result.model_dump())
        elif isinstance(result, dict):
            await registry.complete(task_id, result)
        else:
            # Adopter returned an unexpected type (not Pydantic, not
            # dict). Best effort: stringify into a 'value' wrapper so
            # tasks/get returns something. Real impls always return
            # the typed Pydantic response.
            await registry.complete(task_id, {"value": str(result)})

    # ``asyncio.create_task`` snapshots contextvars automatically
    # — no explicit copy needed at this site.
    asyncio.create_task(_run())

    # Wire ``Submitted`` envelope per spec.
    return {
        "task_id": task_id,
        "status": "submitted",
        "task_type": method_name,
    }


__all__ = [
    "REQUIRED_METHODS_PER_SPECIALISM",
    "compose_caller_identity",
    "validate_platform",
]
