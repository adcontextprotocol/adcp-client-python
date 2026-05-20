"""Tests for the opt-in v3.1 preview surface (``adcp.preview``).

Covers the three catalog-sync proposals shipped in v3.1.0-beta.1:

* #4761 ``catalog_version`` conditional fetch — :class:`CatalogVersionCache`
  hit/miss/unchanged flow.
* #4762 ``get_signals discovery_mode=wholesale`` — mutex enforcement on the
  preview request subclass.
* #4763 per-agent catalog change feed — capability extraction and the HTTP
  poll/subscribe surface against a stubbed transport.

All wire shapes are validated against the v3.1.0-beta.1 JSON Schemas via
:func:`adcp.validation.schema_loader.get_validator` so the tests fail loud
if the SDK's wire format ever drifts from the canonical schema.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from adcp import ADCPClient
from adcp.preview import (
    CatalogChangeFeedClient,
    CatalogChangeFeedError,
    CatalogEvent,
    CatalogEventsPage,
    CatalogVersionCache,
    CatalogVersionEntry,
    _scope_key_for_request,
    catalog_change_feed_from_capabilities,
    get_products_with_cache,
    get_signals_with_cache,
)
from adcp.preview import GetProductsRequest as PreviewGetProductsRequest
from adcp.preview import GetProductsResponse as PreviewGetProductsResponse
from adcp.preview import GetSignalsRequest as PreviewGetSignalsRequest
from adcp.preview import GetSignalsResponse as PreviewGetSignalsResponse
from adcp.types import AgentConfig, Protocol
from adcp.types.core import TaskResult, TaskStatus
from adcp.validation.schema_loader import _reset_for_tests, get_validator

# ---------------------------------------------------------------------------
# #4762 — discovery_mode=wholesale request shape
# ---------------------------------------------------------------------------


def test_signals_wholesale_request_is_minimal() -> None:
    """A wholesale enumeration request needs nothing beyond ``discovery_mode``."""
    req = PreviewGetSignalsRequest(discovery_mode="wholesale")
    wire = req.model_dump(mode="json", exclude_none=True)
    assert wire == {"discovery_mode": "wholesale"}


def test_signals_wholesale_validates_against_3_1_schema() -> None:
    _reset_for_tests()
    validator = get_validator("get_signals", "request", version="3.1.0-beta.1")
    assert validator is not None, "v3.1.0-beta.1 schema cache must be present"

    req = PreviewGetSignalsRequest(discovery_mode="wholesale")
    validator.validate(req.model_dump(mode="json", exclude_none=True))


def test_signals_wholesale_rejects_signal_spec() -> None:
    """Error names the conflicting field and points at the fix."""
    with pytest.raises(ValueError, match=r"wholesale.*conflicts with.*signal_spec.*"):
        PreviewGetSignalsRequest(discovery_mode="wholesale", signal_spec="auto intenders")


def test_signals_wholesale_rejects_signal_ids() -> None:
    with pytest.raises(ValueError, match=r"wholesale.*conflicts with.*signal_ids.*"):
        PreviewGetSignalsRequest(
            discovery_mode="wholesale",
            signal_ids=[
                {
                    "source": "catalog",
                    "data_provider_domain": "acme.com",
                    "id": "auto-intenders",
                },
            ],
        )


def test_signals_pricing_version_requires_catalog_version() -> None:
    """Error tells adopter what to do, not just what went wrong."""
    with pytest.raises(ValueError, match=r"pass both tokens.*or omit both"):
        PreviewGetSignalsRequest(if_pricing_version="p1")


# ---------------------------------------------------------------------------
# #4761 — get_products conditional-fetch tokens
# ---------------------------------------------------------------------------


def test_products_conditional_request_validates() -> None:
    _reset_for_tests()
    validator = get_validator("get_products", "request", version="3.1.0-beta.1")
    assert validator is not None

    req = PreviewGetProductsRequest(
        buying_mode="wholesale",
        if_catalog_version="v2026-05-19-rev42",
    )
    wire = req.model_dump(mode="json", exclude_none=True)
    assert wire["if_catalog_version"] == "v2026-05-19-rev42"
    validator.validate(wire)


def test_products_pricing_version_requires_catalog_version() -> None:
    with pytest.raises(ValueError, match=r"pass both tokens.*or omit both"):
        PreviewGetProductsRequest(buying_mode="wholesale", if_pricing_version="p1")


# ---------------------------------------------------------------------------
# #4761 phase 3 — CatalogVersionCache
# ---------------------------------------------------------------------------


def test_scope_key_ignores_pagination_cursor() -> None:
    """The cache scope MUST be identical across pages of the same enumeration."""
    base = {"buying_mode": "wholesale", "filters": {"category": "video"}}
    a = _scope_key_for_request("get_products", {**base, "pagination": {"cursor": "p1"}})
    b = _scope_key_for_request("get_products", {**base, "pagination": {"cursor": "p2"}})
    assert a == b


def test_scope_key_changes_with_filters() -> None:
    a = _scope_key_for_request(
        "get_products",
        {"buying_mode": "wholesale", "filters": {"category": "video"}},
    )
    b = _scope_key_for_request(
        "get_products",
        {"buying_mode": "wholesale", "filters": {"category": "display"}},
    )
    assert a != b


def test_scope_key_separates_public_and_account_layers() -> None:
    """Account-scoped requests MUST hash to a different key than public ones."""
    public = _scope_key_for_request("get_products", {"buying_mode": "wholesale"})
    account = _scope_key_for_request(
        "get_products",
        {"buying_mode": "wholesale", "account": {"account_id": "acc_1"}},
    )
    assert public != account


def test_cache_invalidate_drops_only_targeted_entry() -> None:
    cache = CatalogVersionCache()
    k1 = ("get_products", "scope-1")
    k2 = ("get_products", "scope-2")
    e1 = CatalogVersionEntry("v1", None, "public", payload="payload-1")
    e2 = CatalogVersionEntry("v2", None, "public", payload="payload-2")
    cache.store("agent_a", k1, e1)
    cache.store("agent_a", k2, e2)

    cache.invalidate("agent_a", k1)

    assert cache.lookup("agent_a", k1) is None
    assert cache.lookup("agent_a", k2) is e2


def test_cache_invalidate_agent_drops_all_for_that_agent() -> None:
    cache = CatalogVersionCache()
    cache.store("agent_a", ("scope-1",), CatalogVersionEntry("v1", None, "public", "p1"))
    cache.store("agent_b", ("scope-1",), CatalogVersionEntry("v1", None, "public", "p1"))

    cache.invalidate("agent_a")

    assert cache.lookup("agent_a", ("scope-1",)) is None
    assert cache.lookup("agent_b", ("scope-1",)) is not None


# Fixture: a minimally-stubbed ADCPClient where get_products / get_signals
# return whatever we hand them. The cache helpers call client.get_products /
# get_signals; we don't want the protocol adapter in the loop.
def _stub_client() -> ADCPClient:
    return ADCPClient(
        AgentConfig(id="agent_1", agent_uri="https://agent.example", protocol=Protocol.MCP),
    )


def _adapter_get_products_returns(client: ADCPClient, body: dict[str, Any]) -> AsyncMock:
    """Stub ``client.adapter.get_products`` to return a raw TaskResult.

    The wrapper bypasses ``client.get_products`` strict parsing — it calls
    the adapter directly and parses with the preview model — so tests mock
    the adapter, not the client method.
    """
    mock = AsyncMock(
        return_value=TaskResult(status=TaskStatus.COMPLETED, success=True, data=body),
    )
    client.adapter.get_products = mock  # type: ignore[method-assign]
    return mock


def _adapter_get_signals_returns(client: ADCPClient, body: dict[str, Any]) -> AsyncMock:
    mock = AsyncMock(
        return_value=TaskResult(status=TaskStatus.COMPLETED, success=True, data=body),
    )
    client.adapter.get_signals = mock  # type: ignore[method-assign]
    return mock


@pytest.mark.asyncio
async def test_cache_miss_stores_version_token() -> None:
    """First call against a v3.1 agent: no token attached, response stored."""
    cache = CatalogVersionCache()
    client = _stub_client()
    fresh_wire = {
        "products": [],
        "catalog_version": "v1",
        "cache_scope": "public",
    }
    mock_call = _adapter_get_products_returns(client, fresh_wire)

    request = PreviewGetProductsRequest(buying_mode="wholesale")
    result = await get_products_with_cache(client, request, cache)

    assert result.success
    sent_params = mock_call.call_args.args[0]
    assert "if_catalog_version" not in sent_params, "cache miss must not attach a token"

    cached = cache.lookup("agent_1", _scope_key_for_request("get_products", sent_params))
    assert cached is not None
    assert cached.catalog_version == "v1"


@pytest.mark.asyncio
async def test_cache_hit_attaches_token_and_short_circuits_unchanged() -> None:
    """Second call: token attached, ``unchanged: true`` returns the cached payload."""
    cache = CatalogVersionCache()
    client = _stub_client()
    request = PreviewGetProductsRequest(buying_mode="wholesale")
    scope = _scope_key_for_request(
        "get_products",
        request.model_dump(mode="json", exclude_none=True),
    )

    cached_payload = PreviewGetProductsResponse(
        products=[],
        catalog_version="v1",
        cache_scope="public",
    )
    cache.store(
        "agent_1",
        scope,
        CatalogVersionEntry("v1", None, "public", payload=cached_payload),
    )

    # v3.1 unchanged wire shape: products omitted, only the version + scope echo
    unchanged_wire = {
        "unchanged": True,
        "catalog_version": "v1",
        "cache_scope": "public",
    }
    mock_call = _adapter_get_products_returns(client, unchanged_wire)

    result = await get_products_with_cache(client, request, cache)

    sent_params = mock_call.call_args.args[0]
    assert sent_params.get("if_catalog_version") == "v1", "cache hit must attach the cached token"
    # The cache returns a deep copy so adopter mutation can't poison the
    # cached entry. Equality holds; identity does not — that's the contract.
    assert result.data is not cached_payload, "deep-copy guard must hand back a fresh instance"
    assert result.data == cached_payload, "unchanged:true MUST return a copy of the cached payload"


@pytest.mark.asyncio
async def test_pre_v3_1_seller_falls_through_to_full_payload() -> None:
    """A seller that ignores conditional-fetch tokens just returns the full payload.

    The cache learns nothing (no version on the response) — same cost as no cache.
    """
    cache = CatalogVersionCache()
    client = _stub_client()
    plain_wire = {"products": []}  # no v3.1 fields
    _adapter_get_products_returns(client, plain_wire)

    request = PreviewGetProductsRequest(buying_mode="wholesale")
    result = await get_products_with_cache(client, request, cache)

    assert result.success
    scope = _scope_key_for_request(
        "get_products",
        request.model_dump(mode="json", exclude_none=True),
    )
    assert cache.lookup("agent_1", scope) is None, "no version → no cache entry"


@pytest.mark.asyncio
async def test_get_signals_with_cache_attaches_token() -> None:
    cache = CatalogVersionCache()
    client = _stub_client()
    request = PreviewGetSignalsRequest(discovery_mode="wholesale")
    scope = _scope_key_for_request(
        "get_signals",
        request.model_dump(mode="json", exclude_none=True),
    )

    cached_payload = PreviewGetSignalsResponse(
        signals=[],
        catalog_version="s1",
        cache_scope="public",
    )
    cache.store(
        "agent_1",
        scope,
        CatalogVersionEntry("s1", None, "public", payload=cached_payload),
    )

    unchanged_wire = {
        "unchanged": True,
        "catalog_version": "s1",
        "cache_scope": "public",
    }
    mock_call = _adapter_get_signals_returns(client, unchanged_wire)

    result = await get_signals_with_cache(client, request, cache)

    sent_params = mock_call.call_args.args[0]
    assert sent_params.get("if_catalog_version") == "s1"
    assert result.data is not cached_payload
    assert result.data == cached_payload


# ---------------------------------------------------------------------------
# #4763 — catalog change feed
# ---------------------------------------------------------------------------


class _FakeCapabilities:
    """Test double matching the public-attribute + ``model_extra`` contract."""

    def __init__(self, stanza: dict[str, Any] | None) -> None:
        self.catalog_change_feed = stanza
        self.model_extra = {"catalog_change_feed": stanza} if stanza else {}


def test_capability_extraction_returns_typed_struct() -> None:
    caps = _FakeCapabilities(
        {
            "supported": True,
            "retention_window_days": 30,
            "webhooks_supported": True,
            "event_types": ["product.created", "product.updated"],
        },
    )
    feed = catalog_change_feed_from_capabilities(caps)
    assert feed is not None
    assert feed.supported is True
    assert feed.retention_window_days == 30
    assert feed.webhooks_supported is True
    assert feed.event_types == ("product.created", "product.updated")


def test_capability_extraction_distinguishes_missing_from_unsupported() -> None:
    """Stanza absent → ``None``; stanza present with ``supported: false`` →
    a ``CatalogChangeFeedCapabilities`` with ``supported=False``.

    Adopters use the distinction for telemetry: a missing stanza means
    "agent doesn't speak 3.1," while ``supported=False`` means "agent
    opted out of the feed despite speaking 3.1." The two get reported
    very differently to operators.
    """
    missing = catalog_change_feed_from_capabilities(_FakeCapabilities(None))
    assert missing is None

    opted_out = catalog_change_feed_from_capabilities(_FakeCapabilities({"supported": False}))
    assert opted_out is not None
    assert opted_out.supported is False


def test_capability_extraction_returns_none_when_stanza_missing() -> None:
    assert catalog_change_feed_from_capabilities(_FakeCapabilities(None)) is None


class _StubResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _StubHttpClient:
    """Captures the last call so tests can assert on URL/params/headers."""

    def __init__(self, response: _StubResponse) -> None:
        self.response = response
        self.last_get: dict[str, Any] | None = None
        self.last_post: dict[str, Any] | None = None

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str],
    ) -> _StubResponse:
        self.last_get = {"url": url, "params": params, "headers": headers}
        return self.response

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
    ) -> _StubResponse:
        self.last_post = {"url": url, "json": json, "headers": headers}
        return self.response

    async def aclose(self) -> None:
        pass


def _agent_with_auth() -> AgentConfig:
    return AgentConfig(
        id="agent_1",
        agent_uri="https://agent.example/",
        protocol=Protocol.MCP,
        auth_token="tok-1",
        auth_type="bearer",
    )


@pytest.mark.asyncio
async def test_change_feed_poll_returns_typed_page() -> None:
    body = {
        "events": [
            {
                "event_id": "01890000-0000-7000-8000-000000000001",
                "event_type": "product.updated",
                "entity_type": "product",
                "entity_id": "prod_1",
                "created_at": "2026-05-19T12:00:00Z",
                "payload": {"product_id": "prod_1"},
            },
        ],
        "has_more": False,
        "next_cursor": "01890000-0000-7000-8000-000000000001",
        "retention_window_days": 30,
    }
    http = _StubHttpClient(_StubResponse(200, body))
    feed = CatalogChangeFeedClient(_agent_with_auth(), http_client=http)

    page = await feed.poll(cursor="01890000-0000-7000-8000-000000000000", max_events=10)

    assert page.has_more is False
    assert page.next_cursor == body["next_cursor"]
    assert page.retention_window_days == 30
    assert len(page.events) == 1
    event = page.events[0]
    assert isinstance(event, CatalogEvent)
    assert event.event_type == "product.updated"

    # Auth + URL composition
    assert http.last_get is not None
    assert http.last_get["url"] == "https://agent.example/catalog/events"
    # Per spec: query params are ``cursor`` / ``limit`` / ``types``
    # (comma-joined), not ``max_events`` / ``event_types``.
    assert http.last_get["params"] == {
        "cursor": "01890000-0000-7000-8000-000000000000",
        "limit": 10,
    }
    assert http.last_get["headers"]["Authorization"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_change_feed_poll_raises_with_error_code_on_retention_expired() -> None:
    body = {
        "error_code": "RETENTION_EXPIRED",
        "message": "Cursor older than retention window",
    }
    http = _StubHttpClient(_StubResponse(410, body))
    feed = CatalogChangeFeedClient(_agent_with_auth(), http_client=http)

    with pytest.raises(CatalogChangeFeedError) as exc_info:
        await feed.poll(cursor="01700000-0000-7000-8000-000000000000")

    assert exc_info.value.status_code == 410
    assert exc_info.value.error_code == "RETENTION_EXPIRED"


@pytest.mark.asyncio
async def test_change_feed_subscribe_posts_typed_body() -> None:
    body = {"subscription_id": "sub-1", "webhook_url": "https://buyer.example/hook"}
    http = _StubHttpClient(_StubResponse(201, body))
    feed = CatalogChangeFeedClient(_agent_with_auth(), http_client=http)

    result = await feed.subscribe(
        "https://buyer.example/hook",
        ["product.created", "product.updated"],
    )

    assert result["subscription_id"] == "sub-1"
    assert http.last_post is not None
    assert http.last_post["url"] == "https://agent.example/catalog/subscriptions"
    assert http.last_post["json"] == {
        "webhook_url": "https://buyer.example/hook",
        "event_types": ["product.created", "product.updated"],
    }


@pytest.mark.asyncio
async def test_change_feed_subscribe_rejects_reserved_key_collision() -> None:
    """`extra` MUST NOT silently overwrite reserved keys."""
    http = _StubHttpClient(_StubResponse(201, {}))
    feed = CatalogChangeFeedClient(_agent_with_auth(), http_client=http)

    with pytest.raises(ValueError, match="reserved keys"):
        await feed.subscribe(
            "https://buyer.example/hook",
            ["product.created"],
            extra={"webhook_url": "https://attacker.example/oops"},
        )


@pytest.mark.asyncio
async def test_cache_helpers_emit_activity_events() -> None:
    """`_get_with_cache` must mirror `client.get_products` observability.

    Adopters who wire ``on_activity=`` for audit MUST see PROTOCOL_REQUEST
    + PROTOCOL_RESPONSE on every conditional-fetch call — including the
    304-equivalent ``unchanged: true`` path that is the whole point of
    conditional fetch.
    """
    from adcp.types.core import ActivityType

    cache = CatalogVersionCache()
    events: list[Any] = []
    client = ADCPClient(
        AgentConfig(id="agent_1", agent_uri="https://agent.example", protocol=Protocol.MCP),
        on_activity=events.append,
    )
    _adapter_get_products_returns(
        client,
        {"products": [], "catalog_version": "v1", "cache_scope": "public"},
    )

    request = PreviewGetProductsRequest(buying_mode="wholesale")
    await get_products_with_cache(client, request, cache)

    types_emitted = [e.type for e in events]
    assert ActivityType.PROTOCOL_REQUEST in types_emitted
    assert ActivityType.PROTOCOL_RESPONSE in types_emitted
    # Both events share an operation_id for correlation
    op_ids = {e.operation_id for e in events}
    assert len(op_ids) == 1
    # task_type is propagated
    assert all(e.task_type == "get_products" for e in events)


# ---------------------------------------------------------------------------
# ADCPClient wiring — client.preview namespace + catalog_change_feed_client()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_preview_namespace_routes_to_cache_helper() -> None:
    """`client.preview.get_products(...)` is the canonical adopter surface.

    Verifies it delegates to the same wire-level path as the free function
    and applies the same cache hit/miss/unchanged semantics.
    """
    cache = CatalogVersionCache()
    client = _stub_client()
    fresh_wire = {
        "products": [],
        "catalog_version": "v1",
        "cache_scope": "public",
    }
    _adapter_get_products_returns(client, fresh_wire)

    namespace = client.preview
    request = PreviewGetProductsRequest(buying_mode="wholesale")
    result = await namespace.get_products(request, cache)

    assert result.success
    # Same namespace instance is returned on a second access — property is memoized.
    assert client.preview is namespace


@pytest.mark.asyncio
async def test_client_preview_namespace_get_signals_routes_correctly() -> None:
    cache = CatalogVersionCache()
    client = _stub_client()
    _adapter_get_signals_returns(
        client,
        {"signals": [], "catalog_version": "s1", "cache_scope": "public"},
    )

    request = PreviewGetSignalsRequest(discovery_mode="wholesale")
    result = await client.preview.get_signals(request, cache)
    assert result.success


def test_client_catalog_change_feed_client_factory() -> None:
    """`client.catalog_change_feed_client()` returns a CatalogChangeFeedClient
    bound to this agent's config."""
    client = _stub_client()
    feed = client.catalog_change_feed_client()
    assert isinstance(feed, CatalogChangeFeedClient)
    # Trailing-slash trim + base URL composition
    assert feed._base_url == "https://agent.example"

    # Pre-supplied http_client is honored (and the wrapper won't close it).
    import httpx

    shared = httpx.AsyncClient()
    feed_shared = client.catalog_change_feed_client(http_client=shared)
    assert feed_shared._http_client is shared
    assert feed_shared._owns_client is False


def test_catalog_events_page_accepts_forward_compat_extras() -> None:
    """``CatalogEventsPage`` uses ``extra='allow'`` so forward-compatible
    server additions (new envelope fields the SDK hasn't seen yet) flow
    through without an SDK release."""
    page = CatalogEventsPage(
        events=(),
        has_more=False,
        next_cursor=None,
        retention_window_days=None,
        future_field="hi",  # not in the model — must pass via extras
    )
    assert (page.model_extra or {}).get("future_field") == "hi"
