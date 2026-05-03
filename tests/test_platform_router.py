"""Tests for :class:`adcp.decisioning.PlatformRouter`.

The router is the multi-platform-per-process primitive. These tests
cover:

* Tenant-keyed dispatch — each call routes to the right child platform.
* Drop-in compatibility — ``isinstance(router, DecisioningPlatform)``.
* Async + sync child methods both work via the synthesized delegation.
* Unknown tenant + unsupported method paths raise the documented
  structured errors (``ACCOUNT_NOT_FOUND`` and ``UNSUPPORTED_FEATURE``).
* Capability-driven advertised tool projection — the router's union
  ``capabilities`` flows through ``advertised_tools_for_instance``.
* Protocol introspection — every method name declared on a known
  specialism Protocol is reachable on the router.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    ExplicitAccounts,
    PlatformRouter,
    RequestContext,
    SalesPlatform,
)
from adcp.decisioning.platform_router import (
    _all_specialism_methods,
    _protocol_method_names,
)
from adcp.decisioning.types import Account

# ---------------------------------------------------------------------------
# Test fixtures: minimal child platforms
# ---------------------------------------------------------------------------


def _capabilities(specialisms: list[str]) -> DecisioningCapabilities:
    """Build a minimal-but-valid capabilities object for tests."""
    from adcp.decisioning.capabilities import (
        Adcp,
        IdempotencySupported,
        SupportedProtocol,
    )

    return DecisioningCapabilities(
        specialisms=specialisms,
        adcp=Adcp(
            major_versions=[3],
            idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
        ),
        supported_protocols=[SupportedProtocol.media_buy],
    )


class _SyncSalesPlatform(DecisioningPlatform, SalesPlatform):
    """Sync child platform — the minimum sales-non-guaranteed surface."""

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
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
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
    """Async-method variant — every required method is ``async def``."""

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


class _NonSalesPlatform(DecisioningPlatform):
    """Platform that doesn't implement sales methods — used to confirm
    UNSUPPORTED_FEATURE projection on cross-tenant capability gaps."""

    def __init__(self) -> None:
        pass

    capabilities = _capabilities(["audience-sync"])
    accounts = ExplicitAccounts(loader=lambda _id: Account(id=_id))

    def sync_audiences(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {"audiences": []}


# ---------------------------------------------------------------------------
# Account store fixture: maps wire account_ref to Account with tenant_id
# ---------------------------------------------------------------------------


def _make_routing_account_store(
    account_to_tenant: dict[str, str],
) -> ExplicitAccounts[Any]:
    """Build an AccountStore that stamps ``metadata['tenant_id']`` per
    the supplied mapping. Mirrors the multi-tenant adopter pattern: the
    AccountStore is the seam that wires resolved accounts to tenants."""

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
    """Build a RequestContext with a resolved account, mirroring what
    the dispatcher's ``_build_request_context`` produces."""
    return RequestContext(account=account)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_router_is_decisioning_platform() -> None:
    """Drop-in compat — the router IS a DecisioningPlatform, satisfying
    ``isinstance`` checks the framework's serve() path runs."""
    accounts = _make_routing_account_store({"acct_a": "tenant-a"})
    router = PlatformRouter(
        accounts=accounts,
        platforms={"tenant-a": _SyncSalesPlatform("a")},
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    assert isinstance(router, DecisioningPlatform)


def test_router_synthesizes_every_specialism_method() -> None:
    """Every method declared on a known specialism Protocol is reachable
    on the router as a callable. New Protocols added to the SDK get
    picked up automatically because the router walks the Protocol set
    at construction.
    """
    router = PlatformRouter(
        accounts=_make_routing_account_store({"acct_a": "tenant-a"}),
        platforms={"tenant-a": _SyncSalesPlatform("a")},
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    for method_name in _all_specialism_methods():
        assert callable(
            getattr(router, method_name)
        ), f"router missing synthesized delegate for {method_name!r}"


def test_protocol_method_names_picks_up_sales_methods() -> None:
    """Sanity check on the introspection helper itself — every method
    on the ``SalesPlatform`` Protocol shows up. Defends against
    accidentally regressing the introspection to skip required
    methods.
    """
    names = _protocol_method_names(SalesPlatform)
    # The required-method core per ``REQUIRED_METHODS_PER_SPECIALISM``.
    for required in (
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
    ):
        assert required in names


@pytest.mark.asyncio
async def test_dispatch_routes_to_correct_tenant() -> None:
    """Three tenants → three platforms. Each tool call routes to the
    tenant's platform and only that platform records the call."""
    platform_a = _SyncSalesPlatform("a")
    platform_b = _SyncSalesPlatform("b")
    platform_c = _SyncSalesPlatform("c")

    router = PlatformRouter(
        accounts=_make_routing_account_store(
            {"acct_a": "tenant-a", "acct_b": "tenant-b", "acct_c": "tenant-c"}
        ),
        platforms={
            "tenant-a": platform_a,
            "tenant-b": platform_b,
            "tenant-c": platform_c,
        },
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )

    ctx_a = _make_ctx(Account(id="acct_a", metadata={"tenant_id": "tenant-a"}))
    ctx_b = _make_ctx(Account(id="acct_b", metadata={"tenant_id": "tenant-b"}))
    ctx_c = _make_ctx(Account(id="acct_c", metadata={"tenant_id": "tenant-c"}))

    resp_a = await router.get_products({}, ctx_a)
    resp_b = await router.get_products({}, ctx_b)
    resp_c = await router.get_products({}, ctx_c)

    assert resp_a["products"][0]["product_id"] == "prod-a"
    assert resp_b["products"][0]["product_id"] == "prod-b"
    assert resp_c["products"][0]["product_id"] == "prod-c"

    # Cross-tenant isolation — each child saw exactly one call, only
    # for its own account.
    assert platform_a.calls == [("get_products", "acct_a")]
    assert platform_b.calls == [("get_products", "acct_b")]
    assert platform_c.calls == [("get_products", "acct_c")]


@pytest.mark.asyncio
async def test_dispatch_supports_async_child_methods() -> None:
    """Async children are awaited, sync children run in a thread — both
    surface the same way through the router's async-def delegate."""
    sync_platform = _SyncSalesPlatform("sync")
    async_platform = _AsyncSalesPlatform("async")

    router = PlatformRouter(
        accounts=_make_routing_account_store(
            {"acct_sync": "tenant-sync", "acct_async": "tenant-async"}
        ),
        platforms={
            "tenant-sync": sync_platform,
            "tenant-async": async_platform,
        },
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )

    ctx_sync = _make_ctx(Account(id="acct_sync", metadata={"tenant_id": "tenant-sync"}))
    ctx_async = _make_ctx(Account(id="acct_async", metadata={"tenant_id": "tenant-async"}))

    sync_resp = await router.get_products({}, ctx_sync)
    async_resp = await router.get_products({}, ctx_async)

    assert sync_resp["products"][0]["product_id"] == "prod-sync"
    assert async_resp["products"][0]["product_id"] == "prod-async-async"


@pytest.mark.asyncio
async def test_dispatch_supports_arg_projector_kwargs() -> None:
    """The dispatcher invokes some methods with keyword args (e.g.
    ``update_media_buy(media_buy_id=..., patch=..., ctx=...)``); the
    router's ``*args, **kwargs`` delegate forwards them verbatim.
    """
    platform = _SyncSalesPlatform("a")
    router = PlatformRouter(
        accounts=_make_routing_account_store({"acct_a": "tenant-a"}),
        platforms={"tenant-a": platform},
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx = _make_ctx(Account(id="acct_a", metadata={"tenant_id": "tenant-a"}))

    resp = await router.update_media_buy(media_buy_id="mb_123", patch={"foo": "bar"}, ctx=ctx)
    assert resp["media_buy_id"] == "mb_123"
    assert platform.calls == [("update_media_buy", "acct_a")]


@pytest.mark.asyncio
async def test_unknown_tenant_raises_account_not_found() -> None:
    """Resolving to a tenant the router doesn't know about projects to
    ACCOUNT_NOT_FOUND — multi-tenant topology stays opaque to the wire.
    """
    router = PlatformRouter(
        accounts=_make_routing_account_store({"acct_a": "tenant-a"}),
        platforms={"tenant-a": _SyncSalesPlatform("a")},
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx_unknown = _make_ctx(Account(id="acct_x", metadata={"tenant_id": "tenant-unknown"}))

    with pytest.raises(AdcpError) as excinfo:
        await router.get_products({}, ctx_unknown)
    assert excinfo.value.code == "ACCOUNT_NOT_FOUND"
    assert excinfo.value.field == "account.metadata.tenant_id"
    assert excinfo.value.recovery == "terminal"


@pytest.mark.asyncio
async def test_missing_tenant_id_raises_account_not_found() -> None:
    """Account whose metadata is missing the ``tenant_id`` key raises
    ACCOUNT_NOT_FOUND with a diagnostic pointing at the AccountStore
    integration as the bug source."""
    router = PlatformRouter(
        accounts=_make_routing_account_store({"acct_a": "tenant-a"}),
        platforms={"tenant-a": _SyncSalesPlatform("a")},
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    ctx_no_tenant = _make_ctx(Account(id="acct_orphan", metadata={}))

    with pytest.raises(AdcpError) as excinfo:
        await router.get_products({}, ctx_no_tenant)
    assert excinfo.value.code == "ACCOUNT_NOT_FOUND"


@pytest.mark.asyncio
async def test_unsupported_method_on_resolved_tenant() -> None:
    """When the router's union capabilities advertise a method but the
    resolved tenant's platform doesn't implement it, we project to
    UNSUPPORTED_FEATURE per spec — not a 404, not silent failure.
    """
    sales_platform = _SyncSalesPlatform("sales")
    audience_platform = _NonSalesPlatform()

    router = PlatformRouter(
        accounts=_make_routing_account_store(
            {
                "acct_sales": "tenant-sales",
                "acct_audience": "tenant-audience",
            }
        ),
        platforms={
            "tenant-sales": sales_platform,
            "tenant-audience": audience_platform,
        },
        # Union capabilities: the router advertises both sales and
        # audience methods even though each tenant only does one.
        capabilities=_capabilities(["sales-non-guaranteed", "audience-sync"]),
    )

    # Audience-tenant request to get_products → UNSUPPORTED_FEATURE.
    ctx_audience = _make_ctx(
        Account(
            id="acct_audience",
            metadata={"tenant_id": "tenant-audience"},
        )
    )
    with pytest.raises(AdcpError) as excinfo:
        await router.get_products({}, ctx_audience)
    assert excinfo.value.code == "UNSUPPORTED_FEATURE"
    assert excinfo.value.recovery == "terminal"


def test_empty_platforms_mapping_rejected() -> None:
    """An empty platforms dict is misconfiguration — fail fast at
    construction rather than 404 every request later."""
    with pytest.raises(ValueError, match="at least one child platform"):
        PlatformRouter(
            accounts=_make_routing_account_store({}),
            platforms={},
            capabilities=_capabilities(["sales-non-guaranteed"]),
        )


def test_tenants_property_lists_registered_tenants() -> None:
    """The ``tenants`` view exposes the registered tenant ids as a
    frozenset for adopter introspection (logging, admin endpoints,
    health checks). Mutations to the source dict don't propagate."""
    source = {"tenant-a": _SyncSalesPlatform("a"), "tenant-b": _SyncSalesPlatform("b")}
    router = PlatformRouter(
        accounts=_make_routing_account_store({"acct_a": "tenant-a", "acct_b": "tenant-b"}),
        platforms=source,
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )

    assert router.tenants == frozenset({"tenant-a", "tenant-b"})

    # Source mutation doesn't leak.
    source["tenant-c"] = _SyncSalesPlatform("c")
    assert router.tenants == frozenset({"tenant-a", "tenant-b"})


def test_platform_for_tenant_returns_registered_child() -> None:
    """Adopter-facing introspection — fetch the child by tenant id."""
    platform_a = _SyncSalesPlatform("a")
    router = PlatformRouter(
        accounts=_make_routing_account_store({"acct_a": "tenant-a"}),
        platforms={"tenant-a": platform_a},
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    assert router.platform_for_tenant("tenant-a") is platform_a
    with pytest.raises(KeyError):
        router.platform_for_tenant("tenant-missing")


def test_router_passes_validate_platform() -> None:
    """The router's synthesized delegates satisfy
    ``validate_platform``'s ``_has_overridden_method`` check — the
    same boot-time contract single-platform adopters depend on."""
    from adcp.decisioning.dispatch import validate_platform

    router = PlatformRouter(
        accounts=_make_routing_account_store({"acct_a": "tenant-a"}),
        platforms={"tenant-a": _SyncSalesPlatform("a")},
        capabilities=_capabilities(["sales-non-guaranteed"]),
    )
    # Should not raise.
    validate_platform(router)


@pytest.mark.asyncio
async def test_resolve_ctx_from_kwargs() -> None:
    """The context resolver picks up ``ctx=`` kwargs (the dispatcher's
    arg_projector path uses kwargs)."""
    from adcp.decisioning.platform_router import _resolve_ctx_from_args

    ctx = _make_ctx(Account(id="acct", metadata={"tenant_id": "t"}))
    resolved = _resolve_ctx_from_args(args=(), kwargs={"ctx": ctx})
    assert resolved is ctx


def test_resolve_ctx_from_positional() -> None:
    """The context resolver picks up the trailing positional argument
    (the dispatcher's standard path)."""
    from adcp.decisioning.platform_router import _resolve_ctx_from_args

    ctx = _make_ctx(Account(id="acct", metadata={"tenant_id": "t"}))
    resolved = _resolve_ctx_from_args(args=({"req": "x"}, ctx), kwargs={})
    assert resolved is ctx


def test_resolve_ctx_missing_raises_internal_error() -> None:
    """No ctx in args or kwargs → INTERNAL_ERROR. This path is only
    reachable if the dispatcher's contract has drifted; adopter code
    never hits it."""
    from adcp.decisioning.platform_router import _resolve_ctx_from_args

    with pytest.raises(AdcpError) as excinfo:
        _resolve_ctx_from_args(args=(), kwargs={})
    assert excinfo.value.code == "INTERNAL_ERROR"


def test_known_specialism_protocols_matches_specialisms_module() -> None:
    """Guards the router's extension contract.

    When a new Protocol class is added to ``adcp.decisioning.specialisms``,
    it MUST also be added to ``_KNOWN_SPECIALISM_PROTOCOLS`` in
    ``platform_router.py`` — otherwise the router silently fails to
    synthesize delegates for the new specialism's methods, and the only
    buyer-side signal is UNSUPPORTED_FEATURE on calls that should work.

    This test fails the moment that drift exists.
    """
    from adcp.decisioning import specialisms
    from adcp.decisioning.platform_router import _KNOWN_SPECIALISM_PROTOCOLS

    known = {p.__name__ for p in _KNOWN_SPECIALISM_PROTOCOLS}
    in_module = {name for name in specialisms.__all__ if name.endswith("Platform")}

    drift = in_module ^ known
    assert not drift, (
        f"PlatformRouter extension contract drift. Mismatch between "
        f"specialisms.__all__ Platform classes and _KNOWN_SPECIALISM_PROTOCOLS: "
        f"{sorted(drift)}. Update _KNOWN_SPECIALISM_PROTOCOLS to match."
    )


def test_account_store_methods_denylist_matches_protocols() -> None:
    """Guards ``_ACCOUNT_STORE_METHODS`` membership against AccountStore Protocol drift.

    If ``AccountStore`` / ``AccountStoreList`` / ``AccountStoreUpsert`` /
    ``AccountStoreSyncGovernance`` add a new method, the denylist must
    grow with them — otherwise the router could synthesize a tenant-keyed
    delegate over an AccountStore method by accident.
    """
    from adcp.decisioning.accounts import (
        AccountStore,
        AccountStoreList,
        AccountStoreSyncGovernance,
        AccountStoreUpsert,
    )
    from adcp.decisioning.platform_router import (
        _ACCOUNT_STORE_METHODS,
    )

    expected = (
        _protocol_method_names(AccountStore)
        | _protocol_method_names(AccountStoreList)
        | _protocol_method_names(AccountStoreUpsert)
        | _protocol_method_names(AccountStoreSyncGovernance)
    )
    drift = expected ^ _ACCOUNT_STORE_METHODS
    assert not drift, (
        f"AccountStore Protocol method drift. Update _ACCOUNT_STORE_METHODS "
        f"in platform_router.py: {sorted(drift)}"
    )
