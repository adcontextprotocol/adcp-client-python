"""Header-driven test context + TestControllerStore composition — closes #227.

Downstream (salesagent) drives mock-ad-server behavior from request
headers via ``AdCPTestContext.from_headers(request.headers)``. The SDK's
:class:`~adcp.server.test_controller.TestControllerStore` is
storyboard-shaped — scenarios dispatch via the ``comply_test_controller``
skill. Before #227, there was no way to read HTTP headers inside a
``TestControllerStore`` method, so sellers who adopted the SDK's
storyboard testing lost their header-driven test scaffolding.

Fix: ``register_test_controller`` accepts the same ``context_factory``
as ``create_mcp_server``. The dispatcher builds a ``ToolContext`` per
call and threads it into store methods that declare a ``context``
keyword. Sellers populate test state in the factory (typically by
reading a ContextVar set by their HTTP middleware from request headers)
and read it off ``context.metadata`` inside their store.

Backward-compatibility contract: stores whose methods do NOT declare
``context`` MUST keep working — the dispatcher inspects the signature
and only passes ``context`` when the store opts in.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

import pytest

from adcp.server import (
    ADCPHandler,
    RequestMetadata,
    ToolContext,
    create_mcp_server,
)
from adcp.server.test_controller import (
    TestControllerStore,
    _accepts_context_kwarg,
    _handle_test_controller,
    register_test_controller,
)

# ---------------------------------------------------------------------------
# Minimal handler — TestControllerStore tests need an MCP server but not
# an interesting ADCPHandler.
# ---------------------------------------------------------------------------


class _MinimalHandler(ADCPHandler):
    _agent_type = "test"

    async def get_adcp_capabilities(self, params, context=None):
        return {"adcp": {"major_versions": [3]}}


# ---------------------------------------------------------------------------
# _accepts_context_kwarg — the signature-inspection helper
# ---------------------------------------------------------------------------


def test_accepts_context_kwarg_detects_keyword_only_param():
    """The documented opt-in pattern — ``*, context=None`` — must be
    detected."""

    async def fn(self, account_id: str, *, context: Any = None) -> dict[str, Any]:
        return {}

    assert _accepts_context_kwarg(fn) is True


def test_accepts_context_kwarg_detects_var_keyword():
    """Stores that catch arbitrary kwargs (``**kwargs``) count as
    accepting ``context`` — passing it won't raise TypeError."""

    async def fn(self, account_id: str, **kwargs: Any) -> dict[str, Any]:
        return {}

    assert _accepts_context_kwarg(fn) is True


def test_accepts_context_kwarg_rejects_methods_without_context():
    """Pre-#227 TestControllerStore overrides don't have ``context`` in
    their signature — the dispatcher must NOT pass it, or the call
    raises TypeError."""

    async def fn(self, account_id: str, status: str) -> dict[str, Any]:
        return {}

    assert _accepts_context_kwarg(fn) is False


def test_accepts_context_kwarg_rejects_positional_only_context():
    """``context`` before a ``/`` is positional-only — the dispatcher
    passes ``context=ctx`` by keyword, so a positional-only declaration
    would raise TypeError. Must be treated as not-opted-in."""
    import textwrap

    # positional-only syntax (PEP 570) works in Py 3.8+; use exec so
    # the file still parses cleanly without introducing a sigil.
    ns: dict[str, Any] = {}
    exec(
        textwrap.dedent(
            """
            async def fn(self, context, /, account_id, status):
                return {}
            """
        ),
        ns,
    )
    assert _accepts_context_kwarg(ns["fn"]) is False


def test_accepts_context_kwarg_follows_functools_wraps():
    """``inspect.signature`` follows ``__wrapped__``. A decorator using
    ``@functools.wraps`` exposes the wrapped signature — which is the
    authoritative contract. Verify the detection pipeline respects it
    so operators can reason about which methods opt in."""
    import functools

    async def legacy(self, account_id: str, status: str) -> dict[str, Any]:
        return {}

    @functools.wraps(legacy)
    async def wrapper(self, *args, **kwargs):
        return await legacy(self, *args, **kwargs)

    # Wrapper preserves the legacy signature — no context visible.
    assert _accepts_context_kwarg(wrapper) is False

    async def modern(self, account_id: str, status: str, *, context: Any = None) -> dict[str, Any]:
        return {}

    @functools.wraps(modern)
    async def modern_wrapper(self, *args, **kwargs):
        return await modern(self, *args, **kwargs)

    # Wrapped signature preserves the kwarg — opt-in survives.
    assert _accepts_context_kwarg(modern_wrapper) is True


def test_dispatcher_finds_override_on_intermediate_base():
    """A store may compose behavior across an inheritance chain
    (``MyStore(Mixin, TestControllerStore)``). The dispatcher must find
    the override wherever it lives in the MRO — and the context-kwarg
    detection must work on the bound method even when the implementing
    class is an intermediate base, not the leaf."""

    class _Mixin:
        async def force_media_buy_status(
            self,
            media_buy_id: str,
            status: str,
            rejection_reason: str | None = None,
            *,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            return {
                "previous_state": "active",
                "current_state": status,
                "from_mixin": True,
                "saw_context": context is not None,
            }

    class _Store(_Mixin, TestControllerStore):
        pass

    ctx = ToolContext(caller_identity="p-x", metadata={"test_context": {"x": 1}})
    import asyncio

    result = asyncio.run(
        _handle_test_controller(
            _Store(),
            {
                "scenario": "force_media_buy_status",
                "params": {"media_buy_id": "mb-1", "status": "paused"},
            },
            context=ctx,
        )
    )
    assert result["success"] is True
    assert result["from_mixin"] is True
    assert result["saw_context"] is True


# ---------------------------------------------------------------------------
# Dispatcher threads context into store methods that opt in
# ---------------------------------------------------------------------------


async def test_store_with_context_kwarg_receives_the_context():
    """The primary #227 scenario: a store method that accepts ``context``
    receives the ToolContext the caller passed into the dispatcher."""
    received: list[ToolContext | None] = []

    class _Store(TestControllerStore):
        async def force_account_status(
            self,
            account_id: str,
            status: str,
            *,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            received.append(context)
            return {"previous_state": "active", "current_state": status}

    ctx = ToolContext(
        caller_identity="p-1",
        tenant_id="t-1",
        metadata={"test_context": {"env": "ci", "slow_ad_server": True}},
    )
    result = await _handle_test_controller(
        _Store(),
        {
            "scenario": "force_account_status",
            "params": {"account_id": "acc-1", "status": "suspended"},
        },
        context=ctx,
    )

    assert result["success"] is True
    assert result["current_state"] == "suspended"
    assert len(received) == 1
    assert received[0] is ctx
    # The seller's header-driven state threads through verbatim.
    assert received[0].metadata["test_context"]["env"] == "ci"


async def test_legacy_store_without_context_kwarg_still_works():
    """Backward-compat contract. Stores written before #227 don't
    declare ``context``; the dispatcher must NOT pass it or the call
    raises TypeError. This test fails fast if signature detection
    regresses."""

    class _LegacyStore(TestControllerStore):
        # Original API shape — no context kwarg.
        async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
            return {"previous_state": "active", "current_state": status}

    ctx = ToolContext(caller_identity="p-legacy")
    result = await _handle_test_controller(
        _LegacyStore(),
        {
            "scenario": "force_account_status",
            "params": {"account_id": "acc-1", "status": "suspended"},
        },
        context=ctx,
    )

    assert result["success"] is True
    assert result["current_state"] == "suspended"


async def test_context_not_passed_when_none():
    """If the caller didn't supply a context (``context=None``), don't
    shove None into a store method that might not have the kwarg. The
    call should succeed without any context-related machinery firing."""

    class _Store(TestControllerStore):
        # Legacy-shape method — no context in signature.
        async def force_creative_status(
            self, creative_id: str, status: str, rejection_reason: str | None = None
        ) -> dict[str, Any]:
            return {"previous_state": "pending", "current_state": status}

    result = await _handle_test_controller(
        _Store(),
        {
            "scenario": "force_creative_status",
            "params": {"creative_id": "cr-1", "status": "approved"},
        },
        context=None,
    )

    assert result["success"] is True


# ---------------------------------------------------------------------------
# End-to-end: context_factory + TestControllerStore via FastMCP registration
# ---------------------------------------------------------------------------


async def test_register_test_controller_threads_context_factory():
    """Integration: ``register_test_controller`` with a
    ``context_factory`` matches the pattern sellers use to wire HTTP
    middleware → ContextVars → ToolContext. The factory is called per
    request; the store reads header-derived state off the context."""
    # A ContextVar the downstream HTTP middleware would populate from
    # request headers. This test simulates the middleware having already
    # run by setting the ContextVar directly.
    test_state_var: ContextVar[dict[str, Any] | None] = ContextVar("_test_state", default=None)
    received: list[ToolContext | None] = []

    class _Store(TestControllerStore):
        async def force_account_status(
            self,
            account_id: str,
            status: str,
            *,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            received.append(context)
            return {"previous_state": "active", "current_state": status}

    def build_context(meta: RequestMetadata) -> ToolContext:
        return ToolContext(
            metadata={
                "tool_name": meta.tool_name,
                "test_context": test_state_var.get(),
            },
        )

    mcp = create_mcp_server(_MinimalHandler(), name="test-agent")
    register_test_controller(mcp, _Store(), context_factory=build_context)

    # Simulate the HTTP middleware populating the ContextVar from headers.
    test_state_var.set({"env": "ci", "slow_ad_server": True})

    tool = mcp._tool_manager._tools["comply_test_controller"]
    # FastMCP's tool wrapper takes the function args as kwargs.
    fn = tool.fn  # type: ignore[attr-defined]
    result = await fn(
        scenario="force_account_status",
        params={"account_id": "acc-1", "status": "suspended"},
    )

    assert result["success"] is True
    assert result["current_state"] == "suspended"
    # The factory ran, built a ToolContext, and the store saw the header-
    # derived test state verbatim.
    assert len(received) == 1
    assert received[0] is not None
    assert received[0].metadata["test_context"] == {
        "env": "ci",
        "slow_ad_server": True,
    }
    # And the tool name was populated by RequestMetadata.
    assert received[0].metadata["tool_name"] == "comply_test_controller"


async def test_register_test_controller_list_scenarios_returns_dict():
    """Regression for #314 — comply_test_controller must return a dict (not a
    JSON string) through the FastMCP registration path so the JS runner's
    structuredContent unwrapper can read data.success and data.scenarios."""

    class _Store(TestControllerStore):
        async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
            return {"previous_state": "active", "current_state": status}

    mcp = create_mcp_server(_MinimalHandler(), name="test-agent")
    register_test_controller(mcp, _Store())

    tool = mcp._tool_manager._tools["comply_test_controller"]
    fn = tool.fn  # type: ignore[attr-defined]
    result = await fn(scenario="list_scenarios")

    assert isinstance(result, dict), "must be a dict, not a JSON string"
    assert result["success"] is True
    assert "force_account_status" in result["scenarios"]


async def test_register_test_controller_rejects_non_toolcontext_from_factory():
    """Guard rail — a factory that returns a dict instead of a
    ToolContext fails loudly at call time, not deep inside the store."""

    class _Store(TestControllerStore):
        async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
            return {"previous_state": "active", "current_state": status}

    def bad_factory(meta: RequestMetadata) -> Any:
        return {"not": "a ToolContext"}

    mcp = create_mcp_server(_MinimalHandler(), name="test-agent")
    register_test_controller(mcp, _Store(), context_factory=bad_factory)

    tool = mcp._tool_manager._tools["comply_test_controller"]
    fn = tool.fn  # type: ignore[attr-defined]

    with pytest.raises(TypeError, match="not a ToolContext"):
        await fn(
            scenario="force_account_status",
            params={"account_id": "acc-1", "status": "suspended"},
        )
