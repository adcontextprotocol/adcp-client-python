"""Hello-seller-with-webhooks — canonical ``WebhookSender`` + supervisor wiring.

Extends ``hello_seller.py`` with a wired :class:`InMemoryWebhookDeliverySupervisor`
and explicitly enables the legacy sync-completion compatibility mode. Uses
:meth:`WebhookSender.from_bearer_token` as the auth mode — no key management,
simplest first step.

Run::

    WEBHOOK_BEARER_TOKEN=<your-token> uv run python examples/hello_seller_with_webhooks.py

The server boots on http://localhost:3001/mcp. Any buyer that registers
``push_notification_config.url`` on a ``create_media_buy`` request receives a
duplicate completion notification POSTed with ``Authorization: Bearer <token>``.
This behavior is non-conformant and exists only to migrate integrations that
depended on the SDK's former default. New integrations consume the inline
terminal response. Pollable handoffs need no publisher; push-configured
handoffs require an external durable outbox and cannot use this supervisor.

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
    # Explicit/manual and migration delivery only; not a production
    # TaskHandoff publisher under the beta.5 durability contract.
    supervisor = InMemoryWebhookDeliverySupervisor(sender=sender)
    serve(
        HelloSeller(),
        name="hello-seller-with-webhooks",
        webhook_supervisor=supervisor,
        auto_emit_completion_webhooks=True,
    )
