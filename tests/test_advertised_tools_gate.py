"""The override-based advertised-tools gate — closes #220.

Before #220, ``get_tools_for_handler`` returned every tool in the
handler-type's allowed set. A minimal seller agent that only overrode
``get_products`` still advertised all 57 tools in ``tools/list`` — 55
of them answering ``not_supported`` to every call. With Pydantic-
generated schemas averaging several hundred tokens each, that's a
significant context tax on every agent client that connects.

#220 adds an override filter: by default only tools whose method has
been overridden by the subclass are advertised. Spec-mandated discovery
tools (``get_adcp_capabilities``, anything in
:data:`~adcp.server.DISCOVERY_TOOLS`) are always advertised. Sellers
who want the old behavior — e.g. for spec-compliance storyboards that
exercise every tool — pass ``advertise_all=True``.

These tests lock the contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.server import (
    ADCPHandler,
    GovernanceHandler,
    create_mcp_server,
)
from adcp.server.a2a_server import ADCPAgentExecutor
from adcp.server.mcp_tools import (
    ADCP_TOOL_DEFINITIONS,
    DISCOVERY_TOOLS,
    get_tools_for_handler,
)

# ---------------------------------------------------------------------------
# Override detection — the filter's core logic
# ---------------------------------------------------------------------------


def test_bare_adcphandler_subclass_advertises_only_discovery_tools():
    """A subclass that implements ``get_adcp_capabilities`` and nothing
    else should advertise just that + any auth-optional discovery
    tools. 1 tool advertised vs the 57 the pre-#220 default would have
    exposed."""

    class _Empty(ADCPHandler):
        _agent_type = "empty"

        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

    tools = {t["name"] for t in get_tools_for_handler(_Empty())}
    assert tools == {"get_adcp_capabilities"} | DISCOVERY_TOOLS


def test_single_override_advertises_only_that_tool_plus_discovery():
    """One override = one advertised tool beyond discovery. This is the
    common minimal-agent case — and the reduction this PR is for."""

    class _Minimal(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(self, params, context=None):
            return {"products": []}

    tools = {t["name"] for t in get_tools_for_handler(_Minimal())}
    assert tools == {"get_adcp_capabilities", "get_products"} | DISCOVERY_TOOLS


def test_multiple_overrides_advertise_every_override():
    """Handler that overrides N tools advertises exactly those N plus
    discovery."""

    class _Multi(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(self, params, context=None):
            return {}

        async def create_media_buy(self, params, context=None):
            return {}

        async def sync_creatives(self, params, context=None):
            return {}

    expected = {
        "get_adcp_capabilities",
        "get_products",
        "create_media_buy",
        "sync_creatives",
    } | DISCOVERY_TOOLS
    tools = {t["name"] for t in get_tools_for_handler(_Multi())}
    assert tools == expected


def test_specialized_handler_delegation_pattern_counts_as_override():
    """**Threat 3 regression**: when a subclass of a specialized handler
    base (``GovernanceHandler``, ``ContentStandardsHandler``,
    ``SponsoredIntelligenceHandler``) follows the documented pattern —
    implementing ``handle_<tool>`` and inheriting the public method
    unchanged — the override gate must still count the tool as
    implemented. Before the fix, such a seller advertised **zero**
    governance tools (e.g. ``update_property_list``, ``acquire_rights``
    silently disappeared from ``tools/list``), because the gate only
    checked the public method and saw it unchanged from the SDK base.

    A fully-implemented ``GovernanceHandler`` subclass must advertise
    every governance tool, regardless of whether the subclass reopens
    the public method.
    """

    class _FullGovernance(GovernanceHandler):
        _agent_type = "full governance"

        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        # Implement every abstract handle_* via the delegation pattern.
        # None of these reopen the public method.
        async def handle_get_creative_features(self, request: Any, context: Any = None) -> Any:
            return {}

        async def handle_sync_plans(self, request: Any, context: Any = None) -> Any:
            return {}

        async def handle_check_governance(self, request: Any, context: Any = None) -> Any:
            return {}

        async def handle_report_plan_outcome(self, request: Any, context: Any = None) -> Any:
            return {}

        async def handle_get_plan_audit_logs(self, request: Any, context: Any = None) -> Any:
            return {}

        async def handle_create_property_list(self, request: Any, context: Any = None) -> Any:
            return {}

        async def handle_get_property_list(self, request: Any, context: Any = None) -> Any:
            return {}

        async def handle_list_property_lists(self, request: Any, context: Any = None) -> Any:
            return {}

        async def handle_update_property_list(self, request: Any, context: Any = None) -> Any:
            return {}

        async def handle_delete_property_list(self, request: Any, context: Any = None) -> Any:
            return {}

    tools = {t["name"] for t in get_tools_for_handler(_FullGovernance())}
    # Every governance tool the handler implements via handle_<tool>
    # must appear — including security-sensitive mutations like
    # update_property_list and delete_property_list. Plus the protocol
    # discovery tool set.
    expected = {
        "get_adcp_capabilities",
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
    } | DISCOVERY_TOOLS
    assert tools == expected


def _make_concrete_subclass(base: type) -> type:
    """Build a concrete subclass of ``base`` that stubs every
    ``@abstractmethod handle_<tool>`` it declares. Used by the Threat-3
    regression tests — Python's ABC machinery requires the concrete
    methods to be in the class namespace at creation time, not
    ``setattr``-ed after.
    """
    abstracts = {
        name
        for name in dir(base)
        if name.startswith("handle_")
        and getattr(getattr(base, name, None), "__isabstractmethod__", False)
    }

    async def _capabilities(self, params, context=None):  # noqa: ARG001
        return {"adcp": {"major_versions": [3]}}

    async def _stub(self, request, context=None):  # noqa: ARG001
        return {}

    namespace: dict[str, Any] = {"get_adcp_capabilities": _capabilities}
    for _name in abstracts:
        namespace[_name] = _stub

    return type(f"_{base.__name__}Concrete", (base,), namespace)


def test_specialized_handler_mutation_tools_never_silently_hidden():
    """**Threat 3 regression guard**: the security-sensitive mutation
    tools on ``GovernanceHandler``, ``ContentStandardsHandler``, and
    ``SponsoredIntelligenceHandler`` must appear in ``tools/list`` when
    the subclass follows the documented delegation pattern. Hiding them
    silently would let callers infer the seller can't accept those
    calls when the seller is actively listening for them — a correctness
    and security hazard. This test lists the exact tool names so any
    regression in override detection fails loudly with a named miss.
    """
    from adcp.server import ContentStandardsHandler, SponsoredIntelligenceHandler

    gov_tools = {
        t["name"] for t in get_tools_for_handler(_make_concrete_subclass(GovernanceHandler)())
    }
    # Property-list tools are the ones GovernanceHandler actually
    # declares abstract ``handle_<tool>`` for — these are the
    # security-sensitive mutations the security reviewer called out.
    # (The collection-list tools live in the GovernanceHandler tool set
    # but aren't plumbed through a ``handle_<tool>`` pattern yet, so
    # they're correctly hidden until someone wires them up — a separate
    # gap, not a Threat 3 regression.)
    assert {
        "update_property_list",
        "delete_property_list",
    }.issubset(gov_tools), "Threat 3: governance mutation tools hidden from tools/list"

    content_tools = {
        t["name"] for t in get_tools_for_handler(_make_concrete_subclass(ContentStandardsHandler)())
    }
    assert (
        "update_content_standards" in content_tools
    ), "Threat 3: content-standards mutation tools hidden from tools/list"

    si_tools = {
        t["name"]
        for t in get_tools_for_handler(_make_concrete_subclass(SponsoredIntelligenceHandler)())
    }
    assert (
        "si_terminate_session" in si_tools
    ), "Threat 3: SI destructive tools hidden from tools/list"


# ---------------------------------------------------------------------------
# advertise_all escape hatch
# ---------------------------------------------------------------------------


def test_advertise_all_restores_pre_220_behavior():
    """``advertise_all=True`` returns the full handler-type tool set —
    including not_supported defaults. Needed for spec-compliance
    storyboard tests that exercise every tool."""

    class _Empty(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

    default = {t["name"] for t in get_tools_for_handler(_Empty())}
    all_tools = {t["name"] for t in get_tools_for_handler(_Empty(), advertise_all=True)}

    # Default should be small; advertise_all should return everything
    # ADCPHandler's allowed set covers.
    assert default == {"get_adcp_capabilities"} | DISCOVERY_TOOLS
    assert len(all_tools) == len(ADCP_TOOL_DEFINITIONS)
    assert default.issubset(all_tools)


# ---------------------------------------------------------------------------
# create_mcp_server / create_a2a_server threading
# ---------------------------------------------------------------------------


def test_create_mcp_server_defaults_to_override_filter():
    """``create_mcp_server`` should register only overridden tools on
    the FastMCP instance."""

    class _Minimal(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(self, params, context=None):
            return {"products": []}

    mcp = create_mcp_server(_Minimal(), name="test-agent")
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert tool_names == {"get_adcp_capabilities", "get_products"} | DISCOVERY_TOOLS


def test_create_mcp_server_advertise_all_restores_full_surface():
    """The escape hatch reaches through create_mcp_server."""

    class _Minimal(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

    mcp = create_mcp_server(_Minimal(), name="test-agent", advertise_all=True)
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    # Full ADCP surface minus comply_test_controller (which is
    # gated behind include_test_controller=True).
    assert len(tool_names) >= 50


def test_adcp_agent_executor_defaults_to_override_filter():
    """The A2A executor's ``supported_skills`` mirrors the filtered list."""

    class _Minimal(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(self, params, context=None):
            return {"products": []}

    executor = ADCPAgentExecutor(_Minimal())
    assert (
        set(executor.supported_skills)
        == {
            "get_adcp_capabilities",
            "get_products",
        }
        | DISCOVERY_TOOLS
    )


def test_adcp_agent_executor_advertise_all_restores_full_surface():
    """The escape hatch reaches through the A2A executor."""

    class _Minimal(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

    executor = ADCPAgentExecutor(_Minimal(), advertise_all=True)
    # Same as create_mcp_server check — full surface, minus comply_test_controller.
    assert len(executor.supported_skills) >= 50


# ---------------------------------------------------------------------------
# Decorator-wrapped overrides
# ---------------------------------------------------------------------------


def test_decorator_wrapped_override_counts_as_override():
    """Overriding a tool method with a decorator that rebinds the
    function (``@functools.wraps`` around a new closure) produces a
    different function object than the SDK base's. The gate uses
    identity comparison — decorator-wrapped overrides must still flip
    the bit, because the wrapper IS the override.
    """
    import functools

    def _noop_decorator(fn):
        @functools.wraps(fn)
        async def _wrapped(self, params, context=None):
            return await fn(self, params, context)

        return _wrapped

    class _Decorated(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        @_noop_decorator
        async def get_products(self, params, context=None):
            return {"products": []}

    tools = {t["name"] for t in get_tools_for_handler(_Decorated())}
    assert "get_products" in tools


# ---------------------------------------------------------------------------
# SDK-base-class detection reads _HANDLER_TOOLS live (no frozen-set drift)
# ---------------------------------------------------------------------------


def test_is_sdk_base_class_reads_handler_tools_live():
    """``_is_sdk_base_class`` reads ``_HANDLER_TOOLS`` directly so handlers
    registered after import time (via :func:`register_handler_tools` or
    :meth:`ADCPHandler.__init_subclass__`) participate in override
    detection without rebuilding any cached set. Regression coverage for
    the prior frozen-set drift bug.
    """
    from adcp.server.mcp_tools import (
        _HANDLER_TOOLS,
        _is_sdk_base_class,
        register_handler_tools,
    )

    # Built-in bases recognised.
    for name in _HANDLER_TOOLS:
        assert _is_sdk_base_class(name), name

    # Unknown name rejected.
    assert not _is_sdk_base_class("DefinitelyNotAHandler")

    # Newly registered name picked up immediately, no rebuild.
    register_handler_tools("_TestLiveDetectionHandler", {"get_products"})
    try:
        assert _is_sdk_base_class("_TestLiveDetectionHandler")
    finally:
        # Test-cleanup — remove the synthetic registration so other
        # tests don't see drift.
        _HANDLER_TOOLS.pop("_TestLiveDetectionHandler", None)


# ---------------------------------------------------------------------------
# Agent card reflects the filter
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    __import__("sys").version_info < (3, 11),
    reason="a2a-sdk's Starlette integration requires Python 3.11+",
)
def test_agent_card_skills_shrink_under_override_filter():
    """The A2A agent card's ``skills`` list mirrors the gate's output.
    A minimal handler's card should advertise only the discovery tool
    set, not the full 57-tool surface. Under ``advertise_all=True`` the
    skill count returns to the full handler-type surface.
    """
    from adcp.server.a2a_server import _build_agent_card

    class _Minimal(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

    filtered_card = _build_agent_card(_Minimal(), name="minimal", port=0)
    filtered_skill_ids = {s.id for s in filtered_card.skills}
    assert filtered_skill_ids == {"get_adcp_capabilities"} | DISCOVERY_TOOLS

    full_card = _build_agent_card(_Minimal(), name="minimal", port=0, advertise_all=True)
    assert len(full_card.skills) >= 50
    assert filtered_skill_ids.issubset({s.id for s in full_card.skills})
