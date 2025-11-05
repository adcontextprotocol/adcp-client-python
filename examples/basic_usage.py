"""
Basic usage example for AdCP Python client.

This example shows how to:
1. Configure an AdCP client
2. Call get_products
3. Handle sync vs async responses
"""

import asyncio
from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol


async def main():
    """Basic usage example."""

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

    # Call get_products
    print("Fetching products...")
    result = await client.get_products(brief="Coffee brands targeting millennials")

    # Handle result
    if result.status == "completed":
        print(f"✅ Sync completion: Got {len(result.data.get('products', []))} products")
        for product in result.data.get("products", []):
            print(f"  - {product.get('name')}: {product.get('description')}")

    elif result.status == "submitted":
        print(f"⏳ Async: Webhook will be sent to {result.submitted.webhook_url}")
        print(f"   Operation ID: {result.submitted.operation_id}")

    elif result.status == "needs_input":
        print(f"❓ Agent needs clarification: {result.needs_input.message}")

    elif result.status == "failed":
        print(f"❌ Error: {result.error}")


if __name__ == "__main__":
    asyncio.run(main())
