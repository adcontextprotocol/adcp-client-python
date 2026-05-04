"""Side-car runtime entrypoint.

Launched as a separate container alongside salesagent's existing
adcp-server. Listens on port 8081 (salesagent's adcp-server is on
8080); nginx routes by `X-Tenant-Id: <experiment-tenant>` header.

Run:
    python -m examples.salesagent_sidecar.serve_sidecar

Environment:
    EXPERIMENT_TENANT_IDS  comma-separated tenant ids the side-car serves
                           (also disabled in salesagent's two cross-tenant
                            schedulers via the local-fork patch — see
                            Step 0.4 of the experiment plan)
    SIDECAR_PORT          default 8081
    DATABASE_URL          shared with salesagent's adcp-server
"""

from __future__ import annotations

import os

from adcp.decisioning.compose import compose_method
from adcp.decisioning.serve import serve
from adcp.webhook_sender import WebhookSender

from .account_store import (
    SalesagentAccountStore,
    SalesagentBuyerAgentRegistry,
)
from .gam_platform import GAMPlatform
from .hitl_gate import make_hitl_before_hook


def build_platform() -> GAMPlatform:
    """Construct the GAMPlatform with HITL gates wired."""
    platform = GAMPlatform()

    # Wrap each mutating method with the HITL gate.
    # compose_method returns a type-preserving wrapper; the binding
    # syntax replaces the bound method on the instance.
    platform.create_media_buy = compose_method(  # type: ignore[method-assign]
        inner=platform.create_media_buy,
        before=make_hitl_before_hook("create_media_buy"),
    )
    platform.update_media_buy = compose_method(  # type: ignore[method-assign]
        inner=platform.update_media_buy,
        before=make_hitl_before_hook("update_media_buy"),
    )
    platform.sync_creatives = compose_method(  # type: ignore[method-assign]
        inner=platform.sync_creatives,
        before=make_hitl_before_hook("sync_creatives"),
    )

    return platform


def build_webhook_sender() -> WebhookSender | None:
    """Configure SDK F12 auto-emit signing.

    Per Step 0.6: salesagent's `X-Webhook-Signature` scheme is incompatible
    with SDK's `X-AdCP-Signature` (`from_adcp_legacy_hmac`). For SDK→SDK
    testing in the experiment, configure with a controlled secret. The
    test buyer is `adcp.WebhookReceiver` with the same secret.
    """
    secret = os.environ.get("SIDECAR_WEBHOOK_SECRET")
    if not secret:
        # No secret configured → auto-emit disabled silently.
        return None

    return WebhookSender.from_adcp_legacy_hmac(
        secret=secret.encode("utf-8"),
        key_id=os.environ.get("SIDECAR_WEBHOOK_KEY_ID", "kid_sidecar_experiment_v1"),
    )


def main() -> None:
    """Start the side-car serving on the experiment tenant."""
    experiment_tenant_ids = {
        t.strip() for t in os.environ.get("EXPERIMENT_TENANT_IDS", "").split(",") if t.strip()
    }
    if not experiment_tenant_ids:
        raise SystemExit(
            "EXPERIMENT_TENANT_IDS not set. Required for tenant-scoped "
            "routing — the side-car only serves these tenants; everyone "
            "else routes to salesagent's existing runtime."
        )

    platform = build_platform()
    platform.accounts = SalesagentAccountStore(  # type: ignore[assignment]
        experiment_tenant_ids=experiment_tenant_ids,
    )

    serve(
        platform,
        name="salesagent-sidecar-experiment",
        buyer_agent_registry=SalesagentBuyerAgentRegistry(),
        webhook_sender=build_webhook_sender(),
        auto_emit_completion_webhooks=True,
        port=int(os.environ.get("SIDECAR_PORT", "8081")),
        host=os.environ.get("SIDECAR_HOST", "0.0.0.0"),
        transport="both",  # MCP + A2A
    )


if __name__ == "__main__":
    main()
