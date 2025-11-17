"""
Basic usage example for AdCP Python client.

This example shows how to:
1. Configure an AdCP client
2. Call get_products using the simple API
3. Access results directly
"""

import asyncio
from adcp import ADCPClient, ADCPSimpleError
from adcp.types import AgentConfig, Protocol


async def main():
    """Basic usage example using .simple API."""

    # Configure agent
    config = AgentConfig(
        id="test_agent",
        agent_uri="https://test-agent.adcontextprotocol.org",
        protocol=Protocol.A2A,
        auth_token="your-token-here",  # Optional
    )

    # Create client
    client = ADCPClient(
        config,
        webhook_url_template="https://myapp.com/webhook/{task_type}/{agent_id}/{operation_id}",
        on_activity=lambda activity: print(f"[{activity.type}] {activity.task_type}"),
    )

    try:
        # Call get_products using simple API with keyword arguments
        print("Fetching products...")
        result = await client.simple.get_products(
            brief="Coffee brands targeting millennials",
            brand_manifest={"name": "My Brand", "url": "https://mybrand.com"},
        )

        # Direct data access - no need for result.data
        print(f"✅ Got {len(result.products)} products")
        for product in result.products:
            print(f"  - {product.name}: {product.description[:100]}...")

    except ADCPSimpleError as e:
        # Simple API raises errors for async tasks or failures
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
