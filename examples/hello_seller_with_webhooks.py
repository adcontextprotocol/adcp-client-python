"""hello_seller_with_webhooks — minimal seller with completion webhook wiring.

Extends the HelloSeller surface from ``examples/hello_seller.py`` with a
``WebhookSender`` and ``InMemoryWebhookDeliverySupervisor`` so buyers who
register ``push_notification_config.url`` actually receive completion
notifications.

Run::

    uv run python examples/hello_seller_with_webhooks.py

The seller boots on port 3001. Send a ``create_media_buy`` request with
``push_notification_config.url`` set to see the webhook fire after the
sync response returns.
"""

from __future__ import annotations

import os

from adcp.decisioning import serve
from adcp.webhook_sender import WebhookSender
from adcp.webhook_supervisor import InMemoryWebhookDeliverySupervisor

# Re-use HelloSeller's full 9-method sales-non-guaranteed surface.
from examples.hello_seller import HelloSeller  # type: ignore[import]

if __name__ == "__main__":
    # Bearer-token sender — simplest constructor, no private key needed.
    # Swap for WebhookSender.from_jwk(my_jwk) in production for RFC 9421
    # body signing, or from_standard_webhooks_secret("whsec_...") for
    # Svix / Resend interop.
    token = os.environ.get("WEBHOOK_BEARER_TOKEN", "dev-fixture-token")
    sender = WebhookSender.from_bearer_token(token)

    # Supervisor adds retry (3 attempts, exponential backoff) and per-endpoint
    # circuit breaking. In-process state only; use PgWebhookDeliverySupervisor
    # for durable retry across restarts.
    supervisor = InMemoryWebhookDeliverySupervisor(sender)

    serve(
        HelloSeller(),
        name="hello-seller-webhooks",
        webhook_supervisor=supervisor,
        # auto_emit_completion_webhooks defaults to True — remove this line
        # or leave it absent; it is shown here for documentation clarity.
        auto_emit_completion_webhooks=True,
    )
