"""``serve(validation=...)`` plumbing — request/response schema enforcement
should reach the tool caller no matter which transport an adopter
selected.

Pre-fix the v3 reference seller called ``serve(...)`` without a
``validation=`` kwarg; the underlying tool-caller factory defaulted to
no enforcement, so spec-divergent responses (the original
``pricing_options`` regression) flowed through silently. This test
pins the wiring: passing ``validation=ValidationHookConfig(...)`` on
``serve()`` reaches every tool caller the framework constructs.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import patch

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.serve import create_mcp_server
from adcp.validation import ValidationHookConfig

# The package re-exports ``adcp.server.serve`` as the function symbol,
# so the module itself is not reachable as ``adcp.server.serve``.
# Resolve the underlying module via importlib for monkey-patching.
_serve_mod = importlib.import_module("adcp.server.serve")


class _StubHandler(ADCPHandler[Any]):
    """Always-succeeds handler returning a fixed payload."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    async def get_products(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return dict(self._response)


def test_create_mcp_server_threads_validation_to_tool_caller() -> None:
    """``create_mcp_server(validation=...)`` must reach
    :func:`create_tool_caller`. Without this, ``serve(validation=...)``
    is silently a no-op on the MCP transport.
    """
    handler = _StubHandler({"products": []})
    captured: list[ValidationHookConfig | None] = []
    real_factory = _serve_mod.create_tool_caller
    config = ValidationHookConfig(requests="strict", responses="strict")

    def _spy(handler_arg: Any, method: str, **kwargs: Any) -> Any:
        captured.append(kwargs.get("validation"))
        return real_factory(handler_arg, method, **kwargs)

    with patch.object(_serve_mod, "create_tool_caller", side_effect=_spy):
        create_mcp_server(handler, validation=config)

    assert captured, "create_tool_caller was never called"
    assert all(
        c is config for c in captured
    ), f"validation kwarg dropped on the way to create_tool_caller: {captured!r}"


def test_serve_validation_default_is_none() -> None:
    """When ``serve(validation=)`` is omitted, tool callers see
    ``validation=None`` — i.e., off (zero overhead). Confirms the
    plumbing doesn't silently force a default mode.
    """
    handler = _StubHandler({"products": []})
    captured: list[ValidationHookConfig | None] = []
    real_factory = _serve_mod.create_tool_caller

    def _spy(handler_arg: Any, method: str, **kwargs: Any) -> Any:
        captured.append(kwargs.get("validation"))
        return real_factory(handler_arg, method, **kwargs)

    with patch.object(_serve_mod, "create_tool_caller", side_effect=_spy):
        create_mcp_server(handler)

    assert captured, "create_tool_caller was never called"
    assert all(
        c is None for c in captured
    ), f"validation default leaked a non-None config: {captured!r}"


@pytest.mark.asyncio
async def test_create_tool_caller_strict_blocks_bad_request() -> None:
    """Smoke: a tool caller built with ``validation=strict`` rejects a
    malformed request before dispatch — the contract every transport
    leans on.
    """
    from adcp.server.mcp_tools import create_tool_caller

    handler = _StubHandler({"products": []})
    caller = create_tool_caller(
        handler,
        "get_products",
        validation=ValidationHookConfig(requests="strict"),
    )
    with pytest.raises(ADCPTaskError) as info:
        await caller({})  # missing required ``promoted_offering``
    assert info.value.errors[0].code == "VALIDATION_ERROR"
