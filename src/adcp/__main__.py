#!/usr/bin/env python3
from __future__ import annotations

"""Command-line interface for AdCP client - compatible with npx @adcp/client."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

from pydantic import TypeAdapter

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


def _check_deprecated_fields(data: Any) -> None:
    """Check response data for deprecated fields and emit warnings to stderr.

    Uses Pydantic's Field(deprecated=True) metadata to generically detect
    any deprecated fields that are populated in the response.
    """
    from pydantic import BaseModel

    deprecated_found: set[str] = set()

    def _find_deprecated_fields(obj: Any, visited: set[int] | None = None) -> None:
        """Recursively find deprecated fields that are populated."""
        if obj is None:
            return

        # Prevent infinite recursion on circular references
        if visited is None:
            visited = set()
        obj_id = id(obj)
        if obj_id in visited:
            return
        visited.add(obj_id)

        # Check Pydantic models for deprecated fields
        if isinstance(obj, BaseModel):
            import warnings

            # Access model_fields from the class, not the instance (Pydantic v2.11+)
            model_fields = type(obj).model_fields

            for field_name, field_info in model_fields.items():
                if field_info.deprecated:
                    # Suppress Pydantic's DeprecationWarning when accessing deprecated fields
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)
                        value = getattr(obj, field_name, None)
                    if value is not None:
                        deprecated_found.add(field_name)

            # Recursively check field values
            for field_name in model_fields:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    value = getattr(obj, field_name, None)
                if value is not None:
                    _find_deprecated_fields(value, visited)

        # Check lists
        elif isinstance(obj, list):
            for item in obj:
                _find_deprecated_fields(item, visited)

        # Check dicts
        elif isinstance(obj, dict):
            for value in obj.values():
                _find_deprecated_fields(value, visited)

    _find_deprecated_fields(data)

    if deprecated_found:
        fields_list = ", ".join(f"'{f}'" for f in sorted(deprecated_found))
        print(
            f"\n⚠️  Warning: Response contains deprecated field(s): {fields_list}\n"
            "   See field descriptions or AdCP spec for migration details.\n",
            file=sys.stderr,
        )


def print_result(result: Any, json_output: bool = False) -> None:
    """Print result in formatted or JSON mode."""
    # Check for deprecated fields and warn (to stderr, so JSON output isn't affected)
    if result.success and result.data:
        _check_deprecated_fields(result.data)

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


# Tool names that don't require request types (protocol introspection)
_NO_REQUEST_TOOLS = frozenset({"list_tools", "get_info"})

# Cached dispatch table (initialized on first use)
_dispatch_table: dict[str, tuple[str, TypeAdapter[Any] | None]] | None = None


def _get_dispatch_table() -> dict[str, tuple[str, TypeAdapter[Any] | None]]:
    """Get the tool dispatch table, initializing types on first access.

    This function fails fast with a clear error if types can't be imported.
    """
    global _dispatch_table

    if _dispatch_table is not None:
        return _dispatch_table

    try:
        from adcp.types import _generated as gen
    except ImportError as e:
        raise ImportError(
            f"Failed to load ADCP types. This may indicate a code generation issue. "
            f"Try running 'scripts/generate_types.py' to regenerate types. Error: {e}"
        ) from e

    def _ta(tp: Any) -> TypeAdapter[Any]:
        return TypeAdapter(tp)

    _dispatch_table = {
        # Protocol introspection (no request type needed)
        "list_tools": ("list_tools", None),
        "get_info": ("get_info", None),
        # Core catalog
        "get_products": ("get_products", _ta(gen.GetProductsRequest)),
        "list_creative_formats": ("list_creative_formats", _ta(gen.ListCreativeFormatsRequest)),
        "preview_creative": ("preview_creative", _ta(gen.PreviewCreativeRequest)),
        "build_creative": ("build_creative", _ta(gen.BuildCreativeRequest)),
        "sync_creatives": ("sync_creatives", _ta(gen.SyncCreativesRequest)),
        "list_creatives": ("list_creatives", _ta(gen.ListCreativesRequest)),
        # Media buy
        "create_media_buy": ("create_media_buy", _ta(gen.CreateMediaBuyRequest)),
        "update_media_buy": ("update_media_buy", _ta(gen.UpdateMediaBuyRequest)),
        "get_media_buy_delivery": ("get_media_buy_delivery", _ta(gen.GetMediaBuyDeliveryRequest)),
        "get_media_buys": ("get_media_buys", _ta(gen.GetMediaBuysRequest)),
        # Signals
        "get_signals": ("get_signals", _ta(gen.GetSignalsRequest)),
        "activate_signal": ("activate_signal", _ta(gen.ActivateSignalRequest)),
        "provide_performance_feedback": (
            "provide_performance_feedback",
            _ta(gen.ProvidePerformanceFeedbackRequest),
        ),
        # Accounts
        "list_accounts": ("list_accounts", _ta(gen.ListAccountsRequest)),
        "sync_accounts": ("sync_accounts", _ta(gen.SyncAccountsRequest)),
        "get_account_financials": (
            "get_account_financials",
            _ta(gen.GetAccountFinancialsRequest),
        ),
        "report_usage": ("report_usage", _ta(gen.ReportUsageRequest)),
        # Events
        "log_event": ("log_event", _ta(gen.LogEventRequest)),
        "sync_event_sources": ("sync_event_sources", _ta(gen.SyncEventSourcesRequest)),
        "sync_audiences": ("sync_audiences", _ta(gen.SyncAudiencesRequest)),
        "sync_catalogs": ("sync_catalogs", _ta(gen.SyncCatalogsRequest)),
        # Creative Delivery
        "get_creative_delivery": ("get_creative_delivery", _ta(gen.GetCreativeDeliveryRequest)),
        # V3 Protocol Discovery
        "get_adcp_capabilities": ("get_adcp_capabilities", _ta(gen.GetAdcpCapabilitiesRequest)),
        # V3 Content Standards
        "create_content_standards": (
            "create_content_standards",
            _ta(gen.CreateContentStandardsRequest),
        ),
        "get_content_standards": ("get_content_standards", _ta(gen.GetContentStandardsRequest)),
        "list_content_standards": ("list_content_standards", _ta(gen.ListContentStandardsRequest)),
        "update_content_standards": (
            "update_content_standards",
            _ta(gen.UpdateContentStandardsRequest),
        ),
        "calibrate_content": ("calibrate_content", _ta(gen.CalibrateContentRequest)),
        "validate_content_delivery": (
            "validate_content_delivery",
            _ta(gen.ValidateContentDeliveryRequest),
        ),
        "get_media_buy_artifacts": (
            "get_media_buy_artifacts",
            _ta(gen.GetMediaBuyArtifactsRequest),
        ),
        # V3 Governance
        "get_creative_features": (
            "get_creative_features",
            _ta(gen.GetCreativeFeaturesRequest),
        ),
        "sync_plans": ("sync_plans", _ta(gen.SyncPlansRequest)),
        "check_governance": ("check_governance", _ta(gen.CheckGovernanceRequest)),
        "report_plan_outcome": (
            "report_plan_outcome",
            _ta(gen.ReportPlanOutcomeRequest),
        ),
        "get_plan_audit_logs": (
            "get_plan_audit_logs",
            _ta(gen.GetPlanAuditLogsRequest),
        ),
        # V3 Sponsored Intelligence
        "si_get_offering": ("si_get_offering", _ta(gen.SiGetOfferingRequest)),
        "si_initiate_session": ("si_initiate_session", _ta(gen.SiInitiateSessionRequest)),
        "si_send_message": ("si_send_message", _ta(gen.SiSendMessageRequest)),
        "si_terminate_session": ("si_terminate_session", _ta(gen.SiTerminateSessionRequest)),
        # V3 Governance (Property Lists)
        "create_property_list": ("create_property_list", _ta(gen.CreatePropertyListRequest)),
        "get_property_list": ("get_property_list", _ta(gen.GetPropertyListRequest)),
        "list_property_lists": ("list_property_lists", _ta(gen.ListPropertyListsRequest)),
        "update_property_list": ("update_property_list", _ta(gen.UpdatePropertyListRequest)),
        "delete_property_list": ("delete_property_list", _ta(gen.DeletePropertyListRequest)),
        # V3 Governance (Collection Lists)
        "create_collection_list": (
            "create_collection_list",
            _ta(gen.CreateCollectionListRequest),
        ),
        "get_collection_list": ("get_collection_list", _ta(gen.GetCollectionListRequest)),
        "list_collection_lists": (
            "list_collection_lists",
            _ta(gen.ListCollectionListsRequest),
        ),
        "update_collection_list": (
            "update_collection_list",
            _ta(gen.UpdateCollectionListRequest),
        ),
        "delete_collection_list": (
            "delete_collection_list",
            _ta(gen.DeleteCollectionListRequest),
        ),
        # V3 Governance (Sync Governance)
        "sync_governance": ("sync_governance", _ta(gen.SyncGovernanceRequest)),
        # V3 TMP
        "context_match": ("context_match", _ta(gen.ContextMatchRequest)),
        "identity_match": ("identity_match", _ta(gen.IdentityMatchRequest)),
        # V3 Brand Rights
        "get_brand_identity": ("get_brand_identity", _ta(gen.GetBrandIdentityRequest)),
        "verify_brand_claim": ("verify_brand_claim", _ta(gen.VerifyBrandClaimRequest)),
        "verify_brand_claims": ("verify_brand_claims", _ta(gen.VerifyBrandClaimsRequestBulk)),
        "get_rights": ("get_rights", _ta(gen.GetRightsRequest)),
        "acquire_rights": ("acquire_rights", _ta(gen.AcquireRightsRequest)),
        "update_rights": ("update_rights", _ta(gen.UpdateRightsRequest)),
        # Creative validation
        "validate_input": ("validate_input", _ta(gen.ValidateInputRequest)),
        # V3 Compliance
        "comply_test_controller": ("comply_test_controller", _ta(gen.ComplyTestControllerRequest)),
    }

    return _dispatch_table


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

    from adcp.types.core import TaskResult, TaskStatus

    # Get dispatch table (initializes types on first access, fails fast on import errors)
    dispatch_table = _get_dispatch_table()

    # Check if tool exists
    if tool_name not in dispatch_table:
        available = ", ".join(sorted(dispatch_table.keys()))
        return TaskResult(
            status=TaskStatus.FAILED,
            success=False,
            error=f"Unknown tool: {tool_name}. Available tools: {available}",
        )

    # Get method and request type
    method_name, request_type = dispatch_table[tool_name]
    method = getattr(client, method_name)

    # Special case: list_tools and get_info take no parameters and return
    # data directly, not TaskResult
    if tool_name == "list_tools":
        try:
            tools = await method()
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data={"tools": tools},
                success=True,
            )
        except Exception as e:
            return TaskResult(
                status=TaskStatus.FAILED,
                success=False,
                error=f"Failed to list tools: {e}",
            )

    if tool_name == "get_info":
        try:
            info = await method()
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=info,
                success=True,
            )
        except Exception as e:
            return TaskResult(
                status=TaskStatus.FAILED,
                success=False,
                error=f"Failed to get agent info: {e}",
            )

    # Type guard - adapter should be initialized by this point for methods that need it
    if request_type is None:
        return TaskResult(
            status=TaskStatus.FAILED,
            success=False,
            error=f"Internal error: {tool_name} request type not initialized",
        )

    # Validate and invoke
    try:
        request = request_type.validate_python(payload)
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
            success=False,
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


def merge_headers(saved: dict[str, str] | None, runtime: dict[str, str]) -> dict[str, str]:
    """Merge runtime --header flags over saved-config headers; runtime wins."""
    merged = dict(saved or {})
    merged.update(runtime)
    return merged


def parse_header_args(header_args: list[str] | None) -> dict[str, str]:
    """Parse repeated ``--header KEY=VALUE`` flags into a dict.

    Exits with an error message on malformed input.
    """
    if not header_args:
        return {}
    headers: dict[str, str] = {}
    for raw in header_args:
        if "=" not in raw:
            print(
                f"Error: --header expects KEY=VALUE, got: {raw!r}",
                file=sys.stderr,
            )
            sys.exit(2)
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            print(f"Error: --header has empty key: {raw!r}", file=sys.stderr)
            sys.exit(2)
        headers[key] = value
    return headers


def handle_save_auth(
    alias: str,
    url: str | None,
    protocol: str | None,
    extra_headers: dict[str, str] | None = None,
) -> None:
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

    save_agent(alias, url, protocol, auth_token, extra_headers=extra_headers or None)
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
        extra_headers = config.get("extra_headers") or {}
        if extra_headers:
            keys = ", ".join(sorted(extra_headers))
            print(f"    Headers: {keys}")


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


def handle_resolve(
    agent_url: str,
    agent_type: str | None,
    agent_id: str | None,
    json_output: bool,
) -> None:
    """Handle --resolve command — bootstrap from agent URL to JWK set.

    Walks ``agent_url`` → ``get_adcp_capabilities`` →
    ``identity.brand_json_url`` → ``brand.json`` → ``jwks_uri`` →
    JWK set, with SSRF guards on each hop. Prints either the
    full :class:`AgentResolution` as JSON (``--json``) or a short
    human-readable summary.

    ``--agent-type`` is required because brand.json may list multiple
    agents (sales, governance, creative, etc.) under the same operator
    and the resolver can't infer which one ``agent_url`` corresponds to
    from the URL alone.
    """
    from adcp.signing.agent_resolver import (
        AgentResolverError,
        async_resolve_agent,
    )

    if not agent_type:
        print(
            "Error: --agent-type is required with --resolve "
            "(brand|rights|measurement|governance|creative|sales|buying|signals)",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        result = asyncio.run(
            async_resolve_agent(
                agent_url,
                agent_type=cast(Any, agent_type),
                agent_id=agent_id,
            )
        )
    except AgentResolverError as exc:
        if json_output:
            print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, indent=2))
        else:
            print(f"Error [{exc.code}]: {exc.message}", file=sys.stderr)
        sys.exit(1)

    if json_output:
        print(result.model_dump_json(indent=2, exclude_none=True))
        return

    print(f"agent_url:       {result.agent_url}")
    print(f"brand_json_url:  {result.brand_json_url}")
    print(f"jwks_uri:        {result.jwks_uri}")
    print(f"agent_entry:     {json.dumps(result.agent_entry)}")
    print(f"jwks keys:       {len(result.jwks.get('keys', []))}")
    print("trace:")
    for entry in result.trace:
        marker = "✓" if entry.status == "ok" else "✗"
        line = f"  {marker} [{entry.hop}] {entry.url} ({entry.latency_ms:.0f}ms)"
        if entry.error_code:
            line += f"  error={entry.error_code}: {entry.error_message}"
        print(line)


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
    parser.add_argument("--version", action="store_true", help="Show SDK and AdCP version")
    parser.add_argument(
        "--resolve",
        metavar="AGENT_URL",
        help="Resolve agent identity via brand.json (capabilities → brand.json → JWKS).",
    )
    parser.add_argument(
        "--agent-type",
        metavar="TYPE",
        choices=[
            "brand",
            "rights",
            "measurement",
            "governance",
            "creative",
            "sales",
            "buying",
            "signals",
        ],
        help="Agent type for --resolve (matches the brand.json agents[] entry). "
        "Required with --resolve.",
    )
    parser.add_argument(
        "--agent-id",
        metavar="ID",
        help="Optional agent ID for --resolve (disambiguates multiple agents of "
        "the same type in brand.json).",
    )

    # Execution options
    parser.add_argument("--protocol", choices=["mcp", "a2a"], help="Force protocol type")
    parser.add_argument("--auth", help="Authentication token")
    parser.add_argument(
        "--header",
        "-H",
        action="append",
        metavar="KEY=VALUE",
        help="Additional HTTP header sent on every request (repeatable). "
        "Example: -H x-adcp-tenant=acme. With --save-auth, persists into the "
        "saved agent config.",
    )
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
                args.version,
                args.resolve,
            ]
        )
    ):
        parser.print_help()
        print("\nExamples:")
        print("  adcp --version")
        print("  adcp --save-auth myagent https://agent.example.com mcp")
        print("  adcp --list-agents")
        print("  adcp myagent get_info")
        print("  adcp myagent list_tools")
        print('  adcp myagent get_products \'{"brief":"TV ads"}\'')
        print("  adcp https://agent.example.com list_tools")
        print("\nV3 Protocol Examples:")
        print("  adcp myagent get_adcp_capabilities")
        print('  adcp cs-agent calibrate_content \'{"content_standards_id":"cs-123"}\'')
        print("  adcp si-agent si_get_offering")
        print("  adcp gov-agent list_property_lists")
        print("\nIdentity Resolution Examples:")
        print("  adcp --resolve https://buyer.example.com/mcp --agent-type sales")
        print(
            "  adcp --resolve https://buyer.example.com/mcp --agent-type sales --json | "
            "jq .jwks_uri"
        )
        sys.exit(0)

    # Handle configuration commands
    if args.version:
        from adcp import __version__, get_adcp_version

        print(f"AdCP Python SDK: v{__version__}")
        print(f"Target AdCP Spec: {get_adcp_version()}")
        sys.exit(0)

    if args.save_auth:
        url = args.agent if args.agent else None
        protocol = args.tool if args.tool else None
        cli_headers = parse_header_args(args.header)
        handle_save_auth(args.save_auth, url, protocol, extra_headers=cli_headers)
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

    if args.resolve:
        handle_resolve(args.resolve, args.agent_type, args.agent_id, args.json)
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

    cli_headers = parse_header_args(args.header)
    if cli_headers:
        agent_config["extra_headers"] = merge_headers(
            agent_config.get("extra_headers"), cli_headers
        )

    if args.debug:
        agent_config["debug"] = True

    # Load payload
    payload = load_payload(args.payload)

    # Execute
    asyncio.run(execute_tool(agent_config, args.tool, payload, args.json))


if __name__ == "__main__":
    main()
