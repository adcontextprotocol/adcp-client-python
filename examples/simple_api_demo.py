"""
Comprehensive demo of the Simple API.

The .simple API provides JavaScript-like ergonomics:
- Keyword arguments instead of request objects
- Direct data access (result.products vs result.data.products)
- Async/await pattern
- Automatic error handling
"""

import asyncio
from adcp import ADCPClient, ADCPSimpleError
from adcp.types import AgentConfig, Protocol


async def demo_get_products(client: ADCPClient):
    """Demo: Get advertising products."""
    print("\n=== Demo: Get Products ===")

    try:
        result = await client.simple.get_products(
            brief="Coffee brands targeting millennials in NYC",
            brand_manifest={
                "name": "Artisan Coffee Co.",
                "url": "https://artisancoffee.com",
                "description": "Handcrafted coffee roasters",
            },
            format_ids=["display_300x250", "native_content"],
        )

        print(f"✅ Found {len(result.products)} products")
        for product in result.products:
            print(f"  • {product.name}")
            print(f"    {product.description[:80]}...")
            print(f"    Formats: {len(product.format_ids)}")

    except ADCPSimpleError as e:
        print(f"❌ Error: {e}")


async def demo_list_authorized_properties(client: ADCPClient):
    """Demo: List authorized publisher properties."""
    print("\n=== Demo: List Authorized Properties ===")

    try:
        result = await client.simple.list_authorized_properties()

        print(f"✅ Found {len(result.publisher_domains)} publisher domains")
        for domain in result.publisher_domains:
            print(f"  • {domain}")

    except ADCPSimpleError as e:
        print(f"❌ Error: {e}")


async def demo_sync_creatives(client: ADCPClient):
    """Demo: Sync creatives to publisher library."""
    print("\n=== Demo: Sync Creatives ===")

    try:
        result = await client.simple.sync_creatives(
            media_buy_id="mb_12345",
            creatives=[
                {
                    "creative_id": "creative_1",
                    "name": "Summer Sale Banner",
                    "format_id": "display_300x250",
                    "assets": {
                        "image": "https://cdn.example.com/summer-banner.jpg",
                        "headline": "Summer Sale - 50% Off",
                        "cta": "Shop Now",
                    },
                },
                {
                    "creative_id": "creative_2",
                    "name": "Product Spotlight",
                    "format_id": "native_content",
                    "assets": {
                        "title": "Discover Our New Blend",
                        "description": "Experience the rich flavor of our newest coffee blend",
                        "image": "https://cdn.example.com/new-blend.jpg",
                    },
                },
            ],
        )

        print(f"✅ Synced {len(result.creatives)} creatives")
        for creative in result.creatives:
            status = creative.get("status", "unknown")
            creative_id = creative.get("creative_id", "unknown")
            print(f"  • {creative_id}: {status}")

    except ADCPSimpleError as e:
        print(f"❌ Error: {e}")


async def demo_error_handling(client: ADCPClient):
    """Demo: Error handling with simple API."""
    print("\n=== Demo: Error Handling ===")

    # Example 1: Missing required field
    try:
        result = await client.simple.get_products()  # No brief provided
        print("✅ This shouldn't happen")
    except Exception as e:
        print(f"Expected validation error: {type(e).__name__}")

    # Example 2: Task needs input
    print("\nSimple API automatically raises errors for:")
    print("  - Async submitted tasks (use webhook instead)")
    print("  - Tasks needing input (agent needs clarification)")
    print("  - Failed tasks (errors during execution)")


async def main():
    """Run all demos."""
    print("=" * 60)
    print("Simple API Demo")
    print("=" * 60)
    print("\nThe .simple API matches JavaScript SDK ergonomics:")
    print("  ✓ Keyword arguments")
    print("  ✓ Direct data access")
    print("  ✓ Async/await")
    print("  ✓ Automatic error handling")

    # Configure agent
    config = AgentConfig(
        id="demo_agent",
        agent_uri="https://demo-agent.adcontextprotocol.org",
        protocol=Protocol.A2A,
        auth_token="demo-token",
    )

    # Create client
    client = ADCPClient(config)

    # Run demos
    await demo_get_products(client)
    await demo_list_authorized_properties(client)
    await demo_sync_creatives(client)
    await demo_error_handling(client)

    print("\n" + "=" * 60)
    print("Demo complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
