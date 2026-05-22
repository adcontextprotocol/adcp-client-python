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
(``SalesPlatform``, ``AudiencePlatform``, ``SignalsPlatform``,
``OwnedSignalsPlatform``, etc.)
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
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
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
    OwnedSignalsPlatform,
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
    OwnedSignalsPlatform,
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
    ``__annotations__`` too. Direct Protocol inheritance does not need
    special handling here because inherited method providers are listed
    separately in :data:`_KNOWN_SPECIALISM_PROTOCOLS`.
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
            method_name = _select_proposal_method(manager, args, kwargs)
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


#: Type alias for a :class:`LazyPlatformRouter` factory.
#:
#: The factory takes a ``tenant_id`` string and returns a
#: :class:`DecisioningPlatform`. It may be sync or async — both shapes
#: work because the router awaits the result via
#: :func:`inspect.isawaitable`.
#:
#: Example (sync)::
#:
#:     def factory(tenant_id: str) -> DecisioningPlatform:
#:         return MockSellerPlatform(tenant_id)
#:
#: Example (async — typical for SDK auth handshakes)::
#:
#:     async def factory(tenant_id: str) -> DecisioningPlatform:
#:         cfg = await load_tenant_config(tenant_id)
#:         return GamPlatform(cfg)
PlatformFactory = Callable[[str], DecisioningPlatform | Awaitable[DecisioningPlatform]]


# Sentinel for cache miss — distinct from any value the cache might
# legitimately hold (None is rejected upstream). Same shape as
# :data:`adcp.server.tenant_router._CACHE_MISS`.
_LAZY_CACHE_MISS: object = object()


class LazyPlatformRouter(DecisioningPlatform):
    """Per-tenant ``DecisioningPlatform`` constructed on first request.

    A :class:`PlatformRouter` variant that defers building per-tenant
    platforms to first-request lookup, with a bounded LRU + TTL cache.
    Drop-in replacement: ``isinstance(router, DecisioningPlatform)`` is
    true, ``serve()`` accepts it identically, and it shares the same
    ``ctx.account.metadata['tenant_id']`` resolution path.

    **When to reach for this over** :class:`PlatformRouter`:

    * **N tenants × per-tenant SDK auth handshake.** Eagerly building
      every platform at boot scales O(N) and the auth handshake (e.g.,
      Google Ad Manager service-account, Kevel API key) typically does
      network I/O — so 50-500 tenants means the boot path either takes
      minutes or you write your own lazy layer.
    * **Hot-add / hot-rotate of tenants.** Today's eager router pins
      every platform at construction; adding a new tenant requires a
      restart. The lazy router builds on first request, with
      :meth:`invalidate` for explicit eviction when a tenant rotates.
    * **Memory profile under tenant churn.** The eager router holds
      every platform for the process lifetime. The lazy router's
      bounded cache evicts platforms for inactive tenants — strictly
      safer for long-lived processes.

    **Async factory.** Building per-tenant adapters typically does I/O.
    The factory may be sync or async; the router awaits at call time
    (matches :class:`adcp.server.CallableSubdomainTenantRouter`'s
    convention). Sync factories that block the event loop should be
    refactored to async or routed through :func:`asyncio.to_thread`
    inside the factory.

    Example::

        from adcp.decisioning import LazyPlatformRouter, DecisioningCapabilities, serve

        async def build_platform(tenant_id: str) -> DecisioningPlatform:
            cfg = await load_tenant_config(tenant_id)
            if cfg.adapter == "google_ad_manager":
                return WonderstruckGamPlatform(cfg)
            elif cfg.adapter == "kevel":
                return KevelPlatform(cfg)
            return MockSellerPlatform(cfg)

        router = LazyPlatformRouter(
            accounts=tenant_routing_account_store,
            factory=build_platform,
            capabilities=DecisioningCapabilities(specialisms=[...]),
            cache_size=256,             # default
            cache_ttl_seconds=3600.0,   # default; 0 = size-only eviction
        )

        serve(router, ...)

    **Invalidation.** Adopters call :meth:`invalidate` from tenant
    rotation / deactivation paths::

        router.invalidate("tenant-a")   # specific tenant
        router.invalidate()             # all platforms — ops "drop everything"

    Behavior on ``invalidate`` of an in-flight request: the request
    that already grabbed the platform reference completes normally
    (caller holds the ref); the next request gets a fresh build. No
    request cancellation. Mirrors
    :class:`adcp.server.CallableSubdomainTenantRouter`'s contract.

    **Thundering herd.** If two concurrent requests for the same cold
    tenant hit, both await the factory; asyncio cooperative scheduling
    means no corruption (last-write wins, both refs are equivalent),
    but the auth handshake runs 2x. Singleflight (one-build-per-tenant
    under contention) is intentionally NOT in v1 — adopters reporting
    DB pressure / API rate-limit spikes is the trigger to add it.

    **Capabilities** are adopter-supplied (the union of what every
    tenant's platform serves). The router can't introspect children at
    boot — it doesn't know what tenants exist yet — so the adopter is
    the source of truth here.

    :param accounts: The adopter's :class:`AccountStore`. Same role as
        :class:`PlatformRouter`: resolves every request to an
        :class:`Account` whose ``metadata['tenant_id']`` keys the
        factory.
    :param factory: Callable taking a ``tenant_id`` string and
        returning a :class:`DecisioningPlatform` (sync or async). Must
        not return ``None`` — return a typed
        :class:`adcp.decisioning.types.AdcpError` raise from inside the
        factory if the tenant is invalid.
    :param capabilities: The router's wire-shape capability declaration
        — should be the union of every child platform's specialisms.
    :param proposal_managers: Optional ``{tenant_id: ProposalManager}``
        — eager (managers are dict-cheap to hold). Per-tenant
        ``get_products`` routes to the manager when wired; otherwise
        falls through to the lazily-resolved platform's
        ``get_products``.
    :param proposal_stores: Optional ``{tenant_id: ProposalStore}`` —
        eager per-tenant proposal store dict. Mirrors the eager
        :class:`PlatformRouter`'s ``proposal_stores=`` shape for
        adopters with a small known tenant set. Mutually exclusive
        with ``proposal_store_factory``. Stores are dict-cheap to
        hold; the lazy machinery applies only to ``platforms`` (which
        wrap upstream connections / credentials and warrant the LRU
        eviction). See #722.
    :param proposal_store_factory: Optional
        ``Callable[[str], ProposalStore | None]`` — lazy-build flavor.
        **Called on every** :meth:`proposal_store_for_tenant`
        **invocation** (no internal caching), and the framework's
        ``proposal_dispatch`` calls the accessor 2–3× per request on
        the proposal path. If your factory does non-trivial work
        (opens a connection pool, reads config, etc.), wrap it with
        :func:`functools.lru_cache` or your own memoization — most
        ``ProposalStore`` implementations hold long-lived DB pool
        references and adopters typically return the same instance
        per tenant. Return ``None`` for tenants that don't need a
        store (pure-catalog mode without finalize). Mutually exclusive
        with ``proposal_stores``. See #722.
    :param cache_size: Maximum number of cached :class:`DecisioningPlatform`
        instances. Bounded LRU eviction past this size. Default 256.
        Adopters with more concurrent active tenants override.
    :param cache_ttl_seconds: Per-entry TTL in seconds. ``0`` means
        size-only eviction (no time-based expiry). Default 3600.0
        (one hour). Distinct from
        :class:`adcp.server.CallableSubdomainTenantRouter` which
        rejects ``0`` — there, *tenants* go stale; here, *platform
        adapters* don't, *unless* your factory reads mutable config
        (rotating API keys, adapter selection driven by a config
        store). In that case, override to a value that bounds your
        rotation lag, or call :meth:`invalidate` from your rotation
        path.

    :raises ValueError: when ``cache_size <= 0`` or
        ``cache_ttl_seconds < 0``.
    """

    def __init__(
        self,
        *,
        accounts: AccountStore[Any],
        factory: PlatformFactory,
        capabilities: DecisioningCapabilities,
        proposal_managers: Mapping[str, ProposalManager] | None = None,
        proposal_stores: Mapping[str, ProposalStore] | None = None,
        proposal_store_factory: Callable[[str], ProposalStore | None] | None = None,
        cache_size: int = 256,
        cache_ttl_seconds: float = 3600.0,
    ) -> None:
        if cache_size <= 0:
            raise ValueError(
                f"cache_size must be > 0, got {cache_size}. The whole point of "
                "LazyPlatformRouter is to bound resident memory; an unbounded "
                "cache would re-introduce the eager-router's leak profile."
            )
        if cache_ttl_seconds < 0:
            raise ValueError(
                f"cache_ttl_seconds must be >= 0, got {cache_ttl_seconds}. "
                "Pass 0 for size-only eviction (no time-based expiry)."
            )

        if proposal_stores is not None and proposal_store_factory is not None:
            raise ValueError(
                "LazyPlatformRouter: pass either proposal_stores= (eager "
                "per-tenant dict) or proposal_store_factory= (lazy), not "
                "both. The eager dict pre-binds tenants; the factory "
                "resolves on first request. See #722."
            )

        self.accounts = accounts
        self.capabilities = capabilities
        self._factory = factory
        self._proposal_managers: dict[str, ProposalManager] = dict(proposal_managers or {})
        # #722: parity with PlatformRouter — accept proposal_stores=
        # (eager) or proposal_store_factory= (lazy). The eager dict is
        # the typical small-tenant-set shape; the factory composes with
        # the lazy-build philosophy of LazyPlatformRouter (matches
        # ``factory=`` for platforms).
        self._proposal_stores: dict[str, ProposalStore] = dict(proposal_stores or {})
        self._proposal_store_factory: Callable[[str], ProposalStore | None] | None = (
            proposal_store_factory
        )

        # Cross-store consistency check on the manager-eager subset.
        # ``proposal_managers`` is eager (dict-cheap), so even though
        # we can't enumerate every possible tenant, we CAN walk the
        # known managers and refuse to construct if a finalize-capable
        # manager has no store wired for its tenant (eager dict miss
        # AND no factory). Recovers ~80% of the boot-time validation
        # the eager :class:`PlatformRouter` provides at line 376-386 —
        # adopters migrating from eager to lazy don't silently lose
        # the wiring-gap signal. The factory-only path defers to
        # first-request validation (the factory might legitimately
        # return ``None`` for some tenants).
        for tenant_id, manager in self._proposal_managers.items():
            caps = getattr(manager, "capabilities", None)
            finalize_supported = bool(getattr(caps, "finalize", False))
            if (
                finalize_supported
                and tenant_id not in self._proposal_stores
                and self._proposal_store_factory is None
            ):
                raise ValueError(
                    f"Tenant {tenant_id!r} wired a ProposalManager declaring "
                    f"finalize=True, but no ProposalStore was registered for "
                    f"that tenant and no proposal_store_factory was configured. "
                    f"Wire one via "
                    f"proposal_stores={{{tenant_id!r}: InMemoryProposalStore()}}, "
                    "supply a proposal_store_factory=, or remove the finalize "
                    "capability."
                )

        self._cache_size = cache_size
        self._cache_ttl = cache_ttl_seconds
        # OrderedDict gives LRU-by-move-to-end and bounded popitem(last=False).
        # Entry: (DecisioningPlatform, expires_at_monotonic). When ttl=0,
        # expires_at = math.inf so the time check never trips.
        self._cache: OrderedDict[str, tuple[DecisioningPlatform, float]] = OrderedDict()
        # Per-tenant generation counter. Bumped by :meth:`invalidate`
        # (specific tenant) and the global counter is bumped by the
        # ``invalidate(None)`` flush. :meth:`_resolve_platform`
        # snapshots the (tenant, global) generation pair before
        # awaiting the factory and refuses to cache a build that lost
        # the rollover race — otherwise a slow build outracing an
        # ``invalidate`` call would silently resurrect the stale
        # platform after operators thought it was gone.
        self._tenant_generations: dict[str, int] = {}
        self._global_generation: int = 0

        # Synthesize delegating methods, mirroring PlatformRouter's
        # construction. ``get_products`` is special-cased below for
        # proposal_managers routing.
        for method_name in sorted(_all_specialism_methods()):
            if method_name in _ACCOUNT_STORE_METHODS:
                continue
            if method_name == "get_products":
                continue
            self.__dict__[method_name] = self._make_delegate(method_name)

    # ----- public introspection / control --------------------------------

    @property
    def cached_tenants(self) -> frozenset[str]:
        """The set of tenant ids currently in the cache.

        Read-only snapshot; mutations to the cache after this property
        is read are not reflected.
        """
        return frozenset(self._cache)

    def invalidate(self, tenant_id: str | None = None) -> None:
        """Drop a cached platform (or every cached platform).

        Adopters call this from tenant rotation / deactivation paths to
        evict before the TTL fires. Safe to call when the tenant isn't
        currently cached (no-op).

        :param tenant_id: Specific tenant to evict. ``None`` clears the
            entire cache.
        """
        if tenant_id is None:
            self._cache.clear()
            self._global_generation += 1
            return
        self._cache.pop(tenant_id, None)
        self._tenant_generations[tenant_id] = self._tenant_generations.get(tenant_id, 0) + 1

    # ----- per-tenant dispatch -------------------------------------------

    async def _resolve_platform(self, tenant_id: str) -> DecisioningPlatform:
        """Return the platform for ``tenant_id``, building via the factory on miss.

        The factory may be sync or async; the result is awaited if
        awaitable. Cache writes happen after a successful build — a
        factory that raises is NOT cached, so the next request retries.

        **Invalidate-during-build race.** :meth:`invalidate` bumps a
        per-tenant or global generation counter. This method snapshots
        both before awaiting the factory and refuses to cache the
        result if either advanced — the freshly-built platform is
        returned to the in-flight caller (which already paid for it),
        but the cache slot stays empty so the next request rebuilds.
        Without this, a slow factory racing an ``invalidate(tenant_id)``
        would silently resurrect the platform after operators thought
        it was gone.
        """
        cached = self._cache_get(tenant_id)
        if cached is not _LAZY_CACHE_MISS:
            return cached  # type: ignore[return-value]

        tenant_gen_at_start = self._tenant_generations.get(tenant_id, 0)
        global_gen_at_start = self._global_generation

        result = self._factory(tenant_id)
        if inspect.isawaitable(result):
            result = await result

        if result is None:
            raise AdcpError(
                "ACCOUNT_NOT_FOUND",
                message=(
                    f"LazyPlatformRouter factory returned None for "
                    f"tenant_id={tenant_id!r}. The factory must return a "
                    "DecisioningPlatform (the lazy-router equivalent of an "
                    "unknown tenant in PlatformRouter.platforms). Raise "
                    "AdcpError from inside the factory if the tenant should "
                    "be rejected."
                ),
                recovery="terminal",
                field="account.metadata.tenant_id",
            )

        # Type guard: the factory's return type union is checked at
        # static analysis; runtime mis-typed returns surface here as
        # validation rather than silent corruption.
        if not isinstance(result, DecisioningPlatform):
            raise AdcpError(
                "INTERNAL_ERROR",
                message=(
                    f"LazyPlatformRouter factory returned a "
                    f"{type(result).__name__!r} for tenant_id={tenant_id!r}; "
                    "expected a DecisioningPlatform instance."
                ),
                recovery="terminal",
            )

        # Skip the cache write if invalidation raced past us.
        invalidated = (
            self._tenant_generations.get(tenant_id, 0) != tenant_gen_at_start
            or self._global_generation != global_gen_at_start
        )
        if not invalidated:
            self._cache_put(tenant_id, result)
        return result

    def _cache_get(self, tenant_id: str) -> DecisioningPlatform | object:
        entry = self._cache.get(tenant_id)
        if entry is None:
            return _LAZY_CACHE_MISS
        platform, expires_at = entry
        if self._cache_ttl > 0 and time.monotonic() > expires_at:
            self._cache.pop(tenant_id, None)
            return _LAZY_CACHE_MISS
        # LRU touch — most-recently used to the end.
        self._cache.move_to_end(tenant_id)
        return platform

    def _cache_put(self, tenant_id: str, platform: DecisioningPlatform) -> None:
        # ttl=0 → never expire; sort everything by LRU only. Use +inf
        # so the same time-check branch above stays trivial.
        if self._cache_ttl > 0:
            expires_at = time.monotonic() + self._cache_ttl
        else:
            expires_at = float("inf")
        self._cache[tenant_id] = (platform, expires_at)
        self._cache.move_to_end(tenant_id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def _platform_for_method(
        self,
        ctx: RequestContext[Any],
        method_name: str,
    ) -> DecisioningPlatform:
        """Async equivalent of :meth:`PlatformRouter._platform_for`.

        :raises AdcpError: ``ACCOUNT_NOT_FOUND`` when the factory rejects
            the tenant (returns None / raises). ``UNSUPPORTED_FEATURE``
            when the platform exists but doesn't implement the method.
        """
        tenant_id = _tenant_id_from_ctx(ctx)
        platform = await self._resolve_platform(tenant_id)
        method = getattr(platform, method_name, None)
        if method is None or not callable(method):
            raise AdcpError(
                "UNSUPPORTED_FEATURE",
                message=(
                    f"Tenant {tenant_id!r}'s platform "
                    f"({type(platform).__name__}) does not implement "
                    f"{method_name!r}. The router advertises this method "
                    "because at least one tenant supports it, but this "
                    "tenant's platform doesn't."
                ),
                recovery="terminal",
            )
        return platform

    def _make_delegate(self, method_name: str) -> Any:
        """Create a delegating callable for ``method_name``.

        Async closure that resolves the tenant, awaits the factory if
        cold, looks up the method, and delegates with sync/async
        handling matching :meth:`PlatformRouter._make_delegate`.
        """
        router = self

        async def _delegate(*args: Any, **kwargs: Any) -> Any:
            ctx = _resolve_ctx_from_args(args, kwargs)
            platform = await router._platform_for_method(ctx, method_name)
            method = getattr(platform, method_name)
            if inspect.iscoroutinefunction(method):
                return await method(*args, **kwargs)
            return await asyncio.to_thread(method, *args, **kwargs)

        _delegate.__name__ = method_name
        _delegate.__qualname__ = f"LazyPlatformRouter.{method_name}"
        return _delegate

    async def refine_get_products(self, *args: Any, **kwargs: Any) -> Any:
        """Refine entry point — delegates to :meth:`get_products`.

        Mirrors :meth:`PlatformRouter.refine_get_products`; the handler's
        refine pathway dispatches via
        ``_invoke_platform_method(platform, "refine_get_products", ...)``
        when the platform's :func:`has_refine_support` returns True.
        """
        return await self.get_products(*args, **kwargs)

    async def get_products(self, *args: Any, **kwargs: Any) -> Any:
        """Per-tenant ``get_products`` dispatch.

        Resolves the tenant, then routes to the wired
        :class:`ProposalManager` when one exists for the tenant
        (refine vs. plain selection mirrors PlatformRouter); otherwise
        falls through to the lazily-resolved platform's
        ``get_products``.
        """
        ctx = _resolve_ctx_from_args(args, kwargs)
        tenant_id = _tenant_id_from_ctx(ctx)
        manager = self._proposal_managers.get(tenant_id)

        if manager is not None:
            method_name = _select_proposal_method(manager, args, kwargs)
            method = getattr(manager, method_name)
            if inspect.iscoroutinefunction(method):
                return await method(*args, **kwargs)
            return await asyncio.to_thread(method, *args, **kwargs)

        platform = await self._platform_for_method(ctx, "get_products")
        method = getattr(platform, "get_products")
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)
        return await asyncio.to_thread(method, *args, **kwargs)

    def proposal_manager_for_tenant(self, tenant_id: str) -> ProposalManager | None:
        """Return the :class:`ProposalManager` for ``tenant_id``, or ``None``."""
        return self._proposal_managers.get(tenant_id)

    def proposal_store_for_tenant(self, tenant_id: str) -> ProposalStore | None:
        """Return the :class:`ProposalStore` for ``tenant_id``, or ``None``.

        Resolution order:

        1. Eager ``proposal_stores`` dict, when wired.
        2. ``proposal_store_factory(tenant_id)``, when wired. The
           factory is invoked on every call — adopters who need
           caching wrap the factory themselves (most ``ProposalStore``
           implementations hold long-lived pool references and are
           cheap to return on repeat calls).
        3. ``None`` — the tenant has no store wired and the framework's
           ``proposal_dispatch`` falls through to the v1 (no-proposal)
           path.

        Sibling-API parity with :meth:`PlatformRouter.proposal_store_for_tenant`
        for adopters wiring a :class:`ProposalStore` per #722. The
        framework's ``proposal_dispatch`` duck-types this method via
        ``hasattr(platform, "proposal_store_for_tenant")``.
        """
        store = self._proposal_stores.get(tenant_id)
        if store is not None:
            return store
        if self._proposal_store_factory is not None:
            return self._proposal_store_factory(tenant_id)
        return None

    async def platform_for_tenant(self, tenant_id: str) -> DecisioningPlatform:
        """Return the platform for ``tenant_id``, building via the factory if needed.

        Sibling-API parity with :meth:`PlatformRouter.platform_for_tenant`
        for adopters with admin / health endpoints that need direct
        access to a tenant's platform. Triggers the factory on cache
        miss (this is a write-path call, not just a getter).

        :raises AdcpError: ``ACCOUNT_NOT_FOUND`` when the factory
            rejects the tenant. ``INTERNAL_ERROR`` when the factory
            returns a non-:class:`DecisioningPlatform` value.
        """
        return await self._resolve_platform(tenant_id)


def _select_proposal_method(
    manager: ProposalManager,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> str:
    """Choose between ``get_products`` and ``refine_products`` on a manager.

    Extracted from :class:`PlatformRouter` so :class:`LazyPlatformRouter`
    reuses the same routing logic without a class-method on the eager
    router. The three conditions for refine: ``buying_mode == 'refine'``,
    manager declares ``capabilities.refine``, and
    ``hasattr(manager, "refine_products")``.
    """
    req: Any = kwargs.get("req")
    if req is None and args:
        req = args[0]
    buying_mode = getattr(req, "buying_mode", None)
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


class _RegistryPlatformAdapter(DecisioningPlatform):
    """DecisioningPlatform adapter backed by a :class:`TenantRegistry`.

    Returned by :meth:`TenantRegistry.as_platform`. Not part of the
    public API — the return type annotation on that method is the
    :class:`DecisioningPlatform` base class.

    Per-request dispatch:

    1. Extract ``ctx.tenant_id`` (set by the transport layer from the
       ``Host`` header or URL path in multi-tenant deployments).
    2. Await :meth:`~TenantRegistry.resolve_by_id` — triggers lazy
       platform construction on first hit for
       :meth:`~TenantRegistry.register_lazy` tenants.
    3. Gate on health: raise ``SERVICE_UNAVAILABLE`` for states not
       in ``serve_states`` (default: ``pending`` and ``disabled``
       are blocked; ``healthy`` and ``unverified`` serve).
    4. Forward the method call to the resolved platform, honouring
       async/sync detection the same way as :class:`PlatformRouter`.
    """

    def __init__(
        self,
        *,
        registry: Any,  # TenantRegistry at runtime; Any avoids circular import
        accounts: Any,
        capabilities: DecisioningCapabilities,
        serve_states: frozenset[Any],
    ) -> None:
        self.accounts = accounts
        self.capabilities = capabilities
        self._registry = registry
        self._serve_states = frozenset(serve_states)

        for method_name in sorted(_all_specialism_methods()):
            if method_name in _ACCOUNT_STORE_METHODS:
                continue
            if method_name == "get_products":
                continue
            self.__dict__[method_name] = self._make_delegate(method_name)

    async def _resolve_tenant_platform(
        self, ctx: RequestContext[Any], method_name: str
    ) -> DecisioningPlatform:
        """Resolve tenant, gate on health, return the child platform."""
        tenant_id = ctx.tenant_id
        if not tenant_id:
            raise AdcpError(
                "ACCOUNT_NOT_FOUND",
                message=(
                    "RegistryPlatformAdapter: ctx.tenant_id is unset. The transport "
                    "layer must populate tenant_id (typically from the Host header) "
                    "before dispatch. Check your SubdomainTenantMiddleware or "
                    "context_factory wiring."
                ),
                recovery="terminal",
                field="ctx.tenant_id",
            )
        resolution = await self._registry.resolve_by_id(tenant_id)
        # Intentional topology hiding: unregistered tenants produce
        # SERVICE_UNAVAILABLE rather than ACCOUNT_NOT_FOUND, matching the
        # behaviour when a tenant is known but health-gated. From the buyer's
        # perspective, "doesn't exist" and "temporarily unavailable" are
        # indistinguishable — this avoids leaking which tenant_ids are
        # enrolled with this seller. (Contrast: PlatformRouter raises
        # ACCOUNT_NOT_FOUND for unknown tenants because its platform list is
        # a closed static set, not a dynamic registry.)
        if resolution is None or resolution.health not in self._serve_states:
            health = resolution.health if resolution is not None else None
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message=(
                    f"RegistryPlatformAdapter: tenant {tenant_id!r} is not available "
                    f"(health={health!r}). Allowed states: {sorted(self._serve_states)}."
                ),
                recovery="transient",
                details={"tenant_id": tenant_id, "health": health},
            )
        # Guard catches tenants whose resolved platform doesn't implement the
        # requested specialism method (e.g. a tenant registered as a
        # SignalsPlatform-only seller being asked for get_products). It fires
        # for any method_name dispatched via _make_delegate, not just
        # get_products.
        method = getattr(resolution.platform, method_name, None)
        if method is None or not callable(method):
            raise AdcpError(
                "UNSUPPORTED_FEATURE",
                message=(
                    f"RegistryPlatformAdapter: tenant {tenant_id!r}'s platform "
                    f"({type(resolution.platform).__name__!r}) does not implement "
                    f"{method_name!r}."
                ),
                recovery="terminal",
            )
        return resolution.platform  # type: ignore[no-any-return]

    def _make_delegate(self, method_name: str) -> Any:
        adapter = self

        async def _delegate(*args: Any, **kwargs: Any) -> Any:
            ctx = _resolve_ctx_from_args(args, kwargs)
            platform = await adapter._resolve_tenant_platform(ctx, method_name)
            method = getattr(platform, method_name)
            if inspect.iscoroutinefunction(method):
                return await method(*args, **kwargs)
            return await asyncio.to_thread(method, *args, **kwargs)

        _delegate.__name__ = method_name
        _delegate.__qualname__ = f"_RegistryPlatformAdapter.{method_name}"
        return _delegate

    async def refine_get_products(self, *args: Any, **kwargs: Any) -> Any:
        """Refine entry point — delegates to get_products.

        Mirrors :meth:`PlatformRouter.refine_get_products` so the
        handler's refine dispatch path works correctly.
        """
        return await self.get_products(*args, **kwargs)

    async def get_products(self, *args: Any, **kwargs: Any) -> Any:
        """Per-tenant get_products dispatch.

        Explicit override (not synthesized) to mirror PlatformRouter's
        pattern — keeps the refine_get_products / get_products chain
        intact without proposal_manager support (which the registry
        adapter doesn't need).
        """
        ctx = _resolve_ctx_from_args(args, kwargs)
        platform = await self._resolve_tenant_platform(ctx, "get_products")
        method = getattr(platform, "get_products")
        if inspect.iscoroutinefunction(method):
            return await method(*args, **kwargs)
        return await asyncio.to_thread(method, *args, **kwargs)


def _make_registry_platform_adapter(
    registry: Any,
    *,
    accounts: Any,
    capabilities: DecisioningCapabilities,
    serve_states: frozenset[Any],
) -> DecisioningPlatform:
    """Factory for :meth:`TenantRegistry.as_platform`.

    Accepts the registry as ``Any`` to avoid a circular import at module
    load time — :mod:`adcp.server.tenant_registry` calls this function
    lazily from inside :meth:`TenantRegistry.as_platform`, not at import
    time, so no circular dependency arises.
    """
    return _RegistryPlatformAdapter(
        registry=registry,
        accounts=accounts,
        capabilities=capabilities,
        serve_states=serve_states,
    )


__all__ = ["LazyPlatformRouter", "PlatformRouter"]
