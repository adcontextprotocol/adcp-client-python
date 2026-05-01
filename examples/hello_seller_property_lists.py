"""Hello-seller-property-lists — minimal PropertyListsPlatform adopter.

The smallest possible ``property-lists`` seller. Five-method CRUD
plus fetch-token issuance.

Run::

    uv run python examples/hello_seller_property_lists.py
"""

from __future__ import annotations

import secrets
from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SingletonAccounts,
    serve,
)


class HelloPropertyListsSeller(DecisioningPlatform):
    """The canonical minimal ``property-lists`` adopter.

    A single in-memory dict simulates the property-list store. Real
    adopters back this with their CMS / inventory database. Note:
    ``delete_property_list`` is security-critical — it MUST revoke
    the per-list fetch_token AND signal cache invalidation
    downstream. Compromise-driven revocation routes through the
    same path.
    """

    capabilities = DecisioningCapabilities(specialisms=["property-lists"])
    accounts = SingletonAccounts(account_id="hello-property-lists")

    def __init__(self) -> None:
        super().__init__()
        self._lists: dict[str, dict[str, Any]] = {}

    def create_property_list(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        list_id = f"pl-{secrets.token_urlsafe(8)}"
        token = secrets.token_urlsafe(24)
        self._lists[list_id] = {
            "list_id": list_id,
            "name": getattr(req, "name", "untitled"),
            "fetch_token": token,
            "properties": getattr(req, "properties", []),
        }
        return {
            "list_id": list_id,
            "fetch_url": f"https://example.com/lists/{list_id}",
            "fetch_token": token,
        }

    def update_property_list(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        list_id = getattr(req, "list_id", None)
        if list_id and list_id in self._lists:
            self._lists[list_id]["properties"] = getattr(req, "properties", [])
        return {"list_id": list_id}

    def get_property_list(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        list_id = getattr(req, "list_id", None)
        return self._lists.get(list_id, {"list_id": list_id, "properties": []})

    def list_property_lists(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {"property_lists": list(self._lists.values())}

    def delete_property_list(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        """Security-critical: revokes the fetch_token AND signals cache
        invalidation downstream. Compromise-driven revocation MUST
        also trigger this path."""
        list_id = getattr(req, "list_id", None)
        if list_id:
            self._lists.pop(list_id, None)
        return {"list_id": list_id, "deleted": True}


def main() -> None:
    """Boot the seller on http://localhost:3001/mcp."""
    serve(HelloPropertyListsSeller(), auto_emit_completion_webhooks=False)


if __name__ == "__main__":
    main()
