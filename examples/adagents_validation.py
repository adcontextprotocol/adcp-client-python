#!/usr/bin/env python3
"""
Example: Validating Publisher Authorization with adagents.json

This example demonstrates how to use the adagents validation utilities
to verify that a sales agent is authorized to sell ads for a publisher's
properties.
"""

import asyncio

from adcp import (
    AdagentsNotFoundError,
    AdagentsValidationError,
    fetch_adagents,
    verify_agent_authorization,
    verify_agent_for_property,
)


async def example_fetch_and_verify():
    """Example: Fetch adagents.json and verify authorization."""
    print("=" * 60)
    print("Example 1: Fetch and Verify Authorization")
    print("=" * 60)

    publisher_domain = "example-publisher.com"
    agent_url = "https://sales-agent.example.com"

    try:
        # Fetch the adagents.json file from the publisher
        print(f"\n1. Fetching adagents.json from {publisher_domain}...")
        adagents_data = await fetch_adagents(publisher_domain)
        print(f"   ✓ Found {len(adagents_data['authorized_agents'])} authorized agents")

        # Verify if our agent is authorized for a specific property
        print(f"\n2. Checking if {agent_url} is authorized...")
        is_authorized = verify_agent_authorization(
            adagents_data=adagents_data,
            agent_url=agent_url,
            property_type="website",
            property_identifiers=[{"type": "domain", "value": "example-publisher.com"}],
        )

        if is_authorized:
            print("   ✓ Agent is authorized for this property")
        else:
            print("   ✗ Agent is NOT authorized for this property")

    except AdagentsNotFoundError as e:
        print(f"   ✗ Error: {e}")
        print("   The publisher has not deployed an adagents.json file")
    except AdagentsValidationError as e:
        print(f"   ✗ Validation Error: {e}")


async def example_convenience_wrapper():
    """Example: Use the convenience wrapper for one-step verification."""
    print("\n\n" + "=" * 60)
    print("Example 2: Convenience Wrapper (Fetch + Verify)")
    print("=" * 60)

    try:
        # Single function call to fetch and verify
        print("\nChecking authorization in one step...")
        is_authorized = await verify_agent_for_property(
            publisher_domain="example-publisher.com",
            agent_url="https://sales-agent.example.com",
            property_identifiers=[{"type": "domain", "value": "example-publisher.com"}],
            property_type="website",
        )

        if is_authorized:
            print("✓ Agent is authorized!")
        else:
            print("✗ Agent is NOT authorized")

    except Exception as e:
        print(f"✗ Error: {e}")


def example_manual_verification():
    """Example: Manual verification with pre-fetched data."""
    print("\n\n" + "=" * 60)
    print("Example 3: Manual Verification with Pre-fetched Data")
    print("=" * 60)

    # Example adagents.json data structure
    adagents_data = {
        "authorized_agents": [
            {
                "url": "https://sales-agent.example.com",
                "properties": [
                    {
                        "property_type": "website",
                        "name": "Main Website",
                        "identifiers": [{"type": "domain", "value": "example.com"}],
                    },
                    {
                        "property_type": "mobile_app",
                        "name": "iOS App",
                        "identifiers": [{"type": "bundle_id", "value": "com.example.app"}],
                    },
                ],
            },
            {
                "url": "https://another-agent.com",
                "properties": [],  # Empty properties = authorized for all
            },
        ]
    }

    # Test various scenarios
    print("\nScenario 1: Agent authorized for website")
    result = verify_agent_authorization(
        adagents_data,
        "https://sales-agent.example.com",
        "website",
        [{"type": "domain", "value": "www.example.com"}],  # www subdomain
    )
    print(f"   Result: {result} (www subdomain matches example.com)")

    print("\nScenario 2: Agent authorized for mobile app")
    result = verify_agent_authorization(
        adagents_data,
        "https://sales-agent.example.com",
        "mobile_app",
        [{"type": "bundle_id", "value": "com.example.app"}],
    )
    print(f"   Result: {result}")

    print("\nScenario 3: Agent NOT authorized for different property")
    result = verify_agent_authorization(
        adagents_data,
        "https://sales-agent.example.com",
        "website",
        [{"type": "domain", "value": "different.com"}],
    )
    print(f"   Result: {result}")

    print("\nScenario 4: Agent with empty properties = authorized for all")
    result = verify_agent_authorization(
        adagents_data, "https://another-agent.com", "website", [{"type": "domain", "value": "any.com"}]
    )
    print(f"   Result: {result}")

    print("\nScenario 5: Protocol-agnostic matching (http vs https)")
    result = verify_agent_authorization(
        adagents_data,
        "http://sales-agent.example.com",  # http instead of https
        "website",
        [{"type": "domain", "value": "example.com"}],
    )
    print(f"   Result: {result} (protocol ignored)")


def example_property_discovery():
    """Example: Discover all properties and tags from adagents.json."""
    print("\n\n" + "=" * 60)
    print("Example 4: Property and Tag Discovery")
    print("=" * 60)

    from adcp import get_all_properties, get_all_tags, get_properties_by_agent

    # Example adagents.json with tags
    adagents_data = {
        "authorized_agents": [
            {
                "url": "https://sales-agent-1.example.com",
                "properties": [
                    {
                        "property_type": "website",
                        "name": "News Site",
                        "identifiers": [{"type": "domain", "value": "news.example.com"}],
                        "tags": ["premium", "news", "desktop"],
                    },
                    {
                        "property_type": "mobile_app",
                        "name": "News App",
                        "identifiers": [{"type": "bundle_id", "value": "com.example.news"}],
                        "tags": ["premium", "news", "mobile"],
                    },
                ],
            },
            {
                "url": "https://sales-agent-2.example.com",
                "properties": [
                    {
                        "property_type": "website",
                        "name": "Sports Site",
                        "identifiers": [{"type": "domain", "value": "sports.example.com"}],
                        "tags": ["sports", "live-streaming"],
                    }
                ],
            },
        ]
    }

    print("\n1. Get all properties across all agents:")
    all_props = get_all_properties(adagents_data)
    print(f"   Found {len(all_props)} total properties")
    for prop in all_props:
        print(f"   - {prop['name']} ({prop['property_type']}) - Agent: {prop['agent_url']}")

    print("\n2. Get all unique tags:")
    all_tags = get_all_tags(adagents_data)
    print(f"   Tags: {', '.join(sorted(all_tags))}")

    print("\n3. Get properties for a specific agent:")
    agent_props = get_properties_by_agent(adagents_data, "https://sales-agent-1.example.com")
    print(f"   Agent 1 has {len(agent_props)} properties:")
    for prop in agent_props:
        print(f"   - {prop['name']} (tags: {', '.join(prop.get('tags', []))})")


def example_domain_matching():
    """Example: Domain matching rules."""
    print("\n\n" + "=" * 60)
    print("Example 5: Domain Matching Rules")
    print("=" * 60)

    from adcp import domain_matches

    print("\n1. Exact match:")
    print(f"   example.com == example.com: {domain_matches('example.com', 'example.com')}")

    print("\n2. Common subdomains (www, m) match bare domain:")
    print(f"   www.example.com matches example.com: {domain_matches('www.example.com', 'example.com')}")
    print(f"   m.example.com matches example.com: {domain_matches('m.example.com', 'example.com')}")

    print("\n3. Other subdomains DON'T match bare domain:")
    print(
        f"   api.example.com matches example.com: {domain_matches('api.example.com', 'example.com')}"
    )

    print("\n4. Wildcard pattern matches all subdomains:")
    print(
        f"   api.example.com matches *.example.com: {domain_matches('api.example.com', '*.example.com')}"
    )
    print(
        f"   www.example.com matches *.example.com: {domain_matches('www.example.com', '*.example.com')}"
    )

    print("\n5. Case-insensitive matching:")
    print(f"   Example.COM matches example.com: {domain_matches('Example.COM', 'example.com')}")


async def main():
    """Run all examples."""
    print("\n🔍 AdCP adagents.json Validation Examples\n")

    # Note: Examples 1 and 2 would require actual HTTP requests
    # Uncomment to test with real domains:
    # await example_fetch_and_verify()
    # await example_convenience_wrapper()

    # These examples work with mock data:
    example_manual_verification()
    example_property_discovery()
    example_domain_matching()

    print("\n\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print("""
Key Functions:
1. fetch_adagents(domain) - Fetch and validate adagents.json
2. verify_agent_authorization(data, agent_url, ...) - Check authorization
3. verify_agent_for_property(domain, agent_url, ...) - Convenience wrapper
4. get_all_properties(data) - Extract all properties from all agents
5. get_all_tags(data) - Get all unique tags across properties
6. get_properties_by_agent(data, agent_url) - Get properties for specific agent
7. domain_matches(prop_domain, pattern) - Domain matching rules
8. identifiers_match(prop_ids, agent_ids) - Identifier matching

Use Cases:
- Sales agents: Verify authorization before accepting media buys
- Publishers: Test their adagents.json files are correctly formatted
- Developer tools: Build validators and testing utilities

See the full API documentation for more details.
    """)


if __name__ == "__main__":
    asyncio.run(main())
