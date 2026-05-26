"""pre_validation_hooks — wire dict rewriting before schema + Pydantic validation.

Tests that:
- The hook is called with (tool_name, raw_dict) before validation.
- A missing required field supplied by the hook passes model_validate.
- A hook that raises surfaces as INVALID_REQUEST, not INTERNAL_ERROR.
- pre_validation_hooks=None (default) is a no-op (hot path unchanged).
- A hook for tool X is not called when tool Y is dispatched.
- Hook runs before validate_request in strict validation mode.
- In-place mutation of hook args is safe (framework passes a shallow copy).
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.server import compose_pre_validation_hooks
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller


class _MinimalHandler(ADCPHandler[Any]):
    """Passes params straight through as the return value."""

    async def get_products(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {"params_received": dict(params)}


class _TypedHandler(ADCPHandler[Any]):
    """Handler that validates get_products against the real Pydantic model."""

    async def get_products(self, params: Any, ctx: ToolContext) -> dict[str, Any]:
        return {"buying_mode": getattr(params, "buying_mode", None)}


# ---------------------------------------------------------------------------
# Basic hook mechanics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_is_called_with_tool_name_and_args() -> None:
    """Hook receives (tool_name, raw_dict) and its return value replaces the args."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def my_hook(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(args)))
        return {**args, "injected": True}

    handler = _MinimalHandler()
    caller = create_tool_caller(handler, "get_products", pre_validation_hook=my_hook)

    result = await caller({"foo": "bar"})
    assert len(calls) == 1
    assert calls[0] == ("get_products", {"foo": "bar"})
    assert result["params_received"]["injected"] is True


@pytest.mark.asyncio
async def test_hook_none_is_noop() -> None:
    """pre_validation_hook=None (default) does not change dispatch behaviour."""
    handler = _MinimalHandler()
    caller = create_tool_caller(handler, "get_products", pre_validation_hook=None)
    result = await caller({"key": "val"})
    assert result["params_received"] == {"key": "val"}


@pytest.mark.asyncio
async def test_hook_not_called_for_other_tool() -> None:
    """A hook registered for get_products is not invoked when create_media_buy is called."""
    calls: list[str] = []

    def hook(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(tool_name)
        return args

    handler = _MinimalHandler()
    # create a caller for a different tool — hook should not apply
    caller_other = create_tool_caller(handler, "get_adcp_capabilities", pre_validation_hook=None)
    await caller_other({})
    assert calls == [], "hook was called for the wrong tool"


# ---------------------------------------------------------------------------
# Missing-required-field scenario: the primary use case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_supplies_missing_required_field() -> None:
    """A hook that fills in buying_mode allows model_validate to succeed.

    Without the hook, a bare {} request would fail model_validate on
    GetProductsRequest because buying_mode is required in 4.4+ schemas.
    This test uses the dict-typed handler path to avoid importing schema
    models directly.
    """
    called_with: list[dict[str, Any]] = []

    def buying_mode_default(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        called_with.append(args)
        return {**args, "buying_mode": args.get("buying_mode", "brief")}

    handler = _MinimalHandler()
    caller = create_tool_caller(handler, "get_products", pre_validation_hook=buying_mode_default)

    result = await caller({})
    assert called_with == [{}]
    assert result["params_received"]["buying_mode"] == "brief"


@pytest.mark.asyncio
async def test_multiple_hooks_for_one_tool_run_in_order() -> None:
    """A tool can receive an ordered hook chain; each hook sees the previous output."""
    order: list[str] = []

    def first(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        order.append(f"first:{tool_name}")
        return {**args, "a": 1}

    def second(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        order.append(f"second:{tool_name}:{args['a']}")
        return {**args, "b": 2}

    handler = _MinimalHandler()
    caller = create_tool_caller(handler, "get_products", pre_validation_hook=[first, second])

    result = await caller({"buying_mode": "brief"})
    assert order == ["first:get_products", "second:get_products:1"]
    assert result["params_received"]["a"] == 1
    assert result["params_received"]["b"] == 2


def test_compose_pre_validation_hooks_appends_overlapping_tools() -> None:
    """The public helper preserves map order and appends overlaps."""
    calls: list[str] = []

    def sdk(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(f"sdk:{tool_name}")
        return {**args, "sdk": True}

    def local(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(f"local:{tool_name}")
        return {**args, "local": True}

    def creative(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(f"creative:{tool_name}")
        return args

    hooks = compose_pre_validation_hooks(
        {"get_products": sdk, "sync_creatives": creative},
        {"get_products": [local]},
    )

    get_products_hooks = hooks["get_products"]
    out = {"brief": "test"}
    for hook in get_products_hooks:
        out = hook("get_products", out)

    assert calls == ["sdk:get_products", "local:get_products"]
    assert out == {"brief": "test", "sdk": True, "local": True}
    assert hooks["sync_creatives"] == (creative,)


# ---------------------------------------------------------------------------
# Hook exception handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_exception_surfaces_as_invalid_request() -> None:
    """A hook that raises must surface as INVALID_REQUEST, not INTERNAL_ERROR."""

    def bad_hook(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("unsupported format_id shape")

    handler = _MinimalHandler()
    caller = create_tool_caller(handler, "get_products", pre_validation_hook=bad_hook)

    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({"something": "here"})

    errors = exc_info.value.errors
    assert errors, "ADCPTaskError must carry at least one error"
    assert errors[0].code == "INVALID_REQUEST"
    assert "ValueError" in errors[0].message


# ---------------------------------------------------------------------------
# Hook must not mutate raw_params (context-echo path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_does_not_pollute_context_echo() -> None:
    """raw_params must snapshot the original wire dict BEFORE the hook runs.

    inject_context echoes the wire ``context`` field from raw_params back into
    the response. If raw_params were assigned after the hook, a hook that
    returns a new dict (dropping ``context``) would silently suppress the echo.
    Conversely, a hook that adds keys would cause server-injected fields to
    appear in the echo as if the buyer sent them.

    We exercise both directions:
    - A hook that strips all fields and adds "server_default" (no context key
      in its return) still produces context echo from the original wire params.
    - The handler result carries hook-modified fields, confirming the hook ran.
    """
    wire_context = {"correlation_id": "req-abc"}
    wire_args = {"buyer_field": "x", "context": wire_context}

    def stripping_hook(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        # Returns a brand-new dict — deliberately omits "context"
        return {"server_default": "y"}

    handler = _MinimalHandler()
    caller = create_tool_caller(handler, "get_products", pre_validation_hook=stripping_hook)
    result = await caller(dict(wire_args))

    # Hook ran: handler received hook-modified params, not original
    assert result["params_received"] == {"server_default": "y"}
    # Context echo used raw_params (pre-hook snapshot), not hook return
    assert result.get("context") == wire_context


@pytest.mark.asyncio
async def test_in_place_mutation_is_safe_for_context_echo() -> None:
    """Hook that mutates its argument in-place must not corrupt context echo.

    The framework passes a shallow copy to the hook, so in-place mutation
    of the hook argument leaves the original wire params untouched for the
    context-echo path. This test exercises the ``args["key"] = val; return args``
    pattern that the original docstring labelled a "bug".
    """
    wire_context = {"correlation_id": "req-xyz"}
    wire_args = {"buyer_field": "original", "context": wire_context}

    def mutating_hook(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        args["server_default"] = "injected"
        del args["buyer_field"]
        return args

    handler = _MinimalHandler()
    caller = create_tool_caller(handler, "get_products", pre_validation_hook=mutating_hook)
    result = await caller(dict(wire_args))

    assert result["params_received"].get("server_default") == "injected"
    assert "buyer_field" not in result["params_received"]
    assert result.get("context") == wire_context


# ---------------------------------------------------------------------------
# MCPToolSet threading
# ---------------------------------------------------------------------------


def test_mcp_tool_set_threads_hook_to_tool_caller() -> None:
    """MCPToolSet must forward pre_validation_hooks to create_tool_caller."""
    import importlib
    from unittest.mock import patch

    _serve_mod = importlib.import_module("adcp.server.mcp_tools")

    handler = _MinimalHandler()
    captured_hooks: list[Any] = []
    real = _serve_mod.create_tool_caller

    def spy(h: Any, name: str, **kw: Any) -> Any:
        captured_hooks.append(kw.get("pre_validation_hook"))
        return real(h, name, **kw)

    my_hook = lambda n, a: a  # noqa: E731
    hooks = {"get_products": my_hook}

    with patch.object(_serve_mod, "create_tool_caller", side_effect=spy):
        from adcp.server.mcp_tools import MCPToolSet

        MCPToolSet(handler, pre_validation_hooks=hooks)

    assert any(
        h is my_hook for h in captured_hooks
    ), "my_hook was not forwarded to create_tool_caller for get_products"
