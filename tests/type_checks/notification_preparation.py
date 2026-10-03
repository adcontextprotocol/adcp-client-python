"""Public preparation APIs accept wire mappings and public Pydantic types."""

from collections.abc import Mapping
from typing import Any

from adcp import PreparedWebhook, WebhookSender
from adcp.types import PrincipalChangedWebhook, WholesaleFeedEvent


def prepare_principal(payload: PrincipalChangedWebhook) -> PreparedWebhook:
    return WebhookSender.prepare_principal_changed(
        url="https://buyer.example/hooks", payload=payload
    )


def prepare_account(payload: Mapping[str, Any]) -> PreparedWebhook:
    return WebhookSender.prepare_account_status_changed(
        url="https://buyer.example/hooks", payload=payload
    )


def prepare_feed(event: WholesaleFeedEvent) -> PreparedWebhook:
    return WebhookSender.prepare_wholesale_feed(
        url="https://buyer.example/hooks",
        subscriber_id="buyer",
        account_id="acct",
        notification_type="product.removed",
        wholesale_feed_version="wf_1",
        cache_scope="public",
        event=event,
    )


async def send_principal(sender: WebhookSender, payload: PrincipalChangedWebhook) -> bool:
    return (
        await sender.send_principal_changed(url="https://buyer.example/hooks", payload=payload)
    ).ok
