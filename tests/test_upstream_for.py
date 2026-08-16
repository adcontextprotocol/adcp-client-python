"""Phase 2 mock-mode upstream URL routing.

Adapter code is unchanged across modes; the framework swaps the upstream
URL the :class:`UpstreamHttpClient` points at based on
``ctx.account.mode``:

- ``mode='live'`` / ``mode='sandbox'``: ``platform.upstream_url``
- ``mode='mock'``: ``account.metadata['mock_upstream_url']``

Test matrix:

- Routing (3): live, sandbox, mock — each routes to the right base URL.
- Fail-closed (3): mock without metadata, mock with empty/None URL,
  live without ``platform.upstream_url`` declared.
- Cache (2): same key returns same client; different mock URLs don't
  collide.
- Auth threading (1): adopter-supplied ``UpstreamAuth`` flows through
  to the constructed client.
- ``get_mock_upstream_url`` helper (4): dict, missing, non-mapping,
  non-string value.

See ``docs/proposals/lifecycle-state-and-sandbox-authority.md``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    StaticBearer,
    UpstreamHttpClient,
    get_mock_upstream_url,
)
from adcp.decisioning.types import Account
from adcp.decisioning.upstream import NoAuth

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PRODUCTION_URL = "https://upstream.example.invalid/api"
MOCK_URL_A = "http://localhost:4500"
MOCK_URL_B = "http://localhost:4501"


class _Platform(DecisioningPlatform):
    """Minimal platform instance — capabilities + upstream_url only.

    Tests mutate ``upstream_url`` per-instance to exercise the
    None-vs-string branches without subclassing for each.
    """

    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    upstream_url = PRODUCTION_URL


def _ctx(
    *,
    mode: str = "live",
    metadata: dict[str, Any] | None = None,
) -> RequestContext[Any]:
    """Build a RequestContext with an Account at the requested mode.

    The test harness builds the context directly per the framework-only-
    construction note on RequestContext — direct construction is
    supported for tests.
    """
    account: Account[Any] = Account(
        id="acct_test",
        mode=mode,  # type: ignore[arg-type]
        metadata=metadata if metadata is not None else {},
    )
    return RequestContext(account=account)


# ---------------------------------------------------------------------------
# Routing — happy paths
# ---------------------------------------------------------------------------


def test_live_mode_routes_to_platform_upstream_url() -> None:
    platform = _Platform()
    ctx = _ctx(mode="live")

    client = platform.upstream_for(ctx)

    assert isinstance(client, UpstreamHttpClient)
    assert client._base_url == PRODUCTION_URL


def test_sandbox_mode_routes_to_platform_upstream_url() -> None:
    """Sandbox shares the production URL — adopter's test infra is reached
    via the same endpoint with different credentials, not a different URL.
    """
    platform = _Platform()
    ctx = _ctx(mode="sandbox")

    client = platform.upstream_for(ctx)

    assert client._base_url == PRODUCTION_URL


def test_mock_mode_routes_to_account_metadata_url() -> None:
    platform = _Platform()
    ctx = _ctx(
        mode="mock",
        metadata={"mock_upstream_url": MOCK_URL_A},
    )

    client = platform.upstream_for(ctx)

    assert client._base_url == MOCK_URL_A


def test_two_mock_accounts_get_distinct_clients() -> None:
    """Different mock URLs must not share a client (each one's
    ``UpstreamHttpClient`` owns its own ``httpx.AsyncClient`` pool
    pointed at a different host).
    """
    platform = _Platform()
    ctx_a = _ctx(mode="mock", metadata={"mock_upstream_url": MOCK_URL_A})
    ctx_b = _ctx(mode="mock", metadata={"mock_upstream_url": MOCK_URL_B})

    client_a = platform.upstream_for(ctx_a)
    client_b = platform.upstream_for(ctx_b)

    assert client_a is not client_b
    assert client_a._base_url == MOCK_URL_A
    assert client_b._base_url == MOCK_URL_B


# ---------------------------------------------------------------------------
# Fail-closed cases
# ---------------------------------------------------------------------------


def test_mock_mode_without_metadata_url_raises_configuration_error() -> None:
    platform = _Platform()
    ctx = _ctx(mode="mock", metadata={})

    with pytest.raises(AdcpError) as excinfo:
        platform.upstream_for(ctx)

    assert excinfo.value.code == "CONFIGURATION_ERROR"
    assert "mock_upstream_url" in str(excinfo.value)
    assert excinfo.value.field == "account.metadata.mock_upstream_url"


def test_mock_mode_with_empty_string_url_raises() -> None:
    platform = _Platform()
    ctx = _ctx(mode="mock", metadata={"mock_upstream_url": ""})

    with pytest.raises(AdcpError) as excinfo:
        platform.upstream_for(ctx)

    assert excinfo.value.code == "CONFIGURATION_ERROR"


def test_mock_mode_with_none_url_raises() -> None:
    platform = _Platform()
    ctx = _ctx(mode="mock", metadata={"mock_upstream_url": None})

    with pytest.raises(AdcpError) as excinfo:
        platform.upstream_for(ctx)

    assert excinfo.value.code == "CONFIGURATION_ERROR"


def test_mock_mode_with_non_string_url_raises() -> None:
    """Non-string ``mock_upstream_url`` (int, dict, etc.) is rejected
    — the helper requires a real URL string."""
    platform = _Platform()
    ctx = _ctx(mode="mock", metadata={"mock_upstream_url": 12345})

    with pytest.raises(AdcpError) as excinfo:
        platform.upstream_for(ctx)

    assert excinfo.value.code == "CONFIGURATION_ERROR"


def test_live_mode_without_platform_upstream_url_raises() -> None:
    class _NoUpstreamPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        upstream_url = None

    platform = _NoUpstreamPlatform()
    ctx = _ctx(mode="live")

    with pytest.raises(AdcpError) as excinfo:
        platform.upstream_for(ctx)

    assert excinfo.value.code == "CONFIGURATION_ERROR"
    assert "upstream_url" in str(excinfo.value)
    # Diagnostic includes the offending mode so adopters know to either
    # set upstream_url or mark the account mock.
    assert "live" in str(excinfo.value)


def test_sandbox_mode_without_platform_upstream_url_raises() -> None:
    class _NoUpstreamPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        upstream_url = None

    platform = _NoUpstreamPlatform()
    ctx = _ctx(mode="sandbox")

    with pytest.raises(AdcpError) as excinfo:
        platform.upstream_for(ctx)

    assert excinfo.value.code == "CONFIGURATION_ERROR"


# ---------------------------------------------------------------------------
# Cache identity
# ---------------------------------------------------------------------------


def test_repeated_call_same_url_returns_same_client_instance() -> None:
    """Connection pooling correctness: the same (base_url, auth) must
    reuse the same :class:`UpstreamHttpClient` across requests so one
    ``httpx.AsyncClient`` pool fronts every call.
    """
    platform = _Platform()
    auth = StaticBearer(token="t1")

    client_1 = platform.upstream_for(_ctx(mode="live"), auth=auth)
    client_2 = platform.upstream_for(_ctx(mode="live"), auth=auth)

    assert client_1 is client_2


def test_repeated_default_no_auth_reuses_client() -> None:
    """Omitting auth must not allocate a fresh cache identity per request."""
    platform = _Platform()
    client_1 = platform.upstream_for(_ctx(mode="live"))
    client_2 = platform.upstream_for(_ctx(mode="live"))
    assert client_1 is client_2


def test_default_headers_are_part_of_cache_identity() -> None:
    """Tenant routing headers cannot bleed through a shared cached client."""
    platform = _Platform()
    auth = StaticBearer(token="shared")
    client_a = platform.upstream_for(
        _ctx(mode="live"), auth=auth, default_headers={"X-Tenant": "tenant-a"}
    )
    client_b = platform.upstream_for(
        _ctx(mode="live"), auth=auth, default_headers={"X-Tenant": "tenant-b"}
    )
    assert client_a is not client_b
    assert client_a._default_headers == {"X-Tenant": "tenant-a"}
    assert client_b._default_headers == {"X-Tenant": "tenant-b"}


def test_transport_options_are_part_of_cache_identity() -> None:
    platform = _Platform()
    auth = StaticBearer(token="shared")
    default = platform.upstream_for(_ctx(mode="live"), auth=auth)
    different_timeout = platform.upstream_for(_ctx(mode="live"), auth=auth, timeout=5.0)
    different_404 = platform.upstream_for(_ctx(mode="live"), auth=auth, treat_404_as_none=False)
    assert len({id(default), id(different_timeout), id(different_404)}) == 3


@pytest.mark.asyncio
async def test_bounded_pool_retires_evicted_client_until_shutdown() -> None:
    platform = _Platform()
    platform.upstream_client_cache_size = 1
    first = platform.upstream_for(_ctx(mode="live"), auth=StaticBearer(token="first"))
    first.aclose = AsyncMock()  # type: ignore[method-assign]

    second = platform.upstream_for(_ctx(mode="live"), auth=StaticBearer(token="second"))
    second.aclose = AsyncMock()  # type: ignore[method-assign]

    first.aclose.assert_not_awaited()
    assert len(platform._upstream_client_pool._cache) == 1
    assert platform._upstream_client_pool._retired == [first]

    await platform.aclose_upstream_clients()
    first.aclose.assert_awaited_once()
    second.aclose.assert_awaited_once()


def test_sync_eviction_drains_cached_and_retired_clients() -> None:
    platform = _Platform()
    platform.upstream_client_cache_size = 1
    first = platform.upstream_for(_ctx(mode="live"), auth=StaticBearer(token="first"))
    second = platform.upstream_for(_ctx(mode="live"), auth=StaticBearer(token="second"))
    first.aclose = AsyncMock()  # type: ignore[method-assign]
    second.aclose = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(platform.aclose_upstream_clients())

    first.aclose.assert_awaited_once()
    second.aclose.assert_awaited_once()


def test_zero_upstream_client_cache_size_fails_closed() -> None:
    platform = _Platform()
    platform.upstream_client_cache_size = 0

    with pytest.raises(ValueError, match="max_size must be at least 1"):
        platform.upstream_for(_ctx(mode="live"))


def test_upstream_client_pool_updates_lru_recency_on_hit() -> None:
    platform = _Platform()
    platform.upstream_client_cache_size = 2
    auth_a = StaticBearer(token="a")
    auth_b = StaticBearer(token="b")
    auth_c = StaticBearer(token="c")
    client_a = platform.upstream_for(_ctx(mode="live"), auth=auth_a)
    client_b = platform.upstream_for(_ctx(mode="live"), auth=auth_b)

    assert platform.upstream_for(_ctx(mode="live"), auth=auth_a) is client_a
    client_c = platform.upstream_for(_ctx(mode="live"), auth=auth_c)

    assert list(platform._upstream_client_pool._cache.values()) == [client_a, client_c]
    assert platform._upstream_client_pool._retired == [client_b]


def test_distinct_auth_strategies_get_distinct_clients() -> None:
    """Different auth instances ⇒ different clients. The auth is
    injected at construction; the framework can't swap it on a cached
    instance, so cache key must include the auth identity.
    """
    platform = _Platform()
    auth_1 = StaticBearer(token="t1")
    auth_2 = StaticBearer(token="t2")

    client_1 = platform.upstream_for(_ctx(mode="live"), auth=auth_1)
    client_2 = platform.upstream_for(_ctx(mode="live"), auth=auth_2)

    assert client_1 is not client_2


def test_cache_is_per_platform_instance() -> None:
    """Multi-platform processes (one DecisioningPlatform instance per
    adapter) MUST NOT share an upstream-client cache. A leak across
    platforms would let one adapter's auth headers ship on another
    adapter's connection pool.
    """
    p1 = _Platform()
    p2 = _Platform()

    c1 = p1.upstream_for(_ctx(mode="live"))
    c2 = p2.upstream_for(_ctx(mode="live"))

    assert c1 is not c2


# ---------------------------------------------------------------------------
# Auth threading
# ---------------------------------------------------------------------------


def test_auth_strategy_flows_through_to_client_construction() -> None:
    """Adopter-supplied :class:`UpstreamAuth` must reach the
    :class:`UpstreamHttpClient`; the client carries it onto every
    request and the auth resolver runs at request time.
    """
    platform = _Platform()
    auth = StaticBearer(token="bearer-xyz")
    ctx = _ctx(mode="live")

    client = platform.upstream_for(ctx, auth=auth)

    assert client._auth is auth


def test_default_auth_is_no_auth() -> None:
    """When the adopter doesn't supply auth, the framework defaults to
    :class:`NoAuth` so the client construction succeeds; adopter
    upstream calls without credentials work for unauthenticated dev /
    fixture endpoints.
    """
    platform = _Platform()
    client = platform.upstream_for(_ctx(mode="live"))

    assert isinstance(client._auth, NoAuth)


def test_default_headers_flow_through() -> None:
    """Adopter-supplied default headers reach the constructed client."""
    platform = _Platform()
    client = platform.upstream_for(
        _ctx(mode="live"),
        default_headers={"X-API-Version": "2"},
    )

    assert client._default_headers == {"X-API-Version": "2"}


# ---------------------------------------------------------------------------
# get_mock_upstream_url helper
# ---------------------------------------------------------------------------


def test_get_mock_upstream_url_reads_dict_metadata() -> None:
    account = Account(id="a", mode="mock", metadata={"mock_upstream_url": MOCK_URL_A})
    assert get_mock_upstream_url(account) == MOCK_URL_A


def test_get_mock_upstream_url_returns_none_when_absent() -> None:
    account = Account(id="a", mode="mock", metadata={})
    assert get_mock_upstream_url(account) is None


def test_get_mock_upstream_url_returns_none_for_non_string() -> None:
    account = Account(id="a", mode="mock", metadata={"mock_upstream_url": 12345})
    assert get_mock_upstream_url(account) is None


def test_get_mock_upstream_url_returns_none_for_empty_string() -> None:
    account = Account(id="a", mode="mock", metadata={"mock_upstream_url": ""})
    assert get_mock_upstream_url(account) is None


def test_get_mock_upstream_url_returns_none_for_non_mapping_metadata() -> None:
    """Pre-mode adopters whose ``metadata`` shape isn't a Mapping
    (TypedDict subclasses ARE mappings; dataclass-shaped TMeta isn't)
    read as ``None`` rather than raising — the framework's
    fail-closed branch in :meth:`DecisioningPlatform.upstream_for`
    surfaces ``CONFIGURATION_ERROR`` cleanly.
    """

    class _MetaDataclass:
        # Intentionally NOT a Mapping
        def __init__(self) -> None:
            self.mock_upstream_url = MOCK_URL_A

    account: Account[Any] = Account(id="a", mode="mock", metadata=_MetaDataclass())
    assert get_mock_upstream_url(account) is None


def test_get_mock_upstream_url_returns_none_for_none_account() -> None:
    assert get_mock_upstream_url(None) is None  # type: ignore[arg-type]
