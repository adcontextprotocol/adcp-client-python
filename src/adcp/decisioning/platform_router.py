"""Tenant-keyed multi-platform dispatcher.

:class:`PlatformRouter` lets a single ``serve()`` process host N
``DecisioningPlatform`` instances behind one server. Each request
resolves to a tenant via the router's :class:`AccountStore`, and the
router dispatches every per-specialism call to the tenant's platform.

This is the migration target for adopters with a salesagent-shaped
``ADAPTER_REGISTRY: dict[str, Type[AdServerAdapter]]`` pattern: rather
than instantiating an adapter per request from a registry, the adopter
preloads one :class:`DecisioningPlatform` per tenant (or per backend)
into the router and lets the framework pick the right one per request.

Drop-in shape
-------------

:class:`PlatformRouter` *is* a :class:`DecisioningPlatform` —
``isinstance(router, DecisioningPlatform)`` is true. It carries
``capabilities``, ``accounts``, and one concrete method per
specialism. Adopters pass it to :func:`adcp.decisioning.serve` exactly
where they would pass a single platform.

::

    from adcp.decisioning import PlatformRouter, DecisioningCapabilities, serve

    router = PlatformRouter(
        accounts=tenant_routing_account_store,
        platforms={
            "tenant-a": MockGuaranteedPlatform(),
            "tenant-b": MockNonGuaranteedPlatform(),
        },
        capabilities=DecisioningCapabilities(
            specialisms=["sales-guaranteed", "sales-non-guaranteed"],
            ...,
        ),
    )

    serve(router, ...)

Tenant resolution
-----------------

The router looks at ``ctx.account.metadata['tenant_id']`` to pick a
platform. The adopter's :class:`AccountStore` is responsible for
populating this — typically it reads the request's
:func:`adcp.server.tenant_router.current_tenant` (set by
:class:`SubdomainTenantMiddleware`) or maps the wire ``account_ref``
to a tenant via its own table.

If the resolved tenant has no registered platform the router raises
``ACCOUNT_NOT_FOUND`` (terminal). The reasoning: from the buyer's
perspective the account doesn't exist on this server — the multi-tenant
fan-out is invisible to them, and surfacing tenant-existence as a
distinct error code would leak deployment topology.

Capabilities
------------

The router's :attr:`capabilities` is supplied by the adopter and
should be the **union** of every child platform's specialisms. Two
reasons for union (not intersection):

1. ``tools/list`` (the ``advertised_tools_for_instance`` filter) reads
   the router's specialisms and advertises every tool any child
   platform serves. Buyers see the full menu and the router routes
   per-call to the platform that supports each tool.
2. Buyers calling a tool the resolved tenant's platform doesn't
   implement get a structured ``UNSUPPORTED_FEATURE`` error per spec —
   not a 404. Intersection-based capabilities would silently hide the
   tool from sellers that DO support it on other tenants, breaking
   the ``one URL → many tenants`` model.

Protocol introspection
----------------------

At construction time the router walks the specialism Protocol classes
(``SalesPlatform``, ``AudiencePlatform``, ``SignalsPlatform``, etc.)
declared in :mod:`adcp.decisioning.specialisms` and synthesizes a
delegating method for each method any child platform implements. New
specialism Protocols added to the SDK are picked up automatically — no
adopter code change required to extend the router's surface.

Out of scope
------------

* **Cross-tenant inventory aggregation.** Each tenant is an island.
* **Per-tenant ``upstream_url``.** The :meth:`upstream_for` helper on
  each child platform handles that; the router doesn't proxy upstream
  HTTP itself.
* **Tenant fan-out (one request → many tenants).** Each request resolves
  to exactly one tenant via the AccountStore.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from adcp.decisioning.platform import (
    DecisioningCapabilities,
    DecisioningPlatform,
)
from adcp.decisioning.specialisms import (
    AudiencePlatform,
    BrandRightsPlatform,
    CampaignGovernancePlatform,
    CollectionListsPlatform,
    ContentStandardsPlatform,
    CreativeAdServerPlatform,
    CreativeBuilderPlatform,
    PropertyListsPlatform,
    SalesPlatform,
    SignalsPlatform,
)
from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from adcp.decisioning.accounts import AccountStore
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.proposal_manager import ProposalManager
    from adcp.decisioning.proposal_store import ProposalStore


# Every specialism Protocol the framework knows about. New Protocol
# classes added to ``adcp.decisioning.specialisms`` get picked up by
# adding them here. Walking ``__protocol_attrs__`` (set by the runtime
# ``@runtime_checkable`` machinery) yields each method name the
# Protocol declares. The router's synthesized delegation covers the
# union of those names across every Protocol — adopter doesn't have
# to enumerate them.
_KNOWN_SPECIALISM_PROTOCOLS: tuple[type, ...] = (
    SalesPlatform,
    SignalsPlatform,
    AudiencePlatform,
    CreativeBuilderPlatform,
    CreativeAdServerPlatform,
    CampaignGovernancePlatform,
    BrandRightsPlatform,
    ContentStandardsPlatform,
    PropertyListsPlatform,
    CollectionListsPlatform,
)

# AccountStore methods the router forwards to its underlying store.
# These are framework-internal — the router does NOT delegate them
# per-tenant (the AccountStore IS the tenant resolver, so threading
# resolution through itself would loop).
_ACCOUNT_STORE_METHODS: frozenset[str] = frozenset({"resolve", "upsert", "list", "sync_governance"})


def _protocol_method_names(proto: type) -> frozenset[str]:
    """Return the public method names a runtime-checkable Protocol declares.

    Walks the Protocol class body (NOT the MRO) and collects every
    callable whose name doesn't start with ``_``. Each Protocol class
    in :mod:`adcp.decisioning.specialisms` defines its methods directly
    — there's no Protocol inheritance to chase, so a single ``vars()``
    pass gets every declared method.

    Annotation-only attributes (``foo: int``) are NOT picked up because
    no Protocol in :mod:`adcp.decisioning.specialisms` declares
    attribute-only members; if that changes, broaden the walk to
    ``__annotations__`` too.
    """
    declared: set[str] = set()
    for name, value in vars(proto).items():
        if name.startswith("_"):
            continue
        if callable(value):
            declared.add(name)
    return frozenset(declared)


def _all_specialism_methods() -> frozenset[str]:
    """Union of declared method names across every known specialism Protocol."""
    union: set[str] = set()
    for proto in _KNOWN_SPECIALISM_PROTOCOLS:
        union |= _protocol_method_names(proto)
    return frozenset(union)


def _resolve_ctx_from_args(
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> RequestContext[Any]:
    """Pull the ``RequestContext`` out of a synthesized delegation's arguments.

    The dispatcher (:func:`adcp.decisioning.dispatch._invoke_platform_method`)
    invokes platform methods two ways:

    * positional: ``method(params, ctx)`` — ctx is the second positional
    * arg_projector: ``method(**arg_projector, ctx=ctx)`` — ctx is a kwarg

    Both shapes need to surface the ``RequestContext`` so the router
    can extract the tenant id. We probe the kwargs first (``ctx=...``)
    and fall back to the last positional argument otherwise.

    :raises AdcpError: ``INTERNAL_ERROR`` when neither shape carries a
        ``RequestContext`` — this means the dispatcher's contract has
        drifted and the router can't function. Adopter code never hits
        this path.
    """
    # Late-import to keep the module-load circle-free.
    from adcp.decisioning.context import RequestContext

    ctx: Any = kwargs.get("ctx")
    if ctx is None and args:
        # Last positional is conventionally the ctx (every Protocol method
        # signature in adcp.decisioning.specialisms ends with ``ctx``).
        candidate = args[-1]
        if isinstance(candidate, RequestContext):
            ctx = candidate

    if not isinstance(ctx, RequestContext):
        raise AdcpError(
            "INTERNAL_ERROR",
            message=(
                "PlatformRouter delegation could not find a RequestContext "
                "in the call arguments. The dispatcher contract requires "
                "every platform method to receive ctx as the trailing "
                "positional or as ``ctx=`` kwarg."
            ),
            recovery="terminal",
        )
    return ctx


def _tenant_id_from_ctx(ctx: RequestContext[Any]) -> str:
    """Extract the tenant id from the resolved account.

    The convention: the AccountStore wired into the router populates
    ``account.metadata['tenant_id']`` per request. This is the same
    metadata-key precedent set by Phase 2's ``mock_upstream_url``
    (see :mod:`adcp.decisioning.account_mode`).

    :raises AdcpError: ``ACCOUNT_NOT_FOUND`` when the resolved account
        carries no tenant id. The buyer sees the same error as a
        nonexistent account — multi-tenant topology stays opaque to
        the wire.
    """
    metadata = getattr(ctx.account, "metadata", None) or {}
    tenant_id: Any = None
    if isinstance(metadata, Mapping):
        tenant_id = metadata.get("tenant_id")
    else:
        # Adopter passed a typed-dataclass metadata; try attribute access.
        tenant_id = getattr(metadata, "tenant_id", None)

    if not tenant_id or not isinstance(tenant_id, str):
        raise AdcpError(
            "ACCOUNT_NOT_FOUND",
            message=(
                f"PlatformRouter could not resolve a tenant for "
                f"account_id={ctx.account.id!r}. The router's AccountStore "
                "must populate ``metadata['tenant_id']`` on every "
                "resolved account; got "
                f"{tenant_id!r}."
            ),
            recovery="terminal",
            field="account.metadata.tenant_id",
        )
    return str(tenant_id)


class PlatformRouter(DecisioningPlatform):
    """Drop-in :class:`DecisioningPlatform` that fans calls out across N tenants.

    Each per-specialism call resolves a tenant from
    ``ctx.account.metadata['tenant_id']`` and delegates to the
    matching child :class:`DecisioningPlatform`. The router's class-
    level ``capabilities`` is the union of every child's specialisms;
    individual calls that the resolved tenant's platform doesn't
    implement raise ``UNSUPPORTED_FEATURE``.

    The set of methods the router exposes is computed at construction
    by walking the framework's known specialism Protocols (see
    :data:`_KNOWN_SPECIALISM_PROTOCOLS`). New Protocols added to the
    SDK appear on the router automatically — adopters don't have to
    update their router wiring when the specialism surface grows.

    :param accounts: The adopter's :class:`AccountStore`. Resolves
        every request to an :class:`Account` whose
        ``metadata['tenant_id']`` keys :attr:`platforms`. This is the
        adopter's responsibility — typically the store reads
        :func:`adcp.server.current_tenant` (set by
        :class:`SubdomainTenantMiddleware`) and writes the tenant id
        onto the account metadata.
    :param platforms: ``{tenant_id: DecisioningPlatform}`` mapping. The
        router copies the dict shallowly at construction; later
        mutations to the source dict are NOT reflected. Pass a fresh
        dict per ``serve()`` call.
    :param proposal_managers: Optional ``{tenant_id: ProposalManager}``
        mapping for the two-platform composition (see
        ``docs/proposals/product-architecture.md``). When a tenant has
        a wired :class:`ProposalManager`, the router routes
        ``get_products`` (and refine-mode ``get_products`` when the
        manager declares :attr:`ProposalCapabilities.refine` and
        implements ``refine_products``) to that manager instead of the
        tenant's :class:`DecisioningPlatform`. Tenants without an
        entry fall through to ``platform.get_products`` —
        backward-compatible per tenant. Keys MUST be a subset of
        :attr:`platforms`; orphan tenants raise at construction.
        Single-tenant adopters use a one-entry router with
        ``{"default": MyProposalManager(...)}``.
    :param capabilities: The router's wire-shape capability
        declaration. Should be the union of every child platform's
        specialisms — the framework's ``tools/list`` filter reads this
        to advertise the right tools, and ``validate_platform`` reads
        the same value to verify each claimed specialism has its
        required methods (which the router's synthesized delegation
        provides).

    :raises ValueError: when :attr:`platforms` is empty (a router with
        no children is misconfiguration, not a valid empty state), or
        when :attr:`proposal_managers` contains tenant_ids not present
        in :attr:`platforms`.
    """

    def __init__(
        self,
        *,
        accounts: AccountStore[Any],
        platforms: Mapping[str, DecisioningPlatform],
        capabilities: DecisioningCapabilities,
        proposal_managers: Mapping[str, ProposalManager] | None = None,
        proposal_stores: Mapping[str, ProposalStore] | None = None,
    ) -> None:
        if not platforms:
            raise ValueError(
                "PlatformRouter requires at least one child platform; "
                "got an empty mapping. A router with no children would "
                "404 every request."
            )

        # Shallow copy — adopter-side dict mutations don't leak into the
        # router's view. Children are NOT defensively copied (they're
        # framework-instance singletons by contract).
        self._platforms: dict[str, DecisioningPlatform] = dict(platforms)

        # Per-tenant ProposalManager binding. Validate keys are a
        # subset of platforms — every tenant that wires a manager must
        # have a corresponding execution-side platform; orphan tenants
        # would silently route nothing.
        self._proposal_managers: dict[str, ProposalManager] = dict(proposal_managers or {})
        if self._proposal_managers:
            orphans = set(self._proposal_managers) - set(self._platforms)
            if orphans:
                raise ValueError(
                    f"proposal_managers keys must be a subset of platforms keys; "
                    f"orphan tenant_id(s): {sorted(orphans)}"
                )

        # Per-tenant ProposalStore binding (v1.5 § D5). Same orphan
        # validation; no auto-allocation — finalize-capable managers
        # without a wired store are a hard error so multi-worker
        # deployments don't silently lose proposals at the first
        # worker that didn't see put_draft.
        self._proposal_stores: dict[str, ProposalStore] = dict(proposal_stores or {})
        if self._proposal_stores:
            orphans = set(self._proposal_stores) - set(self._platforms)
            if orphans:
                raise ValueError(
                    f"proposal_stores keys must be a subset of platforms keys; "
                    f"orphan tenant_id(s): {sorted(orphans)}"
                )

        # Cross-store consistency check: a tenant declaring
        # finalize=True needs a wired store. The error message names
        # the exact kwarg to add — adopters get a 30-second copy-paste
        # rather than a debugging session at first finalize request.
        for tenant_id, manager in self._proposal_managers.items():
            caps = getattr(manager, "capabilities", None)
            finalize_supported = bool(getattr(caps, "finalize", False))
            if finalize_supported and tenant_id not in self._proposal_stores:
                raise ValueError(
                    f"Tenant {tenant_id!r} wired a ProposalManager declaring "
                    f"finalize=True, but no ProposalStore was registered for "
                    f"that tenant. Wire one via "
                    f"proposal_stores={{{tenant_id!r}: InMemoryProposalStore()}}, "
                    "or remove the finalize capability."
                )

        self.accounts = accounts
        self.capabilities = capabilities

        # Synthesize a delegating method per specialism method name.
        # Bound at construction so ``getattr(router, name)`` resolves
        # to a callable that closes over ``self`` and ``method_name``.
        # The framework's dispatcher uses ``getattr(platform, name)``
        # to find methods; instance-level callables work for that.
        # Sorted for deterministic synthesis order — easier to debug
        # than the underlying frozenset iteration order.
        #
        # ``get_products`` is special-cased: when proposal_managers is
        # wired the router needs to inspect the request's buying_mode
        # and the manager's capabilities before delegating. The
        # synthesized delegation can't do that — it just forwards.
        # Skip get_products here; the explicit method below handles it.
        for method_name in sorted(_all_specialism_methods()):
            if method_name in _ACCOUNT_STORE_METHODS:
                # Defensive: AccountStore methods MUST stay on the
                # router's accounts store, not be synthesized as
                # tenant-keyed delegations. Skip.
                continue
            if method_name == "get_products":
                # Handled explicitly by ``self.get_products`` below to
                # support per-tenant proposal_manager routing.
                continue
            self.__dict__[method_name] = self._make_delegate(method_name)

    # ----- per-tenant dispatch helpers --------------------------------

    def _platform_for(
        self,
        ctx: RequestContext[Any],
        method_name: str,
    ) -> DecisioningPlatform:
        """Look up the child platform for ``ctx``'s tenant.

        :raises AdcpError: ``ACCOUNT_NOT_FOUND`` when the tenant id
            resolves to no registered platform. ``UNSUPPORTED_FEATURE``
            when the platform exists but doesn't implement
            ``method_name``.
        """
        tenant_id = _tenant_id_from_ctx(ctx)
        platform = self._platforms.get(tenant_id)
        if platform is None:
            raise AdcpError(
                "ACCOUNT_NOT_FOUND",
                message=(
                    f"PlatformRouter has no platform for "
                    f"tenant_id={tenant_id!r}. Register one in the "
                    "``platforms=`` mapping passed to the router, or "
                    "fix the AccountStore to resolve to a known tenant."
                ),
                recovery="terminal",
                field="account.metadata.tenant_id",
            )

        method = getattr(platform, method_name, None)
        if method is None or not callable(method):
            raise AdcpError(
                "UNSUPPORTED_FEATURE",
                message=(
                    f"Tenant {tenant_id!r}'s platform "
                    f"({type(platform).__name__}) does not implement "
                    f"{method_name!r}. The router advertises this method "
                    "because at least one child platform supports it, "
                    "but this tenant's platform doesn't."
                ),
                recovery="terminal",
            )
        return platform

    async def refine_get_products(self, *args: Any, **kwargs: Any) -> Any:
        """Refine entry point — delegates to :meth:`get_products`.

        The handler's refine pathway dispatches via
        ``_invoke_platform_method(platform, "refine_get_products", ...)``
        when the platform's :func:`has_refine_support` returns True. The
        router's get_products already handles refine routing internally
        (per-tenant ProposalManager.refine_products selection), so this
        method just forwards. Keeps the handler's existing call shape
        intact without router-specific branching there.
        """
        return await self.get_products(*args, **kwargs)

    async def get_products(self, *args: Any, **kwargs: Any) -> Any:
        """Per-tenant ``get_products`` dispatch.

        Resolves the tenant from ``ctx.account.metadata['tenant_id']``
        (same path as every other router delegation). When the tenant
        has a wired :class:`ProposalManager`, routes the call to it;
        when refine-mode + capability + method-presence all hold,
        routes to ``proposal_manager.refine_products``; otherwise
        falls through to the tenant's
        :meth:`DecisioningPlatform.get_products`.

        The fall-through path (no proposal_manager wired for this
        tenant) is bit-identical to the synthesized delegation
        :meth:`_make_delegate` would have produced — adopters with
        zero proposal_managers configured see the same behaviour as
        before this method existed.
        """
        ctx = _resolve_ctx_from_args(args, kwargs)
        tenant_id = _tenant_id_from_ctx(ctx)
        manager = self._proposal_managers.get(tenant_id)

        if manager is not None:
            method_name = self._select_proposal_method(manager, args, kwargs)
            method = getattr(manager, method_name)
            if inspect.iscoroutinefunction(method):
                return await method(*args, **kwargs)
            return await asyncio.to_thread(method, *args, **kwargs)

        # No proposal_manager for this tenant — fall through to the
        # platform. Reuses the same lookup helper as the synthesized
        # delegations so error projection is identical.
        platform = self._platform_for(ctx, "get_products")
        method = getattr(platform, "get_products")
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)
        return await asyncio.to_thread(method, *args, **kwargs)

    def _select_proposal_method(
        self,
        manager: ProposalManager,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> str:
        """Choose between ``get_products`` and ``refine_products`` on
        the wired :class:`ProposalManager`.

        Refine is dispatched only when all three conditions hold:

        1. The request's ``buying_mode`` is ``'refine'``.
        2. The manager's ``capabilities.refine`` flag is True.
        3. The manager subclass implements ``refine_products``
           (``hasattr`` covers the Protocol's "present-or-absent"
           semantics).

        Otherwise routes to ``get_products``. Adopters whose
        ``get_products`` handler also handles refine internally keep
        working without declaring the refine capability.

        ``buying_mode`` is read off the request — conventionally the
        first positional argument of ``get_products(req, ctx)``, or
        the ``req=`` kwarg.
        """
        req: Any = kwargs.get("req")
        if req is None and args:
            req = args[0]
        buying_mode = getattr(req, "buying_mode", None)
        # ``buying_mode`` may be a string or a generated enum (the
        # Pydantic model coerces). Normalize via ``getattr(.., 'value',
        # buying_mode)`` so both shapes compare cleanly.
        buying_mode_str = getattr(buying_mode, "value", buying_mode)
        if buying_mode_str != "refine":
            return "get_products"
        caps = getattr(manager, "capabilities", None)
        refine_supported = bool(getattr(caps, "refine", False))
        if not refine_supported:
            return "get_products"
        if not hasattr(manager, "refine_products"):
            return "get_products"
        return "refine_products"

    def _make_delegate(self, method_name: str) -> Any:
        """Create a delegating callable for ``method_name``.

        The returned callable forwards every positional and keyword
        argument verbatim to the child platform's same-named method.
        The dispatcher invokes platform methods either as
        ``method(params, ctx)`` (positional) or
        ``method(**arg_projector, ctx=ctx)`` (kwargs); the synthesized
        ``*args, **kwargs`` shape covers both.

        Sync-vs-async dispatch is decided at the dispatcher
        (:func:`adcp.decisioning.dispatch._invoke_platform_method`)
        by checking the router's method. The delegate is always
        ``async def`` so the dispatcher takes its async path and
        awaits the result. Inside the delegate we re-dispatch on the
        CHILD platform: async children are awaited directly; sync
        children run via :func:`asyncio.to_thread` so a blocking sync
        handler doesn't serialize the event loop.
        """
        router = self

        async def _delegate(*args: Any, **kwargs: Any) -> Any:
            ctx = _resolve_ctx_from_args(args, kwargs)
            platform = router._platform_for(ctx, method_name)
            method = getattr(platform, method_name)

            if inspect.iscoroutinefunction(method):
                return await method(*args, **kwargs)

            # Sync child — push to a thread so the loop stays
            # responsive. The framework's standard sync-platform
            # dispatch goes through its own ThreadPoolExecutor with a
            # contextvars snapshot; ``asyncio.to_thread`` does the same
            # using the running loop's default executor with copied
            # context.
            return await asyncio.to_thread(method, *args, **kwargs)

        _delegate.__name__ = method_name
        _delegate.__qualname__ = f"PlatformRouter.{method_name}"
        return _delegate

    # ----- introspection ---------------------------------------------

    @property
    def tenants(self) -> frozenset[str]:
        """The set of tenant ids the router knows about.

        Read-only view; mutations to the source mapping after
        construction are NOT reflected.
        """
        return frozenset(self._platforms)

    def platform_for_tenant(self, tenant_id: str) -> DecisioningPlatform:
        """Return the child platform registered for ``tenant_id``.

        :raises KeyError: when no platform is registered for the
            tenant. Adopter callers handle this; the router's runtime
            dispatch path uses :meth:`_platform_for` instead, which
            projects to ``ACCOUNT_NOT_FOUND``.
        """
        return self._platforms[tenant_id]

    def proposal_manager_for_tenant(self, tenant_id: str) -> ProposalManager | None:
        """Return the :class:`ProposalManager` for ``tenant_id``, or
        ``None`` when the tenant falls through to its platform's own
        ``get_products``.
        """
        return self._proposal_managers.get(tenant_id)

    def proposal_store_for_tenant(self, tenant_id: str) -> ProposalStore | None:
        """Return the :class:`ProposalStore` for ``tenant_id``, or
        ``None`` when the tenant has no store wired.

        Tenants without a wired store cannot dispatch finalize / expiry
        / consume paths — the cross-store consistency check at
        construction prevents declaring ``finalize=True`` without a
        store, but tenants running pure-catalog mode with no finalize
        legitimately have no store.
        """
        return self._proposal_stores.get(tenant_id)


__all__ = ["PlatformRouter"]
