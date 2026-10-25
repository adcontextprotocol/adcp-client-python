"""``get_media_buys`` echoes the ``context`` its media buys were created with.

AdCP 3.2 ``get_media_buys`` echoes, on each media buy and each package, the
``context`` of the ``create_media_buy`` request (and package request) that
created it. ``create_media_buy_store`` persists both and backfills them, on
by default. These tests run ``create_media_buy`` then ``get_media_buys``
through the real MCP and A2A servers, against the in-memory and PostgreSQL
context stores, and compare the echoed objects as JSON text so ``null``
values and number types count.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryMediaBuyContextStore,
    MediaBuyContexts,
    MediaBuyContextStore,
    SingletonAccounts,
    create_media_buy_store,
)
from adcp.server import ADCPHandler
from adcp.testing import build_test_client
from adcp.types import ContextObject, CreateMediaBuySuccessResponse, GetProductsResponse, Package

BUY_CONTEXT: dict[str, Any] = {
    "campaign": "spring-26",
    "nil": None,
    "n": 7,
    "ratio": 2.5,
    "nested": {"x": None, "l": [1, None, "z"]},
}
PACKAGE_CONTEXTS: list[dict[str, Any]] = [
    {"buyer_ref": "line-1", "gone": None},
    {"buyer_ref": "line-2", "seq": 2},
]


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


@pytest.fixture(params=["memory", "postgres"])
async def context_store(request: pytest.FixtureRequest) -> AsyncIterator[MediaBuyContextStore]:
    if request.param == "memory":
        yield InMemoryMediaBuyContextStore()
        return
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        pytest.skip("ADCP_PG_TEST_URL required")
    from psycopg_pool import AsyncConnectionPool

    from adcp.decisioning.pg import PgMediaBuyContextStore

    table = f"test_mb_ctx_{secrets.token_hex(6)}"
    async with AsyncConnectionPool(url, open=False) as pool:
        store = PgMediaBuyContextStore(pool=pool, table_name=table)
        await store.create_schema()
        await store.create_schema()  # idempotent
        yield store
        async with pool.connection() as conn:
            await conn.execute(f"DROP TABLE {table}")


class Seller(DecisioningPlatform):
    """Creates buys without echoing context and reads them back without it."""

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"], channels=["display"], pricing_models=["cpm"]
    )
    accounts = SingletonAccounts(account_id="acct_1")

    def __init__(self, *, reverse: bool = True) -> None:
        self.reverse = reverse
        self.buys: dict[str, list[tuple[str, str]]] = {}

    def create_media_buy(self, req: Any, ctx: Any) -> CreateMediaBuySuccessResponse:
        media_buy_id = f"mb_{len(self.buys) + 1}"
        packages = [
            (f"{media_buy_id}_pkg_{i}", p.product_id) for i, p in enumerate(req.packages or [])
        ]
        self.buys[media_buy_id] = packages
        # Answer in reverse order, matched only by product_id.
        ordered = list(reversed(packages)) if self.reverse else packages
        return CreateMediaBuySuccessResponse(
            media_buy_id=media_buy_id,
            confirmed_at="2026-05-01T00:00:00Z",
            revision=1,
            status="active",
            packages=[Package(package_id=pid, product_id=prod) for pid, prod in ordered],
        )

    def get_media_buys(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {
            "media_buys": [
                {
                    "media_buy_id": media_buy_id,
                    "status": "active",
                    "currency": "USD",
                    "total_budget": 2000,
                    "confirmed_at": "2026-05-01T00:00:00Z",
                    "revision": 1,
                    "packages": [{"package_id": pid, "product_id": prod} for pid, prod in pkgs],
                }
                for media_buy_id, pkgs in self.buys.items()
            ]
        }

    def update_media_buy(self, media_buy_id: Any, patch: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def sync_creatives(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def get_media_buy_delivery(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def get_products(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError


def _create_request(context: dict[str, Any] | None = BUY_CONTEXT) -> dict[str, Any]:
    request: dict[str, Any] = {
        "idempotency_key": f"idem-{secrets.token_hex(8)}",
        "account": {"account_id": "acct_1"},
        "brand": {"domain": "brand.example"},
        "start_time": "2026-10-01T00:00:00Z",
        "end_time": "2026-10-31T00:00:00Z",
        "packages": [
            {
                "product_id": f"prod_{i}",
                "pricing_option_id": "cpm_1",
                "budget": 1000,
                "context": package_context,
            }
            for i, package_context in enumerate(PACKAGE_CONTEXTS)
        ],
    }
    if context is not None:
        request["context"] = context
    return request


async def _call(client: Any, transport: str, tool: str, arguments: dict[str, Any]) -> Any:
    if transport == "a2a":
        response = await client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": secrets.token_hex(4),
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": secrets.token_hex(8),
                        "role": "user",
                        "parts": [
                            {"kind": "data", "data": {"skill": tool, "parameters": arguments}}
                        ],
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return next(p["data"] for p in result["artifacts"][0]["parts"] if "data" in p)
    response = await client.post(
        "/mcp/",
        headers={"accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]["structuredContent"]


@pytest.mark.parametrize("transport", ["mcp", "a2a"])
async def test_get_media_buys_echoes_create_context(
    context_store: MediaBuyContextStore, transport: str
) -> None:
    seller = Seller()
    store = create_media_buy_store(capabilities=seller.capabilities, context_store=context_store)
    async with build_test_client(
        seller, transport=transport, stateless_http=True, media_buy_store=store
    ) as client:
        created = await _call(client, transport, "create_media_buy", _create_request())
        assert "adcp_error" not in created, created
        read = await _call(
            client,
            transport,
            "get_media_buys",
            {"account": {"account_id": "acct_1"}, "context": {"poll": 1}},
        )
    assert "adcp_error" not in read, read
    assert read["context"] == {"poll": 1}
    (buy,) = read["media_buys"]
    assert _json(buy["context"]) == _json(BUY_CONTEXT)
    by_product = {p["product_id"]: p for p in buy["packages"]}
    for i, package_context in enumerate(PACKAGE_CONTEXTS):
        assert _json(by_product[f"prod_{i}"]["context"]) == _json(package_context)


async def test_buy_without_context_echoes_only_package_contexts(
    context_store: MediaBuyContextStore,
) -> None:
    seller = Seller(reverse=False)
    store = create_media_buy_store(capabilities=seller.capabilities, context_store=context_store)
    async with build_test_client(seller, stateless_http=True, media_buy_store=store) as client:
        await _call(client, "mcp", "create_media_buy", _create_request(context=None))
        read = await _call(client, "mcp", "get_media_buys", {"account": {"account_id": "acct_1"}})
    (buy,) = read["media_buys"]
    assert "context" not in buy
    assert [p["context"] for p in buy["packages"]] == PACKAGE_CONTEXTS


async def test_echo_context_false_leaves_reads_alone() -> None:
    seller = Seller()
    store = create_media_buy_store(capabilities=seller.capabilities, echo_context=False)
    async with build_test_client(seller, stateless_http=True, media_buy_store=store) as client:
        await _call(client, "mcp", "create_media_buy", _create_request())
        read = await _call(client, "mcp", "get_media_buys", {"account": {"account_id": "acct_1"}})
    (buy,) = read["media_buys"]
    assert "context" not in buy
    assert all("context" not in p for p in buy["packages"])


async def test_store_is_first_write_wins_and_account_scoped(
    context_store: MediaBuyContextStore,
) -> None:
    """A replayed create can't replace the echoed objects; reads never cross accounts."""
    first = MediaBuyContexts(context={"v": 1, "f": 7.0}, package_contexts={"p1": {"a": None}})
    replay = MediaBuyContexts(context={"v": 2}, package_contexts={"p1": {"a": 1}, "p2": {"b": 2}})
    await _maybe(context_store.save_contexts("acct_a", "mb_1", first))
    await _maybe(context_store.save_contexts("acct_a", "mb_1", replay))
    stored = await _maybe(context_store.load_contexts("acct_a", ["mb_1", "mb_missing"]))
    assert list(stored) == ["mb_1"]
    record = stored["mb_1"]
    # ``7.0`` keeps its spelling: the store returns the JSON it was given.
    assert _json(record.context) == _json({"v": 1, "f": 7.0})
    assert _json(dict(record.package_contexts)) == _json({"p1": {"a": None}, "p2": {"b": 2}})
    assert await _maybe(context_store.load_contexts("acct_b", ["mb_1"])) == {}


async def _maybe(value: Any) -> Any:
    import inspect

    return await value if inspect.isawaitable(value) else value


def test_pg_sql_file_matches_migration_sql() -> None:
    from pathlib import Path

    import adcp.decisioning.pg as pg_package
    from adcp.decisioning.pg.media_buy_context_store import PgMediaBuyContextStore

    sql = Path(pg_package.__file__).with_name("media_buy_context_store.sql").read_text()
    body = "\n".join(line for line in sql.splitlines() if not line.startswith("--")).strip()
    assert body == PgMediaBuyContextStore.migration_sql()["upgrade"].strip()


def test_claiming_overlay_specialism_still_requires_an_adopter_store() -> None:
    with pytest.raises(ValueError, match="property-lists"):
        create_media_buy_store(capabilities=DecisioningCapabilities(specialisms=["property-lists"]))


# ---------------------------------------------------------------------------
# ContextObject keeps its nulls on every SDK dump path (#1575 footgun)
# ---------------------------------------------------------------------------


def test_context_object_dump_keeps_null_values() -> None:
    context = ContextObject.model_validate({"a": None, "b": 1, "n": {"x": None}})
    assert context.model_dump() == {"a": None, "b": 1, "n": {"x": None}}
    assert json.loads(context.model_dump_json()) == {"a": None, "b": 1, "n": {"x": None}}
    response = GetProductsResponse(products=[], context=context)
    assert response.model_dump(mode="json")["context"] == {"a": None, "b": 1, "n": {"x": None}}
    # An explicit exclusion still wins.
    assert context.model_dump(exclude={"a"}) == {"b": 1, "n": {"x": None}}


class _TypedEchoHandler(ADCPHandler[Any]):
    """Returns a typed response that copies the request's ``context``."""

    advertised_tools: ClassVar[set[str]] = {"get_adcp_capabilities", "get_products"}

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        from adcp.server.responses import capabilities_response

        return capabilities_response(["media_buy"])

    async def get_products(self, params: Any, context: Any = None) -> GetProductsResponse:
        raw = params if isinstance(params, dict) else params.model_dump(exclude_none=False)
        return GetProductsResponse(products=[], context=raw.get("context"))


@pytest.mark.parametrize("transport", ["mcp", "a2a"])
def test_typed_response_context_keeps_nulls_on_the_wire(transport: str) -> None:
    from starlette.testclient import TestClient

    from adcp.server.serve import _build_mcp_and_a2a_app

    context = {"a": None, "b": "x"}
    app = _build_mcp_and_a2a_app(
        _TypedEchoHandler(),
        name="typed-echo",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        stateless_http=True,
        allowed_hosts=["testserver"],
    )
    arguments = {"buying_mode": "brief", "brief": "video", "context": context}
    with TestClient(app, base_url="http://localhost:3001") as client:
        if transport == "mcp":
            response = client.post(
                "/mcp/",
                headers={"accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_products", "arguments": arguments},
                },
            )
            data = response.json()["result"]["structuredContent"]
        else:
            response = client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "messageId": "m-typed",
                            "role": "user",
                            "parts": [
                                {
                                    "kind": "data",
                                    "data": {"skill": "get_products", "parameters": arguments},
                                }
                            ],
                        }
                    },
                },
            )
            parts = response.json()["result"]["artifacts"][0]["parts"]
            data = next(p["data"] for p in parts if "data" in p)
    assert "adcp_error" not in data, data
    assert data["context"] == context
