"""Caller-identity propagation from transport layer into ToolContext.

Bridges the gap between the per-request authenticated principal (lives in the
A2A ``ServerCallContext.user`` / sellers' FastMCP auth middleware) and the
server-side middleware layer (idempotency per-principal scoping, future
audit logging). Without this wiring, ``ToolContext.caller_identity`` is
always ``None`` and the idempotency middleware's fail-closed path skips
dedup entirely — effectively inert in production.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from adcp.server.a2a_server import ADCPAgentExecutor, _tool_context_from_request
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.idempotency import IdempotencyStore, MemoryBackend
from adcp.server.mcp_tools import create_tool_caller


class _FakeUser:
    def __init__(self, name: str, authenticated: bool = True) -> None:
        self._name = name
        self._auth = authenticated

    @property
    def is_authenticated(self) -> bool:
        return self._auth

    @property
    def user_name(self) -> str:
        return self._name


class _FakeCallContext:
    def __init__(self, user: Any = None) -> None:
        self.user = user


class _FakeRequestContext:
    def __init__(self, *, task_id: str | None = None, user: Any = None) -> None:
        self.task_id = task_id
        self.call_context = _FakeCallContext(user=user) if user is not None else None


class TestToolContextFromRequest:
    def test_authenticated_user_populates_caller_identity(self) -> None:
        req = _FakeRequestContext(user=_FakeUser("buyer-acme"))
        ctx = _tool_context_from_request(req)
        assert ctx.caller_identity == "buyer-acme"

    def test_unauthenticated_user_leaves_identity_none(self) -> None:
        req = _FakeRequestContext(user=_FakeUser("", authenticated=False))
        ctx = _tool_context_from_request(req)
        assert ctx.caller_identity is None

    def test_missing_call_context_leaves_identity_none(self) -> None:
        req = _FakeRequestContext(user=None)
        ctx = _tool_context_from_request(req)
        assert ctx.caller_identity is None

    def test_authenticated_with_empty_user_name_leaves_identity_none(self) -> None:
        req = _FakeRequestContext(user=_FakeUser("", authenticated=True))
        ctx = _tool_context_from_request(req)
        assert ctx.caller_identity is None

    def test_task_id_propagates(self) -> None:
        req = _FakeRequestContext(task_id="task-xyz", user=_FakeUser("buyer-1"))
        ctx = _tool_context_from_request(req)
        assert ctx.request_id == "task-xyz"
        assert ctx.caller_identity == "buyer-1"


class TestToolCallerContextPassthrough:
    @pytest.mark.asyncio
    async def test_caller_accepts_context_and_forwards_it(self) -> None:
        class H(ADCPHandler):
            def __init__(self) -> None:
                self.seen_ctx: ToolContext | None = None

            async def get_products(
                self, params: Any, context: ToolContext | None = None
            ) -> dict[str, Any]:
                self.seen_ctx = context
                return {"products": []}

        h = H()
        caller = create_tool_caller(h, "get_products")
        injected = ToolContext(caller_identity="buyer-xyz")
        await caller({"brief": "x"}, injected)
        assert h.seen_ctx is injected
        assert h.seen_ctx.caller_identity == "buyer-xyz"

    @pytest.mark.asyncio
    async def test_caller_defaults_to_bare_context(self) -> None:
        class H(ADCPHandler):
            def __init__(self) -> None:
                self.seen_ctx: ToolContext | None = None

            async def get_products(
                self, params: Any, context: ToolContext | None = None
            ) -> dict[str, Any]:
                self.seen_ctx = context
                return {"products": []}

        h = H()
        caller = create_tool_caller(h, "get_products")
        # No context passed — backward-compatible behavior.
        await caller({"brief": "x"})
        assert isinstance(h.seen_ctx, ToolContext)
        assert h.seen_ctx.caller_identity is None


class TestEndToEndIdempotencyViaTransport:
    @pytest.mark.asyncio
    async def test_a2a_transport_identity_enables_middleware_dedup(self) -> None:
        """Full wire: A2A authenticated user → ToolContext.caller_identity →
        IdempotencyStore.wrap scopes per-principal and dedups the replay."""
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class Seller(ADCPHandler):
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def create_media_buy(
                self, params: Any, context: ToolContext | None = None
            ) -> dict[str, Any]:
                self.calls += 1
                return {"media_buy_id": f"mb_{self.calls}", "status": "completed"}

        seller = Seller()
        executor = ADCPAgentExecutor(seller, validation=None)
        key = str(uuid.uuid4())

        # Simulate two successive A2A calls from the same authenticated buyer.
        params = {"idempotency_key": key, "brand": {"domain": "acme.test"}}
        tool_context = _tool_context_from_request(_FakeRequestContext(user=_FakeUser("buyer-acme")))
        r1 = await executor._tool_callers["create_media_buy"](params, tool_context)
        r2 = await executor._tool_callers["create_media_buy"](params, tool_context)
        assert seller.calls == 1  # middleware dedup'd the second call
        # IdempotencyStore.wrap injects ``replayed: true`` on the replay
        # envelope per AdCP L1/security rule 4 (#714); the rest of the
        # response must be identical to the first call.
        assert r2.get("replayed") is True
        assert {k: v for k, v in r2.items() if k != "replayed"} == r1

    @pytest.mark.asyncio
    async def test_distinct_principals_scope_independently(self) -> None:
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class Seller(ADCPHandler):
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def create_media_buy(
                self, params: Any, context: ToolContext | None = None
            ) -> dict[str, Any]:
                self.calls += 1
                return {"media_buy_id": f"mb_{self.calls}"}

        seller = Seller()
        executor = ADCPAgentExecutor(seller, validation=None)
        key = str(uuid.uuid4())
        params = {"idempotency_key": key, "brand": "acme"}

        ctx_a = _tool_context_from_request(_FakeRequestContext(user=_FakeUser("buyer-a")))
        ctx_b = _tool_context_from_request(_FakeRequestContext(user=_FakeUser("buyer-b")))
        r_a = await executor._tool_callers["create_media_buy"](params, ctx_a)
        r_b = await executor._tool_callers["create_media_buy"](params, ctx_b)
        # Same key under distinct principals must NOT collide.
        assert seller.calls == 2
        assert r_a["media_buy_id"] != r_b["media_buy_id"]

    @pytest.mark.asyncio
    async def test_unauthenticated_falls_through_no_dedup(self) -> None:
        """Without a principal, the middleware's fail-closed path skips dedup."""
        store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

        class Seller(ADCPHandler):
            def __init__(self) -> None:
                self.calls = 0

            @store.wrap
            async def create_media_buy(
                self, params: Any, context: ToolContext | None = None
            ) -> dict[str, Any]:
                self.calls += 1
                return {"media_buy_id": f"mb_{self.calls}"}

        seller = Seller()
        executor = ADCPAgentExecutor(seller, validation=None)
        key = str(uuid.uuid4())
        params = {"idempotency_key": key, "brand": "acme"}

        anon_ctx = _tool_context_from_request(
            _FakeRequestContext(user=_FakeUser("", authenticated=False))
        )
        await executor._tool_callers["create_media_buy"](params, anon_ctx)
        await executor._tool_callers["create_media_buy"](params, anon_ctx)
        # Both executed — no principal to scope by, middleware skipped dedup.
        assert seller.calls == 2


class TestA2AExecutorUsesRealContext:
    """Integration with the real A2A execute() path."""

    @pytest.mark.asyncio
    async def test_execute_passes_tool_context_with_identity(self) -> None:
        from tests.a2a_compat_shim import DataPart, Message, Part, Role

        seen: dict[str, Any] = {}

        class CaptureHandler(ADCPHandler):
            async def get_products(
                self, params: Any, context: ToolContext | None = None
            ) -> dict[str, Any]:
                seen["identity"] = context.caller_identity if context else None
                return {"products": []}

        # Transport-plumbing test — opt out of strict-by-default
        # wire-conformance so the stub's empty-products response and
        # missing required request fields don't short-circuit the
        # dispatch under test.
        executor = ADCPAgentExecutor(CaptureHandler(), validation=None)

        # Build a minimal A2A request with an authenticated user.
        class _Req:
            def __init__(self) -> None:
                self.task_id = "task-1"
                self.context_id = "ctx-1"
                self.call_context = _FakeCallContext(user=_FakeUser("buyer-live"))
                # Message with an explicit skill DataPart — mimics a real client.
                self.message = Message(
                    message_id="m1",
                    role=Role.user,
                    parts=[
                        Part(
                            root=DataPart(
                                data={
                                    "skill": "get_products",
                                    "parameters": {"brief": "test"},
                                }
                            )
                        )
                    ],
                )

        captured_events: list[Any] = []

        class FakeQueue:
            async def enqueue_event(self, event: Any) -> None:
                captured_events.append(event)

        await executor.execute(_Req(), FakeQueue())
        assert seen["identity"] == "buyer-live"
