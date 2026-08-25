"""AdCP 3.2 compact lifecycle and 3.x compatibility matrix."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import pytest

from adcp import ADCPClient
from adcp._idempotency import is_mutating
from adcp._version import resolve_adcp_version
from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import LifecycleTool, MediaBuy
from adcp.decisioning.dispatch import validate_platform
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.types import AdcpError
from adcp.server.base import ToolContext
from adcp.types import (
    AcceptProposalRequest,
    AcceptProposalResponse,
    AgentConfig,
    BuyProductsRequest,
    BuyProductsResponse,
    ControlMediaBuyRequest,
    ControlMediaBuyResponse,
    DeclineProposalsRequest,
    DeclineProposalsResponse,
    ListProductsRequest,
    ListProductsResponse,
    Protocol,
    RefineProposalsRequest,
    RefineProposalsResponse,
    RequestProposalsRequest,
    RequestProposalsResponse,
)
from adcp.types.core import TaskResult, TaskStatus

COMPACT_TASKS = {
    "list_products",
    "request_proposals",
    "refine_proposals",
    "decline_proposals",
    "buy_products",
    "accept_proposal",
    "control_media_buy",
}
PROPOSAL_TASKS = COMPACT_TASKS - {"buy_products"}
STATEFUL_COMPACT_TASKS = COMPACT_TASKS - {"list_products"}
LEGACY_LIFECYCLE_TASKS = {"get_products", "create_media_buy", "update_media_buy"}


class _LegacySalesPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="matrix-account")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "mb-legacy"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}


class _DirectLifecyclePlatform(_LegacySalesPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        media_buy=MediaBuy(
            lifecycle_tools=[
                LifecycleTool.list_products,
                LifecycleTool.buy_products,
                LifecycleTool.control_media_buy,
            ]
        ),
    )

    def list_products(self, req, ctx):
        return {"status": "completed", "products": []}

    def buy_products(self, req, ctx):
        return {"status": "completed", "media_buy_id": "mb-direct"}

    def control_media_buy(self, req, ctx):
        return {"status": "completed", "media_buy_id": "mb-direct"}


class _ProposalLifecyclePlatform(_DirectLifecyclePlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-proposal-mode"],
        media_buy=MediaBuy(
            lifecycle_tools=[LifecycleTool(tool) for tool in sorted(PROPOSAL_TASKS)]
        ),
    )

    def request_proposals(self, req, ctx):
        return {"status": "completed", "proposals": []}

    def refine_proposals(self, req, ctx):
        return {"status": "completed", "proposals": []}

    def decline_proposals(self, req, ctx):
        return {"status": "completed", "declined": []}

    def accept_proposal(self, req, ctx):
        return {"status": "completed", "media_buy_id": "mb-proposal"}


@pytest.mark.parametrize(
    ("version", "variant", "expected_tasks"),
    [
        ("3.0", "legacy", LEGACY_LIFECYCLE_TASKS),
        ("3.1", "legacy", LEGACY_LIFECYCLE_TASKS),
        ("3.2-beta.6", "legacy", LEGACY_LIFECYCLE_TASKS),
        (
            "3.2-beta.6",
            "direct",
            LEGACY_LIFECYCLE_TASKS | {"list_products", "buy_products", "control_media_buy"},
        ),
        ("3.2-beta.6", "proposal", LEGACY_LIFECYCLE_TASKS | PROPOSAL_TASKS),
    ],
)
def test_protocol_lifecycle_matrix(version: str, variant: str, expected_tasks: set[str]) -> None:
    """Protocol version and lifecycle shape are independent matrix axes."""
    resolved = resolve_adcp_version(version)
    assert resolved.startswith(version)
    assert LEGACY_LIFECYCLE_TASKS <= expected_tasks
    if variant == "legacy":
        assert expected_tasks.isdisjoint(COMPACT_TASKS)
    elif variant == "direct":
        assert "request_proposals" not in expected_tasks
        assert {"list_products", "buy_products", "control_media_buy"} <= expected_tasks
    else:
        assert PROPOSAL_TASKS <= expected_tasks
        assert "buy_products" not in expected_tasks


def test_decisioning_advertises_only_declared_compact_variant() -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        legacy = PlatformHandler(
            _LegacySalesPlatform(), executor=executor, registry=InMemoryTaskRegistry()
        )
        direct = PlatformHandler(
            _DirectLifecyclePlatform(), executor=executor, registry=InMemoryTaskRegistry()
        )
        proposal = PlatformHandler(
            _ProposalLifecyclePlatform(), executor=executor, registry=InMemoryTaskRegistry()
        )

        assert legacy.get_advertised_tools().isdisjoint(COMPACT_TASKS)
        assert direct.get_advertised_tools() & COMPACT_TASKS == {
            "list_products",
            "buy_products",
            "control_media_buy",
        }
        assert proposal.get_advertised_tools() & COMPACT_TASKS == PROPOSAL_TASKS


def test_decisioning_rejects_claimed_lifecycle_tool_without_method() -> None:
    class _Invalid(_LegacySalesPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            media_buy=MediaBuy(lifecycle_tools=[LifecycleTool.buy_products]),
        )

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_Invalid())
    assert {item["method"] for item in exc_info.value.details["missing"]} >= {"buy_products"}


@pytest.mark.asyncio
async def test_decisioning_dispatches_compact_task_with_resolved_account() -> None:
    platform = _DirectLifecyclePlatform()
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())
        response = await handler.list_products(ListProductsRequest.model_construct(), ToolContext())
    assert response == {"status": "completed", "products": []}


@pytest.mark.parametrize(
    ("task_name", "request_type", "response_type"),
    [
        ("list_products", ListProductsRequest, ListProductsResponse),
        ("request_proposals", RequestProposalsRequest, RequestProposalsResponse),
        ("refine_proposals", RefineProposalsRequest, RefineProposalsResponse),
        ("decline_proposals", DeclineProposalsRequest, DeclineProposalsResponse),
        ("buy_products", BuyProductsRequest, BuyProductsResponse),
        ("accept_proposal", AcceptProposalRequest, AcceptProposalResponse),
        ("control_media_buy", ControlMediaBuyRequest, ControlMediaBuyResponse),
    ],
)
@pytest.mark.asyncio
async def test_client_routes_each_compact_task_to_same_named_transport(
    task_name: str, request_type: type, response_type: object
) -> None:
    client = ADCPClient(
        AgentConfig(id="compact", agent_uri="https://seller.example", protocol=Protocol.A2A)
    )
    raw = TaskResult(status=TaskStatus.COMPLETED, data={}, success=True)
    parsed = TaskResult(status=TaskStatus.COMPLETED, data=None, success=True)
    transport = AsyncMock(return_value=raw)
    with (
        patch.object(client.adapter, task_name, transport),
        patch.object(client.adapter, "_parse_response", return_value=parsed) as parse,
    ):
        result = await getattr(client, task_name)(request_type.model_construct())
    transport.assert_awaited_once()
    parse.assert_called_once_with(raw, response_type)
    assert result.success is True


def test_stateful_compact_tasks_have_independent_idempotency_identity() -> None:
    assert not is_mutating("list_products")
    assert all(is_mutating(task_name) for task_name in STATEFUL_COMPACT_TASKS)
