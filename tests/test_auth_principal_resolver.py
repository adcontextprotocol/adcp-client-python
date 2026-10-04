"""Metadata-only principal resolution at both asynchronous auth boundaries."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from adcp.server import (
    ADCPHandler,
    AuthRequest,
    BearerTokenAuth,
    Principal,
    PrincipalResolver,
    PrincipalResolverError,
)
from adcp.server.auth import (
    REQUEST_STATE_PRINCIPAL,
    REQUEST_STATE_PRINCIPAL_METADATA,
    REQUEST_STATE_TENANT,
    auth_context_factory,
    current_principal,
    current_principal_metadata,
    current_tenant,
)
from adcp.server.serve import (
    RequestMetadata,
    _build_mcp_and_a2a_app,
    _wrap_a2a_with_auth,
    _wrap_mcp_with_auth,
)

CALL = {"method": "tools/call", "params": {"name": "get_products"}, "opaque": [1, 2, 3]}
PROXY = Principal("buyer-42", "embedded-tenant", {"role": "buyer"})
BEARER = Principal("bearer-buyer", "bearer-tenant", {"role": "admin"})


def resolver_app(
    leg: str,
    config: BearerTokenAuth,
    *,
    fail: bool = False,
    store: Any = None,
    executions: list[tuple[str, str]] | None = None,
) -> Any:
    async def result(params: Any, context: Any) -> dict[str, Any]:
        if executions is not None:
            executions.append((context.tenant_id, context.caller_identity))
        return {"principal": context.caller_identity, "tenant": context.tenant_id}

    operation = store.wrap(result) if store else result

    async def endpoint(request: Request) -> JSONResponse:
        await asyncio.sleep(0)
        context = auth_context_factory(
            RequestMetadata(
                tool_name="get_products",
                transport=leg,
                request_id="test",
                request_context=request,
            )
        )
        if fail:
            raise RuntimeError("downstream failed")
        body = await request.json()
        if store:
            return JSONResponse(await operation(params=body, context=context))
        assert current_principal.get() == context.caller_identity
        assert current_tenant.get() == context.tenant_id
        assert getattr(request.state, REQUEST_STATE_PRINCIPAL) == context.caller_identity
        assert getattr(request.state, REQUEST_STATE_TENANT) == context.tenant_id
        assert (
            getattr(request.state, REQUEST_STATE_PRINCIPAL_METADATA)
            == current_principal_metadata.get()
        )
        if leg == "a2a" and context.caller_identity is not None:
            assert request.scope["user"].is_authenticated
            assert request.scope["user"].display_name == context.caller_identity
            assert request.scope["user"].tenant_id == context.tenant_id
            assert request.scope["user"].principal_metadata == current_principal_metadata.get()
            assert request.scope["auth"].caller_identity == context.caller_identity
        auth_info = context.metadata.get("adcp.auth_info")
        return JSONResponse(
            {
                "principal": context.caller_identity,
                "tenant": context.tenant_id,
                "role": context.metadata.get("role"),
                "kind": auth_info.kind if auth_info else None,
                "body": body,
            }
        )

    inner = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
    return (_wrap_mcp_with_auth if leg == "mcp" else _wrap_a2a_with_auth)(inner, config)


class MetadataMiddleware:
    """Populate trusted server metadata, without minting bearer credentials."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            scope.setdefault("state", {})["verified_proxy_principal"] = PROXY
            scope.setdefault("extensions", {})["tls"] = {"client_cert_name": "CN=buyer"}
        await self.app(scope, receive, send)


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_metadata_view_and_every_identity_channel(leg: str, asynchronous: bool) -> None:
    views: list[AuthRequest] = []

    def resolve(request: AuthRequest) -> Principal | None:
        views.append(request)
        assert request.method == "POST"
        assert request.transport == leg
        assert request.url.hostname == "test"
        assert request.query_params["mode"] == "embedded"
        assert request.headers.getlist("x-duplicate") == ["one", "two"]
        assert request.client is not None
        assert request.tls == {"client_cert_name": "CN=buyer"}
        assert not hasattr(request, "body") and not hasattr(request, "scope")
        assert not hasattr(request, "receive") and not hasattr(request, "json")
        with pytest.raises(TypeError):
            request.state["evil"] = True
        return request.state["verified_proxy_principal"]

    async def async_resolve(request: AuthRequest) -> Principal | None:
        await asyncio.sleep(0)
        return resolve(request)

    callback: PrincipalResolver = async_resolve if asynchronous else resolve
    app = MetadataMiddleware(
        resolver_app(
            leg, BearerTokenAuth(validate_token=lambda token: None, resolve_principal=callback)
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/?mode=embedded",
            json=CALL,
            headers=[
                ("x-duplicate", "one"),
                ("x-duplicate", "two"),
                ("x-private", "private-token"),
            ],
        )
    assert response.status_code == 200
    assert response.json() == {
        "principal": PROXY.caller_identity,
        "tenant": PROXY.tenant_id,
        "role": "buyer",
        "kind": "derived",
        "body": CALL,
    }
    assert len(views) == 1
    assert "private-token" not in repr(views[0])
    assert current_principal.get() is None
    assert current_tenant.get() is None
    assert current_principal_metadata.get() is None
    assert PROXY.metadata == {"role": "buyer"}  # no mutation of caller-owned principal


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
@pytest.mark.parametrize(
    "headers,status",
    [
        ({"authorization": "Bearer valid"}, 200),
        ({"authorization": "Bearer invalid"}, 401),
        ({"authorization": "Basic valid"}, 401),
        ({"authorization": ""}, 401),
        ({"x-legacy": ""}, 401),
        ({"x-legacy": "invalid"}, 401),
        ({"authorization": "Bearer valid", "x-legacy": "different"}, 401),
    ],
)
async def test_supplied_credentials_never_use_resolver(
    leg: str, headers: dict[str, str], status: int
) -> None:
    called = False

    def resolve(request: AuthRequest) -> Principal | None:
        nonlocal called
        called = True
        return PROXY

    async def validate(token: str) -> Principal | None:
        await asyncio.sleep(0)
        return BEARER if token == "valid" else None

    config = BearerTokenAuth(
        validate_token=validate, resolve_principal=resolve, legacy_header_aliases=["x-legacy"]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=resolver_app(leg, config)), base_url="http://test"
    ) as client:
        response = await client.post("/", json=CALL, headers=headers)
    assert response.status_code == status
    assert not called
    if status == 200:
        assert response.json()["principal"] == BEARER.caller_identity
        assert response.json()["kind"] == "bearer"
    assert current_principal.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
@pytest.mark.parametrize("denial", [401, 403, "unexpected", "bad-result"])
@pytest.mark.parametrize("allow", [False, True])
async def test_safe_rejection(
    leg: str, denial: Any, allow: bool, caplog: pytest.LogCaptureFixture
) -> None:
    def resolve(request: AuthRequest) -> Any:
        if denial in (401, 403):
            error = PrincipalResolverError(denial)
            # Exercise private exception text on Python 3.10 as well as newer runtimes.
            error.args = ("private-token",)
            raise error
        if denial == "bad-result":
            return "private-token"
        raise RuntimeError("private-token in resolver's internal error")

    config = BearerTokenAuth(
        validate_token=lambda token: None, resolve_principal=resolve, allow_unauthenticated=allow
    )
    with caplog.at_level(logging.DEBUG, logger="adcp.server.auth"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=resolver_app(leg, config)), base_url="http://test"
        ) as client:
            response = await client.post("/", json=CALL)
    assert response.status_code == (403 if denial == 403 else 401)
    assert ("www-authenticate" in response.headers) == (denial != 403)
    if denial != 403:
        assert response.headers["www-authenticate"] == 'Bearer realm="adcp", error="invalid_token"'
    assert response.json() == {"error": "forbidden" if denial == 403 else "unauthenticated"}
    assert "private-token" not in caplog.text + response.text
    assert all(
        record.exc_info is None for record in caplog.records if record.name == "adcp.server.auth"
    )
    errors = [
        record
        for record in caplog.records
        if record.name == "adcp.server.auth" and record.levelno >= logging.ERROR
    ]
    if denial in ("unexpected", "bad-result"):
        assert len(errors) == 1
        assert errors[0].levelno == logging.ERROR
        assert errors[0].reason == "resolver_error"
        assert errors[0].args == ()
        assert errors[0].exc_info is None
        assert errors[0].exc_text is None
    else:
        assert not errors


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
@pytest.mark.parametrize("allow", [False, True])
async def test_resolver_none_applies_existing_missing_bearer_policy(leg: str, allow: bool) -> None:
    config = BearerTokenAuth(
        validate_token=lambda token: None,
        resolve_principal=lambda request: None,
        allow_unauthenticated=allow,
    )
    # A2A's historical anonymous pass-through has no request-state installation.
    from tests.test_auth_credential_parity import middleware_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware_app(leg, config)), base_url="http://test"
    ) as client:
        response = await client.post("/", json=CALL)
    assert response.status_code == (200 if allow else 401)
    if allow:
        assert response.json()["principal"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_concurrency_reset_and_downstream_error(leg: str) -> None:
    async def resolve(request: AuthRequest) -> Principal | None:
        identity = request.headers.get("x-identity")
        await asyncio.sleep(0)
        return Principal(identity, "proxy") if identity else None

    config = BearerTokenAuth(validate_token=lambda token: None, resolve_principal=resolve)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=resolver_app(leg, config)), base_url="http://test"
    ) as client:

        async def call(identity: str | None) -> None:
            response = await client.post(
                "/", json=CALL, headers={"x-identity": identity} if identity else {}
            )
            assert response.status_code == (200 if identity else 401)
            if identity:
                assert response.json()["principal"] == identity

        await asyncio.gather(*(call(identity) for identity in ["a", None, "b", None] * 8))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=resolver_app(leg, config, fail=True)),
        base_url="http://test",
    ) as client:
        with pytest.raises(RuntimeError, match="downstream failed"):
            await client.post("/", json=CALL, headers={"x-identity": "a"})
    assert current_principal.get() is None
    assert current_tenant.get() is None
    assert current_principal_metadata.get() is None


@pytest.mark.asyncio
async def test_idempotency_scope_matches_between_transports() -> None:
    from adcp.server.idempotency import IdempotencyStore, MemoryBackend

    executions: list[tuple[str, str]] = []
    store = IdempotencyStore(MemoryBackend())

    def resolve(request: AuthRequest) -> Principal | None:
        return Principal("same-buyer", request.headers.get("x-tenant", "proxy-tenant"))

    config = BearerTokenAuth(
        validate_token=lambda token: Principal("same-buyer", "bearer-tenant"),
        resolve_principal=resolve,
    )
    for leg in ["mcp", "a2a"]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=resolver_app(leg, config, store=store, executions=executions)
            ),
            base_url="http://test",
        ) as client:
            for headers in [{}, {"x-tenant": "second-proxy"}, {"authorization": "Bearer valid"}]:
                response = await client.post(
                    "/", json={"idempotency_key": "shared-idempotency-key"}, headers=headers
                )
                assert response.status_code == 200
                assert response.json().get("replayed", False) is (leg == "a2a")
    assert executions == [
        ("proxy-tenant", "same-buyer"),
        ("second-proxy", "same-buyer"),
        ("bearer-tenant", "same-buyer"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_combined_server_proxy_and_bearer_tenants(stateless: bool) -> None:
    seen: list[tuple[str | None, str | None, str]] = []

    class Seller(ADCPHandler):
        advertised_tools = {"get_products"}

        async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
            seen.append(
                (
                    context.caller_identity,
                    context.tenant_id,
                    context.metadata["adcp.auth_info"].kind,
                )
            )
            return {"products": []}

    async def resolve(request: AuthRequest) -> Principal | None:
        await asyncio.sleep(0)
        principal = request.state.get("verified_proxy_principal")
        return principal if isinstance(principal, Principal) else None

    config = BearerTokenAuth(
        validate_token=lambda token: BEARER if token == "valid" else None, resolve_principal=resolve
    )
    inner = _build_mcp_and_a2a_app(
        Seller(),
        name="seller",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        stateless_http=stateless,
        enable_dns_rebinding_protection=False,
        context_factory=auth_context_factory,
        auth=config,
    )

    class SelectiveProxy(MetadataMiddleware):
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if (b"x-trusted-test", b"yes") in scope.get("headers", ()):
                await super().__call__(scope, receive, send)
            else:
                await self.app(scope, receive, send)

    app = SelectiveProxy(inner)
    params = {"buying_mode": "brief", "brief": "video"}
    mcp = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_products", "arguments": params},
    }
    a2a = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "ROLE_USER",
                "parts": [{"data": {"skill": "get_products", "parameters": params}}],
            }
        },
    }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=True
        ) as client:
            session_headers: dict[str, str] = {}
            for path, body in [("/mcp", mcp), ("/", a2a)]:
                for headers, expected in [
                    ({"x-trusted-test": "yes"}, ("buyer-42", "embedded-tenant", "derived")),
                    (
                        {"authorization": "Bearer valid", "x-trusted-test": "yes"},
                        ("bearer-buyer", "bearer-tenant", "bearer"),
                    ),
                    ({"x-principal-id": "forged"}, None),
                    ({}, None),
                    ({"authorization": "Bearer invalid", "x-trusted-test": "yes"}, None),
                ]:
                    if not stateless and path == "/mcp" and expected is not None:
                        initialized = await client.post(
                            "/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "initialize",
                                "params": {
                                    "protocolVersion": "2025-03-26",
                                    "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"},
                                },
                            },
                            headers={"accept": "application/json, text/event-stream"},
                        )
                        assert initialized.status_code == 200
                        session_headers["mcp-session-id"] = initialized.headers["mcp-session-id"]
                    seen.clear()
                    response = await client.post(
                        path,
                        json=body,
                        headers={
                            "accept": "application/json, text/event-stream",
                            "A2A-Version": "1.0",
                            **(session_headers if path == "/mcp" else {}),
                            **headers,
                        },
                    )
                    assert response.status_code == (200 if expected else 401), response.text
                    assert seen == ([expected] if expected else [])
    assert current_principal.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_streamed_body_survives_metadata_resolution(leg: str) -> None:
    async def chunks() -> Any:
        raw = json.dumps(CALL).encode()
        for index in range(0, len(raw), 7):
            yield raw[index : index + 7]
            await asyncio.sleep(0)

    config = BearerTokenAuth(
        validate_token=lambda token: None, resolve_principal=lambda request: PROXY
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=resolver_app(leg, config)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/", content=chunks(), headers={"content-type": "application/json"}
        )
    assert response.status_code == 200
    assert response.json()["body"] == CALL


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_verified_signer_and_required_bearer_fallback_keep_precedence(leg: str) -> None:
    from tests.test_signed_request_verification import (
        AGENT_URL,
        MCP_HEADERS,
        _a2a_send,
        _client,
        _config,
        _Recording,
        _signed,
        _tools_call,
    )

    invoked: list[AuthRequest] = []

    def resolve(request: AuthRequest) -> Principal | None:
        invoked.append(request)
        return PROXY

    auth = BearerTokenAuth(
        validate_token=lambda token: BEARER if token == "valid" else None, resolve_principal=resolve
    )
    handler = _Recording()
    app = _build_mcp_and_a2a_app(
        handler,
        name="signed-seller",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        stateless_http=True,
        allowed_hosts=["localhost"],
        context_factory=auth_context_factory,
        auth=auth,
        advertise_all=True,
        request_signature_verification=_config(allow_bearer_fallback=True),
    )
    path = "/mcp" if leg == "mcp" else "/"
    body = _tools_call("get_products") if leg == "mcp" else _a2a_send("get_products")
    signed_body, signed_headers = _signed("http://localhost" + path, body, headers=MCP_HEADERS)
    async with _client(app) as client:
        signed = await client.post(path, content=signed_body, headers=signed_headers)
        assert signed.status_code == 200, signed.text
        assert handler.contexts[-1].caller_identity == AGENT_URL
        assert handler.contexts[-1].metadata["adcp.auth_info"].kind == "http_sig"
        unsigned = await client.post(path, json=body, headers=MCP_HEADERS)
        assert unsigned.status_code == 401
        assert unsigned.headers["www-authenticate"].startswith(
            'Signature error="request_signature_required"'
        )
        bearer = await client.post(
            path, json=body, headers={**MCP_HEADERS, "authorization": "Bearer valid"}
        )
        assert bearer.status_code == 200, bearer.text
        assert handler.contexts[-1].caller_identity == BEARER.caller_identity
    assert invoked == []


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
@pytest.mark.parametrize("denial", [401, 403, "unexpected"])
@pytest.mark.parametrize("credential", ["absent", "invalid-bearer", "required", "bad-signature"])
async def test_resolver_denial_and_signature_challenges(
    leg: str, denial: int | str, credential: str, caplog: pytest.LogCaptureFixture
) -> None:
    from adcp.signing import load_private_key_pem
    from tests.test_signed_request_verification import (
        _OTHER_PEM,
        MCP_HEADERS,
        _a2a_send,
        _both_app,
        _client,
        _config,
        _Recording,
        _signed,
        _tools_call,
    )

    invoked: list[AuthRequest] = []

    def resolve(request: AuthRequest) -> Principal | None:
        invoked.append(request)
        if denial == "unexpected":
            raise RuntimeError("resolver-private-sentinel")
        error = PrincipalResolverError(403 if denial == 403 else 401)
        error.args = ("resolver-private-sentinel",)
        raise error

    handler = _Recording()
    app = _both_app(
        handler,
        _config(allow_bearer_fallback=True),
        auth=BearerTokenAuth(validate_token=lambda token: None, resolve_principal=resolve),
    )
    operation = "get_products" if credential == "required" else "get_media_buys"
    path = "/mcp" if leg == "mcp" else "/"
    body = _tools_call(operation) if leg == "mcp" else _a2a_send(operation)
    headers = dict(MCP_HEADERS)
    if credential == "invalid-bearer":
        headers["authorization"] = "Bearer supplied-invalid"
    if credential == "bad-signature":
        raw, headers = _signed(
            f"http://localhost{path}",
            body,
            headers=headers,
            private_key=load_private_key_pem(_OTHER_PEM),
            key_id="stranger-key",
        )
    else:
        raw = json.dumps(body).encode()
    with caplog.at_level(logging.DEBUG, logger="adcp.server.auth"):
        async with _client(app) as client:
            response = await client.post(path, content=raw, headers=headers)

    forbidden = credential == "absent" and denial == 403
    assert response.status_code == (403 if forbidden else 401), response.text
    if credential == "absent":
        assert len(invoked) == 1
        assert response.json() == {"error": "forbidden" if forbidden else "unauthenticated"}
    else:
        assert invoked == []
    challenge = response.headers.get("www-authenticate")
    if forbidden:
        assert challenge is None
    elif credential == "required":
        assert challenge == 'Signature error="request_signature_required"'
    elif credential == "bad-signature":
        assert challenge == 'Signature error="request_signature_key_unknown"'
    else:
        assert challenge == 'Bearer realm="adcp", error="invalid_token"'
    assert "resolver-private-sentinel" not in response.text + caplog.text
    errors = [
        record
        for record in caplog.records
        if record.name == "adcp.server.auth" and record.levelno >= logging.ERROR
    ]
    assert len(errors) == int(credential == "absent" and denial == "unexpected")
    for record in errors:
        assert record.reason == "resolver_error"
        assert record.args == ()
        assert record.exc_info is None
        assert record.exc_text is None
    assert handler.contexts == []
    assert current_principal.get() is None
    assert current_tenant.get() is None
    assert current_principal_metadata.get() is None
