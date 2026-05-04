"""Hello-seller-with-webhooks — canonical ``WebhookSender`` + supervisor wiring.

Extends ``hello_seller.py`` with a wired :class:`InMemoryWebhookDeliverySupervisor`
so sync-completion webhooks are delivered to buyers who register
``push_notification_config.url``.  Uses :meth:`WebhookSender.from_bearer_token`
as the auth mode — no key management, simplest first step.

Run::

    WEBHOOK_BEARER_TOKEN=<your-token> uv run python examples/hello_seller_with_webhooks.py

The server boots on http://localhost:3001/mcp.  Any buyer that registers
``push_notification_config.url`` on a ``create_media_buy`` request receives a
completion notification POSTed with ``Authorization: Bearer <token>``.

To use RFC 9421 JWK signing instead (AdCP spec baseline, required for buyers
that verify body signatures), swap :meth:`~WebhookSender.from_bearer_token`
for :meth:`~WebhookSender.from_jwk`.  See ``docs/handler-authoring.md#webhooks``
for the full constructor comparison.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow importing hello_seller as a sibling module when run as a script.
sys.path.insert(0, str(Path(__file__).parent))

from hello_seller import HelloSeller  # type: ignore[import]  # noqa: E402

from adcp.decisioning import serve
from adcp.webhook_sender import WebhookSender
from adcp.webhook_supervisor import InMemoryWebhookDeliverySupervisor

if __name__ == "__main__":
    token = os.environ.get("WEBHOOK_BEARER_TOKEN", "")
    if not token:
        import warnings

        warnings.warn(
            "WEBHOOK_BEARER_TOKEN is not set; using 'dev-fixture-token'. "
            "Set WEBHOOK_BEARER_TOKEN=<real-token> before connecting real buyers.",
            category=UserWarning,
            stacklevel=1,
        )
        token = "dev-fixture-token"
    sender = WebhookSender.from_bearer_token(token)
    # InMemoryWebhookDeliverySupervisor wraps the sender with retry
    # (exponential backoff, 3 attempts) and per-endpoint circuit breakers.
    # Pass webhook_supervisor= rather than webhook_sender= in production.
    supervisor = InMemoryWebhookDeliverySupervisor(sender=sender)
    serve(
        HelloSeller(),
        name="hello-seller-with-webhooks",
        webhook_supervisor=supervisor,
    )
