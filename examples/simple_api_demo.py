"""Demo of the simplified API accessor.

This example demonstrates the .simple accessor available on all ADCPClient instances:
- Accepts kwargs directly (no request objects needed)
- Returns unwrapped data (no TaskResult.data unwrapping)
- Raises exceptions on errors

Compare this to the standard API which requires explicit request objects
and TaskResult unwrapping.
"""

import asyncio

# Import test agents
from adcp.testing import creative_agent, test_agent
from adcp.types.generated import GetProductsRequest


async def demo_simple_api():
    """Demo the .simple accessor API."""
    print("=== Simple API Demo (client.simple.*) ===\n")

    # Simple kwargs-based call, direct data return
    products = await test_agent.simple.get_products(
        brief="Coffee subscription service for busy professionals",
    )

    print(f"Found {len(products.products)} products")
    if products.products:
        product = products.products[0]
        print(f"  - {product.name}")
        print(f"    {product.description}\n")

    # List formats with simple API
    formats = await test_agent.simple.list_creative_formats()
    print(f"Found {len(formats.formats)} creative formats")
    if formats.formats:
        fmt = formats.formats[0]
        print(f"  - {fmt.name}")
        print(f"    {fmt.description}\n")

    # Creative agent also has .simple accessor
    print("Creative agent preview:")
    try:
        preview = await creative_agent.simple.preview_creative(
            manifest={
                "format_id": {
                    "id": "banner_300x250",
                    "agent_url": "https://creative.adcontextprotocol.org",
                },
                "assets": {},
            }
        )
        if preview.previews:
            print(f"  Generated {len(preview.previews)} preview(s)\n")
    except Exception as e:
        print(f"  Preview failed (expected for demo): {e}\n")


async def demo_standard_api_comparison():
    """Compare with standard API for reference."""
    print("=== Standard API (for comparison) ===\n")

    # Standard API: More verbose but full control over error handling
    request = GetProductsRequest(
        brief="Coffee subscription service for busy professionals",
    )

    result = await test_agent.get_products(request)

    if result.success and result.data:
        print(f"Found {len(result.data.products)} products")
        if result.data.products:
            product = result.data.products[0]
            print(f"  - {product.name}")
            print(f"    {product.description}\n")
    else:
        print(f"Error: {result.error}\n")


async def demo_production_client():
    """Show that .simple works on any ADCPClient."""
    print("=== Simple API on Production Clients ===\n")

    # Create a production client
    from adcp import ADCPClient, AgentConfig, Protocol
    from adcp.testing import TEST_AGENT_TOKEN

    client = ADCPClient(
        AgentConfig(
            id="my-agent",
            agent_uri="https://test-agent.adcontextprotocol.org/mcp/",
            protocol=Protocol.MCP,
            auth_token=TEST_AGENT_TOKEN,  # Public test token (rate-limited)
        )
    )

    # Both APIs available
    print("Standard API:")
    result = await client.get_products(GetProductsRequest(brief="Test"))
    print(f"  Result type: {type(result).__name__}")
    print(f"  Has .success: {hasattr(result, 'success')}")
    print(f"  Has .data: {hasattr(result, 'data')}\n")

    print("Simple API:")
    try:
        products = await client.simple.get_products(brief="Test")
        print(f"  Result type: {type(products).__name__}")
        print(f"  Direct access to .products: {hasattr(products, 'products')}")
    except Exception as e:
        print(f"  (Expected error for demo: {e})")


def demo_sync_usage():
    """Show how to use simple API in sync contexts."""
    print("\n=== Using Simple API in Sync Contexts ===\n")

    print("The simple API is async-only, but you can use asyncio.run() for sync contexts:")
    print()
    print("  # In a Jupyter notebook or sync function:")
    print("  import asyncio")
    print("  from adcp.testing import test_agent")
    print()
    print("  products = asyncio.run(test_agent.simple.get_products(brief='Coffee'))")
    print("  print(f'Found {len(products.products)} products')")
    print()
    print("  # Or create an async function and run it:")
    print("  async def my_function():")
    print("      products = await test_agent.simple.get_products(brief='Coffee')")
    print("      return products")
    print()
    print("  result = asyncio.run(my_function())")
    print()


async def main():
    """Run all demos."""
    print("\n" + "=" * 60)
    print("ADCP Python SDK - Simple API Demo")
    print("=" * 60 + "\n")

    # Demo simple API
    await demo_simple_api()

    # Show standard API for comparison
    await demo_standard_api_comparison()

    # Show it works on any client
    await demo_production_client()

    # Show sync usage pattern
    demo_sync_usage()

    print("\n" + "=" * 60)
    print("Key Differences:")
    print("=" * 60)
    print("\nSimple API (client.simple.*):")
    print("  ✓ Kwargs instead of request objects")
    print("  ✓ Direct data return (no unwrapping)")
    print("  ✓ Raises exceptions on errors")
    print("  ✓ Available on ALL ADCPClient instances")
    print("  ✓ Use asyncio.run() for sync contexts")
    print("  → Best for: documentation, examples, quick testing, notebooks")
    print("\nStandard API (client.*):")
    print("  ✓ Explicit request objects (type-safe)")
    print("  ✓ TaskResult wrapper (full status info)")
    print("  ✓ Explicit error handling")
    print("  → Best for: production code, complex workflows, webhooks")
    print("\n")


if __name__ == "__main__":
    asyncio.run(main())
