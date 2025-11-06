#!/usr/bin/env python3
"""Test individual agents one at a time to avoid async cleanup issues."""

import asyncio
import json
import os
import sys
from dotenv import load_dotenv

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from adcp.client import ADCPClient
from adcp.types.core import AgentConfig


# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"


def print_header(text: str) -> None:
    """Print a formatted header."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 80}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{text}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 80}{Colors.RESET}\n")


def print_success(text: str) -> None:
    """Print success message."""
    print(f"{Colors.GREEN}✓ {text}{Colors.RESET}")


def print_error(text: str) -> None:
    """Print error message."""
    print(f"{Colors.RED}✗ {text}{Colors.RESET}")


def print_info(text: str) -> None:
    """Print info message."""
    print(f"{Colors.BLUE}ℹ {text}{Colors.RESET}")


async def test_agent(name: str, config: AgentConfig) -> dict:
    """Test a single agent."""
    print_header(f"Testing: {name} ({config.protocol.value.upper()})")

    print_info(f"URI: {config.agent_uri.split('//')[1].split('/')[0]}")
    print_info(f"Auth: {'Yes' if config.auth_token else 'No'}")

    result = {"name": name, "connected": False, "tools": [], "error": None}

    try:
        client = ADCPClient(config)

        print_info("Listing available tools...")
        tools = await client.list_tools()

        result["connected"] = True
        result["tools"] = tools

        print_success(f"Connected! Found {len(tools)} tools:")
        for tool_name in tools:
            print(f"  • {Colors.BOLD}{tool_name}{Colors.RESET}")

        # Close the adapter
        if hasattr(client.adapter, "close"):
            try:
                await client.adapter.close()
            except Exception:
                pass

    except Exception as e:
        result["error"] = str(e)
        print_error(f"Failed to connect: {e}")

    return result


async def main():
    """Main test function."""
    load_dotenv()

    agents_json = os.getenv("ADCP_AGENTS")
    if not agents_json:
        print_error("ADCP_AGENTS environment variable not set")
        return

    agents_data = json.loads(agents_json)

    if len(sys.argv) > 1:
        # Test specific agent by name or index
        arg = sys.argv[1]
        if arg.isdigit():
            idx = int(arg)
            if 0 <= idx < len(agents_data):
                agent = agents_data[idx]
                config = AgentConfig(**agent)
                name = agent.get("name", agent["id"])
                await test_agent(name, config)
            else:
                print_error(f"Invalid index: {idx}. Must be 0-{len(agents_data)-1}")
        else:
            # Find by name
            for agent in agents_data:
                if arg.lower() in agent.get("name", "").lower() or arg.lower() in agent["id"].lower():
                    config = AgentConfig(**agent)
                    name = agent.get("name", agent["id"])
                    await test_agent(name, config)
                    return
            print_error(f"Agent not found: {arg}")
    else:
        # List all agents
        print_header("Available Agents")
        for i, agent in enumerate(agents_data):
            name = agent.get("name", agent["id"])
            protocol = agent["protocol"].upper()
            print(f"  {i}. {Colors.BOLD}{name}{Colors.RESET} ({protocol})")
        print(f"\n{Colors.YELLOW}Usage: python test_agents_individual.py <index|name>{Colors.RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
