"""Unit tests for adcp.decisioning.handler.PlatformHandler.

Covers the wire-shape shim layer that routes typed Pydantic requests
through dispatch._invoke_platform_method to the adopter's
DecisioningPlatform method bodies.

Each test exercises one shim end-to-end: typed request → account
resolution → RequestContext build → method invocation → typed
response. Errors flow through verbatim (AdcpError) or wrapped
(unexpected exceptions → INTERNAL_ERROR).
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.decisioning import (
    AdcpError,
    AuthInfo,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.server.base import ToolContext


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-handler-")
    yield pool
    pool.shutdown(wait=True)


def _make_handler(platform: DecisioningPlatform, executor: ThreadPoolExecutor) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )


# ---- advertised_tools class attribute ----


def test_advertised_tools_covers_sales_specialism() -> None:
    """The class-level set declares all 9 sales tools — both the 5
    required (every sales-* specialism) and the 4 optional (rc.1+
    sales additions)."""
    assert "get_products" in PlatformHandler.advertised_tools
    assert "create_media_buy" in PlatformHandler.advertised_tools
    assert "update_media_buy" in PlatformHandler.advertised_tools
    assert "sync_creatives" in PlatformHandler.advertised_tools
    assert "get_media_buy_delivery" in PlatformHandler.advertised_tools
    # Optional but covered.
    assert "get_media_buys" in PlatformHandler.advertised_tools
    assert "provide_performance_feedback" in PlatformHandler.advertised_tools
    assert "list_creative_formats" in PlatformHandler.advertised_tools
    assert "list_creatives" in PlatformHandler.advertised_tools


# ---- get_products — sync read, account-bearing wire request ----


@pytest.mark.asyncio
async def test_get_products_routes_through_platform(executor) -> None:
    from adcp.types import GetProductsRequest, GetProductsResponse

    received_account_id: list[str] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            received_account_id.append(ctx.account.id)
            return GetProductsResponse(products=[])

    handler = _make_handler(_Platform(), executor)
    req = GetProductsRequest(buying_mode="brief", brief="any inventory")
    resp = await handler.get_products(req, ToolContext())
    assert isinstance(resp, GetProductsResponse)
    # SingletonAccounts synthesizes per-principal id; with no auth_info
    # the principal is "anonymous".
    assert received_account_id == ["hello:anonymous"]


@pytest.mark.asyncio
async def test_get_products_threads_auth_info_to_account(executor) -> None:
    """ToolContext.metadata['adcp.auth_info'] flows into account
    resolution AND onto the RequestContext.auth_info field."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    received_principal: list[str] = []
    received_auth_info: list[Any] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acme")

        async def get_products(self, req, ctx):
            received_principal.append(ctx.account.id)
            received_auth_info.append(ctx.auth_info)
            return GetProductsResponse(products=[])

    handler = _make_handler(_Platform(), executor)
    ctx = ToolContext(
        metadata={
            "adcp.auth_info": AuthInfo(
                kind="signed_request",
                principal="buyer-x",
                key_id="kid-1",
            ),
        }
    )
    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any inventory"),
        ctx,
    )
    assert received_principal == ["acme:buyer-x"]
    assert received_auth_info[0].principal == "buyer-x"


@pytest.mark.asyncio
async def test_get_products_propagates_adcp_error_verbatim(executor) -> None:
    """Adopter raises AdcpError → flows through dispatch verbatim."""
    from adcp.types import GetProductsRequest

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise AdcpError(
                "POLICY_VIOLATION",
                message="cannot show inventory",
                recovery="terminal",
            )

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"), ToolContext()
        )
    assert exc_info.value.code == "POLICY_VIOLATION"


@pytest.mark.asyncio
async def test_get_products_wraps_unexpected_exception(executor) -> None:
    """Unexpected exception in adopter code → INTERNAL_ERROR."""
    from adcp.types import GetProductsRequest

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def get_products(self, req, ctx):
            raise KeyError("internal")

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc_info:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="any"), ToolContext()
        )
    assert exc_info.value.code == "X_INTERNAL_ERROR"
    # Original exception preserved as __cause__; not exposed in message.
    assert isinstance(exc_info.value.__cause__, KeyError)


# ---- create_media_buy — hybrid, returns Submitted envelope on handoff ----


@pytest.mark.asyncio
async def test_create_media_buy_sync_path_returns_typed_response(executor) -> None:
    from adcp.types import (
        CreateMediaBuyRequest,
        CreateMediaBuySuccessResponse,
    )

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def create_media_buy(self, req, ctx):
            return CreateMediaBuySuccessResponse(
                media_buy_id="mb_xyz",
                packages=[],
                status="active",
            )

    handler = _make_handler(_Platform(), executor)
    resp = await handler.create_media_buy(
        CreateMediaBuyRequest(
            account={"account_id": "acct_a"},
            brand={"domain": "example.com"},
            idempotency_key="idem_aaaa1234567890",
            start_time="2026-05-01T00:00:00Z",
            end_time="2026-05-31T23:59:59Z",
        ),
        ToolContext(),
    )
    assert isinstance(resp, CreateMediaBuySuccessResponse)
    assert resp.media_buy_id == "mb_xyz"


@pytest.mark.asyncio
async def test_create_media_buy_handoff_path_returns_submitted_envelope(
    executor,
) -> None:
    """Adopter returns ctx.handoff_to_task(fn) → handler returns the
    wire Submitted envelope (dict) instead of a Pydantic Success."""
    from adcp.types import CreateMediaBuyRequest

    async def _async_review(task_ctx):
        return {"media_buy_id": "mb_after_review", "status": "active"}

    class _HybridPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def create_media_buy(self, req, ctx):
            return ctx.handoff_to_task(_async_review)

    handler = _make_handler(_HybridPlatform(), executor)
    result = await handler.create_media_buy(
        CreateMediaBuyRequest(
            account={"account_id": "acct_a"},
            brand={"domain": "example.com"},
            idempotency_key="idem_aaaa1234567890",
            start_time="2026-05-01T00:00:00Z",
            end_time="2026-05-31T23:59:59Z",
        ),
        ToolContext(),
    )
    # Wire envelope, not Pydantic. Spec submitted shape is
    # {task_id, status} only.
    assert isinstance(result, dict)
    assert result["status"] == "submitted"
    assert "task_type" not in result


# ---- update_media_buy — arg-projected (media_buy_id, patch, ctx) ----


@pytest.mark.asyncio
async def test_update_media_buy_arg_projects_media_buy_id_and_patch(
    executor,
) -> None:
    """The shim splits UpdateMediaBuyRequest into separate
    media_buy_id + patch kwargs — adopters write
    ``update_media_buy(media_buy_id, patch, ctx)`` with the full
    request as ``patch``."""
    from adcp.types import UpdateMediaBuyRequest, UpdateMediaBuySuccessResponse

    seen_args: dict[str, Any] = {}

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def update_media_buy(self, media_buy_id, patch, ctx):
            seen_args["media_buy_id"] = media_buy_id
            seen_args["patch_paused"] = patch.paused
            return UpdateMediaBuySuccessResponse(
                media_buy_id=media_buy_id,
                status="paused",
                packages=[],
            )

    handler = _make_handler(_Platform(), executor)
    req = UpdateMediaBuyRequest(
        account={"account_id": "acct_a"},
        media_buy_id="mb_1",
        idempotency_key="idem_bbbb1234567890",
        paused=True,
    )
    resp = await handler.update_media_buy(req, ToolContext())
    assert isinstance(resp, UpdateMediaBuySuccessResponse)
    assert seen_args == {"media_buy_id": "mb_1", "patch_paused": True}


# ---- sync_creatives — hybrid for creative review ----


@pytest.mark.asyncio
async def test_sync_creatives_routes_through_platform(executor) -> None:
    from adcp.types import SyncCreativesRequest, SyncCreativesSuccessResponse

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def sync_creatives(self, req, ctx):
            return SyncCreativesSuccessResponse(creatives=[])

    handler = _make_handler(_Platform(), executor)
    # SyncCreativesRequest has tight validation (creatives minItems=1,
    # asset URL+format requirements). The handler-level routing is
    # already covered by get_products / create_media_buy / update_media_buy
    # tests; a simpler invocation via model_construct(_fields_set=None)
    # bypasses the pydantic validator and exercises the dispatch path.
    req = SyncCreativesRequest.model_construct(
        account={"account_id": "acct_a"},
        creatives=[],
        idempotency_key="idem_cccc1234567890",
    )
    resp = await handler.sync_creatives(req, ToolContext())
    assert isinstance(resp, SyncCreativesSuccessResponse)


# ---- no-account tools ----


@pytest.mark.asyncio
async def test_list_creative_formats_resolves_with_no_ref(executor) -> None:
    """Wire request has no ``account`` field; shim passes None to
    AccountStore.resolve. SingletonAccounts handles the None case
    (synthesizes anonymous), so the shim flow works."""
    from adcp.types import ListCreativeFormatsRequest, ListCreativeFormatsResponse

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="hello")

        async def list_creative_formats(self, req, ctx):
            return ListCreativeFormatsResponse(formats=[])

    handler = _make_handler(_Platform(), executor)
    resp = await handler.list_creative_formats(
        ListCreativeFormatsRequest(),
        ToolContext(),
    )
    assert isinstance(resp, ListCreativeFormatsResponse)


# ---- account-resolver Awaitable + sync paths both work ----


@pytest.mark.asyncio
async def test_handler_awaits_async_account_resolver(executor) -> None:
    """Custom AccountStore impls may be async — handler must await."""
    from adcp.decisioning.types import Account
    from adcp.types import GetProductsRequest, GetProductsResponse

    received_id: list[str] = []

    class _AsyncStore:
        resolution = "explicit"

        async def resolve(self, ref, auth_info=None):
            await asyncio.sleep(0)  # actual async work
            return Account(id="async-resolved")

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = _AsyncStore()

        async def get_products(self, req, ctx):
            received_id.append(ctx.account.id)
            return GetProductsResponse(products=[])

    handler = _make_handler(_Platform(), executor)
    await handler.get_products(GetProductsRequest(buying_mode="brief", brief="any"), ToolContext())
    assert received_id == ["async-resolved"]


@pytest.mark.asyncio
async def test_handler_extract_auth_info_from_dict(executor) -> None:
    """Operators populating ctx.metadata['adcp.auth_info'] as a dict
    (instead of an AuthInfo instance — common shape from generic
    middleware) get re-coerced to AuthInfo."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    received_kind: list[str] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        async def get_products(self, req, ctx):
            received_kind.append(ctx.auth_info.kind if ctx.auth_info else "none")
            return GetProductsResponse(products=[])

    handler = _make_handler(_Platform(), executor)
    ctx = ToolContext(
        metadata={
            "adcp.auth_info": {
                "kind": "bearer",
                "principal": "buyer-y",
                "scopes": ["read"],
            }
        }
    )
    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="any inventory"),
        ctx,
    )
    assert received_kind == ["bearer"]
