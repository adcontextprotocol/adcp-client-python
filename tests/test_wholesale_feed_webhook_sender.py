from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from adcp import NotificationConfig
from adcp.webhooks import WebhookSender


def _removed_event(event_type: str, *, scope: str = "public") -> dict[str, object]:
    event_id = "018f13f8-7b40-7000-8000-000000000123"
    if event_type == "product.removed":
        return {
            "event_id": event_id,
            "event_type": "product.removed",
            "entity_type": "product",
            "entity_id": "prod_1",
            "created_at": "2026-05-23T12:00:00Z",
            "payload": {
                "product_id": "prod_1",
                "applies_to": {"scope": scope},
            },
        }
    if event_type == "signal.removed":
        return {
            "event_id": event_id,
            "event_type": "signal.removed",
            "entity_type": "signal",
            "entity_id": "seg_1",
            "created_at": "2026-05-23T12:00:00Z",
            "payload": {
                "signal_agent_segment_id": "seg_1",
                "applies_to": {"scope": scope},
            },
        }
    raise AssertionError(event_type)


async def _capturing_sender() -> tuple[WebhookSender, list[httpx.Request], httpx.AsyncClient]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebhookSender.from_bearer_token("test-token", client=client), captured, client


@pytest.mark.asyncio
async def test_send_wholesale_feed_product_event_envelope() -> None:
    sender, captured, client = await _capturing_sender()
    fired_at = datetime(2026, 5, 23, 12, 30, tzinfo=timezone.utc)

    async with client:
        result = await sender.send_wholesale_feed(
            url="https://buyer.example/webhooks/catalog",
            subscriber_id="buyer-primary",
            account_id="acct_1",
            notification_type="product.removed",
            wholesale_feed_version="wf_2",
            previous_wholesale_feed_version="wf_1",
            cache_scope="public",
            event=_removed_event("product.removed"),
            fired_at=fired_at,
            idempotency_key="whk_product_removed_001",
            subscription_event_types=["product.removed", "signal.removed"],
        )

    assert result.ok
    body = json.loads(captured[0].content)
    assert body["idempotency_key"] == "whk_product_removed_001"
    assert body["notification_id"] == "018f13f8-7b40-7000-8000-000000000123"
    assert body["notification_type"] == "product.removed"
    assert body["subscriber_id"] == "buyer-primary"
    assert body["account_id"] == "acct_1"
    assert body["wholesale_feed_version"] == "wf_2"
    assert body["previous_wholesale_feed_version"] == "wf_1"
    assert body["cache_scope"] == "public"
    assert body["event"]["event_type"] == "product.removed"
    assert captured[0].headers["authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_send_wholesale_feed_signal_event_envelope() -> None:
    sender, captured, client = await _capturing_sender()
    subscription = NotificationConfig.model_validate(
        {
            "subscriber_id": "audit-bus",
            "url": "https://buyer.example/webhooks/catalog",
            "event_types": ["signal.removed"],
        }
    )

    async with client:
        await sender.send_wholesale_feed(
            url=str(subscription.url),
            subscriber_id=subscription.subscriber_id,
            account_id="acct_1",
            notification_type="signal.removed",
            wholesale_feed_version="swf_9",
            cache_scope="account",
            event=_removed_event("signal.removed", scope="account"),
            idempotency_key="whk_signal_removed_001",
            subscription_event_types=subscription.event_types,
        )

    body = json.loads(captured[0].content)
    assert body["notification_type"] == "signal.removed"
    assert body["subscriber_id"] == "audit-bus"
    assert body["account_id"] == "acct_1"
    assert body["wholesale_feed_version"] == "swf_9"
    assert body["cache_scope"] == "account"
    assert body["event"]["entity_type"] == "signal"
    assert UUID(body["notification_id"]) == UUID(body["event"]["event_id"])


@pytest.mark.asyncio
async def test_send_wholesale_feed_to_subscription_uses_config_fields() -> None:
    sender, captured, client = await _capturing_sender()
    subscription = NotificationConfig.model_validate(
        {
            "subscriber_id": "buyer-primary",
            "url": "https://buyer.example/webhooks/catalog",
            "event_types": ["product.removed"],
        }
    )

    async with client:
        await sender.send_wholesale_feed_to_subscription(
            subscription=subscription,
            account_id="acct_1",
            notification_type="product.removed",
            wholesale_feed_version="wf_2",
            cache_scope="public",
            event=_removed_event("product.removed"),
            idempotency_key="whk_subscription_product_removed_001",
        )

    body = json.loads(captured[0].content)
    assert str(captured[0].url) == "https://buyer.example/webhooks/catalog"
    assert body["subscriber_id"] == "buyer-primary"
    assert body["notification_type"] == "product.removed"


@pytest.mark.asyncio
async def test_send_wholesale_feed_rejects_event_type_mismatch() -> None:
    sender, _captured, client = await _capturing_sender()

    async with client:
        with pytest.raises(ValueError, match="notification_type must match"):
            await sender.send_wholesale_feed(
                url="https://buyer.example/webhooks/catalog",
                subscriber_id="buyer-primary",
                account_id="acct_1",
                notification_type="signal.removed",
                wholesale_feed_version="wf_2",
                cache_scope="public",
                event=_removed_event("product.removed"),
            )


@pytest.mark.asyncio
async def test_send_wholesale_feed_rejects_unsubscribed_event_type() -> None:
    sender, _captured, client = await _capturing_sender()

    async with client:
        with pytest.raises(ValueError, match="subscription's event_types"):
            await sender.send_wholesale_feed(
                url="https://buyer.example/webhooks/catalog",
                subscriber_id="buyer-primary",
                account_id="acct_1",
                notification_type="product.removed",
                wholesale_feed_version="wf_2",
                cache_scope="public",
                event=_removed_event("product.removed"),
                subscription_event_types=["signal.removed"],
            )
