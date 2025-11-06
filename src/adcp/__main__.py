from __future__ import annotations

"""Command-line interface for AdCP client."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from adcp.client import ADCPClient
from adcp.types.core import AgentConfig


def print_result(result):
    """Print result in a formatted way."""
    print("\n" + "=" * 80)
    print(f"Status: {result.status.value}")
    print(f"Success: {result.success}")

    if result.data:
        print("\nData:")
        print(json.dumps(result.data, indent=2, default=str))

    if result.error:
        print(f"\nError: {result.error}")

    if result.metadata:
        print("\nMetadata:")
        print(json.dumps(result.metadata, indent=2))

    if result.debug_info:
        print("\nDebug Information:")
        print(json.dumps(
            {
                "request": result.debug_info.request,
                "response": result.debug_info.response,
                "duration_ms": result.debug_info.duration_ms,
            },
            indent=2,
            default=str,
        ))
    print("=" * 80 + "\n")


async def list_tools_command(args):
    """List tools for an agent."""
    config = AgentConfig(**json.loads(args.config))

    async with ADCPClient(config) as client:
        tools = await client.list_tools()
        print(f"\nFound {len(tools)} tools:")
        for tool in tools:
            print(f"  • {tool}")
        print()


async def call_tool_command(args):
    """Call a tool on an agent."""
    config = AgentConfig(**json.loads(args.config))
    params = json.loads(args.params) if args.params else {}

    async with ADCPClient(config) as client:
        result = await client.call_tool(args.tool, params)
        print_result(result)


async def test_connection_command(args):
    """Test connection to an agent."""
    config = AgentConfig(**json.loads(args.config))

    try:
        async with ADCPClient(config) as client:
            tools = await client.list_tools()
            print(f"\n✓ Successfully connected to agent: {config.id}")
            print(f"  Protocol: {config.protocol.value.upper()}")
            print(f"  URI: {config.agent_uri}")
            print(f"  Tools available: {len(tools)}")
            print()
            return 0
    except Exception as e:
        print(f"\n✗ Failed to connect to agent: {config.id}")
        print(f"  Error: {e}")
        print()
        return 1


def load_config_from_env(agent_id: str | None = None) -> str:
    """Load agent config from ADCP_AGENTS environment variable."""
    agents_json = os.getenv("ADCP_AGENTS")
    if not agents_json:
        print("Error: ADCP_AGENTS environment variable not set", file=sys.stderr)
        sys.exit(1)

    agents_data = json.loads(agents_json)

    if agent_id:
        # Find specific agent
        for agent in agents_data:
            if agent.get("id") == agent_id or agent.get("name") == agent_id:
                return json.dumps(agent)
        print(f"Error: Agent '{agent_id}' not found in ADCP_AGENTS", file=sys.stderr)
        sys.exit(1)
    else:
        # Return first agent
        if not agents_data:
            print("Error: No agents configured in ADCP_AGENTS", file=sys.stderr)
            sys.exit(1)
        return json.dumps(agents_data[0])


def load_config_from_file(config_path: str) -> str:
    """Load agent config from a file."""
    path = Path(config_path)
    if not path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    return path.read_text()


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AdCP Client - Interact with AdCP agents from the command line",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--config",
        help="Agent configuration as JSON string, file path (@file.json), or agent ID from ADCP_AGENTS env var",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # list-tools command
    list_parser = subparsers.add_parser("list-tools", help="List available tools from agent")

    # call-tool command
    call_parser = subparsers.add_parser("call-tool", help="Call a tool on the agent")
    call_parser.add_argument("tool", help="Name of the tool to call")
    call_parser.add_argument("--params", help="Tool parameters as JSON string")

    # test command
    test_parser = subparsers.add_parser("test", help="Test connection to agent")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Load configuration
    if not args.config:
        # Try to load from environment
        args.config = load_config_from_env()
    elif args.config.startswith("@"):
        # Load from file
        args.config = load_config_from_file(args.config[1:])
    elif not args.config.startswith("{"):
        # Treat as agent ID and load from environment
        args.config = load_config_from_env(args.config)

    # Execute command
    if args.command == "list-tools":
        asyncio.run(list_tools_command(args))
    elif args.command == "call-tool":
        asyncio.run(call_tool_command(args))
    elif args.command == "test":
        exit_code = asyncio.run(test_connection_command(args))
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
