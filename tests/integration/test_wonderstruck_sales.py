"""Integration tests for Wonderstruck sales agent."""

import asyncio
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol


async def test_wonderstruck_sales():
    """Test the Wonderstruck sales agent."""
    print("\n" + "=" * 60)
    print("Testing Wonderstruck Sales Agent (MCP)")
    print("=" * 60)

    config = AgentConfig(
        id="principal_8ac9e391",
        agent_uri="https://wonderstruck.sales-agent.scope3.com/mcp/",
        protocol=Protocol.MCP,
        auth_token="UhwoigyVKdd6GT8hS04cc51ckGfi8qXpZL6OvS2i2cU",
    )

    client = ADCPClient(config)

    # Test 1: List authorized properties
    print("\n🏢 Test 1: Listing authorized properties...")
    try:
        result = await client.list_authorized_properties()
        if result.success:
            print("✅ Success!")
            print(f"Status: {result.status}")
            print(f"Data: {result.data}")
        else:
            print(f"❌ Failed: {result.error}")
    except Exception as e:
        print(f"❌ Exception: {e}")

    # Test 2: Get products
    print("\n🛍️  Test 2: Getting products...")
    try:
        result = await client.get_products(
            brief="Looking for display advertising opportunities for a tech product launch"
        )
        if result.success:
            print("✅ Success!")
            print(f"Status: {result.status}")
            print(f"Data: {result.data}")
        else:
            print(f"❌ Failed: {result.error}")
    except Exception as e:
        print(f"❌ Exception: {e}")

    # Clean up
    if hasattr(client.adapter, "close"):
        await client.adapter.close()


async def main():
    """Run all tests."""
    await test_wonderstruck_sales()
    print("\n" + "=" * 60)
    print("Integration tests completed")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
