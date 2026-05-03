"""Wire-level conformance test for A2A Task.artifacts population.

Drives the full A2A Starlette app through an ASGI transport, sends a raw
JSON-RPC `message/send` for each `comply_test_controller` scenario shape, and
asserts the returned JSON has a populated ``result.artifacts`` list with the
tool payload in a ``DataPart``.

Guards issue #211: an external storyboard validator reported "A2A response
missing result.artifacts field" on seller runs. The SDK path verified here is
the one that backs ``scripts/skill-run.sh seller ... media_buy_seller`` — if
this test ever goes red, the same warning will fire on those runs.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest

from adcp.server import ADCPHandler
from adcp.server.a2a_server import create_a2a_server
from adcp.server.test_controller import TestControllerError, TestControllerStore

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)


@pytest.fixture(autouse=True)
def _admit_sandbox_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2A conformance tests cover wire-shape contracts, not the
    sandbox-authority gate. Set the legacy env opt-in so the gate
    admits without requiring per-call resolver wiring."""
    monkeypatch.setenv("ADCP_SANDBOX", "1")


class _MinimalSeller(ADCPHandler):
    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}}


class _Store(TestControllerStore):
    async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
        if account_id == "missing":
            raise TestControllerError("NOT_FOUND", f"Account {account_id} not found")
        return {"previous_state": "active", "current_state": status}


async def _send(client: httpx.AsyncClient, scenario_payload: dict[str, Any]) -> dict[str, Any]:
    """POST a JSON-RPC ``message/send`` for comply_test_controller."""
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [
                    {
                        "kind": "data",
                        "data": {
                            "skill": "comply_test_controller",
                            "parameters": scenario_payload,
                        },
                    }
                ],
            }
        },
    }
    resp = await client.post("/", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _assert_artifact_carries(body: dict[str, Any], expected_keys: set[str]) -> dict[str, Any]:
    """Verify ``result.artifacts[].parts[].data`` carries the tool payload."""
    assert "result" in body, f"JSON-RPC response missing result: {body}"
    result = body["result"]

    artifacts = result.get("artifacts")
    assert artifacts, (
        "A2A Task missing result.artifacts — storyboard validators that "
        "strictly require Task.artifacts will reject this response. "
        f"Full result: {result}"
    )
    assert isinstance(artifacts, list) and len(artifacts) >= 1

    parts = artifacts[-1].get("parts") or []
    data_parts = [p for p in parts if p.get("kind") == "data"]
    assert data_parts, f"Artifact missing DataPart (kind='data'): {parts}"

    data = data_parts[-1].get("data")
    assert isinstance(data, dict), f"DataPart.data must be an object: {data!r}"
    assert expected_keys.issubset(
        data.keys()
    ), f"DataPart.data missing expected keys {expected_keys - data.keys()}: {data}"
    return data


async def _bootstrap_client() -> tuple[httpx.AsyncClient, Any]:
    app = create_a2a_server(_MinimalSeller(), name="conformance-seller", test_controller=_Store())
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, app


async def test_state_transition_populates_artifacts() -> None:
    """force_* scenario → StateTransitionSuccess shape under result.artifacts."""
    client, _ = await _bootstrap_client()
    async with client:
        body = await _send(
            client,
            {
                "scenario": "force_account_status",
                "params": {"account_id": "a1", "status": "suspended"},
            },
        )
        data = _assert_artifact_carries(body, {"success", "previous_state", "current_state"})
        assert data["success"] is True
        assert data["current_state"] == "suspended"
        assert body["result"]["status"]["state"] == "completed"


async def test_list_scenarios_populates_artifacts() -> None:
    """list_scenarios → ListScenariosSuccess shape under result.artifacts."""
    client, _ = await _bootstrap_client()
    async with client:
        body = await _send(client, {"scenario": "list_scenarios"})
        data = _assert_artifact_carries(body, {"success", "scenarios"})
        assert data["success"] is True
        assert "force_account_status" in data["scenarios"]


async def test_controller_error_populates_artifacts() -> None:
    """TestControllerError → ControllerError shape still lands in artifacts.

    The AdCP comply_test_controller contract treats application errors
    (NOT_FOUND, INVALID_TRANSITION, UNKNOWN_SCENARIO, ...) as successful
    Tasks whose DataPart carries ``success: false``. The Task state stays
    ``completed`` — the error lives in the payload, not the transport.
    """
    client, _ = await _bootstrap_client()
    async with client:
        body = await _send(
            client,
            {
                "scenario": "force_account_status",
                "params": {"account_id": "missing", "status": "suspended"},
            },
        )
        data = _assert_artifact_carries(body, {"success", "error"})
        assert data["success"] is False
        assert data["error"] == "NOT_FOUND"
        assert body["result"]["status"]["state"] == "completed"


async def test_unknown_scenario_populates_artifacts() -> None:
    """Unsupported scenario → ControllerError(UNKNOWN_SCENARIO) in artifacts."""
    client, _ = await _bootstrap_client()
    async with client:
        body = await _send(
            client,
            {"scenario": "simulate_delivery", "params": {"media_buy_id": "x"}},
        )
        data = _assert_artifact_carries(body, {"success", "error"})
        assert data["success"] is False
        assert data["error"] == "UNKNOWN_SCENARIO"
