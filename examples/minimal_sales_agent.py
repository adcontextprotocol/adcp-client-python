#!/usr/bin/env python3
"""Minimal AdCP sales agent — a single-file MCP server.

A local news publisher ("Riverdale Gazette") with four advertising products:
newsletter sponsorship, website display, mobile display, and event sponsorship.

AdCP:Buy flow:
    1. Buyer calls get_adcp_capabilities → discovers this agent supports media_buy
    2. Buyer calls get_products → browses the product catalog and pricing options
    3. Buyer calls create_media_buy with selected products → gets confirmed packages

Run:
    python examples/minimal_sales_agent.py

Test with Claude Code:
    claude mcp add-json gazette '{"type":"streamableHttp","url":"http://localhost:8000/mcp"}'

Test with curl:
    curl -X POST http://localhost:8000/mcp \\
      -H 'Content-Type: application/json' \\
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
           "params":{"protocolVersion":"2025-03-26",
                     "clientInfo":{"name":"test","version":"1.0"},
                     "capabilities":{}}}'

For production agents with many operations, see adcp.server.ADCPHandler
which provides a base class with all AdCP operations and MCP tool registration.

Requires:
    pip install adcp mcp uvicorn
"""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

from adcp.types import (
    CpmPricingOption,
    CreateMediaBuyErrorResponse,
    CreateMediaBuySuccessResponse,
    DeliveryMeasurement,
    DeliveryType,
    Error,
    FlatRatePricingOption,
    FormatId,
    GetAdcpCapabilitiesResponse,
    GetProductsResponse,
    Package,
    Product,
    PublisherPropertiesAll,
)

# -- Agent URL (used in FormatId to identify this agent) ----------------------

AGENT_URL = "http://localhost:8000/mcp"

# -- Product catalog (in-memory) ----------------------------------------------

PRODUCTS: list[Product] = [
    Product(
        product_id="newsletter_sponsorship",
        name="Daily Newsletter Sponsorship",
        description=(
            "Exclusive sponsor placement in the Riverdale Gazette daily email "
            "newsletter. Reaches 12,000 subscribers. Includes logo, 50-word "
            "blurb, and a click-through link."
        ),
        publisher_properties=[
            PublisherPropertiesAll(
                publisher_domain="riverdalenews.example.com",
                selection_type="all",
            )
        ],
        format_ids=[
            FormatId(agent_url=AGENT_URL, id="email_banner_600x200"),
        ],
        delivery_type=DeliveryType.guaranteed,
        delivery_measurement=DeliveryMeasurement(provider="Internal"),
        pricing_options=[
            FlatRatePricingOption(
                pricing_option_id="newsletter_weekly",
                pricing_model="flat_rate",
                fixed_price=250.00,
                currency="USD",
            ),
            FlatRatePricingOption(
                pricing_option_id="newsletter_monthly",
                pricing_model="flat_rate",
                fixed_price=800.00,
                currency="USD",
            ),
        ],
    ),
    Product(
        product_id="website_display",
        name="Website Display — Run of Site",
        description=(
            "300x250 display ads on riverdalenews.example.com. "
            "Average 150K monthly page views. Served across all sections."
        ),
        publisher_properties=[
            PublisherPropertiesAll(
                publisher_domain="riverdalenews.example.com",
                selection_type="all",
            )
        ],
        format_ids=[
            FormatId(agent_url=AGENT_URL, id="display_300x250"),
        ],
        delivery_type=DeliveryType.non_guaranteed,
        delivery_measurement=DeliveryMeasurement(provider="Google Ad Manager"),
        pricing_options=[
            CpmPricingOption(
                pricing_option_id="website_cpm",
                pricing_model="cpm",
                floor_price=8.00,
                currency="USD",
            ),
        ],
    ),
    Product(
        product_id="mobile_display",
        name="Mobile Display — In-App",
        description=(
            "320x50 banner ads in the Riverdale Gazette mobile app. "
            "Average 45K monthly active users."
        ),
        publisher_properties=[
            PublisherPropertiesAll(
                publisher_domain="app.riverdalenews.example.com",
                selection_type="all",
            )
        ],
        format_ids=[
            FormatId(agent_url=AGENT_URL, id="mobile_320x50"),
        ],
        delivery_type=DeliveryType.non_guaranteed,
        delivery_measurement=DeliveryMeasurement(provider="Google Ad Manager"),
        pricing_options=[
            CpmPricingOption(
                pricing_option_id="mobile_cpm",
                pricing_model="cpm",
                floor_price=5.00,
                currency="USD",
            ),
        ],
    ),
    Product(
        product_id="event_sponsorship",
        name="Annual River Festival Sponsorship",
        description=(
            "Title sponsor of Riverdale's annual River Festival. Includes "
            "booth space, logo on all print materials, 3 social media posts, "
            "and a 30-second PA announcement."
        ),
        publisher_properties=[
            PublisherPropertiesAll(
                publisher_domain="riverdalenews.example.com",
                selection_type="all",
            )
        ],
        format_ids=[
            FormatId(agent_url=AGENT_URL, id="event_sponsorship"),
        ],
        delivery_type=DeliveryType.guaranteed,
        delivery_measurement=DeliveryMeasurement(provider="Internal"),
        pricing_options=[
            FlatRatePricingOption(
                pricing_option_id="event_title",
                pricing_model="flat_rate",
                fixed_price=5000.00,
                currency="USD",
            ),
        ],
    ),
]

PRODUCTS_BY_ID = {p.product_id: p for p in PRODUCTS}

# -- Pricing lookup (for validating create_media_buy) -------------------------

PRICING_OPTIONS: dict[str, Any] = {}
for _product in PRODUCTS:
    for _po in _product.pricing_options:
        # pricing_option_id is accessible directly thanks to RootModel proxy
        PRICING_OPTIONS[_po.pricing_option_id] = _po

# -- MCP server ----------------------------------------------------------------

mcp = FastMCP(
    "Riverdale Gazette",
    instructions=(
        "You are the advertising sales agent for the Riverdale Gazette, "
        "a local news publisher. You sell newsletter sponsorships, website "
        "and mobile display ads, and event sponsorships."
    ),
)


@mcp.tool()
async def get_adcp_capabilities() -> dict[str, Any]:
    """Get AdCP capabilities supported by this agent.

    Call this first to discover what protocols this agent supports.
    """
    return GetAdcpCapabilitiesResponse(
        adcp={"major_versions": [3]},
        supported_protocols=["media_buy"],
    ).model_dump(mode="json", exclude_none=True)


@mcp.tool()
async def get_products(
    context: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Get advertising products from the catalog.

    Call this after get_adcp_capabilities to browse available products
    and their pricing options before creating a media buy.
    """
    products = PRODUCTS

    # Filter by product_ids if provided
    if filters and "product_ids" in filters:
        ids = set(filters["product_ids"])
        products = [p for p in products if p.product_id in ids]

    return GetProductsResponse(
        products=products,
    ).model_dump(mode="json", exclude_none=True)


@mcp.tool()
async def create_media_buy(
    packages: list[dict[str, Any]],
    buyer_ref: str = "anonymous",
    proposal_id: str | None = None,
) -> dict[str, Any]:
    """Create a media buy from selected products.

    Requires product_id and pricing_option_id from get_products.
    Each package must include: product_id, pricing_option_id, budget.
    """
    confirmed_packages: list[Package] = []

    for pkg in packages:
        product_id = pkg.get("product_id")
        pricing_option_id = pkg.get("pricing_option_id")

        if product_id not in PRODUCTS_BY_ID:
            return CreateMediaBuyErrorResponse(
                errors=[Error(code="INVALID_PRODUCT", message=f"Unknown product: {product_id}")]
            ).model_dump(mode="json", exclude_none=True)

        if pricing_option_id and pricing_option_id not in PRICING_OPTIONS:
            return CreateMediaBuyErrorResponse(
                errors=[
                    Error(
                        code="INVALID_PRICING_OPTION",
                        message=f"Unknown pricing option: {pricing_option_id}",
                    )
                ]
            ).model_dump(mode="json", exclude_none=True)

        confirmed_packages.append(
            Package(
                package_id=f"pkg-{uuid.uuid4().hex[:8]}",
                product_id=product_id,
                pricing_option_id=pricing_option_id,
                budget=pkg.get("budget"),
            )
        )

    media_buy_id = f"mb-{uuid.uuid4().hex[:8]}"

    return CreateMediaBuySuccessResponse(
        media_buy_id=media_buy_id,
        buyer_ref=buyer_ref,
        packages=confirmed_packages,
    ).model_dump(mode="json", exclude_none=True)


# -- Entry point ---------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
