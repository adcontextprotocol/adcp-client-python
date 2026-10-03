"""SDK14 compatibility at the examples' real dispatch boundaries."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.decisioning import AdcpError, InMemoryTaskRegistry
from adcp.decisioning.handler import PlatformHandler
from adcp.exceptions import IdempotencyConflictError
from adcp.server import Tenant, ToolContext
from adcp.server.tenant_router import _current_tenant
from adcp.server.test_controller import _handle_test_controller
from adcp.types import GetProductsRequest
from examples.multi_platform_seller.src.account_store import MultiTenantAccountStore
from examples.multi_platform_seller.src.app import build_router as build_multi_router
from examples.multi_platform_seller.src.mock_guaranteed import MockGuaranteedPlatform
from examples.multi_platform_seller.src.mock_non_guaranteed import MockNonGuaranteedPlatform
from examples.multi_platform_seller.src.test_controller import MultiTenantTestController
from examples.sales_proposal_mode_seller.src.app import build_router as build_proposal_router
from examples.sales_proposal_mode_seller.src.proposal_manager import PROPOSAL_ID


@pytest.mark.asyncio
async def test_controller_seeds_normal_account_resolution_and_isolates_tenants() -> None:
    accounts = MultiTenantAccountStore(tenants=frozenset({"tenant-a", "tenant-b"}))
    controller = MultiTenantTestController(accounts)
    fixture = {
        "brand": {"domain": "advertiser.example"},
        "operator": "buyer.example",
        "operator_unit": {"id": "unit"},
        "sandbox": False,
    }
    token = _current_tenant.set(Tenant(id="tenant-a"))
    try:
        await controller.seed_account("seeded-live", fixture)
        assert accounts.resolve(fixture).id == "seeded-live"
        assert accounts.resolve(fixture).mode == "live"
        assert {a.id for a in accounts.list({"sandbox": False})} == {"seeded-live"}
        denied = await _handle_test_controller(
            controller,
            {
                "scenario": "seed_account",
                "account": {"account_id": "seeded-live"},
                "params": {"account_id": "forbidden-write", "fixture": {}},
            },
            account_resolver=accounts.resolve,
        )
        assert denied["success"] is False
        assert denied["error"] == "FORBIDDEN"
        assert "forbidden-write" not in {a.id for a in accounts.list()}
    finally:
        _current_tenant.reset(token)
    token = _current_tenant.set(Tenant(id="tenant-b"))
    try:
        assert {a.id for a in accounts.list()} == {"tenant-b:default"}
        assert accounts.resolve(fixture).id == "tenant-b:default"
    finally:
        _current_tenant.reset(token)
    assert accounts.list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_type", [MockGuaranteedPlatform, MockNonGuaranteedPlatform])
async def test_multi_buy_commit_revision_cancel_and_unknown_package(platform_type: Any) -> None:
    from adcp.testing import make_request_context

    platform = platform_type()
    router = build_multi_router()
    account = router.accounts.resolve({"account_id": "tenant-a:demo"})
    ctx = make_request_context(account=account)
    product = platform.get_products({}, ctx)["products"][0]
    created = platform.create_media_buy(
        {"packages": [{"product_id": product["product_id"], "budget": 100}], "start_time": "asap"},
        ctx,
    )
    assert created["revision"] == 1
    assert created["confirmed_at"]
    buy_id = created["media_buy_id"]
    with pytest.raises(AdcpError) as error:
        platform.update_media_buy(buy_id, {"packages": [{"package_id": "missing"}]}, ctx)
    assert error.value.code == "PACKAGE_NOT_FOUND"
    canceled = platform.update_media_buy(buy_id, {"canceled": True}, ctx)
    assert canceled["revision"] == 2
    assert canceled["media_buy_status"] == "canceled"
    with pytest.raises(AdcpError) as error:
        platform.update_media_buy(buy_id, {"canceled": True}, ctx)
    assert error.value.code == "NOT_CANCELLABLE"
    stored = platform.get_media_buys({"media_buy_ids": [buy_id]}, ctx)["media_buys"][0]
    assert stored["confirmed_at"] == created["confirmed_at"]
    assert stored["revision"] == 2


@pytest.mark.asyncio
async def test_finalize_replay_covers_framework_interception_and_concurrent_calls() -> None:
    router = build_proposal_router()
    with ThreadPoolExecutor(max_workers=4) as pool:
        handler = PlatformHandler(router, executor=pool, registry=InMemoryTaskRegistry())
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="initial"), ToolContext()
        )
        params = {
            "buying_mode": "refine",
            "idempotency_key": "sdk14-finalize-key-0001",
            "refine": [{"scope": "proposal", "proposal_id": PROPOSAL_ID, "action": "finalize"}],
        }
        request = GetProductsRequest.model_validate(params)

        async def finalize() -> Any:
            return await router.idempotency_middleware(
                "get_products",
                params,
                ToolContext(),
                lambda: handler.get_products(request, ToolContext()),
            )

        results = await asyncio.gather(finalize(), finalize())
        normalized = [r if isinstance(r, dict) else r.model_dump(mode="json") for r in results]
        assert sum(bool(r.get("replayed")) for r in normalized) == 1
        assert all(r["proposals"][0]["proposal_status"] == "committed" for r in normalized)
        assert normalized[0]["proposals"] == normalized[1]["proposals"]
        replay = await finalize()
        assert replay["replayed"] is True
        assert replay["proposals"] == normalized[0]["proposals"]


@pytest.mark.asyncio
async def test_replay_keeps_authenticated_principal_and_tenant_scopes_distinct() -> None:
    router = build_proposal_router()
    calls = 0

    async def invoke(principal: str, tenant: str) -> Any:
        async def next_call() -> dict[str, int]:
            nonlocal calls
            calls += 1
            return {"sequence": calls}

        return await router.idempotency_middleware(
            "get_products",
            {"idempotency_key": "shared-scope-key-0001"},
            ToolContext(caller_identity=principal, tenant_id=tenant),
            next_call,
        )

    assert (await invoke("alice", "a"))["sequence"] == 1
    assert (await invoke("bob", "a"))["sequence"] == 2
    assert (await invoke("alice", "b"))["sequence"] == 3
    assert (await invoke("alice", "a"))["replayed"] is True
    assert calls == 3


@pytest.mark.asyncio
async def test_identical_payload_and_key_do_not_replay_across_tasks() -> None:
    router = build_proposal_router()
    calls = 0

    async def next_call() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"sequence": calls}

    params = {"idempotency_key": "cross-task-key-000001"}
    context = ToolContext(caller_identity="alice", tenant_id="default")
    first = await router.idempotency_middleware("get_products", params, context, next_call)
    second = await router.idempotency_middleware("create_media_buy", params, context, next_call)
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_same_task_key_with_changed_payload_conflicts() -> None:
    router = build_proposal_router()

    async def next_call() -> dict[str, bool]:
        return {"success": True}

    context = ToolContext(caller_identity="alice", tenant_id="default")
    original = {"idempotency_key": "changed-payload-key-001", "brief": "original"}
    await router.idempotency_middleware("get_products", original, context, next_call)
    with pytest.raises(IdempotencyConflictError):
        await router.idempotency_middleware(
            "get_products", {**original, "brief": "changed"}, context, next_call
        )


@pytest.mark.asyncio
async def test_forced_rejection_is_one_shot_and_scoped_to_account_tenant_and_caller() -> None:
    from adcp.testing import make_request_context

    router = build_multi_router()
    controller = router.fixture_controller
    token = _current_tenant.set(Tenant(id="tenant-a"))
    try:
        account = router.accounts.resolve({"account_id": "demo"})
        await controller.force_get_products_arm(
            "rejected",
            reason="Budget below minimum",
            account={"account_id": "demo"},
            context=ToolContext(caller_identity="alice"),
        )
        wrong_caller = make_request_context(account=account, auth_principal="bob")
        assert controller.take_rejection(wrong_caller) is None
        other = router.accounts.resolve({"account_id": "other"})
        assert (
            controller.take_rejection(make_request_context(account=other, auth_principal="alice"))
            is None
        )
        matching = make_request_context(account=account, auth_principal="alice")
        assert "products" in await router.get_products({"buying_mode": "wholesale"}, matching)
        rejected = await router.get_products({"buying_mode": "brief"}, matching)
        assert rejected == {"status": "rejected", "reason": "Budget below minimum"}
        assert "products" in await router.get_products({"buying_mode": "brief"}, matching)
    finally:
        _current_tenant.reset(token)
