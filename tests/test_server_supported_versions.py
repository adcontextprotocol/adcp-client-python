"""Explicit served-version selections match discovery and every dispatch path."""

from __future__ import annotations

import importlib
import json
from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from adcp.exceptions import ADCPTaskError, ConfigurationError
from adcp.server import ADCPHandler, ServeConfig, ToolContext, create_a2a_server, create_mcp_server
from adcp.server.a2a_server import ADCPAgentExecutor
from adcp.server.mcp_tools import MCPToolSet, create_tool_caller
from adcp.server.responses import capabilities_response
from adcp.server.serve import _build_a2a_app, _build_mcp_and_a2a_app
from adcp.server.version_policy import resolve_supported_versions
from adcp.validation.client_hooks import ValidationHookConfig


class VersionSelectionSeller(ADCPHandler[ToolContext]):
    advertised_tools = {"get_products", "get_adcp_capabilities"}
    adcp_capabilities = {"media_buy": {"features": {"canonical_creatives": True}}}

    def __init__(self) -> None:
        self.versions: list[str | None] = []
        self.capabilities = capabilities_response(["media_buy"])

    async def get_products(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        assert context is not None
        self.versions.append(context.resolved_adcp_version)
        return {"products": [], "cache_scope": "public"}

    async def get_adcp_capabilities(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> dict[str, Any]:
        return self.capabilities


REQUEST = {"buying_mode": "brief", "brief": "sports"}


@pytest.mark.parametrize("selection", [[], "3.2", ["3.3"], ["3.2.1"], ["3.2-rc.8"], [None], [{}]])
@pytest.mark.parametrize("factory", [ServeConfig, create_mcp_server, create_a2a_server, MCPToolSet])
def test_invalid_selections_fail_at_construction(factory: Any, selection: Any) -> None:
    with pytest.raises(ConfigurationError, match="supported_versions"):
        if factory is ServeConfig:
            factory(supported_versions=selection)
        else:
            factory(VersionSelectionSeller(), supported_versions=selection)


def test_selected_versions_require_installed_schema_bundles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("adcp.server.version_policy.list_validator_keys", lambda **kwargs: [])
    with pytest.raises(ConfigurationError, match="installed schema bundles"):
        create_mcp_server(VersionSelectionSeller(), supported_versions=["3.2"])


def test_trusted_pin_must_belong_to_selected_contracts() -> None:
    class PinnedSeller(VersionSelectionSeller):
        def get_adcp_version(self) -> str:
            return "3.1"

    for factory in [create_mcp_server, create_a2a_server, MCPToolSet]:
        with pytest.raises(ConfigurationError, match="outside supported_versions"):
            factory(PinnedSeller(), supported_versions=["3.2"])


@pytest.mark.parametrize(
    "validation", [None, ValidationHookConfig(requests="strict", responses="strict")]
)
@pytest.mark.parametrize("claim", ["3.0", "3.1", "2.5", "3.2-rc.7"])
async def test_explicit_excluded_versions_never_reach_handler(validation: Any, claim: str) -> None:
    seller = VersionSelectionSeller()
    caller = create_tool_caller(
        seller, "get_products", validation=validation, supported_versions=["3.2"]
    )
    with pytest.raises(ADCPTaskError) as caught:
        await caller({**REQUEST, "adcp_version": claim})
    assert caught.value.errors[0].code == "VERSION_UNSUPPORTED"
    assert caught.value.errors[0].details == {
        "claimed_version": claim,
        "supported_versions": ["3.2"],
    }
    assert seller.versions == []


@pytest.mark.parametrize(
    "envelope", [{"adcp_version": "3.2"}, {"adcp_version": "3.2.1"}, {"adcp_major_version": 3}]
)
async def test_selected_envelopes_route_to_the_selected_bundle(envelope: dict[str, Any]) -> None:
    seller = VersionSelectionSeller()
    caller = create_tool_caller(
        seller,
        "get_products",
        supported_versions=["3.2"],
        validation=(
            None
            if envelope.get("adcp_version") == "3.2.1"
            else ValidationHookConfig(requests="strict", responses="strict")
        ),
    )
    assert (await caller({**REQUEST, **envelope}))["products"] == []
    assert seller.versions == ["3.2"]


@pytest.mark.parametrize(
    "params,claimed",
    [(REQUEST, "3.0"), ({**REQUEST, "brand_manifest": "https://brand.example"}, "2.5")],
)
async def test_unversioned_defaults_and_legacy_probes_cannot_escape_selection(
    params: dict[str, Any], claimed: str
) -> None:
    seller = VersionSelectionSeller()
    caller = create_tool_caller(seller, "get_products", supported_versions=["3.2"])
    with pytest.raises(ADCPTaskError) as caught:
        await caller(params)
    assert caught.value.errors[0].code == "VERSION_UNSUPPORTED"
    assert caught.value.errors[0].details["claimed_version"] == claimed
    assert seller.versions == []


async def test_a2a_omitted_envelope_is_served_as_the_newest_selected_release() -> None:
    """An unpinned A2A server runs on its newest selected release, not the packaged one."""
    seller = VersionSelectionSeller()
    executor = ADCPAgentExecutor(seller, validation=None, supported_versions=["3.1"])
    context = ToolContext()
    response = await executor._tool_callers["get_products"](REQUEST, context)
    assert response["products"] == []
    assert seller.versions == [None]
    assert context.application_adcp_version == "3.1"


async def test_hooks_are_checked_before_dispatch_and_aliases_remain_explicit() -> None:
    seller = VersionSelectionSeller()
    caller = create_tool_caller(
        seller,
        "get_products",
        supported_versions=["3.2", "3.2-rc.7"],
        pre_validation_hook=lambda tool, args: {**args, "adcp_version": "3.2-rc.7"},
    )
    result = await caller(REQUEST)
    assert seller.versions == ["3.2"]
    assert result["adcp_version"] == "3.2-rc.7"
    excluded = create_tool_caller(
        seller,
        "get_products",
        supported_versions=["3.2"],
        pre_validation_hook=lambda tool, args: {**args, "adcp_version": "3.1"},
    )
    with pytest.raises(ADCPTaskError):
        await excluded(REQUEST)
    assert seller.versions == ["3.2"]


async def test_selections_are_frozen_and_independent_on_the_same_handler() -> None:
    seller = VersionSelectionSeller()
    original_versions = list(seller.capabilities["adcp"]["supported_versions"])
    selection = ["3.2"]
    current = MCPToolSet(seller, supported_versions=selection)
    previous = MCPToolSet(seller, supported_versions=["3.1"])
    selection.append("3.1")
    for tools, version in [(current, "3.2"), (previous, "3.1")]:
        response = await tools.call_tool("get_adcp_capabilities", {"adcp_version": version})
        assert response["adcp"]["supported_versions"] == [version]
        assert response["adcp"]["major_versions"] == [3]
    assert seller.capabilities["adcp"]["supported_versions"] == original_versions
    with pytest.raises(ADCPTaskError):
        await current.call_tool("get_products", {**REQUEST, "adcp_version": "3.1"})


async def test_capability_enhancer_cannot_widen_or_mutate_another_server_selection() -> None:
    seller = VersionSelectionSeller()
    original_versions = list(seller.capabilities["adcp"]["supported_versions"])

    def enhance(response: dict[str, Any]) -> None:
        response["adcp"]["supported_versions"].append("3.1")

    caller = create_tool_caller(
        seller,
        "get_adcp_capabilities",
        supported_versions=["3.2"],
        response_enhancer=enhance,
    )
    response = await caller({"adcp_version": "3.2"})
    assert response["adcp"]["supported_versions"] == ["3.2"]
    assert seller.capabilities["adcp"]["supported_versions"] == original_versions


def test_config_freezes_selection_and_controls_server_forwarding() -> None:
    serve_module = importlib.import_module("adcp.server.serve")
    selection = ["3.2"]
    config = ServeConfig(supported_versions=selection)
    selection.append("3.1")
    assert config.supported_versions == ("3.2",)
    with patch.object(serve_module, "_serve_mcp") as run:
        serve_module.serve(VersionSelectionSeller(), config=config, supported_versions=["3.1"])
    assert run.call_args.kwargs["supported_versions"] == ("3.2",)


@pytest.mark.parametrize("transport", ["mcp", "a2a", "direct-a2a", "both"])
def test_public_transport_dispatch_and_capabilities_share_selection(transport: str) -> None:
    seller = VersionSelectionSeller()
    kwargs = {"supported_versions": ["3.2"], "validation": None}
    if transport == "mcp":
        app = create_mcp_server(seller, stateless_http=True, **kwargs).streamable_http_app()
    elif transport == "direct-a2a":
        app = create_a2a_server(seller, **kwargs)
    elif transport == "a2a":
        app = _build_a2a_app(seller, name="seller", port=3001, test_controller=None, **kwargs)
    else:
        app = _build_mcp_and_a2a_app(
            seller,
            name="seller",
            port=3001,
            host="localhost",
            instructions=None,
            test_controller=None,
            stateless_http=True,
            **kwargs,
        )
    legs = ["mcp", "a2a"] if transport == "both" else ["mcp" if transport == "mcp" else "a2a"]
    with TestClient(app, base_url="http://localhost:3001") as client:
        for leg in legs:

            def call(tool: str, params: dict[str, Any]) -> Any:
                if leg == "mcp":
                    body = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": params},
                    }
                else:
                    body = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "message/send",
                        "params": {
                            "message": {
                                "messageId": "m1",
                                "role": "user",
                                "parts": [
                                    {"kind": "data", "data": {"skill": tool, "parameters": params}}
                                ],
                            }
                        },
                    }
                response = client.post(
                    "/mcp" if leg == "mcp" else "/",
                    json=body,
                    headers={"accept": "application/json, text/event-stream"},
                )
                assert response.status_code == 200
                return response.json()

            assert "VERSION_UNSUPPORTED" in json.dumps(
                call("get_products", {**REQUEST, "adcp_version": "3.1"})
            )
            assert seller.versions == (["3.2"] if leg == "a2a" and transport == "both" else [])
            response = call("get_products", {**REQUEST, "adcp_version": "3.2"})
            assert "products" in json.dumps(response)
            capabilities = call("get_adcp_capabilities", {"adcp_version": "3.2"})
            serialized = json.dumps(capabilities)
            assert '"supported_versions": ["3.2"]' in serialized


def test_decisioning_selection_validates_before_allocating_executor_and_reaches_transports() -> (
    None
):
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from tests.test_decisioning_serve import _BarePlatform

    serve_module = importlib.import_module("adcp.decisioning.serve")
    with patch.object(serve_module, "ThreadPoolExecutor") as executor:
        with pytest.raises(ConfigurationError, match="supported_versions"):
            create_adcp_server_from_platform(_BarePlatform(), supported_versions=["3.3"])
        executor.assert_not_called()
    handler, executor, _ = create_adcp_server_from_platform(
        _BarePlatform(), supported_versions=["3.2"]
    )
    try:
        assert resolve_supported_versions(None, handler=handler) == ("3.2",)
        for factory in [create_mcp_server, create_a2a_server, MCPToolSet]:
            factory(handler)
    finally:
        executor.shutdown(wait=True)


async def test_decisioning_capabilities_and_testing_helpers_preserve_selection() -> None:
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.testing import build_test_client
    from tests.test_decisioning_serve import _BarePlatform

    handler, executor, _ = create_adcp_server_from_platform(
        _BarePlatform(), supported_versions=["3.2"], validate_at_init=False
    )
    try:
        response = await handler.get_adcp_capabilities({})
        assert response["adcp"]["supported_versions"] == ["3.2"]
    finally:
        executor.shutdown(wait=True)
    async with build_test_client(
        _BarePlatform(),
        transport="a2a",
        supported_versions=["3.2"],
        validation=None,
        validate_at_init=False,
    ) as client:
        response = await client.get("/.well-known/agent-card.json")
        assert response.status_code == 200


async def test_composed_handler_supports_independent_transport_selections() -> None:
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from tests.test_decisioning_serve import _BarePlatform

    handler, executor, _ = create_adcp_server_from_platform(
        _BarePlatform(), supported_versions=["3.2"], validate_at_init=False
    )
    try:
        inherited = MCPToolSet(handler)
        overridden = MCPToolSet(handler, supported_versions=["3.1", "3.2"])
        with pytest.raises(ADCPTaskError) as caught:
            await inherited.call_tool("get_adcp_capabilities", {"adcp_version": "3.1"})
        assert caught.value.errors[0].code == "VERSION_UNSUPPORTED"
        response = await overridden.call_tool("get_adcp_capabilities", {"adcp_version": "3.1"})
        assert response["adcp"]["supported_versions"] == ["3.1", "3.2"]
        assert (await handler.get_adcp_capabilities({}))["adcp"]["supported_versions"] == ["3.2"]
        assert (await inherited.call_tool("get_adcp_capabilities", {}))["adcp"][
            "supported_versions"
        ] == ["3.2"]
    finally:
        executor.shutdown(wait=True)


def test_only_current_selection_removes_legacy_discovery_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adcp.decisioning.dispatch import validate_platform
    from adcp.decisioning.types import AdcpError
    from tests.test_decisioning_serve import _SalesPlatformWithRequiredMethods

    monkeypatch.setenv("ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM", "1")

    class CurrentSeller(_SalesPlatformWithRequiredMethods):
        def get_media_buys(self, req: Any, ctx: Any) -> dict[str, Any]:
            return {"media_buys": []}

        def list_creatives(self, req: Any, ctx: Any) -> dict[str, Any]:
            return {"creatives": []}

        def provide_performance_feedback(self, req: Any, ctx: Any) -> dict[str, Any]:
            return {}

    validate_platform(CurrentSeller(), supported_versions=["3.2"])
    with pytest.raises(AdcpError, match="list_creative_formats_legacy"):
        validate_platform(CurrentSeller(), supported_versions=["3.1", "3.2"])


@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_managed_test_controller_obeys_server_selection(leg: str) -> None:
    from adcp.server.test_controller import TestControllerStore, register_test_controller

    store = TestControllerStore()
    if leg == "mcp":
        mcp = create_mcp_server(VersionSelectionSeller(), supported_versions=["3.2"])
        register_test_controller(mcp, store)
        caller = mcp._tool_manager._tools["comply_test_controller"].fn
    else:
        executor = ADCPAgentExecutor(
            VersionSelectionSeller(), test_controller=store, supported_versions=["3.2"]
        )
        caller = executor._tool_callers["comply_test_controller"]

    async def call(params: dict[str, Any]) -> Any:
        return await caller(**params) if leg == "mcp" else await caller(params)

    with pytest.raises(ADCPTaskError) as caught:
        await call({"scenario": "list_scenarios", "adcp_version": "3.1"})
    assert caught.value.errors[0].code == "VERSION_UNSUPPORTED"
    assert caught.value.errors[0].details["supported_versions"] == ["3.2"]
    response = await call(
        {"scenario": "list_scenarios", "adcp_version": "3.2", "account": {"sandbox": True}}
    )
    assert response["success"] is True
    assert "scenarios" in response


def test_custom_a2a_request_handler_cannot_bypass_selected_dispatch() -> None:
    from unittest.mock import MagicMock

    with pytest.raises(ValueError, match="supported_versions.*request_handler"):
        create_a2a_server(
            VersionSelectionSeller(), supported_versions=["3.2"], request_handler=MagicMock()
        )


async def test_toolset_explicit_pin_takes_precedence_over_handler_pin() -> None:
    class PinnedSeller(VersionSelectionSeller):
        def get_adcp_version(self) -> str:
            return "3.2"

    tools = MCPToolSet(
        PinnedSeller(), adcp_version="3.1", supported_versions=["3.1"], advertise_all=True
    )
    assert (await tools.call_tool("get_products", REQUEST))["products"] == []


@pytest.mark.parametrize("pin", ["3.2-rc.7", "3.2.1"])
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_trusted_default_pins_use_the_selected_canonical_bundle(pin: str, leg: str) -> None:
    class PinnedSeller(VersionSelectionSeller):
        def get_adcp_version(self) -> str:
            return pin

    seller = PinnedSeller()
    selection = ["3.2", "3.2-rc.7"]
    if leg == "mcp":
        tools = MCPToolSet(seller, supported_versions=selection, advertise_all=True)
        response = await tools.call_tool("get_products", REQUEST)
    else:
        executor = ADCPAgentExecutor(seller, supported_versions=selection, advertise_all=True)
        response = await executor._tool_callers["get_products"](REQUEST)
    assert response["products"] == []
    assert seller.versions == ["3.2"]


@pytest.mark.parametrize("pin", ["3.2-rc.7", "3.2.1"])
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_controller_trusted_default_pins_use_the_selected_canonical_bundle(
    pin: str, leg: str
) -> None:
    from adcp.server.test_controller import TestControllerStore, register_test_controller

    class PinnedSeller(VersionSelectionSeller):
        def get_adcp_version(self) -> str:
            return pin

    selection = ["3.2", "3.2-rc.7"]
    store = TestControllerStore()
    if leg == "mcp":
        mcp = create_mcp_server(PinnedSeller(), supported_versions=selection)
        register_test_controller(mcp, store)
        caller = mcp._tool_manager._tools["comply_test_controller"].fn
        response = await caller(scenario="list_scenarios", account={"sandbox": True})
    else:
        executor = ADCPAgentExecutor(
            PinnedSeller(), test_controller=store, supported_versions=selection
        )
        response = await executor._tool_callers["comply_test_controller"](
            {"scenario": "list_scenarios", "account": {"sandbox": True}}
        )
    assert response["success"] is True
