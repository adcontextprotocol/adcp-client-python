"""Tests for :class:`adcp.decisioning.LazyPlatformRouter`.

The lazy variant of :class:`PlatformRouter`: defers per-tenant
platform construction to first request, with a bounded LRU + TTL
cache. These tests cover:

* Drop-in compatibility — ``isinstance(router, DecisioningPlatform)``.
* Lazy construction — factory called once per cold tenant.
* Cache semantics — second call hits cache; LRU eviction past
  ``cache_size``; TTL expiry; ``cache_ttl_seconds=0`` size-only mode.
* Async + sync factory; async + sync child platform methods.
* ``invalidate(tenant_id)`` and ``invalidate()``.
* Construction validation — ``cache_size <= 0`` and
  ``cache_ttl_seconds < 0`` rejected.
* Factory rejection paths — ``None`` return, wrong type return.
* ``proposal_managers`` routing through the lazy resolver.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    ExplicitAccounts,
    LazyPlatformRouter,
    RequestContext,
    SalesPlatform,
)
from adcp.decisioning.types import Account


def _capabilities(specialisms: list[str]) -> DecisioningCapabilities:
    from adcp.decisioning.capabilities import (
        Adcp,
        IdempotencyUnsupported,
        SupportedProtocol,
    )

    return DecisioningCapabilities(
        specialisms=specialisms,
        adcp=Adcp(
            major_versions=[3],
            idempotency=IdempotencyUnsupported(supported=False),
        ),
        supported_protocols=[SupportedProtocol.media_buy],
    )


class _SyncSalesPlatform(DecisioningPlatform, SalesPlatform):
    """Sync child platform — minimum sales-non-guaranteed surface."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[tuple[str, Any]] = []

    capabilities = _capabilities(["sales-non-guaranteed"])
    accounts = ExplicitAccounts(loader=lambda _id: Account(id=_id))

    def get_products(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        self.calls.append(("get_products", ctx.account.id))
        return {"products": [{"product_id": f"prod-{self.tag}"}]}

    def create_media_buy(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        self.calls.append(("create_media_buy", ctx.account.id))
        return {"media_buy_id": f"mb-{self.tag}", "status": "active"}

    def update_media_buy(
        self, media_buy_id: str, patch: Any, ctx: RequestContext[Any]
    ) -> dict[str, Any]:
        self.calls.append(("update_media_buy", ctx.account.id))
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        self.calls.append(("sync_creatives", ctx.account.id))
        return {"creatives": []}

    def get_media_buy_delivery(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        self.calls.append(("get_media_buy_delivery", ctx.account.id))
        return {"media_buy_deliveries": []}


class _AsyncSalesPlatform(_SyncSalesPlatform):
    async def get_products(  # type: ignore[override]
        self, req: Any, ctx: RequestContext[Any]
    ) -> dict[str, Any]:
        self.calls.append(("get_products", ctx.account.id))
        return {"products": [{"product_id": f"prod-async-{self.tag}"}]}

    async def create_media_buy(  # type: ignore[override]
        self, req: Any, ctx: RequestContext[Any]
    ) -> dict[str, Any]:
        self.calls.append(("create_media_buy", ctx.account.id))
        return {"media_buy_id": f"mb-async-{self.tag}", "status": "active"}


def _make_routing_account_store(
    account_to_tenant: dict[str, str],
) -> ExplicitAccounts[Any]:
    def _load(account_id: str) -> Account[Any]:
        if account_id not in account_to_tenant:
            raise AdcpError(
                "ACCOUNT_NOT_FOUND",
                message=f"unknown account {account_id!r}",
                recovery="terminal",
            )
        return Account(
            id=account_id,
            metadata={"tenant_id": account_to_tenant[account_id]},
        )

    return ExplicitAccounts(loader=_load)


def _make_ctx(account: Account[Any]) -> RequestContext[Any]:
    return RequestContext(account=account)


# ---------------------------------------------------------------------------
# Drop-in compatibility
# ---------------------------------------------------------------------------


def test_lazy_router_is_decisioning_platform() -> None:
    accounts = _make_routing_account_store({"acct_a": "tenant-a"})
    router = LazyPlatformRouter(
        accounts=accounts,
        factory=lambda tid: _SyncSalesPlatform(tag=tid),
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    assert isinstance(router, DecisioningPlatform)


# ---------------------------------------------------------------------------
# Lazy construction + cache hit/miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_called_once_per_cold_tenant() -> None:
    """First request for tenant-a builds; second hits cache."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    build_count = {"a": 0}

    def factory(tid: str) -> DecisioningPlatform:
        build_count["a"] += 1
        return _SyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    await router.create_media_buy({}, ctx)
    await router.create_media_buy({}, ctx)
    await router.create_media_buy({}, ctx)

    assert build_count["a"] == 1
    assert "tenant-a" in router.cached_tenants


@pytest.mark.asyncio
async def test_async_factory_awaited() -> None:
    """Factory may be async — router awaits at call time."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})

    async def factory(tid: str) -> DecisioningPlatform:
        await asyncio.sleep(0)
        return _AsyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    result = await router.create_media_buy({}, ctx)
    assert result["media_buy_id"] == "mb-async-tenant-a"


@pytest.mark.asyncio
async def test_sync_child_via_to_thread() -> None:
    """Sync child platform method runs through asyncio.to_thread —
    matches PlatformRouter's behaviour."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    router = LazyPlatformRouter(
        accounts=accounts,
        factory=lambda tid: _SyncSalesPlatform(tag=tid),
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    result = await router.get_products({}, ctx)
    assert result["products"][0]["product_id"] == "prod-tenant-a"


# ---------------------------------------------------------------------------
# Cache eviction: size-bounded LRU
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_size_bound_evicts_lru() -> None:
    """With cache_size=2, building a third tenant evicts the LRU one."""
    accounts = _make_routing_account_store({"a1": "tenant-a", "b1": "tenant-b", "c1": "tenant-c"})
    builds: list[str] = []

    def factory(tid: str) -> DecisioningPlatform:
        builds.append(tid)
        return _SyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
        cache_size=2,
        cache_ttl_seconds=0.0,  # no TTL — exercise size-only eviction
    )

    ctx_a = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))
    ctx_b = _make_ctx(Account(id="b1", metadata={"tenant_id": "tenant-b"}))
    ctx_c = _make_ctx(Account(id="c1", metadata={"tenant_id": "tenant-c"}))

    await router.create_media_buy({}, ctx_a)
    await router.create_media_buy({}, ctx_b)
    # tenant-a was the LRU — adding tenant-c evicts it.
    await router.create_media_buy({}, ctx_c)
    assert router.cached_tenants == {"tenant-b", "tenant-c"}
    # Re-request tenant-a — factory rebuilds.
    await router.create_media_buy({}, ctx_a)
    assert builds == ["tenant-a", "tenant-b", "tenant-c", "tenant-a"]


@pytest.mark.asyncio
async def test_cache_ttl_zero_disables_time_expiry() -> None:
    """``cache_ttl_seconds=0`` keeps platforms forever (size-only)."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    builds = {"n": 0}

    def factory(tid: str) -> DecisioningPlatform:
        builds["n"] += 1
        return _SyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
        cache_size=10,
        cache_ttl_seconds=0.0,
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    for _ in range(5):
        await router.create_media_buy({}, ctx)
    assert builds["n"] == 1


@pytest.mark.asyncio
async def test_cache_ttl_expiry_evicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry past its TTL is rebuilt on next access."""
    import adcp.decisioning.platform_router as pr_mod

    accounts = _make_routing_account_store({"a1": "tenant-a"})
    builds = {"n": 0}

    def factory(tid: str) -> DecisioningPlatform:
        builds["n"] += 1
        return _SyncSalesPlatform(tag=tid)

    fake_clock = {"t": 1000.0}
    monkeypatch.setattr(pr_mod.time, "monotonic", lambda: fake_clock["t"])

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
        cache_size=10,
        cache_ttl_seconds=60.0,
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    await router.create_media_buy({}, ctx)
    assert builds["n"] == 1

    # Advance past TTL
    fake_clock["t"] = 1100.0
    await router.create_media_buy({}, ctx)
    assert builds["n"] == 2


# ---------------------------------------------------------------------------
# invalidate()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_specific_tenant_forces_rebuild() -> None:
    accounts = _make_routing_account_store({"a1": "tenant-a", "b1": "tenant-b"})
    builds: list[str] = []

    def factory(tid: str) -> DecisioningPlatform:
        builds.append(tid)
        return _SyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx_a = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))
    ctx_b = _make_ctx(Account(id="b1", metadata={"tenant_id": "tenant-b"}))

    await router.create_media_buy({}, ctx_a)
    await router.create_media_buy({}, ctx_b)
    assert builds == ["tenant-a", "tenant-b"]

    router.invalidate("tenant-a")
    assert "tenant-a" not in router.cached_tenants
    assert "tenant-b" in router.cached_tenants

    await router.create_media_buy({}, ctx_a)
    assert builds == ["tenant-a", "tenant-b", "tenant-a"]


@pytest.mark.asyncio
async def test_invalidate_all_clears_cache() -> None:
    accounts = _make_routing_account_store({"a1": "tenant-a", "b1": "tenant-b"})

    def factory(tid: str) -> DecisioningPlatform:
        return _SyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx_a = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))
    ctx_b = _make_ctx(Account(id="b1", metadata={"tenant_id": "tenant-b"}))

    await router.create_media_buy({}, ctx_a)
    await router.create_media_buy({}, ctx_b)
    assert router.cached_tenants == {"tenant-a", "tenant-b"}

    router.invalidate()
    assert router.cached_tenants == frozenset()


def test_invalidate_unknown_tenant_is_noop() -> None:
    accounts = _make_routing_account_store({})
    router = LazyPlatformRouter(
        accounts=accounts,
        factory=lambda tid: _SyncSalesPlatform(tag=tid),
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    router.invalidate("never-cached")  # no raise


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestConstructionValidation:
    def test_cache_size_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="cache_size"):
            LazyPlatformRouter(
                accounts=_make_routing_account_store({}),
                factory=lambda _t: _SyncSalesPlatform(tag="x"),
                capabilities=_capabilities(["sales-non-guaranteed"]),
                cache_size=0,
            )

    def test_cache_size_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="cache_size"):
            LazyPlatformRouter(
                accounts=_make_routing_account_store({}),
                factory=lambda _t: _SyncSalesPlatform(tag="x"),
                capabilities=_capabilities(["sales-non-guaranteed"]),
                cache_size=-1,
            )

    def test_cache_ttl_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="cache_ttl_seconds"):
            LazyPlatformRouter(
                accounts=_make_routing_account_store({}),
                factory=lambda _t: _SyncSalesPlatform(tag="x"),
                capabilities=_capabilities(["sales-non-guaranteed"]),
                cache_ttl_seconds=-1.0,
            )

    def test_cache_ttl_zero_accepted(self) -> None:
        """Distinct from CallableSubdomainTenantRouter — platform
        adapters don't go stale, so TTL=0 (size-only eviction) is a
        valid mode."""
        LazyPlatformRouter(
            accounts=_make_routing_account_store({}),
            factory=lambda _t: _SyncSalesPlatform(tag="x"),
            capabilities=_capabilities(["sales-non-guaranteed"]),
            cache_ttl_seconds=0.0,
        )


# ---------------------------------------------------------------------------
# Factory rejection paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_returning_none_raises_account_not_found() -> None:
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    router = LazyPlatformRouter(
        accounts=accounts,
        factory=lambda _tid: None,  # type: ignore[arg-type,return-value]
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    with pytest.raises(AdcpError) as exc_info:
        await router.create_media_buy({}, ctx)
    err = exc_info.value
    assert err.code == "ACCOUNT_NOT_FOUND"
    assert err.recovery == "terminal"


@pytest.mark.asyncio
async def test_factory_returning_wrong_type_raises_internal_error() -> None:
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    router = LazyPlatformRouter(
        accounts=accounts,
        factory=lambda _tid: "not a platform",  # type: ignore[arg-type,return-value]
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    with pytest.raises(AdcpError) as exc_info:
        await router.create_media_buy({}, ctx)
    err = exc_info.value
    assert err.code == "INTERNAL_ERROR"
    assert err.recovery == "terminal"


@pytest.mark.asyncio
async def test_factory_raise_not_cached() -> None:
    """A factory that raises does not cache; next request retries."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    attempts = {"n": 0}

    def factory(tid: str) -> DecisioningPlatform:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return _SyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    with pytest.raises(RuntimeError):
        await router.create_media_buy({}, ctx)
    assert "tenant-a" not in router.cached_tenants

    result = await router.create_media_buy({}, ctx)
    assert result["media_buy_id"] == "mb-tenant-a"
    assert attempts["n"] == 2


# ---------------------------------------------------------------------------
# Unknown tenant + unsupported method paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_method_raises_unsupported_feature() -> None:
    """The platform doesn't implement audience methods; calling one
    raises ``UNSUPPORTED_FEATURE``."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    router = LazyPlatformRouter(
        accounts=accounts,
        factory=lambda tid: _SyncSalesPlatform(tag=tid),
        capabilities=_capabilities(["sales-non-guaranteed", "audience-sync"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    with pytest.raises(AdcpError) as exc_info:
        await router.sync_audiences({}, ctx)  # type: ignore[attr-defined]
    assert exc_info.value.code == "UNSUPPORTED_FEATURE"


@pytest.mark.asyncio
async def test_invalidate_during_build_does_not_resurrect() -> None:
    """Race contract: ``invalidate(tenant_id)`` while the factory is
    in-flight must not resurrect the just-evicted slot when the build
    completes. The in-flight caller still gets the platform it paid
    for; the cache stays empty so the next request rebuilds."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_factory(tid: str) -> DecisioningPlatform:
        started.set()
        await finish.wait()
        return _SyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=slow_factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    task = asyncio.create_task(router.create_media_buy({}, ctx))
    await started.wait()
    router.invalidate("tenant-a")
    finish.set()

    result = await task  # in-flight build completes
    assert result["media_buy_id"] == "mb-tenant-a"
    # The cache must NOT have resurrected the platform.
    assert "tenant-a" not in router.cached_tenants


@pytest.mark.asyncio
async def test_invalidate_all_during_build_does_not_resurrect() -> None:
    """``invalidate()`` (no arg) must also bump generation so an
    in-flight build can't slip back into the cache."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_factory(tid: str) -> DecisioningPlatform:
        started.set()
        await finish.wait()
        return _SyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=slow_factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    task = asyncio.create_task(router.create_media_buy({}, ctx))
    await started.wait()
    router.invalidate()  # global flush mid-build
    finish.set()
    await task
    assert "tenant-a" not in router.cached_tenants


@pytest.mark.asyncio
async def test_concurrent_cold_requests_each_build_v1_contract() -> None:
    """v1 contract — no singleflight. Two concurrent requests for the
    same cold tenant each invoke the factory. Locks the contract; if a
    future change adds singleflight, this test fails and the change is
    intentional."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    builds = {"n": 0}

    async def factory(tid: str) -> DecisioningPlatform:
        builds["n"] += 1
        await asyncio.sleep(0.01)  # let the second request enter resolve_platform
        return _SyncSalesPlatform(tag=tid)

    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    results = await asyncio.gather(
        router.create_media_buy({}, ctx),
        router.create_media_buy({}, ctx),
    )
    assert all(r["media_buy_id"] == "mb-tenant-a" for r in results)
    assert builds["n"] == 2


# ---------------------------------------------------------------------------
# proposal_managers routing
# ---------------------------------------------------------------------------


class _StubProposalManager:
    """Minimal :class:`ProposalManager`-shaped stub for routing tests.
    Records which method the router invoked."""

    def __init__(self) -> None:
        from adcp.decisioning import ProposalCapabilities

        self.capabilities = ProposalCapabilities(
            sales_specialism="sales-non-guaranteed",
            refine=False,
        )
        self.calls: list[str] = []

    async def get_products(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        self.calls.append("get_products")
        return {"products": [{"product_id": "manager-prod"}]}


@pytest.mark.asyncio
async def test_proposal_manager_routed_for_tenant_with_manager() -> None:
    """Tenant with a wired manager → router calls manager.get_products,
    not the lazily-built platform."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    builds = {"n": 0}

    def factory(tid: str) -> DecisioningPlatform:
        builds["n"] += 1
        return _SyncSalesPlatform(tag=tid)

    manager = _StubProposalManager()
    router = LazyPlatformRouter(
        accounts=accounts,
        factory=factory,
        capabilities=_capabilities(["sales-non-guaranteed"]),
        proposal_managers={"tenant-a": manager},  # type: ignore[dict-item]
    )
    ctx = _make_ctx(Account(id="a1", metadata={"tenant_id": "tenant-a"}))

    result = await router.get_products({}, ctx)
    assert result["products"][0]["product_id"] == "manager-prod"
    assert manager.calls == ["get_products"]
    # Manager handled it — factory was never called.
    assert builds["n"] == 0


@pytest.mark.asyncio
async def test_proposal_manager_fall_through_when_unwired() -> None:
    """Tenant without a manager → router falls through to the
    lazily-built platform's get_products, identical to the no-manager
    case."""
    accounts = _make_routing_account_store({"b1": "tenant-b"})
    manager = _StubProposalManager()
    router = LazyPlatformRouter(
        accounts=accounts,
        factory=lambda tid: _SyncSalesPlatform(tag=tid),
        capabilities=_capabilities(["sales-non-guaranteed"]),
        proposal_managers={"tenant-a": manager},  # type: ignore[dict-item]
    )
    ctx = _make_ctx(Account(id="b1", metadata={"tenant_id": "tenant-b"}))

    result = await router.get_products({}, ctx)
    assert result["products"][0]["product_id"] == "prod-tenant-b"
    assert manager.calls == []


def test_proposal_manager_for_tenant_lookup() -> None:
    """``proposal_manager_for_tenant`` returns the wired manager
    (or None) without touching the platform factory."""
    manager = _StubProposalManager()
    router = LazyPlatformRouter(
        accounts=_make_routing_account_store({}),
        factory=lambda tid: _SyncSalesPlatform(tag=tid),
        capabilities=_capabilities(["sales-non-guaranteed"]),
        proposal_managers={"tenant-a": manager},  # type: ignore[dict-item]
    )
    assert router.proposal_manager_for_tenant("tenant-a") is manager
    assert router.proposal_manager_for_tenant("tenant-x") is None


# ---------------------------------------------------------------------------
# platform_for_tenant introspection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_for_tenant_builds_via_factory() -> None:
    """Sibling-API parity: ``platform_for_tenant`` triggers the
    factory and returns the same instance the cache would serve to
    request-path delegations."""
    accounts = _make_routing_account_store({"a1": "tenant-a"})
    router = LazyPlatformRouter(
        accounts=accounts,
        factory=lambda tid: _SyncSalesPlatform(tag=tid),
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )

    platform = await router.platform_for_tenant("tenant-a")
    assert isinstance(platform, _SyncSalesPlatform)
    assert "tenant-a" in router.cached_tenants

    # Subsequent call returns the same cached instance.
    again = await router.platform_for_tenant("tenant-a")
    assert again is platform


@pytest.mark.asyncio
async def test_missing_tenant_metadata_raises_account_not_found() -> None:
    """Account without ``metadata['tenant_id']`` → ACCOUNT_NOT_FOUND."""
    router = LazyPlatformRouter(
        accounts=_make_routing_account_store({}),
        factory=lambda tid: _SyncSalesPlatform(tag=tid),
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="acct"))  # no metadata

    with pytest.raises(AdcpError) as exc_info:
        await router.create_media_buy({}, ctx)
    assert exc_info.value.code == "ACCOUNT_NOT_FOUND"


# ===========================================================================
# #722: proposal_stores= / proposal_store_factory= parity with PlatformRouter
# ===========================================================================


class TestProposalStores:
    """LazyPlatformRouter parity with PlatformRouter on proposal_stores.

    Before #722, adopters wiring LazyPlatformRouter had no way to thread
    a ProposalStore into proposal_dispatch — the framework duck-types
    ``hasattr(platform, "proposal_store_for_tenant")`` and silently
    fell through to v1 no-proposal behavior. These tests pin the new
    parity surface.
    """

    @pytest.mark.asyncio
    async def test_eager_proposal_stores_dict_resolves_per_tenant(self) -> None:
        """Eager dict — same shape PlatformRouter accepts. Per-tenant
        store wired at construction; resolution is dict lookup."""
        from adcp.decisioning.proposal_store import InMemoryProposalStore

        store_a = InMemoryProposalStore()
        store_b = InMemoryProposalStore()
        router = LazyPlatformRouter(
            accounts=_make_routing_account_store({}),
            factory=lambda tid: _SyncSalesPlatform(tag=tid),
            capabilities=_capabilities(["sales-non-guaranteed"]),
            proposal_stores={"tenant_a": store_a, "tenant_b": store_b},
        )
        assert router.proposal_store_for_tenant("tenant_a") is store_a
        assert router.proposal_store_for_tenant("tenant_b") is store_b
        # Unwired tenant → None (falls through to no-proposal path in
        # proposal_dispatch).
        assert router.proposal_store_for_tenant("tenant_c") is None

    @pytest.mark.asyncio
    async def test_proposal_store_factory_resolves_lazily(self) -> None:
        """Factory shape — matches the ``factory=`` philosophy of the
        lazy router. Invoked on every call so adopters can wrap with
        their own memoization if needed."""
        from adcp.decisioning.proposal_store import InMemoryProposalStore

        invocations: list[str] = []

        def factory(tenant_id: str) -> Any:
            invocations.append(tenant_id)
            return InMemoryProposalStore()

        router = LazyPlatformRouter(
            accounts=_make_routing_account_store({}),
            factory=lambda tid: _SyncSalesPlatform(tag=tid),
            capabilities=_capabilities(["sales-non-guaranteed"]),
            proposal_store_factory=factory,
        )
        s1 = router.proposal_store_for_tenant("tenant_a")
        s2 = router.proposal_store_for_tenant("tenant_a")
        # Factory called on every invocation — no internal caching.
        # Adopters who need caching wrap the factory themselves.
        assert invocations == ["tenant_a", "tenant_a"]
        assert s1 is not s2

    def test_proposal_store_factory_can_return_none(self) -> None:
        """Adopters with mixed tenants (some need stores, some don't)
        return None from the factory for pure-catalog tenants. The
        framework's proposal_dispatch falls through to the v1 path
        when the accessor returns None."""
        router = LazyPlatformRouter(
            accounts=_make_routing_account_store({}),
            factory=lambda tid: _SyncSalesPlatform(tag=tid),
            capabilities=_capabilities(["sales-non-guaranteed"]),
            proposal_store_factory=lambda tid: None,
        )
        assert router.proposal_store_for_tenant("tenant_a") is None

    def test_mutually_exclusive_eager_and_factory(self) -> None:
        """Passing both ``proposal_stores=`` (eager dict) and
        ``proposal_store_factory=`` (lazy) is a config error — they
        cover the same accessor, and the precedence would be silent.
        Loud-fail at construction."""
        from adcp.decisioning.proposal_store import InMemoryProposalStore

        with pytest.raises(ValueError, match="either proposal_stores=.*or proposal_store_factory="):
            LazyPlatformRouter(
                accounts=_make_routing_account_store({}),
                factory=lambda tid: _SyncSalesPlatform(tag=tid),
                capabilities=_capabilities(["sales-non-guaranteed"]),
                proposal_stores={"t": InMemoryProposalStore()},
                proposal_store_factory=lambda tid: InMemoryProposalStore(),
            )

    def test_default_returns_none_for_no_store_configured(self) -> None:
        """Back-compat: when neither kwarg is passed, the accessor
        exists but returns None. Adopters not using proposals see the
        same v1 behavior they had before #722."""
        router = LazyPlatformRouter(
            accounts=_make_routing_account_store({}),
            factory=lambda tid: _SyncSalesPlatform(tag=tid),
            capabilities=_capabilities(["sales-non-guaranteed"]),
        )
        # Method exists (so hasattr-based dispatch can find it).
        assert hasattr(router, "proposal_store_for_tenant")
        # Returns None for any tenant.
        assert router.proposal_store_for_tenant("any") is None

    def test_proposal_dispatch_can_duck_type_the_accessor(self) -> None:
        """Regression guard for the bug #722 closes: framework's
        ``proposal_dispatch`` does ``hasattr(platform,
        "proposal_store_for_tenant")``. Verify the method is
        callable + returns ``None`` cleanly when no store is wired,
        so the duck-type check succeeds and the dispatch doesn't
        silently fall to the no-proposal path."""
        router = LazyPlatformRouter(
            accounts=_make_routing_account_store({}),
            factory=lambda tid: _SyncSalesPlatform(tag=tid),
            capabilities=_capabilities(["sales-non-guaranteed"]),
        )
        # The hasattr check the framework runs:
        assert hasattr(router, "proposal_store_for_tenant")
        # And the method is callable without raising on unknown tenants.
        result = router.proposal_store_for_tenant("never-wired")
        assert result is None
