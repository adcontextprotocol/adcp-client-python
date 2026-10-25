"""A2A failures without an AdCP error envelope still echo the request ``context``.

An unknown skill and an unexpected handler exception produce a failed task
from ``ADCPAgentExecutor._send_error``. AdCP echoes ``context`` on every
response, errors included. These tests drive the real A2A server over HTTP
on both wire versions.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from starlette.testclient import TestClient

from adcp.server import ADCPHandler, create_a2a_server
from adcp.server.responses import capabilities_response

CONTEXT: dict[str, Any] = {"correlation_id": "buyer-req-1573", "trace": {"hop": "a"}}
REQUEST: dict[str, Any] = {"buying_mode": "brief", "brief": "premium video"}
_SECRET = "upstream-said-something-private"


class _CrashingHandler(ADCPHandler[Any]):
    advertised_tools: ClassVar[set[str]] = {"get_adcp_capabilities", "get_products"}

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return capabilities_response(["media_buy"])

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        raise RuntimeError(_SECRET)


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    app = create_a2a_server(_CrashingHandler(), name="a2a-errors", allowed_hosts=["testserver"])
    with TestClient(app) as test_client:
        yield test_client


def _send(client: TestClient, wire: str, skill: str, parameters: dict[str, Any]) -> dict[str, Any]:
    invocation = {"skill": skill, "parameters": parameters}
    if wire == "v0.3":
        headers: dict[str, str] = {}
        method, role = "message/send", "user"
        part: dict[str, Any] = {"kind": "data", "data": invocation}
    else:
        headers = {"A2A-Version": "1.0"}
        method, role = "SendMessage", "ROLE_USER"
        part = {"data": invocation}
    response = client.post(
        "/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                "message": {"messageId": f"m-{wire}-{skill}", "role": role, "parts": [part]}
            },
        },
    )
    assert response.status_code == 200, response.text
    assert _SECRET not in response.text
    result = response.json()["result"]
    task: dict[str, Any] = result.get("task", result)
    assert task["status"]["state"] in {"failed", "TASK_STATE_FAILED"}, task
    return task


def _parts(task: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    parts = task["artifacts"][0]["parts"]
    return [p["data"] for p in parts if "data" in p], [p["text"] for p in parts if "text" in p]


WIRES = pytest.mark.parametrize("wire", ["v0.3", "v1.0"])


@WIRES
def test_unknown_skill_echoes_context(client: TestClient, wire: str) -> None:
    data, text = _parts(_send(client, wire, "no_such_skill", {**REQUEST, "context": CONTEXT}))
    assert data == [{"context": CONTEXT}]
    assert text == ["Unknown skill: no_such_skill"]


@WIRES
def test_unexpected_exception_echoes_context(client: TestClient, wire: str) -> None:
    data, text = _parts(_send(client, wire, "get_products", {**REQUEST, "context": CONTEXT}))
    assert data == [{"context": CONTEXT}]
    assert text == ["Skill execution failed: get_products"]


@WIRES
@pytest.mark.parametrize("skill", ["no_such_skill", "get_products"])
def test_failure_without_context_keeps_the_text_only_task(
    client: TestClient, wire: str, skill: str
) -> None:
    data, text = _parts(_send(client, wire, skill, REQUEST))
    assert data == []
    assert len(text) == 1
