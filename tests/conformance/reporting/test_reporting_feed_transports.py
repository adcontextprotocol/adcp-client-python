"""Public MCP/A2A pages: exact authorization, raw schemas, generated clients."""

import json
import re
from contextlib import AsyncExitStack
from copy import deepcopy
from functools import partial

import pytest
from jsonschema import Draft7Validator, FormatChecker

from adcp.decisioning import Account, AuthInfo, RequestContext
from adcp.exceptions import ADCPTaskError
from adcp.reporting.feed.request import feed_schema
from adcp.reporting.receipts import ReportingReceiptHandler
from adcp.server import ADCPHandler, ToolContext
from adcp.server.mcp_tools import get_tools_for_handler
from adcp.types import GetReportingStatusRequest, GetReportingStatusResponse

from ._feed_support import (
    MountedFeed,
    feed_request,
    feeds,
    mixed_case,
    second_consumer,
    without_feed,
)
from ._receipt_support import receipt_case, receipt_harness, request_for
from ._receipt_transport import MountedReceipts, error_code

__all__ = ["feeds"]


async def test_mounted_fractional_context_and_filters_keep_exact_integer_page_bounds(feeds):
    h = feeds
    s, _, _ = await mixed_case(h)
    mounted = MountedFeed(h)
    mounted.authorize(s)
    req = feed_request(s, context={"sample": 0.125}, ext={"vendor": {"threshold": 0.25}})
    async with mounted.client() as client:
        _, first = await mounted.mcp(client, req)
        assert first["context"] == req["context"], first
        assert first["pagination"]["has_more"] is True
        continued = {
            **req,
            "context": {"sample": 0.75},
            "pagination": {"cursor": first["pagination"]["cursor"], "max_results": 100},
        }
        for v1 in (False, True):
            _, last = await mounted.a2a(client, continued, v1=v1)
            assert last["context"] == continued["context"], last
            assert last["changes_checkpoint"] == first["changes_checkpoint"]
            assert last["pagination"]["has_more"] is False
        before = await h.image()
        for call in (mounted.mcp, mounted.a2a, partial(mounted.a2a, v1=True)):
            _, invalid = await call(
                client,
                req,
                mutate_wire=lambda raw: re.sub(
                    r'"max_results": 1(?:\.0)?(?=\s*[,}])',
                    '"max_results": 1.000000000000000000001',
                    raw,
                ),
            )
            assert error_code(invalid) in {"INVALID_REQUEST", "VALIDATION_ERROR"}, invalid
        assert await h.image() == before


@pytest.mark.parametrize("view", ["summary", "revision"])
async def test_cross_view_positions_fail_before_current_projection_on_every_mount(
    feeds, view, monkeypatch
):
    from adcp.reporting.ledger.status import ReportingStatusHandler

    h = feeds
    s, _, _ = await mixed_case(h)
    mounted = MountedFeed(h)
    mounted.authorize(s)
    async with mounted.client() as client:
        _, first = await mounted.mcp(client, feed_request(s))

        async def forbidden_projection(*args, **kwargs):
            raise AssertionError("a bound feed token must never consult today's Core projection")

        monkeypatch.setattr(ReportingStatusHandler, "handle", forbidden_projection)
        for position in (
            {"pagination": {"cursor": first["pagination"]["cursor"]}},
            {"changes_after": first["changes_checkpoint"]},
        ):
            req = feed_request(s, view=view, **position)
            if view == "revision":
                req["reporting_revision_id"] = s.revision.reporting_revision_id
            with pytest.raises(ADCPTaskError) as error:
                await mounted.handler.get_reporting_status(
                    req, ToolContext(caller_identity=s.binding.consumer_id)
                )
            assert error.value.errors[0].code == "INVALID_CHECKPOINT"
            for transport in (mounted.mcp, mounted.a2a):
                _, rejected = await transport(client, req)
                assert error_code(rejected) in {
                    "INVALID_CHECKPOINT",
                    "INVALID_REQUEST",
                    "VALIDATION_ERROR",
                }
        mounted.grants.clear()
        with pytest.raises(ADCPTaskError) as error:
            await mounted.handler.get_reporting_status(
                req, ToolContext(caller_identity=s.binding.consumer_id)
            )
        assert error.value.errors[0].code == "UNAUTHORIZED"
        _, denied = await mounted.mcp(
            client, feed_request(s, pagination={"cursor": first["pagination"]["cursor"]})
        )
        assert error_code(denied) == "UNAUTHORIZED"


async def test_feed_mount_preserves_closed_tier_and_new_store_notification_admission(feeds):
    from adcp.reporting.ledger.notification_models import ReportingNotificationError
    from adcp.reporting.outbox import (
        InMemoryReportingOutbox,
        PgReportingOutbox,
        ReportingEnvelopeCipher,
        ReportingNotificationWorker,
    )

    class NoSubscriptions:
        async def list_active(self, **kwargs):
            raise AssertionError("an unadmitted store must not expand notification recipients")

        async def get_active(self, **kwargs):
            raise AssertionError("an unadmitted store must not authorize delivery")

    h = feeds
    s, _, _ = await mixed_case(h)
    mounted = MountedFeed(h)
    mounted.authorize(s)
    assert await mounted.handler.get_adcp_capabilities({}) == (
        await ADCPHandler().get_adcp_capabilities({})
    )
    before = without_feed(await h.image())
    async with mounted.client() as client:
        for transport in (mounted.mcp, mounted.a2a):
            _, raw = await transport(
                client,
                {},
                mutate_wire=lambda wire: wire.replace(
                    '"get_reporting_status"', '"get_adcp_capabilities"'
                ),
            )
            # The inherited default stub is unsupported; strict output schema
            # validation may translate that into its existing validation error.
            assert error_code(raw) in {"NOT_SUPPORTED", "VALIDATION_ERROR"}, raw
            assert all(
                field not in json.dumps(raw)
                for field in ("managed_delivery", "reconciled_billing", "delivery_ready")
            )
        # The mounted Core polling path is available while admission stays shut.
        _, page = await mounted.mcp(client, feed_request(s, limit=100))
        assert page["periods"] and page["revisions"]
    if h.pool is None and h.store._notification_state is None:
        with pytest.raises(ValueError, match="notifications=True"):
            InMemoryReportingOutbox(h.store)
        assert without_feed(await h.image()) == before
        return
    outbox = (
        PgReportingOutbox(pool=h.pool) if h.pool is not None else InMemoryReportingOutbox(h.store)
    )
    worker = ReportingNotificationWorker(
        outbox=outbox,
        subscriptions=NoSubscriptions(),
        cipher=ReportingEnvelopeCipher(b"e" * 32),
    )
    with pytest.raises(ReportingNotificationError, match="notification_chain_unready"):
        await worker.advertised_notifications(
            h.store, account_id=s.obligation.account_id, ready_scope=None
        )
    assert without_feed(await h.image()) == before


@pytest.mark.parametrize(
    "hydrated,registry_kind",
    [(False, None), (True, None), (True, "api_key"), (True, "oauth"), (True, "http_sig")],
)
async def test_mounted_mcp_to_a2a_continuation_reauthorizes_exact_canonical_consumer(
    feeds, hydrated, registry_kind
):
    h = feeds
    s, _, _ = await mixed_case(h, consumer_id="https://buyer.example.test/first")
    other = await second_consumer(h, s, "https://buyer.example.test/second")
    mounted = MountedFeed(h, hydrated=hydrated, registry_kind=registry_kind)
    mounted.authorize(s)
    mounted.authorize(other, token="token-two")
    req = GetReportingStatusRequest.model_validate(feed_request(s)).model_dump(
        mode="json", exclude_unset=True
    )
    before = without_feed(await h.image())
    async with mounted.client() as client:
        status, first = await mounted.mcp(client, req)
        assert status == 200 and first["pagination"]["total_count"] == 6, first
        GetReportingStatusResponse.model_validate(first)
        cursor = first["pagination"]["cursor"]
        continued = feed_request(s, pagination={"cursor": cursor, "max_results": 100})
        _, other_result = await mounted.a2a(client, continued, token="token-two")
        assert error_code(other_result) == "INVALID_CHECKPOINT", other_result
        for v1 in (False, True):
            status, tail = await mounted.a2a(client, continued, v1=v1)
            assert status == 200 and tail["pagination"]["has_more"] is False, tail
            assert tail["changes_checkpoint"] == first["changes_checkpoint"]
            GetReportingStatusResponse.model_validate(tail)
            assert tail["receipts"][0]["reporting_receipt_id"] == s.receipt.reporting_receipt_id
        mounted.grants.remove((s.obligation.account_id, s.binding.consumer_id))
        for transport in (mounted.mcp, mounted.a2a):
            _, denied = await transport(client, continued)
            assert error_code(denied) == "UNAUTHORIZED", denied
    assert len(mounted.auth_calls) >= 6
    assert without_feed(await h.image()) == before


@pytest.mark.parametrize("feedback", [False, True])
async def test_mounted_feed_private_counts_do_not_depend_on_feedback_flag(feeds, feedback):
    h = feeds
    s, _, _ = await mixed_case(h)
    other = await second_consumer(h, s)
    mounted = MountedFeed(h, feedback=feedback)
    mounted.authorize(s)
    mounted.authorize(other, token="token-two")
    async with mounted.client() as client:
        _, first = await mounted.mcp(client, feed_request(s, limit=100))
        _, second = await mounted.a2a(client, feed_request(other, limit=100), token="token-two")
        assert first["pagination"]["total_count"] == 6, first
        assert second["pagination"]["total_count"] == 5, second
        assert len(first["receipts"]) == len(second["receipts"]) == 1
        assert first["adjustment_receipts"] and second["adjustment_receipts"] == []
        assert first["changes_checkpoint"] != second["changes_checkpoint"]


async def test_mounted_malformed_scoped_positions_and_generic_cache_cannot_bypass_authorization(
    feeds,
):
    h = feeds
    s, _, _ = await mixed_case(h)
    mounted = MountedFeed(h)
    mounted.authorize(s)
    async with mounted.client() as client:
        _, page = await mounted.mcp(client, feed_request(s))
        mutations = [
            {"changes_after": "old-unbound-token"},
            {"changes_after": "x" * 2049},
            {"pagination": {"cursor": "x" * 2049}},
            {"pagination": {"max_results": 101}},
            {"consumer_id": "spoofed"},
            {"idempotency_key": "feed-must-not-cache"},
            {
                "delivery_config_ids": ["foreign"],
                "pagination": {"cursor": page["pagination"]["cursor"]},
            },
        ]
        for changes in mutations:
            for transport in (mounted.mcp, mounted.a2a):
                _, result = await transport(client, feed_request(s, **changes))
                assert error_code(result) in {"INVALID_REQUEST", "INVALID_CHECKPOINT"}, result
        # A registry revocation is independently re-resolved, even if the ACL
        # and opaque AccountStore cache key have remained unchanged.
        mounted.grants.clear()
        _, denied = await mounted.mcp(
            client,
            feed_request(
                s,
                pagination={"cursor": page["pagination"]["cursor"]},
                idempotency_key="feed-must-not-cache",
            ),
        )
        assert error_code(denied) in {"UNAUTHORIZED", "INVALID_REQUEST"}


@pytest.mark.parametrize("version", [None, "3.2.0-rc.3"])
async def test_actual_inventory_and_fallback_schemas_bound_positions_without_mutating_legacy(
    feeds, version
):
    h = feeds
    s, _, _ = await mixed_case(h)
    mounted = MountedFeed(h, version=version)
    mounted.authorize(s)
    before_schema = feed_schema("request")
    async with mounted.client() as client:
        _, inventory = await mounted.mcp(client, inventory=True)
        tool = next(t for t in inventory["tools"] if t["name"] == "get_reporting_status")
        validator = Draft7Validator(tool["inputSchema"], format_checker=FormatChecker())
        assert validator.is_valid(feed_request(s))
        assert not validator.is_valid(feed_request(s, changes_after="x" * 2049))
        assert not validator.is_valid(feed_request(s, pagination={"cursor": "x" * 2049}))
        _, page = await mounted.mcp(client, feed_request(s))
        assert Draft7Validator(tool["outputSchema"], format_checker=FormatChecker()).is_valid(page)
    assert feed_schema("request") == before_schema
    # Public definitions are copies, not aliases into memoized upstream state.
    definitions = get_tools_for_handler(mounted.handler)
    for definition in definitions:
        if definition["name"] == "get_reporting_status":
            definition["inputSchema"]["properties"]["changes_after"]["maxLength"] = 1
    assert feed_schema("request")["properties"]["changes_after"]["maxLength"] == 2048


async def test_direct_hydrated_identity_conflicts_and_revoked_registry_fail_closed(feeds):
    h = feeds
    s, _, _ = await mixed_case(h, consumer_id="https://buyer.example.test/identity")
    mounted = MountedFeed(h, hydrated=True, registry_kind="oauth")
    mounted.authorize(s)
    req = feed_request(s)
    contexts = [
        None,
        ToolContext(caller_identity="anonymous", tenant_id=s.binding.consumer_id),
        ToolContext(
            caller_identity=s.binding.consumer_id,
            metadata={"auth_info": AuthInfo(kind="oauth", principal="https://other.example.test/")},
        ),
        RequestContext(
            account=Account(id=s.obligation.account_id),
            caller_identity=s.binding.consumer_id,
            tenant_id=s.binding.consumer_id,
        ),
        RequestContext(
            account=Account(id="other-account"),
            caller_identity="opaque-cache-key",
            auth_principal=s.binding.consumer_id,
        ),
    ]
    for context in contexts:
        with pytest.raises(ADCPTaskError) as error:
            await mounted.handler.get_reporting_status(req, context)
        assert error.value.errors[0].code == "UNAUTHORIZED"
    async with mounted.client() as client:
        _, first = await mounted.mcp(client, req)
        mounted.registry.agents.clear()
        _, denied = await mounted.a2a(
            client, feed_request(s, pagination={"cursor": first["pagination"]["cursor"]})
        )
        assert error_code(denied) == "UNAUTHORIZED"


async def test_model_generation_fallback_still_mounts_bound_feed_schema_without_changing_core(
    feeds, monkeypatch
):
    from adcp.server import mcp_tools

    h = feeds
    s, _, _ = await mixed_case(h)
    mounted = MountedFeed(h)
    mounted.authorize(s)
    stub = {"type": "object", "properties": {"view": {"type": "string"}}}
    definitions = deepcopy(mcp_tools.ADCP_TOOL_DEFINITIONS)
    for definition in definitions:
        if definition["name"] == "get_reporting_status":
            definition["inputSchema"] = deepcopy(stub)
            definition["outputSchema"] = {"type": "object"}
    monkeypatch.setattr(mcp_tools, "ADCP_TOOL_DEFINITIONS", definitions)
    monkeypatch.setattr(mcp_tools, "_ensure_pydantic_schemas_applied", lambda names: None)

    class CoreOnly(ADCPHandler):
        advertised_tools = {"get_reporting_status"}

        async def get_reporting_status(self, params, context=None):
            return {}

    async with mounted.client() as client:
        _, inventory = await mounted.mcp(client, inventory=True)
        feed_tool = next(t for t in inventory["tools"] if t["name"] == "get_reporting_status")
        validator = Draft7Validator(feed_tool["inputSchema"])
        assert validator.is_valid(feed_request(s))
        assert not validator.is_valid(feed_request(s, pagination={"cursor": "x" * 2049}))
        _, response = await mounted.mcp(client, feed_request(s))
        assert response["pagination"]["total_count"] == 6
    core_tool = next(
        t for t in get_tools_for_handler(CoreOnly()) if t["name"] == "get_reporting_status"
    )
    assert core_tool["inputSchema"] == stub


@pytest.mark.parametrize("feed_first", [True, False], ids=["feed-first", "receipts-first"])
async def test_paired_handler_instances_keep_mcp_a2a_inventory_calls_and_authorization_isolated(
    feeds, feed_first
):
    """Both construction/mount orders exercise the same class-level registry."""
    h = feeds
    s, original_request, original_response = await mixed_case(h)
    other_consumer = await second_consumer(h, s)
    other_account = await receipt_case(h, account_id="paired-account")
    await h.store.ingest_receipt_batch(
        request_for(other_account), caller=other_account.binding.principal
    )
    backend = "postgres" if h.pool is not None else "memory"
    notifications = (
        h.store._notifications_enabled
        if h.pool is not None
        else h.store._notification_state is not None
    )
    async with receipt_harness(backend, notifications=notifications) as old:
        # Same account/consumer/materialization/receipt/key on distinct stores.
        # The receipt-only handler has no adjustment, so cached cross-handler
        # admission or replay would give the wrong exact batch response.
        legacy = await receipt_case(old)
        constructors = {
            "feed": lambda: MountedFeed(h, hydrated=True),
            "receipts": lambda: MountedReceipts(old, hydrated=True),
        }
        order = ("feed", "receipts") if feed_first else ("receipts", "feed")
        mounts = {name: constructors[name]() for name in order}
        feed, receipts = mounts["feed"], mounts["receipts"]
        assert type(feed.handler) is type(receipts.handler) is ReportingReceiptHandler
        feed.authorize(s)
        feed.authorize(other_consumer, token="token-two")
        feed.authorize(other_account, token="token-account")
        receipts.authorize(legacy)
        unsupported = await ADCPHandler().get_reporting_status(feed_request(s))
        assert await receipts.handler.get_reporting_status(feed_request(s)) == unsupported
        async with AsyncExitStack() as stack:
            clients = {
                name: await stack.enter_async_context(mounts[name].client()) for name in order
            }
            fc, rc = clients["feed"], clients["receipts"]
            expected = {
                "feed": {
                    "get_adcp_capabilities",
                    "sync_reporting_receipts",
                    "get_reporting_status",
                },
                "receipts": {"get_adcp_capabilities", "sync_reporting_receipts"},
            }
            for name in order + tuple(reversed(order)):
                mount, client = mounts[name], clients[name]
                _, inventory = await mount.mcp(client, inventory=True)
                assert {t["name"] for t in inventory["tools"]} == expected[name]
                for path in ("/.well-known/agent.json", "/.well-known/agent-card.json"):
                    response = await client.get(path)
                    assert response.status_code == 200, response.text
                    assert {skill["id"] for skill in response.json()["skills"]} == expected[name]
            _, first = await feed.mcp(fc, feed_request(s))
            assert first["pagination"]["total_count"] == 6
            continuation = feed_request(s, pagination={"cursor": first["pagination"]["cursor"]})
            # Same idempotency key and external IDs remain store/handler scoped.
            _, legacy_response = await receipts.mcp(rc, request_for(legacy))
            assert len(legacy_response["results"]) == 1
            for v1 in (False, True):
                _, replay = await receipts.a2a(rc, request_for(legacy), v1=v1)
                assert replay == legacy_response
                _, feed_replay = await MountedReceipts.a2a(feed, fc, original_request, v1=v1)
                assert feed_replay == original_response
                _, unsupported_wire = await receipts.a2a(
                    rc,
                    continuation,
                    v1=v1,
                    mutate_wire=lambda wire: wire.replace(
                        '"skill": "sync_reporting_receipts"', '"skill": "get_reporting_status"'
                    ),
                )
                assert "Unknown skill: get_reporting_status" in json.dumps(unsupported_wire)
            # MCP's unmapped task has the legacy protocol error, not a feed page.
            headers = {
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer token-one",
            }
            if receipts.sessions["token-one"] is not None:
                headers["mcp-session-id"] = receipts.sessions["token-one"]
            response = await rc.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 901,
                    "method": "tools/call",
                    "params": {"name": "get_reporting_status", "arguments": continuation},
                },
            )
            assert response.status_code == 200
            assert "Unknown tool: get_reporting_status" in response.text
            assert first["ledger_snapshot_id"] not in response.text
            # Revocation on one handler cannot be masked by authorization of
            # the same account/consumer on its neighbor.
            receipts.grants.clear()
            _, denied = await receipts.a2a(rc, request_for(legacy))
            assert error_code(denied) == "UNAUTHORIZED"
            _, feed_tail = await feed.a2a(fc, continuation)
            assert feed_tail["ledger_snapshot_id"] == first["ledger_snapshot_id"]
            for token, subject, total in (
                ("token-two", other_consumer, 5),
                ("token-account", other_account, 4),
            ):
                for transport in (feed.mcp, feed.a2a):
                    _, crossed = await transport(
                        fc,
                        feed_request(subject, pagination=continuation["pagination"]),
                        token=token,
                    )
                    assert error_code(crossed) == "INVALID_CHECKPOINT"
                    _, own = await transport(fc, feed_request(subject, limit=100), token=token)
                    assert own["pagination"]["total_count"] == total
                    assert own["ledger_snapshot_id"] != first["ledger_snapshot_id"]
                    assert own["adjustment_receipts"] == []
            receipts.grants.add((legacy.obligation.account_id, legacy.binding.consumer_id))
            feed.grants.remove((s.obligation.account_id, s.binding.consumer_id))
            for transport in (feed.mcp, feed.a2a):
                _, denied = await transport(fc, continuation)
                assert error_code(denied) == "UNAUTHORIZED"
            _, replay = await receipts.mcp(rc, request_for(legacy))
            assert replay == legacy_response
            assert await receipts.handler.get_reporting_status(feed_request(s)) == unsupported
            _, inventory = await receipts.mcp(rc, inventory=True)
            assert {t["name"] for t in inventory["tools"]} == expected["receipts"]
