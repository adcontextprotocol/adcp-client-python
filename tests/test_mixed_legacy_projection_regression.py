"""Integrator regression: one projection error must not reject valid siblings."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.validation import ValidationHookConfig


class Seller(ADCPHandler[Any]):
    adcp_capabilities = {"media_buy": {"features": {"canonical_creatives": False}}}

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def sync_creatives(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        self.received.append(params)
        return {
            "creatives": [
                {"creative_id": creative["creative_id"], "action": "created", "status": "approved"}
                for creative in params["creatives"]
            ]
        }


def batch() -> dict[str, Any]:
    return {
        "adcp_version": "3.1",
        "idempotency_key": "mixed-creative-projection-0001",
        "account": {
            "brand": {"domain": "acmeoutdoor.example"},
            "operator": "pinnacle-agency.example",
            "sandbox": True,
        },
        "creatives": [
            {
                "creative_id": "unsupported-native",
                "name": "Native",
                "format_id": {
                    "agent_url": "https://seller.example/mcp",
                    "id": "unsupported-native",
                },
                "assets": {},
            },
            {
                "creative_id": "display-one",
                "name": "Display one",
                "format_id": {
                    "agent_url": "https://seller.example/mcp",
                    "id": "display_300x250_image",
                    "width": 300,
                    "height": 250,
                },
                "assets": {},
            },
            {
                "creative_id": "display-two",
                "name": "Display two",
                "format_id": {
                    "agent_url": "https://seller.example/mcp",
                    "id": "display_300x250_image",
                    "width": 300,
                    "height": 250,
                },
                "assets": {},
            },
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "strict"])
async def test_strict_projection_rejects_the_batch_before_the_seller(mode: str | None) -> None:
    seller = Seller()
    request = batch()
    if mode is not None:
        request["validation_mode"] = mode
    with pytest.raises(ADCPTaskError) as raised:
        await create_tool_caller(seller, "sync_creatives")(request)
    assert seller.received == []
    assert raised.value.errors[0].code == "INVALID_REQUEST"
    assert (
        raised.value.errors[0].message
        == "request.creatives[0].format_id cannot be projected (no_match)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_validation", [False, True])
@pytest.mark.parametrize("version", ["3.0", "3.1"])
async def test_lenient_mixed_batch_accepts_two_creatives_and_fails_one(
    strict_validation: bool,
    version: str,
) -> None:
    seller = Seller()
    response = await create_tool_caller(
        seller,
        "sync_creatives",
        validation=(
            ValidationHookConfig(requests="strict", responses="strict")
            if strict_validation
            else None
        ),
    )({**batch(), "adcp_version": version, "validation_mode": "lenient"})
    rows = {creative["creative_id"]: creative for creative in response["creatives"]}
    assert rows["unsupported-native"]["action"] == "failed"
    assert rows["unsupported-native"]["errors"]
    assert rows["display-one"]["action"] == "created"
    assert rows["display-two"]["action"] == "created"
    assert [row["creative_id"] for row in response["creatives"]] == [
        "unsupported-native",
        "display-one",
        "display-two",
    ]
    assert [row["creative_id"] for row in seller.received[0]["creatives"]] == [
        "display-one",
        "display-two",
    ]


@pytest.mark.asyncio
async def test_configured_converter_receives_original_batch_positions() -> None:
    seller = Seller()
    paths: list[str] = []

    def converter(context: Any) -> dict[str, Any] | None:
        paths.append(context.field)
        if context.field == "request.creatives[1].format_id":
            return None
        return {"format_kind": "image", "params": {"width": 300, "height": 250}}

    seller.legacy_format_converter = converter
    request = {**batch(), "validation_mode": "lenient"}
    for index, creative in enumerate(request["creatives"]):
        creative["format_id"]["id"] = f"publisher-format-{index}"
    response = await create_tool_caller(seller, "sync_creatives")(request)
    assert paths == [f"request.creatives[{index}].format_id" for index in range(3)]
    assert [row["action"] for row in response["creatives"]] == ["created", "failed", "created"]
    assert response["creatives"][1]["errors"][0]["field"] == "creatives[1].format_id"
    assert response["creatives"][1]["errors"][0]["code"] == "UNSUPPORTED_FEATURE"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["account", "creative_id", "duplicate", "format_id"])
async def test_malformed_lenient_request_never_reaches_seller(mutation: str) -> None:
    seller = Seller()
    request = {**batch(), "validation_mode": "lenient"}
    if mutation == "account":
        request["account"] = {"sandbox": True}
    elif mutation == "creative_id":
        request["creatives"][0].pop("creative_id")
    elif mutation == "duplicate":
        request["creatives"][0]["creative_id"] = "display-one"
    else:
        request["creatives"][0]["format_id"] = {"id": "missing-owner"}
    with pytest.raises(ADCPTaskError) as caught:
        await create_tool_caller(seller, "sync_creatives")(request)
    assert caught.value.errors[0].code == "INVALID_REQUEST"
    assert seller.received == []


@pytest.mark.asyncio
async def test_all_unprojectable_rows_fail_without_empty_handler_dispatch() -> None:
    seller = Seller()
    request = {
        **batch(),
        "validation_mode": "lenient",
        "dry_run": True,
        "context": {"trace": "batch"},
    }
    request["creatives"] = request["creatives"][:1]
    response = await create_tool_caller(
        seller,
        "sync_creatives",
        validation=ValidationHookConfig(requests="strict", responses="strict"),
    )(request)
    assert response["creatives"][0]["action"] == "failed"
    assert response["dry_run"] is True
    assert response["context"] == {"trace": "batch"}
    assert seller.received == []


@pytest.mark.asyncio
async def test_delete_missing_fails_before_any_library_reconciliation() -> None:
    seller = Seller()
    with pytest.raises(ADCPTaskError, match="delete_missing"):
        await create_tool_caller(seller, "sync_creatives")(
            {**batch(), "validation_mode": "lenient", "delete_missing": True}
        )
    assert seller.received == []
    # A fully projectable replacement still receives every submitted ID and
    # the original reconciliation flag. No failure silently disables a sweep.
    request = {**batch(), "validation_mode": "lenient", "delete_missing": True}
    request["creatives"][0]["format_id"] = deepcopy(request["creatives"][1]["format_id"])
    await create_tool_caller(seller, "sync_creatives")(request)
    assert seller.received[0]["delete_missing"] is True
    assert len(seller.received[0]["creatives"]) == 3


@pytest.mark.asyncio
async def test_assignments_cannot_use_a_rejected_existing_creative() -> None:
    seller = Seller()
    request = {
        **batch(),
        "validation_mode": "lenient",
        "assignments": [
            {"creative_id": "unsupported-native", "package_id": "package_bad"},
            {"creative_id": "display-one", "package_id": "package_good"},
        ],
    }
    response = await create_tool_caller(seller, "sync_creatives")(request)
    assert seller.received[0]["assignments"] == [
        {"creative_id": "display-one", "package_id": "package_good"}
    ]
    assert "package_bad" in response["creatives"][0]["assignment_errors"]


@pytest.mark.asyncio
async def test_partial_creative_ids_filter_fails_before_mutation() -> None:
    seller = Seller()
    with pytest.raises(ADCPTaskError, match="creative_ids filtering"):
        await create_tool_caller(seller, "sync_creatives")(
            {**batch(), "validation_mode": "lenient", "creative_ids": ["display-one"]}
        )
    assert seller.received == []


@pytest.mark.asyncio
async def test_all_failed_uploads_do_not_silently_drop_valid_assignment_work() -> None:
    seller = Seller()
    request = {
        **batch(),
        "validation_mode": "lenient",
        "assignments": [
            {"creative_id": "already-stored", "package_id": "package_existing"},
        ],
    }
    request["creatives"] = request["creatives"][:1]
    with pytest.raises(ADCPTaskError, match="unrelated assignments"):
        await create_tool_caller(seller, "sync_creatives")(request)
    assert seller.received == []


@pytest.mark.asyncio
async def test_explicit_assignment_operations_remain_strict_and_atomic() -> None:
    seller = Seller()
    with pytest.raises(ADCPTaskError, match="requires strict"):
        await create_tool_caller(seller, "sync_creatives")(
            {
                **batch(),
                "adcp_version": "3.2",
                "validation_mode": "lenient",
                "assignment_operations": [
                    {
                        "operation": "assign",
                        "creative_id": "display-one",
                        "package_id": "package_one",
                    }
                ],
            }
        )
    assert seller.received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["creative_id", "name"])
async def test_rejected_inputs_remain_in_original_idempotency_identity(change: str) -> None:
    from adcp.exceptions import IdempotencyConflictError
    from adcp.server import IdempotencyStore, MemoryBackend

    store = IdempotencyStore(MemoryBackend())

    class IdempotentSeller(Seller):
        @store.wrap
        async def sync_creatives(
            self, params: dict[str, Any], context: ToolContext
        ) -> dict[str, Any]:
            return await super().sync_creatives(params, context)

    seller = IdempotentSeller()
    caller = create_tool_caller(seller, "sync_creatives")
    request = {**batch(), "validation_mode": "lenient", "dry_run": True}
    first = await caller(request, ToolContext(caller_identity="buyer_one"))
    replay = await caller(deepcopy(request), ToolContext(caller_identity="buyer_one"))
    assert replay["replayed"] is True
    assert replay["creatives"] == first["creatives"]
    assert len(seller.received) == 1
    assert seller.received[0]["dry_run"] is True
    assert seller.received[0]["idempotency_key"] == request["idempotency_key"]
    changed = deepcopy(request)
    changed["creatives"][0][change] = "different-rejected-input"
    with pytest.raises(IdempotencyConflictError):
        await caller(changed, ToolContext(caller_identity="buyer_one"))
    assert len(seller.received) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["poll", "push"])
async def test_custom_async_surfaces_are_refused_before_dispatch(surface: str) -> None:
    class PollingSeller(Seller):
        async def get_task_status(self, params: Any, context: Any = None) -> Any:
            return {"task_id": "custom-task"}

    seller = PollingSeller() if surface == "poll" else Seller()
    request = {**batch(), "validation_mode": "lenient"}
    if surface == "push":
        request["push_notification_config"] = {
            "url": "https://buyer.example/webhook",
            "operation_id": "mixed-batch-op",
        }
    with pytest.raises(ADCPTaskError, match="custom asynchronous"):
        await create_tool_caller(seller, "sync_creatives")(request)
    assert seller.received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("same_key,wrap_outer", [(False, True), (True, True), (False, False)])
async def test_nested_wrapped_sync_keeps_its_own_request_identity(
    same_key: bool, wrap_outer: bool
) -> None:
    from adcp.server import IdempotencyStore, MemoryBackend
    from adcp.server.idempotency.canonicalize import canonical_json_sha256

    inner_backend = MemoryBackend()
    inner_store = IdempotencyStore(inner_backend)
    outer_store = IdempotencyStore(MemoryBackend())
    request = {**batch(), "validation_mode": "lenient"}
    child_key = request["idempotency_key"] if same_key else "child-operation-0001"
    child_request = {"idempotency_key": child_key, "creatives": [], "child_operation": True}

    class Inner:
        @inner_store.wrap
        async def sync_creatives(self, params: Any, context: Any) -> Any:
            return {"creatives": []}

    class Outer(Seller):
        async def sync_creatives(
            self, params: dict[str, Any], context: ToolContext
        ) -> dict[str, Any]:
            await Inner().sync_creatives(child_request, context)
            return await super().sync_creatives(params, context)

    class WrappedOuter(Outer):
        @outer_store.wrap
        async def sync_creatives(
            self, params: dict[str, Any], context: ToolContext
        ) -> dict[str, Any]:
            return await super().sync_creatives(params, context)

    seller = WrappedOuter() if wrap_outer else Outer()
    await create_tool_caller(seller, "sync_creatives")(
        request, ToolContext(caller_identity="buyer_one")
    )
    cached = await inner_backend.get("buyer_one", child_key)
    assert cached is not None
    assert cached.payload_hash == canonical_json_sha256(child_request)
    if not same_key:
        assert await inner_backend.get("buyer_one", request["idempotency_key"]) is None


@pytest.mark.asyncio
async def test_pre_validation_hook_key_still_deduplicates_partial_batches() -> None:
    from adcp.exceptions import IdempotencyConflictError
    from adcp.server import IdempotencyStore, MemoryBackend

    store = IdempotencyStore(MemoryBackend())

    class IdempotentSeller(Seller):
        @store.wrap
        async def sync_creatives(
            self, params: dict[str, Any], context: ToolContext
        ) -> dict[str, Any]:
            return await super().sync_creatives(params, context)

    seller = IdempotentSeller()
    request = {**batch(), "validation_mode": "lenient"}
    request.pop("idempotency_key")
    caller = create_tool_caller(
        seller,
        "sync_creatives",
        pre_validation_hook=lambda tool, params: {
            **params,
            "idempotency_key": "hook-provided-key-0001",
        },
    )
    await caller(request, ToolContext(caller_identity="buyer_one"))
    replay = await caller(deepcopy(request), ToolContext(caller_identity="buyer_one"))
    assert replay["replayed"] is True
    assert len(seller.received) == 1
    changed = deepcopy(request)
    changed["creatives"][0]["name"] = "different rejected content"
    with pytest.raises(IdempotencyConflictError):
        await caller(changed, ToolContext(caller_identity="buyer_one"))
    assert len(seller.received) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["inline", "task", "workflow"])
@pytest.mark.parametrize("typed", [False, True])
async def test_managed_completion_preserves_rejection_rows(completion: str, typed: bool) -> None:
    from adcp.decisioning import (
        AdcpError,
        DecisioningCapabilities,
        DecisioningPlatform,
        InMemoryTaskRegistry,
        SingletonAccounts,
    )
    from adcp.decisioning.handler import PlatformHandler
    from adcp.types import SyncCreativesSuccessResponse

    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="acct_one")

        def __init__(self) -> None:
            self.uploaded: list[str] = []
            self.workflows: list[str] = []

        def sync_creatives(self, req: Any, ctx: Any) -> Any:
            def finish(task_ctx: Any = None) -> Any:
                self.uploaded.extend(creative.creative_id for creative in req.creatives)
                result = {
                    "creatives": [
                        {
                            "creative_id": creative.creative_id,
                            "action": "created",
                            "status": "approved",
                        }
                        for creative in reversed(req.creatives)
                    ]
                }
                return SyncCreativesSuccessResponse(**result) if typed else result

            if completion == "task":
                return ctx.handoff_to_task(finish)
            if completion == "workflow":
                return ctx.handoff_to_workflow(lambda task_ctx: self.workflows.append(task_ctx.id))
            return finish()

    platform = Platform()
    registry = InMemoryTaskRegistry()
    with ThreadPoolExecutor(max_workers=2) as executor:
        handler = PlatformHandler(platform, executor=executor, registry=registry)
        handler.adcp_capabilities = Seller.adcp_capabilities
        caller = create_tool_caller(handler, "sync_creatives")
        request = {**batch(), "account": {"account_id": "acct_one"}, "validation_mode": "lenient"}
        if completion == "workflow":
            with pytest.raises(AdcpError, match="cannot use WorkflowHandoff"):
                await caller(request)
            assert platform.workflows == []
            assert platform.uploaded == []
            assert registry._records == {}
            return
        response = await caller(request)
        if completion == "task":
            assert response["status"] == "submitted"
            for _ in range(100):
                record = await registry.get(
                    response["task_id"], expected_account_id="acct_one:anonymous"
                )
                if record is not None and record["state"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert record is not None
            assert record["state"] == "completed", record
            response = record["result"]
        assert platform.uploaded == ["display-one", "display-two"]
        assert [row["creative_id"] for row in response["creatives"]] == [
            "unsupported-native",
            "display-one",
            "display-two",
        ]
        assert response["creatives"][0]["action"] == "failed"
        assert [row["action"] for row in response["creatives"][1:]] == ["created", "created"]


@pytest.mark.asyncio
@pytest.mark.parametrize("package_id", ["package.with.dot", ""])
@pytest.mark.parametrize("handoff", [False, True], ids=["inline", "task"])
async def test_typed_assignment_failures_keep_all_wire_valid_package_ids(
    package_id: str, handoff: bool
) -> None:
    from adcp.decisioning import (
        DecisioningCapabilities,
        DecisioningPlatform,
        InMemoryTaskRegistry,
        SingletonAccounts,
    )
    from adcp.decisioning.handler import PlatformHandler
    from adcp.server import IdempotencyStore, MemoryBackend
    from adcp.types import SyncCreativesSuccessResponse
    from adcp.validation import validate_response

    store = IdempotencyStore(MemoryBackend())

    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"], supported_protocols=["media_buy"]
        )
        accounts = SingletonAccounts(account_id="assignment_test")

        def __init__(self) -> None:
            self.uploaded: list[str] = []
            self.response: Any = None
            self.original_rows: list[Any] = []

        @store.wrap
        async def sync_creatives(self, req: Any, ctx: Any) -> Any:
            def finish(task_ctx: Any = None) -> Any:
                self.uploaded.extend(row.creative_id for row in req.creatives)
                assert req.assignments is None
                self.response = SyncCreativesSuccessResponse(
                    creatives=[
                        {"creative_id": row.creative_id, "action": "created", "status": "approved"}
                        for row in req.creatives
                    ]
                )
                self.original_rows = list(self.response.creatives)
                return self.response

            return ctx.handoff_to_task(finish) if handoff else finish()

    platform = Platform()
    registry = InMemoryTaskRegistry()
    with ThreadPoolExecutor(max_workers=2) as executor:
        handler = PlatformHandler(platform, executor=executor, registry=registry)
        handler.adcp_capabilities = Seller.adcp_capabilities
        request = {
            **batch(),
            "validation_mode": "lenient",
            "account": {"account_id": "assignment_test"},
            "assignments": [{"creative_id": "unsupported-native", "package_id": package_id}],
        }
        caller = create_tool_caller(
            handler,
            "sync_creatives",
            validation=ValidationHookConfig(requests="strict", responses="strict"),
        )
        response = await caller(request)
        if handoff:
            for _ in range(100):
                record = await registry.get(
                    response["task_id"], expected_account_id="assignment_test:anonymous"
                )
                if record is not None and record["state"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert record is not None and record["state"] == "completed", record
            response = record["result"]
        assert validate_response("sync_creatives", response, version="3.1").valid
        assert platform.uploaded == ["display-one", "display-two"]
        assert response["creatives"][0]["action"] == "failed"
        assert package_id in response["creatives"][0]["assignment_errors"]
        assert platform.response.creatives[1] is platform.original_rows[0]
        assert platform.response.creatives[2] is platform.original_rows[1]
        if not handoff:
            replay = await caller(deepcopy(request))
            assert replay["replayed"] is True
            assert replay["creatives"] == response["creatives"]
            assert platform.uploaded == ["display-one", "display-two"]
