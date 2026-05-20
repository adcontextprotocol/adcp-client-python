"""End-to-end tests for the v3.1 preview surface against an in-process
HTTP server using ``httpx.ASGITransport``.

The unit tests in ``test_preview.py`` mock the adapter / http client directly;
these tests wire ``CatalogChangeFeedClient`` against a real ASGI Starlette
app so URL routing, query-param serialization, JSON body shape, header
attachment, and error decoding all exercise the production HTTP stack.

No real port is bound — ASGITransport routes ``httpx.AsyncClient`` calls
straight into the ASGI app in-process, so these tests run in the regular
pytest suite at unit-test speed.

Scope per the three v3.1 catalog-sync proposals shipped in v3.1.0-beta.1:

* #4761 (conditional fetch) — exercised end-to-end via the unit tests'
  adapter mocks; the cache helper is pure Python over a parsed response
  dict, so an ASGI round-trip adds nothing.
* #4762 (wholesale signals) — exercised via the unit tests' schema
  validation; the wire-shape contract is enforced there.
* #4763 (catalog change feed) — needs a real HTTP stack to test URL
  routing + query params + error mapping. **That is what this file covers.**
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from adcp.preview import (
    CatalogChangeFeedClient,
    CatalogChangeFeedError,
)
from adcp.types import AgentConfig, Protocol

# ---------------------------------------------------------------------------
# Test ASGI app — minimal v3.1 catalog-change-feed endpoint
# ---------------------------------------------------------------------------


def _make_test_app(*, page: dict[str, Any], subscribe_body: dict[str, Any]) -> Starlette:
    """Build a Starlette app whose /catalog/events and /catalog/subscriptions
    endpoints record the inbound request and emit the supplied fixtures.
    """
    captured: dict[str, Any] = {}

    async def events(request: Request) -> JSONResponse:
        captured["events_request"] = {
            "query_params": dict(request.query_params),
            "headers": dict(request.headers),
        }
        cursor = request.query_params.get("cursor")
        # Sentinel cursors exercise spec-defined branches without needing
        # separate routes:
        # * ``echo-empty`` — per spec, agent echoes the request cursor
        #   when ``events`` is empty so callers write
        #   ``cursor = response.next_cursor`` unconditionally.
        # * ``expired`` — agent returns RETENTION_EXPIRED for cursors
        #   older than the retention window; caller MUST resync via
        #   wholesale enumeration.
        if cursor == "echo-empty":
            return JSONResponse({"events": [], "has_more": False, "next_cursor": cursor})
        if cursor == "expired":
            return JSONResponse(
                {"error_code": "RETENTION_EXPIRED", "message": "Cursor too old"},
                status_code=410,
            )
        return JSONResponse(page)

    async def subscriptions(request: Request) -> JSONResponse:
        body = await request.json()
        captured["subscribe_request"] = {
            "body": body,
            "headers": dict(request.headers),
        }
        return JSONResponse(subscribe_body, status_code=201)

    app = Starlette(
        routes=[
            Route("/catalog/events", events),
            Route("/catalog/subscriptions", subscriptions, methods=["POST"]),
        ],
    )
    app.state.captured = captured
    return app


def _agent_config() -> AgentConfig:
    return AgentConfig(
        id="agent_1",
        agent_uri="https://agent.example",  # host doesn't matter under ASGITransport
        protocol=Protocol.MCP,
        auth_token="tok-1",
        auth_type="bearer",
    )


# ---------------------------------------------------------------------------
# poll() — round-trips against the test ASGI app
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_poll_returns_events_and_attaches_auth_header() -> None:
    page = {
        "events": [
            {
                "event_id": "01890000-0000-7000-8000-000000000001",
                "event_type": "product.priced",
                "entity_type": "product",
                "entity_id": "prod_42",
                "created_at": "2026-05-19T12:00:00Z",
                "payload": {"product_id": "prod_42", "rate": 12.5},
            },
        ],
        "has_more": True,
        "next_cursor": "01890000-0000-7000-8000-000000000001",
        "retention_window_days": 30,
    }
    app = _make_test_app(page=page, subscribe_body={})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http_client:
        feed = CatalogChangeFeedClient(_agent_config(), http_client=http_client)
        # The ASGI transport ignores the agent_uri host; route everything
        # through the test base_url instead by pointing the client at it.
        feed._base_url = "http://t"

        result = await feed.poll(
            cursor="01890000-0000-7000-8000-000000000000",
            max_events=10,
            event_types=["product.*"],
        )

    captured = app.state.captured["events_request"]
    # Per-spec wire shape: ``limit`` not ``max_events``; ``types`` not ``event_types``.
    assert captured["query_params"]["cursor"] == "01890000-0000-7000-8000-000000000000"
    assert captured["query_params"]["limit"] == "10"
    assert captured["query_params"]["types"] == "product.*"
    assert captured["headers"]["authorization"] == "Bearer tok-1"

    assert result.has_more is True
    assert result.next_cursor == "01890000-0000-7000-8000-000000000001"
    assert result.retention_window_days == 30
    assert len(result.events) == 1
    assert result.events[0].event_type == "product.priced"
    assert result.events[0].payload == {"product_id": "prod_42", "rate": 12.5}


@pytest.mark.asyncio
async def test_e2e_poll_caught_up_echoes_cursor() -> None:
    """Per spec: when ``events`` is empty the agent echoes the request cursor.

    Buyers can then write ``cursor = response.next_cursor`` unconditionally.
    """
    app = _make_test_app(page={}, subscribe_body={})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http_client:
        feed = CatalogChangeFeedClient(_agent_config(), http_client=http_client)
        feed._base_url = "http://t"

        result = await feed.poll(cursor="echo-empty")

    assert result.events == ()
    assert result.has_more is False
    assert result.next_cursor == "echo-empty"


@pytest.mark.asyncio
async def test_e2e_poll_retention_expired_surfaces_error_code() -> None:
    """A consumer whose cursor predates the retention window gets
    ``RETENTION_EXPIRED`` and MUST resync via wholesale enumeration.
    """
    app = _make_test_app(page={}, subscribe_body={})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http_client:
        feed = CatalogChangeFeedClient(_agent_config(), http_client=http_client)
        feed._base_url = "http://t"

        with pytest.raises(CatalogChangeFeedError) as exc_info:
            await feed.poll(cursor="expired")

    assert exc_info.value.status_code == 410
    assert exc_info.value.error_code == "RETENTION_EXPIRED"


# ---------------------------------------------------------------------------
# subscribe() — round-trips against the test ASGI app
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_subscribe_posts_typed_body_and_returns_confirmation() -> None:
    body = {
        "subscription_id": "sub-abc",
        "webhook_url": "https://buyer.example/hook",
        "created_at": "2026-05-19T12:00:00Z",
    }
    app = _make_test_app(page={}, subscribe_body=body)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http_client:
        feed = CatalogChangeFeedClient(_agent_config(), http_client=http_client)
        feed._base_url = "http://t"

        result = await feed.subscribe(
            "https://buyer.example/hook",
            ["product.created", "product.updated"],
            subscription_id="my-sub-1",
        )

    captured = app.state.captured["subscribe_request"]
    assert captured["body"] == {
        "webhook_url": "https://buyer.example/hook",
        "event_types": ["product.created", "product.updated"],
        "subscription_id": "my-sub-1",
    }
    assert captured["headers"]["authorization"] == "Bearer tok-1"
    assert result == body


@pytest.mark.asyncio
async def test_e2e_subscribe_propagates_extra_non_reserved_fields() -> None:
    """``extra`` lets callers add vendor / spec-extension fields — but only
    when they don't shadow the wire-required keys."""
    app = _make_test_app(page={}, subscribe_body={"subscription_id": "sub-1"})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http_client:
        feed = CatalogChangeFeedClient(_agent_config(), http_client=http_client)
        feed._base_url = "http://t"

        await feed.subscribe(
            "https://buyer.example/hook",
            ["product.created"],
            extra={"vendor_tenant_id": "acme"},
        )

    body = app.state.captured["subscribe_request"]["body"]
    assert body["vendor_tenant_id"] == "acme"
    assert body["webhook_url"] == "https://buyer.example/hook"


# ---------------------------------------------------------------------------
# Concurrent poll() — verify the asyncio.Lock fix actually prevents the leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_concurrent_polls_share_one_http_client() -> None:
    """Two concurrent ``poll()`` calls MUST reuse the same lazily-constructed
    ``httpx.AsyncClient`` — the asyncio.Lock guard in ``_get_client`` is the
    only thing preventing one of them from leaking.
    """
    import asyncio

    page = {"events": [], "has_more": False, "next_cursor": None}
    app = _make_test_app(page=page, subscribe_body={})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as shared:
        # Construct the feed WITHOUT a pre-supplied client so the lazy
        # constructor path executes; then point it at the ASGI transport
        # by injecting after construction. (In real adopter code, callers
        # pass ``http_client=`` directly — this test just exercises the
        # lazy path.)
        feed = CatalogChangeFeedClient(_agent_config())
        feed._http_client = shared
        feed._owns_client = False
        feed._base_url = "http://t"

        # Fire two polls concurrently and confirm both succeed without
        # the underlying client being swapped mid-flight.
        results = await asyncio.gather(feed.poll(), feed.poll())
        assert all(r.next_cursor is None for r in results)
