"""``control_media_buy`` refuses an undeclared creative start policy (#1869).

AdCP 3.3: a seller without ``media_buy.features.creative_start_policy: true``
MUST reject ``packages[].start_requires_all_creatives_approved: true`` with
``UNSUPPORTED_FEATURE`` before mutation; ``false`` is always allowed. The
dispatcher enforces it from the seller's own declared capabilities.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from adcp import ADCPClient
from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import Features, LifecycleTool, MediaBuy, SupportedProtocol
from adcp.decisioning.handler import PlatformHandler
from adcp.exceptions import ADCPTaskError
from adcp.server import ADCPHandler, create_mcp_server
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import capabilities_response
from adcp.types import ControlMediaBuyRequest
from adcp.validation import ValidationHookConfig

FIELD = "start_requires_all_creatives_approved"


def _request(*policies: bool | None, adcp_version: str = "3.3-beta.5") -> dict[str, Any]:
    packages = []
    for index, policy in enumerate(policies):
        package: dict[str, Any] = {"package_id": f"pkg_{index}"}
        if policy is not None:
            package[FIELD] = policy
        packages.append(package)
    return {
        "adcp_version": adcp_version,
        "idempotency_key": "a" * 16,
        "account": {"account_id": "acct_1"},
        "media_buy_id": "mb_1",
        "revision": 1,
        "packages": packages,
    }


def _capabilities(*, creative_start_policy: bool | None = None) -> dict[str, Any]:
    response = capabilities_response(["media_buy"])
    response["adcp"]["idempotency"] = {"supported": False}
    if creative_start_policy is not None:
        response["media_buy"]["features"]["creative_start_policy"] = creative_start_policy
    return response


class _StartPolicySeller(ADCPHandler[Any]):
    """A 3.3 seller; ``capabilities`` is what its discovery answers, if anything."""

    advertised_tools = {"get_adcp_capabilities", "control_media_buy"}

    def __init__(self, capabilities: dict[str, Any] | None = None) -> None:
        self.capabilities = capabilities
        self.discovery_calls = 0
        self.received: list[Any] = []

    async def get_adcp_capabilities(self, params, context=None):
        self.discovery_calls += 1
        if self.capabilities is None:
            return await super().get_adcp_capabilities(params, context)
        return self.capabilities

    async def control_media_buy(self, params, context=None):
        self.received.append(params)
        return {"status": "completed", "media_buy_id": "mb_1", "revision": 2}


def _assert_refused(exc: ADCPTaskError, *fields: str) -> None:
    [error] = exc.errors
    assert error.code == "UNSUPPORTED_FEATURE"
    assert error.recovery == "correctable"
    assert error.field == fields[0]
    assert error.mutation_outcome == "not_applied"
    assert error.details == {
        "feature": "media_buy.features.creative_start_policy",
        "fields": list(fields),
    }


@pytest.mark.asyncio
async def test_issue_repro_refuses_true_before_the_handler_runs() -> None:
    seller = _StartPolicySeller()
    call = create_tool_caller(seller, "control_media_buy")

    with pytest.raises(ADCPTaskError) as raised:
        await call(_request(True))

    _assert_refused(raised.value, f"packages[0].{FIELD}")
    assert seller.received == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [None, _capabilities(), _capabilities(creative_start_policy=False)],
    ids=["no-discovery", "no-feature", "feature-false"],
)
async def test_true_is_refused_unless_the_seller_declares_the_feature(capabilities) -> None:
    seller = _StartPolicySeller(capabilities)

    with pytest.raises(ADCPTaskError) as raised:
        await create_tool_caller(seller, "control_media_buy")(_request(True))

    _assert_refused(raised.value, f"packages[0].{FIELD}")
    assert seller.received == []


@pytest.mark.asyncio
async def test_true_reaches_a_seller_that_declares_the_feature() -> None:
    seller = _StartPolicySeller(_capabilities(creative_start_policy=True))

    await create_tool_caller(seller, "control_media_buy")(_request(True))

    [received] = seller.received
    assert received["packages"][0][FIELD] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("policies", [(False,), (None,), (False, None)])
async def test_false_or_omitted_is_always_allowed_without_reading_capabilities(policies) -> None:
    seller = _StartPolicySeller()

    await create_tool_caller(seller, "control_media_buy")(_request(*policies))

    assert len(seller.received) == 1
    assert seller.discovery_calls == 0


@pytest.mark.asyncio
async def test_field_names_the_first_opted_in_package_and_details_list_all() -> None:
    seller = _StartPolicySeller()

    with pytest.raises(ADCPTaskError) as raised:
        await create_tool_caller(seller, "control_media_buy")(_request(False, True, None, True))

    _assert_refused(raised.value, f"packages[1].{FIELD}", f"packages[3].{FIELD}")
    assert seller.received == []


@pytest.mark.asyncio
async def test_a_handler_on_3_2_is_not_gated() -> None:
    # The field is a 3.3 addition; a 3.2 seller's open schema ignores it (DR-0009).
    seller = _StartPolicySeller()

    await create_tool_caller(seller, "control_media_buy", adcp_version="3.2")(
        _request(True, adcp_version="3.2")
    )

    assert len(seller.received) == 1
    assert seller.discovery_calls == 0


@pytest.mark.asyncio
async def test_create_and_update_media_buy_are_not_gated() -> None:
    # AdCP 3.3 withdraws the field from create/update packages (adcp#8767).
    received: list[str] = []

    class _LegacySeller(ADCPHandler[Any]):
        async def update_media_buy(self, params, context=None):
            received.append("update_media_buy")
            return {"media_buy_id": "mb_1", "status": "active"}

    await create_tool_caller(_LegacySeller(), "update_media_buy")(
        {
            "adcp_version": "3.3-beta.5",
            "idempotency_key": "a" * 16,
            "account": {"account_id": "acct_1"},
            "media_buy_id": "mb_1",
            "packages": [{"package_id": "pkg_0", FIELD: True}],
        }
    )

    assert received == ["update_media_buy"]


@pytest.mark.asyncio
async def test_the_refusal_reaches_a_buyer_over_mcp() -> None:
    seller = _StartPolicySeller(_capabilities())
    server = create_mcp_server(
        seller, validation=ValidationHookConfig(requests="strict", responses="strict")
    )
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
                True,
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                client = ADCPClient.from_mcp_client(session)
                result = await client.control_media_buy(
                    ControlMediaBuyRequest.model_validate(_request(True))
                )
            task_group.cancel_scope.cancel()

    assert not result.success
    assert result.adcp_error is not None
    assert result.adcp_error["code"] == "UNSUPPORTED_FEATURE"
    assert result.adcp_error["field"] == f"packages[0].{FIELD}"
    assert result.mutation_outcome == "not_applied"
    assert seller.received == []


# ---------------------------------------------------------------------------
# Decisioning platforms: the platform's (request-scoped) capabilities decide.
# ---------------------------------------------------------------------------


def _media_buy(*, creative_start_policy: bool | None = None) -> MediaBuy:
    features = (
        None
        if creative_start_policy is None
        else Features(creative_start_policy=creative_start_policy)
    )
    return MediaBuy(lifecycle_tools=[LifecycleTool.control_media_buy], features=features)


class _Platform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        supported_protocols=[SupportedProtocol.media_buy],
        specialisms=["sales-non-guaranteed"],
        media_buy=_media_buy(),
    )
    accounts = SingletonAccounts(account_id="acct_1")

    def __init__(self) -> None:
        self.received: list[ControlMediaBuyRequest] = []

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "mb_1"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}

    def control_media_buy(self, req, ctx):
        self.received.append(req)
        return {"status": "completed", "media_buy_id": "mb_1", "revision": 2}


class _DeclaringPlatform(_Platform):
    capabilities = DecisioningCapabilities(
        supported_protocols=[SupportedProtocol.media_buy],
        specialisms=["sales-non-guaranteed"],
        media_buy=_media_buy(creative_start_policy=True),
    )


class _TenantScopedPlatform(_Platform):
    """Declares the feature only in the capabilities scoped to this request."""

    def __init__(self) -> None:
        super().__init__()
        self.scoped_for: list[Any] = []

    def get_adcp_capabilities_for_request(self, params=None, context=None):
        self.scoped_for.append(params)
        return _DeclaringPlatform.capabilities


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-creative-start-")
    yield pool
    pool.shutdown(wait=True)


def _caller(platform: DecisioningPlatform, executor: ThreadPoolExecutor):
    handler = PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())
    return create_tool_caller(handler, "control_media_buy")


@pytest.mark.asyncio
async def test_platform_without_the_feature_refuses_true(executor) -> None:
    platform = _Platform()

    with pytest.raises(ADCPTaskError) as raised:
        await _caller(platform, executor)(_request(True))

    _assert_refused(raised.value, f"packages[0].{FIELD}")
    assert platform.received == []


@pytest.mark.asyncio
async def test_platform_without_the_feature_accepts_false(executor) -> None:
    platform = _Platform()

    await _caller(platform, executor)(_request(False))

    assert len(platform.received) == 1


@pytest.mark.asyncio
async def test_platform_declaring_the_feature_receives_true(executor) -> None:
    platform = _DeclaringPlatform()

    await _caller(platform, executor)(_request(True))

    [received] = platform.received
    assert received.packages[0].start_requires_all_creatives_approved is True


@pytest.mark.asyncio
async def test_request_scoped_capabilities_decide(executor) -> None:
    platform = _TenantScopedPlatform()

    await _caller(platform, executor)(_request(True))

    assert len(platform.received) == 1
    assert isinstance(platform.scoped_for[0], ControlMediaBuyRequest)
