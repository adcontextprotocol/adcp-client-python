"""Hello-seller-content-standards — minimal ContentStandardsPlatform adopter.

The smallest possible ``content-standards`` seller. Six required CRUD +
calibration + validation methods. Two optional analyzer reads
(``get_media_buy_artifacts``, ``get_creative_features``) are omitted —
the framework's UNSUPPORTED_FEATURE gate surfaces them as such to
buyers who call them.

Run::

    uv run python examples/hello_seller_content_standards.py
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


class HelloContentStandardsSeller(DecisioningPlatform):
    """The canonical minimal ``content-standards`` adopter."""

    capabilities = DecisioningCapabilities(specialisms=["content-standards"])
    accounts = SingletonAccounts(account_id="hello-standards")

    def list_content_standards(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {
            "content_standards": [
                {
                    "standards_id": "std-default",
                    "name": "Default Content Standards",
                    "version": "1.0.0",
                }
            ]
        }

    def get_content_standards(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {
            "content_standards": {
                "standards_id": "std-default",
                "name": "Default Content Standards",
                "version": "1.0.0",
                "policies": [],
            }
        }

    def create_content_standards(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {"standards_id": "std-new"}

    def update_content_standards(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {"standards_id": getattr(req, "standards_id", "std-default")}

    def calibrate_content(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        """Score content against published standards."""
        return {"score": 0.92, "violations": []}

    def validate_content_delivery(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        """Post-flight conformance check."""
        return {"passed": True, "violations": []}


def main() -> None:
    """Boot the seller on http://localhost:3001/mcp."""
    serve(HelloContentStandardsSeller(), auto_emit_completion_webhooks=False)


if __name__ == "__main__":
    main()
