"""Integration tests for Optable signals agent."""

import asyncio
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol


async def test_optable_signals():
    """Test the Optable signals agent."""
    print("\n" + "=" * 60)
    print("Testing Optable Signals Agent (MCP)")
    print("=" * 60)

    config = AgentConfig(
        id="optable_signals",
        agent_uri="https://sandbox.optable.co/admin/adcp/signals/mcp",
        protocol=Protocol.MCP,
        auth_token="5ZWQoDY8sReq7CTNQdgPokHdEse8JB2LDjOfo530_9A=",
    )

    client = ADCPClient(config)

    # Test 1: Get available signals
    print("\n📊 Test 1: Getting available signals...")
    try:
        result = await client.get_signals()
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
    await test_optable_signals()
    print("\n" + "=" * 60)
    print("Integration tests completed")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
