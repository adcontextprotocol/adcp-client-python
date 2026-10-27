"""Client-side integration tests for schema-driven validation (issue #249).

Exercises the wiring between :class:`adcp.ADCPClient`, the protocol
adapter, and the validation hook. The adapter's external transport is
mocked so these stay fast and deterministic; the validator itself runs
unmocked against the real bundled schemas.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adcp import ADCPClient, ValidationHookConfig
from adcp.types import AgentConfig, Protocol


def _agent_config() -> AgentConfig:
    return AgentConfig(
        id="test_agent",
        agent_uri="https://test.example.com",
        protocol=Protocol.MCP,
    )


class TestConfigPropagation:
    def test_defaults_apply_when_omitted(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            client = ADCPClient(_agent_config())
        assert client.adapter.request_validation_mode == "warn"
        assert client.adapter.response_validation_mode == "strict"

    def test_production_env_flips_responses_to_warn(self) -> None:
        with patch.dict(os.environ, {"ADCP_ENV": "production"}, clear=True):
            client = ADCPClient(_agent_config())
        assert client.adapter.response_validation_mode == "warn"

    def test_explicit_config_wins(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            client = ADCPClient(
                _agent_config(),
                validation=ValidationHookConfig(requests="strict", responses="off"),
            )
        assert client.adapter.request_validation_mode == "strict"
        assert client.adapter.response_validation_mode == "off"


class TestMCPAdapterHooks:
    @pytest.mark.asyncio
    async def test_strict_request_blocks_call_before_transport(self) -> None:
        client = ADCPClient(
            _agent_config(),
            server_version="3.2",
            validation=ValidationHookConfig(requests="strict"),
        )
        session = MagicMock()
        session.call_tool = AsyncMock()

        with patch.object(client.adapter, "_get_session", AsyncMock(return_value=session)):
            result = await client.adapter._call_mcp_tool("get_products", {})

        assert result.success is False
        assert "VALIDATION_ERROR" in (result.error or "") or "failed schema validation" in (
            result.error or ""
        )
        session.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_strict_response_fails_task_on_drift(self) -> None:
        client = ADCPClient(
            _agent_config(),
            server_version="3.2",
            validation=ValidationHookConfig(requests="off", responses="strict"),
        )

        # Fake MCP tool result: drift — products should be an array, not a string.
        fake_result = MagicMock()
        fake_result.isError = False
        fake_result.structuredContent = {"products": "oops"}
        fake_result.content = []

        session = MagicMock()
        session.call_tool = AsyncMock(return_value=fake_result)

        with patch.object(client.adapter, "_get_session", AsyncMock(return_value=session)):
            result = await client.adapter._call_mcp_tool(
                "get_products",
                {
                    "brief": "b",
                    "promoted_offering": "shoes",
                    "buying_mode": "brief",
                },
            )

        assert result.success is False
        assert "Schema validation failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_off_mode_skips_validator(self) -> None:
        client = ADCPClient(
            _agent_config(),
            server_version="3.2",
            validation=ValidationHookConfig(requests="off", responses="off"),
        )

        # Drift response — in off mode it should pass through unchanged.
        fake_result = MagicMock()
        fake_result.isError = False
        fake_result.structuredContent = {"products": "oops"}
        fake_result.content = []

        session = MagicMock()
        session.call_tool = AsyncMock(return_value=fake_result)

        with patch.object(client.adapter, "_get_session", AsyncMock(return_value=session)):
            result = await client.adapter._call_mcp_tool("get_products", {})

        assert result.success is True
        assert result.data == {"products": "oops"}
