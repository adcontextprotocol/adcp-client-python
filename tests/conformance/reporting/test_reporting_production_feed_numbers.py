"""Raw lexemes and typed reporting clients over separate-process HTTP mounts."""

import json
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from adcp.types import GetReportingStatusRequest

from ._receipt_transport import error_code
from .test_reporting_production_scope import (
    audited,
    onboarding_server,
    public_client,
)


@pytest.fixture(autouse=True)
def _a2a_compat_send_and_aggregate():
    # Real SDK network streams, without the repository's unit-mock shim.
    pass


def assert_invalid_checkpoint(result):
    assert not result.success and result.data is None, result
    if result.adcp_error is not None:
        assert result.adcp_error["code"] == "INVALID_CHECKPOINT", result
    else:
        # The MCP adapter preserves the closed public task error as text.
        assert result.error.endswith(
            "restart the reporting walk; this position is unavailable for this scope"
        ), result


async def raw_call(client, route, request, *, lexeme=None):
    headers = {"Authorization": "Bearer acct_a", "Content-Type": "application/json"}
    if route == "mcp":
        headers["Accept"] = "application/json, text/event-stream"
        initial = await client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "exact-reporting-numbers", "version": "1"},
                },
            },
        )
        assert initial.status_code == 200
        envelope = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_reporting_status", "arguments": request},
        }
        path = "/mcp/"
    else:
        v1 = route == "a2a-1.0"
        headers["A2A-Version"] = "1.0" if v1 else "0.3"
        part = {"data": {"skill": "get_reporting_status", "parameters": request}}
        if not v1:
            part["kind"] = "data"
        envelope = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "SendMessage" if v1 else "message/send",
            "params": {
                "message": {
                    "messageId": "exact-number-message",
                    "role": "ROLE_USER" if v1 else "user",
                    "parts": [part],
                }
            },
        }
        path = "/"
    wire = json.dumps(envelope)
    if lexeme is not None:
        assert wire.count('"__exact_lexeme__"') == 1
        wire = wire.replace('"__exact_lexeme__"', lexeme)
    response = await client.post(path, headers=headers, content=wire)
    assert response.status_code in {200, 400}, response.text
    payload = next(
        (json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")),
        None,
    )
    if payload is None:
        payload = response.json()
    result = payload.get("result", payload)
    if "structuredContent" in result:
        return result["structuredContent"]
    if "task" in result:
        result = result["task"]
    for artifact in result.get("artifacts", []):
        for part in artifact.get("parts", []):
            if "data" in part:
                return part["data"]
    return result


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("notifications", [False, True])
async def test_large_received_decimal_cannot_replay_a_different_filter(
    backend, notifications, tmp_path
):
    async with onboarding_server(
        backend, tmp_path, scenario="feed", notifications=notifications
    ) as (uri, ready):
        async with httpx.AsyncClient(base_url=uri.removesuffix("/mcp/"), timeout=30) as client:
            routes = ("mcp", "a2a-0.3", "a2a-1.0")
            for index, route in enumerate(routes):
                other = routes[(index + 1) % len(routes)]
                for original, changed in (
                    ("9007199254740993.0", "9007199254740992.0"),
                    ("9007199254740995.0", "9007199254740996.0"),
                    ("9.007199254740993e15", "9007199254740992"),
                ):
                    raw = {**ready["request"], "ext": {"vendor": {"n": "__exact_lexeme__"}}}
                    first = await audited(tmp_path, raw_call(client, route, raw, lexeme=original))
                    assert first["pagination"]["has_more"], first
                    continued = deepcopy(raw)
                    continued["pagination"] = {
                        "cursor": first["pagination"]["cursor"],
                        "max_results": 100,
                    }
                    # A bare exact integer and its decimal/exponent spellings
                    # remain the same semantic filter, across protocols.
                    last = await audited(
                        tmp_path,
                        raw_call(client, other, continued, lexeme=str(int(Decimal(original)))),
                    )
                    assert last["pagination"]["has_more"] is False
                    assert last["ledger_snapshot_id"] == first["ledger_snapshot_id"]
                    assert last["changes_checkpoint"] == first["changes_checkpoint"]
                    for position in (
                        {"pagination": continued["pagination"]},
                        {"changes_after": last["changes_checkpoint"]},
                    ):
                        rejected = await audited(
                            tmp_path, raw_call(client, other, {**raw, **position}, lexeme=changed)
                        )
                        assert "pagination" not in rejected, rejected
                        assert error_code(rejected) == "INVALID_CHECKPOINT", rejected
                        after = json.loads((tmp_path / "audit.json").read_text())
                        assert after["http"][-1]["store_unchanged"]
                    empty = await audited(
                        tmp_path,
                        raw_call(
                            client,
                            route,
                            {
                                **raw,
                                "changes_after": last["changes_checkpoint"],
                            },
                            lexeme=original,
                        ),
                    )
                    assert empty["pagination"] == {"total_count": 0, "has_more": False}

                for lexeme in (
                    "0.100000000000000000001",
                    "9007199254740993.25",
                    "1e-400",
                    "1e400",
                    "NaN",
                    "Infinity",
                    "-Infinity",
                ):
                    raw = {
                        **ready["request"],
                        "ext": {
                            "vendor": {
                                "n": "__exact_lexeme__",
                                "private": "numeric-redaction-control",
                            }
                        },
                    }
                    rejected = await audited(tmp_path, raw_call(client, route, raw, lexeme=lexeme))
                    assert "pagination" not in rejected
                    if "error" in rejected:
                        assert rejected["error"]["code"] in {-32700, -32600, -32602}
                    else:
                        assert error_code(rejected) in {"INVALID_REQUEST", "VALIDATION_ERROR"}
                    assert "numeric-redaction-control" not in json.dumps(rejected)
                    assert lexeme not in json.dumps(rejected)
                    assert json.loads((tmp_path / "audit.json").read_text())["http"][-1][
                        "store_unchanged"
                    ]


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_typed_numbers_cross_protocol_and_client_rounding_are_distinct(backend, tmp_path):
    async with onboarding_server(backend, tmp_path, scenario="feed") as (uri, ready):
        for version in ("0.3", "1.0"):
            async with (
                public_client(uri, "acct_a") as mcp,
                public_client(uri, "acct_a", "a2a-" + version) as a2a,
            ):
                for start, finish in ((mcp, a2a), (a2a, mcp)):
                    raw = {
                        **ready["request"],
                        "ext": {
                            "vendor": {
                                "n": 1,
                                "fraction": 4.8,
                                "nested": [0.5, True, False, "1", None],
                            }
                        },
                    }
                    first = await audited(
                        tmp_path,
                        start.get_reporting_status(GetReportingStatusRequest.model_validate(raw)),
                    )
                    assert first.success, first
                    initial = first.data.model_dump(mode="json", exclude_unset=True)
                    continued = {
                        **raw,
                        "pagination": {
                            "max_results": 100,
                            "cursor": initial["pagination"]["cursor"],
                        },
                    }
                    last = await audited(
                        tmp_path,
                        finish.get_reporting_status(
                            GetReportingStatusRequest.model_validate(continued)
                        ),
                    )
                    assert last.success, last
                    final = last.data.model_dump(mode="json", exclude_unset=True)
                    assert final["pagination"] == {"total_count": 6, "has_more": False}
                    assert final["ledger_snapshot_id"] == initial["ledger_snapshot_id"]
                    for changed in (True, "1", 0.5, None):
                        for position in (
                            {"pagination": continued["pagination"]},
                            {"changes_after": final["changes_checkpoint"]},
                        ):
                            negative = deepcopy({**raw, **position})
                            negative["ext"]["vendor"]["n"] = changed
                            denied = await audited(
                                tmp_path,
                                finish.get_reporting_status(
                                    GetReportingStatusRequest.model_validate(negative)
                                ),
                            )
                            assert_invalid_checkpoint(denied)
                            assert json.loads((tmp_path / "audit.json").read_text())["http"][-1][
                                "store_unchanged"
                            ]
                exact = {**ready["request"], "ext": {"vendor": {"n": 9007199254740993}}}
                first = await audited(
                    tmp_path,
                    mcp.get_reporting_status(GetReportingStatusRequest.model_validate(exact)),
                )
                assert first.success, first
                assert json.loads((tmp_path / "audit.json").read_text())["http"][-1][
                    "received_number"
                ] == {
                    "type": "int",
                    "text": "9007199254740993",
                }
                for position in (
                    {"pagination": {"cursor": first.data.pagination.cursor}},
                    {"changes_after": first.data.changes_checkpoint},
                ):
                    denied = await audited(
                        tmp_path,
                        a2a.get_reporting_status(
                            GetReportingStatusRequest.model_validate({**exact, **position})
                        ),
                    )
                    assert_invalid_checkpoint(denied)
                    audit = json.loads((tmp_path / "audit.json").read_text())["http"][-1]
                    assert Decimal(audit["received_number"]["text"]) == 9007199254740992
                    assert audit["store_unchanged"]
                    # Protobuf rounded before transmission. The server rejects
                    # the changed filter; it cannot recover the intended value.
