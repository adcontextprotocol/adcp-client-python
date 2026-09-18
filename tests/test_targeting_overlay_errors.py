"""The beta.14 runtime bridge must not change request-document error paths."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.testclient import TestClient

from adcp.__main__ import _dispatch_tool
from adcp.decisioning.dispatch import _validation_error_to_invalid_request
from adcp.exceptions import ADCPTaskError
from adcp.server import ADCPHandler
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import capabilities_response
from adcp.server.serve import _build_mcp_and_a2a_app
from adcp.types import (
    BuyProductsRequest,
    ControlMediaBuyRequest,
    CreateMediaBuyRequest,
    UpdateMediaBuyRequest,
)
from adcp.types.error_narrowing import narrow_union_errors
from adcp.types.generated_poc.core.targeting_input import TargetingOverlayInput

_SECRET = "never-echo-this-buyer-value"
_PATHS = [
    pytest.param("create_media_buy", "packages", CreateMediaBuyRequest, id="create"),
    pytest.param("update_media_buy", "packages", UpdateMediaBuyRequest, id="update"),
    pytest.param("update_media_buy", "new_packages", UpdateMediaBuyRequest, id="update-new"),
    pytest.param("control_media_buy", "packages", ControlMediaBuyRequest, id="control"),
    pytest.param("buy_products", "purchases", BuyProductsRequest, id="buy"),
]


def _payload(task: str, collection: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "adcp_version": "3.2-rc.3",
        "idempotency_key": "targeting-error-1181",
        "account": {"account_id": "account-1"},
    }
    new_package = {"product_id": "product-1", "pricing_option_id": "price-1", "budget": 1000}
    if task in {"create_media_buy", "buy_products"}:
        params.update(
            brand={"brand_id": "brand_1", "domain": "brand.example"},
            start_time="2026-10-01T00:00:00Z",
            end_time="2026-10-31T00:00:00Z",
        )
        item = new_package
    else:
        params["media_buy_id"] = "buy-1"
        item = new_package if collection == "new_packages" else {"package_id": "package-1"}
    if task == "control_media_buy":
        params["revision"] = 1
    elif task == "buy_products":
        params["feed_version"] = "feed-1"
    params[collection] = [{**item, "targeting_overlay": {"geo_countries": [{"secret": _SECRET}]}}]
    return params


class _TargetingErrorHandler(ADCPHandler[Any]):
    advertised_tools: ClassVar[set[str]] = {
        "get_adcp_capabilities",
        "create_media_buy",
        "update_media_buy",
        "control_media_buy",
        "buy_products",
    }

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return capabilities_response(["media_buy"])

    async def create_media_buy(self, params: CreateMediaBuyRequest, context: Any = None) -> Any:
        raise AssertionError("invalid targeting reached the handler")

    async def update_media_buy(self, params: UpdateMediaBuyRequest, context: Any = None) -> Any:
        raise AssertionError("invalid targeting reached the handler")

    async def control_media_buy(self, params: ControlMediaBuyRequest, context: Any = None) -> Any:
        raise AssertionError("invalid targeting reached the handler")

    async def buy_products(self, params: BuyProductsRequest, context: Any = None) -> Any:
        raise AssertionError("invalid targeting reached the handler")


def _assert_error(error: dict[str, Any], collection: str) -> None:
    path = [collection, 0, "targeting_overlay", "geo_countries", 0]
    field = ".".join(map(str, path))
    assert error["code"] == "INVALID_REQUEST"
    assert error["field"] == field
    assert "Input should be a valid string" in error["message"]
    details = error["details"]["validation_errors"]
    assert len(details) == 1, details
    assert list(details[0]["loc"]) == path
    assert details[0]["type"] == "string_type"
    assert details[0]["msg"] == "Input should be a valid string"
    assert set(details[0]) == {"type", "loc", "msg"}
    encoded = json.dumps(error)
    assert _SECRET not in encoded
    assert "TargetingOverlay" not in encoded


@pytest.mark.parametrize("task,collection,model", _PATHS)
def test_direct_validation_error_uses_request_document_path(
    task: str, collection: str, model: type[BaseModel]
) -> None:
    with pytest.raises(ValidationError) as raised:
        model.model_validate(_payload(task, collection))
    errors = raised.value.errors(include_input=False, include_context=False, include_url=False)
    assert errors == [
        {
            "type": "string_type",
            "loc": (collection, 0, "targeting_overlay", "geo_countries", 0),
            "msg": "Input should be a valid string",
        }
    ]


@pytest.mark.parametrize("task,collection,model", _PATHS)
@pytest.mark.parametrize("mode", ["python", "json"])
@pytest.mark.parametrize(
    "overlay",
    [
        {"geo_countries": [_SECRET]},
        {"geo_countries": []},
        {"keyword_targets": [{"keyword": "", "match_type": _SECRET, "bid_price": -1}]},
        {"placement_selection": {"mode": "selected", "placement_refs": [{}]}},
        _SECRET,
    ],
    ids=["pattern-context", "length-context", "multiple-errors", "nested-union", "wrong-type"],
)
def test_errors_match_input_schema_including_nested_unions_and_context(
    task: str, collection: str, model: type[BaseModel], mode: str, overlay: Any
) -> None:
    reference = TypeAdapter(TargetingOverlayInput | None)
    with pytest.raises(ValidationError) as expected:
        if mode == "json":
            reference.validate_json(json.dumps(overlay))
        else:
            reference.validate_python(overlay)
    params = _payload(task, collection)
    params[collection][0]["targeting_overlay"] = overlay
    with pytest.raises(ValidationError) as actual:
        if mode == "json":
            model.model_validate_json(json.dumps(params))
        else:
            model.model_validate(params)
    prefix = (collection, 0, "targeting_overlay")
    # Compare complete raw diagnostics, not just the sanitized wire subset:
    # error codes, contexts, inputs, URLs and genuine inner arms must survive.
    assert actual.value.errors() == [
        {**error, "loc": (*prefix, *error["loc"])} for error in expected.value.errors()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("task,collection,model", _PATHS)
async def test_typed_caller_has_one_safe_document_error(
    task: str, collection: str, model: type[BaseModel]
) -> None:
    caller = create_tool_caller(_TargetingErrorHandler(), task)
    with pytest.raises(ADCPTaskError) as raised:
        await caller(_payload(task, collection))
    assert len(raised.value.errors) == 1
    _assert_error(raised.value.errors[0].model_dump(mode="json", exclude_none=True), collection)


@pytest.fixture(scope="module")
def mounted_client() -> Iterator[TestClient]:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ADCP_SANDBOX", "1")
        app = _build_mcp_and_a2a_app(
            _TargetingErrorHandler(),
            name="targeting-errors",
            port=3001,
            host="127.0.0.1",
            instructions=None,
            test_controller=None,
            # Exercise the typed-model boundary; strict JSON Schema validation
            # rejects the same invalid document before reaching that boundary.
            validation=None,
            stateless_http=True,
            allowed_hosts=["testserver"],
        )
        with TestClient(app) as client:
            yield client


@pytest.mark.parametrize("task,collection,model", _PATHS)
@pytest.mark.parametrize("transport", ["mcp", "a2a"])
def test_mounted_transports_keep_targeting_errors_runtime_only(
    mounted_client: TestClient,
    task: str,
    collection: str,
    model: type[BaseModel],
    transport: str,
) -> None:
    params = _payload(task, collection)
    if transport == "mcp":
        response = mounted_client.post(
            "/mcp/",
            headers={"accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": task, "arguments": params},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["isError"] is True
        error = result["structuredContent"]["adcp_error"]
    else:
        response = mounted_client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": f"targeting-{task}-{collection}",
                        "role": "user",
                        "parts": [{"kind": "data", "data": {"skill": task, "parameters": params}}],
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["status"]["state"] == "failed", result
        error = result["artifacts"][0]["parts"][0]["data"]["adcp_error"]
    _assert_error(error, collection)
    assert _SECRET not in response.text
    assert "TargetingOverlay" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("task,collection,model", _PATHS)
async def test_cli_preserves_one_document_error(
    task: str, collection: str, model: type[BaseModel]
) -> None:
    client = AsyncMock()
    result = await _dispatch_tool(client, task, _payload(task, collection))
    assert result.success is False
    field = f"{collection}.0.targeting_overlay.geo_countries.0"
    assert result.error == (
        f"Invalid request payload for {task}:\n  - {field}: Input should be a valid string"
    )
    getattr(client, task).assert_not_awaited()


@pytest.mark.parametrize("task,collection,model", _PATHS)
def test_shared_platform_formatter_preserves_document_error(
    task: str, collection: str, model: type[BaseModel]
) -> None:
    with pytest.raises(ValidationError) as raised:
        model.model_validate(_payload(task, collection))
    error = _validation_error_to_invalid_request(task, raised.value)
    _assert_error(
        {
            "code": error.code,
            "field": error.field,
            "message": error.args[0],
            "details": error.details,
        },
        collection,
    )


@pytest.mark.asyncio
async def test_many_invalid_values_do_not_depend_on_the_narrowing_size_cap() -> None:
    params = _payload("create_media_buy", "packages")
    params["packages"][0]["targeting_overlay"]["geo_countries"] *= 501
    caller = create_tool_caller(_TargetingErrorHandler(), "create_media_buy")
    with pytest.raises(ADCPTaskError) as raised:
        await caller(params)
    wire = raised.value.errors[0].model_dump(mode="json", exclude_none=True)
    errors = wire["details"]["validation_errors"]
    assert len(errors) == 501
    assert [error["loc"] for error in errors] == [
        ["packages", 0, "targeting_overlay", "geo_countries", index] for index in range(501)
    ]
    assert _SECRET not in json.dumps(wire)
    assert "TargetingOverlay" not in json.dumps(wire)


def test_genuine_union_keeps_both_variant_errors_and_locations() -> None:
    class FirstVariant(BaseModel):
        value: int

    class SecondVariant(BaseModel):
        value: float

    class Request(BaseModel):
        choice: FirstVariant | SecondVariant

    with pytest.raises(ValidationError) as raised:
        Request.model_validate({"choice": {"value": _SECRET}})
    errors = raised.value.errors(include_input=False, include_context=False, include_url=False)
    assert [error["loc"] for error in errors] == [
        ("choice", "FirstVariant", "value"),
        ("choice", "SecondVariant", "value"),
    ]
    assert narrow_union_errors(errors) == errors
    public_error = _validation_error_to_invalid_request("example", raised.value)
    assert public_error.field == "choice.FirstVariant.value"
    assert public_error.details == {"validation_errors": errors}
    assert _SECRET not in public_error.args[0]
