"""Tests for adcp.feed_mirror.FeedMirror.

Exercises the AdCP 3.1 wholesale-feed sync pattern against real Pydantic
types: bootstrap (full load + pagination), incremental webhook application,
conditional refresh (unchanged no-op), and bulk_change re-bootstrap.

The seller side is a minimal in-memory stub satisfying FeedMirrorClient —
no HTTP mocking — so request construction and response parsing run through
the real generated request/response models.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp import (
    FeedMirror,
    FeedMirrorError,
    FeedState,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
)
from adcp.types import WholesaleFeedEvent, WholesaleFeedWebhook
from adcp.types.core import TaskResult, TaskStatus

# ---------------------------------------------------------------------------
# Fixtures: minimal-but-valid wire payloads
# ---------------------------------------------------------------------------


def make_product_dict(product_id: str, *, cpm: float = 18.5) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "name": f"Product {product_id}",
        "description": f"Description for {product_id}",
        "publisher_properties": [{"selection_type": "all", "publisher_domain": "pub.example.com"}],
        "format_ids": [],
        "delivery_type": "guaranteed",
        "pricing_options": [
            {
                "pricing_option_id": "po_cpm_v1",
                "pricing_model": "cpm",
                "currency": "USD",
                "fixed_cpm": cpm,
            }
        ],
        "reporting_capabilities": {
            "available_reporting_frequencies": ["daily"],
            "expected_delay_minutes": 60,
            "timezone": "UTC",
            "supports_webhooks": False,
            "available_metrics": ["impressions"],
            "date_range_support": "date_range",
        },
    }


def make_signal_dict(segment_id: str, *, cpm: float = 2.5) -> dict[str, Any]:
    return {
        "signal_agent_segment_id": segment_id,
        "name": f"Signal {segment_id}",
        "description": f"Description for {segment_id}",
        "signal_type": "marketplace",
        "data_provider": "Acme Data",
        "deployments": [{"type": "platform", "platform": "the-trade-desk", "is_live": True}],
        "pricing_options": [
            {"pricing_option_id": "po_cpm_1", "model": "cpm", "cpm": cpm, "currency": "USD"}
        ],
    }


def make_event(
    event_id: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "created_at": "2026-05-22T12:00:00Z",
        "payload": payload,
    }


def make_webhook(
    event: dict[str, Any],
    *,
    version: str = "v2",
    previous: str | None = "v1",
    cache_scope: str = "public",
) -> WholesaleFeedWebhook:
    body: dict[str, Any] = {
        "idempotency_key": f"idem-{event['event_id']}",
        "notification_id": event["event_id"],
        "notification_type": event["event_type"],
        "fired_at": "2026-05-22T12:00:00Z",
        "subscriber_id": "wholesale-feed-mirror",
        "account_id": "acc_acme",
        "wholesale_feed_version": version,
        "cache_scope": cache_scope,
        "event": event,
    }
    if previous is not None:
        body["previous_wholesale_feed_version"] = previous
    return WholesaleFeedWebhook.model_validate(body)


# ---------------------------------------------------------------------------
# In-memory seller stub satisfying FeedMirrorClient
# ---------------------------------------------------------------------------


class StubClient:
    """Records requests and replays scripted GetProducts/GetSignals responses.

    Each ``products``/``signals`` entry is the raw response dict for the Nth
    call (0-indexed). A callable entry receives the parsed request and the
    call count.
    """

    def __init__(
        self,
        *,
        products: list[dict[str, Any]] | None = None,
        signals: list[dict[str, Any]] | None = None,
    ) -> None:
        self._products = products or []
        self._signals = signals or []
        self.product_requests: list[GetProductsRequest] = []
        self.signal_requests: list[GetSignalsRequest] = []

    async def get_products(self, request: GetProductsRequest) -> TaskResult[GetProductsResponse]:
        idx = len(self.product_requests)
        self.product_requests.append(request)
        body = self._products[idx] if idx < len(self._products) else {"products": []}
        return TaskResult(
            status=TaskStatus.COMPLETED,
            success=True,
            data=GetProductsResponse.model_validate(body),
        )

    async def get_signals(self, request: GetSignalsRequest) -> TaskResult[GetSignalsResponse]:
        idx = len(self.signal_requests)
        self.signal_requests.append(request)
        body = self._signals[idx] if idx < len(self._signals) else {"signals": []}
        return TaskResult(
            status=TaskStatus.COMPLETED,
            success=True,
            data=GetSignalsResponse.model_validate(body),
        )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_loads_products_and_signals() -> None:
    client = StubClient(
        products=[
            {
                "products": [make_product_dict("p1"), make_product_dict("p2")],
                "wholesale_feed_version": "prod-v1",
                "pricing_version": "prod-price-v1",
                "cache_scope": "public",
            }
        ],
        signals=[
            {
                "signals": [make_signal_dict("s1")],
                "wholesale_feed_version": "sig-v1",
                "cache_scope": "public",
            }
        ],
    )
    mirror = FeedMirror(client)

    result = await mirror.bootstrap()

    assert result.product_count == 2
    assert result.signal_count == 1
    assert set(mirror.products) == {"p1", "p2"}
    assert mirror.get_product("p1").name == "Product p1"
    assert mirror.get_signal("s1").name == "Signal s1"
    assert mirror.product_state.wholesale_feed_version == "prod-v1"
    assert mirror.product_state.pricing_version == "prod-price-v1"
    assert mirror.signal_state.wholesale_feed_version == "sig-v1"


@pytest.mark.asyncio
async def test_bootstrap_sends_wholesale_mode_and_account() -> None:
    account = {"account_id": "acc_acme"}
    client = StubClient(products=[{"products": []}], signals=[{"signals": []}])
    from adcp.types import AccountReference

    mirror = FeedMirror(client, account=AccountReference.model_validate(account))

    await mirror.bootstrap()

    prod_req = client.product_requests[0]
    assert prod_req.buying_mode.value == "wholesale"
    assert prod_req.account is not None
    sig_req = client.signal_requests[0]
    assert sig_req.discovery_mode.value == "wholesale"
    assert sig_req.account is not None


@pytest.mark.asyncio
async def test_bootstrap_walks_pagination_cursor() -> None:
    client = StubClient(
        products=[
            {
                "products": [make_product_dict("p1")],
                "wholesale_feed_version": "prod-v1",
                "cache_scope": "public",
                "pagination": {"has_more": True, "cursor": "cur-1"},
            },
            {
                "products": [make_product_dict("p2")],
                "wholesale_feed_version": "prod-v1",
                "cache_scope": "public",
                "pagination": {"has_more": False},
            },
        ],
        signals=[{"signals": []}],
    )
    mirror = FeedMirror(client)

    await mirror.bootstrap("product")

    assert set(mirror.products) == {"p1", "p2"}
    # Page 0 carries no cursor; page 1 echoes the cursor from page 0.
    assert client.product_requests[0].pagination.cursor is None
    assert client.product_requests[1].pagination.cursor == "cur-1"


# ---------------------------------------------------------------------------
# Incremental webhook application
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_events_mutate_index() -> None:
    client = StubClient(
        products=[
            {
                "products": [make_product_dict("p1")],
                "wholesale_feed_version": "prod-v1",
                "cache_scope": "public",
            }
        ],
        signals=[{"signals": []}],
    )
    mirror = FeedMirror(client)
    await mirror.bootstrap()

    # product.created — add p2
    created = make_event(
        "018f0000-0000-7000-8000-000000000001",
        "product.created",
        "product",
        "p2",
        {
            "product_id": "p2",
            "product": make_product_dict("p2"),
            "applies_to": {"scope": "public"},
        },
    )
    await mirror.apply_webhook(make_webhook(created, version="prod-v2"))
    assert set(mirror.products) == {"p1", "p2"}
    assert mirror.product_state.wholesale_feed_version == "prod-v2"

    # product.priced — replace p1 pricing
    priced = make_event(
        "018f0000-0000-7000-8000-000000000002",
        "product.priced",
        "product",
        "p1",
        {
            "product_id": "p1",
            "pricing_options": [
                {
                    "pricing_option_id": "po_cpm_v2",
                    "pricing_model": "cpm",
                    "currency": "USD",
                    "fixed_cpm": 25.0,
                }
            ],
            "applies_to": {"scope": "public"},
        },
    )
    await mirror.apply_webhook(make_webhook(priced, version="prod-v3"))
    assert mirror.get_product("p1").pricing_options[0].pricing_option_id == "po_cpm_v2"

    # product.removed — drop p2
    removed = make_event(
        "018f0000-0000-7000-8000-000000000003",
        "product.removed",
        "product",
        "p2",
        {"product_id": "p2", "applies_to": {"scope": "public"}},
    )
    await mirror.apply_webhook(make_webhook(removed, version="prod-v4"))
    assert set(mirror.products) == {"p1"}


@pytest.mark.asyncio
async def test_incremental_signal_events_mutate_index() -> None:
    client = StubClient(
        products=[{"products": []}],
        signals=[
            {
                "signals": [make_signal_dict("s1")],
                "wholesale_feed_version": "sig-v1",
                "cache_scope": "public",
            }
        ],
    )
    mirror = FeedMirror(client)
    await mirror.bootstrap()

    created = make_event(
        "018f0000-0000-7000-8000-000000000010",
        "signal.created",
        "signal",
        "s2",
        {
            "signal_agent_segment_id": "s2",
            "signal": make_signal_dict("s2"),
            "applies_to": {"scope": "public"},
        },
    )
    await mirror.apply_webhook(make_webhook(created, version="sig-v2"))
    assert set(mirror.signals) == {"s1", "s2"}

    priced = make_event(
        "018f0000-0000-7000-8000-000000000011",
        "signal.priced",
        "signal",
        "s1",
        {
            "signal_agent_segment_id": "s1",
            "pricing_options": [
                {"pricing_option_id": "po_cpm_2", "model": "cpm", "cpm": 9.0, "currency": "USD"}
            ],
            "applies_to": {"scope": "public"},
        },
    )
    await mirror.apply_webhook(make_webhook(priced, version="sig-v3"))
    assert mirror.get_signal("s1").pricing_options[0].pricing_option_id == "po_cpm_2"

    removed = make_event(
        "018f0000-0000-7000-8000-000000000012",
        "signal.removed",
        "signal",
        "s2",
        {"signal_agent_segment_id": "s2", "applies_to": {"scope": "public"}},
    )
    await mirror.apply_webhook(make_webhook(removed, version="sig-v4"))
    assert set(mirror.signals) == {"s1"}


@pytest.mark.asyncio
async def test_on_event_callback_fires() -> None:
    client = StubClient(
        products=[
            {
                "products": [make_product_dict("p1")],
                "wholesale_feed_version": "prod-v1",
                "cache_scope": "public",
            }
        ],
        signals=[{"signals": []}],
    )
    seen: list[WholesaleFeedEvent] = []
    mirror = FeedMirror(client, on_event=seen.append)
    await mirror.bootstrap()

    removed = make_event(
        "018f0000-0000-7000-8000-000000000020",
        "product.removed",
        "product",
        "p1",
        {"product_id": "p1", "applies_to": {"scope": "public"}},
    )
    await mirror.apply_webhook(make_webhook(removed, version="prod-v2"))

    assert len(seen) == 1
    assert str(seen[0].event_type) == "product.removed"


# ---------------------------------------------------------------------------
# Conditional refresh (unchanged no-op)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conditional_refresh_unchanged_is_noop() -> None:
    client = StubClient(
        products=[
            {
                "products": [make_product_dict("p1")],
                "wholesale_feed_version": "prod-v1",
                "pricing_version": "prod-price-v1",
                "cache_scope": "public",
            },
            # Conditional re-read short-circuits with unchanged: true.
            {
                "unchanged": True,
                "wholesale_feed_version": "prod-v1",
                "pricing_version": "prod-price-v1",
                "cache_scope": "public",
            },
        ],
        signals=[
            {
                "signals": [make_signal_dict("s1")],
                "wholesale_feed_version": "sig-v1",
                "cache_scope": "public",
            },
            {"unchanged": True, "wholesale_feed_version": "sig-v1", "cache_scope": "public"},
        ],
    )
    mirror = FeedMirror(client)
    await mirror.bootstrap()
    assert set(mirror.products) == {"p1"}
    assert set(mirror.signals) == {"s1"}

    result = await mirror.refresh()

    assert result.unchanged is True
    assert result.products_unchanged is True
    assert result.signals_unchanged is True
    # Replica preserved, not wiped.
    assert set(mirror.products) == {"p1"}
    assert set(mirror.signals) == {"s1"}
    # The conditional read presented the cached version tokens.
    second_prod_req = client.product_requests[1]
    assert second_prod_req.if_wholesale_feed_version == "prod-v1"
    assert second_prod_req.if_pricing_version == "prod-price-v1"
    second_sig_req = client.signal_requests[1]
    assert second_sig_req.if_wholesale_feed_version == "sig-v1"


@pytest.mark.asyncio
async def test_refresh_applies_changed_feed() -> None:
    client = StubClient(
        products=[
            {
                "products": [make_product_dict("p1")],
                "wholesale_feed_version": "prod-v1",
                "cache_scope": "public",
            },
            # Changed feed: p1 dropped, p2 added.
            {
                "products": [make_product_dict("p2")],
                "wholesale_feed_version": "prod-v2",
                "cache_scope": "public",
            },
        ],
        signals=[
            {"signals": []},
            {"unchanged": True, "wholesale_feed_version": None, "cache_scope": "public"},
        ],
    )
    mirror = FeedMirror(client)
    await mirror.bootstrap()
    assert set(mirror.products) == {"p1"}

    result = await mirror.refresh("product")

    assert result.products_unchanged is False
    assert set(mirror.products) == {"p2"}
    assert mirror.product_state.wholesale_feed_version == "prod-v2"


# ---------------------------------------------------------------------------
# bulk_change re-bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_change_rebootstraps_product_feed() -> None:
    client = StubClient(
        products=[
            {
                "products": [make_product_dict("p1")],
                "wholesale_feed_version": "prod-v1",
                "cache_scope": "public",
            },
            # Re-bootstrap after bulk_change returns a fresh full feed.
            {
                "products": [make_product_dict("p2"), make_product_dict("p3")],
                "wholesale_feed_version": "prod-v2",
                "cache_scope": "public",
            },
        ],
        signals=[{"signals": []}],
    )
    mirror = FeedMirror(client)
    await mirror.bootstrap()
    assert set(mirror.products) == {"p1"}

    bulk = make_event(
        "018f0000-0000-7000-8000-000000000030",
        "wholesale_feed.bulk_change",
        "feed",
        "op-1",
        {
            "summary": "Q3 2026 rate card refresh",
            "affected_count": 2,
            "applies_to": {"scope": "public"},
            "affected_entity_type": "product",
        },
    )
    result = await mirror.apply_webhook(make_webhook(bulk, version="prod-v2"))

    assert result is not None
    # Only the product feed was re-read; the signal feed was not refetched.
    assert set(mirror.products) == {"p2", "p3"}
    assert len(client.product_requests) == 2
    assert len(client.signal_requests) == 1
    assert mirror.product_state.wholesale_feed_version == "prod-v2"


@pytest.mark.asyncio
async def test_bulk_change_rebootstraps_signal_feed() -> None:
    client = StubClient(
        products=[{"products": []}],
        signals=[
            {
                "signals": [make_signal_dict("s1")],
                "wholesale_feed_version": "sig-v1",
                "cache_scope": "public",
            },
            {
                "signals": [make_signal_dict("s2")],
                "wholesale_feed_version": "sig-v2",
                "cache_scope": "public",
            },
        ],
    )
    mirror = FeedMirror(client)
    await mirror.bootstrap()
    assert set(mirror.signals) == {"s1"}

    bulk = make_event(
        "018f0000-0000-7000-8000-000000000031",
        "wholesale_feed.bulk_change",
        "feed",
        "op-2",
        {
            "summary": "signal refresh",
            "affected_count": 1,
            "applies_to": {"scope": "public"},
            "affected_entity_type": "signal",
        },
    )
    await mirror.apply_webhook(make_webhook(bulk, version="sig-v2"))

    assert set(mirror.signals) == {"s2"}
    # Product feed untouched by a signal-only bulk change.
    assert len(client.product_requests) == 1
    assert len(client.signal_requests) == 2


# ---------------------------------------------------------------------------
# State persistence hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_store_restores_version_on_bootstrap() -> None:
    saved: dict[str, FeedState] = {
        "product": FeedState(wholesale_feed_version="prod-v1", cache_scope="public"),
    }

    class Store:
        async def load(self, entity: str) -> FeedState | None:
            return saved.get(entity)

        async def save(self, entity: str, state: FeedState) -> None:
            saved[entity] = state

    client = StubClient(
        products=[
            {"unchanged": True, "wholesale_feed_version": "prod-v1", "cache_scope": "public"}
        ],
        signals=[{"signals": []}],
    )
    mirror = FeedMirror(client, state_store=Store())

    result = await mirror.bootstrap()

    # Restored version was presented on the first read, so the seller
    # short-circuited as unchanged.
    assert result.products_unchanged is True
    assert client.product_requests[0].if_wholesale_feed_version == "prod-v1"


@pytest.mark.asyncio
async def test_state_store_persists_version_on_webhook() -> None:
    saved: dict[str, FeedState] = {}

    class Store:
        async def load(self, entity: str) -> FeedState | None:
            return saved.get(entity)

        async def save(self, entity: str, state: FeedState) -> None:
            saved[entity] = state

    client = StubClient(
        products=[
            {
                "products": [make_product_dict("p1")],
                "wholesale_feed_version": "prod-v1",
                "cache_scope": "public",
            }
        ],
        signals=[{"signals": []}],
    )
    mirror = FeedMirror(client, state_store=Store())
    await mirror.bootstrap()

    removed = make_event(
        "018f0000-0000-7000-8000-000000000040",
        "product.removed",
        "product",
        "p1",
        {"product_id": "p1", "applies_to": {"scope": "public"}},
    )
    await mirror.apply_webhook(make_webhook(removed, version="prod-v9"))

    assert saved["product"].wholesale_feed_version == "prod-v9"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_webhook_rejects_type_mismatch() -> None:
    client = StubClient(products=[{"products": []}], signals=[{"signals": []}])
    mirror = FeedMirror(client)

    event = make_event(
        "018f0000-0000-7000-8000-000000000050",
        "product.removed",
        "product",
        "p1",
        {"product_id": "p1", "applies_to": {"scope": "public"}},
    )
    webhook = make_webhook(event)
    # Corrupt the envelope discriminator so it disagrees with the event.
    object.__setattr__(webhook, "notification_type", "product.created")

    with pytest.raises(FeedMirrorError, match="notification_type"):
        await mirror.apply_webhook(webhook)


@pytest.mark.asyncio
async def test_failed_read_raises() -> None:
    class FailingClient:
        async def get_products(
            self, request: GetProductsRequest
        ) -> TaskResult[GetProductsResponse]:
            return TaskResult(status=TaskStatus.FAILED, success=False, error="boom")

        async def get_signals(self, request: GetSignalsRequest) -> TaskResult[GetSignalsResponse]:
            return TaskResult(status=TaskStatus.COMPLETED, success=True, data=GetSignalsResponse())

    mirror = FeedMirror(FailingClient())

    with pytest.raises(FeedMirrorError, match="get_products"):
        await mirror.bootstrap("product")
