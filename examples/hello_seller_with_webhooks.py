"""hello_seller_with_webhooks — minimal seller with completion webhook wiring.

Demonstrates ``WebhookSender`` + ``InMemoryWebhookDeliverySupervisor`` so buyers
who register ``push_notification_config.url`` actually receive completion
notifications.

Run from the repo root::

    uv run python examples/hello_seller_with_webhooks.py

The seller boots on port 3001. Send a ``create_media_buy`` request with
``push_notification_config.url`` set to see the webhook fire after the
sync response returns.

See ``docs/handler-authoring.md#webhooks`` for the full wiring guide.
"""

from __future__ import annotations

import os
from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
    serve,
)
from adcp.webhook_sender import WebhookSender
from adcp.webhook_supervisor import InMemoryWebhookDeliverySupervisor


class _WebhookSeller(DecisioningPlatform):
    """Minimal sales-non-guaranteed seller used by this example."""

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        channels=["display"],
        pricing_models=["cpm"],
    )
    accounts = SingletonAccounts(account_id="hello")

    def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"products": [{"product_id": "display-rotation", "name": "Display rotation"}]}

    def create_media_buy(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"media_buy_id": "mb-1", "status": "active", "packages": []}

    def update_media_buy(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"media_buy_id": "mb-1", "status": "active", "packages": []}

    def sync_creatives(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"creatives": []}

    def get_media_buy_delivery(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"delivery": []}


if __name__ == "__main__":
    # Bearer-token sender — simplest constructor, no private key needed.
    # Swap for WebhookSender.from_jwk(my_jwk) in production for RFC 9421
    # body signing, or from_standard_webhooks_secret("whsec_...", key_id="wh-1")
    # for Svix / Resend interop.
    token = os.environ.get("WEBHOOK_BEARER_TOKEN", "dev-fixture-token")
    sender = WebhookSender.from_bearer_token(token)

    # Supervisor adds retry (3 attempts, exponential backoff) and per-endpoint
    # circuit breaking. In-process state only; use PgWebhookDeliverySupervisor
    # for durable retry across restarts.
    supervisor = InMemoryWebhookDeliverySupervisor(sender)

    serve(
        _WebhookSeller(),
        name="hello-seller-webhooks",
        webhook_supervisor=supervisor,
    )
