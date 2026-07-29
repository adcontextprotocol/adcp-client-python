"""TenantRegistry — higher-level multi-tenant management primitive.

Provides JS ``createTenantRegistry`` parity for Python multi-tenant
deployments. Composes per-tenant health tracking with runtime
register/unregister/recheck, closing the JS↔Python parity gap on the
most-touched server-side primitive.

Adopters pre-build per-tenant :class:`~adcp.decisioning.DecisioningPlatform`
instances and register them here (eager mode), or supply a factory callable
that is invoked on first request (lazy mode). The registry tracks health
state and surfaces :meth:`TenantRegistry.resolve_by_host` (sync, eager) or
:meth:`TenantRegistry.resolve` (async, both modes) for the request path.

Comparison with lower-level building blocks:

* :class:`~adcp.server.CallableSubdomainTenantRouter` — host→Tenant lookup
  with TTL cache. Suitable when tenant routing is all you need.
* :class:`~adcp.decisioning.LazyPlatformRouter` — per-tenant
  :class:`~adcp.decisioning.DecisioningPlatform` factory with LRU+TTL
  cache. Suitable when you need lazy platform construction without health
  tracking.
* :class:`TenantRegistry` — combines health tracking + runtime mutation
  (register/unregister/recheck) with optional lazy platform construction.
  Reach for this when multi-tenant SaaS topology requires observability
  into per-tenant health state and runtime admin operations without a
  process restart. Lazy mode (``register_lazy``) avoids paying per-tenant
  platform-build cost at boot.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from adcp.server.tenant_router import normalize_host_key

if TYPE_CHECKING:
    from adcp.decisioning.accounts import AccountStore
    from adcp.decisioning.platform import DecisioningCapabilities, DecisioningPlatform

logger = logging.getLogger(__name__)

#: Per-tenant health state. See :class:`TenantRegistry` for semantics.
TenantHealthState = Literal["pending", "healthy", "unverified", "disabled"]

#: Default health states that the :meth:`TenantRegistry.as_platform` adapter
#: will serve. ``healthy`` and ``unverified`` allow traffic; ``pending`` and
#: ``disabled`` raise ``SERVICE_UNAVAILABLE``.
_DEFAULT_SERVE_STATES: frozenset[TenantHealthState] = frozenset({"healthy", "unverified"})

#: Validator callable. Takes ``(tenant_id, agent_url)`` and returns
#: ``True`` when the tenant is valid, ``False`` otherwise.
#: May be sync or async — the registry awaits at call time.
TenantValidator = Callable[[str, str], "bool | Awaitable[bool]"]

#: Lazy platform factory callable. Takes ``tenant_id`` and returns an
#: awaitable :class:`~adcp.decisioning.DecisioningPlatform`. Used with
#: :meth:`TenantRegistry.register_lazy` to defer per-tenant platform
#: construction until first request.
PlatformFactory = Callable[[str], "Awaitable[DecisioningPlatform]"]


@dataclass(frozen=True)
class TenantResolution:
    """Result of :meth:`TenantRegistry.resolve_by_host`.

    :param tenant_id: Stable identifier for the resolved tenant.
    :param health: Current health state. Callers gate traffic on this —
        typically 503 for ``pending`` and ``disabled``, serve for
        ``healthy`` and ``unverified``.
    :param platform: The :class:`~adcp.decisioning.DecisioningPlatform`
        for this tenant. Pass to :func:`adcp.decisioning.serve` or use
        with a :class:`~adcp.decisioning.PlatformRouter`.
    """

    tenant_id: str
    health: TenantHealthState
    platform: DecisioningPlatform


class TenantRegistry:
    """Higher-level multi-tenant primitive with health tracking.

    Mirrors JS SDK ``createTenantRegistry`` for Python deployments.
    Supports two registration modes:

    * **Eager** (:meth:`register`) — caller pre-builds the
      :class:`~adcp.decisioning.DecisioningPlatform` and passes it in.
      :meth:`resolve_by_host` (sync) and :meth:`resolve` (async) both
      return a resolution immediately.
    * **Lazy** (:meth:`register_lazy`) — caller supplies a factory
      callable; the platform is built on the first :meth:`resolve` call
      and cached. Avoids paying per-tenant construction costs (network
      handshakes, KMS credential fetches) at boot. Suitable for
      deployments with many tenants.

    **Health states:**

    * ``pending`` — registered, not yet validated (or lazy factory not
      yet invoked). Adopters should 503 traffic until validation
      completes.
    * ``healthy`` — validated and serving.
    * ``unverified`` — was healthy; a subsequent :meth:`recheck` failed
      (transient failure). The tenant still serves (graceful-degrade).
    * ``disabled`` — persistent failure. 503 until an operator calls
      :meth:`recheck` and validation succeeds.

    **Validator:** Optional callable ``(tenant_id, agent_url) -> bool``.
    Pass a JWKS health-check, a connectivity probe, or any custom
    validation logic. Adopters using principal-token bearer auth (no
    JWKS) pass ``None`` — validation always succeeds immediately so
    ``await_first_validation=True`` transitions the tenant to
    ``healthy`` without a network round-trip.

    **Per-tenant locks:** Each tenant gets an ``asyncio.Lock`` on first
    use. Locks are removed when the tenant is unregistered. Any
    in-flight :meth:`recheck` or :meth:`resolve` that held the lock
    before ``unregister()`` was called completes safely — zombie-entry
    guards in both methods prevent stale writes after removal.

    **Do not pass a TenantRegistry as a SubdomainTenantRouter.**
    Both classes expose ``async def resolve(host)``, but the return types
    are incompatible (:class:`TenantResolution` vs :class:`Tenant`).
    Mypy will flag the mismatch; duck-typing and ``isinstance`` checks
    will not.

    :param validator: Optional validation callable (sync or async).
        ``None`` → principal-token mode; validation always succeeds.
    :param default_serve_options: Optional dict of defaults to store for
        adopter convenience. Retrieve via :attr:`serve_options`.

    Example (using :meth:`as_platform` — recommended path for
    :func:`~adcp.decisioning.serve` integration)::

        from adcp.server import TenantRegistry
        from adcp.decisioning import serve

        registry = TenantRegistry(validator=check_jwks)

        for tenant in load_tenants_from_db():
            # await_first_validation=True pre-warms tenants at boot so the
            # first request doesn't see health='pending'.
            await registry.register_lazy(
                tenant.id,
                agent_url=tenant.agent_url,
                factory=build_platform_for_tenant,
                await_first_validation=True,
            )

        # Returns a DecisioningPlatform that routes per-request via
        # ctx.tenant_id (set from the Host header by SubdomainTenantMiddleware).
        serve(registry.as_platform(accounts=my_account_store), port=8080)

    Example (escape-hatch — manual resolve() when you need custom dispatch)::

        from adcp.server import TenantRegistry

        registry = TenantRegistry(validator=None)

        for tenant in load_tenants_from_db():
            await registry.register(
                tenant.id,
                agent_url=tenant.agent_url,
                platform=build_platform_for(tenant),
                await_first_validation=True,
            )

        async def resolve(ctx):
            # Use resolve_by_id when tenant_id is already known (e.g. from
            # ctx.tenant_id); use resolve(host) for host-based lookup.
            resolved = await registry.resolve_by_id(ctx.tenant_id)
            if resolved is None or resolved.health in ("pending", "disabled"):
                raise HTTPException(503)
            return resolved.platform

    Example (runtime admin operations)::

        # Hot-add a newly onboarded tenant
        await registry.register(new_id, agent_url=..., platform=...)

        # Remove a deactivated tenant
        registry.unregister(old_id)

        # Re-validate after key rotation
        await registry.recheck(rotated_id)
        status = registry.health(rotated_id)
    """

    def __init__(
        self,
        *,
        validator: TenantValidator | None = None,
        default_serve_options: dict[str, Any] | None = None,
    ) -> None:
        self._validator = validator
        self._default_serve_options: dict[str, Any] = default_serve_options or {}
        # Per-tenant health state.
        self._health: dict[str, TenantHealthState] = {}
        # Per-tenant DecisioningPlatform. Annotation uses TYPE_CHECKING import;
        # safe because from __future__ import annotations makes it a lazy string.
        self._platforms: dict[str, DecisioningPlatform] = {}
        # Per-tenant agent_url — used to derive + update host_map entries.
        self._agent_urls: dict[str, str] = {}
        # Normalized host → tenant_id for O(1) resolve_by_host lookups.
        self._host_map: dict[str, str] = {}
        # Per-tenant asyncio.Lock for TOCTOU-safe state transitions.
        # State mutations (register, recheck) read, await I/O, then write;
        # without a lock two concurrent rechecks for the same tenant could
        # both read, both await, and both commit — racing on the final state.
        self._locks: dict[str, asyncio.Lock] = {}
        # Per-tenant lazy platform factory. Set by register_lazy(); absent
        # for tenants registered eagerly via register().
        self._factories: dict[str, PlatformFactory] = {}

    # ----- internal helpers ------------------------------------------------

    def _get_lock(self, tenant_id: str) -> asyncio.Lock:
        # No await between the check and the insertion, so this is safe
        # under asyncio cooperative scheduling (single event loop thread).
        if tenant_id not in self._locks:
            self._locks[tenant_id] = asyncio.Lock()
        return self._locks[tenant_id]

    @staticmethod
    def _normalize_host(raw: str) -> str:
        """Reduce a host or URL to its tenant-lookup key.

        Accepts both full URLs (``https://acme.example.com``) and raw
        Host-header values (``acme.example.com``, ``acme.example.com:443``).
        Delegates to :func:`~adcp.server.tenant_router.normalize_host_key`
        so that a tenant registered by ``agent_url`` is reachable by the
        ``Host`` header the subdomain router resolves — the two used to
        key the same address differently.

        Beyond lower-casing and port stripping this discards any
        ``user:pw@`` userinfo, removes IPv6 brackets (``[::1]:8443`` is
        keyed as ``::1``), and folds a trailing FQDN-root dot.

        Note: port stripping is correct for ``Host`` headers where the port
        matches the scheme default. Some load-balancers forward
        ``X-Forwarded-Host`` with non-default ports preserved; callers
        using that header should strip the port themselves before passing
        the value to :meth:`resolve_by_host` or :meth:`resolve`.
        """
        return normalize_host_key(raw)

    async def _run_validator(self, tenant_id: str) -> bool:
        """Invoke the configured validator; return True when valid."""
        if self._validator is None:
            return True
        agent_url = self._agent_urls.get(tenant_id, "")
        result = self._validator(tenant_id, agent_url)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    # ----- public API ------------------------------------------------------

    async def register(
        self,
        tenant_id: str,
        *,
        agent_url: str,
        platform: DecisioningPlatform,
        await_first_validation: bool = False,
    ) -> None:
        """Register a tenant.

        Health starts as ``pending``. When ``await_first_validation=True``
        the coroutine suspends until the validator resolves, then
        transitions to ``healthy`` or ``disabled`` before returning — the
        next :meth:`resolve_by_host` call sees the final state.

        Re-registering an existing tenant atomically replaces its platform
        and agent_url under the per-tenant lock. The old host-map entry is
        removed if the URL changed.

        :param tenant_id: Stable identifier (e.g. DB primary key).
        :param agent_url: The tenant's agent endpoint URL. The host
            component is extracted and used as the key for
            :meth:`resolve_by_host` lookups.
        :param platform: Pre-built
            :class:`~adcp.decisioning.DecisioningPlatform` for this tenant.
        :param await_first_validation: When ``True``, suspends the caller
            until validation completes (not "blocks the event loop" — the
            coroutine yields cooperatively while awaiting I/O). Useful at
            boot so the first incoming request doesn't race the validation
            roundtrip. The typical ``False`` default is correct for
            background hot-add where traffic is gated on ``health != 'pending'``.
        """
        lock = self._get_lock(tenant_id)
        async with lock:
            # Remove stale host-map entry when the URL changes.
            old_url = self._agent_urls.get(tenant_id)
            if old_url is not None and old_url != agent_url:
                self._host_map.pop(self._normalize_host(old_url), None)

            self._platforms[tenant_id] = platform
            self._agent_urls[tenant_id] = agent_url
            self._host_map[self._normalize_host(agent_url)] = tenant_id
            self._health[tenant_id] = "pending"
            # Clear any lazy factory if re-registering as eager.
            self._factories.pop(tenant_id, None)

            if await_first_validation:
                try:
                    ok = await self._run_validator(tenant_id)
                except Exception:
                    logger.warning(
                        "TenantRegistry.register: validator raised for tenant %r; "
                        "health=disabled",
                        tenant_id,
                        exc_info=True,
                    )
                    self._health[tenant_id] = "disabled"
                    return
                self._health[tenant_id] = "healthy" if ok else "disabled"

    async def register_lazy(
        self,
        tenant_id: str,
        *,
        agent_url: str,
        factory: PlatformFactory,
        await_first_validation: bool = False,
    ) -> None:
        """Register a tenant with a lazy platform factory.

        The platform is built on the first :meth:`resolve` call for this
        tenant's host, then cached. Subsequent resolves return the cached
        instance. Suitable for deployments with many tenants where eager
        construction is too expensive at boot — network handshakes, KMS
        credential fetches, inventory-manager construction, etc.

        Health starts as ``pending``. When ``await_first_validation=True``
        the factory is invoked immediately, the platform is built, and
        validation completes before returning — the next :meth:`resolve`
        call sees the final state without triggering the factory again.

        Use :meth:`resolve` (async) to get a :class:`TenantResolution`
        for lazy-registered tenants; the synchronous :meth:`resolve_by_host`
        returns ``None`` until the platform is built.

        Lazy and eager tenants share the same health state machine:
        :meth:`health`, :meth:`unregister`, :meth:`recheck`, and
        :attr:`registered_tenants` work identically regardless of
        registration mode.

        Re-registering an existing tenant (eager or lazy) atomically
        replaces its factory and agent_url under the per-tenant lock.

        :param tenant_id: Stable identifier (e.g. DB primary key).
        :param agent_url: The tenant's agent endpoint URL. The host
            component is extracted for :meth:`resolve` / :meth:`resolve_by_host`.
        :param factory: Async callable ``(tenant_id) -> DecisioningPlatform``.
            Called at most once per registration (not once per request).
        :param await_first_validation: When ``True``, invokes the factory
            and validator immediately before returning.
        """
        lock = self._get_lock(tenant_id)
        async with lock:
            old_url = self._agent_urls.get(tenant_id)
            if old_url is not None and old_url != agent_url:
                self._host_map.pop(self._normalize_host(old_url), None)

            self._factories[tenant_id] = factory
            # Clear any eagerly-built platform if re-registering as lazy.
            self._platforms.pop(tenant_id, None)
            self._agent_urls[tenant_id] = agent_url
            self._host_map[self._normalize_host(agent_url)] = tenant_id
            self._health[tenant_id] = "pending"

            if await_first_validation:
                try:
                    platform = await factory(tenant_id)
                    ok = await self._run_validator(tenant_id)
                except Exception:
                    logger.warning(
                        "TenantRegistry.register_lazy: factory/validator raised for "
                        "tenant %r; health=disabled",
                        tenant_id,
                        exc_info=True,
                    )
                    self._health[tenant_id] = "disabled"
                    self._factories.pop(tenant_id, None)
                    return
                if ok:
                    self._platforms[tenant_id] = platform
                    self._factories.pop(tenant_id, None)
                    self._health[tenant_id] = "healthy"
                else:
                    # Validator rejected the platform — discard it and clear the
                    # factory to mirror resolve() cold-path behavior: a disabled
                    # lazy tenant needs register_lazy() + recheck() to recover.
                    self._health[tenant_id] = "disabled"
                    self._factories.pop(tenant_id, None)

    def unregister(self, tenant_id: str) -> None:
        """Remove a tenant from the registry.

        Callers that already hold a reference to the tenant's platform
        (e.g. an in-flight request that called :meth:`resolve_by_host`
        before this call) complete normally — the registry does not cancel
        in-flight work. Subsequent :meth:`resolve_by_host` calls for this
        host return ``None``.

        Safe to call when the tenant is not registered (no-op).
        """
        agent_url = self._agent_urls.pop(tenant_id, None)
        if agent_url is not None:
            self._host_map.pop(self._normalize_host(agent_url), None)
        self._platforms.pop(tenant_id, None)
        self._factories.pop(tenant_id, None)
        self._health.pop(tenant_id, None)
        self._locks.pop(tenant_id, None)

    async def recheck(self, tenant_id: str) -> None:
        """Re-validate a tenant after key rotation or config change.

        **State transitions on validator success:** any state → ``healthy``.

        **State transitions on validator failure or exception:**

        * ``healthy`` → ``unverified`` (was serving; graceful-degrade so
          existing traffic keeps flowing while the operator investigates).
        * ``pending`` / ``unverified`` / ``disabled`` → ``disabled``
          (no prior healthy baseline; fail closed).

        The health state is updated before any exception propagates, so
        the state is always consistent even when the validator raises.

        **Lazy-tenant caveats:**

        * For a lazy tenant in ``pending`` state (factory never invoked),
          ``recheck()`` runs the validator against the registered
          ``agent_url`` only. If it succeeds, health advances to
          ``healthy`` — but the platform has not been built yet.
          :meth:`resolve_by_host` still returns ``None``; use the async
          :meth:`resolve` which triggers the factory on first call.
        * For a lazy tenant that reached ``disabled`` via factory failure,
          the factory has been cleared. Calling ``recheck()`` alone is
          insufficient to recover — the validator may succeed but there
          is no platform to serve. To retry platform construction, call
          :meth:`register_lazy` again with the same factory, then call
          :meth:`recheck` if you also need to re-run the validator.

        :raises KeyError: when ``tenant_id`` is not registered.
        :raises Exception: re-raises any exception from the validator
            after updating the health state.
        """
        if tenant_id not in self._health:
            raise KeyError(f"Tenant {tenant_id!r} is not registered")

        lock = self._get_lock(tenant_id)
        async with lock:
            # Re-check inside the lock — unregister may have raced.
            if tenant_id not in self._health:
                raise KeyError(f"Tenant {tenant_id!r} is not registered")
            prior = self._health[tenant_id]
            try:
                ok = await self._run_validator(tenant_id)
            except Exception:
                # Guard: unregister() may have run while we awaited the validator.
                # If so, _health no longer has this tenant — writing back would
                # create a zombie entry visible via health() / registered_tenants.
                if tenant_id not in self._health:
                    return
                self._health[tenant_id] = (
                    "unverified" if prior == "healthy" else "disabled"
                )
                logger.warning(
                    "TenantRegistry.recheck: validator raised for tenant %r; "
                    "health=%s",
                    tenant_id,
                    self._health[tenant_id],
                    exc_info=True,
                )
                raise
            # Same guard for the success path.
            if tenant_id not in self._health:
                return
            if ok:
                self._health[tenant_id] = "healthy"
            else:
                self._health[tenant_id] = (
                    "unverified" if prior == "healthy" else "disabled"
                )

    def health(self, tenant_id: str) -> TenantHealthState | None:
        """Return the current health state for ``tenant_id``.

        Returns ``None`` when the tenant is not registered (distinct from
        any health state value — callers can use ``is None`` to detect
        unknown tenants).
        """
        return self._health.get(tenant_id)

    def resolve_by_host(self, host: str) -> TenantResolution | None:
        """Synchronous lookup by ``Host`` header value.

        Returns ``None`` when no tenant is registered for this host.
        The caller is responsible for checking ``result.health`` and
        gating traffic as appropriate — the registry does not 503
        automatically (health-gating belongs in the adopter's request
        dispatch layer).

        The lookup is synchronous because the registry maintains its own
        in-memory host → tenant mapping (updated eagerly by
        :meth:`register` and :meth:`unregister`). This intentionally
        departs from the JS SDK's async variant, which must call an
        external resolver; the Python registry owns the mapping directly.

        :param host: Raw ``Host`` header value. Port suffixes are stripped
            before lookup; the string may also be a full URL.
        """
        normalized = self._normalize_host(host)
        tenant_id = self._host_map.get(normalized)
        if tenant_id is None:
            return None
        platform = self._platforms.get(tenant_id)
        if platform is None:
            return None
        health = self._health.get(tenant_id, "pending")
        return TenantResolution(tenant_id=tenant_id, health=health, platform=platform)

    async def resolve(self, host: str) -> TenantResolution | None:
        """Async lookup by ``Host`` header value; builds lazy platforms on first hit.

        For eager tenants (registered via :meth:`register`), equivalent to
        :meth:`resolve_by_host` at an async call site — no I/O occurs.

        For lazy tenants (registered via :meth:`register_lazy`), the
        platform factory is invoked on the first call, then cached.
        Concurrent first-hit resolves for the same tenant serialize on
        the per-tenant lock — only one factory invocation occurs.

        Returns ``None`` when no tenant is registered for this host, or when
        a lazy tenant's factory/validator fails on this call (health set to
        ``disabled`` in both cases).

        Returns a :class:`TenantResolution` — which may have
        ``health="disabled"`` — when the platform was already built (eager
        registration, lazy + ``await_first_validation=True``, or a previous
        :meth:`resolve` call). **Always check ``result.health`` before
        serving; never gate solely on ``result is None``.**

        The caller is responsible for gating traffic — the registry does
        not 503 automatically.

        :param host: Raw ``Host`` header value. Port suffixes are stripped;
            full URLs are also accepted. See :meth:`_normalize_host` for
            load-balancer caveats.
        """
        normalized = self._normalize_host(host)
        tenant_id = self._host_map.get(normalized)
        if tenant_id is None:
            return None

        # Fast path: platform already built (eager or previously-resolved lazy).
        platform = self._platforms.get(tenant_id)
        if platform is not None:
            health = self._health.get(tenant_id, "pending")
            return TenantResolution(tenant_id=tenant_id, health=health, platform=platform)

        # If there is no factory either, nothing to do.
        if tenant_id not in self._factories:
            return None

        # Lazy path: acquire per-tenant lock to serialize concurrent first-hit
        # resolves — only one factory invocation per tenant.
        lock = self._get_lock(tenant_id)
        async with lock:
            # Double-check: another coroutine may have built the platform
            # while we waited for the lock.
            platform = self._platforms.get(tenant_id)
            if platform is not None:
                health = self._health.get(tenant_id, "pending")
                return TenantResolution(
                    tenant_id=tenant_id, health=health, platform=platform
                )

            # Guard: unregister() may have run while we waited.
            if tenant_id not in self._health:
                return None

            factory = self._factories.get(tenant_id)
            if factory is None:
                return None

            try:
                platform = await factory(tenant_id)
            except Exception:
                logger.warning(
                    "TenantRegistry.resolve: factory raised for tenant %r; "
                    "health=disabled",
                    tenant_id,
                    exc_info=True,
                )
                if tenant_id in self._health:
                    self._health[tenant_id] = "disabled"
                    # Drop the factory so subsequent resolve() calls don't re-invoke
                    # it — a disabled tenant needs operator intervention via recheck().
                    self._factories.pop(tenant_id, None)
                return None

            try:
                ok = await self._run_validator(tenant_id)
            except Exception:
                logger.warning(
                    "TenantRegistry.resolve: validator raised for tenant %r; "
                    "health=disabled",
                    tenant_id,
                    exc_info=True,
                )
                if tenant_id in self._health:
                    self._health[tenant_id] = "disabled"
                    self._factories.pop(tenant_id, None)
                return None

            # Guard: unregister() may have run while we awaited factory/validator.
            if tenant_id not in self._health:
                return None

            if ok:
                self._platforms[tenant_id] = platform
                self._health[tenant_id] = "healthy"
                # Factory no longer needed — platform is cached in _platforms.
                self._factories.pop(tenant_id, None)
                return TenantResolution(
                    tenant_id=tenant_id, health="healthy", platform=platform
                )
            else:
                self._health[tenant_id] = "disabled"
                self._factories.pop(tenant_id, None)
                return None

    async def resolve_by_id(self, tenant_id: str) -> TenantResolution | None:
        """Async lookup by ``tenant_id``; builds lazy platforms on first hit.

        Equivalent to :meth:`resolve` but accepts a ``tenant_id`` string
        directly instead of a ``Host`` header value. Used by the
        :meth:`as_platform` adapter to resolve per-request platforms keyed
        on ``ctx.tenant_id`` (set by the transport layer from the Host
        header) rather than re-doing the host → tenant_id lookup.

        For eager tenants this is a synchronous in-memory lookup wrapped in
        a coroutine — no I/O occurs. For lazy tenants (registered via
        :meth:`register_lazy`) the factory is invoked on the first call and
        the result is cached, with concurrent first-hit calls serialised on
        the per-tenant lock.

        Returns ``None`` when the tenant is not registered or when a lazy
        tenant's factory or validator fails.

        Returns a :class:`TenantResolution` — which may have any health
        state — when the platform is available. **Always check
        ``result.health`` before serving.**

        :param tenant_id: Stable tenant identifier as registered via
            :meth:`register` or :meth:`register_lazy`.
        """
        if tenant_id not in self._health:
            return None

        # Fast path: platform already built (eager or previously resolved lazy).
        platform = self._platforms.get(tenant_id)
        if platform is not None:
            health = self._health.get(tenant_id, "pending")
            return TenantResolution(tenant_id=tenant_id, health=health, platform=platform)

        # No factory either — lazy tenant that disabled or cleared itself.
        if tenant_id not in self._factories:
            return None

        # Lazy path: mirrors resolve()'s concurrent first-hit serialisation,
        # skipping the host_map lookup since we already have the tenant_id.
        lock = self._get_lock(tenant_id)
        async with lock:
            platform = self._platforms.get(tenant_id)
            if platform is not None:
                health = self._health.get(tenant_id, "pending")
                return TenantResolution(
                    tenant_id=tenant_id, health=health, platform=platform
                )

            if tenant_id not in self._health:
                return None

            factory = self._factories.get(tenant_id)
            if factory is None:
                return None

            try:
                platform = await factory(tenant_id)
            except Exception:
                logger.warning(
                    "TenantRegistry.resolve_by_id: factory raised for tenant %r; "
                    "health=disabled",
                    tenant_id,
                    exc_info=True,
                )
                if tenant_id in self._health:
                    self._health[tenant_id] = "disabled"
                    self._factories.pop(tenant_id, None)
                return None

            try:
                ok = await self._run_validator(tenant_id)
            except Exception:
                logger.warning(
                    "TenantRegistry.resolve_by_id: validator raised for tenant %r; "
                    "health=disabled",
                    tenant_id,
                    exc_info=True,
                )
                if tenant_id in self._health:
                    self._health[tenant_id] = "disabled"
                    self._factories.pop(tenant_id, None)
                return None

            if tenant_id not in self._health:
                return None

            if ok:
                self._platforms[tenant_id] = platform
                self._health[tenant_id] = "healthy"
                self._factories.pop(tenant_id, None)
                return TenantResolution(
                    tenant_id=tenant_id, health="healthy", platform=platform
                )
            else:
                self._health[tenant_id] = "disabled"
                self._factories.pop(tenant_id, None)
                return None

    def as_platform(
        self,
        *,
        accounts: AccountStore[Any],
        capabilities: DecisioningCapabilities | None = None,
        serve_states: frozenset[TenantHealthState] = _DEFAULT_SERVE_STATES,
    ) -> DecisioningPlatform:
        """Return a :class:`~adcp.decisioning.DecisioningPlatform` backed by this registry.

        The returned platform resolves the per-request tenant via
        ``ctx.tenant_id`` (populated by the transport layer from the
        ``Host`` header or URL path), applies health gating, and forwards
        every specialism method call to the resolved tenant's platform.
        Pass it directly to :func:`adcp.decisioning.serve`::

            registry = TenantRegistry(validator=check_jwks)
            for tenant in load_tenants():
                await registry.register_lazy(
                    tenant.id, agent_url=tenant.url, factory=build_platform
                )

            serve(registry.as_platform(accounts=my_account_store), port=8080)

        **Tenant resolution.** The adapter reads ``ctx.tenant_id``, which
        the transport layer sets from the ``Host`` header (via
        :class:`~adcp.server.SubdomainTenantMiddleware`) or your custom
        ``context_factory``. **This value must equal the ``tenant_id``
        string you passed as the first argument to** :meth:`register` **/**
        :meth:`register_lazy`. The host itself (e.g. ``"acme.example.com"``)
        is NOT used — only the registry key (e.g. ``"acme"``).

        If you use :class:`~adcp.server.SubdomainTenantMiddleware`, wire
        ``tenant_id`` in your ``context_factory`` like this::

            from adcp.server import current_tenant

            def context_factory(request):
                t = current_tenant()
                return {"tenant_id": t.id if t else None}

        The ``Tenant.id`` value (from your
        :class:`~adcp.server.SubdomainTenantRouter`) must match the key
        you registered — ``register_lazy("acme", ...)`` requires
        ``Tenant(id="acme", ...)``, not ``Tenant(id="acme.example.com", ...)``.
        A mismatch silently produces ``SERVICE_UNAVAILABLE`` with
        ``health=None`` on every request.

        **Health gating.** By default the adapter serves ``healthy`` and
        ``unverified`` tenants, and raises ``SERVICE_UNAVAILABLE`` for
        ``pending`` and ``disabled`` tenants. Override via
        ``serve_states`` for fail-closed behaviour::

            # Serve only fully-validated tenants (fail-closed):
            registry.as_platform(
                accounts=store,
                serve_states=frozenset({"healthy"}),
            )

        **``accounts`` parameter.** :func:`~adcp.decisioning.serve` validates
        ``platform.accounts`` at boot time before any request arrives. Pass
        the same tenant-aware :class:`~adcp.decisioning.AccountStore` you
        would pass to :class:`~adcp.decisioning.PlatformRouter` — typically
        one that reads ``tenant_id`` from the resolved account's metadata or
        from the transport-layer ``current_tenant`` ContextVar.

        **``capabilities`` parameter.** Should be the union of all tenants'
        specialisms. The adapter cannot introspect child platforms at
        boot time, so the adopter is the source of truth. Defaults to an
        empty :class:`~adcp.decisioning.DecisioningCapabilities` (no
        specialisms advertised) — pass the full union for accurate
        ``tools/list`` projection.

        :param accounts: The :class:`~adcp.decisioning.AccountStore` for the
            returned platform. Required by framework boot-time validation.
        :param capabilities: Capability declaration for the adapter. Defaults
            to an empty :class:`~adcp.decisioning.DecisioningCapabilities`.
        :param serve_states: Health states for which requests proceed.
            Default is ``frozenset({"healthy", "unverified"})``.
        :returns: A :class:`~adcp.decisioning.DecisioningPlatform` suitable
            for passing to :func:`adcp.decisioning.serve`.
        """
        from adcp.decisioning.platform import DecisioningCapabilities as _DecisioningCapabilities
        from adcp.decisioning.platform_router import _make_registry_platform_adapter

        if capabilities is None:
            logger.warning(
                "TenantRegistry.as_platform: no capabilities= passed; tools/list will "
                "advertise no tools. Pass capabilities=DecisioningCapabilities(specialisms=[...]) "
                "for accurate tools/list projection."
            )
        cap = capabilities if capabilities is not None else _DecisioningCapabilities()
        return _make_registry_platform_adapter(
            self,
            accounts=accounts,
            capabilities=cap,
            serve_states=frozenset(serve_states),
        )

    @property
    def serve_options(self) -> dict[str, Any]:
        """The ``default_serve_options`` dict passed at construction.

        Convenience accessor for single-tenant setups or when spreading
        common options into :func:`adcp.decisioning.serve`::

            serve(platform, **registry.serve_options)

        Multi-tenant deployments typically pass a router (not a single
        platform) to ``serve()``; in that case these options are consumed
        by the per-request dispatch layer rather than passed to ``serve``
        directly.

        Returns an empty dict when no options were passed at construction.
        Returns a shallow copy — mutations to the returned dict do not
        affect the registry's stored options.
        """
        return dict(self._default_serve_options)

    @property
    def registered_tenants(self) -> frozenset[str]:
        """Snapshot of the currently registered tenant ids.

        Read-only — mutations to the registry after this property is read
        are not reflected in the returned frozenset.
        """
        return frozenset(self._health)


__all__ = [
    "PlatformFactory",
    "TenantHealthState",
    "TenantRegistry",
    "TenantResolution",
    "TenantValidator",
]
