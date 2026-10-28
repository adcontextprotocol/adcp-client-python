"""A server's version pin sets the release its handlers run on (#1685).

The application version is the server's ``adcp_version`` pin, else the newest
of its ``supported_versions``, else the packaged default. Every dispatch path
converts a buyer's request to it and the handler's response back to the
buyer's release.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp._version import resolve_adcp_version
from adcp.compat.versions import CONVERSION_METADATA_KEY
from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform, SingletonAccounts
from adcp.decisioning.capabilities import Features, MediaBuy
from adcp.decisioning.serve import create_adcp_server_from_platform
from adcp.exceptions import ADCPTaskError
from adcp.server import ADCPHandler, ToolContext, create_mcp_server
from adcp.server.a2a_server import ADCPAgentExecutor
from adcp.server.mcp_tools import MCPToolSet, create_tool_caller
from adcp.server.version_policy import resolve_application_adcp_version
from adcp.types import GetProductsRequest, GetProductsResponse
from adcp.validation.client_hooks import ValidationHookConfig
from adcp.validation.envelope import resolve_requested_adcp_version
from adcp.validation.schema_validator import validate_request, validate_response

PACKAGED = resolve_adcp_version(None)
STRICT = ValidationHookConfig(requests="strict", responses="strict")
BUYERS = ["3.2", "3.1", PACKAGED]

# ``include_collections`` exists only from 3.3; a 3.3-beta.0 buyer sending it
# proves the request is converted *down* to a 3.2 application.
NEWEST_ONLY_REQUEST_FIELD = "include_collections"


def _wire_request(version: str) -> dict[str, Any]:
    request: dict[str, Any] = {"adcp_version": version, "buying_mode": "brief", "brief": "shoes"}
    if version == PACKAGED:
        request[NEWEST_ONLY_REQUEST_FIELD] = True
    return request


class _Seen:
    def __init__(self) -> None:
        self.params: list[Any] = []
        self.contexts: list[ToolContext] = []


class DictSeller(ADCPHandler[ToolContext]):
    """A seller whose handler code is written against the stable 3.2 contract."""

    adcp_capabilities = {"media_buy": {"features": {"canonical_creatives": True}}}

    def __init__(self, pin: str | None = "3.2") -> None:
        self.seen = _Seen()
        self._pin = pin

    def get_adcp_version(self) -> str | None:
        return self._pin

    async def get_products(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        assert context is not None
        self.seen.params.append(params)
        self.seen.contexts.append(context)
        return {"products": [], "cache_scope": "public"}


class TypedSeller(DictSeller):
    async def get_products(  # type: ignore[override]
        self, params: GetProductsRequest, context: ToolContext | None = None
    ) -> GetProductsResponse:
        assert context is not None
        self.seen.params.append(params)
        self.seen.contexts.append(context)
        return GetProductsResponse(products=[], cache_scope="public")


def _assert_handler_ran_on(seller: DictSeller, application: str, buyer: str) -> None:
    (params,) = seller.seen.params
    (context,) = seller.seen.contexts
    payload = (
        params
        if isinstance(params, dict)
        else params.model_dump(mode="json", by_alias=True, exclude_unset=True)
    )
    assert payload["adcp_version"] == application
    assert resolve_requested_adcp_version(payload) == application
    assert NEWEST_ONLY_REQUEST_FIELD not in payload or application == PACKAGED
    assert validate_request("get_products", payload, version=application).valid
    assert context.application_adcp_version == application
    assert context.resolved_adcp_version == buyer
    if buyer != application:
        assert context.metadata[CONVERSION_METADATA_KEY]["wire_version"] == buyer


def _assert_wire_response(response: dict[str, Any], buyer: str) -> None:
    assert validate_response("get_products", response, version=buyer).valid
    assert response.get("adcp_version", buyer) == buyer
    assert response["products"] == []


@pytest.mark.parametrize("buyer", BUYERS)
@pytest.mark.parametrize("seller_type", [DictSeller, TypedSeller])
async def test_pinned_server_runs_handlers_on_its_pin_over_mcp(seller_type, buyer: str) -> None:
    seller = seller_type()
    tools = MCPToolSet(seller, validation=STRICT, advertise_all=True)
    response = await tools.call_tool("get_products", _wire_request(buyer))
    _assert_handler_ran_on(seller, "3.2", buyer)
    _assert_wire_response(response, buyer)


@pytest.mark.parametrize("buyer", BUYERS)
@pytest.mark.parametrize("seller_type", [DictSeller, TypedSeller])
async def test_pinned_server_runs_handlers_on_its_pin_over_a2a(seller_type, buyer: str) -> None:
    seller = seller_type()
    executor = ADCPAgentExecutor(seller, validation=STRICT, advertise_all=True)
    context = ToolContext()
    response = await executor._tool_callers["get_products"](_wire_request(buyer), context)
    _assert_handler_ran_on(seller, "3.2", buyer)
    assert context.application_adcp_version == "3.2"
    _assert_wire_response(response, buyer)


@pytest.mark.parametrize("pinning", ["adcp_version", "supported_versions"])
async def test_a2a_request_without_envelope_is_served_as_the_application_version(
    pinning: str,
) -> None:
    seller = DictSeller(pin="3.2" if pinning == "adcp_version" else None)
    executor = ADCPAgentExecutor(
        seller,
        validation=STRICT,
        advertise_all=True,
        supported_versions=["3.1", "3.2"] if pinning == "supported_versions" else None,
    )
    context = ToolContext()
    response = await executor._tool_callers["get_products"](
        {"buying_mode": "brief", "brief": "shoes"}, context
    )
    # A pinned server resolves an omitted envelope to its pin; an unpinned
    # one leaves the buyer's version unknown and serves its application release.
    assert context.resolved_adcp_version == ("3.2" if pinning == "adcp_version" else None)
    assert context.application_adcp_version == "3.2"
    assert seller.seen.params[0].get("adcp_version") is None
    assert validate_response("get_products", response, version="3.2").valid


@pytest.mark.parametrize("buyer", BUYERS)
async def test_typed_params_carry_exactly_the_application_payload(buyer: str) -> None:
    seller = TypedSeller()
    caller = create_tool_caller(seller, "get_products", validation=STRICT)
    await caller(_wire_request(buyer), ToolContext())
    (params,) = seller.seen.params
    assert isinstance(params, GetProductsRequest)
    assert params.adcp_version == "3.2"
    # The packaged class is a superset of 3.2: the 3.3-only field reads as
    # unset and is not written back, even when the 3.3 buyer sent it.
    assert NEWEST_ONLY_REQUEST_FIELD not in params.model_fields_set
    assert params.model_dump(mode="json", by_alias=True, exclude_unset=True) == {
        "adcp_version": "3.2",
        "buying_mode": "brief",
        "brief": "shoes",
    }


@pytest.mark.parametrize("buyer", BUYERS)
async def test_newest_supported_version_is_the_application_version(buyer: str) -> None:
    seller = DictSeller(pin=None)
    caller = create_tool_caller(
        seller, "get_products", validation=STRICT, supported_versions=["3.1", "3.2"]
    )
    if buyer == PACKAGED:
        with pytest.raises(ADCPTaskError) as caught:
            await caller(_wire_request(buyer), ToolContext())
        assert caught.value.errors[0].code == "VERSION_UNSUPPORTED"
        return
    response = await caller(_wire_request(buyer), ToolContext())
    _assert_handler_ran_on(seller, "3.2", buyer)
    _assert_wire_response(response, buyer)


@pytest.mark.parametrize("buyer", BUYERS)
async def test_unpinned_server_still_runs_on_the_packaged_release(buyer: str) -> None:
    seller = DictSeller(pin=None)
    tools = MCPToolSet(seller, validation=STRICT, advertise_all=True)
    response = await tools.call_tool("get_products", _wire_request(buyer))
    _assert_handler_ran_on(seller, PACKAGED, buyer)
    _assert_wire_response(response, buyer)


async def test_explicit_factory_pin_overrides_the_handler_pin() -> None:
    seller = DictSeller(pin="3.2")
    tools = MCPToolSet(seller, adcp_version="3.1", validation=STRICT, advertise_all=True)
    response = await tools.call_tool("get_products", _wire_request("3.2"))
    _assert_handler_ran_on(seller, "3.1", "3.2")
    _assert_wire_response(response, "3.2")


async def test_response_outside_the_application_contract_fails_before_conversion() -> None:
    class Seller(DictSeller):
        async def get_products(self, params, context=None):  # type: ignore[override]
            # ``collections`` is a 3.3 response field; a 3.2 handler may not emit it.
            return {"products": [], "cache_scope": "public", "collections": []}

    caller = create_tool_caller(Seller(), "get_products", validation=STRICT)
    with pytest.raises(ADCPTaskError) as caught:
        await caller(_wire_request(PACKAGED), ToolContext())
    assert caught.value.errors[0].code == "VALIDATION_ERROR"


def test_one_resolver_orders_pin_then_supported_then_packaged() -> None:
    assert resolve_application_adcp_version() == PACKAGED
    assert resolve_application_adcp_version(supported_versions=["3.0", "3.2", "3.1"]) == "3.2"
    assert resolve_application_adcp_version(adcp_version="3.1", supported_versions=["3.2"]) == "3.1"
    assert resolve_application_adcp_version(DictSeller(pin="3.2")) == "3.2"
    assert resolve_application_adcp_version(DictSeller(pin="3.2"), adcp_version="3.1") == "3.1"
    assert resolve_application_adcp_version(DictSeller(pin=None)) == PACKAGED
    assert resolve_application_adcp_version(DictSeller(pin="3.2.1")) == "3.2"


class _Platform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        supported_billing=("operator",),
        # 3.1 buyers need a declared creative dialect.
        media_buy=MediaBuy(features=Features(canonical_creatives=True)),
    )
    accounts = SingletonAccounts(account_id="hello")

    def __init__(self) -> None:
        self.seen = _Seen()

    def get_products(self, req, ctx):
        self.seen.params.append(req)
        self.seen.contexts.append(ctx)
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "x", "status": "active"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}


@pytest.mark.parametrize("buyer", BUYERS)
@pytest.mark.parametrize("pinning", ["adcp_version", "supported_versions"])
async def test_decisioning_platform_runs_on_the_server_pin(pinning: str, buyer: str) -> None:
    platform = _Platform()
    options: dict[str, Any] = (
        {"adcp_version": "3.2"} if pinning == "adcp_version" else {"supported_versions": ["3.2"]}
    )
    handler, executor, _ = create_adcp_server_from_platform(
        platform, validate_at_init=False, **options
    )
    try:
        assert handler.get_adcp_version() == "3.2"
        if pinning == "supported_versions" and buyer != "3.2":
            with pytest.raises(ADCPTaskError) as caught:
                await MCPToolSet(handler, validation=STRICT).call_tool(
                    "get_products", _wire_request(buyer)
                )
            assert caught.value.errors[0].code == "VERSION_UNSUPPORTED"
            return
        for leg in ("mcp", "a2a"):
            platform.seen = _Seen()
            context = ToolContext()
            if leg == "mcp":
                mcp = create_mcp_server(handler, validation=STRICT)
                caller = mcp._tool_manager._tools["get_products"].fn
                response = await caller(**_wire_request(buyer))
            else:
                caller = ADCPAgentExecutor(handler, validation=STRICT)._tool_callers["get_products"]
                response = await caller(_wire_request(buyer), context)
                assert context.application_adcp_version == "3.2"
            (request,) = platform.seen.params
            (ctx,) = platform.seen.contexts
            assert isinstance(request, GetProductsRequest)
            assert request.adcp_version == "3.2"
            assert NEWEST_ONLY_REQUEST_FIELD not in request.model_fields_set
            assert ctx.application_adcp_version == "3.2"
            assert ctx.resolved_adcp_version == buyer
            if isinstance(response, dict):
                _assert_wire_response(response, buyer)
    finally:
        executor.shutdown(wait=True)
