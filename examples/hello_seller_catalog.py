"""Hello-seller-catalog — minimal sales-catalog-driven adopter.

The smallest possible ``sales-catalog-driven`` seller. The specialism
adds ``sync_catalogs`` on top of the standard sales surface, which lets
buyers discover existing catalog state and push catalog updates before
``sync_creatives`` / catalog-referenced media buys.

Run::

    uv run python examples/hello_seller_catalog.py
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SingletonAccounts,
    serve,
)


class HelloCatalogSeller(DecisioningPlatform):
    """Minimal ``sales-catalog-driven`` adopter.

    ``sync_catalogs`` is required when claiming this specialism —
    ``validate_platform`` hard-fails at boot if the method is absent.

    Discovery mode: ``req.catalogs is None`` means the buyer wants
    existing catalog state without modification. Always check before
    applying mutations.

    Return a list of catalog-result rows (ergonomic form) or a fully-shaped
    ``SyncCatalogsSuccessResponse``. The framework wraps the list form to
    ``{catalogs: [...]}`` on the wire.
    """

    capabilities = DecisioningCapabilities(
        specialisms=["sales-catalog-driven"],
        supported_billing=("agent",),
    )
    accounts = SingletonAccounts(account_id="hello-catalog")

    def get_products(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {"products": []}

    def create_media_buy(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {"media_buy_id": "mb_1", "status": "active"}

    def update_media_buy(
        self, media_buy_id: str, patch: Any, ctx: RequestContext[Any]
    ) -> dict[str, Any]:
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {"creatives": []}

    def get_media_buy_delivery(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {"media_buy_deliveries": []}

    def sync_catalogs(self, req: Any, ctx: RequestContext[Any]) -> list[dict[str, Any]]:
        """Sync product catalogs with the platform.

        Spec-required guard: ``delete_missing=True`` with ``catalogs=None``
        is undefined — reject it rather than silently deleting buyer-managed
        catalogs.

        Discovery mode (``req.catalogs is None``): return existing catalogs
        without any mutation.
        """
        if getattr(req, "delete_missing", False) and getattr(req, "catalogs", None) is None:
            raise AdcpError("INVALID_REQUEST", field="catalogs")

        if getattr(req, "catalogs", None) is None:
            # Discovery mode — return existing catalog state, no mutations.
            return []

        # Push mode — upsert the supplied catalogs.
        return [
            {
                "catalog_id": getattr(c, "catalog_id", str(i)),
                "action": "created",
                "item_count": 0,
            }
            for i, c in enumerate(req.catalogs or [])
        ]


def main() -> None:
    """Boot the seller on http://localhost:3001/mcp.

    ``auto_emit_completion_webhooks=False`` opts out so this example
    boots without a ``webhook_sender``. In production, wire
    ``webhook_sender=`` for buyer notification.
    """
    serve(HelloCatalogSeller(), auto_emit_completion_webhooks=False)


if __name__ == "__main__":
    main()
