"""Application task polling/listing through the actual MCP HTTP transport."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest

from adcp.decisioning import (
    Account,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    ListableTaskRegistry,
    SingletonAccounts,
    TaskRegistry,
)
from adcp.decisioning.serve import create_adcp_server_from_platform
from adcp.decisioning.types import AdcpError
from adcp.server.mcp_tools import create_tool_caller, get_tools_for_handler
from adcp.testing import build_test_client
from adcp.validation.schema_loader import get_validator


class Accounts(SingletonAccounts):
    def resolve(self, ref: dict[str, Any] | None, *, auth_info: Any = None) -> Account[Any]:
        return Account(id=(ref or {}).get("account_id", "acct_1"))


class Seller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"], channels=["display"], pricing_models=["cpm"]
    )
    accounts = Accounts(account_id="acct_1")

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.failed = False

    def get_products(self, req: Any, ctx: Any) -> Any:
        return ctx.handoff_to_task(self.work)

    async def work(self, ctx: Any) -> dict[str, Any]:
        await ctx.update({"percentage": 25, "current_step": "Review"})
        self.started.set()
        await self.finish.wait()
        if self.failed:
            raise AdcpError("INTERNAL_ERROR", message="Background failed", recovery="terminal")
        return {"products": [], "cache_scope": "account", "status": "completed"}

    def create_media_buy(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def update_media_buy(self, media_buy_id: Any, patch: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def sync_creatives(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def get_media_buy_delivery(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def get_media_buys(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError


@pytest.fixture(params=["memory", "postgres"])
async def registry(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield InMemoryTaskRegistry()
        return
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        pytest.skip("ADCP_PG_TEST_URL required")
    from psycopg_pool import AsyncConnectionPool

    from adcp.decisioning.pg import PgTaskRegistry

    table = f"test_polling_{secrets.token_hex(6)}"
    async with AsyncConnectionPool(url, open=False) as pool:
        reg = PgTaskRegistry(pool=pool, _table=table)
        await reg.create_schema()
        yield reg
        async with pool.connection() as conn:
            await conn.execute(f"DROP TABLE {table}")


def rpc_data(response: Any) -> dict[str, Any]:
    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else json.loads(
            next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        )
    )
    assert "error" not in body, body
    result = body["result"]
    if "artifacts" in result:
        return next(
            part["data"]
            for artifact in result["artifacts"]
            for part in artifact["parts"]
            if "data" in part
        )
    if "structuredContent" in result:
        return result["structuredContent"]
    if "content" in result:
        return json.loads(result["content"][0]["text"])
    return result


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("transport", ["mcp", "a2a"])
async def test_handoff_poll_progress_terminal_over_http(
    registry: Any, failed: bool, transport: str
) -> None:
    seller = Seller()
    seller.failed = failed
    async with build_test_client(
        seller,
        registry=registry,
        validate_at_init=False,
        stateless_http=True,
        validation=None,
        transport=transport,
    ) as client:

        async def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if transport == "a2a":
                if method == "tools/list":
                    card = (await client.get("/.well-known/agent-card.json")).json()
                    return {"tools": [{"name": skill["id"]} for skill in card["skills"]]}
                body = {
                    "jsonrpc": "2.0",
                    "id": str(secrets.token_hex(4)),
                    "method": "message/send",
                    "params": {
                        "message": {
                            "messageId": secrets.token_hex(8),
                            "role": "user",
                            "parts": [
                                {
                                    "kind": "data",
                                    "data": {
                                        "skill": params["name"],
                                        "parameters": params["arguments"],
                                    },
                                }
                            ],
                        }
                    },
                }
                response = await client.post("/", json=body)
            else:
                response = await client.post(
                    "/mcp/",
                    headers={"accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
            assert response.status_code == 200, response.text
            return rpc_data(response)

        tools = await rpc("tools/list", {})
        assert {"get_task_status", "list_tasks"} <= {t["name"] for t in tools["tools"]}
        issued = await rpc(
            "tools/call",
            {
                "name": "get_products",
                "arguments": {
                    "brief": "Test",
                    "buying_mode": "brief",
                    "account": {"account_id": "acct_1"},
                },
            },
        )
        task_id = issued["task_id"]
        await asyncio.wait_for(seller.started.wait(), 5)
        working = await rpc(
            "tools/call", {"name": "get_task_status", "arguments": {"task_id": task_id}}
        )
        assert working["status"] == "working"
        assert working["progress"]["percentage"] == 25
        assert working["protocol"] == "media-buy"
        assert isinstance(working["created_at"], str)
        assert "result" not in working
        seller.finish.set()
        for _ in range(100):
            record = await registry.get(task_id)
            if record["state"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.01)
        terminal = await rpc(
            "tools/call",
            {"name": "get_task_status", "arguments": {"task_id": task_id, "include_result": True}},
        )
        assert terminal["status"] == ("failed" if failed else "completed")
        assert "completed_at" in terminal
        if failed:
            assert terminal["error"]["message"] == "Background failed"
            assert terminal["result"]["adcp_error"] == terminal["error"]
        else:
            assert terminal["result"]["products"] == []
        validator = get_validator("get_task_status", "sync")
        assert validator is not None
        validator.validate(terminal)
        result_validator = get_validator("get_products", "sync")
        assert result_validator is not None
        result_validator.validate(terminal["result"])
        listed = await rpc(
            "tools/call", {"name": "list_tasks", "arguments": {"filters": {"task_ids": [task_id]}}}
        )
        assert listed["tasks"][0]["task_id"] == task_id
        assert listed["tasks"][0]["domain"] == "media-buy"
        list_validator = get_validator("list_tasks", "sync")
        assert list_validator is not None
        list_validator.validate(listed)


async def test_scoped_listing_filters_cursor_and_not_found(registry: Any) -> None:
    handler, executor, _ = create_adcp_server_from_platform(
        Seller(), registry=registry, validate_at_init=False
    )
    try:
        caller = create_tool_caller(handler, "get_task_status")
        task = await registry.issue(
            account_id="acct_1", task_type="get_products", request_context={"campaign": "alpha"}
        )
        foreign = await registry.issue(account_id="acct_2", task_type="get_products")
        await registry.complete(task, {"products": []})
        other = await registry.issue(account_id="acct_1", task_type="get_signals")

        async def missing_error(task_id: str) -> dict[str, Any]:
            with pytest.raises(AdcpError) as caught:
                await caller({"task_id": task_id})
            return caught.value.to_wire()

        missing = await missing_error("absent")
        cross = await missing_error(foreign)
        await registry.discard(other)
        discarded = await missing_error(other)
        assert missing == cross == discarded
        assert missing["code"] == "REFERENCE_NOT_FOUND"
        first = await registry.list(
            account_id="acct_1",
            filters={
                "context_contains": "alpha",
                "statuses": ["completed"],
                "protocols": ["media-buy"],
            },
            pagination={"max_results": 1},
        )
        assert [t["task_id"] for t in first["tasks"]] == [task]
        for _ in range(2):
            await registry.issue(account_id="acct_1", task_type="get_products")
        page = await registry.list(account_id="acct_1", pagination={"max_results": 1})
        seen = {page["tasks"][0]["task_id"]}
        while page["pagination"]["has_more"]:
            page = await registry.list(
                account_id="acct_1",
                pagination={"max_results": 1, "cursor": page["pagination"]["cursor"]},
            )
            seen.add(page["tasks"][0]["task_id"])
        assert len(seen) == 3 and foreign not in seen
        cursor = (await registry.list(account_id="acct_1", pagination={"max_results": 1}))[
            "pagination"
        ]["cursor"]
        with pytest.raises(AdcpError):
            await registry.list(account_id="acct_2", pagination={"cursor": cursor})
        with pytest.raises(AdcpError):
            await registry.list(
                account_id="acct_1", filters={"status": "completed"}, pagination={"cursor": cursor}
            )
    finally:
        executor.shutdown()


class MinimalRegistry:
    is_durable: ClassVar[bool] = True

    def __init__(self) -> None:
        self.delegate = InMemoryTaskRegistry()

    async def issue(self, **kwargs: Any) -> str:
        return await self.delegate.issue(**kwargs)

    async def get(self, task_id: str, *, expected_account_id: str | None = None) -> Any:
        record = await self.delegate.get(task_id, expected_account_id=expected_account_id)
        if record is not None:
            record["expires_at"] = 0
        return record

    async def update_progress(self, task_id: str, progress: dict[str, Any]) -> None:
        await self.delegate.update_progress(task_id, progress)

    async def complete(self, task_id: str, result: dict[str, Any]) -> None:
        await self.delegate.complete(task_id, result)

    async def fail(self, task_id: str, error: dict[str, Any]) -> None:
        await self.delegate.fail(task_id, error)

    async def discard(self, task_id: str) -> None:
        await self.delegate.discard(task_id)


async def test_minimal_third_party_registry_boots_and_expiry_is_hidden() -> None:
    registry = MinimalRegistry()
    assert isinstance(registry, TaskRegistry)
    assert not isinstance(registry, ListableTaskRegistry)
    handler, executor, _ = create_adcp_server_from_platform(
        Seller(), registry=registry, validate_at_init=False
    )
    try:
        names = {t["name"] for t in get_tools_for_handler(handler)}
        assert "get_task_status" in names and "list_tasks" not in names
        task = await registry.issue(account_id="acct_1", task_type="get_products")
        caller = create_tool_caller(handler, "get_task_status")
        errors = []
        for reference in (task, "absent"):
            with pytest.raises(AdcpError) as caught:
                await caller({"task_id": reference})
            errors.append(caught.value.to_wire())
        assert errors[0] == errors[1]
        assert (await create_tool_caller(handler, "list_tasks")({}))["error"][
            "code"
        ] == "NOT_SUPPORTED"
    finally:
        executor.shutdown()


async def test_account_resolution_precedes_registry_reads() -> None:
    from unittest.mock import AsyncMock

    class DeniedAccounts(Accounts):
        def resolve(self, ref: dict[str, Any] | None, *, auth_info: Any = None) -> Account[Any]:
            raise AdcpError("PERMISSION_DENIED", message="Denied")

    seller = Seller()
    seller.accounts = DeniedAccounts(account_id="acct_1")
    reg = InMemoryTaskRegistry()
    handler, executor, _ = create_adcp_server_from_platform(
        seller, registry=reg, validate_at_init=False
    )
    try:
        read = AsyncMock(wraps=reg.get)
        listing = AsyncMock(wraps=reg.list)
        reg.get = read
        reg.list = listing
        for tool, params in (("get_task_status", {"task_id": "probe"}), ("list_tasks", {})):
            with pytest.raises(AdcpError, match="PERMISSION_DENIED"):
                await create_tool_caller(handler, tool)(params)
        read.assert_not_awaited()
        listing.assert_not_awaited()
    finally:
        executor.shutdown()
