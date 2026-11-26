"""The validator's node cap guards untrusted input, not a server's own responses.

``DEFAULT_MAX_PAYLOAD_NODES`` bounds the CPU a peer can make ``iter_errors``
spend. It stays on every inbound request and on every response a client
parses. A server validating the response it is about to send applies its own,
configurable budget instead: within it the response is validated in full, and
past it the response is sent with an error log rather than refused for size.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.validation import (
    DEFAULT_MAX_PAYLOAD_NODES,
    SERVER_DEFAULT_VALIDATION,
    ValidationHookConfig,
    count_payload_nodes,
    validate_request,
    validate_response,
)
from adcp.validation.client_hooks import (
    DEFAULT_SERVER_RESPONSE_MAX_NODES,
    validate_incoming_response,
    validate_outgoing_request,
)
from adcp.validation.schema_validator import _MAX_PAYLOAD_NODES, _count_nodes

_BUY: dict[str, Any] = {
    "media_buy_id": "mb_0",
    "status": "active",
    "currency": "USD",
    "total_budget": 1000.0,
    "confirmed_at": "2026-01-01T00:00:00Z",
    "revision": 1,
    "packages": [
        {"package_id": f"pkg_{i}", "budget": 10.0, "product_id": "p", "pricing_option_id": "po"}
        for i in range(5)
    ],
}
_REQUEST = {"adcp_version": "3.2", "account": {"account_id": "acc_1"}}


def _media_buys(count: int) -> dict[str, Any]:
    return {"media_buys": [dict(_BUY, media_buy_id=f"mb_{i}") for i in range(count)]}


# 400 buys is ~13,000 nodes: over the untrusted-input cap, a size a seller's
# get_media_buys legitimately returns.
_LARGE = _media_buys(400)


class _MediaBuys(ADCPHandler[Any]):
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    async def get_media_buys(self, params: Any, ctx: ToolContext) -> dict[str, Any]:
        return self.response


def _caller(response: dict[str, Any], validation: ValidationHookConfig) -> Any:
    return create_tool_caller(_MediaBuys(response), "get_media_buys", validation=validation)


def test_the_large_fixture_is_over_the_untrusted_input_cap() -> None:
    assert count_payload_nodes(_LARGE) > DEFAULT_MAX_PAYLOAD_NODES


class TestNodeCounting:
    def test_counts_every_container_and_scalar(self) -> None:
        assert count_payload_nodes({"a": [1, 2, {"b": None}], "c": "x"}) == 7

    def test_stops_at_the_limit(self) -> None:
        assert count_payload_nodes(list(range(1_000)), 10) == 10

    def test_private_spellings_still_resolve(self) -> None:
        assert _MAX_PAYLOAD_NODES == DEFAULT_MAX_PAYLOAD_NODES == 10_000
        assert _count_nodes(list(range(1_000)), 10) == 10


class TestUntrustedInputKeepsTheCap:
    def test_a_client_still_refuses_an_oversized_response(self) -> None:
        outcome = validate_response("get_media_buys", _LARGE, version="3.2")
        assert not outcome.valid
        assert outcome.issues[0].keyword == "payload_size"

    def test_the_client_hook_still_refuses_an_oversized_response(self) -> None:
        outcome = validate_incoming_response("get_media_buys", _LARGE, "strict", version="3.2")
        assert not outcome.valid
        assert outcome.issues[0].keyword == "payload_size"

    def test_a_request_is_still_refused_at_the_cap(self) -> None:
        oversized = {**_REQUEST, "media_buy_ids": [f"mb_{i}" for i in range(10_000)]}
        outcome = validate_request("get_media_buys", oversized, version="3.2")
        assert not outcome.valid
        assert outcome.issues[0].keyword == "payload_size"
        assert str(DEFAULT_MAX_PAYLOAD_NODES) in outcome.issues[0].message
        with pytest.raises(Exception, match="payload exceeds validator size limit"):
            validate_outgoing_request("get_media_buys", oversized, "strict", version="3.2")

    async def test_a_server_refuses_an_oversized_request_whatever_its_response_budget(
        self,
    ) -> None:
        handler = _MediaBuys(_media_buys(1))
        caller = create_tool_caller(
            handler,
            "get_media_buys",
            validation=ValidationHookConfig(
                requests="strict", responses="strict", server_response_max_nodes=None
            ),
        )
        oversized = {**_REQUEST, "media_buy_ids": [f"mb_{i}" for i in range(10_000)]}
        with pytest.raises(ADCPTaskError) as info:
            await caller(oversized)
        error = info.value.errors[0]
        assert error.code == "VALIDATION_ERROR"
        assert error.details["side"] == "request"
        assert "size limit" in error.message


class TestValidateResponseBudget:
    def test_none_validates_a_payload_of_any_size_in_full(self) -> None:
        assert validate_response("get_media_buys", _LARGE, version="3.2", max_nodes=None).valid

    def test_none_still_finds_a_schema_violation(self) -> None:
        invalid = _media_buys(400)
        invalid["media_buys"][399]["status"] = "not-a-status"
        outcome = validate_response("get_media_buys", invalid, version="3.2", max_nodes=None)
        assert not outcome.valid
        assert outcome.issues[0].pointer.startswith("/media_buys/399")

    def test_an_explicit_cap_is_reported(self) -> None:
        outcome = validate_response("get_media_buys", _LARGE, version="3.2", max_nodes=500)
        assert not outcome.valid
        assert "(500 nodes)" in outcome.issues[0].message


class TestServerResponseBudget:
    def test_the_server_default_budget(self) -> None:
        assert DEFAULT_SERVER_RESPONSE_MAX_NODES == 100_000
        assert ValidationHookConfig().server_response_max_nodes == 100_000
        assert SERVER_DEFAULT_VALIDATION.server_response_max_nodes == 100_000

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "100"])
    def test_an_invalid_budget_is_refused(self, value: Any) -> None:
        with pytest.raises(ValueError, match="server_response_max_nodes"):
            ValidationHookConfig(server_response_max_nodes=value)

    async def test_a_large_valid_response_is_sent(self) -> None:
        caller = _caller(_LARGE, SERVER_DEFAULT_VALIDATION)
        result = await caller(dict(_REQUEST))
        assert len(result["media_buys"]) == 400

    async def test_a_large_invalid_response_is_still_refused(self) -> None:
        invalid = _media_buys(400)
        invalid["media_buys"][399]["status"] = "not-a-status"
        with pytest.raises(ADCPTaskError) as info:
            await _caller(invalid, SERVER_DEFAULT_VALIDATION)(dict(_REQUEST))
        error = info.value.errors[0]
        assert error.code == "VALIDATION_ERROR"
        assert error.details["side"] == "response"
        assert "size limit" not in error.message

    async def test_a_response_over_the_budget_is_sent_and_logged_as_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caller = _caller(
            _LARGE, ValidationHookConfig(responses="strict", server_response_max_nodes=1_000)
        )
        with caplog.at_level(logging.ERROR, logger="adcp.server.mcp_tools"):
            result = await caller(dict(_REQUEST))
        assert len(result["media_buys"]) == 400
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "an unvalidated response must not be sent silently"
        message = errors[0].getMessage()
        assert "get_media_buys" in message
        assert "server_response_max_nodes" in message
        assert "1000" in message
        assert errors[0].__dict__["adcp_validation"] == "response_over_budget"

    async def test_a_budget_of_none_validates_every_response_in_full(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        invalid = _media_buys(400)
        invalid["media_buys"][0]["status"] = "not-a-status"
        caller = _caller(
            invalid, ValidationHookConfig(responses="strict", server_response_max_nodes=None)
        )
        with pytest.raises(ADCPTaskError) as info:
            await caller(dict(_REQUEST))
        assert info.value.errors[0].details["side"] == "response"

    async def test_warn_mode_logs_an_over_budget_response_too(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caller = _caller(
            _LARGE, ValidationHookConfig(responses="warn", server_response_max_nodes=1_000)
        )
        with caplog.at_level(logging.ERROR, logger="adcp.server.mcp_tools"):
            await caller(dict(_REQUEST))
        assert any(r.levelno >= logging.ERROR for r in caplog.records)
