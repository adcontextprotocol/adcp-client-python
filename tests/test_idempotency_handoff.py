"""``IdempotencyStore.wrap`` returns task and workflow handoffs uncached (#1781).

A handoff is a control marker for the dispatcher, not a response, so the
store cannot cache it. ``cache_response=`` lets a seller mark other results
as not cacheable.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform, SingletonAccounts
from adcp.decisioning.types import TaskHandoff, WorkflowHandoff
from adcp.decisioning.validate_idempotency import is_wrapped
from adcp.server import IdempotencyStore, MemoryBackend, ToolContext
from adcp.testing import build_test_client
from adcp.types import CreateMediaBuySuccessResponse, Package

CONTEXT = ToolContext(caller_identity="buyer_1")


def _params(key: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"idempotency_key": key or "k" * 16, "media_buy_id": "mb_1", **extra}


@pytest.mark.parametrize(
    "make_handoff",
    [lambda: WorkflowHandoff(lambda task_ctx: None), lambda: TaskHandoff(lambda task_ctx: {})],
    ids=["workflow", "task"],
)
async def test_handoff_is_returned_uncached(make_handoff: Any) -> None:
    store = IdempotencyStore(MemoryBackend())
    returned: list[Any] = []

    class Platform:
        @store.wrap
        async def update_media_buy(self, params: Any, context: Any = None) -> Any:
            handoff = make_handoff()
            returned.append(handoff)
            return handoff

    platform = Platform()
    first = await platform.update_media_buy(_params(), CONTEXT)
    assert first is returned[0]
    # Nothing was cached, so a retry with the same key runs the method again
    # rather than replaying a marker the dispatcher already consumed.
    second = await platform.update_media_buy(_params(), CONTEXT)
    assert second is returned[1]
    assert len(returned) == 2


async def test_a_completed_response_after_a_handoff_is_cached() -> None:
    store = IdempotencyStore(MemoryBackend())
    calls = 0

    class Platform:
        @store.wrap
        async def update_media_buy(self, params: Any, context: Any = None) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                return WorkflowHandoff(lambda task_ctx: None)
            return {"media_buy_id": "mb_1", "revision": calls}

    platform = Platform()
    assert isinstance(await platform.update_media_buy(_params(), CONTEXT), WorkflowHandoff)
    assert await platform.update_media_buy(_params(), CONTEXT) == {
        "media_buy_id": "mb_1",
        "revision": 2,
    }
    replay = await platform.update_media_buy(_params(), CONTEXT)
    assert replay == {"media_buy_id": "mb_1", "revision": 2, "replayed": True}
    assert calls == 2


async def test_cache_response_predicate_marks_results_uncached() -> None:
    store = IdempotencyStore(MemoryBackend())
    calls = 0

    class Pending:
        """A seller's own control result, not a wire response."""

    class Platform:
        @store.wrap(cache_response=lambda result: not isinstance(result, Pending))
        async def update_media_buy(self, params: Any, context: Any = None) -> Any:
            nonlocal calls
            calls += 1
            return Pending() if params.get("paused") else {"media_buy_id": "mb_1"}

    platform = Platform()
    assert is_wrapped(platform.update_media_buy)
    assert isinstance(await platform.update_media_buy(_params(paused=True), CONTEXT), Pending)
    assert isinstance(await platform.update_media_buy(_params(paused=True), CONTEXT), Pending)
    assert calls == 2

    key = "c" * 16
    assert await platform.update_media_buy(_params(key), CONTEXT) == {"media_buy_id": "mb_1"}
    assert (await platform.update_media_buy(_params(key), CONTEXT))["replayed"] is True
    assert calls == 3


async def test_predicate_cannot_cache_a_handoff() -> None:
    store = IdempotencyStore(MemoryBackend())
    calls = 0

    class Platform:
        @store.wrap(cache_response=lambda result: True)
        async def update_media_buy(self, params: Any, context: Any = None) -> Any:
            nonlocal calls
            calls += 1
            return WorkflowHandoff(lambda task_ctx: None)

    platform = Platform()
    await platform.update_media_buy(_params(), CONTEXT)
    await platform.update_media_buy(_params(), CONTEXT)
    assert calls == 2


async def test_uncached_result_still_checks_conflicts_against_cached_ones() -> None:
    """A key first used for a cached response keeps its conflict detection."""
    from adcp.exceptions import IdempotencyConflictError

    store = IdempotencyStore(MemoryBackend())

    class Platform:
        @store.wrap
        async def update_media_buy(self, params: Any, context: Any = None) -> Any:
            if params.get("paused"):
                return WorkflowHandoff(lambda task_ctx: None)
            return {"media_buy_id": "mb_1"}

    platform = Platform()
    await platform.update_media_buy(_params(), CONTEXT)
    with pytest.raises(IdempotencyConflictError):
        await platform.update_media_buy(_params(paused=True), CONTEXT)


# End to end: a decisioning method that may hand off to a workflow keeps the
# SDK's idempotency, and the buyer gets the framework's Submitted envelope.

store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86_400)


class Seller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"], channels=["display"], pricing_models=["cpm"]
    )
    accounts = SingletonAccounts(account_id="acct_1")

    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.created = 0

    @store.wrap
    async def create_media_buy(self, req: Any, ctx: Any) -> Any:
        if req.packages[0].budget > 5000:
            # Needs a person: hand the call to an external review workflow.
            return ctx.handoff_to_workflow(lambda task_ctx: self.enqueued.append(task_ctx.id))
        self.created += 1
        return CreateMediaBuySuccessResponse(
            media_buy_id=f"mb_{self.created}",
            confirmed_at="2026-05-01T00:00:00Z",
            revision=1,
            status="active",
            packages=[Package(package_id="pkg_1", product_id="prod_1")],
        )

    def get_products(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def update_media_buy(self, media_buy_id: Any, patch: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def sync_creatives(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def get_media_buy_delivery(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def get_media_buys(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError


def _create_request(key: str, budget: int) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "account": {"account_id": "acct_1"},
        "brand": {"domain": "brand.example"},
        "start_time": "2026-10-01T00:00:00Z",
        "end_time": "2026-10-31T00:00:00Z",
        "packages": [{"product_id": "prod_1", "pricing_option_id": "cpm_1", "budget": budget}],
    }


async def _create(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        "/mcp/",
        headers={"accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_media_buy", "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]["structuredContent"]  # type: ignore[no-any-return]


async def test_wrapped_decisioning_method_can_hand_off_to_a_workflow() -> None:
    seller = Seller()
    async with build_test_client(
        seller,
        transport="mcp",
        stateless_http=True,
        context_factory=lambda meta: ToolContext(caller_identity="buyer_1"),
    ) as client:
        submitted = await _create(client, _create_request(f"idem-{secrets.token_hex(8)}", 9000))
        assert submitted["status"] == "submitted", submitted
        assert seller.enqueued == [submitted["task_id"]]

        # The synchronous path on the same wrapper still replays.
        key = f"idem-{secrets.token_hex(8)}"
        first = await _create(client, _create_request(key, 1000))
        replay = await _create(client, _create_request(key, 1000))
    assert first["media_buy_id"] == replay["media_buy_id"] == "mb_1"
    assert seller.created == 1
