"""Shared memory/PostgreSQL feed vectors and real mounted reporting reads."""

import json
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import replace

import pytest

from adcp.reporting.feed import InMemoryReportingFeedStore
from adcp.reporting.ledger.delivery_models import ReportingDeliveryScope
from adcp.reporting.receipts import ReportingReceiptHandler

from ._durable_materializer_support import DurableHarness
from ._generation_support import isolated_reporting_pool
from ._receipt_support import adjustment_for, receipt_case, request_for
from ._receipt_transport import MountedReceipts
from ._reconciliation_support import Clock

ARRAYS = (
    "periods",
    "revisions",
    "adjustments",
    "consumer_statuses",
    "materializations",
    "receipts",
    "adjustment_receipts",
)


@asynccontextmanager
async def feed_harness(backend, *, notifications=False):
    clock = Clock()
    if backend == "memory":
        yield DurableHarness(
            InMemoryReportingFeedStore(clock=clock, notifications=notifications), clock
        )
    else:
        from adcp.reporting.feed import PgReportingFeedStore

        async with isolated_reporting_pool(autocommit=True) as pool:
            # Autonomous writers use DB time. A historical application clock
            # on the low-level reconciliation API would reject their new rows.
            store = PgReportingFeedStore(pool=pool, notifications=notifications)
            await store.create_schema()
            yield DurableHarness(store, clock, pool)


@pytest.fixture(
    params=[("memory", False), ("memory", True), ("postgres", False), ("postgres", True)]
)
async def feeds(request):
    backend, notifications = request.param
    async with feed_harness(backend, notifications=notifications) as h:
        yield h


async def mixed_case(h, **kwargs):
    s = await receipt_case(h, **kwargs)
    adjustment = await adjustment_for(h, s)
    req = request_for(s, adjustment_receipts=[adjustment])
    result = await h.store.ingest_receipt_batch(req, caller=s.binding.principal)
    assert [r["result"] for r in result["results"]] == ["recorded", "recorded"]
    return s, req, result


def feed_request(s, *, limit=1, **kwargs):
    return {
        "adcp_version": "3.2-rc.6",
        "view": "periods",
        "account": {"account_id": s.obligation.account_id},
        "pagination": {"max_results": limit},
        **kwargs,
    }


async def walk(store, request, caller, *, first=None, **kwargs):
    """Reference consumption: return the checkpoint only after exact exhaustion."""
    request = deepcopy(request)
    pages, rows, seen = [], {a: [] for a in ARRAYS}, set()
    checkpoint = None
    while True:
        page = (
            first
            if not pages and first is not None
            else await store.read_reporting_feed(request, caller=caller, **kwargs)
        )
        pages.append(page)
        if checkpoint is None:
            checkpoint = page["changes_checkpoint"]
        assert page["changes_checkpoint"] == checkpoint
        assert page["ledger_snapshot_id"] == pages[0]["ledger_snapshot_id"]
        assert page["ledger_as_of"] == pages[0]["ledger_as_of"]
        assert page["pagination"]["total_count"] == pages[0]["pagination"]["total_count"]
        for name in ARRAYS:
            rows[name].extend(page.get(name, []))
        if not page["pagination"]["has_more"]:
            assert sum(map(len, rows.values())) == page["pagination"]["total_count"]
            return pages, rows, checkpoint
        cursor = page["pagination"]["cursor"]
        assert cursor not in seen and len(pages) < 1000
        seen.add(cursor)
        request["pagination"]["cursor"] = cursor


async def restart(h):
    """Memory keeps serialized durable state; PG uses a new store instance."""
    if h.pool is None:
        store = InMemoryReportingFeedStore(
            clock=h.clock, notifications=h.store._notification_state is not None
        )
        for key, value in vars(h.store).items():
            if key not in {"_clock", "_lock"}:
                vars(store)[key] = deepcopy(value)
    else:
        from adcp.reporting.feed import PgReportingFeedStore

        store = PgReportingFeedStore(
            pool=h.pool, clock=h.clock, notifications=h.store._notifications_enabled
        )
    h.store = store
    return store


async def second_consumer(h, s, consumer="other-buyer"):
    scope = ReportingDeliveryScope(
        s.obligation.generation_key, consumer, s.obligation.reporting_obligation_id
    )
    binding = replace(s.binding, consumer_id=consumer)
    await h.store.put_destination_binding(binding)
    await h.store.bind_obligation_delivery(replace(s.delivery, scope=scope))
    await h.store.commit_materialization_attempt(replace(s.attempt, scope=scope))
    await h.store.commit_materialization(replace(s.outcome, scope=scope))
    receipt = replace(s.receipt, scope=scope)
    other = replace(
        s,
        binding=binding,
        delivery=replace(s.delivery, scope=scope),
        attempt=replace(s.attempt, scope=scope),
        outcome=replace(s.outcome, scope=scope),
        receipt=receipt,
    )
    await h.store.ingest_receipt_batch(request_for(other), caller=binding.principal)
    return other


def without_feed(image):
    return {
        k: v
        for k, v in image.items()
        if k not in {"_reporting_feed_snapshots", "reporting_feed_snapshots"}
    }


class MountedFeed(MountedReceipts):
    def __init__(self, h, *, feedback=False, **kwargs):
        super().__init__(h, **kwargs)
        self.handler = ReportingReceiptHandler(
            h.store,
            resolve_account=self.resolve_account,
            buyer_agents=self.registry,
            consumer_status_enabled=feedback,
            adcp_version=self.version,
        )
        self.handler.get_reporting_status = self.idempotency.wrap(self.handler.get_reporting_status)

    @asynccontextmanager
    async def sdk_clients(self, a2a_version, *, token="token-one"):
        """Public typed clients and real encoders; only HTTP I/O uses ASGI."""
        import httpx2

        from adcp import ADCPClient, AgentConfig

        observed = []

        async def capture(request):
            if request.method != "POST":
                return
            body = json.loads(request.content)
            method = body.get("method")
            if method == "tools/call":
                params = body["params"]["arguments"]
                protocol = "mcp"
            elif method in {"message/send", "SendMessage"}:
                params = body["params"]["message"]["parts"][0]["data"]["parameters"]
                protocol = "a2a"
            else:
                return
            observed.append((protocol, method, params))

        async with self.client(public_url="http://localhost") as transport:
            # Keep both public SDK transport stacks, negotiation and encoders.
            # The factory/HTTP pool injection only replaces the network socket.
            def mcp_http(**kwargs):
                return httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=transport._transport.app),
                    event_hooks={"request": [capture]},
                    **kwargs,
                )

            clients = {}
            async with AsyncExitStack() as stack:
                for protocol in ("mcp", "a2a"):
                    config = AgentConfig(
                        id=f"feed-{protocol}",
                        agent_uri=(
                            "http://localhost/mcp/" if protocol == "mcp" else "http://localhost"
                        ),
                        protocol=protocol,
                        auth_token=token,
                        auth_header="Authorization",
                        auth_type="bearer",
                    )
                    clients[protocol] = await stack.enter_async_context(
                        ADCPClient(
                            config,
                            adcp_version=self.version,
                            force_a2a_version=a2a_version if protocol == "a2a" else None,
                            httpx_client_factory=mcp_http if protocol == "mcp" else None,
                        )
                    )
                transport.headers["Authorization"] = f"Bearer {token}"
                transport.event_hooks["request"].append(capture)
                clients["a2a"].adapter._httpx_client = transport
                yield clients, observed

    async def mcp(self, client, request=None, *, mutate_wire=None, **kwargs):
        def rewrite(wire):
            wire = wire.replace(
                '"name": "sync_reporting_receipts"', '"name": "get_reporting_status"'
            )
            return mutate_wire(wire) if mutate_wire else wire

        return await super().mcp(client, request, mutate_wire=rewrite, **kwargs)

    async def a2a(self, client, request, *, mutate_wire=None, **kwargs):
        def rewrite(wire):
            wire = wire.replace(
                '"skill": "sync_reporting_receipts"', '"skill": "get_reporting_status"'
            )
            return mutate_wire(wire) if mutate_wire else wire

        return await super().a2a(client, request, mutate_wire=rewrite, **kwargs)
