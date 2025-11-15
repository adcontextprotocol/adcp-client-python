#!/usr/bin/env python3
"""Test Helpers Demo - Using Pre-configured Test Agents.

This example shows how to use the built-in test helpers for quick testing and examples.
"""

from __future__ import annotations

import asyncio

from adcp.client import ADCPMultiAgentClient
from adcp.testing import (
    create_test_agent,
    test_agent,
    test_agent_a2a,
    test_agent_client,
    test_agent_no_auth,
)
from adcp.types.generated import GetProductsRequest, ListCreativeFormatsRequest


async def simplest_example() -> None:
    """Example 1: Simplest Possible Usage.

    Use the pre-configured test agent directly - no setup needed!
    """
    print("🎯 Example 1: Simplest Usage with test_agent")
    print("=" * 43)
    print()

    try:
        # Just import and use - that's it!
        result = await test_agent.get_products(
            GetProductsRequest(
                brief="Premium coffee subscription service",
                promoted_offering="Artisan coffee deliveries",
            )
        )

        if result.success and result.data:
            print(f"✅ Success! Found {len(result.data.products)} products")
            print("   Protocol: MCP")
            print()
        else:
            print(f"❌ Error: {result.error}")
            print()
    except Exception as e:
        print(f"❌ Network error: {e}")
        print()


async def protocol_comparison() -> None:
    """Example 2: Testing Both Protocols.

    Use both A2A and MCP test agents to compare behavior.
    """
    print("🔄 Example 2: Protocol Comparison (A2A vs MCP)")
    print("=" * 46)
    print()

    request = GetProductsRequest(
        brief="Sustainable fashion brands",
        promoted_offering="Eco-friendly clothing",
    )

    try:
        print("Testing MCP protocol...")
        mcp_result = await test_agent.get_products(request)
        print(f"  MCP: {'✅' if mcp_result.success else '❌'}")

        print("Testing A2A protocol...")
        a2a_result = await test_agent_a2a.get_products(request)
        print(f"  A2A: {'✅' if a2a_result.success else '❌'}")
        print()
    except Exception as e:
        print(f"❌ Error: {e}")
        print()


async def multi_agent_example() -> None:
    """Example 3: Multi-Agent Testing.

    Use the test_agent_client for parallel operations.
    """
    print("🌐 Example 3: Multi-Agent Operations")
    print("=" * 36)
    print()

    try:
        print(f"Testing with {len(test_agent_client.agent_ids)} agents in parallel...")

        # Run the same query on both agents in parallel
        results = await test_agent_client.get_products(
            GetProductsRequest(
                brief="Tech gadgets for remote work",
                promoted_offering="Ergonomic workspace solutions",
            )
        )

        print("\nResults:")
        for i, result in enumerate(results, 1):
            print(f"  {i}. {'✅' if result.success else '❌'}")
        print()
    except Exception as e:
        print(f"❌ Error: {e}")
        print()


async def custom_test_agent() -> None:
    """Example 4: Custom Test Agent Configuration.

    Create a custom test agent with modifications.
    """
    print("⚙️  Example 4: Custom Test Agent Configuration")
    print("=" * 46)
    print()

    # Create a custom config with your own ID
    custom_config = create_test_agent(
        id="my-custom-test",
        timeout=60.0,
    )

    print("Created custom config:")
    print(f"  ID: {custom_config.id}")
    print(f"  Protocol: {custom_config.protocol}")
    print(f"  URI: {custom_config.agent_uri}")
    print(f"  Timeout: {custom_config.timeout}s")
    print()

    # Use it with a client
    client = ADCPMultiAgentClient([custom_config])
    agent = client.agent("my-custom-test")

    try:
        result = await agent.get_products(
            GetProductsRequest(
                brief="Travel packages",
                promoted_offering="European vacations",
            )
        )

        print(f"Result: {'✅ Success' if result.success else '❌ Failed'}")
        print()
    except Exception as e:
        print(f"❌ Error: {e}")
        print()
    finally:
        await client.close()


async def auth_vs_no_auth_comparison() -> None:
    """Example 5: Authenticated vs Unauthenticated Requests.

    Compare behavior between authenticated and unauthenticated test agents.
    Useful for testing how agents handle different authentication states.
    """
    print("🔐 Example 5: Authentication Comparison")
    print("=" * 39)
    print()

    request = GetProductsRequest(
        brief="Coffee subscription service",
        promoted_offering="Premium coffee",
    )

    try:
        # Test with authentication
        print("Testing WITH authentication (MCP)...")
        auth_result = await test_agent.get_products(request)
        auth_success = "✅" if auth_result.success else "❌"
        auth_count = len(auth_result.data.products) if auth_result.data else 0
        print(f"  {auth_success} With Auth: {auth_count} products")

        # Test without authentication
        print("Testing WITHOUT authentication (MCP)...")
        no_auth_result = await test_agent_no_auth.get_products(request)
        no_auth_success = "✅" if no_auth_result.success else "❌"
        no_auth_count = len(no_auth_result.data.products) if no_auth_result.data else 0
        print(f"  {no_auth_success} No Auth: {no_auth_count} products")

        # Compare results
        print()
        if auth_count != no_auth_count:
            print("  💡 Note: Different results with/without auth!")
            print(f"     Auth returned {auth_count} products")
            print(f"     No auth returned {no_auth_count} products")
        else:
            print("  💡 Note: Same results with/without auth")

        print()
    except Exception as e:
        print(f"❌ Error: {e}")
        print()


async def various_operations() -> None:
    """Example 6: Testing Different Operations.

    Show various ADCP operations with test agents.
    """
    print("🎬 Example 6: Various ADCP Operations")
    print("=" * 37)
    print()

    try:
        # Get products
        print("1. Getting products...")
        products = await test_agent.get_products(
            GetProductsRequest(
                brief="Coffee brands",
                promoted_offering="Premium coffee",
            )
        )
        success = "✅" if products.success else "❌"
        count = len(products.data.products) if products.data else 0
        print(f"   {success} Products: {count}")

        # List creative formats
        print("2. Listing creative formats...")
        formats = await test_agent.list_creative_formats(ListCreativeFormatsRequest())
        success = "✅" if formats.success else "❌"
        count = len(formats.data.formats) if formats.data else 0
        print(f"   {success} Formats: {count}")

        print()
    except Exception as e:
        print(f"❌ Error: {e}")
        print()


async def main() -> None:
    """Main function - run all examples."""
    print("\n📚 ADCP Test Helpers - Demo Examples")
    print("=" * 37)
    print("These examples show how to use pre-configured test agents\n")

    await simplest_example()
    await protocol_comparison()
    await multi_agent_example()
    await custom_test_agent()
    await auth_vs_no_auth_comparison()
    await various_operations()

    print("💡 Key Takeaways:")
    print("   • test_agent = Pre-configured MCP test agent with auth")
    print("   • test_agent_a2a = Pre-configured A2A test agent with auth")
    print("   • test_agent_no_auth = Pre-configured MCP test agent WITHOUT auth")
    print("   • test_agent_a2a_no_auth = Pre-configured A2A test agent WITHOUT auth")
    print("   • test_agent_client = Multi-agent client with both protocols")
    print("   • create_test_agent() = Create custom test configurations")
    print("   • Perfect for examples, docs, and quick testing")
    print("\n⚠️  Remember: Test agents are rate-limited and for testing only!")
    print("   DO NOT use in production applications.\n")


if __name__ == "__main__":
    asyncio.run(main())
