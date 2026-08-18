"""Round-trip coverage for the update_rights task.

Spec-coverage suite proves the method exists; this file proves it actually
serializes the request correctly, reaches the adapter, and parses the
response through the Union-typed UpdateRightsResponse.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol, UpdateRightsRequest, UpdateRightsResponse
from tests.a2a_compat_shim import (
    Artifact,
    DataPart,
    Part,
    SendMessageSuccessResponse,
    Task,
    part_data_dict,
)
from tests.a2a_compat_shim import (
    TaskStatus as A2ATaskStatus,
)


def _cfg(protocol: Protocol = Protocol.A2A) -> AgentConfig:
    return AgentConfig(id="t", agent_uri="https://example.test", protocol=protocol)


def _task_with_data(data: dict[str, Any]) -> Task:
    return Task(
        id=f"task_{uuid.uuid4().hex[:8]}",
        context_id=f"ctx_{uuid.uuid4().hex[:8]}",
        status=A2ATaskStatus(state="completed"),
        artifacts=[Artifact(artifact_id="a1", parts=[Part(root=DataPart(data=data))])],
    )


def _full_terms(**overrides: Any) -> dict[str, Any]:
    """A valid RightsTerms payload; overrides merge in per-test fields."""
    base: dict[str, Any] = {
        "pricing_option_id": "po_standard",
        "amount": 10000.0,
        "currency": "USD",
        "uses": ["endorsement"],
    }
    base.update(overrides)
    return base


def _rights_constraint(rights_id: str) -> dict[str, Any]:
    """A minimally complete AdCP 3.2 attested rights constraint."""
    agent_url = "https://rights.example/adcp"
    holder = {"domain": "brand.example"}
    digest = f"sha256:{'0' * 64}"
    return {
        "rights_id": rights_id,
        "rights_agent": {"url": agent_url, "id": "rights-agent"},
        "rights_holder": holder,
        "uses": ["endorsement"],
        "grant_status": "active",
        "content_digest": digest,
        "attestation_refs": [
            {
                "issuer": {"type": "brand", "brand": holder},
                "claim_type": "https://adcontextprotocol.org/claims/rights/grant",
                "subject": {
                    "type": "resource",
                    "resource_type": ("https://adcontextprotocol.org/claims/subjects/rights-grant"),
                    "namespace": agent_url,
                    "id": rights_id,
                    "content_digest": digest,
                },
                "locator": {
                    "type": "issuer_credential_id",
                    "credential_id": f"credential-{rights_id}",
                    "resolver_id": "primary",
                },
            }
        ],
    }


def _success_response(rights_id: str, **overrides: Any) -> dict[str, Any]:
    """A complete AdCP 3.2 update_rights success response."""
    response: dict[str, Any] = {
        "rights_id": rights_id,
        "terms": _full_terms(),
        "generation_credentials": [],
        "rights_constraint": _rights_constraint(rights_id),
    }
    response.update(overrides)
    return response


class TestUpdateRightsA2A:
    @pytest.mark.asyncio
    async def test_partial_update_reaches_wire(self) -> None:
        """Only the mutated fields travel to the adapter; rights_id is required."""
        client = ADCPClient(_cfg())
        req = UpdateRightsRequest.model_validate(
            {
                "idempotency_key": str(uuid.uuid4()),
                "rights_id": "rts_live_01",
                "end_date": "2026-12-31",
            }
        )
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(
                result=_task_with_data(
                    _success_response("rts_live_01", terms=_full_terms(end_date="2026-12-31"))
                )
            )
        )
        with patch.object(client.adapter, "_get_a2a_client", return_value=mock_client):
            result = await client.update_rights(req)

        sent = mock_client.send_message.call_args[0][0]
        parts = sent.message.parts
        data = next(part_data_dict(p) for p in parts if p.WhichOneof("content") == "data")
        assert data["skill"] == "update_rights"
        params = data["parameters"]
        assert params["rights_id"] == "rts_live_01"
        assert params["end_date"] == "2026-12-31"
        # Partial update: no impression_cap or paused in the payload
        assert "impression_cap" not in params
        assert "paused" not in params
        # idempotency_key survives round-trip
        assert params["idempotency_key"] == req.idempotency_key

        assert result.success is True
        assert result.idempotency_key == req.idempotency_key

    @pytest.mark.asyncio
    async def test_response_parses_as_union_type(self) -> None:
        """UpdateRightsResponse is UpdateRightsResponse1 | UpdateRightsResponse2
        — the client's _parse_response must dispatch through the Union."""
        client = ADCPClient(_cfg())
        req = UpdateRightsRequest.model_validate(
            {
                "idempotency_key": str(uuid.uuid4()),
                "rights_id": "rts_live_02",
                "paused": True,
            }
        )
        # Response shape: successful update.
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(
                result=_task_with_data(_success_response("rts_live_02", paused=True))
            )
        )
        with patch.object(client.adapter, "_get_a2a_client", return_value=mock_client):
            result = await client.update_rights(req)

        assert result.success is True
        assert result.data is not None
        # Data is a concrete variant of the Union (UpdateRightsResponse1 or 2).
        import typing as _t

        union_args = set(_t.get_args(UpdateRightsResponse))
        assert type(result.data) in union_args or isinstance(result.data, tuple(union_args))


class TestUpdateRightsMCP:
    @pytest.mark.asyncio
    async def test_partial_update_reaches_mcp_call_tool(self) -> None:
        from adcp.protocols.mcp import MCPAdapter

        adapter = MCPAdapter(_cfg(Protocol.MCP))
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = []
        mock_result.structuredContent = {
            "rights_id": "rts_mcp_01",
            "status": "updated",
        }
        session.call_tool = AsyncMock(return_value=mock_result)
        with patch.object(adapter, "_get_session", AsyncMock(return_value=session)):
            params = {
                "idempotency_key": str(uuid.uuid4()),
                "rights_id": "rts_mcp_01",
                "impression_cap": 5_000_000,
            }
            result = await adapter._call_mcp_tool("update_rights", params)

        sent_name, sent_params = session.call_tool.call_args[0]
        assert sent_name == "update_rights"
        assert sent_params["impression_cap"] == 5_000_000
        assert result.replayed is False


class TestHandlerSurface:
    def test_handler_default_stub_returns_not_supported(self) -> None:
        """Default ADCPHandler.update_rights returns the not-supported response
        — sellers who don't override it get a clear signal, not a crash."""
        import asyncio

        from adcp.server.base import ADCPHandler, NotImplementedResponse

        class Bare(ADCPHandler):
            pass

        resp = asyncio.run(Bare().update_rights({"rights_id": "x"}))
        assert isinstance(resp, NotImplementedResponse)
        assert resp.supported is False
