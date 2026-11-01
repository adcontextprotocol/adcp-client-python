"""A raised ``AdcpError`` can reject a request with every problem (#1780).

AdCP's payload ``errors[]`` exists so a seller that validates a whole request
returns all of its problems in one response. ``AdcpError(errors=[...])`` and
:meth:`AdcpError.from_errors` carry the list; both transports write it to
``errors[]`` and keep the envelope ``adcp_error``. The ``details['errors']``
slot the class documented for multi-error preflight is lifted too.
"""

from __future__ import annotations

from typing import Any

import pytest
from a2a import types as pb
from a2a.auth.user import UnauthenticatedUser
from a2a.server.agent_execution.context import RequestContext as A2ARequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from mcp.types import CallToolResult

from adcp.decisioning import AdcpError, ValidationError
from adcp.server import ADCPHandler
from adcp.server.a2a_server import ADCPAgentExecutor
from adcp.server.translate import build_mcp_error_result, translate_error
from adcp.types import Error


def _budget() -> AdcpError:
    return AdcpError(
        "BUDGET_TOO_LOW",
        message="budget below minimum",
        recovery="correctable",
        field="packages[0].budget",
    )


def _dates() -> AdcpError:
    return AdcpError(
        "INVALID_REQUEST",
        message="end_time before start_time",
        recovery="correctable",
        field="end_time",
    )


class TestAdcpErrorErrors:
    def test_single_error_has_no_list(self) -> None:
        error = _budget()
        assert error.errors == []
        assert error.errors_to_wire() == [error.to_wire()]

    def test_from_errors_first_entry_fills_the_envelope(self) -> None:
        error = AdcpError.from_errors([_budget(), _dates()])
        assert error.code == "BUDGET_TOO_LOW"
        assert error.field == "packages[0].budget"
        assert error.recovery == "correctable"
        assert error.errors_to_wire() == [_budget().to_wire(), _dates().to_wire()]
        assert error.errors_to_wire()[0] == error.to_wire()

    def test_from_errors_accepts_mappings_and_models(self) -> None:
        error = AdcpError.from_errors(
            [
                {"code": "INVALID_REQUEST", "message": "unknown product", "field": "packages[1]"},
                Error(code="BUDGET_TOO_LOW", message="too low", recovery="correctable"),
            ]
        )
        wire = error.errors_to_wire()
        # A mapping without ``recovery`` takes the first entry's, ``terminal`` here.
        assert wire[0] == {
            "code": "INVALID_REQUEST",
            "message": "unknown product",
            "field": "packages[1]",
            "recovery": "terminal",
        }
        assert wire[1] == {
            "code": "BUDGET_TOO_LOW",
            "message": "too low",
            "recovery": "correctable",
        }
        assert error.code == "INVALID_REQUEST"

    def test_from_errors_returns_the_base_class_for_a_subclass_entry(self) -> None:
        error = AdcpError.from_errors([ValidationError(field="name"), _dates()])
        assert type(error) is AdcpError
        assert error.code == "VALIDATION_ERROR"
        assert [entry["code"] for entry in error.errors] == ["VALIDATION_ERROR", "INVALID_REQUEST"]

    def test_from_errors_rejects_an_empty_list(self) -> None:
        with pytest.raises(ValueError):
            AdcpError.from_errors([])

    def test_errors_keyword_keeps_a_summary_envelope(self) -> None:
        error = AdcpError(
            "INVALID_REQUEST",
            message="2 problems",
            recovery="correctable",
            errors=[_budget(), {"code": "INVALID_REQUEST", "message": "bad end_time"}],
        )
        assert error.to_wire()["message"] == "2 problems"
        assert [entry["message"] for entry in error.errors_to_wire()] == [
            "budget below minimum",
            "bad end_time",
        ]
        # An entry without recovery takes the raised error's.
        assert error.errors_to_wire()[1]["recovery"] == "correctable"

    @pytest.mark.parametrize(
        "entry",
        [
            {"message": "no code"},
            {"code": "", "message": "empty code"},
            {"code": "INVALID_REQUEST"},
            {"code": 7, "message": "numeric code"},
        ],
    )
    def test_malformed_mapping_entry_is_refused(self, entry: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            AdcpError("INVALID_REQUEST", errors=[entry])

    def test_non_mapping_entry_is_refused(self) -> None:
        with pytest.raises(TypeError):
            AdcpError("INVALID_REQUEST", errors=["budget below minimum"])

    def test_entry_details_are_sanitized_like_to_wire(self) -> None:
        details = {"connection": {"type": "oauth", "secret": "hunter2"}}
        entry = AdcpError("AUTHORIZATION_REQUIRED", message="connect", details=details)
        error = AdcpError.from_errors(
            [_budget(), {"code": "AUTHORIZATION_REQUIRED", "message": "x", "details": details}]
        )
        assert error.errors_to_wire()[1].get("details") == entry.to_wire().get("details")

    def test_errors_to_wire_returns_copies(self) -> None:
        error = AdcpError.from_errors([_budget(), _dates()])
        error.errors_to_wire()[0]["code"] = "MUTATED"
        assert error.errors_to_wire()[0]["code"] == "BUDGET_TOO_LOW"


class TestDetailsErrorsSlot:
    def test_well_formed_details_errors_are_lifted(self) -> None:
        error = AdcpError(
            "INVALID_REQUEST",
            message="2 problems",
            recovery="correctable",
            details={
                "errors": [
                    {"code": "INVALID_REQUEST", "message": "budget", "field": "packages[0].budget"},
                    {"code": "INVALID_REQUEST", "message": "dates", "field": "end_time"},
                ]
            },
        )
        assert [entry["field"] for entry in error.errors_to_wire()] == [
            "packages[0].budget",
            "end_time",
        ]
        assert {entry["recovery"] for entry in error.errors_to_wire()} == {"correctable"}
        # The envelope keeps its details unchanged.
        assert len(error.to_wire()["details"]["errors"]) == 2

    @pytest.mark.parametrize(
        "value",
        [
            ["budget below minimum"],
            [{"code": "INVALID_REQUEST"}],
            [{"path": "x", "msg": "bad"}],
            [],
            "not a list",
        ],
    )
    def test_other_details_errors_are_left_alone(self, value: Any) -> None:
        error = AdcpError("INVALID_REQUEST", message="bad", details={"errors": value})
        assert error.errors == []
        assert error.errors_to_wire() == [error.to_wire()]

    def test_explicit_errors_win_over_details(self) -> None:
        error = AdcpError(
            "INVALID_REQUEST",
            details={"errors": [{"code": "A", "message": "a"}]},
            errors=[_dates()],
        )
        assert [entry["code"] for entry in error.errors_to_wire()] == ["INVALID_REQUEST"]


class TestMcpProjection:
    def test_issue_repro_returns_both_problems(self) -> None:
        error = AdcpError(
            "INVALID_REQUEST",
            message="2 problems",
            recovery="correctable",
            details={
                "errors": [
                    {
                        "code": "INVALID_REQUEST",
                        "message": "budget below minimum",
                        "field": "packages[0].budget",
                    },
                    {
                        "code": "INVALID_REQUEST",
                        "message": "end_time before start_time",
                        "field": "end_time",
                    },
                ]
            },
        )
        structured = build_mcp_error_result(error).structured_content
        assert [entry.get("field") for entry in structured["errors"]] == [
            "packages[0].budget",
            "end_time",
        ]
        assert structured["adcp_error"]["code"] == "INVALID_REQUEST"

    def test_from_errors_envelope_is_errors_zero(self) -> None:
        structured = build_mcp_error_result(
            AdcpError.from_errors([_budget(), _dates()])
        ).structured_content
        assert structured["adcp_error"] == structured["errors"][0]
        assert structured["adcp_error"] is not structured["errors"][0]
        assert [entry["field"] for entry in structured["errors"]] == [
            "packages[0].budget",
            "end_time",
        ]

    def test_single_error_still_mirrors(self) -> None:
        structured = build_mcp_error_result(_budget()).structured_content
        assert structured["errors"] == [structured["adcp_error"]]

    def test_errors_survive_context_echo_and_enhancer(self) -> None:
        seen: list[int] = []

        def enhancer(response: dict[str, Any]) -> None:
            seen.append(len(response["errors"]))

        structured = build_mcp_error_result(
            AdcpError.from_errors([_budget(), _dates()]),
            params={"context": {"correlation_id": "c-1"}},
            response_enhancer=enhancer,
        ).structured_content
        assert seen == [2]
        assert structured["context"] == {"correlation_id": "c-1"}

    @pytest.mark.asyncio
    async def test_through_tool_registration(self) -> None:
        from mcp.server import MCPServer

        from adcp.server.serve import _register_tool

        async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
            raise AdcpError.from_errors([_budget(), _dates()])

        mcp = MCPServer("test-1780")
        _register_tool(mcp, "create_media_buy", "test", {"type": "object"}, caller)
        result = await mcp.call_tool("create_media_buy", {})

        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        assert [entry["code"] for entry in result.structured_content["errors"]] == [
            "BUDGET_TOO_LOW",
            "INVALID_REQUEST",
        ]

    def test_translate_error_a2a_data_carries_every_error(self) -> None:
        a2a_error = translate_error(AdcpError.from_errors([_budget(), _dates()]), "a2a")
        data: Any = a2a_error.data  # type: ignore[attr-defined]
        assert [entry["field"] for entry in data["errors"]] == ["packages[0].budget", "end_time"]

    def test_translate_error_a2a_single_error_has_no_errors_key(self) -> None:
        a2a_error = translate_error(_budget(), "a2a")
        data: Any = a2a_error.data  # type: ignore[attr-defined]
        assert "errors" not in data


class _Raiser(ADCPHandler):
    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def get_products(self, params: Any, context: Any = None) -> Any:
        raise AdcpError.from_errors([_budget(), _dates()])


@pytest.mark.asyncio
async def test_a2a_failed_task_carries_every_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADCP_SANDBOX", "1")
    value = Value()
    ParseDict({"skill": "get_products", "parameters": {}}, value)
    message = pb.Message(message_id="m-1", role=pb.Role.ROLE_USER, parts=[pb.Part(data=value)])
    request = A2ARequestContext(
        call_context=ServerCallContext(user=UnauthenticatedUser()),
        request=pb.SendMessageRequest(message=message),
    )
    queue = EventQueue()
    await ADCPAgentExecutor(_Raiser(), validation=None).execute(request, queue)
    task = await queue.dequeue_event()

    payloads = [
        MessageToDict(part.data)
        for part in task.artifacts[0].parts
        if part.WhichOneof("content") == "data"
    ]
    payload = next(p for p in payloads if "adcp_error" in p)
    assert payload["adcp_error"]["code"] == "BUDGET_TOO_LOW"
    assert [entry["field"] for entry in payload["errors"]] == ["packages[0].budget", "end_time"]
