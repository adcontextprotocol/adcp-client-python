"""Regression guard for the bearer-flow → BuyerAgentRegistry resolve
chain under stateful streamable-http (issue #703).

The v3 reference seller wires bearer auth + ``SubdomainTenantMiddleware``
+ a tenant-scoped :class:`BuyerAgentRegistry` reading
``current_tenant()`` + a ``context_factory`` that upgrades the bearer
flow's ``adcp.auth_info`` to a typed :class:`ApiKeyCredential` so
:meth:`BuyerAgentRegistry.resolve_by_credential` matches the seeded
``api_key_id`` row. Without ALL FOUR of those pieces correctly wired,
every authenticated tool call is rejected at the framework's
commercial-identity gate with ``PERMISSION_DENIED``.

Issue #703 surfaced when an intermediate state of PR #693 had the
storyboard CI assertion enabled but the seller's bearer-auth wiring
not yet wired in ``examples/v3_reference_seller/src/app.py``. The
fix landed in the same PR. These tests pin the wired contract so a
future refactor (PR #636 stateful-default change, PR #720 additive
legacy headers, etc.) that breaks any leg of the chain trips here
instead of in the storyboard CI cascade.

These tests cover the harder-to-reproduce slice — the dispatch
sub-task that runs the tool handler under stateful streamable-http
is a distinct async task from the request task. ContextVars set in
the outer ASGI middleware are visible to the dispatch sub-task only
when the spawning chain (request task → session task → dispatch
sub-task) carries the context forward, which is the property the
upstream MCP transport relies on. A regression in any of those
spawn paths reads ``current_tenant() == None`` inside the registry
and silently fails the resolve.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.mark.asyncio
async def test_subdomain_tenant_middleware_propagates_into_dispatch_task() -> None:
    """``current_tenant()`` must be observable inside the dispatch
    task that runs the MCP tool handler under stateful streamable-http.

    Adopter stores (the v3-ref-seller's
    :class:`TenantScopedBuyerAgentRegistry`, the salesagent
    ``AccountStore``) read this accessor to scope every query. When
    it returns ``None`` they silently return no rows and the
    framework's commercial-identity gate rejects with
    ``PERMISSION_DENIED`` — the failure mode of issue #703.
    """
    from adcp.server import (
        ADCPHandler,
        InMemorySubdomainTenantRouter,
        SubdomainTenantMiddleware,
        Tenant,
        ToolContext,
        create_mcp_server,
        current_tenant,
    )

    received: dict[str, Any] = {}

    class _Recording(ADCPHandler[Any]):
        async def get_products(
            self, params: Any, context: ToolContext | None = None
        ) -> dict[str, Any]:
            tenant = current_tenant()
            received["tenant"] = tenant
            return {"products": []}

    router = InMemorySubdomainTenantRouter(
        tenants={"acme.localhost": Tenant(id="t-acme", display_name="Acme")}
    )

    mcp = create_mcp_server(
        _Recording(),
        name="t",
        advertise_all=True,
        allowed_hosts=["acme.localhost", "localhost"],
        validation=None,
    )
    app = mcp.streamable_http_app()
    app.add_middleware(SubdomainTenantMiddleware, router=router)

    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "host": "acme.localhost",
    }

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://acme.localhost",
            follow_redirects=True,
        ) as client:
            init = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers=headers,
            )
            assert init.status_code == 200, init.text
            session_id = init.headers["mcp-session-id"]

            call = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_products", "arguments": {}},
                },
                headers={**headers, "mcp-session-id": session_id},
            )
            assert call.status_code == 200, call.text

    tenant = received.get("tenant")
    assert tenant is not None, (
        "SubdomainTenantMiddleware set a tenant on the request task, but the "
        "dispatch task that ran the handler saw current_tenant() == None. "
        "Tenant-scoped stores (BuyerAgentRegistry, AccountStore) will silently "
        "return no rows and the framework will reject with PERMISSION_DENIED. "
        "See issue #703."
    )
    assert tenant.id == "t-acme"


@pytest.mark.asyncio
async def test_buyer_agent_registry_resolves_under_stateful_bearer_flow() -> None:
    """End-to-end pin: bearer credential → middleware → tenant-scoped
    registry resolve → ``_resolve_buyer_agent`` returns the seeded agent.

    Wires the four pieces the v3 reference seller wires together (issue
    #703 fix): bearer ``BearerTokenAuthMiddleware`` + ``SubdomainTenantMiddleware``
    + ``context_factory`` that upgrades ``adcp.auth_info`` from the
    framework's no-credential bearer shape to a typed
    :class:`ApiKeyCredential` + a tenant-scoped registry reading
    ``current_tenant()``. If any one of those legs regresses, the
    framework's commercial-identity gate at
    :func:`adcp.decisioning.handler._resolve_buyer_agent` rejects with
    ``PERMISSION_DENIED`` and the assertion below fires.
    """
    from dataclasses import replace

    from adcp.decisioning.context import AuthInfo
    from adcp.decisioning.handler import _resolve_buyer_agent
    from adcp.decisioning.registry import (
        ApiKeyCredential,
        BuyerAgent,
    )
    from adcp.server import (
        ADCPHandler,
        BearerTokenAuthMiddleware,
        InMemorySubdomainTenantRouter,
        Principal,
        SubdomainTenantMiddleware,
        Tenant,
        ToolContext,
        auth_context_factory,
        create_mcp_server,
        current_tenant,
        validator_from_token_map,
    )

    # Tenant-scoped in-memory registry — mirrors the v3-ref-seller's
    # TenantScopedBuyerAgentRegistry shape: scopes the lookup on
    # current_tenant() and matches credentials against an api_key_id
    # column. Returning None for an unknown (tenant, credential) pair
    # is what the framework projects to PERMISSION_DENIED.
    seeded = BuyerAgent(
        agent_url="https://buyer.example/",
        display_name="Buyer",
        status="active",
    )

    class _Registry:
        async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
            tenant = current_tenant()
            if tenant is None or tenant.id != "t-acme":
                return None
            return seeded if agent_url == seeded.agent_url else None

        async def resolve_by_credential(self, credential: Any) -> BuyerAgent | None:
            tenant = current_tenant()
            if tenant is None or tenant.id != "t-acme":
                return None
            if isinstance(credential, ApiKeyCredential) and credential.key_id == "tk-acme":
                return seeded
            return None

    received: dict[str, Any] = {}

    class _Recording(ADCPHandler[Any]):
        async def get_products(
            self, params: Any, context: ToolContext | None = None
        ) -> dict[str, Any]:
            # Reconstruct what PlatformHandler._prime_auth_context does
            # inside the framework dispatch — call _resolve_buyer_agent
            # with the ctx's auth_info. This is the exact callsite that
            # fires PERMISSION_DENIED in the regression.
            assert context is not None
            auth_info = context.metadata.get("adcp.auth_info")
            try:
                agent = await _resolve_buyer_agent(_Registry(), auth_info)
                received["agent"] = agent
                received["error"] = None
            except Exception as exc:  # noqa: BLE001 — surface for assertion
                received["agent"] = None
                received["error"] = exc
            return {"products": []}

    # context_factory mirrors examples/v3_reference_seller/src/app.py's
    # _build_context_factory.build: pins tenant from the
    # SubdomainTenantMiddleware-set ContextVar and upgrades the bearer
    # flow's ``adcp.auth_info`` to carry a typed ``ApiKeyCredential``
    # so the registry's resolve_by_credential() can match it.
    def build_context(meta: Any) -> ToolContext:
        ctx = auth_context_factory(meta)
        tenant = current_tenant()
        if tenant is not None:
            ctx = replace(ctx, tenant_id=tenant.id)
        api_key_id = ctx.metadata.get("api_key_id")
        existing = ctx.metadata.get("adcp.auth_info")
        if api_key_id and isinstance(existing, AuthInfo):
            ctx.metadata["adcp.auth_info"] = AuthInfo(
                kind="api_key",
                key_id=api_key_id,
                principal=existing.principal,
                credential=ApiKeyCredential(kind="api_key", key_id=api_key_id),
            )
        return ctx

    router = InMemorySubdomainTenantRouter(
        tenants={"acme.localhost": Tenant(id="t-acme", display_name="Acme")}
    )

    mcp = create_mcp_server(
        _Recording(),
        name="t",
        advertise_all=True,
        context_factory=build_context,
        allowed_hosts=["acme.localhost", "localhost"],
        validation=None,
    )
    app = mcp.streamable_http_app()
    # Order: BearerTokenAuthMiddleware added first (inner-of-the-two),
    # SubdomainTenantMiddleware added second (outermost). Starlette's
    # add_middleware inserts at index 0 of user_middleware and wraps in
    # reverse, so the outermost-to-innermost runtime order is
    # SubdomainTenant → BearerAuth → app — matching v3-ref-seller's
    # ``asgi_middleware=[(SubdomainTenantMiddleware, ...)]`` + framework
    # bearer-auth wiring composition.
    app.add_middleware(
        BearerTokenAuthMiddleware,
        validate_token=validator_from_token_map(
            {
                "tk-acme": Principal(
                    caller_identity="p-acme",
                    tenant_id="t-acme",
                    metadata={"api_key_id": "tk-acme"},
                )
            }
        ),
    )
    app.add_middleware(SubdomainTenantMiddleware, router=router)

    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "authorization": "Bearer tk-acme",
        "host": "acme.localhost",
    }

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://acme.localhost",
            follow_redirects=True,
        ) as client:
            init = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers=headers,
            )
            assert init.status_code == 200, init.text
            session_id = init.headers["mcp-session-id"]

            call = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_products", "arguments": {}},
                },
                headers={**headers, "mcp-session-id": session_id},
            )
            assert call.status_code == 200, call.text

    error = received.get("error")
    agent = received.get("agent")
    assert error is None, (
        f"_resolve_buyer_agent raised {type(error).__name__}: {error}. "
        "Issue #703: the bearer credential reached the handler but the "
        "tenant-scoped registry returned None. Check (a) the seller's "
        "context_factory still upgrades adcp.auth_info to a typed "
        "ApiKeyCredential, (b) BearerTokenAuthMiddleware still mirrors "
        "principal/tenant onto request.state, (c) SubdomainTenantMiddleware's "
        "ContextVar still propagates into the dispatch task."
    )
    assert agent is not None
    assert agent.agent_url == seeded.agent_url


@pytest.mark.asyncio
async def test_buyer_agent_registry_rejects_when_credential_upgrade_skipped() -> None:
    """Negative-case contract pin: when the ``context_factory`` does
    NOT upgrade ``adcp.auth_info`` to a typed credential, the framework
    rejects with ``PERMISSION_DENIED`` (no leaked details) — the
    documented behavior for adopters who wire a registry but forget
    the credential upgrade.

    This is the exact regression #703 surfaced: pre-fix, the v3
    reference seller's ``_build_context_factory`` left
    ``adcp.auth_info.credential = None``, so
    :func:`_resolve_buyer_agent` saw a no-credential AuthInfo and
    fell into the registry-miss branch. The fix in PR #693 added the
    ApiKeyCredential upgrade in
    ``examples/v3_reference_seller/src/app.py:_build_context_factory.build``.
    """
    from adcp.decisioning.handler import _resolve_buyer_agent
    from adcp.decisioning.registry import ApiKeyCredential, BuyerAgent
    from adcp.decisioning.types import AdcpError
    from adcp.server import (
        ADCPHandler,
        BearerTokenAuthMiddleware,
        InMemorySubdomainTenantRouter,
        Principal,
        SubdomainTenantMiddleware,
        Tenant,
        ToolContext,
        auth_context_factory,
        create_mcp_server,
        current_tenant,
        validator_from_token_map,
    )

    seeded = BuyerAgent(
        agent_url="https://buyer.example/",
        display_name="Buyer",
        status="active",
    )

    class _Registry:
        async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
            return None

        async def resolve_by_credential(self, credential: Any) -> BuyerAgent | None:
            tenant = current_tenant()
            if tenant is None or tenant.id != "t-acme":
                return None
            if isinstance(credential, ApiKeyCredential) and credential.key_id == "tk-acme":
                return seeded
            return None

    received: dict[str, Any] = {}

    class _Recording(ADCPHandler[Any]):
        async def get_products(
            self, params: Any, context: ToolContext | None = None
        ) -> dict[str, Any]:
            assert context is not None
            auth_info = context.metadata.get("adcp.auth_info")
            try:
                await _resolve_buyer_agent(_Registry(), auth_info)
                received["error"] = None
            except AdcpError as exc:
                received["error"] = exc
            return {"products": []}

    router = InMemorySubdomainTenantRouter(
        tenants={"acme.localhost": Tenant(id="t-acme", display_name="Acme")}
    )

    # Bare auth_context_factory — no credential upgrade. This is the
    # pre-fix shape the v3 reference seller had before PR #693's auth
    # wiring landed.
    mcp = create_mcp_server(
        _Recording(),
        name="t",
        advertise_all=True,
        context_factory=auth_context_factory,
        allowed_hosts=["acme.localhost", "localhost"],
        validation=None,
    )
    app = mcp.streamable_http_app()
    app.add_middleware(
        BearerTokenAuthMiddleware,
        validate_token=validator_from_token_map(
            {
                "tk-acme": Principal(
                    caller_identity="p-acme",
                    tenant_id="t-acme",
                    metadata={"api_key_id": "tk-acme"},
                )
            }
        ),
    )
    app.add_middleware(SubdomainTenantMiddleware, router=router)

    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "authorization": "Bearer tk-acme",
        "host": "acme.localhost",
    }

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://acme.localhost",
            follow_redirects=True,
        ) as client:
            init = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers=headers,
            )
            session_id = init.headers["mcp-session-id"]

            call = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_products", "arguments": {}},
                },
                headers={**headers, "mcp-session-id": session_id},
            )
            assert call.status_code == 200, call.text

    error = received.get("error")
    assert isinstance(error, AdcpError), (
        "Without the context_factory upgrading adcp.auth_info to a typed "
        "credential, the registry has nothing to match against — the "
        "framework MUST reject with PERMISSION_DENIED. If this is no "
        "longer the contract, the v3-ref-seller's auth wiring is no "
        "longer load-bearing and the documented adopter pattern needs "
        "to be revised."
    )
    assert error.code == "PERMISSION_DENIED"
    # The unrecognized-agent path omits details per the omit-on-
    # unestablished-identity rule (the wire shape MUST be
    # indistinguishable from a recognized-but-denied response). The
    # AdcpError constructor materializes ``details`` to an empty dict
    # when no scope/status was supplied — the wire-equivalence check
    # is "no details keys set", not "details is None".
    assert not error.details, (
        "PERMISSION_DENIED on the registry-miss path MUST NOT carry "
        "details. Setting ``details.scope`` would make this path "
        "distinguishable from a recognized-but-denied response, "
        "leaking an onboarding oracle. See _resolve_buyer_agent docs."
    )
