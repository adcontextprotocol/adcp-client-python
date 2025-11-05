"""Integration tests for the creative agent at creative.adcontextprotocol.org."""

import asyncio
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol


async def test_creative_agent():
    """Test the reference creative agent."""
    print("\n" + "=" * 60)
    print("Testing Creative Agent (MCP)")
    print("=" * 60)

    config = AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.adcontextprotocol.org",
        protocol=Protocol.MCP,
    )

    client = ADCPClient(config)

    # Test 1: List creative formats
    print("\n📋 Test 1: Listing creative formats...")
    try:
        result = await client.list_creative_formats()
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
    await test_creative_agent()
    print("\n" + "=" * 60)
    print("Integration tests completed")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
