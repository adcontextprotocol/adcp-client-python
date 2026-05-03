#!/usr/bin/env python3
"""Typed handler params — minimal demonstration (#214).

Shows a handler that declares its ``params`` as a Pydantic model
rather than ``dict[str, Any]``. The dispatcher validates and
deserialises the request at the boundary, so the handler body works
with typed attributes instead of ``params.get(...)``.

Mixing typed and dict signatures on the same handler is supported —
useful for migrating a large seller one method at a time.

Run::

    python examples/typed_handler_demo.py

Then call ``get_products`` from any MCP client. A request missing
``buying_mode`` returns a structured ``INVALID_REQUEST`` AdCP error.
"""

from __future__ import annotations

from typing import Any

from adcp.server import ADCPHandler, ServeConfig, ToolContext, serve
from adcp.types import (
    GetAdcpCapabilitiesResponse,
    GetProductsRequest,
    GetProductsResponse,
    Product,
    PublisherPropertiesAll,
)


class TypedSeller(ADCPHandler):
    """Minimal handler demonstrating typed dispatch.

    Only two methods are overridden:

    - ``get_adcp_capabilities`` — required by the AdCP spec.
    - ``get_products`` — typed ``params: GetProductsRequest``. The
      dispatcher deserialises before calling, so ``params.buying_mode``
      is a typed enum attribute, not a dict lookup.
    """

    _agent_type = "typed-demo-seller"

    async def get_adcp_capabilities(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        return GetAdcpCapabilitiesResponse(
            adcp={"major_versions": [3]},
            supported_protocols=["media_buy"],
        ).model_dump(mode="json", exclude_none=True)

    async def get_products(
        self,
        params: GetProductsRequest,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        # Typed attribute access — no params.get("buying_mode") anywhere.
        # Pydantic already validated the shape; the handler focuses on
        # business logic.
        requested_mode = params.buying_mode.value

        products: list[Product] = [
            Product(
                product_id="demo-product",
                name=f"Demo — {requested_mode} mode",
                description="A demonstration product for the typed-dispatch example.",
                publisher_properties=[
                    PublisherPropertiesAll(
                        publisher_domain="example.com",
                        selection_type="all",
                    )
                ],
                format_ids=[],
                delivery_type="non_guaranteed",
                pricing_options=[],
            )
        ]

        return GetProductsResponse(products=products).model_dump(mode="json", exclude_none=True)


if __name__ == "__main__":
    # Demo only — ``serve()`` defaults to binding 0.0.0.0 with no auth.
    # For production, wrap with an auth middleware (see
    # ``examples/mcp_with_auth_middleware.py``) and restrict the host
    # via reverse-proxy config or the ``port=`` / bind-host hooks.

    # ServeConfig bundles all options — IDE autocomplete shows each field
    # with its doc.  The legacy kwargs form still works unchanged.
    config = ServeConfig(name="typed-demo-seller", transport="streamable-http")
    serve(TypedSeller(), config=config)
