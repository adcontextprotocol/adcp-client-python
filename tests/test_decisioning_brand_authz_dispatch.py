"""Tier 3 — brand-authorization dispatch gate.

Tests the dispatch-layer composition between
:class:`adcp.signing.BrandAuthorizationResolver` (v3-identity stages
1-3, PR #770) and the framework's ``serve()`` wire-up:

* Boot validation: ``brand_authz_resolver`` and
  ``brand_identity_resolver`` MUST be wired together. Partial wiring
  is a misconfiguration (a resolver without an extractor never has a
  brand to check; an extractor without a resolver never has anything
  to do) → ``ValueError`` at boot.
* Authorized path: the gate runs after Tier 2 + ``accounts.resolve``;
  on success the platform method runs unchanged.
* Denied path: rejection emits ``PERMISSION_DENIED`` with the same
  cross-tenant-safe message as Tier 2's unrecognized-agent rejection.
* No-buyer-agent skip: Tier 3 is a no-op when Tier 2 is not wired
  (no subject to authorize).
* Extractor-returns-None skip: the adopter signals "no brand to
  bind against" and the gate skips.
* Async extractor: the framework awaits when the extractor returns
  an awaitable.
* ``brand_id`` propagation: when the extractor surfaces a ``brand_id``,
  the resolver sees it (for scoped operator-delegation checks).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.decisioning import (
    AdcpError,
    BuyerAgent,
    DecisioningCapabilities,
    DecisioningPlatform,
    HttpSigCredential,
    InMemoryTaskRegistry,
    SingletonAccounts,
    signing_only_registry,
)
from adcp.decisioning.brand_authz_gate import BrandAuthorizationGate, BrandIdentity
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.serve import create_adcp_server_from_platform
from adcp.server.base import ToolContext


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-brand-authz-")
    yield pool
    pool.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBrandAuthzResolver:
    """Records every ``is_authorized`` call; returns a configured bool.

    Adopters in production wire :class:`BrandJsonAuthorizationResolver`
    from :mod:`adcp.signing.brand_authz`. The dispatch path only sees
    the Protocol surface, so the test fake matches that surface
    exactly without spinning up real brand.json fixtures (those are
    covered in ``tests/test_brand_authz.py``).
    """

    def __init__(self, *, authorized: bool) -> None:
        self._authorized = authorized
        self.calls: list[dict[str, Any]] = []

    async def is_authorized(
        self,
        *,
        agent_url: str,
        brand_domain: str,
        agent_type: str | None = None,
        brand_id: str | None = None,
    ) -> bool:
        self.calls.append(
            {
                "agent_url": agent_url,
                "brand_domain": brand_domain,
                "agent_type": agent_type,
                "brand_id": brand_id,
            }
        )
        return self._authorized


def _signed_auth(agent_url: str) -> AuthInfo:
    return AuthInfo(
        kind="http_sig",
        credential=HttpSigCredential(
            kind="http_sig",
            keyid="kid-1",
            agent_url=agent_url,
            verified_at=1700000000.0,
        ),
    )


# ---------------------------------------------------------------------------
# Boot validation
# ---------------------------------------------------------------------------


def test_serve_requires_both_brand_resolver_and_identity_resolver_resolver_only() -> None:
    """``brand_authz_resolver`` without ``brand_identity_resolver`` is
    a misconfiguration. The resolver has no brand to check; would
    silently never gate. Fail fast at boot."""

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

    with pytest.raises(ValueError, match="brand_authz_resolver and brand_identity_resolver"):
        create_adcp_server_from_platform(
            _Platform(),
            brand_authz_resolver=_FakeBrandAuthzResolver(authorized=True),
        )


def test_serve_requires_both_brand_resolver_and_identity_resolver_extractor_only() -> None:
    """``brand_identity_resolver`` without ``brand_authz_resolver`` is
    a misconfiguration. The extractor would never fire. Fail fast at
    boot."""

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

    with pytest.raises(ValueError, match="brand_authz_resolver and brand_identity_resolver"):
        create_adcp_server_from_platform(
            _Platform(),
            brand_identity_resolver=lambda _account, _agent: BrandIdentity(domain="brand.com"),
        )


def test_serve_neither_wired_is_back_compat() -> None:
    """Pre-Tier-3 adopters omit both kwargs entirely; dispatch
    continues to work unchanged. No ValueError, no warning."""

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

    handler, _executor, _registry = create_adcp_server_from_platform(
        _Platform(),
        validate_at_init=False,
    )
    assert handler is not None
    _executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Authorized path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorized_request_runs_platform_method(executor) -> None:
    """Happy path: registry → buyer_agent → accounts.resolve →
    brand_authz.is_authorized returns True → platform method runs."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    buyer = BuyerAgent(
        agent_url="https://ads.brand.com/agent",
        display_name="Ads-on-brand.com",
        status="active",
    )

    async def lookup(agent_url: str) -> BuyerAgent | None:
        return buyer if agent_url == buyer.agent_url else None

    resolver = _FakeBrandAuthzResolver(authorized=True)
    seen: list[Any] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            seen.append(ctx.buyer_agent)
            return GetProductsResponse(products=[])

    handler = PlatformHandler(
        _Platform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=signing_only_registry(lookup),
        brand_authorization_gate=BrandAuthorizationGate(
            resolver=resolver,
            extract_identity=lambda _account, _agent: BrandIdentity(domain="brand.com"),
        ),
    )

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any"),
        ToolContext(metadata={"adcp.auth_info": _signed_auth(buyer.agent_url)}),
    )

    assert seen == [buyer]
    assert len(resolver.calls) == 1
    assert resolver.calls[0]["agent_url"] == buyer.agent_url
    assert resolver.calls[0]["brand_domain"] == "brand.com"
    assert resolver.calls[0]["brand_id"] is None


@pytest.mark.asyncio
async def test_authorized_propagates_brand_id_when_extractor_returns_it(executor) -> None:
    """When the extractor returns a ``BrandIdentity`` with an ``id``
    set, the resolver sees it via the ``brand_id=`` keyword — required
    for spec-conformant scoped operator-delegation checks."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    buyer = BuyerAgent(
        agent_url="https://ads.brand.com/agent",
        display_name="X",
        status="active",
    )

    async def lookup(agent_url: str) -> BuyerAgent | None:
        return buyer

    resolver = _FakeBrandAuthzResolver(authorized=True)

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            return GetProductsResponse(products=[])

    handler = PlatformHandler(
        _Platform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=signing_only_registry(lookup),
        brand_authorization_gate=BrandAuthorizationGate(
            resolver=resolver,
            extract_identity=lambda _account, _agent: BrandIdentity(domain="brand.com", id="nike"),
        ),
    )

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any"),
        ToolContext(metadata={"adcp.auth_info": _signed_auth(buyer.agent_url)}),
    )

    assert resolver.calls[0]["brand_id"] == "nike"


# ---------------------------------------------------------------------------
# Denied path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_request_raises_permission_denied(executor) -> None:
    """Rejection: ``is_authorized`` returns False → ``PERMISSION_DENIED``
    with the cross-tenant-safe denial message, ``recovery="correctable"``,
    no ``details``. Platform method does NOT run."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    buyer = BuyerAgent(
        agent_url="https://attacker.example/agent",
        display_name="Attacker",
        status="active",
    )

    async def lookup(agent_url: str) -> BuyerAgent | None:
        return buyer

    method_ran = False

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            nonlocal method_ran
            method_ran = True
            return GetProductsResponse(products=[])

    handler = PlatformHandler(
        _Platform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=signing_only_registry(lookup),
        brand_authorization_gate=BrandAuthorizationGate(
            resolver=_FakeBrandAuthzResolver(authorized=False),
            extract_identity=lambda _account, _agent: BrandIdentity(domain="brand.com"),
        ),
    )

    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth(buyer.agent_url)}),
        )

    assert exc_info.value.code == "PERMISSION_DENIED"
    assert exc_info.value.recovery == "correctable"
    # Cross-tenant onboarding-oracle clamp: the wire-level message MUST
    # be the SAME bytes as the Tier 2 unrecognized-agent rejection so
    # the message itself is not a discriminator between the two gates.
    assert "Buyer agent is not authorized for this seller" in str(exc_info.value)
    assert method_ran is False


@pytest.mark.asyncio
async def test_denied_omits_details_payload(executor) -> None:
    """The denial MUST NOT carry a ``details`` payload — same posture
    as Tier 2's unrecognized-agent rejection (omit-on-unestablished-
    identity rule)."""
    from adcp.types import GetProductsRequest

    buyer = BuyerAgent(
        agent_url="https://ads.brand.com/agent",
        display_name="X",
        status="active",
    )

    async def lookup(agent_url: str) -> BuyerAgent | None:
        return buyer

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AssertionError("should not reach platform method")

    handler = PlatformHandler(
        _Platform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=signing_only_registry(lookup),
        brand_authorization_gate=BrandAuthorizationGate(
            resolver=_FakeBrandAuthzResolver(authorized=False),
            extract_identity=lambda _account, _agent: BrandIdentity(domain="brand.com"),
        ),
    )

    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth(buyer.agent_url)}),
        )

    # AdcpError.details defaults to ``{}`` — matches the Tier 2
    # unrecognized-agent rejection shape (no enumerated discriminator).
    assert exc_info.value.details == {}


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_buyer_agent_skips_gate(executor) -> None:
    """Tier 3 wired without Tier 2 → no buyer_agent to authorize → gate
    is a silent no-op. The framework does not synthesize a fake
    buyer-agent identity from the AuthInfo credential."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    resolver = _FakeBrandAuthzResolver(authorized=False)
    method_ran = False

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            nonlocal method_ran
            method_ran = True
            return GetProductsResponse(products=[])

    handler = PlatformHandler(
        _Platform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        # No buyer_agent_registry wired.
        brand_authorization_gate=BrandAuthorizationGate(
            resolver=resolver,
            extract_identity=lambda _account, _agent: BrandIdentity(domain="brand.com"),
        ),
    )

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any"),
        ToolContext(metadata={}),
    )

    assert method_ran is True
    assert resolver.calls == []  # Gate did not fire.


@pytest.mark.asyncio
async def test_extractor_returns_none_skips_gate(executor) -> None:
    """When the extractor returns ``None`` (adopter says "this request
    has no brand to bind against"), the gate skips. The platform method
    runs without consulting the resolver."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    buyer = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="X",
        status="active",
    )

    async def lookup(agent_url: str) -> BuyerAgent | None:
        return buyer

    resolver = _FakeBrandAuthzResolver(authorized=False)
    method_ran = False

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            nonlocal method_ran
            method_ran = True
            return GetProductsResponse(products=[])

    handler = PlatformHandler(
        _Platform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=signing_only_registry(lookup),
        brand_authorization_gate=BrandAuthorizationGate(
            resolver=resolver,
            # Adopter signals "no brand" — gate skips.
            extract_identity=lambda _account, _agent: None,
        ),
    )

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any"),
        ToolContext(metadata={"adcp.auth_info": _signed_auth(buyer.agent_url)}),
    )

    assert method_ran is True
    assert resolver.calls == []


# ---------------------------------------------------------------------------
# Async extractor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_extractor_is_awaited(executor) -> None:
    """The extractor MAY be async — adopters fetching brand identity
    from a remote registry shouldn't have to wrap in
    :func:`asyncio.to_thread`. The framework awaits when the return
    value is an awaitable."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    buyer = BuyerAgent(
        agent_url="https://ads.brand.com/agent",
        display_name="X",
        status="active",
    )

    async def lookup(agent_url: str) -> BuyerAgent | None:
        return buyer

    resolver = _FakeBrandAuthzResolver(authorized=True)

    async def async_extract(_account: Any, _agent: Any) -> BrandIdentity:
        return BrandIdentity(domain="brand.com", id="nike")

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            return GetProductsResponse(products=[])

    handler = PlatformHandler(
        _Platform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=signing_only_registry(lookup),
        brand_authorization_gate=BrandAuthorizationGate(
            resolver=resolver,
            extract_identity=async_extract,
        ),
    )

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any"),
        ToolContext(metadata={"adcp.auth_info": _signed_auth(buyer.agent_url)}),
    )

    assert resolver.calls[0]["brand_id"] == "nike"


# ---------------------------------------------------------------------------
# Three-tier conformance chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_tier_chain_orders_tier2_before_tier3(executor) -> None:
    """End-to-end conformance: Tier 2 (BuyerAgentRegistry) MUST run
    before Tier 3 (brand-authz). A suspended buyer agent rejects at
    Tier 2 with ``AGENT_SUSPENDED`` — the brand-authz resolver is
    never consulted. This pins the ordering so a future refactor
    can't accidentally invert it and leak a Tier 3 binding decision
    for a suspended agent."""
    from adcp.types import GetProductsRequest

    suspended = BuyerAgent(
        agent_url="https://agent.example/",
        display_name="X",
        status="suspended",
    )

    async def lookup(agent_url: str) -> BuyerAgent | None:
        return suspended

    resolver = _FakeBrandAuthzResolver(authorized=True)

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AssertionError("should not reach platform method")

    handler = PlatformHandler(
        _Platform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=signing_only_registry(lookup),
        brand_authorization_gate=BrandAuthorizationGate(
            resolver=resolver,
            extract_identity=lambda _account, _agent: BrandIdentity(domain="brand.com"),
        ),
    )

    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"),
            ToolContext(metadata={"adcp.auth_info": _signed_auth(suspended.agent_url)}),
        )

    assert exc_info.value.code == "AGENT_SUSPENDED"
    # Tier 3 resolver was NOT consulted — the chain short-circuited at
    # Tier 2 per the spec ordering.
    assert resolver.calls == []
