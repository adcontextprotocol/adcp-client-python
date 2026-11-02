"""AdCP 3.3 ``adcp.implementation.sdks[]`` self-report (#1646).

``serve()`` stamps ``{"name": "adcp", "version": <installed>, "components":
[...]}`` on ``get_adcp_capabilities`` responses served at 3.3 or later, listing
only components the server provably wires agent-wide. These tests drive the
reporter through ``create_tool_caller`` -- the dispatch path ``serve()`` uses on
both MCP and A2A -- so version negotiation, 3.3 -> 3.2 conversion and strict
response validation all run on the stamped response.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import (
    Account,
    Adcp,
    IdempotencySupported,
    Implementation,
    ImplementationComponent,
    RequestSigning,
    Sdk,
    SupportedProtocol,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.server import ADCPHandler, ServeConfig, ToolContext
from adcp.server.auth import BearerTokenAuth
from adcp.server.idempotency import IdempotencyStore, MemoryBackend
from adcp.server.implementation import (
    SDK_PACKAGE_NAME,
    ImplementationReport,
    ImplementationReporter,
    adcp_sdk_entry,
    resolve_implementation_report,
    sdk_package_version,
)
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import capabilities_response
from adcp.types.domains.protocol.get_adcp_capabilities_response import (
    Component,
    GetAdcpCapabilitiesResponse,
)
from adcp.validation.client_hooks import SERVER_DEFAULT_VALIDATION, ValidationHookConfig

# A served set without the 2.5 legacy adapter, whose translated responses are
# not re-validated (so the default set never earns ``schema_validation``).
MODERN = ("3.2", "3.3-beta.3")
serve_module = importlib.import_module("adcp.server.serve")
INSTALLED = importlib.metadata.version("adcp")
_NO_IDEMPOTENCY = {"supported": False}


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-implementation-")
    yield pool
    pool.shutdown(wait=True)


class _PlainAgent(ADCPHandler):
    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return capabilities_response(["media_buy"], idempotency=_NO_IDEMPOTENCY)

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"products": []}


class _DeclaringAgent(_PlainAgent):
    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return capabilities_response(
            ["media_buy"],
            idempotency=_NO_IDEMPOTENCY,
            implementation={
                "sdks": [{"name": "my-fork", "version": "1.2.3", "components": ["types"]}]
            },
        )


class _ListsOnlyAgent(ADCPHandler):
    """No task accepts a webhook, so webhook signing is inapplicable."""

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        response = capabilities_response(["governance"], idempotency=_NO_IDEMPOTENCY)
        response["request_signing"] = {
            "supported": True,
            "required_for": [],
            "covers_content_digest": "required",
        }
        return response

    async def get_property_list(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {}


_store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)


def _signals_capabilities(**overrides: Any) -> DecisioningCapabilities:
    fields: dict[str, Any] = {
        "specialisms": ["signal-marketplace"],
        "supported_protocols": [SupportedProtocol.signals],
        "account": Account(supported_billing=["agent"]),
        "adcp": Adcp(
            major_versions=[3],
            idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
        ),
    }
    fields.update(overrides)
    return DecisioningCapabilities(**fields)


class _WrappedSignals(DecisioningPlatform):
    capabilities = _signals_capabilities()
    accounts = SingletonAccounts(account_id="test")

    async def get_signals(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"signals": []}

    @_store.wrap
    async def activate_signal(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {}


class _UnwrappedSignals(_WrappedSignals):
    async def activate_signal(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {}


class _ExternalIdempotencySignals(_WrappedSignals):
    _adcp_idempotency_external = True


class _DeclaredSignals(_WrappedSignals):
    capabilities = _signals_capabilities(
        adcp=Adcp(
            major_versions=[3],
            idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
            implementation=Implementation(
                sdks=[
                    Sdk(
                        name="adcp",
                        version="9.9.9",
                        components=[ImplementationComponent.transport],
                    )
                ]
            ),
        )
    )


def _platform_handler(platform: DecisioningPlatform, executor: ThreadPoolExecutor) -> Any:
    return PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())


def _reporter(
    handler: Any,
    *,
    report: ImplementationReport | None = None,
    transport: str = "streamable-http",
    validation: ValidationHookConfig | None = SERVER_DEFAULT_VALIDATION,
    supported_versions: Sequence[str] | None = MODERN,
    auth: BearerTokenAuth | None = None,
    request_signature_verification: Any = None,
    then: Any = None,
) -> ImplementationReporter:
    return ImplementationReporter(
        handler,
        report=report or ImplementationReport(),
        transport=transport,
        validation=validation,
        supported_versions=supported_versions,
        auth=auth,
        request_signature_verification=request_signature_verification,
        advertise_all=False,
        then=then,
    )


def _capabilities(
    handler: Any,
    reporter: ImplementationReporter | None,
    params: dict[str, Any] | None = None,
    *,
    supported_versions: Sequence[str] | None = MODERN,
    validation: ValidationHookConfig | None = SERVER_DEFAULT_VALIDATION,
) -> dict[str, Any]:
    call = create_tool_caller(
        handler,
        "get_adcp_capabilities",
        validation=validation,
        response_enhancer=reporter,
        supported_versions=supported_versions,
    )
    result: dict[str, Any] = asyncio.run(call(dict(params or {})))
    return result


def _sdks(response: dict[str, Any]) -> list[dict[str, Any]]:
    sdks: list[dict[str, Any]] = response["adcp"]["implementation"]["sdks"]
    return sdks


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------


def test_component_order_matches_generated_enum() -> None:
    from adcp.server import implementation

    assert implementation._COMPONENT_ORDER == tuple(member.value for member in Component)


def test_entry_names_the_distribution_and_its_exact_version() -> None:
    entry = adcp_sdk_entry(["transport", Component.types])

    assert entry == {
        "name": SDK_PACKAGE_NAME,
        "version": INSTALLED,
        "components": ["types", "transport"],
    }
    # Same name ``pyproject.toml`` publishes and ``importlib.metadata`` resolves.
    assert importlib.metadata.metadata(SDK_PACKAGE_NAME)["Name"] == "adcp"


def test_entry_rejects_unknown_component() -> None:
    with pytest.raises(ValueError, match="unknown adcp.implementation component"):
        adcp_sdk_entry(["telemetry"])


@pytest.mark.parametrize(
    "resolved",
    ["10.0.0 b5", "10.0.0b5.dev123+g0123456789abcdef.d20261101"],
)
def test_version_outside_schema_pattern_reports_nothing(resolved: str) -> None:
    with patch("importlib.metadata.version", return_value=resolved):
        assert sdk_package_version() is None
        assert adcp_sdk_entry() is None


def test_missing_distribution_reports_nothing() -> None:
    with patch(
        "importlib.metadata.version",
        side_effect=importlib.metadata.PackageNotFoundError("adcp"),
    ):
        assert sdk_package_version() is None


def test_report_option_normalization() -> None:
    assert resolve_implementation_report(False) is None
    assert resolve_implementation_report(None) is None
    assert resolve_implementation_report(True) == ImplementationReport()
    custom = ImplementationReport(extra_components=["lifecycle"])
    assert resolve_implementation_report(custom) is custom
    with pytest.raises(TypeError):
        resolve_implementation_report("yes")  # type: ignore[arg-type]


def test_report_refuses_own_name_and_unknown_components() -> None:
    with pytest.raises(ValueError, match="may not list 'adcp'"):
        ImplementationReport(extra_sdks=[{"name": "adcp", "version": "1", "components": []}])
    with pytest.raises(ValueError, match="unknown adcp.implementation component"):
        ImplementationReport(extra_components=["everything"])
    with pytest.raises(ValueError):
        ImplementationReport(extra_sdks=[{"name": "has space", "version": "1", "components": []}])


# ---------------------------------------------------------------------------
# Emission on the plain ADCPHandler path
# ---------------------------------------------------------------------------


def test_plain_handler_reports_transport_and_strict_validation() -> None:
    handler = _PlainAgent()
    response = _capabilities(handler, _reporter(handler))

    assert _sdks(response) == [
        {"name": "adcp", "version": INSTALLED, "components": ["schema_validation", "transport"]}
    ]
    # Valid against the generated 3.3 model (strict validation also passed).
    parsed = GetAdcpCapabilitiesResponse.model_validate(response)
    assert parsed.adcp.implementation is not None
    assert parsed.adcp.implementation.sdks[0].components == [
        Component.schema_validation,
        Component.transport,
    ]


@pytest.mark.parametrize(
    "validation",
    [
        None,
        ValidationHookConfig(requests="strict", responses="warn"),
        ValidationHookConfig(requests="warn", responses="strict"),
        ValidationHookConfig(requests="strict", responses="off"),
    ],
)
def test_partial_or_warn_only_validation_is_not_claimed(
    validation: ValidationHookConfig | None,
) -> None:
    handler = _PlainAgent()
    response = _capabilities(handler, _reporter(handler, validation=validation))

    assert _sdks(response)[0]["components"] == ["transport"]


def test_serving_the_legacy_adapter_forfeits_schema_validation() -> None:
    handler = _PlainAgent()
    response = _capabilities(
        handler, _reporter(handler, supported_versions=None), supported_versions=None
    )

    assert _sdks(response)[0]["components"] == ["transport"]


def test_bearer_auth_covering_every_task_is_claimed() -> None:
    handler = _PlainAgent()
    auth = BearerTokenAuth(validate_token=lambda token: None)
    response = _capabilities(handler, _reporter(handler, auth=auth))

    assert "authentication" in _sdks(response)[0]["components"]


@pytest.mark.parametrize(
    ("auth", "transport"),
    [
        (BearerTokenAuth(validate_token=lambda token: None, allow_unauthenticated=True), "a2a"),
        (
            BearerTokenAuth(
                validate_token=lambda token: None,
                mcp_discovery_tools={"get_adcp_capabilities", "get_products"},
            ),
            "streamable-http",
        ),
        (BearerTokenAuth(validate_token=lambda token: None), "stdio"),
    ],
)
def test_auth_with_gaps_is_not_claimed(auth: BearerTokenAuth, transport: str) -> None:
    handler = _PlainAgent()
    response = _capabilities(handler, _reporter(handler, auth=auth, transport=transport))

    assert "authentication" not in _sdks(response)[0]["components"]


def test_signing_needs_verification_declaration_and_no_unsigned_webhooks() -> None:
    lists_only = _ListsOnlyAgent()
    verifier = object()
    response = _capabilities(
        lists_only, _reporter(lists_only, request_signature_verification=verifier)
    )
    assert "signing" in _sdks(response)[0]["components"]

    # Without the verifier, the declaration alone does not earn it.
    response = _capabilities(lists_only, _reporter(lists_only))
    assert "signing" not in _sdks(response)[0]["components"]

    # get_products accepts push_notification_config: a plain handler's
    # webhooks are not SDK-delivered, so signing coverage is unproven.
    plain = _PlainAgent()
    response = _capabilities(plain, _reporter(plain, request_signature_verification=verifier))
    assert "signing" not in _sdks(response)[0]["components"]


def test_extra_components_and_sdks_are_appended() -> None:
    handler = _PlainAgent()
    other = {"name": "@acme/adcp-tools", "version": "2.0.0", "components": []}
    report = ImplementationReport(extra_components=["lifecycle"], extra_sdks=[other])
    response = _capabilities(handler, _reporter(handler, report=report))

    assert _sdks(response) == [
        {
            "name": "adcp",
            "version": INSTALLED,
            "components": ["schema_validation", "transport", "lifecycle"],
        },
        other,
    ]
    GetAdcpCapabilitiesResponse.model_validate(response)


# ---------------------------------------------------------------------------
# Override, opt-out and version projection
# ---------------------------------------------------------------------------


def test_declared_implementation_is_never_overwritten() -> None:
    handler = _DeclaringAgent()
    response = _capabilities(handler, _reporter(handler))

    assert _sdks(response) == [{"name": "my-fork", "version": "1.2.3", "components": ["types"]}]


def test_no_reporter_means_no_block() -> None:
    handler = _PlainAgent()
    response = _capabilities(handler, None)

    assert "implementation" not in response["adcp"]


@pytest.mark.parametrize("claimed", ["3.2", "3.1"])
def test_pre_3_3_callers_never_see_the_block(claimed: str) -> None:
    handler = _PlainAgent()
    versions = ("3.0", "3.1", "3.2", "3.3-beta.3")
    response = _capabilities(
        handler,
        _reporter(handler, supported_versions=versions),
        {"adcp_version": claimed},
        supported_versions=versions,
    )

    assert "implementation" not in response["adcp"]


def test_3_3_caller_sees_the_block() -> None:
    handler = _PlainAgent()
    response = _capabilities(handler, _reporter(handler), {"adcp_version": "3.3-beta.3"})

    assert _sdks(response)[0]["name"] == "adcp"


def test_server_pinned_to_3_2_never_emits() -> None:
    class _Pinned(_PlainAgent):
        _adcp_version = "3.2"

        def get_adcp_version(self) -> str:
            return "3.2"

    handler = _Pinned()
    response = _capabilities(handler, _reporter(handler), supported_versions=("3.2",))

    assert "implementation" not in response["adcp"]


def test_adopter_enhancer_runs_after_and_may_amend() -> None:
    seen: list[Any] = []

    def enhancer(method: str, result: dict[str, Any], context: ToolContext | None) -> None:
        if method == "get_adcp_capabilities":
            seen.append(result["adcp"].get("implementation"))
            result["adcp"]["implementation"]["sdks"][0]["components"].append("lifecycle")

    handler = _PlainAgent()
    response = _capabilities(handler, _reporter(handler, then=enhancer))

    assert seen and seen[0]["sdks"][0]["name"] == "adcp"
    assert _sdks(response)[0]["components"][-1] == "lifecycle"


def test_reporter_failure_still_runs_adopter_enhancer() -> None:
    def enhancer(result: dict[str, Any]) -> None:
        result["x_stamp"] = True

    handler = _PlainAgent()
    reporter = _reporter(handler, then=enhancer)
    with patch.object(ImplementationReporter, "components", side_effect=RuntimeError("boom")):
        response = _capabilities(handler, reporter)

    assert "implementation" not in response["adcp"]
    assert response["x_stamp"] is True


def test_reporter_ignores_other_tools_and_error_envelopes() -> None:
    handler = _PlainAgent()
    reporter = _reporter(handler)
    context = ToolContext(application_adcp_version="3.3-beta.3")
    other = {"adcp": {"major_versions": [3]}}
    reporter("get_products", other, context)
    error = {"adcp_error": {"code": "X"}, "adcp": {"major_versions": [3]}}
    reporter("get_adcp_capabilities", error, context)

    assert "implementation" not in other["adcp"]
    assert "implementation" not in error["adcp"]


# ---------------------------------------------------------------------------
# Decisioning platforms
# ---------------------------------------------------------------------------


def test_platform_with_every_mutating_task_wrapped_claims_idempotency(
    executor: ThreadPoolExecutor,
) -> None:
    handler = _platform_handler(_WrappedSignals(), executor)
    response = _capabilities(handler, _reporter(handler))

    assert _sdks(response)[0]["components"] == [
        "types",
        "schema_validation",
        "transport",
        "idempotency",
    ]
    GetAdcpCapabilitiesResponse.model_validate(response)


@pytest.mark.parametrize("platform_type", [_UnwrappedSignals, _ExternalIdempotencySignals])
def test_unproven_idempotency_is_not_claimed(
    platform_type: type[DecisioningPlatform], executor: ThreadPoolExecutor
) -> None:
    handler = _platform_handler(platform_type(), executor)
    response = _capabilities(handler, _reporter(handler))

    assert _sdks(response)[0]["components"] == ["types", "schema_validation", "transport"]


def test_platform_declared_implementation_wins(executor: ThreadPoolExecutor) -> None:
    handler = _platform_handler(_DeclaredSignals(), executor)
    response = _capabilities(handler, _reporter(handler))

    assert _sdks(response) == [{"name": "adcp", "version": "9.9.9", "components": ["transport"]}]


def test_platform_without_sdk_task_outbox_does_not_claim_webhooks_or_signing(
    executor: ThreadPoolExecutor,
) -> None:
    class _SigningSignals(_WrappedSignals):
        capabilities = _signals_capabilities(
            request_signing=RequestSigning(supported=True, required_for=[])
        )

    handler = _platform_handler(_SigningSignals(), executor)
    response = _capabilities(handler, _reporter(handler, request_signature_verification=object()))
    components = _sdks(response)[0]["components"]

    # activate_signal / get_signals accept push_notification_config, and no
    # SDK outbox owns their delivery.
    assert "webhooks" not in components
    assert "signing" not in components


def test_direct_handler_call_is_unknown(executor: ThreadPoolExecutor) -> None:
    """Outside ``serve()`` nothing proves transport ownership: no block."""
    handler = _platform_handler(_WrappedSignals(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert "implementation" not in response["adcp"]


# ---------------------------------------------------------------------------
# serve() wiring
# ---------------------------------------------------------------------------


def _served_enhancer(**serve_kwargs: Any) -> Any:
    with patch.object(serve_module, "_serve_mcp") as mock_mcp:
        serve_module.serve(_PlainAgent(), **serve_kwargs)
    mock_mcp.assert_called_once()
    return mock_mcp.call_args.kwargs["response_enhancer"]


def test_serve_installs_the_reporter_by_default() -> None:
    def adopter(result: dict[str, Any]) -> None:
        return None

    enhancer = _served_enhancer(response_enhancer=adopter)

    assert isinstance(enhancer, ImplementationReporter)
    assert enhancer._then is adopter


def test_serve_opt_out_leaves_the_adopter_enhancer_alone() -> None:
    def adopter(result: dict[str, Any]) -> None:
        return None

    assert _served_enhancer(report_implementation=False, response_enhancer=adopter) is adopter
    assert _served_enhancer(report_implementation=False) is None
    assert (
        _served_enhancer(config=ServeConfig(report_implementation=False, response_enhancer=adopter))
        is adopter
    )


def test_serve_config_carries_a_custom_report() -> None:
    report = ImplementationReport(extra_components=["lifecycle"])
    enhancer = _served_enhancer(config=ServeConfig(report_implementation=report))

    assert isinstance(enhancer, ImplementationReporter)
    assert enhancer._report is report
