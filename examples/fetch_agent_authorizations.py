"""
Example showing how to discover which publishers have authorized your agent.

This example demonstrates TWO approaches:

1. "Push" approach - Ask the agent what it's authorized for:
   - Use the agent's list_authorized_properties endpoint
   - Agent tells you which publisher domains it represents
   - Fast and efficient - single API call

2. "Pull" approach - Check publisher adagents.json files:
   - Use fetch_agent_authorizations to check multiple publishers
   - Fetch adagents.json from each publisher's .well-known directory
   - Useful when you have a specific list of publishers to check
   - Supports connection pooling for better performance
"""

import asyncio

from adcp import ADCPClient, AgentConfig, Protocol, fetch_agent_authorizations


async def approach_1_push():
    """APPROACH 1: Ask the agent what it's authorized for (RECOMMENDED)."""
    print("=" * 70)
    print("APPROACH 1: Push - Ask agent what it's authorized for")
    print("=" * 70)
    print()

    # Configure the agent client
    agent_config = AgentConfig(
        id="sales_agent",
        agent_uri="https://our-sales-agent.com",
        protocol=Protocol.A2A,
    )

    async with ADCPClient(agent_config) as client:
        # Ask the agent directly what publishers it represents
        # This is fast - just one API call!
        response = await client.simple.list_authorized_properties()

        print(f"✅ Agent represents {len(response.publisher_domains)} publishers:\n")

        for domain in response.publisher_domains:
            print(f"  • {domain}")

        print()
        print("📊 Portfolio Summary:")
        if response.primary_channels:
            print(f"  Primary Channels: {', '.join(response.primary_channels)}")
        if response.primary_countries:
            print(f"  Primary Countries: {', '.join(response.primary_countries)}")
        if response.portfolio_description:
            print(f"  Description: {response.portfolio_description[:100]}...")

        print()
        print("💡 TIP: Now fetch each publisher's adagents.json to see property details")
        print()


async def approach_2_pull():
    """APPROACH 2: Check publisher adagents.json files (when you know which publishers to check)."""
    print("=" * 70)
    print("APPROACH 2: Pull - Check specific publisher adagents.json files")
    print("=" * 70)
    print()

    # Your agent's URL
    agent_url = "https://our-sales-agent.com"

    # Publisher domains to check
    publisher_domains = [
        "nytimes.com",
        "wsj.com",
        "cnn.com",
        "espn.com",
        "techcrunch.com",
    ]

    print(f"Checking authorization for {agent_url} across {len(publisher_domains)} publishers...\n")

    # Fetch authorization contexts (fetches all in parallel)
    contexts = await fetch_agent_authorizations(agent_url, publisher_domains)

    # Display results
    if not contexts:
        print("No authorizations found.")
        return

    print(f"✅ Authorized for {len(contexts)}/{len(publisher_domains)} publishers:\n")

    for domain, ctx in contexts.items():
        print(f"{domain}:")
        print(f"  Property IDs: {ctx.property_ids}")
        print(f"  Property Tags: {ctx.property_tags}")
        print(f"  Total Properties: {len(ctx.raw_properties)}")
        print()

    # Example: Check if specific tags are available
    all_tags = set()
    for ctx in contexts.values():
        all_tags.update(ctx.property_tags)

    print(f"📊 Total unique tags across all publishers: {len(all_tags)}")
    print(f"Tags: {sorted(all_tags)}")
    print()


async def main():
    """Demonstrate both approaches."""
    # APPROACH 1: Fast - ask agent what it's authorized for
    await approach_1_push()

    print("\n" + "=" * 70 + "\n")

    # APPROACH 2: Check specific publishers
    await approach_2_pull()


async def main_with_connection_pooling():
    """More efficient version using connection pooling for multiple requests."""
    import httpx

    agent_url = "https://our-sales-agent.com"
    publisher_domains = ["nytimes.com", "wsj.com", "cnn.com"]

    # Use a shared HTTP client for connection pooling
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
    ) as client:
        print("Using connection pooling for better performance...\n")

        contexts = await fetch_agent_authorizations(agent_url, publisher_domains, client=client)

        for domain, ctx in contexts.items():
            print(f"{domain}: {len(ctx.property_ids)} properties")


if __name__ == "__main__":
    # Run basic example
    asyncio.run(main())

    # Uncomment to run connection pooling example
    # asyncio.run(main_with_connection_pooling())
