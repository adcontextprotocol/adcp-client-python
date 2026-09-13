"""AdCP 3.2 compact lifecycle and 3.x compatibility matrix."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

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
from adcp.server import ADCPHandler, create_mcp_server
from adcp.server.base import ToolContext
from adcp.server.mcp_tools import _normalize_response_envelope
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
from adcp.validation import ValidationHookConfig, validate_response
from adcp.validation._response_envelope import uses_task_status_envelope

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
        return {
            "outcome": "listed",
            "products": [],
            "feed_version": "fixture-feed",
            "cache_scope": "public",
        }

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
        return {
            "outcome": "rejected",
            "reason": "No matching inventory",
            "status": "completed",
        }

    def refine_proposals(self, req, ctx):
        return {
            "results": [
                {
                    "source_proposal_id": "proposal-1",
                    "outcome": "unable",
                    "reason_code": "source_unavailable",
                    "reason": "The source proposal is unavailable",
                }
            ],
            "products": [],
            "status": "completed",
        }

    def decline_proposals(self, req, ctx):
        return {
            "results": [
                {
                    "proposal_id": "proposal-1",
                    "outcome": "declined",
                }
            ]
        }

    def accept_proposal(self, req, ctx):
        return {"status": "completed", "media_buy_id": "mb-proposal"}


class _NativeCompactEnvelopeHandler(ADCPHandler[Any]):
    advertised_tools = {"list_products", "decline_proposals"}

    def get_adcp_version(self) -> str:
        return "3.2.0-rc.1"

    async def list_products(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return {
            "outcome": "listed",
            "products": [],
            "feed_version": "fixture-feed",
            "cache_scope": "public",
        }

    async def decline_proposals(
        self, params: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        return {
            "results": [
                {
                    "proposal_id": "proposal-1",
                    "outcome": "declined",
                }
            ]
        }


@pytest.mark.parametrize(
    ("version", "variant", "expected_tasks"),
    [
        ("3.0", "legacy", LEGACY_LIFECYCLE_TASKS),
        ("3.1", "legacy", LEGACY_LIFECYCLE_TASKS),
        ("3.2-rc.2", "legacy", LEGACY_LIFECYCLE_TASKS),
        (
            "3.2-rc.2",
            "direct",
            LEGACY_LIFECYCLE_TASKS | {"list_products", "buy_products", "control_media_buy"},
        ),
        ("3.2-rc.2", "proposal", LEGACY_LIFECYCLE_TASKS | PROPOSAL_TASKS),
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
    assert response == {
        "outcome": "listed",
        "products": [],
        "feed_version": "fixture-feed",
        "cache_scope": "public",
    }


@pytest.mark.asyncio
async def test_native_compact_mcp_roundtrip_preserves_response_envelopes() -> None:
    """The server and MCP client's output validators accept both native arms."""
    server = create_mcp_server(
        _NativeCompactEnvelopeHandler(),
        name="native-compact-envelopes",
        validation=ValidationHookConfig(requests="off", responses="strict"),
    )

    expected_responses = {
        "list_products": {
            "outcome": "listed",
            "products": [],
            "feed_version": "fixture-feed",
            "cache_scope": "public",
        },
        "decline_proposals": {
            "results": [
                {
                    "proposal_id": "proposal-1",
                    "outcome": "declined",
                }
            ]
        },
    }

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            # MCP 2.2 exposes the paired memory streams but no public helper
            # that runs an MCPServer on them, so this is the narrow integration
            # seam needed to exercise ClientSession's real output validator.
            task_group.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
                True,
            )
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                tools = await client.list_tools()
                advertised = {tool.name: tool for tool in tools.tools}
                results = {}
                for task_name in expected_responses:
                    assert advertised[task_name].output_schema
                    results[task_name] = await client.call_tool(
                        task_name,
                        {"adcp_version": "3.2-rc.2"},
                    )
            task_group.cancel_scope.cancel()

    for task_name, expected in expected_responses.items():
        assert results[task_name].is_error is False
        assert results[task_name].structured_content == expected


@pytest.mark.parametrize(
    ("task_name", "response"),
    [
        (
            "list_products",
            {
                "outcome": "listed",
                "products": [],
                "feed_version": "fixture-feed",
                "cache_scope": "public",
            },
        ),
        (
            "request_proposals",
            {"outcome": "rejected", "reason": "No matching inventory"},
        ),
        (
            "refine_proposals",
            {
                "results": [
                    {
                        "source_proposal_id": "proposal-1",
                        "outcome": "unable",
                        "reason_code": "source_unavailable",
                        "reason": "The source proposal is unavailable",
                    }
                ],
                "products": [],
            },
        ),
        (
            "decline_proposals",
            {
                "results": [
                    {
                        "proposal_id": "proposal-1",
                        "outcome": "declined",
                    }
                ]
            },
        ),
    ],
)
def test_compact_response_enrichment_matches_pinned_schema(
    task_name: str,
    response: dict[str, Any],
) -> None:
    uses_status = uses_task_status_envelope(task_name)
    with_completed_status = {**response, "status": "completed"}

    assert (
        validate_response(
            task_name,
            with_completed_status,
            version="3.2-rc.2",
        ).valid
        is uses_status
    )

    _normalize_response_envelope(task_name, response, {}, adcp_version="3.2-rc.2")

    assert (response.get("status") == "completed") is uses_status
    assert validate_response(task_name, response, version="3.2-rc.2").valid


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
