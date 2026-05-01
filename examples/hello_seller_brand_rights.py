"""Hello-seller-brand-rights — minimal BrandRightsPlatform adopter.

The smallest possible ``brand-rights`` seller. Three required methods:
``get_brand_identity``, ``get_rights``, ``acquire_rights``.

The ``acquire_rights`` method has a 4-arm discriminated success union
(acquired / pending / rejected / error) — rejection-as-data per the
Protocol; the buyer doesn't need to disambiguate exception types.

Run::

    uv run python examples/hello_seller_brand_rights.py
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


class HelloBrandRightsSeller(DecisioningPlatform):
    """The canonical minimal ``brand-rights`` adopter."""

    capabilities = DecisioningCapabilities(specialisms=["brand-rights"])
    accounts = SingletonAccounts(account_id="hello-rights")

    def get_brand_identity(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Read brand identity record."""
        return {
            "brand": {
                "brand_id": "example-brand",
                "name": "Example Brand",
                "asset_pack_url": "https://cdn.example.com/brand-pack/example.zip",
            }
        }

    def get_rights(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """List rights matching brand + use query."""
        return {
            "rights": [
                {
                    "rights_id": "right-1",
                    "use": "display_advertising",
                    "term_start": "2026-01-01",
                    "term_end": "2026-12-31",
                    "status": "available",
                }
            ]
        }

    def acquire_rights(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Acquire rights — 4-arm discriminated success union.

        Adopters pick one shape per call:
        * ``{"status": "acquired", ...}`` — rights granted
        * ``{"status": "pending", ...}`` — needs human approval
        * ``{"status": "rejected", "reason": ...}`` — denied as data
        * ``{"status": "error", "message": ...}`` — rights system
          failure the buyer can retry against
        """
        return {
            "status": "acquired",
            "rights_id": "right-1",
            "acquisition_id": "acq-1",
        }


def main() -> None:
    """Boot the seller on http://localhost:3001/mcp.

    ``auto_emit_completion_webhooks=False`` opts out so this example
    boots without a ``webhook_sender``. In production, wire
    ``webhook_sender=`` for buyer notification.
    """
    serve(HelloBrandRightsSeller(), auto_emit_completion_webhooks=False)


if __name__ == "__main__":
    main()
