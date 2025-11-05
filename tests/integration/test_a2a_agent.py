"""Integration tests for Test Agent (A2A protocol)."""

import asyncio
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol


async def test_a2a_agent():
    """Test the A2A test agent."""
    print("\n" + "=" * 60)
    print("Testing Test Agent (A2A)")
    print("=" * 60)

    config = AgentConfig(
        id="principal_3bd0d4a8",
        agent_uri="https://test-agent.adcontextprotocol.org",
        protocol=Protocol.A2A,
        auth_token="L4UCklW_V_40eTdWuQYF6HD5GWeKkgV8U6xxK-jwNO8",
    )

    client = ADCPClient(config)

    # Test 1: Get products
    print("\n🛍️  Test 1: Getting products...")
    try:
        result = await client.get_products(
            brief="Looking for video advertising opportunities for a new product"
        )
        if result.success:
            print("✅ Success!")
            print(f"Status: {result.status}")
            print(f"Data: {result.data}")
            if result.metadata:
                print(f"Metadata: {result.metadata}")
        else:
            print(f"❌ Failed: {result.error}")
    except Exception as e:
        print(f"❌ Exception: {e}")

    # Test 2: List authorized properties
    print("\n🏢 Test 2: Listing authorized properties...")
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


async def main():
    """Run all tests."""
    await test_a2a_agent()
    print("\n" + "=" * 60)
    print("Integration tests completed")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
