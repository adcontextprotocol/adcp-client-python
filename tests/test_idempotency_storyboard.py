"""Client-side integration test that walks the AdCP idempotency compliance storyboard.

Source storyboard: adcontextprotocol/adcp/static/compliance/source/universal/idempotency.yaml

The storyboard is a SELLER compliance test (run via `npx @adcp/client storyboard`).
This file exercises the same 6 phases from a BUYER-SDK perspective, verifying
that our Python client drives each phase correctly against a mock seller:

1. capability_discovery  — strict client fails closed when ttl undeclared
2. missing_key           — client MUST NEVER send a mutating request without a key
3. replay_same_payload   — replayed=True surfaces; idempotency_key echoes through
4. key_reuse_conflict    — IDEMPOTENCY_CONFLICT maps to IdempotencyConflictError
5. fresh_key_new_resource — fresh UUID on second call creates a new resource
6. verify_media_buy_count — dedup actually held (single resource, two keys)
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adcp.client import ADCPClient
from adcp.exceptions import IdempotencyConflictError, IdempotencyUnsupportedError
from adcp.protocols.a2a import A2AAdapter
from adcp.types.core import AgentConfig, Protocol
from tests.a2a_compat_shim import (
    Artifact,
    DataPart,
    Part,
    SendMessageSuccessResponse,
    Task,
    part_data_dict,
)
from tests.a2a_compat_shim import (
    TaskStatus as A2ATaskStatus,
)


def _task_with_data(data: dict[str, Any]) -> Task:
    return Task(
        id=f"task_{uuid.uuid4().hex[:8]}",
        context_id=f"ctx_{uuid.uuid4().hex[:8]}",
        status=A2ATaskStatus(state="completed"),
        artifacts=[Artifact(artifact_id="a1", parts=[Part(root=DataPart(data=data))])],
    )


def _media_buy_data(media_buy_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "media_buy_id": media_buy_id,
        "confirmed_at": "2026-05-01T00:00:00Z",
        "revision": 1,
        "packages": [],
        **extra,
    }


def _cfg() -> AgentConfig:
    return AgentConfig(
        id="storyboard_seller",
        agent_uri="https://seller.test",
        protocol=Protocol.A2A,
    )


class TestStoryboardPhase1CapabilityDiscovery:
    """Phase 1 — strict mode refuses mutating calls without a declared TTL."""

    @pytest.mark.asyncio
    async def test_missing_ttl_fails_closed_in_strict_mode(self) -> None:
        client = ADCPClient(_cfg(), strict_idempotency=True)
        caps = MagicMock(spec=[])  # no adcp attribute at all
        with patch.object(client, "fetch_capabilities", AsyncMock(return_value=caps)):
            with pytest.raises(IdempotencyUnsupportedError) as exc_info:
                await client._ensure_idempotency_capability()
        # DX: suggestion should point to the workaround explicitly
        assert "strict_idempotency=False" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_declared_ttl_passes(self) -> None:
        client = ADCPClient(_cfg(), strict_idempotency=True)
        caps = MagicMock()
        caps.adcp = MagicMock(idempotency=MagicMock(supported=True, replay_ttl_seconds=86400))
        with patch.object(client, "fetch_capabilities", AsyncMock(return_value=caps)):
            await client._ensure_idempotency_capability()  # no raise


class TestStoryboardPhase2MissingKey:
    """Phase 2 — our client must never send a mutating request without a key."""

    @pytest.mark.asyncio
    async def test_auto_injected_key_on_every_mutating_call(self) -> None:
        adapter = A2AAdapter(_cfg())
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=_task_with_data({"ok": True}))
        )
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            # Caller gives no idempotency_key; SDK must inject one.
            await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})
        sent = mock_client.send_message.call_args[0][0]
        params = next(
            part_data_dict(p) for p in sent.message.parts if p.WhichOneof("content") == "data"
        )["parameters"]
        assert "idempotency_key" in params
        assert len(params["idempotency_key"]) >= 16

    @pytest.mark.asyncio
    async def test_non_mutating_call_never_gets_injection(self) -> None:
        # get_media_buys and get_products must stay payload-clean.
        adapter = A2AAdapter(_cfg())
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(result=_task_with_data({"products": []}))
        )
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            await adapter._call_a2a_tool("get_products", {"brief": "x"})
        sent = mock_client.send_message.call_args[0][0]
        params = next(
            part_data_dict(p) for p in sent.message.parts if p.WhichOneof("content") == "data"
        )["parameters"]
        assert "idempotency_key" not in params


class TestStoryboardPhase3ReplaySamePayload:
    """Phase 3 — replayed responses surface replayed=True and echo the key."""

    @pytest.mark.asyncio
    async def test_second_call_with_same_key_surfaces_replayed(self) -> None:
        adapter = A2AAdapter(_cfg())
        pinned = str(uuid.uuid4())

        # First response: fresh. Second response: same media_buy_id, replayed=True.
        seller_cache: dict[str, dict[str, Any]] = {}

        async def mock_send(request: Any) -> SendMessageSuccessResponse:
            parts = request.message.parts
            params = next(part_data_dict(p) for p in parts if p.WhichOneof("content") == "data")[
                "parameters"
            ]
            key = params["idempotency_key"]
            if key in seller_cache:
                data = dict(seller_cache[key])
                data["replayed"] = True
                return SendMessageSuccessResponse(result=_task_with_data(data))
            fresh = _media_buy_data(f"mb_{uuid.uuid4().hex[:8]}", idempotency_key=key)
            seller_cache[key] = fresh
            return SendMessageSuccessResponse(result=_task_with_data(fresh))

        mock_client = AsyncMock()
        mock_client.send_message = mock_send
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            r1 = await adapter._call_a2a_tool(
                "create_media_buy", {"brand": "acme", "idempotency_key": pinned}
            )
            r2 = await adapter._call_a2a_tool(
                "create_media_buy", {"brand": "acme", "idempotency_key": pinned}
            )

        assert r1.replayed is False
        assert r1.idempotency_key == pinned
        assert r2.replayed is True
        assert r2.idempotency_key == pinned
        # Same seller-side resource — dedup held.
        assert r1.data["media_buy_id"] == r2.data["media_buy_id"]


class TestStoryboardPhase4KeyReuseConflict:
    """Phase 4 — same key with a different payload maps to IdempotencyConflictError."""

    @pytest.mark.asyncio
    async def test_conflict_raises_typed_exception(self) -> None:
        adapter = A2AAdapter(_cfg())
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(
            return_value=SendMessageSuccessResponse(
                result=_task_with_data(
                    {
                        "errors": [
                            {
                                "code": "IDEMPOTENCY_CONFLICT",
                                "message": "payload differs from the original request",
                            }
                        ]
                    }
                )
            )
        )
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            with pytest.raises(IdempotencyConflictError) as exc_info:
                await adapter._call_a2a_tool(
                    "create_media_buy",
                    {"brand": "acme", "idempotency_key": str(uuid.uuid4())},
                )
        # DX: recovery hint is present and does NOT forward server message
        assert "mint a fresh key" in str(exc_info.value)
        assert "payload differs from the original request" not in str(exc_info.value)


class TestStoryboardPhase5FreshKeyNewResource:
    """Phase 5 — a fresh key with identical payload creates a distinct resource."""

    @pytest.mark.asyncio
    async def test_two_calls_without_pinned_key_create_two_resources(self) -> None:
        adapter = A2AAdapter(_cfg())
        seller_cache: dict[str, dict[str, Any]] = {}

        async def mock_send(request: Any) -> SendMessageSuccessResponse:
            parts = request.message.parts
            params = next(part_data_dict(p) for p in parts if p.WhichOneof("content") == "data")[
                "parameters"
            ]
            key = params["idempotency_key"]
            if key in seller_cache:
                data = dict(seller_cache[key])
                data["replayed"] = True
                return SendMessageSuccessResponse(result=_task_with_data(data))
            fresh = _media_buy_data(f"mb_{uuid.uuid4().hex[:8]}", idempotency_key=key)
            seller_cache[key] = fresh
            return SendMessageSuccessResponse(result=_task_with_data(fresh))

        mock_client = AsyncMock()
        mock_client.send_message = mock_send
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            r1 = await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})
            r2 = await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})

        assert r1.idempotency_key != r2.idempotency_key  # SDK minted fresh each time
        assert r1.data["media_buy_id"] != r2.data["media_buy_id"]
        assert r1.replayed is False and r2.replayed is False


class TestStoryboardPhase6VerifyDedupActuallyHeld:
    """Phase 6 — client-side invariant: a pinned key across retries yields exactly one resource."""

    @pytest.mark.asyncio
    async def test_pinned_key_across_retry_yields_one_resource(self) -> None:
        client = ADCPClient(_cfg())
        adapter = client.adapter
        seller_cache: dict[str, dict[str, Any]] = {}
        created_ids: set[str] = set()

        async def mock_send(request: Any) -> SendMessageSuccessResponse:
            parts = request.message.parts
            params = next(part_data_dict(p) for p in parts if p.WhichOneof("content") == "data")[
                "parameters"
            ]
            key = params["idempotency_key"]
            if key in seller_cache:
                data = dict(seller_cache[key])
                data["replayed"] = True
                return SendMessageSuccessResponse(result=_task_with_data(data))
            mb_id = f"mb_{uuid.uuid4().hex[:8]}"
            created_ids.add(mb_id)
            fresh = _media_buy_data(mb_id, idempotency_key=key)
            seller_cache[key] = fresh
            return SendMessageSuccessResponse(result=_task_with_data(fresh))

        pinned = str(uuid.uuid4())
        mock_client = AsyncMock()
        mock_client.send_message = mock_send
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            with client.use_idempotency_key(pinned):
                r1 = await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})
            with client.use_idempotency_key(pinned):
                r2 = await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})

        assert r1.idempotency_key == pinned
        assert r2.idempotency_key == pinned
        assert r2.replayed is True
        assert len(created_ids) == 1  # seller dedup'd; only one resource exists
