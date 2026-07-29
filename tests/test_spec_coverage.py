"""Spec coverage tests for SDK surface area.

These tests ensure the schema index and the SDK's public task surfaces stay aligned.
"""

from __future__ import annotations

import json
from pathlib import Path


def _schema_task_names() -> set[str]:
    from adcp._version import _read_packaged_version
    from adcp.validation.version import resolve_bundle_key

    bundle_key = resolve_bundle_key(_read_packaged_version())
    index_path = (
        Path(__file__).resolve().parents[1] / "schemas" / "cache" / bundle_key / "index.json"
    )
    index_data = json.loads(index_path.read_text())

    task_names: set[str] = set()
    for schema_group in index_data.get("schemas", {}).values():
        tasks = schema_group.get("tasks") if isinstance(schema_group, dict) else None
        if isinstance(tasks, dict):
            task_names.update(name.replace("-", "_") for name in tasks)

    return task_names


def _python_task_name(schema_task_name: str) -> str:
    """Map wire task names whose Python surface is intentionally legacy-only."""
    if schema_task_name == "list_creative_formats":
        return "list_creative_formats_legacy"
    return schema_task_name


def test_client_methods_cover_schema_index():
    """ADCPClient exposes every schema task as a method."""
    from adcp.client import ADCPClient

    missing = sorted(
        name for name in _schema_task_names() if not hasattr(ADCPClient, _python_task_name(name))
    )
    assert missing == []


def test_handler_methods_cover_schema_index():
    """ADCPHandler provides a default stub for every schema task."""
    from adcp.server import ADCPHandler

    missing = sorted(
        name for name in _schema_task_names() if not hasattr(ADCPHandler, _python_task_name(name))
    )
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
    missing = sorted(
        name for name in _schema_task_names() if _python_task_name(name) not in dispatch_table
    )
    assert missing == []


def test_mcp_tool_definitions_cover_schema_index():
    """MCP tool definitions cover every schema task."""
    from adcp.server.mcp_tools import ADCP_TOOL_DEFINITIONS

    tool_names = {tool["name"] for tool in ADCP_TOOL_DEFINITIONS}
    missing = sorted(name for name in _schema_task_names() if name not in tool_names)
    assert missing == []


def test_feature_and_domain_maps_cover_brand_tasks() -> None:
    """Every brand task appears in TASK_FEATURE_MAP and HANDLER_TO_DOMAIN.

    A missing entry is a silent fail-open: client-side ``FeatureResolver``
    skips the feature check, and decorator-style servers fail to advertise
    the ``brand`` domain in auto-generated capabilities.

    Scoped to brand tasks for now — extend as other task families adopt
    feature-gated handlers.
    """
    from adcp.capabilities import TASK_FEATURE_MAP
    from adcp.server.builder import HANDLER_TO_DOMAIN

    brand_tasks = {name for name, domain in HANDLER_TO_DOMAIN.items() if domain == "brand"}
    missing = sorted(t for t in brand_tasks if t not in TASK_FEATURE_MAP)
    assert missing == [], (
        f"brand tasks present in HANDLER_TO_DOMAIN but missing from "
        f"TASK_FEATURE_MAP (fail-open gap): {missing}"
    )


def test_tool_filtering_by_handler_type():
    """Specialized handlers get only their tools plus protocol discovery.

    This test exercises the handler-type filter in isolation — the
    pre-#220 behavior — by passing ``advertise_all=True`` so the
    override-based filter doesn't intersect the expected sets. The
    override-based default is covered separately by
    ``tests/test_advertised_tools_gate.py``.
    """
    from adcp.server.mcp_tools import ADCP_TOOL_DEFINITIONS, get_tools_for_handler

    all_tool_names = {tool["name"] for tool in ADCP_TOOL_DEFINITIONS}

    # GovernanceHandler: governance tools + protocol discovery
    from adcp.server.base import ADCPHandler
    from adcp.server.content_standards import ContentStandardsHandler
    from adcp.server.governance import GovernanceHandler
    from adcp.server.sponsored_intelligence import SponsoredIntelligenceHandler

    gov_tools = {t["name"] for t in get_tools_for_handler(GovernanceHandler, advertise_all=True)}
    assert gov_tools == {
        "get_creative_features",
        "sync_plans",
        "check_governance",
        "report_plan_outcome",
        "get_plan_audit_logs",
        "create_property_list",
        "get_property_list",
        "list_property_lists",
        "update_property_list",
        "delete_property_list",
        "create_collection_list",
        "get_collection_list",
        "list_collection_lists",
        "update_collection_list",
        "delete_collection_list",
        "get_adcp_capabilities",
    }

    # ContentStandardsHandler: content standards tools + protocol discovery
    cs_tools = {
        t["name"] for t in get_tools_for_handler(ContentStandardsHandler, advertise_all=True)
    }
    assert cs_tools == {
        "create_content_standards",
        "get_content_standards",
        "list_content_standards",
        "update_content_standards",
        "calibrate_content",
        "validate_content_delivery",
        "get_media_buy_artifacts",
        "get_adcp_capabilities",
    }

    # SponsoredIntelligenceHandler: SI tools + protocol discovery
    si_tools = {
        t["name"] for t in get_tools_for_handler(SponsoredIntelligenceHandler, advertise_all=True)
    }
    assert si_tools == {
        "si_get_offering",
        "si_initiate_session",
        "si_send_message",
        "si_terminate_session",
        "get_adcp_capabilities",
    }

    # ADCPHandler: all tools (no filtering)
    adcp_tools = {t["name"] for t in get_tools_for_handler(ADCPHandler, advertise_all=True)}
    assert adcp_tools == all_tool_names

    # Subclass of GovernanceHandler gets governance tools (MRO walk)
    class MyGovernanceAgent(GovernanceHandler):
        pass

    subclass_tools = {
        t["name"] for t in get_tools_for_handler(MyGovernanceAgent, advertise_all=True)
    }
    assert subclass_tools == gov_tools


def test_mcp_tool_input_schema_matches_pydantic_models():
    """MCP tool inputSchemas are generated from Pydantic request models.

    The ``ADCP_TOOL_DEFINITIONS[*].inputSchema`` is overwritten on first
    ``tools/list`` call by ``_ensure_pydantic_schemas_applied()`` with the output of
    ``model_json_schema()`` on the corresponding ``<ToolName>Request``
    model. This test is a coarse guard that every tool with a mapped
    request model carries a schema advertising every field of that
    model. For byte-level drift enforcement, see
    ``tests/test_mcp_schema_drift.py``.
    """
    from adcp.__main__ import _get_dispatch_table
    from adcp.server.mcp_tools import _PYDANTIC_SCHEMAS, ADCP_TOOL_DEFINITIONS

    dispatch_table = _get_dispatch_table()
    tool_schemas = {
        tool["name"]: set(tool["inputSchema"].get("properties", {}).keys())
        for tool in ADCP_TOOL_DEFINITIONS
    }

    # Tools with no request type or that intentionally diverge
    skip_tools = {"list_tools", "get_info"}

    drift: list[str] = []
    for tool_name, (_, type_adapter) in dispatch_table.items():
        if tool_name in skip_tools or type_adapter is None:
            continue
        if tool_name not in tool_schemas:
            continue
        # If generation failed for this tool, the inputSchema is the
        # hand-crafted stub — covered by tests/test_mcp_schema_drift.py's
        # test_every_tool_has_pydantic_generated_schema.
        if tool_name not in _PYDANTIC_SCHEMAS:
            continue

        model_props = set(type_adapter.json_schema().get("properties", {}).keys())
        mcp_props = tool_schemas[tool_name]
        missing = sorted(model_props - mcp_props)
        if missing:
            drift.append(f"{tool_name}: model has {missing} not in inputSchema")

    assert drift == [], (
        "MCP tool inputSchema fields have drifted from Pydantic models.\n"
        "The inputSchema is auto-generated from the request model on first\n"
        "tools/list call (_ensure_pydantic_schemas_applied()); this drift\n"
        "shouldn't be possible unless schema generation is broken.\n"
        "See tests/test_mcp_schema_drift.py.\n" + "\n".join(f"  - {d}" for d in drift)
    )
