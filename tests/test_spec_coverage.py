"""Spec coverage tests for SDK surface area.

These tests ensure the schema index and the SDK's public task surfaces stay aligned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _schema_task_names() -> set[str]:
    index_path = Path(__file__).resolve().parents[1] / "schemas" / "cache" / "index.json"
    index_data = json.loads(index_path.read_text())

    task_names: set[str] = set()
    for schema_group in index_data.get("schemas", {}).values():
        tasks = schema_group.get("tasks") if isinstance(schema_group, dict) else None
        if isinstance(tasks, dict):
            task_names.update(name.replace("-", "_") for name in tasks)

    return task_names


def test_client_methods_cover_schema_index():
    """ADCPClient exposes every schema task as a method."""
    from adcp.client import ADCPClient

    missing = sorted(name for name in _schema_task_names() if not hasattr(ADCPClient, name))
    assert missing == []


def test_handler_methods_cover_schema_index():
    """ADCPHandler provides a default stub for every schema task."""
    from adcp.server import ADCPHandler

    missing = sorted(name for name in _schema_task_names() if not hasattr(ADCPHandler, name))
    assert missing == []


def test_protocol_adapters_cover_schema_index():
    """Concrete protocol adapters implement every schema task wrapper."""
    from adcp.protocols.a2a import A2AAdapter
    from adcp.protocols.mcp import MCPAdapter

    task_names = _schema_task_names()
    mcp_missing = sorted(name for name in task_names if not hasattr(MCPAdapter, name))
    a2a_missing = sorted(name for name in task_names if not hasattr(A2AAdapter, name))

    assert mcp_missing == []
    assert a2a_missing == []


def test_cli_dispatch_covers_schema_index():
    """CLI dispatch table covers every schema task."""
    from adcp.__main__ import _get_dispatch_table

    dispatch_table = _get_dispatch_table()
    missing = sorted(name for name in _schema_task_names() if name not in dispatch_table)
    assert missing == []


def test_mcp_tool_definitions_cover_schema_index():
    """MCP tool definitions cover every schema task."""
    from adcp.server.mcp_tools import ADCP_TOOL_DEFINITIONS

    tool_names = {tool["name"] for tool in ADCP_TOOL_DEFINITIONS}
    missing = sorted(name for name in _schema_task_names() if name not in tool_names)
    assert missing == []


def test_tool_filtering_by_handler_type():
    """Specialized handlers get only their tools plus protocol discovery."""
    from adcp.server.mcp_tools import ADCP_TOOL_DEFINITIONS, get_tools_for_handler

    all_tool_names = {tool["name"] for tool in ADCP_TOOL_DEFINITIONS}

    # GovernanceHandler: governance tools + protocol discovery
    from adcp.server.base import ADCPHandler
    from adcp.server.content_standards import ContentStandardsHandler
    from adcp.server.governance import GovernanceHandler
    from adcp.server.sponsored_intelligence import SponsoredIntelligenceHandler

    gov_tools = {t["name"] for t in get_tools_for_handler(GovernanceHandler)}
    assert gov_tools == {
        "get_creative_features", "sync_plans", "check_governance",
        "report_plan_outcome", "get_plan_audit_logs",
        "create_property_list", "get_property_list", "list_property_lists",
        "update_property_list", "delete_property_list",
        "get_adcp_capabilities",
    }

    # ContentStandardsHandler: content standards tools + protocol discovery
    cs_tools = {t["name"] for t in get_tools_for_handler(ContentStandardsHandler)}
    assert cs_tools == {
        "create_content_standards", "get_content_standards",
        "list_content_standards", "update_content_standards",
        "calibrate_content", "validate_content_delivery",
        "get_media_buy_artifacts",
        "get_adcp_capabilities",
    }

    # SponsoredIntelligenceHandler: SI tools + protocol discovery
    si_tools = {t["name"] for t in get_tools_for_handler(SponsoredIntelligenceHandler)}
    assert si_tools == {
        "si_get_offering", "si_initiate_session",
        "si_send_message", "si_terminate_session",
        "get_adcp_capabilities",
    }

    # ADCPHandler: all tools (no filtering)
    adcp_tools = {t["name"] for t in get_tools_for_handler(ADCPHandler)}
    assert adcp_tools == all_tool_names

    # Subclass of GovernanceHandler gets governance tools (MRO walk)
    class MyGovernanceAgent(GovernanceHandler):
        pass

    subclass_tools = {t["name"] for t in get_tools_for_handler(MyGovernanceAgent)}
    assert subclass_tools == gov_tools


def _collect_all_properties(schema: dict[str, Any]) -> set[str]:
    """Extract all top-level property names from a JSON schema.

    Handles plain objects, oneOf/anyOf union schemas, and $defs references.
    """
    defs = schema.get("$defs", {})

    def _props_from_subschema(sub: dict[str, Any]) -> set[str]:
        if "properties" in sub:
            return set(sub["properties"].keys())
        if "$ref" in sub:
            ref_name = sub["$ref"].rsplit("/", 1)[-1]
            ref_schema = defs.get(ref_name, {})
            return set(ref_schema.get("properties", {}).keys())
        return set()

    # Direct properties on the schema
    if "properties" in schema:
        return set(schema["properties"].keys())

    # Union types produce oneOf or anyOf at top level
    for key in ("oneOf", "anyOf"):
        if key in schema:
            props: set[str] = set()
            for variant in schema[key]:
                props |= _props_from_subschema(variant)
            return props

    return set()


def test_mcp_tool_input_schema_matches_pydantic_models():
    """MCP tool inputSchema properties stay in sync with Pydantic request models.

    For each MCP tool that has a corresponding Pydantic request model, verify
    that no *new* fields have appeared on the model without being reflected in
    the hand-written MCP inputSchema.

    Known gaps (fields the MCP schema intentionally omits) are recorded below.
    If a Pydantic model gains a field that is not in the MCP inputSchema AND
    not in the known-gaps set, this test fails -- signaling that the MCP tool
    definition needs updating.
    """
    from adcp.__main__ import _get_dispatch_table
    from adcp.server.mcp_tools import ADCP_TOOL_DEFINITIONS

    dispatch_table = _get_dispatch_table()
    tool_schemas = {
        tool["name"]: set(tool["inputSchema"].get("properties", {}).keys())
        for tool in ADCP_TOOL_DEFINITIONS
    }

    # Tools with no request type or that intentionally diverge
    SKIP_TOOLS = {"list_tools", "get_info"}

    # Fields the MCP inputSchema intentionally omits per tool.
    # When adding a field to a Pydantic model, either add it to the MCP
    # inputSchema in mcp_tools.py or add it here with a comment explaining why.
    KNOWN_GAPS: dict[str, set[str]] = {
        "get_products": {
            "account", "brand", "brief", "buyer_campaign_ref", "buying_mode",
            "catalog", "ext", "preferred_delivery_types", "property_list",
            "refine", "required_policies", "time_budget",
        },
        "list_creative_formats": {
            "asset_types", "context", "disclosure_persistence",
            "disclosure_positions", "ext", "format_ids", "input_format_ids",
            "is_responsive", "max_height", "max_width", "min_height",
            "min_width", "name_search", "output_format_ids", "wcag_level",
        },
        "preview_creative": {
            "context", "creative_id", "ext", "inputs", "item_limit", "quality",
            "request_type", "requests", "template_id", "variant_id",
        },
        "build_creative": {
            "brand", "concept_id", "context", "creative_id",
            "creative_manifest", "ext", "idempotency_key", "include_preview",
            "item_limit", "macro_values", "media_buy_id", "message",
            "package_id", "preview_inputs", "preview_output_format",
            "preview_quality", "quality", "target_format_id",
            "target_format_ids",
        },
        "sync_creatives": {
            "account", "assignments", "context", "creative_ids",
            "delete_missing", "dry_run", "ext", "idempotency_key",
            "push_notification_config", "validation_mode",
        },
        "list_creatives": {
            "context", "ext", "include_assignments", "include_items",
            "include_snapshot", "include_variables", "sort",
        },
        "create_media_buy": {
            "account", "advertiser_industry", "artifact_webhook", "brand",
            "buyer_campaign_ref", "buyer_ref", "context", "end_time", "ext",
            "idempotency_key", "invoice_recipient", "io_acceptance", "plan_id",
            "po_number", "push_notification_config", "reporting_webhook",
            "start_time", "total_budget",
        },
        "update_media_buy": {
            "buyer_ref", "canceled", "cancellation_reason", "context",
            "end_time", "ext", "idempotency_key", "invoice_recipient",
            "new_packages", "paused", "push_notification_config",
            "reporting_webhook", "revision", "start_time", "status_filter",
            "total_budget",
        },
        "get_media_buy_delivery": {
            "account", "attribution_window", "buyer_refs", "context",
            "end_date", "ext", "fields", "include_creative_breakdown",
            "include_package_daily_breakdown", "media_buy_ids",
            "metrics_granularity", "package_ids", "pagination",
            "reporting_dimensions", "start_date", "status_filter",
        },
        "get_media_buys": {
            "context", "ext", "fields", "include_delivery_snapshot",
            "include_history", "include_packages", "include_snapshot",
            "status_filter",
        },
        "get_signals": {
            "account", "buyer_campaign_ref", "context", "countries",
            "destinations", "ext", "max_results", "signal_ids", "signal_spec",
        },
        "activate_signal": {
            "account", "action", "buyer_campaign_ref", "context",
            "destinations", "ext", "idempotency_key", "pricing_option_id",
            "signal_agent_segment_id",
        },
        "provide_performance_feedback": {
            "buyer_ref", "context", "creative_id", "ext", "feedback_source",
            "idempotency_key", "measurement_period", "metric_type",
            "package_id", "performance_index",
        },
        "list_accounts": {"context", "ext", "sandbox", "status"},
        "sync_accounts": {
            "context", "delete_missing", "dry_run", "ext",
            "push_notification_config",
        },
        "get_account_financials": {"context", "ext", "period"},
        "report_usage": {"context", "ext", "idempotency_key", "reporting_period"},
        "log_event": {
            "context", "event_source_id", "ext", "idempotency_key",
            "test_event_code",
        },
        "sync_event_sources": {"account", "context", "delete_missing", "ext"},
        "sync_audiences": {"context", "delete_missing", "ext"},
        "sync_catalogs": {
            "catalog_ids", "context", "delete_missing", "dry_run", "ext",
            "push_notification_config", "validation_mode",
        },
        "get_creative_delivery": {
            "account", "context", "end_date", "ext", "max_variants",
            "media_buy_buyer_refs", "pagination", "start_date",
        },
        "get_adcp_capabilities": {"context", "ext", "protocols"},
        "create_content_standards": {
            "calibration_exemplars", "context", "ext", "idempotency_key",
            "policy", "registry_policy_ids", "scope",
        },
        "get_content_standards": {"context", "ext", "standards_id"},
        "list_content_standards": {
            "channels", "context", "countries", "ext", "languages",
        },
        "update_content_standards": {
            "calibration_exemplars", "context", "ext", "idempotency_key",
            "policy", "registry_policy_ids", "scope", "standards_id",
        },
        "calibrate_content": {"artifact", "idempotency_key", "standards_id"},
        "validate_content_delivery": {
            "context", "ext", "feature_ids", "include_passed", "records",
            "standards_id",
        },
        "get_media_buy_artifacts": {
            "account", "context", "ext", "failures_only", "package_ids",
            "pagination", "sampling", "time_range",
        },
        "get_creative_features": {"ext", "feature_ids"},
        "check_governance": {
            "buyer_ref", "delivery_metrics", "invoice_recipient",
            "media_buy_id", "modification_summary", "phase",
            "planned_delivery",
        },
        "report_plan_outcome": {
            "governance_context", "idempotency_key",
        },
        "si_get_offering": {
            "context", "ext", "include_products", "offering_id",
            "product_limit",
        },
        "si_initiate_session": {
            "context", "ext", "idempotency_key", "identity", "media_buy_id",
            "offering_id", "offering_token", "placement",
            "supported_capabilities",
        },
        "si_send_message": {"action_response", "ext"},
        "si_terminate_session": {"ext", "reason", "termination_context"},
        "create_property_list": {"brand", "context", "ext", "idempotency_key"},
        "get_property_list": {"context", "ext"},
        "list_property_lists": {"context", "ext", "name_contains"},
        "update_property_list": {
            "base_properties", "brand", "context", "ext", "idempotency_key",
            "webhook_url",
        },
        "delete_property_list": {"context", "ext", "idempotency_key"},
    }

    drift: list[str] = []

    for tool_name, (_, type_adapter) in dispatch_table.items():
        if tool_name in SKIP_TOOLS or type_adapter is None:
            continue
        if tool_name not in tool_schemas:
            continue

        model_schema = type_adapter.json_schema()
        model_props = _collect_all_properties(model_schema)
        mcp_props = tool_schemas[tool_name]
        known = KNOWN_GAPS.get(tool_name, set())

        missing = sorted(model_props - mcp_props - known)
        if missing:
            drift.append(f"{tool_name}: model has {missing} not in MCP inputSchema")

    assert drift == [], (
        "MCP tool inputSchema fields have drifted from Pydantic models.\n"
        "Either add the field to ADCP_TOOL_DEFINITIONS in mcp_tools.py,\n"
        "or add it to KNOWN_GAPS in this test with a comment explaining why.\n"
        + "\n".join(f"  - {d}" for d in drift)
    )
