"""Binding authorization stays fresh independently of transport key discovery."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.requests import Request
from starlette.responses import JSONResponse

from adcp.decisioning.types import AdcpError
from adcp.server.signed_requests import (
    RequestSignatureVerification,
    SignedRequestVerificationMiddleware,
    apply_verified_signer,
    check_wba_authorization,
    is_wba_spend_committing,
)
from adcp.server.translate import build_mcp_error_result
from adcp.signing import (
    OriginBindings,
    StaticSignerKeys,
    TrustRecordEvaluator,
    TrustResponse,
    VerifiedSigner,
    public_directory_key,
    sign_request,
    sign_wba_request,
)
from adcp.signing.middleware import VERIFIED_SIGNER_SCOPE_KEY
from adcp.signing.trust import TrustRecordMatchError, TrustRecordNotFoundError

ORIGIN = "https://buyer.platform.com"
TARGET = "https://seller.com/mcp"


async def not_revoked(origin, keyid):
    return False


def test_a2a_list_tasks_alias_is_nonspend_and_unknown_operations_fail_closed():
    assert not is_wba_spend_committing("ListTasks")
    assert not is_wba_spend_committing("tasks/list")
    assert not is_wba_spend_committing("GetTask")
    assert is_wba_spend_committing("FutureSpendTask")
    assert is_wba_spend_committing("")


@pytest.mark.parametrize("revoked", [False, True, "unavailable"])
@pytest.mark.parametrize("transport", ["mcp", "a2a"])
async def test_standalone_middleware_never_calls_app_that_ignores_pending_denial(
    revoked, transport
):
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    invoked = []

    async def check_key(origin, keyid):
        if revoked == "unavailable":
            raise OSError("private outage details")
        return revoked

    async def unsafe_dispatch(scope, receive, send):
        invoked.append(True)
        await JSONResponse({"side_effect": True})(scope, receive, send)

    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({ORIGIN: {"keys": [jwk]}}),
        enable_web_bot_auth=True,
        wba_revocation_checker=check_key,
    )
    app = SignedRequestVerificationMiddleware(unsafe_dispatch, config, transport=transport)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "create_media_buy", "arguments": {}},
    }
    if transport == "a2a":
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "guard",
                    "role": "user",
                    "parts": [
                        {"kind": "data", "data": {"skill": "create_media_buy", "parameters": {}}}
                    ],
                }
            },
        }
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(
        sign_wba_request(
            method="POST",
            url=TARGET,
            headers=headers,
            body=body,
            private_key=private,
            agent_origin=ORIGIN,
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
        response = await client.post(TARGET, content=body, headers=headers)
    assert response.status_code == (503 if revoked == "unavailable" else 401)
    if revoked == "unavailable":
        assert response.json() == {
            "errors": [
                {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Signer identity is temporarily unavailable",
                    "recovery": "transient",
                }
            ]
        }
    else:
        assert response.json() == {"error": "unauthenticated"}
    assert "Accept-Signature" not in response.headers
    assert not invoked


@asynccontextmanager
async def bound_client(
    *, transport="mcp", unbound_for=(), spend_classifier=None, defer_authorization_errors=True
):
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    now, mode = [1000], ["valid"]

    async def fetch(url, *, allow_private=False):
        if mode[0] == "offline_late":
            now[0] = 1300
            raise OSError("fetch crossed the grace deadline")
        if mode[0] == "offline":
            raise OSError("offline")
        if mode[0] == "removed":
            return TrustResponse(b"", 404)
        if mode[0] == "invalid":
            return TrustResponse(b'{"agents":NaN}')
        roles = ["adcp:sales"] if mode[0] == "role_changed" else ["adcp:buying"]
        agents = [] if mode[0] == "delisted" else [{"url": ORIGIN, "roles": roles}]
        return TrustResponse(json.dumps({"agents": agents}).encode())

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    bindings = OriginBindings()
    approved = await records.bind(
        VerifiedSigner(
            jwk["kid"], "ed25519", "sig1", time.time(), agent_url=ORIGIN, profile="web-bot-auth"
        ),
        principal_id="principal-1",
        bindings=bindings,
    )
    assert approved.operator_domain == "platform.com"
    options = {"spend_classifier": spend_classifier} if spend_classifier is not None else {}
    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({ORIGIN: {"keys": [jwk]}}),
        enable_web_bot_auth=True,
        origin_bindings=bindings,
        trust_records=records,
        wba_revocation_checker=not_revoked,
        unbound_for=frozenset(unbound_for),
        **options,
    )
    observed = []

    async def dispatch(scope, receive, send):
        context = apply_verified_signer(None, Request(scope))
        try:
            check_wba_authorization(context)
        except AdcpError as exc:
            await JSONResponse(build_mcp_error_result(exc).model_dump(mode="json", by_alias=True))(
                scope, receive, send
            )
            return
        principal = context.metadata["adcp.auth_info"].extra["principal_id"]
        user = scope.get("user")
        observed.append((principal, getattr(user, "display_name", None)))
        await JSONResponse({"principal": principal})(scope, receive, send)

    app = SignedRequestVerificationMiddleware(
        dispatch, config, transport=transport, defer_authorization_errors=defer_authorization_errors
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:

        async def post(operation, arguments=None, *, method="tools/call"):
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": (
                    {"name": operation, "arguments": arguments or {}}
                    if method == "tools/call"
                    else {"id": "owned-task"}
                ),
            }
            body = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json"}
            headers.update(
                sign_wba_request(
                    method="POST",
                    url=TARGET,
                    headers=headers,
                    body=body,
                    private_key=private,
                    agent_origin=ORIGIN,
                )
            )
            return await client.post(TARGET, content=body, headers=headers)

        yield post, now, mode, bindings, records, observed, config, dispatch


@pytest.mark.parametrize("mode_name", ["delisted", "removed", "role_changed"])
async def test_delisted_binding_retains_anonymous_reads_principal_and_prevents_rebinding(mode_name):
    async with bound_client(unbound_for={"get_products"}) as (
        post,
        now,
        mode,
        bindings,
        records,
        observed,
        config,
        dispatch,
    ):
        assert (await post("get_products")).json()["principal"] == "principal-1"
        mode[0], now[0] = mode_name, 1061
        response = await post("get_products")
        assert response.json()["principal"] is None
        assert (await post("get_principal")).json()["principal"] is None
        assert await bindings(ORIGIN) == "principal-1"
        signer = VerifiedSigner(
            "kid", "ed25519", "sig1", time.time(), agent_url=ORIGIN, profile="web-bot-auth"
        )
        with pytest.raises((TrustRecordMatchError, ValueError)):
            await records.bind(signer, principal_id="principal-1", bindings=bindings)
        mode[0], now[0] = "offline", 1122
        assert (await post("get_products")).json()["principal"] is None
        response = await post("create_media_buy")
        assert response.json()["structuredContent"]["adcp_error"]["code"] == "PERMISSION_DENIED"
        assert "Accept-Signature" not in response.headers


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("get_media_buys", {}),
        ("sync_creatives", {}),
        ("control_media_buy", {"paused": True}),
        ("control_media_buy", {"canceled": True}),
        ("update_media_buy", {"name": "new label"}),
    ],
)
async def test_operator_outage_keeps_cached_binding_for_nonspend_calls(operation, arguments):
    async with bound_client() as (post, now, mode, bindings, records, observed, config, dispatch):
        assert (await post(operation, arguments)).json()["principal"] == "principal-1"
        mode[0], now[0] = "offline", 1061
        assert (await post(operation, arguments)).json()["principal"] == "principal-1"


async def test_binding_outage_grace_boundary_is_shared_across_transport_ports():
    async with bound_client() as (post, now, mode, bindings, records, observed, config, dispatch):
        assert (await post("get_media_buys")).json()["principal"] == "principal-1"
        mode[0], now[0] = "offline", 1299.999
        assert (await post("get_media_buys")).json()["principal"] == "principal-1"
        a2a = SignedRequestVerificationMiddleware(
            dispatch, config, transport="a2a", defer_authorization_errors=True
        )
        signer = VerifiedSigner(
            "kid", "ed25519", "sig1", time.time(), agent_url=ORIGIN, profile="web-bot-auth"
        )
        established, error = await a2a._authorize_wba_signer(signer, "get_media_buys")
        assert error is None
        assert established.principal_id == "principal-1"
        now[0] = 1300
        response = await post("get_media_buys")
        error = response.json()["structuredContent"]["adcp_error"]
        assert error["code"] == "SERVICE_UNAVAILABLE"
        assert error["recovery"] == "transient"
        assert "Accept-Signature" not in response.headers
        _, error = await a2a._authorize_wba_signer(signer, "get_media_buys")
        assert error.code == "SERVICE_UNAVAILABLE"
        assert len(observed) == 2


async def test_binding_outage_grace_is_rechecked_after_slow_fetch():
    async with bound_client() as (post, now, mode, bindings, records, observed, config, dispatch):
        mode[0], now[0] = "offline_late", 1061
        response = await post("get_media_buys")
        assert response.json()["structuredContent"]["adcp_error"]["code"] == "SERVICE_UNAVAILABLE"
        assert now[0] == 1300
        assert not observed


@pytest.mark.parametrize("protocol_method", [False, True])
async def test_binding_outage_outside_grace_returns_service_on_edge_only_paths(protocol_method):
    async with bound_client(
        transport="a2a" if protocol_method else "mcp",
        defer_authorization_errors=protocol_method,
    ) as (post, now, mode, bindings, records, observed, config, dispatch):
        mode[0], now[0] = "offline", 1300
        response = await post(
            "get_media_buys", method="tasks/get" if protocol_method else "tools/call"
        )
        assert response.status_code == 503
        error = response.json()["errors"][0]
        assert error["code"] == "SERVICE_UNAVAILABLE"
        assert error["recovery"] == "transient"
        assert "Accept-Signature" not in response.headers
        assert not observed


@pytest.mark.parametrize("newer_policy", ["delisted", "removed", "expired"])
async def test_binding_final_check_rejects_newer_policy_or_time_after_evaluation(
    monkeypatch, newer_policy
):
    async with bound_client(unbound_for={"get_media_buys"}) as (
        post,
        now,
        mode,
        bindings,
        records,
        observed,
        config,
        dispatch,
    ):
        evaluate = records.evaluate

        async def delayed_evaluate(origin, **kwargs):
            captured = await evaluate(origin, **kwargs)
            if newer_policy == "expired":
                now[0] = captured.expires_at
            else:
                mode[0], now[0] = newer_policy, 1061
                try:
                    await evaluate("https://other.platform.com")
                except TrustRecordNotFoundError:
                    # Retain the removal, then return the older in-flight listing
                    # so the framework's final guard must reject that snapshot.
                    assert newer_policy == "removed"
            return captured

        monkeypatch.setattr(records, "evaluate", delayed_evaluate)
        response = await post("get_media_buys")
        if newer_policy == "expired":
            assert (
                response.json()["structuredContent"]["adcp_error"]["code"] == "SERVICE_UNAVAILABLE"
            )
            assert not observed
        else:
            assert response.json()["principal"] is None
            assert observed == [(None, None)]


async def test_historical_binding_without_prior_listing_cannot_continue_through_outage():
    async with bound_client() as (post, now, mode, bindings, records, observed, config, dispatch):

        async def unavailable(url, *, allow_private=False):
            raise OSError("offline after restart")

        restarted = TrustRecordEvaluator(fetcher=unavailable, clock=lambda: now[0])
        middleware = SignedRequestVerificationMiddleware(
            dispatch, replace(config, trust_records=restarted), transport="mcp"
        )
        signer = VerifiedSigner(
            "kid", "ed25519", "sig1", time.time(), agent_url=ORIGIN, profile="web-bot-auth"
        )
        _, error = await middleware._authorize_wba_signer(signer, "get_media_buys")
        assert error.code == "SERVICE_UNAVAILABLE"
        assert error.recovery == "transient"


async def test_other_origins_delisting_read_survives_cache_eviction_and_local_capacity():
    async with bound_client(unbound_for={"get_media_buys"}) as (
        post,
        now,
        mode,
        bindings,
        records,
        observed,
        config,
        dispatch,
    ):
        assert (await post("get_media_buys")).json()["principal"] == "principal-1"
        mode[0], now[0] = "delisted", 1061
        # A different origin's read, without requesting this origin's role,
        # still supersedes its historical positive membership.
        await records.evaluate("https://other.platform.com")
        records._cache.clear()
        records._max_records = 1
        release = asyncio.Event()
        pending = asyncio.create_task(release.wait())
        records._pending["unrelated.com"] = pending
        try:
            mode[0], now[0] = "offline", 1122
            response = await post("get_media_buys")
            assert response.json()["principal"] is None
            assert await bindings(ORIGIN) == "principal-1"
            # A spend request made through a second middleware has never
            # observed the origin's membership itself; latest owner policy
            # still takes precedence over a local discovery-capacity outage.
            fresh_config = replace(config)
            fresh_config._binding_membership.clear()
            other_port = SignedRequestVerificationMiddleware(
                dispatch, fresh_config, transport="a2a", defer_authorization_errors=True
            )
            signer = VerifiedSigner(
                "kid", "ed25519", "sig1", time.time(), agent_url=ORIGIN, profile="web-bot-auth"
            )
            established, error = await other_port._authorize_wba_signer(signer, "create_media_buy")
            assert established.principal_id is None
            assert error.code == "PERMISSION_DENIED"
        finally:
            records._pending.pop("unrelated.com")
            release.set()
            assert await asyncio.wait_for(pending, timeout=5)


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("create_media_buy", {}),
        ("buy_products", {}),
        ("accept_proposal", {}),
        ("control_media_buy", {"paused": False}),
        ("control_media_buy", {"total_budget": {"amount": 500}}),
        ("update_media_buy", {"new_packages": [{}]}),
        ("future_paid_task", {}),
    ],
)
async def test_operator_outage_stops_new_or_unknown_spend_with_retryable_adcp_error(
    operation, arguments
):
    async with bound_client() as (post, now, mode, bindings, records, observed, config, dispatch):
        mode[0], now[0] = "offline", 1061
        response = await post(operation, arguments)
        error = response.json()["structuredContent"]["adcp_error"]
        assert error["code"] == "SERVICE_UNAVAILABLE"
        assert error["recovery"] == "transient"
        assert not observed
        assert "Accept-Signature" not in response.headers


async def test_seller_can_classify_known_budget_reductions_using_local_state():
    async with bound_client(spend_classifier=lambda operation, arguments: False) as (
        post,
        now,
        mode,
        bindings,
        records,
        observed,
        config,
        dispatch,
    ):
        mode[0], now[0] = "offline", 1061
        assert (await post("control_media_buy", {"total_budget": {"amount": 50}})).json()[
            "principal"
        ] == "principal-1"


async def test_invalid_operator_record_then_outage_cannot_restore_commercial_binding():
    async with bound_client(unbound_for={"get_products"}) as (
        post,
        now,
        mode,
        bindings,
        records,
        observed,
        config,
        dispatch,
    ):
        assert (await post("get_products")).json()["principal"] == "principal-1"
        mode[0], now[0] = "invalid", 1061
        invalid = await post("get_products")
        assert invalid.json()["structuredContent"]["adcp_error"]["code"] == "PERMISSION_DENIED"
        mode[0], now[0] = "offline", 1122
        assert (await post("get_products")).json()["structuredContent"]["adcp_error"][
            "code"
        ] == "PERMISSION_DENIED"
        assert (await post("create_media_buy")).json()["structuredContent"]["adcp_error"][
            "code"
        ] == "PERMISSION_DENIED"
        mode[0], now[0] = "valid", 1183
        assert (await post("get_products")).json()["principal"] == "principal-1"


async def test_delisted_membership_is_shared_across_mcp_and_a2a_middleware():
    async with bound_client(unbound_for={"get_products"}) as (
        post,
        now,
        mode,
        bindings,
        records,
        observed,
        config,
        dispatch,
    ):
        mode[0], now[0] = "delisted", 1061
        assert (await post("get_products")).json()["principal"] is None
        mode[0], now[0] = "offline", 1122
        a2a = SignedRequestVerificationMiddleware(
            dispatch, config, transport="a2a", defer_authorization_errors=True
        )
        signer = VerifiedSigner(
            "kid", "ed25519", "sig1", time.time(), agent_url=ORIGIN, profile="web-bot-auth"
        )
        established, error = await a2a._authorize_wba_signer(signer, "create_media_buy")
        assert established.principal_id is None
        assert error.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("method", ["tasks/get", "tasks/cancel"])
async def test_delisted_a2a_origin_keeps_only_historical_task_owner(method):
    async with bound_client(transport="a2a") as (
        post,
        now,
        mode,
        bindings,
        records,
        observed,
        config,
        dispatch,
    ):
        assert (await post("unused", method=method)).status_code == 200
        assert observed == [("principal-1", ORIGIN)]
        mode[0], now[0] = "delisted", 1061
        response = await post("unused", method=method)
        assert response.status_code == 200
        assert observed == [("principal-1", ORIGIN), (None, ORIGIN)]
        assert "Accept-Signature" not in response.headers


@pytest.mark.parametrize("preverified", [False, True])
@pytest.mark.parametrize("failure", ["delisted", "offline"])
async def test_bound_identity_loses_authority_on_refresh_without_legacy_downgrade(
    preverified, failure
):
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        **public_directory_key(private),
        "alg": "EdDSA",
        "adcp_use": "request-signing",
        "key_ops": ["verify"],
    }
    now, mode, fetches, dispatched = [1000], ["valid"], [], []

    async def fetch(url, *, allow_private=False):
        fetches.append(url)
        if mode[0] == "offline":
            raise OSError("offline")
        agents = [{"url": ORIGIN, "roles": ["adcp:buying"]}] if mode[0] == "valid" else []
        return TrustResponse(json.dumps({"agents": agents}).encode())

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    bindings = OriginBindings()
    await records.bind(
        VerifiedSigner(
            jwk["kid"], "ed25519", "sig1", time.time(), agent_url=ORIGIN, profile="web-bot-auth"
        ),
        principal_id="principal-1",
        bindings=bindings,
    )
    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({ORIGIN: {"keys": [jwk]}}),
        enable_web_bot_auth=True,
        origin_bindings=bindings,
        trust_records=records,
        wba_revocation_checker=not_revoked,
        signing_profile_version="3.2",
    )

    async def dispatch(scope, receive, send):
        context = apply_verified_signer(None, Request(scope))
        try:
            check_wba_authorization(context)
        except AdcpError as exc:
            await JSONResponse(build_mcp_error_result(exc).model_dump(mode="json", by_alias=True))(
                scope, receive, send
            )
            return
        dispatched.append(True)
        await JSONResponse({"ok": True})(scope, receive, send)

    middleware = SignedRequestVerificationMiddleware(
        dispatch, config, transport="mcp", defer_authorization_errors=True
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "create_media_buy", "arguments": {}},
    }
    body = json.dumps(payload).encode()

    async def app(scope, receive, send):
        if preverified:
            scope[VERIFIED_SIGNER_SCOPE_KEY] = VerifiedSigner(
                jwk["kid"],
                "ed25519",
                "sig1",
                time.time(),
                agent_url=ORIGIN,
                profile="web-bot-auth",
                parsed_body=payload,
            )
        await middleware(scope, receive, send)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:

        async def post():
            headers = {"Content-Type": "application/json"}
            headers.update(
                sign_wba_request(
                    method="POST",
                    url=TARGET,
                    headers=headers,
                    body=body,
                    private_key=private,
                    agent_origin=ORIGIN,
                )
            )
            return await client.post(TARGET, content=body, headers=headers)

        assert (await post()).status_code == 200
        mode[0] = failure
        now[0] += 61
        response = await post()
        assert response.status_code == 200
        error = response.json()["structuredContent"]["adcp_error"]
        assert error["code"] == (
            "PERMISSION_DENIED" if failure == "delisted" else "SERVICE_UNAVAILABLE"
        )
        assert error["recovery"] == ("correctable" if failure == "delisted" else "transient")
        assert "Accept-Signature" not in response.headers
        assert len(dispatched) == 1
        assert len(fetches) == 2
        assert await bindings(ORIGIN) == "principal-1"

        # Historical approval must still prevent transport downgrade.
        headers = {"Content-Type": "application/json"}
        signed = sign_request(
            method="POST",
            url=TARGET,
            headers=headers,
            body=body,
            private_key=private,
            key_id=jwk["kid"],
            alg="ed25519",
            signing_profile_version="3.2",
        )
        legacy = SignedRequestVerificationMiddleware(
            dispatch, config, transport="mcp", defer_authorization_errors=True
        )

        async def legacy_app(scope, receive, send):
            if preverified:
                scope[VERIFIED_SIGNER_SCOPE_KEY] = VerifiedSigner(
                    jwk["kid"],
                    "ed25519",
                    "sig1",
                    time.time(),
                    agent_url=ORIGIN,
                    parsed_body=payload,
                )
            await legacy(scope, receive, send)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(legacy_app)) as legacy_client:
            response = await legacy_client.post(
                TARGET, content=body, headers={**headers, **signed.as_dict()}
            )
            assert response.status_code == 200
            assert response.json()["structuredContent"]["adcp_error"]["code"] == "PERMISSION_DENIED"
            assert "Accept-Signature" not in response.headers
        assert len(dispatched) == 1


@pytest.mark.parametrize("operation", ["get_products", "list_creative_formats", "sync_principal"])
async def test_unbound_policy_can_expose_anonymous_reads_and_explicit_open_onboarding(operation):
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    dispatched = []

    async def dispatch(scope, receive, send):
        context = apply_verified_signer(None, Request(scope))
        try:
            check_wba_authorization(context)
        except AdcpError as exc:
            await JSONResponse(build_mcp_error_result(exc).model_dump(mode="json", by_alias=True))(
                scope, receive, send
            )
            return
        dispatched.append(scope["adcp.server.signature_verified"])
        await JSONResponse({"ok": True})(scope, receive, send)

    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({ORIGIN: {"keys": [jwk]}}),
        enable_web_bot_auth=True,
        wba_revocation_checker=not_revoked,
        unbound_for=frozenset({operation}),
    )
    app = SignedRequestVerificationMiddleware(
        dispatch, config, transport="mcp", defer_authorization_errors=True
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
        for op in (operation, "create_media_buy", operation):
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": op, "arguments": {}},
                }
            ).encode()
            headers = {"Content-Type": "application/json"}
            headers.update(
                sign_wba_request(
                    method="POST",
                    url=TARGET,
                    headers=headers,
                    body=body,
                    private_key=private,
                    agent_origin=ORIGIN,
                )
            )
            response = await client.post(TARGET, content=body, headers=headers)
            assert response.status_code == 200
            assert "Accept-Signature" not in response.headers
            if op == "create_media_buy":
                assert (
                    response.json()["structuredContent"]["adcp_error"]["code"]
                    == "PERMISSION_DENIED"
                )
    assert dispatched == [True, True]


@pytest.mark.parametrize("method", ["initialize", "tools/list"])
async def test_unbound_policy_can_include_public_protocol_methods(method):
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)

    async def dispatch(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({ORIGIN: {"keys": [jwk]}}),
        enable_web_bot_auth=True,
        wba_revocation_checker=not_revoked,
        unbound_for=frozenset({method}),
    )
    app = SignedRequestVerificationMiddleware(
        dispatch, config, transport="mcp", defer_authorization_errors=True
    )
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(
        sign_wba_request(
            method="POST",
            url=TARGET,
            headers=headers,
            body=body,
            private_key=private,
            agent_origin=ORIGIN,
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
        assert (await client.post(TARGET, content=body, headers=headers)).status_code == 200
