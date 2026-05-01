"""Hello-seller-collection-lists — minimal CollectionListsPlatform adopter.

The smallest possible ``collection-lists`` seller. Five-method CRUD
plus fetch-token issuance. Pattern-mirrors ``property-lists`` —
adopters typically implement both side-by-side.

Run::

    uv run python examples/hello_seller_collection_lists.py
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


class HelloCollectionListsSeller(DecisioningPlatform):
    """The canonical minimal ``collection-lists`` adopter."""

    capabilities = DecisioningCapabilities(specialisms=["collection-lists"])
    accounts = SingletonAccounts(account_id="hello-collection-lists")

    def __init__(self) -> None:
        super().__init__()
        self._lists: dict[str, dict[str, Any]] = {}

    def create_collection_list(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        list_id = f"cl-{secrets.token_urlsafe(8)}"
        token = secrets.token_urlsafe(24)
        self._lists[list_id] = {
            "list_id": list_id,
            "name": getattr(req, "name", "untitled"),
            "fetch_token": token,
            "items": getattr(req, "items", []),
        }
        return {
            "list_id": list_id,
            "fetch_url": f"https://example.com/lists/{list_id}",
            "fetch_token": token,
        }

    def update_collection_list(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        list_id = getattr(req, "list_id", None)
        if list_id and list_id in self._lists:
            self._lists[list_id]["items"] = getattr(req, "items", [])
        return {"list_id": list_id}

    def get_collection_list(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        list_id = getattr(req, "list_id", None)
        return self._lists.get(list_id, {"list_id": list_id, "items": []})

    def list_collection_lists(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        return {"collection_lists": list(self._lists.values())}

    def delete_collection_list(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        """Security-critical: revokes the fetch_token. See
        ``hello_seller_property_lists.delete_property_list`` for the
        same security contract."""
        list_id = getattr(req, "list_id", None)
        if list_id:
            self._lists.pop(list_id, None)
        return {"list_id": list_id, "deleted": True}


def main() -> None:
    """Boot the seller on http://localhost:3001/mcp."""
    serve(HelloCollectionListsSeller(), auto_emit_completion_webhooks=False)


if __name__ == "__main__":
    main()
