"""Hello-seller-audience — minimal AudiencePlatform adopter.

The smallest possible ``audience-sync`` seller. One method:
``sync_audiences`` accepts buyer-supplied first-party audiences with
delta upsert.

Run::

    uv run python examples/hello_seller_audience.py
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SingletonAccounts,
    serve,
)


class HelloAudienceSeller(DecisioningPlatform):
    """The canonical minimal ``audience-sync`` adopter.

    Note the platform method signature: ``sync_audiences(audiences,
    ctx)`` — the wire shape carries ``audiences[]`` on the request
    body, but the framework's arg-projector unpacks it so the platform
    method receives the list directly. Cleaner than reaching through
    ``req.audiences``.
    """

    capabilities = DecisioningCapabilities(specialisms=["audience-sync"])
    accounts = SingletonAccounts(account_id="hello-audience")

    def sync_audiences(
        self,
        audiences: list[Any],
        ctx: RequestContext[Any],
    ) -> list[dict[str, Any]]:
        """Upsert each audience and return per-row sync status. Returning
        a list (rather than the full ``SyncAudiencesSuccessResponse``)
        is the ergonomic arm — the framework wraps to
        ``{audiences: [...]}`` on the wire."""
        return [
            {
                "audience_id": getattr(a, "audience_id", str(i)),
                "status": "synced",
                "match_rate": 0.85,
                "estimated_size": 100_000,
            }
            for i, a in enumerate(audiences)
        ]


def main() -> None:
    """Boot the seller on http://localhost:3001/mcp.

    ``auto_emit_completion_webhooks=False`` opts out so this example
    boots without a ``webhook_sender``. In production, wire
    ``webhook_sender=`` for buyer notification.
    """
    serve(HelloAudienceSeller(), auto_emit_completion_webhooks=False)


if __name__ == "__main__":
    main()
