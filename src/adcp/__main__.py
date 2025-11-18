#!/usr/bin/env python3
from __future__ import annotations

"""Command-line interface for AdCP client - compatible with npx @adcp/client."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

from adcp.client import ADCPClient
from adcp.config import (
    CONFIG_FILE,
    get_agent,
    list_agents,
    remove_agent,
    save_agent,
)
from adcp.types.core import AgentConfig, Protocol


def print_json(data: Any) -> None:
    """Print data as JSON."""
    from pydantic import BaseModel

    # Handle Pydantic models
    if isinstance(data, BaseModel):
        print(data.model_dump_json(indent=2, exclude_none=True))
    else:
        print(json.dumps(data, indent=2, default=str))


def print_result(result: Any, json_output: bool = False) -> None:
    """Print result in formatted or JSON mode."""
    if json_output:
        # Match JavaScript client: output just the data for scripting
        if result.success and result.data:
            print_json(result.data)
        else:
            # On error, output error info
            print_json({"error": result.error, "success": False})
    else:
        # Pretty output with message and data (like JavaScript client)
        if result.success:
            print("\nSUCCESS\n")
            # Show protocol message if available
            if hasattr(result, "message") and result.message:
                print("Protocol Message:")
                print(result.message)
                print()
            if result.data:
                print("Response:")
                print_json(result.data)
        else:
            print("\nFAILED\n")
            print(f"Error: {result.error}")


async def execute_tool(
    agent_config: dict[str, Any], tool_name: str, payload: dict[str, Any], json_output: bool = False
) -> None:
    """Execute a tool on an agent."""
    # Ensure required fields
    if "id" not in agent_config:
        agent_config["id"] = agent_config.get("agent_uri", "unknown")

    if "protocol" not in agent_config:
        agent_config["protocol"] = "mcp"

    # Convert string protocol to enum
    if isinstance(agent_config["protocol"], str):
        agent_config["protocol"] = Protocol(agent_config["protocol"].lower())

    config = AgentConfig(**agent_config)

    async with ADCPClient(config) as client:
        # Dispatch to specific method based on tool name
        result = await _dispatch_tool(client, tool_name, payload)
        print_result(result, json_output)


# Tool dispatch mapping - single source of truth for ADCP methods
# Types are filled at runtime to avoid circular imports
TOOL_DISPATCH: dict[str, tuple[str, type | None]] = {
    "get_products": ("get_products", None),
    "list_creative_formats": ("list_creative_formats", None),
    "sync_creatives": ("sync_creatives", None),
    "list_creatives": ("list_creatives", None),
    "get_media_buy_delivery": ("get_media_buy_delivery", None),
    "list_authorized_properties": ("list_authorized_properties", None),
    "get_signals": ("get_signals", None),
    "activate_signal": ("activate_signal", None),
    "provide_performance_feedback": ("provide_performance_feedback", None),
}


async def _dispatch_tool(client: ADCPClient, tool_name: str, payload: dict[str, Any]) -> Any:
    """Dispatch tool call to appropriate client method.

    Args:
        client: ADCP client instance
        tool_name: Name of the tool to invoke
        payload: Request payload as dict

    Returns:
        TaskResult with typed response or error

    Raises:
        ValidationError: If payload doesn't match request schema (caught and returned as TaskResult)
    """
    from pydantic import ValidationError

    from adcp.types import _generated as gen
    from adcp.types.core import TaskResult, TaskStatus

    # Lazy initialization of request types (avoid circular imports)
    if TOOL_DISPATCH["get_products"][1] is None:
        TOOL_DISPATCH["get_products"] = ("get_products", gen.GetProductsRequest)
        TOOL_DISPATCH["list_creative_formats"] = (
            "list_creative_formats",
            gen.ListCreativeFormatsRequest,
        )
        TOOL_DISPATCH["sync_creatives"] = ("sync_creatives", gen.SyncCreativesRequest)
        TOOL_DISPATCH["list_creatives"] = ("list_creatives", gen.ListCreativesRequest)
        TOOL_DISPATCH["get_media_buy_delivery"] = (
            "get_media_buy_delivery",
            gen.GetMediaBuyDeliveryRequest,
        )
        TOOL_DISPATCH["list_authorized_properties"] = (
            "list_authorized_properties",
            gen.ListAuthorizedPropertiesRequest,
        )
        TOOL_DISPATCH["get_signals"] = ("get_signals", gen.GetSignalsRequest)
        TOOL_DISPATCH["activate_signal"] = ("activate_signal", gen.ActivateSignalRequest)
        TOOL_DISPATCH["provide_performance_feedback"] = (
            "provide_performance_feedback",
            gen.ProvidePerformanceFeedbackRequest,
        )

    # Check if tool exists
    if tool_name not in TOOL_DISPATCH:
        available = ", ".join(sorted(TOOL_DISPATCH.keys()))
        return TaskResult(
            status=TaskStatus.FAILED,
            error=f"Unknown tool: {tool_name}. Available tools: {available}",
        )

    # Get method and request type
    method_name, request_type = TOOL_DISPATCH[tool_name]

    # Type guard - request_type should be initialized by this point
    if request_type is None:
        return TaskResult(
            status=TaskStatus.FAILED,
            error=f"Internal error: {tool_name} request type not initialized",
        )

    method = getattr(client, method_name)

    # Validate and invoke
    try:
        request = request_type(**payload)
        return await method(request)
    except ValidationError as e:
        # User-friendly error for invalid payloads
        error_details = []
        for error in e.errors():
            field = ".".join(str(loc) for loc in error["loc"])
            msg = error["msg"]
            error_details.append(f"  - {field}: {msg}")

        return TaskResult(
            status=TaskStatus.FAILED,
            error=f"Invalid request payload for {tool_name}:\n" + "\n".join(error_details),
        )


def load_payload(payload_arg: str | None) -> dict[str, Any]:
    """Load payload from argument (JSON, @file, or stdin)."""
    if not payload_arg:
        # Try to read from stdin if available and has data
        if not sys.stdin.isatty():
            try:
                return cast(dict[str, Any], json.load(sys.stdin))
            except (json.JSONDecodeError, ValueError):
                pass
        return {}

    if payload_arg.startswith("@"):
        # Load from file
        file_path = Path(payload_arg[1:])
        if not file_path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        return cast(dict[str, Any], json.loads(file_path.read_text()))

    # Parse as JSON
    try:
        return cast(dict[str, Any], json.loads(payload_arg))
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON payload: {e}", file=sys.stderr)
        sys.exit(1)


def handle_save_auth(alias: str, url: str | None, protocol: str | None) -> None:
    """Handle --save-auth command."""
    if not url:
        # Interactive mode
        url = input(f"Agent URL for '{alias}': ").strip()
        if not url:
            print("Error: URL is required", file=sys.stderr)
            sys.exit(1)

    if not protocol:
        protocol = input("Protocol (mcp/a2a) [mcp]: ").strip() or "mcp"

    auth_token = input("Auth token (optional): ").strip() or None

    save_agent(alias, url, protocol, auth_token)
    print(f"✓ Saved agent '{alias}'")


def handle_list_agents() -> None:
    """Handle --list-agents command."""
    agents = list_agents()

    if not agents:
        print("No saved agents")
        return

    print("\nSaved agents:")
    for alias, config in agents.items():
        auth = "yes" if config.get("auth_token") else "no"
        print(f"  {alias}")
        print(f"    URL: {config.get('agent_uri')}")
        print(f"    Protocol: {config.get('protocol', 'mcp').upper()}")
        print(f"    Auth: {auth}")


def handle_remove_agent(alias: str) -> None:
    """Handle --remove-agent command."""
    if remove_agent(alias):
        print(f"✓ Removed agent '{alias}'")
    else:
        print(f"Error: Agent '{alias}' not found", file=sys.stderr)
        sys.exit(1)


def handle_show_config() -> None:
    """Handle --show-config command."""
    print(f"Config file: {CONFIG_FILE}")


def resolve_agent_config(agent_identifier: str) -> dict[str, Any]:
    """Resolve agent identifier to configuration."""
    # Check if it's a saved alias
    saved = get_agent(agent_identifier)
    if saved:
        return saved

    # Check if it's a URL
    if agent_identifier.startswith(("http://", "https://")):
        return {
            "id": agent_identifier.split("/")[-1],
            "agent_uri": agent_identifier,
            "protocol": "mcp",
        }

    # Check if it's a JSON config
    if agent_identifier.startswith("{"):
        try:
            return cast(dict[str, Any], json.loads(agent_identifier))
        except json.JSONDecodeError:
            pass

    print(f"Error: Unknown agent '{agent_identifier}'", file=sys.stderr)
    print("  Not found as saved alias", file=sys.stderr)
    print("  Not a valid URL", file=sys.stderr)
    print("  Not valid JSON config", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    """Main CLI entry point - compatible with JavaScript version."""
    parser = argparse.ArgumentParser(
        description="AdCP Client - Interact with AdCP agents",
        usage="adcp [options] <agent> [tool] [payload]",
        add_help=False,
    )

    # Configuration management
    parser.add_argument("--save-auth", metavar="ALIAS", help="Save agent configuration")
    parser.add_argument("--list-agents", action="store_true", help="List saved agents")
    parser.add_argument("--remove-agent", metavar="ALIAS", help="Remove saved agent")
    parser.add_argument("--show-config", action="store_true", help="Show config file location")

    # Execution options
    parser.add_argument("--protocol", choices=["mcp", "a2a"], help="Force protocol type")
    parser.add_argument("--auth", help="Authentication token")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")

    # Positional arguments
    parser.add_argument("agent", nargs="?", help="Agent alias, URL, or config")
    parser.add_argument("tool", nargs="?", help="Tool name to execute")
    parser.add_argument("payload", nargs="?", help="Payload (JSON, @file, or stdin)")

    # Parse known args to handle --save-auth with positional args
    args, remaining = parser.parse_known_args()

    # Handle help
    if args.help or (
        not args.agent
        and not any(
            [
                args.save_auth,
                args.list_agents,
                args.remove_agent,
                args.show_config,
            ]
        )
    ):
        parser.print_help()
        print("\nExamples:")
        print("  adcp --save-auth myagent https://agent.example.com mcp")
        print("  adcp --list-agents")
        print("  adcp myagent list_tools")
        print('  adcp myagent get_products \'{"brief":"TV ads"}\'')
        print("  adcp https://agent.example.com list_tools")
        sys.exit(0)

    # Handle configuration commands
    if args.save_auth:
        url = args.agent if args.agent else None
        protocol = args.tool if args.tool else None
        handle_save_auth(args.save_auth, url, protocol)
        sys.exit(0)

    if args.list_agents:
        handle_list_agents()
        sys.exit(0)

    if args.remove_agent:
        handle_remove_agent(args.remove_agent)
        sys.exit(0)

    if args.show_config:
        handle_show_config()
        sys.exit(0)

    # Execute tool
    if not args.agent:
        print("Error: Agent identifier required", file=sys.stderr)
        sys.exit(1)

    if not args.tool:
        print("Error: Tool name required", file=sys.stderr)
        sys.exit(1)

    # Resolve agent config
    agent_config = resolve_agent_config(args.agent)

    # Override with command-line options
    if args.protocol:
        agent_config["protocol"] = args.protocol

    if args.auth:
        agent_config["auth_token"] = args.auth

    if args.debug:
        agent_config["debug"] = True

    # Load payload
    payload = load_payload(args.payload)

    # Execute
    asyncio.run(execute_tool(agent_config, args.tool, payload, args.json))


if __name__ == "__main__":
    main()
