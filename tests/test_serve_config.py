"""Tests for ServeConfig dataclass and its integration with serve().

ServeConfig provides a bundled alternative to passing 22 individual kwargs
to serve(). When config= is supplied, values come from the dataclass;
when it's absent, individual kwargs work as before.
"""

from __future__ import annotations

import dataclasses
import importlib
import warnings
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from adcp.server import ServeConfig
from adcp.server.base import ADCPHandler, ToolContext

_serve_mod = importlib.import_module("adcp.server.serve")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubHandler(ADCPHandler[Any]):
    async def get_products(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {"products": []}


# ---------------------------------------------------------------------------
# ServeConfig basic construction
# ---------------------------------------------------------------------------


def test_serve_config_defaults() -> None:
    cfg = ServeConfig()
    assert cfg.name == "adcp-agent"
    assert cfg.transport == "streamable-http"
    assert cfg.port is None
    assert cfg.host is None
    assert cfg.advertise_all is False
    assert cfg.streaming_responses is False
    assert cfg.enable_debug_endpoints is False
    assert cfg.middleware is None
    assert cfg.validation is None


def test_serve_config_frozen() -> None:
    cfg = ServeConfig(name="my-agent")
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
        cfg.name = "other"  # type: ignore[misc]


def test_serve_config_replace() -> None:
    base = ServeConfig(name="base", transport="a2a")
    updated = dataclasses.replace(base, name="updated")
    assert updated.name == "updated"
    assert updated.transport == "a2a"


def test_serve_config_exportable_from_adcp_server() -> None:
    """ServeConfig must be importable from the public adcp.server namespace."""
    import adcp.server as _server

    assert _server.ServeConfig is ServeConfig


# ---------------------------------------------------------------------------
# ServeConfig transport-field warnings
# ---------------------------------------------------------------------------


def test_serve_config_warns_a2a_only_on_mcp_transport() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ServeConfig(transport="streamable-http", task_store=MagicMock())
    messages = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any("A2A-only" in m for m in messages), messages


def test_serve_config_warns_mcp_only_on_a2a_transport() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ServeConfig(transport="a2a", instructions="hello")
    messages = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any("MCP-only" in m for m in messages), messages


def test_serve_config_no_warning_on_both_transport() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ServeConfig(transport="both", task_store=MagicMock(), instructions="hi")
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert not user_warnings, "No warning expected for transport='both'"


def test_serve_config_no_warning_clean_config() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ServeConfig(name="my-agent", transport="a2a")
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert not user_warnings


# ---------------------------------------------------------------------------
# serve() respects config= over default kwargs
# ---------------------------------------------------------------------------


def _patch_serve_internals(mock_fn_name: str = "_serve_mcp") -> Any:
    """Return a patch context for the internal serve dispatch function."""
    return patch.object(_serve_mod, mock_fn_name, return_value=None)


def test_serve_config_name_propagates() -> None:
    handler = _StubHandler()
    cfg = ServeConfig(name="from-config", transport="streamable-http", port=9999)

    with patch.object(_serve_mod, "_serve_mcp") as mock_mcp:
        _serve_mod.serve(handler, config=cfg)

    mock_mcp.assert_called_once()
    _, kwargs = mock_mcp.call_args
    assert kwargs.get("name") == "from-config"


def test_serve_config_kwargs_ignored_when_config_provided() -> None:
    """When config= is supplied, individual kwargs must be ignored."""
    handler = _StubHandler()
    cfg = ServeConfig(name="from-config", transport="streamable-http", port=9999)

    with patch.object(_serve_mod, "_serve_mcp") as mock_mcp:
        # Pass a contradicting name kwarg — config should win
        _serve_mod.serve(handler, config=cfg, name="ignored-name")

    mock_mcp.assert_called_once()
    _, kwargs = mock_mcp.call_args
    assert kwargs.get("name") == "from-config", (
        "config.name should override the per-kwarg name when config= is provided"
    )


def test_serve_without_config_uses_kwargs() -> None:
    """Without config=, individual kwargs must still reach the transport."""
    handler = _StubHandler()

    with patch.object(_serve_mod, "_serve_mcp") as mock_mcp:
        _serve_mod.serve(handler, name="kwarg-name", transport="streamable-http")

    mock_mcp.assert_called_once()
    _, kwargs = mock_mcp.call_args
    assert kwargs.get("name") == "kwarg-name"


def test_serve_config_advertise_all_propagates() -> None:
    handler = _StubHandler()
    cfg = ServeConfig(transport="streamable-http", advertise_all=True)

    with patch.object(_serve_mod, "_serve_mcp") as mock_mcp:
        _serve_mod.serve(handler, config=cfg)

    mock_mcp.assert_called_once()
    _, kwargs = mock_mcp.call_args
    assert kwargs.get("advertise_all") is True
