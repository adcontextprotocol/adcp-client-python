"""A2A invocation member rule: ``input`` vs ``parameters`` (adcp-client-python#1887).

The AdCP A2A profile sends ``{"skill": ..., "input": {...}}``; the legacy
shape sends ``parameters``. The default parser reads ``parameters`` when
present, otherwise ``input``, and never merges them. A request that activates
the profile (``A2A-Extensions`` header or ``message.extensions``) should carry
exactly one DataPart with an object ``input`` and no ``parameters``.

9.x (this line) is non-breaking: it refuses only the both-members case, and
dispatches the other non-conforming activated shapes (``parameters`` only,
``input`` missing or not an object, a DataPart count other than one) with a
deprecation warning. 10.x refuses all of them.

Everything is driven over HTTP through the real app, not mocked parsers.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from adcp.reporting.receipts.transport import a2a_receipt_parameters
from adcp.reporting.receipts.wire import TASK as RECEIPT_TASK
from adcp.server import ADCPHandler, RequestSignatureVerification
from adcp.signing import StaticSignerKeys

PROFILE = "https://adcontextprotocol.org/extensions/adcp/v3"
ARGS: dict[str, Any] = {"brief": "ctv", "adcp_version": "3.2", "context": {"trace": "t-1"}}

V1 = {"content-type": "application/json", "A2A-Version": "1.0"}
V1_ACTIVE = {**V1, "A2A-Extensions": PROFILE}
V03 = {"content-type": "application/json"}
PROFILE_REFUSAL = "must carry exactly one DataPart with an `input` object"


class _InputMemberSeller(ADCPHandler[Any]):
    advertised_tools: set[str] = {"get_adcp_capabilities", "get_products"}

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        if not isinstance(params, dict):
            params = params.model_dump(exclude_none=True)
        self.seen.append(params)
        return {"products": []}


def _app(handler: ADCPHandler[Any], **kwargs: Any) -> Any:
    from adcp.server.serve import _build_mcp_and_a2a_app

    return _build_mcp_and_a2a_app(
        handler,
        name="test-agent",
        port=0,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        advertise_all=False,
        stateless_http=True,
        allowed_hosts=["localhost"],
        auth=None,
        **kwargs,
    )


@contextlib.asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            yield client


def _v1(data: dict[str, Any], **message: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "m",
                "role": "ROLE_USER",
                "parts": [{"data": data}],
                **message,
            }
        },
    }


def _v03(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m",
                "role": "user",
                "kind": "message",
                "parts": [{"kind": "data", "data": data}],
            }
        },
    }


async def _post(
    handler: _InputMemberSeller,
    body: dict[str, Any],
    headers: dict[str, str],
    **app_kwargs: Any,
) -> dict[str, Any]:
    async with _client(_app(handler, **app_kwargs)) as client:
        response = await client.post("/", content=json.dumps(body).encode(), headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result.get("task", result)  # type: ignore[no-any-return]


def _state(task: dict[str, Any]) -> str:
    # 1.0 spells it TASK_STATE_COMPLETED, 0.3 spells it completed.
    return str(task["status"]["state"]).upper()


def _data(task: dict[str, Any]) -> dict[str, Any]:
    parts = task["artifacts"][0]["parts"]
    return next(p["data"] for p in parts if "data" in p)  # type: ignore[no-any-return]


def _assert_completed_with_args(handler: _InputMemberSeller, task: dict[str, Any]) -> None:
    assert _state(task).endswith("COMPLETED"), task
    assert len(handler.seen) == 1
    assert handler.seen[0]["brief"] == "ctv"
    assert handler.seen[0]["context"] == {"trace": "t-1"}
    data = _data(task)
    assert "adcp_error" not in data
    assert data["context"] == {"trace": "t-1"}
    assert "VERSION_UNSUPPORTED" not in json.dumps(task)


def _assert_refused(handler: _InputMemberSeller, task: dict[str, Any]) -> None:
    assert _state(task).endswith("FAILED"), task
    data = _data(task)
    assert data["adcp_error"]["code"] == "INVALID_REQUEST"
    assert "context" not in data
    assert handler.seen == []


# ---------------------------------------------------------------------------
# Profile activated: input is read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_activated_by_header_reads_input() -> None:
    handler = _InputMemberSeller()
    body = _v1({"skill": "get_products", "input": ARGS})
    _assert_completed_with_args(handler, await _post(handler, body, V1_ACTIVE))


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_activated_by_message_extensions_reads_input() -> None:
    handler = _InputMemberSeller()
    body = _v1({"skill": "get_products", "input": ARGS}, extensions=[PROFILE])
    _assert_completed_with_args(handler, await _post(handler, body, V1))


# ---------------------------------------------------------------------------
# Profile activated: anything but exactly one object `input` is refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_activated_both_members_is_refused() -> None:
    handler = _InputMemberSeller()
    data = {
        "skill": "get_products",
        "input": ARGS,
        # Echoable context rides along so a leak would be visible.
        "parameters": {"brief": "x", "context": {"trace": "t-1"}},
    }
    task = await _post(handler, _v1(data), V1_ACTIVE)
    _assert_refused(handler, task)


# ---------------------------------------------------------------------------
# Profile activated: 9.x dispatches the other non-conforming shapes, with a warning
# ---------------------------------------------------------------------------


def _deprecation_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "adcp.server.a2a_server"
        and r.levelno == logging.WARNING
        and r.getMessage().startswith("Deprecated A2A invocation (")
    ]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_activated_parameters_only_is_dispatched_with_a_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="adcp.server.a2a_server")
    handler = _InputMemberSeller()
    body = _v1({"skill": "get_products", "parameters": ARGS})
    _assert_completed_with_args(handler, await _post(handler, body, V1_ACTIVE))
    records = _deprecation_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "parameters_member" in message
    assert "`parameters` is not an alias for `input`" in message


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"skill": "get_products"}, id="input-missing"),
        pytest.param({"skill": "get_products", "input": "x"}, id="input-not-object"),
        pytest.param({"skill": "get_products", "input": [ARGS]}, id="input-list"),
    ],
)
async def test_activated_missing_or_non_object_input_warns_and_is_not_refused(
    data: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="adcp.server.a2a_server")
    handler = _InputMemberSeller()
    async with _client(_app(handler)) as client:
        response = await client.post("/", content=json.dumps(_v1(data)).encode(), headers=V1_ACTIVE)
    assert response.status_code == 200, response.text
    # Whether the handler runs with `{}` is not stable (a version check can
    # fail); what is stable is the warning and that this is not the refusal.
    assert PROFILE_REFUSAL not in response.text
    records = _deprecation_records(caplog)
    assert len(records) == 1
    assert "input_missing" in records[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_activated_extra_data_part_warns_and_is_dispatched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="adcp.server.a2a_server")
    handler = _InputMemberSeller()
    body = _v1(
        {},
        parts=[
            {"data": {"skill": "get_products", "input": ARGS}},
            {"data": {"note": "x"}},
        ],
    )
    _assert_completed_with_args(handler, await _post(handler, body, V1_ACTIVE))
    records = _deprecation_records(caplog)
    assert len(records) == 1
    assert "data_part_count" in records[0].getMessage()


# ---------------------------------------------------------------------------
# Profile not activated: legacy behaviour is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_v03_message_send_parameters_still_works() -> None:
    handler = _InputMemberSeller()
    body = _v03({"skill": "get_products", "parameters": ARGS})
    _assert_completed_with_args(handler, await _post(handler, body, V03))


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_v1_unactivated_parameters_still_works() -> None:
    handler = _InputMemberSeller()
    body = _v1({"skill": "get_products", "parameters": ARGS})
    _assert_completed_with_args(handler, await _post(handler, body, V1))


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_unactivated_both_members_parameters_wins_without_merging() -> None:
    handler = _InputMemberSeller()
    body = _v1(
        {
            "skill": "get_products",
            "parameters": ARGS,
            "input": {"brief": "from-input", "input_only_key": "leaked"},
        }
    )
    task = await _post(handler, body, V1)
    _assert_completed_with_args(handler, task)
    assert "input_only_key" not in handler.seen[0]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize("version", ["0.3", "1.0"])
async def test_unactivated_input_only_is_read(version: str) -> None:
    handler = _InputMemberSeller()
    if version == "0.3":
        body, headers = _v03({"skill": "get_products", "input": ARGS}), V03
    else:
        body, headers = _v1({"skill": "get_products", "input": ARGS}), V1
    _assert_completed_with_args(handler, await _post(handler, body, headers))


# ---------------------------------------------------------------------------
# Custom parser owns its shapes; signed-request gate carries input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_custom_message_parser_is_not_subject_to_the_profile_check() -> None:
    handler = _InputMemberSeller()

    def parser(context: Any) -> tuple[str | None, dict[str, Any]]:
        return "get_products", {"brief": "custom"}

    body = _v1({"skill": "get_products", "parameters": ARGS})
    task = await _post(handler, body, V1_ACTIVE, message_parser=parser)
    assert _state(task).endswith("COMPLETED"), task
    assert [seen["brief"] for seen in handler.seen] == ["custom"]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_signature_gate_middleware_parse_carries_input() -> None:
    """Unsigned get_products is allowed; the gate's cached parse must hand
    the dispatcher the ``input`` member, not an empty argument set."""
    handler = _InputMemberSeller()
    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({"https://buyer.example.com/": {"keys": []}}),
        required_for=frozenset({"create_media_buy"}),
    )
    body = _v1({"skill": "get_products", "input": ARGS})
    task = await _post(handler, body, V1_ACTIVE, request_signature_verification=config)
    _assert_completed_with_args(handler, task)


# ---------------------------------------------------------------------------
# Raw-body receipt path
# ---------------------------------------------------------------------------


def _receipt_body(**members: Any) -> bytes:
    return json.dumps(_v1({"skill": RECEIPT_TASK, **members})).encode()


def test_receipt_raw_body_reads_input() -> None:
    parsed = a2a_receipt_parameters(_receipt_body(input={"marker": "from-input"}))
    assert parsed is not None
    assert parsed["marker"] == "from-input"


def test_receipt_raw_body_reads_parameters() -> None:
    parsed = a2a_receipt_parameters(_receipt_body(parameters={"marker": "from-parameters"}))
    assert parsed is not None
    assert parsed["marker"] == "from-parameters"


def test_receipt_raw_body_both_members_parameters_wins() -> None:
    parsed = a2a_receipt_parameters(
        _receipt_body(parameters={"marker": "from-parameters"}, input={"marker": "from-input"})
    )
    assert parsed is not None
    assert parsed["marker"] == "from-parameters"
    assert "from-input" not in json.dumps(parsed)
