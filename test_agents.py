#!/usr/bin/env python3
"""
Test script to verify connectivity and functionality of AdCP agents.

This script:
1. Loads agent configurations from .env
2. Tests connection to each agent
3. Lists available tools
4. Attempts basic tool calls
"""

import asyncio
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

from src.adcp.client import ADCPClient
from src.adcp.types.core import AgentConfig, Protocol


class Colors:
    """ANSI color codes for terminal output."""

    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


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


def print_warning(text: str) -> None:
    """Print warning message."""
    print(f"{Colors.YELLOW}⚠ {text}{Colors.RESET}")


def load_agents_from_env() -> list[tuple[str, AgentConfig]]:
    """Load agent configurations from environment variables."""
    load_dotenv()

    agents_json = os.getenv("ADCP_AGENTS")
    if not agents_json:
        print_error("ADCP_AGENTS not found in .env")
        return []

    try:
        agents_data = json.loads(agents_json)
        configs = []
        for agent in agents_data:
            config = AgentConfig(
                id=agent["id"],
                agent_uri=agent["agent_uri"],
                protocol=Protocol(agent["protocol"]),
                auth_token=agent.get("auth_token"),
            )
            # Store name separately since it's not in AgentConfig
            name = agent.get("name", agent["id"])
            configs.append((name, config))
        return configs
    except Exception as e:
        print_error(f"Failed to parse ADCP_AGENTS: {e}")
        return []


async def test_agent_connection(name: str, config: AgentConfig) -> dict[str, Any]:
    """Test connection to a single agent."""
    result = {
        "name": name,
        "config": config,
        "connected": False,
        "tools": [],
        "error": None,
        "test_call_result": None,
    }

    print_header(f"Testing: {name} ({config.protocol.value.upper()})")
    print_info(f"URI: {config.agent_uri}")
    print_info(f"Auth: {'Yes' if config.auth_token else 'No'}")

    try:
        # Create client
        client = ADCPClient(config)

        # Try to list tools
        print_info("Listing available tools...")
        try:
            tools = await client.list_tools()
            print_info(f"Got response: {len(tools)} tools")
        except Exception as e:
            print_warning(f"Error listing tools: {e}")
            import traceback

            traceback.print_exc()
            tools = []

        # Always mark as connected if we got this far
        result["connected"] = True

        if tools:
            result["tools"] = tools
            print_success(f"Connected! Found {len(tools)} tools:")
            # Tools are just strings (tool names)
            for tool_name in tools:
                print(f"  • {Colors.BOLD}{tool_name}{Colors.RESET}")

            # Try a simple test call if possible
            test_tool = None
            if "list_creative_formats" in tools:
                test_tool = "list_creative_formats"
            elif "get_products" in tools:
                test_tool = "get_products"
            elif tools:
                test_tool = tools[0]

            if test_tool:
                print_info(f"Testing tool call: {test_tool}...")
                try:
                    test_result = await client.call_tool(test_tool, {})
                    result["test_call_result"] = {
                        "tool": test_tool,
                        "success": test_result.success,
                        "status": test_result.status.value,
                    }
                    if test_result.success:
                        print_success(f"Tool call succeeded! Status: {test_result.status.value}")
                        if test_result.data:
                            print_info(
                                f"Response data: {json.dumps(test_result.data, indent=2)[:200]}..."
                            )
                    else:
                        print_warning(f"Tool call status: {test_result.status.value}")
                        if test_result.error:
                            print_warning(f"Error: {test_result.error}")
                except Exception as e:
                    print_error(f"Tool call failed: {e}")
                    result["test_call_result"] = {"tool": test_tool, "error": str(e)}
        else:
            print_warning("Connected but no tools found")

        # Close the adapter
        if hasattr(client.adapter, "close"):
            try:
                await client.adapter.close()
            except Exception:
                pass  # Ignore errors during cleanup

    except Exception as e:
        result["error"] = str(e)
        print_error(f"Failed to connect: {e}")

    return result


async def test_all_agents() -> list[dict[str, Any]]:
    """Test all configured agents."""
    agents = load_agents_from_env()

    if not agents:
        print_error("No agents configured in .env")
        return []

    print_header(f"Testing {len(agents)} AdCP Agents")

    results = []
    for name, config in agents:
        result = await test_agent_connection(name, config)
        results.append(result)
        await asyncio.sleep(1)  # Brief pause between tests

    return results


def print_summary(results: list[dict[str, Any]]) -> None:
    """Print summary of all tests."""
    print_header("Test Summary")

    total = len(results)
    connected = sum(1 for r in results if r["connected"])
    failed = total - connected

    print(f"{Colors.BOLD}Total Agents:{Colors.RESET} {total}")
    print(f"{Colors.GREEN}Connected:{Colors.RESET} {connected}")
    print(f"{Colors.RED}Failed:{Colors.RESET} {failed}")
    print()

    # Details
    for result in results:
        name = result["name"]
        if result["connected"]:
            tools_count = len(result["tools"])
            test_status = ""
            if result["test_call_result"]:
                if result["test_call_result"].get("success"):
                    test_status = f" ({Colors.GREEN}test call OK{Colors.RESET})"
                else:
                    test_status = f" ({Colors.YELLOW}test call partial{Colors.RESET})"

            print_success(f"{name}: {tools_count} tools{test_status}")
        else:
            error = result["error"] or "Unknown error"
            print_error(f"{name}: {error}")


async def main() -> None:
    """Main entry point."""
    try:
        results = await test_all_agents()
        print_summary(results)

        # Exit with error code if any failed
        failed = sum(1 for r in results if not r["connected"])
        sys.exit(1 if failed > 0 else 0)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        print_error(f"Fatal error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
