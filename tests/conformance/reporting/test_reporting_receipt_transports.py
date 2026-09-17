"""Mounted MCP/A2A auth, canonical identity, raw wire shape and durable replay."""

from copy import deepcopy
from dataclasses import replace

import pytest
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from jsonschema import Draft7Validator

from adcp.decisioning import Account, AuthInfo, RequestContext
from adcp.decisioning.registry import BuyerAgent, HttpSigCredential
from adcp.exceptions import ADCPTaskError
from adcp.reporting.receipts.wire import receipt_schema
from adcp.server import ToolContext

from ._receipt_support import adjustment_for, receipt_case, receipt_harness, receipts, request_for
from ._receipt_transport import MountedReceipts, error_code

__all__ = ["receipts"]


@pytest.mark.parametrize("hydrated", [False, True])
async def test_actual_mounts_reauthorize_exact_replay_and_bypass_both_generic_caches(
    receipts, hydrated
):
    h = receipts
    one = await receipt_case(h, consumer_id="https://buyer.example.test/one")
    two = await receipt_case(h, consumer_id="https://buyer.example.test/two")
    mount = MountedReceipts(h, hydrated=hydrated)
    mount.authorize(one)
    mount.authorize(two, token="token-two")
    adjustment = await adjustment_for(h, one)
    request = request_for(
        one,
        adjustment_receipts=[adjustment],
        context={"buyer": "original context", "\ue000": "BMP", "\U00010000": "supplementary"},
    )
    async with mount.client() as client:
        code, first = await mount.mcp(client, request)
        assert code == 200 and first.get("status") == "completed", first
        assert [r["result"] for r in first["results"]] == ["recorded", "recorded"]
        _, replay = await mount.a2a(client, deepcopy(request))
        assert replay == first
        assert "replayed" not in replay
        _, sibling = await mount.mcp(client, request, token="token-two")
        assert [r["result"] for r in sibling["results"]] == ["recorded", "recorded"]
        assert await h.store.get_receipt(one.receipt.key)
        assert await h.store.get_receipt(two.receipt.key)
        assert len(mount.auth_calls) == 3
        if hydrated:
            assert len({c.caller_identity for c in mount.contexts}) == 1
        mount.grants.remove((one.obligation.account_id, one.binding.consumer_id))
        before = await h.image()
        for call in (mount.mcp, mount.a2a):
            _, denied = await call(client, request)
            assert error_code(denied) == "UNAUTHORIZED"
        assert await h.image() == before
        assert len(mount.auth_calls) == 5
        del mount.tokens["token-one"]
        for call in (mount.mcp, mount.a2a):
            code, _ = await call(client, request)
            assert code == 401


async def test_same_consumer_key_across_two_accounts_is_not_a_generic_cache_collision(receipts):
    h = receipts
    first = await receipt_case(h, account_id="account:a", consumer_id="b:c")
    second = await receipt_case(h, account_id="account:a:b", consumer_id="b:c")
    mount = MountedReceipts(h)
    mount.authorize(first)
    mount.grants.add((second.obligation.account_id, second.binding.consumer_id))
    mount.accounts[second.obligation.account_id] = second.obligation.account_id
    async with mount.client() as client:
        _, a = await mount.mcp(client, request_for(first))
        _, b = await mount.a2a(client, request_for(second))
        assert a["results"][0]["result"] == b["results"][0]["result"] == "recorded"
        _, replay = await mount.mcp(client, request_for(second))
        assert replay == b
        changed = request_for(second, context={"changed": True})
        _, conflict = await mount.a2a(client, changed)
        assert error_code(conflict) == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("first_route", ["mcp", "a2a"])
@pytest.mark.parametrize("v1", [False, True])
async def test_protobuf_integer_spellings_preserve_whole_request_identity_and_timestamps(
    receipts, first_route, v1
):
    h = receipts
    s = await receipt_case(h)
    mount = MountedReceipts(h, hydrated=True, version="3.2-rc.3")
    mount.authorize(s)
    request = request_for(
        s,
        context={"integer": 42, "nested": [100], "bounds": [-9007199254740991, 9007199254740991]},
    )
    # Use the actual protobuf conversion that an A2A client performs. Python's
    # JSON printer then emits 1.0, so reading the original HTTP bytes must accept
    # that exact integral spelling without rounding arbitrary fractional input.
    proto = Value()
    ParseDict(request, proto)
    protobuf_request = MessageToDict(proto)
    assert type(protobuf_request["receipts"][0]["observed_row_count"]) is float
    assert type(protobuf_request["context"]["integer"]) is float
    assert Draft7Validator(receipt_schema("request", version="3.2-rc.3")).is_valid(protobuf_request)
    requests = {"mcp": request, "a2a": protobuf_request}
    other = "a2a" if first_route == "mcp" else "mcp"
    async with mount.client() as client:
        _, first = await getattr(mount, first_route)(
            client, requests[first_route], **({"v1": v1} if first_route == "a2a" else {})
        )
        assert first["results"][0]["result"] == "recorded", first
        _, replay = await getattr(mount, other)(
            client, requests[other], **({"v1": v1} if other == "a2a" else {})
        )
        assert replay == first
        assert replay["context"] == request["context"]
        _, exponent = await mount.mcp(
            client,
            request,
            mutate_wire=lambda w: w.replace(
                '"observed_row_count": 1', '"observed_row_count": 1.0e+0'
            ),
        )
        assert exponent == first
        assert len(mount.auth_calls) == 3


@pytest.mark.parametrize("chunk_size", [1, 257])
async def test_request_bound_capture_preserves_chunked_bytes_and_read_ahead_overflow(
    receipts, monkeypatch, chunk_size
):
    h = receipts
    s = await receipt_case(h)
    mount = MountedReceipts(h, hydrated=True)
    mount.authorize(s)
    # Shorten only the read-ahead window; the raw capture still has its real
    # 10 MiB bound. The rest of this valid request must reach the normal decoder.
    monkeypatch.setattr("adcp.reporting.receipts.transport.MAX_RECEIPT_BODY_BYTES", 256)
    request = request_for(s, context={"original": "same request bytes"})

    def chunked(wire):
        async def chunks():
            raw = wire.encode()
            for i in range(0, len(raw), chunk_size):
                yield b""
                yield raw[i : i + chunk_size]

        return chunks()

    async with mount.client() as client:
        _, first = await mount.a2a(client, request, mutate_wire=chunked)
        assert first["results"][0]["result"] == "recorded", first
        assert (await mount.mcp(client, request))[1] == first
        assert len(mount.auth_calls) == 2


@pytest.mark.parametrize("registry_kind", ["api_key", "oauth", "http_sig"])
async def test_api_oauth_signed_registry_identity_is_refreshed_on_mounted_replay(
    receipts, registry_kind
):
    h = receipts
    s = await receipt_case(h, consumer_id="https://buyer.example.test/agent")
    mount = MountedReceipts(h, hydrated=True, registry_kind=registry_kind)
    mount.authorize(s)
    async with mount.client() as client:
        _, first = await mount.mcp(client, request_for(s))
        assert first["results"][0]["result"] == "recorded", first
        _, replay = await mount.a2a(client, request_for(s))
        assert replay == first
        assert len(mount.registry.calls) >= 2
        for key, agent in mount.registry.agents.items():
            mount.registry.agents[key] = replace(agent, status="blocked")
        before = await h.image()
        _, denied = await mount.a2a(client, request_for(s))
        assert error_code(denied) == "UNAUTHORIZED"
        assert await h.image() == before


@pytest.mark.parametrize(
    "bad",
    [
        "absent",
        "anonymous",
        "tenant_only",
        "cache_only",
        "auth_conflict",
        "signed_conflict",
        "agent_conflict",
        "metadata_conflict",
        "account_conflict",
    ],
)
async def test_all_trusted_identities_must_agree_and_cache_and_tenant_never_identify_consumer(bad):
    async with receipt_harness("memory") as h:
        s = await receipt_case(h, consumer_id="https://buyer.example.test/agent")
        mount = MountedReceipts(h, hydrated=True)
        mount.authorize(s)
        consumer = s.binding.consumer_id
        context = RequestContext(
            caller_identity="cache-only",
            account=Account(id=s.obligation.account_id),
            auth_info=AuthInfo(kind="bearer", principal=consumer, credential=None),
            auth_principal=consumer,
            buyer_agent=BuyerAgent(consumer, "Buyer", "active"),
        )
        if bad == "absent":
            context = ToolContext()
        elif bad == "anonymous":
            context = ToolContext(caller_identity="anonymous")
        elif bad == "tenant_only":
            context = ToolContext(tenant_id=consumer)
        elif bad == "cache_only":
            context = replace(
                context,
                auth_info=None,
                auth_principal=None,
                buyer_agent=None,
                caller_identity=consumer,
            )
        elif bad == "auth_conflict":
            context = replace(
                context, auth_info=AuthInfo(kind="bearer", principal="other", credential=None)
            )
        elif bad == "signed_conflict":
            context = replace(
                context,
                auth_info=AuthInfo(
                    kind="http_sig",
                    principal=consumer,
                    credential=HttpSigCredential(
                        "http_sig", "key", "https://other.example.test/agent", 1.0
                    ),
                ),
            )
        elif bad == "agent_conflict":
            context = replace(
                context,
                buyer_agent=BuyerAgent("https://other.example.test/agent", "Other", "active"),
            )
        elif bad == "metadata_conflict":
            context.metadata["adcp.auth_info"] = AuthInfo(
                kind="bearer", principal="other", credential=None
            )
        else:
            context = replace(context, account=replace(context.account, id="other-account"))
        before = await h.image()
        with pytest.raises(ADCPTaskError) as error:
            await mount.handler.sync_reporting_receipts(request_for(s), context)
        assert error.value.errors[0].code == "UNAUTHORIZED"
        assert await h.image() == before


@pytest.mark.parametrize(
    "bad", ["empty", "empty_adjustments", "received_at", "duplicate", "combined101", "spoof"]
)
async def test_actual_mount_shape_failure_is_task_invalid_request_with_no_writes(bad):
    async with receipt_harness("memory") as h:
        s = await receipt_case(h)
        mount = MountedReceipts(h)
        mount.authorize(s)
        request = request_for(s)
        if bad == "empty":
            request["receipts"] = []
        elif bad == "empty_adjustments":
            request["adjustment_receipts"] = []
        elif bad == "received_at":
            request["receipts"][0]["received_at"] = "2026-09-01T02:00:00Z"
        elif bad == "duplicate":
            request["receipts"] *= 2
        elif bad == "spoof":
            request["consumer_id"] = "someone-else"
        else:
            request["receipts"] = [
                {**request["receipts"][0], "reporting_receipt_id": f"receipt-{i:016d}"}
                for i in range(101)
            ]
        before = await h.image()
        async with mount.client(validation=None) as client:
            for call in (mount.mcp, mount.a2a):
                _, response = await call(client, request)
                assert error_code(response) == "INVALID_REQUEST"
        assert await h.image() == before
        assert mount.auth_calls == []


@pytest.mark.parametrize("version", [None, "3.2-rc.3"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_pinned_unpinned_and_fallback_mcp_schemas_keep_combined_and_received_rules(
    version, fallback, monkeypatch
):
    async with receipt_harness("memory") as h:
        s = await receipt_case(h)
        adjustment = await adjustment_for(h, s)
        mount = MountedReceipts(h, version=version)
        mount.authorize(s)
        if fallback:
            monkeypatch.setattr(
                "adcp.server.mcp_tools._ensure_pydantic_schemas_applied", lambda _: None
            )
        async with mount.client() as client:
            _, inventory = await mount.mcp(client, inventory=True)
            definition = next(
                t for t in inventory["tools"] if t["name"] == "sync_reporting_receipts"
            )
            request = request_for(s, adjustment_receipts=[adjustment])
            validator = Draft7Validator(definition["inputSchema"])
            assert validator.is_valid(request)
            assert validator.is_valid({k: v for k, v in request.items() if k != "receipts"})
            for change in (
                {"receipts": []},
                {"adjustment_receipts": []},
                {"receipts": [{**request["receipts"][0], "received_at": "2026-09-01T02:00:00Z"}]},
                {"receipts": request["receipts"] * 51, "adjustment_receipts": [adjustment] * 50},
            ):
                assert not validator.is_valid({**request, **change})
            absent = {
                k: v for k, v in request.items() if k not in {"receipts", "adjustment_receipts"}
            }
            assert not validator.is_valid(absent)
            _, result = await mount.mcp(client, request)
            assert Draft7Validator(definition["outputSchema"]).is_valid(result), result
            del result["results"][0]["receipt"]["received_at"]
            assert not Draft7Validator(definition["outputSchema"]).is_valid(result)
        # Per-mount overlays never mutate another caller's cached schema.
        definition["inputSchema"]["anyOf"] = []
        assert receipt_schema("request")["anyOf"]


async def test_receipt_hook_rewrite_rejected_and_response_enhancer_cannot_mutate_replay():
    async with receipt_harness("memory") as h:
        s = await receipt_case(h)
        mount = MountedReceipts(h)
        mount.authorize(s)
        request = request_for(s)
        original = deepcopy(request)

        def rewrite(task, params):
            params["receipts"][0]["consumer_commit_ref"] = "changed"
            return params

        async with mount.client(
            pre_validation_hooks={"sync_reporting_receipts": [rewrite]}
        ) as client:
            _, error = await mount.mcp(client, request)
            assert error_code(error) == "INVALID_REQUEST"
        assert request == original
        assert not await h.store.get_receipt(s.receipt.key)
        mount.sessions.clear()

        def enhancer(*args):
            raise AssertionError("a receipt response must not be enhanced")

        async with mount.client(response_enhancer=enhancer) as client:
            _, first = await mount.mcp(client, request)
            _, replay = await mount.a2a(client, request)
        assert first == replay and first["results"][0]["result"] == "recorded"


@pytest.mark.parametrize("v1", [False, True])
async def test_a2a_to_mcp_replay_after_mount_restart_keeps_every_original_ordinal_and_timestamp(
    receipts, v1
):
    h = receipts
    s = await receipt_case(h, consumer_id="https://buyer.example.test/restarted")
    adjustment = await adjustment_for(h, s)
    request = request_for(s, adjustment_receipts=[adjustment])
    request["receipts"].insert(
        0,
        {
            **request["receipts"][0],
            "reporting_receipt_id": "failed-ordinal-0000",
            "reporting_revision_id": "unknown",
        },
    )
    mount = MountedReceipts(h, hydrated=True)
    mount.authorize(s)
    async with mount.client() as client:
        _, first = await mount.a2a(client, request, v1=v1)
        assert [r["result"] for r in first["results"]] == ["failed", "recorded", "recorded"], first
    if h.pool is not None:
        from adcp.reporting.receipts import PgReportingReceiptStore

        h.store = PgReportingReceiptStore(pool=h.pool, notifications=h.store._notifications_enabled)
    restarted = MountedReceipts(h, hydrated=True)
    restarted.authorize(s)
    async with restarted.client() as client:
        _, replay = await restarted.mcp(client, request)
        assert replay == first
        _, again = await restarted.a2a(client, request, v1=v1)
        assert again == first
        assert len(restarted.auth_calls) == 2
        assert "replayed" not in again


@pytest.mark.parametrize(
    "fault",
    [
        "fractional",
        "rounded_fraction",
        "deep_rounded_fraction",
        "unsafe_integer",
        "unsafe_exponent",
        "unrepresentable_exponent",
        "negative_integer",
        "nonfinite",
        "boolean_count",
        "numeric_string_field",
        "duplicate_key",
        "duplicate_nested",
        "empty_array",
        "metadata_shortcut",
        "unpaired_surrogate",
    ],
)
@pytest.mark.parametrize("route", ["mcp", "a2a"])
async def test_real_raw_strict_json_and_numeric_admission_fail_before_batch_mutation(
    receipts, fault, route
):
    h = receipts
    s = await receipt_case(h)
    mount = MountedReceipts(h, version="3.2-rc.3")
    mount.authorize(s)
    request = request_for(s)
    mutations = {
        "fractional": lambda w: w.replace('"observed_row_count": 1', '"observed_row_count": 1.5'),
        "rounded_fraction": lambda w: w.replace(
            '"observed_row_count": 1', '"observed_row_count": 1.0000000000000001'
        ),
        "deep_rounded_fraction": lambda w: w.replace(
            '"observed_row_count": 1', '"observed_row_count": 1.000000000000000000001'
        ),
        "unsafe_integer": lambda w: w.replace(
            '"observed_row_count": 1', '"observed_row_count": 9007199254740993'
        ),
        "unsafe_exponent": lambda w: w.replace(
            '"observed_row_count": 1', '"observed_row_count": 9.007199254740993e15'
        ),
        "unrepresentable_exponent": lambda w: w.replace(
            '"observed_row_count": 1', '"observed_row_count": 1e99999999999999999999999'
        ),
        "negative_integer": lambda w: w.replace(
            '"observed_row_count": 1', '"observed_row_count": -1.0'
        ),
        "nonfinite": lambda w: w.replace('"observed_row_count": 1', '"observed_row_count": NaN'),
        "boolean_count": lambda w: w.replace(
            '"observed_row_count": 1', '"observed_row_count": true'
        ),
        "duplicate_key": lambda w: w.replace(
            '"account": {', '"account": {"account_id":"other"}, "account": {'
        ),
        "duplicate_nested": lambda w: w.replace(
            '"observed_row_count": 1', '"observed_row_count": 999, "observed_row_count": 1'
        ),
    }
    if fault == "empty_array":
        request["receipts"] = []
    if fault == "unpaired_surrogate":
        request["context"] = {"unicode": "\ud800"}
    if fault == "numeric_string_field":
        request["receipts"][0]["observed_control_totals"][0]["value"] = 1.0
    if fault == "metadata_shortcut":
        request["receipts"] = []
        request["context"] = {
            "adcp.receipt_ingress.raw_body": request_for(s),
            "adcp.a2a_parsed_request": request_for(s),
        }
    before = await h.image()
    async with mount.client(validation=None) as client:
        _, result = await getattr(mount, route)(client, request, mutate_wire=mutations.get(fault))
        if fault == "unpaired_surrogate":
            assert result["error"]["code"] == -32700, result
        else:
            assert error_code(result) == "INVALID_REQUEST", result
    assert await h.image() == before
    assert mount.auth_calls == []


@pytest.mark.parametrize("route", ["mcp", "a2a"])
async def test_malformed_numeric_token_is_rejected_by_the_actual_json_decoder(receipts, route):
    h = receipts
    s = await receipt_case(h)
    mount = MountedReceipts(h, version="3.2-rc.3")
    mount.authorize(s)
    request = request_for(s)
    before = await h.image()
    async with mount.client() as client:
        status, response = await getattr(mount, route)(
            client,
            request,
            mutate_wire=lambda w: w.replace('"observed_row_count": 1', '"observed_row_count": 1e'),
        )
        # This invalid JSON cannot reach task admission. Preserve each real
        # transport's parse-error envelope instead of inventing a task result.
        assert status in {200, 400, 422}, response
        if status == 200:
            assert response["error"]["code"] in {-32700, -32600, -32602}, response
    assert await h.image() == before
    assert mount.auth_calls == []


@pytest.mark.parametrize("route", ["mcp", "a2a"])
async def test_raw_capture_cannot_supply_auth_or_cross_request_body_or_override_actual_account(
    route,
):
    async with receipt_harness("memory") as h:
        s = await receipt_case(h)
        mount = MountedReceipts(h)
        mount.authorize(s)
        request = request_for(s)
        async with mount.client() as client:
            call = getattr(mount, route)
            _, first = await call(client, request)
            assert first["results"][0]["result"] == "recorded"
            before = await h.image()
            _, invalid = await call(client, {**request, "receipts": []})
            assert error_code(invalid) == "INVALID_REQUEST"
            changed = {
                **request,
                "account": {"account_id": "other"},
                "context": {"adcp.receipt_ingress.raw_body": request},
            }
            _, denied = await call(client, changed)
            assert error_code(denied) == "UNAUTHORIZED"
            if route == "mcp":
                # Avoid a fresh initialize in this helper: the authenticated
                # tool request itself must reject a forged token.
                mount.sessions["forged"] = mount.sessions["token-one"]
            assert (await call(client, request, token="forged"))[0] == 401
            assert await h.image() == before


async def test_unsupported_version_rejected_on_both_routes_before_ingress():
    async with receipt_harness("memory") as h:
        s = await receipt_case(h)
        mount = MountedReceipts(h)
        mount.authorize(s)
        before = await h.image()
        async with mount.client() as client:
            for call in (mount.mcp, mount.a2a):
                _, denied = await call(client, request_for(s, adcp_version="99.0"))
                assert error_code(denied) == "VERSION_UNSUPPORTED"
        assert await h.image() == before


@pytest.mark.parametrize("route", ["mcp", "a2a"])
async def test_raw_capture_size_bound_and_fallback_are_closed(monkeypatch, route):
    from adcp.reporting.receipts.transport import receipt_body_receive

    async with receipt_harness("memory") as h:
        s = await receipt_case(h)
        mount = MountedReceipts(h)
        mount.authorize(s)

        def bounded(scope, receive, **kwargs):
            return receipt_body_receive(scope, receive, limit=128)

        monkeypatch.setattr("adcp.reporting.receipts.transport.receipt_body_receive", bounded)
        before = await h.image()
        async with mount.client() as client:
            _, result = await getattr(mount, route)(client, request_for(s))
            assert error_code(result) == "INVALID_REQUEST"
        assert await h.image() == before


@pytest.mark.parametrize("route", ["mcp", "a2a"])
async def test_generic_middleware_cannot_replace_the_authenticated_whole_request(route):
    async with receipt_harness("memory") as h:
        s = await receipt_case(h)
        mount = MountedReceipts(h, hydrated=True)
        mount.authorize(s)
        request = request_for(s, context={"attempt": 1})

        async def rewrite(name, params, context, call_next):
            params["context"]["attempt"] = 2
            return await call_next()

        mount.middleware = rewrite
        before = await h.image()
        async with mount.client() as client:
            _, result = await getattr(mount, route)(client, request)
            assert error_code(result) == "INVALID_REQUEST"
        assert await h.image() == before
        assert mount.auth_calls == []


async def test_raw_mounted_requests_do_not_alias_account_consumer_pairs_or_cached_context(receipts):
    h = receipts
    first = await receipt_case(h, account_id="acct:a", consumer_id="b:c")
    second = await receipt_case(h, account_id="acct:a:b", consumer_id="c")
    mount = MountedReceipts(h, hydrated=True)
    mount.authorize(first)
    mount.authorize(second, token="token-two")
    async with mount.client() as client:
        _, a = await mount.mcp(client, request_for(first))
        _, b = await mount.a2a(client, request_for(second), token="token-two")
        assert a["results"][0]["result"] == b["results"][0]["result"] == "recorded"
        assert a != b
        assert (await mount.a2a(client, request_for(first)))[1] == a
        assert (await mount.mcp(client, request_for(second), token="token-two"))[1] == b
        before = await h.image()
        assert error_code((await mount.a2a(client, request_for(second)))[1]) == "UNAUTHORIZED"
        assert (
            error_code((await mount.mcp(client, request_for(first), token="token-two"))[1])
            == "UNAUTHORIZED"
        )
        assert await h.image() == before
    assert len({c.caller_identity for c in mount.contexts}) == 1
