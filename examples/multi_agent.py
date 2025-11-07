"""
Multi-agent usage example for AdCP Python client.

This example shows how to:
1. Configure multiple agents
2. Execute operations across all agents in parallel
3. Handle mixed sync/async responses
"""

import asyncio
from adcp import ADCPMultiAgentClient
from adcp.types import AgentConfig, Protocol


async def main():
    """Multi-agent usage example."""

    # Configure multiple agents
    agents = [
        AgentConfig(
            id="agent_x",
            agent_uri="https://agent-x.com",
            protocol=Protocol.A2A,
        ),
        AgentConfig(
            id="agent_y",
            agent_uri="https://agent-y.com/mcp/",
            protocol=Protocol.MCP,
        ),
        AgentConfig(
            id="agent_z",
            agent_uri="https://agent-z.com",
            protocol=Protocol.A2A,
        ),
    ]

    # Use context manager for automatic resource cleanup
    async with ADCPMultiAgentClient(
        agents=agents,
        webhook_url_template="https://myapp.com/webhook/{task_type}/{agent_id}/{operation_id}",
        on_activity=lambda activity: print(
            f"[{activity.agent_id}] [{activity.type}] {activity.task_type}"
        ),
        handlers={
            "on_get_products_status_change": handle_products_result,
        },
    ) as client:
        # Execute across all agents in parallel
        print(f"Querying {len(agents)} agents in parallel...")
        results = await client.get_products(brief="Coffee brands")

        # Process results
        sync_count = sum(1 for r in results if r.status == "completed")
        async_count = sum(1 for r in results if r.status == "submitted")

        print(f"\n📊 Results:")
        print(f"  ✅ Sync completions: {sync_count}")
        print(f"  ⏳ Async (webhooks pending): {async_count}")

        for i, result in enumerate(results):
            agent_id = client.agent_ids[i]

            if result.status == "completed":
                products = result.data.get("products", [])
                print(f"\n{agent_id}: {len(products)} products (sync)")

            elif result.status == "submitted":
                print(f"\n{agent_id}: webhook to {result.submitted.webhook_url}")

    # All agent connections automatically closed here


def handle_products_result(response, metadata):
    """Handler called when products result arrives (sync or async)."""
    print(f"\n🔔 Handler called for {metadata.agent_id}")
    print(f"   Operation: {metadata.operation_id}")
    print(f"   Status: {metadata.status}")

    if metadata.status == "completed":
        products = response.get("products", [])
        print(f"   Products: {len(products)}")


if __name__ == "__main__":
    asyncio.run(main())
