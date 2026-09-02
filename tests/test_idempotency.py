"""Tests for idempotency_key auto-injection, typed errors, and fail-closed capability check."""

from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adcp import _idempotency
from adcp.client import ADCPClient
from adcp.exceptions import (
    ADCPTaskError,
    IdempotencyConflictError,
    IdempotencyExpiredError,
    IdempotencyUnsupportedError,
    classify_task_error,
)
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _media_buy_data(media_buy_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "media_buy_id": media_buy_id,
        "confirmed_at": "2026-05-01T00:00:00Z",
        "revision": 1,
        "packages": [],
        **extra,
    }


class TestKeyHelpers:
    def test_generate_key_returns_uuid_v4(self) -> None:
        key = _idempotency.generate_key()
        assert UUID_RE.match(key)
        # Can be parsed as UUID
        uuid.UUID(key)

    def test_validate_key_accepts_uuid_v4(self) -> None:
        k = str(uuid.uuid4())
        assert _idempotency.validate_key(k) == k

    def test_validate_key_accepts_spec_chars(self) -> None:
        # All of [A-Za-z0-9_.:-] within bounds
        k = "abc_ABC-123.:key000000"
        assert _idempotency.validate_key(k) == k

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "too-short",
            "contains spaces here please",
            "contains/slash/chars/0000",
            "x" * 256,
            "unicode_key_é_key0000",
        ],
    )
    def test_validate_key_rejects_bad_format(self, bad: str) -> None:
        with pytest.raises(ValueError, match="idempotency_key"):
            _idempotency.validate_key(bad)

    def test_validate_key_rejects_non_string(self) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            _idempotency.validate_key(123)  # type: ignore[arg-type]

    def test_redact_prefix_form(self) -> None:
        key = "0123456789abcdef"
        assert _idempotency.redact(key) == "01234567..."

    def test_redact_none_and_short(self) -> None:
        assert _idempotency.redact(None) == "<none>"
        assert _idempotency.redact("short") == "<short>"

    def test_is_mutating_coverage(self) -> None:
        # Spot-check idempotency-required tasks across supported domains.
        mutating = {
            "create_media_buy",
            "update_media_buy",
            "sync_creatives",
            "sync_accounts",
            "si_send_message",
            "si_initiate_session",
            "sync_principal",
            "sync_reporting_receipts",
        }
        for t in mutating:
            assert _idempotency.is_mutating(t), t
        assert not _idempotency.is_mutating("get_products")
        assert not _idempotency.is_mutating("si_terminate_session")
        assert not _idempotency.is_mutating("get_adcp_capabilities")


class TestInjectKey:
    def test_injects_for_mutating_task(self) -> None:
        params, key = _idempotency.inject_key("create_media_buy", {"foo": "bar"})
        assert key is not None
        assert params["idempotency_key"] == key
        assert UUID_RE.match(key)

    def test_skips_non_mutating_task(self) -> None:
        params, key = _idempotency.inject_key("get_products", {"brief": "x"})
        assert key is None
        assert "idempotency_key" not in params

    def test_respects_caller_provided_key(self) -> None:
        mine = str(uuid.uuid4())
        params, key = _idempotency.inject_key("create_media_buy", {"idempotency_key": mine})
        assert key == mine
        assert params["idempotency_key"] == mine

    def test_rejects_bad_caller_key(self) -> None:
        with pytest.raises(ValueError):
            _idempotency.inject_key("create_media_buy", {"idempotency_key": "short"})

    def test_does_not_mutate_original_dict(self) -> None:
        original = {"foo": "bar"}
        _idempotency.inject_key("create_media_buy", original)
        assert "idempotency_key" not in original


class TestContextManager:
    def test_scoped_key_consumed_on_first_call(self) -> None:
        client = ADCPClient(_cfg())
        my_key = str(uuid.uuid4())
        with client.use_idempotency_key(my_key):
            params, key = _idempotency.inject_key(
                "create_media_buy",
                {"foo": "bar"},
                client_token=client._idempotency_client_token,
            )
            assert key == my_key

    def test_second_call_inside_scope_gets_fresh_key(self) -> None:
        # Single-use within scope: gather() siblings must not share the key.
        client = ADCPClient(_cfg())
        my_key = str(uuid.uuid4())
        with client.use_idempotency_key(my_key):
            _, k1 = _idempotency.inject_key(
                "create_media_buy",
                {},
                client_token=client._idempotency_client_token,
            )
            _, k2 = _idempotency.inject_key(
                "create_media_buy",
                {},
                client_token=client._idempotency_client_token,
            )
        assert k1 == my_key
        assert k2 != my_key  # fresh UUID generated
        assert UUID_RE.match(k2 or "")

    def test_caller_key_wins_over_scoped(self) -> None:
        client = ADCPClient(_cfg())
        scoped = str(uuid.uuid4())
        explicit = str(uuid.uuid4())
        with client.use_idempotency_key(scoped):
            _, key = _idempotency.inject_key(
                "create_media_buy",
                {"idempotency_key": explicit},
                client_token=client._idempotency_client_token,
            )
            assert key == explicit

    def test_cleanup_on_exit(self) -> None:
        client = ADCPClient(_cfg())
        k = str(uuid.uuid4())
        with client.use_idempotency_key(k):
            pass
        _, key = _idempotency.inject_key(
            "create_media_buy",
            {},
            client_token=client._idempotency_client_token,
        )
        assert key != k

    def test_nested_raises(self) -> None:
        client = ADCPClient(_cfg())
        k = str(uuid.uuid4())
        with client.use_idempotency_key(k):
            with pytest.raises(RuntimeError, match="nested"):
                with client.use_idempotency_key(k):
                    pass

    def test_invalid_key_rejected_at_entry(self) -> None:
        client = ADCPClient(_cfg())
        with pytest.raises(ValueError):
            with client.use_idempotency_key("too-short"):
                pass

    def test_scoped_key_does_not_leak_to_sibling_client(self) -> None:
        # Key pinned on client_a must NOT be used by client_b inside the same
        # block. A UserWarning is raised via warnings.warn so it shows up in
        # pytest.warns / the Python warnings machinery.
        client_a = ADCPClient(_cfg())
        client_b = ADCPClient(_cfg())
        pinned = str(uuid.uuid4())
        with client_a.use_idempotency_key(pinned):
            with pytest.warns(UserWarning, match="different client"):
                _, key = _idempotency.inject_key(
                    "create_media_buy",
                    {},
                    client_token=client_b._idempotency_client_token,
                )
        assert key != pinned
        assert UUID_RE.match(key or "")

    def test_scoped_key_used_when_client_token_matches(self) -> None:
        client = ADCPClient(_cfg())
        pinned = str(uuid.uuid4())
        with client.use_idempotency_key(pinned):
            _, key = _idempotency.inject_key(
                "create_media_buy",
                {},
                client_token=client._idempotency_client_token,
            )
        assert key == pinned


class TestTypedExceptions:
    def test_classify_routes_to_conflict(self) -> None:
        err = classify_task_error(
            "create_media_buy",
            [_fake_err("IDEMPOTENCY_CONFLICT", "payload drift")],
        )
        assert isinstance(err, IdempotencyConflictError)
        assert err.error_codes == ["IDEMPOTENCY_CONFLICT"]

    def test_classify_routes_to_expired(self) -> None:
        err = classify_task_error(
            "create_media_buy",
            [_fake_err("IDEMPOTENCY_EXPIRED", "past TTL")],
        )
        assert isinstance(err, IdempotencyExpiredError)

    def test_classify_falls_back_to_generic(self) -> None:
        err = classify_task_error(
            "create_media_buy",
            [_fake_err("INVALID_BUDGET", "nope")],
        )
        assert isinstance(err, ADCPTaskError)
        assert not isinstance(err, IdempotencyConflictError)
        assert not isinstance(err, IdempotencyExpiredError)

    def test_conflict_is_adcp_task_error(self) -> None:
        err = IdempotencyConflictError("create_media_buy", [])
        assert isinstance(err, ADCPTaskError)

    def test_expired_is_adcp_task_error(self) -> None:
        err = IdempotencyExpiredError("create_media_buy", [])
        assert isinstance(err, ADCPTaskError)


class TestRaiseForErrorData:
    def test_raises_on_conflict_code(self) -> None:
        data = {"errors": [{"code": "IDEMPOTENCY_CONFLICT", "message": "drift"}]}
        with pytest.raises(IdempotencyConflictError):
            _idempotency.raise_for_idempotency_error("create_media_buy", data, "agent-1")

    def test_raises_on_expired_code(self) -> None:
        data = {"errors": [{"code": "IDEMPOTENCY_EXPIRED", "message": "past ttl"}]}
        with pytest.raises(IdempotencyExpiredError):
            _idempotency.raise_for_idempotency_error("create_media_buy", data, "agent-1")

    def test_noop_on_other_errors(self) -> None:
        data = {"errors": [{"code": "INVALID_BUDGET"}]}
        _idempotency.raise_for_idempotency_error("create_media_buy", data, "agent-1")

    def test_noop_on_empty_data(self) -> None:
        _idempotency.raise_for_idempotency_error("create_media_buy", None, "agent-1")
        _idempotency.raise_for_idempotency_error("create_media_buy", {}, "agent-1")


class TestAnnotateResult:
    def test_surfaces_key_and_replayed_on_first_class_attrs(self) -> None:
        key = str(uuid.uuid4())
        result: TaskResult[Any] = TaskResult(
            status=TaskStatus.COMPLETED,
            data={"replayed": True, "media_buy_id": "mb_1"},
            success=True,
        )
        _idempotency.annotate_result(result, key)
        assert result.idempotency_key == key
        assert result.replayed is True

    def test_metadata_mirror_populated(self) -> None:
        key = str(uuid.uuid4())
        result: TaskResult[Any] = TaskResult(
            status=TaskStatus.COMPLETED,
            data={"replayed": True, "media_buy_id": "mb_1"},
            success=True,
        )
        _idempotency.annotate_result(result, key)
        assert result.metadata is not None
        assert result.metadata["idempotency_key"] == key
        assert result.metadata["idempotency_replayed"] is True

    def test_replayed_defaults_false(self) -> None:
        key = str(uuid.uuid4())
        result: TaskResult[Any] = TaskResult(
            status=TaskStatus.COMPLETED,
            data={"media_buy_id": "mb_1"},
            success=True,
        )
        _idempotency.annotate_result(result, key)
        assert result.replayed is False

    def test_preserves_existing_metadata(self) -> None:
        key = str(uuid.uuid4())
        result: TaskResult[Any] = TaskResult(
            status=TaskStatus.COMPLETED,
            data={},
            success=True,
            metadata={"task_id": "t_1"},
        )
        _idempotency.annotate_result(result, key)
        assert result.metadata is not None
        assert result.metadata["task_id"] == "t_1"
        assert result.metadata["idempotency_key"] == key


class TestRedactParams:
    def test_redacts_key(self) -> None:
        params = {"idempotency_key": "0123456789abcdef-xyz"}
        out = _idempotency.redact_params(params)
        assert out["idempotency_key"] == "01234567..."

    def test_leaves_params_without_key_untouched(self) -> None:
        params = {"brief": "x"}
        out = _idempotency.redact_params(params)
        assert out is params

    def test_does_not_mutate_original(self) -> None:
        full_key = "0123456789abcdef-xyz"
        params = {"idempotency_key": full_key, "other": "v"}
        _idempotency.redact_params(params)
        assert params["idempotency_key"] == full_key


class TestStrictIdempotencyCapabilityCheck:
    @pytest.mark.asyncio
    async def test_strict_off_skips_check(self) -> None:
        cfg = _cfg()
        client = ADCPClient(cfg, strict_idempotency=False)
        assert client.adapter.idempotency_capability_check is None

    @pytest.mark.asyncio
    async def test_strict_raises_when_idempotency_missing(self) -> None:
        cfg = _cfg()
        client = ADCPClient(cfg, strict_idempotency=True)
        caps = MagicMock()
        caps.adcp = MagicMock(idempotency=None)
        with patch.object(client, "fetch_capabilities", AsyncMock(return_value=caps)):
            with pytest.raises(IdempotencyUnsupportedError):
                await client._ensure_idempotency_capability()

    @pytest.mark.asyncio
    async def test_strict_raises_when_supported_false(self) -> None:
        cfg = _cfg()
        client = ADCPClient(cfg, strict_idempotency=True)
        caps = MagicMock()
        caps.adcp = MagicMock(idempotency=MagicMock(supported=False, replay_ttl_seconds=86400))
        with patch.object(client, "fetch_capabilities", AsyncMock(return_value=caps)):
            with pytest.raises(IdempotencyUnsupportedError):
                await client._ensure_idempotency_capability()

    @pytest.mark.asyncio
    async def test_strict_raises_when_supported_true_but_ttl_missing(self) -> None:
        cfg = _cfg()
        client = ADCPClient(cfg, strict_idempotency=True)
        caps = MagicMock()
        caps.adcp = MagicMock(idempotency=MagicMock(supported=True, replay_ttl_seconds=None))
        with patch.object(client, "fetch_capabilities", AsyncMock(return_value=caps)):
            with pytest.raises(IdempotencyUnsupportedError):
                await client._ensure_idempotency_capability()

    @pytest.mark.asyncio
    async def test_strict_passes_when_supported_and_ttl_declared(self) -> None:
        cfg = _cfg()
        client = ADCPClient(cfg, strict_idempotency=True)
        caps = MagicMock()
        caps.adcp = MagicMock(idempotency=MagicMock(supported=True, replay_ttl_seconds=86400))
        with patch.object(client, "fetch_capabilities", AsyncMock(return_value=caps)):
            await client._ensure_idempotency_capability()
            # Second call is a no-op (cached)
            await client._ensure_idempotency_capability()

    @pytest.mark.asyncio
    async def test_strict_raises_when_adcp_info_missing(self) -> None:
        cfg = _cfg()
        client = ADCPClient(cfg, strict_idempotency=True)
        caps = MagicMock(spec=[])  # no `adcp` attribute
        with patch.object(client, "fetch_capabilities", AsyncMock(return_value=caps)):
            with pytest.raises(IdempotencyUnsupportedError):
                await client._ensure_idempotency_capability()

    @pytest.mark.asyncio
    async def test_capability_check_runs_once_across_calls(self) -> None:
        cfg = _cfg()
        client = ADCPClient(cfg, strict_idempotency=True)
        caps = MagicMock()
        caps.adcp = MagicMock(idempotency=MagicMock(supported=True, replay_ttl_seconds=86400))
        fetch = AsyncMock(return_value=caps)
        with patch.object(client, "fetch_capabilities", fetch):
            await client._ensure_idempotency_capability()
            await client._ensure_idempotency_capability()
            await client._ensure_idempotency_capability()
        assert fetch.call_count == 1  # cached after first verification

    @pytest.mark.asyncio
    async def test_capability_check_clears_flag_on_error(self) -> None:
        cfg = _cfg()
        client = ADCPClient(cfg, strict_idempotency=True)
        fetch = AsyncMock(side_effect=RuntimeError("network blip"))
        with patch.object(client, "fetch_capabilities", fetch):
            with pytest.raises(RuntimeError):
                await client._ensure_idempotency_capability()
        assert client._idempotency_capability_verified is False

    def test_get_adcp_capabilities_is_not_mutating(self) -> None:
        # Invariant: the capability-fetch tool MUST stay out of the mutating set;
        # otherwise the strict-idempotency check would recurse on itself.
        assert not _idempotency.is_mutating("get_adcp_capabilities")


class TestA2AAdapterIntegration:
    """End-to-end coverage on the A2A adapter — injection, raising, metadata surface."""

    @pytest.mark.asyncio
    async def test_injects_key_into_outbound_message(self) -> None:
        from adcp.protocols.a2a import A2AAdapter
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

        adapter = A2AAdapter(_cfg(Protocol.A2A))
        task = Task(
            id="t1",
            context_id="c1",
            status=A2ATaskStatus(state="completed"),
            artifacts=[Artifact(artifact_id="a1", parts=[Part(root=DataPart(data={"ok": True}))])],
        )
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=SendMessageSuccessResponse(result=task))
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})
        sent = mock_client.send_message.call_args[0][0]
        # Walk the outbound DataPart to find injected params
        parts = sent.message.parts
        data = next(part_data_dict(p) for p in parts if p.WhichOneof("content") == "data")
        assert "idempotency_key" in data["parameters"]
        assert UUID_RE.match(data["parameters"]["idempotency_key"])

    @pytest.mark.asyncio
    async def test_non_mutating_task_omits_key(self) -> None:
        from adcp.protocols.a2a import A2AAdapter
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

        adapter = A2AAdapter(_cfg(Protocol.A2A))
        task = Task(
            id="t1",
            context_id="c1",
            status=A2ATaskStatus(state="completed"),
            artifacts=[Artifact(artifact_id="a1", parts=[Part(root=DataPart(data={}))])],
        )
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=SendMessageSuccessResponse(result=task))
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            await adapter._call_a2a_tool("get_products", {"brief": "x"})
        sent = mock_client.send_message.call_args[0][0]
        data = next(
            part_data_dict(p) for p in sent.message.parts if p.WhichOneof("content") == "data"
        )
        assert "idempotency_key" not in data["parameters"]

    @pytest.mark.asyncio
    async def test_conflict_code_raises(self) -> None:
        from adcp.protocols.a2a import A2AAdapter
        from tests.a2a_compat_shim import (
            Artifact,
            DataPart,
            Part,
            SendMessageSuccessResponse,
            Task,
        )
        from tests.a2a_compat_shim import (
            TaskStatus as A2ATaskStatus,
        )

        adapter = A2AAdapter(_cfg(Protocol.A2A))
        task = Task(
            id="t1",
            context_id="c1",
            status=A2ATaskStatus(state="completed"),
            artifacts=[
                Artifact(
                    artifact_id="a1",
                    parts=[
                        Part(
                            root=DataPart(
                                data={
                                    "errors": [
                                        {
                                            "code": "IDEMPOTENCY_CONFLICT",
                                            "message": "payload differs",
                                        }
                                    ]
                                }
                            )
                        )
                    ],
                )
            ],
        )
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=SendMessageSuccessResponse(result=task))
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            with pytest.raises(IdempotencyConflictError):
                await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})

    @pytest.mark.asyncio
    async def test_replayed_surfaces_on_result(self) -> None:
        from adcp.protocols.a2a import A2AAdapter
        from tests.a2a_compat_shim import (
            Artifact,
            DataPart,
            Part,
            SendMessageSuccessResponse,
            Task,
        )
        from tests.a2a_compat_shim import (
            TaskStatus as A2ATaskStatus,
        )

        adapter = A2AAdapter(_cfg(Protocol.A2A))
        task = Task(
            id="t1",
            context_id="c1",
            status=A2ATaskStatus(state="completed"),
            artifacts=[
                Artifact(
                    artifact_id="a1",
                    parts=[Part(root=DataPart(data=_media_buy_data("mb_1", replayed=True)))],
                )
            ],
        )
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=SendMessageSuccessResponse(result=task))
        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            result = await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})
        assert result.replayed is True
        assert result.idempotency_key is not None
        assert UUID_RE.match(result.idempotency_key)


class TestMCPAdapterIntegration:
    """End-to-end MCP adapter: injection, structured + text-only errors, replay."""

    @pytest.mark.asyncio
    async def test_injects_key_into_mcp_call(self) -> None:
        from adcp.protocols.mcp import MCPAdapter

        adapter = MCPAdapter(_cfg(Protocol.MCP))
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = []
        mock_result.structuredContent = _media_buy_data("mb_1")
        session.call_tool = AsyncMock(return_value=mock_result)
        with patch.object(adapter, "_get_session", AsyncMock(return_value=session)):
            await adapter._call_mcp_tool("create_media_buy", {"brand": "acme"})
        # call_tool is invoked with (tool_name, params_dict)
        sent_name, sent_params = session.call_tool.call_args[0]
        assert sent_name == "create_media_buy"
        assert "idempotency_key" in sent_params
        assert UUID_RE.match(sent_params["idempotency_key"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("task_name", ["sync_principal", "sync_reporting_receipts"])
    async def test_new_mutating_tasks_run_strict_idempotency_preflight(
        self, task_name: str
    ) -> None:
        from adcp.protocols.mcp import MCPAdapter

        adapter = MCPAdapter(_cfg(Protocol.MCP))
        adapter.request_validation_mode = "off"
        preflight = AsyncMock()
        adapter.idempotency_capability_check = preflight
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = []
        mock_result.structuredContent = {}
        session.call_tool = AsyncMock(return_value=mock_result)

        with patch.object(adapter, "_get_session", AsyncMock(return_value=session)):
            await adapter._call_mcp_tool(task_name, {})

        preflight.assert_awaited_once()
        _, sent_params = session.call_tool.call_args[0]
        assert "idempotency_key" in sent_params

    @pytest.mark.asyncio
    async def test_non_mutating_mcp_call_omits_key(self) -> None:
        from adcp.protocols.mcp import MCPAdapter

        adapter = MCPAdapter(_cfg(Protocol.MCP))
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = []
        mock_result.structuredContent = {"products": []}
        session.call_tool = AsyncMock(return_value=mock_result)
        with patch.object(adapter, "_get_session", AsyncMock(return_value=session)):
            await adapter._call_mcp_tool("get_products", {"brief": "x"})
        _, sent_params = session.call_tool.call_args[0]
        assert "idempotency_key" not in sent_params

    @pytest.mark.asyncio
    async def test_structured_conflict_raises(self) -> None:
        # Spec-canonical MCP error shape per transport-errors.mdx: structuredContent
        # carries {"adcp_error": {"code": ..., "message": ...}} (singular), not
        # the A2A-style {"errors": [...]} array.
        from adcp.protocols.mcp import MCPAdapter

        adapter = MCPAdapter(_cfg(Protocol.MCP))
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.isError = True
        mock_result.content = [{"type": "text", "text": "Conflict"}]
        mock_result.structuredContent = {
            "adcp_error": {"code": "IDEMPOTENCY_CONFLICT", "message": "drift"}
        }
        session.call_tool = AsyncMock(return_value=mock_result)
        with patch.object(adapter, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(IdempotencyConflictError):
                await adapter._call_mcp_tool("create_media_buy", {"brand": "acme"})

    @pytest.mark.asyncio
    async def test_text_only_conflict_raises(self) -> None:
        # FastMCP default: is_error=true, text content only (no structuredContent).
        from adcp.protocols.mcp import MCPAdapter

        adapter = MCPAdapter(_cfg(Protocol.MCP))
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.isError = True
        # Dict-shaped content passes through _serialize_mcp_content unchanged.
        mock_result.content = [
            {"type": "text", "text": "IDEMPOTENCY_CONFLICT: payload differs from original request"}
        ]
        mock_result.structuredContent = None
        session.call_tool = AsyncMock(return_value=mock_result)
        with patch.object(adapter, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(IdempotencyConflictError):
                await adapter._call_mcp_tool("create_media_buy", {"brand": "acme"})

    @pytest.mark.asyncio
    async def test_text_only_expired_raises(self) -> None:
        from adcp.protocols.mcp import MCPAdapter

        adapter = MCPAdapter(_cfg(Protocol.MCP))
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.isError = True
        mock_result.content = [
            {"type": "text", "text": "IDEMPOTENCY_EXPIRED: replay window has closed"}
        ]
        mock_result.structuredContent = None
        session.call_tool = AsyncMock(return_value=mock_result)
        with patch.object(adapter, "_get_session", AsyncMock(return_value=session)):
            with pytest.raises(IdempotencyExpiredError):
                await adapter._call_mcp_tool("create_media_buy", {"brand": "acme"})

    @pytest.mark.asyncio
    async def test_replayed_surfaces_via_mcp(self) -> None:
        from adcp.protocols.mcp import MCPAdapter

        adapter = MCPAdapter(_cfg(Protocol.MCP))
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.isError = False
        mock_result.content = []
        mock_result.structuredContent = _media_buy_data("mb_1", replayed=True)
        session.call_tool = AsyncMock(return_value=mock_result)
        with patch.object(adapter, "_get_session", AsyncMock(return_value=session)):
            result = await adapter._call_mcp_tool("create_media_buy", {"brand": "acme"})
        assert result.replayed is True
        assert result.idempotency_key is not None


class TestGatherSemantics:
    """Lock down single-use behavior under asyncio.gather inside use_idempotency_key."""

    @pytest.mark.asyncio
    async def test_gather_siblings_do_not_share_pinned_key(self) -> None:
        import asyncio

        from adcp.protocols.a2a import A2AAdapter
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

        client = ADCPClient(_cfg())
        adapter: A2AAdapter = client.adapter  # type: ignore[assignment]
        task = Task(
            id="t1",
            context_id="c1",
            status=A2ATaskStatus(state="completed"),
            artifacts=[Artifact(artifact_id="a1", parts=[Part(root=DataPart(data={}))])],
        )
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=SendMessageSuccessResponse(result=task))

        pinned = str(uuid.uuid4())
        sent_keys: list[str] = []

        async def one_call() -> None:
            await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})

        with patch.object(adapter, "_get_a2a_client", return_value=mock_client):
            with client.use_idempotency_key(pinned):
                await asyncio.gather(one_call(), one_call(), one_call())

        # Walk the three send_message invocations and extract the keys sent.
        for call in mock_client.send_message.call_args_list:
            req = call[0][0]
            parts = req.message.parts
            data = next(part_data_dict(p) for p in parts if p.WhichOneof("content") == "data")
            sent_keys.append(data["parameters"]["idempotency_key"])

        assert pinned in sent_keys  # the pinned key was consumed exactly once
        assert sum(1 for k in sent_keys if k == pinned) == 1
        # The other two gather siblings got fresh UUIDs.
        fresh = [k for k in sent_keys if k != pinned]
        assert len(fresh) == 2
        assert len(set(fresh)) == 2  # and they're distinct from each other


class TestPydanticRoundTrip:
    """Confirm the Pydantic request → params dict → adapter chain preserves keys.

    Uses ``ReportUsageRequest`` because it's a minimal mutating request
    (3 required fields) — sufficient to exercise the Pydantic → model_dump →
    inject_key path without having to construct a full media-buy skeleton.
    """

    @pytest.mark.asyncio
    async def test_caller_set_pydantic_key_reaches_adapter(self) -> None:
        from adcp.types import ReportUsageRequest
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

        client = ADCPClient(_cfg())
        pinned = str(uuid.uuid4())
        req = ReportUsageRequest.model_validate(
            {
                "idempotency_key": pinned,
                "reporting_period": {
                    "start": "2026-05-01T00:00:00Z",
                    "end": "2026-05-31T23:59:59Z",
                },
                "usage": [
                    {
                        "account": {"account_id": "acct_1"},
                        "vendor_cost": 1.0,
                        "currency": "USD",
                    }
                ],
            }
        )

        task = Task(
            id="t1",
            context_id="c1",
            status=A2ATaskStatus(state="completed"),
            artifacts=[
                Artifact(artifact_id="a1", parts=[Part(root=DataPart(data={"status": "ok"}))])
            ],
        )
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=SendMessageSuccessResponse(result=task))
        with patch.object(client.adapter, "_get_a2a_client", return_value=mock_client):
            result = await client.report_usage(req)

        sent = mock_client.send_message.call_args[0][0]
        parts = sent.message.parts
        data = next(part_data_dict(p) for p in parts if p.WhichOneof("content") == "data")
        assert data["parameters"]["idempotency_key"] == pinned
        assert result.idempotency_key == pinned


class TestWireFormat:
    """Capture the actual bytes the SDK puts on the wire — catches a class of
    regressions (renames, re-encodings, double-wrapping) that assertions at the
    adapter-object boundary cannot see."""

    @pytest.mark.asyncio
    async def test_outbound_http_body_contains_one_unredacted_key(self) -> None:
        """Wire-level assertion: the outbound ``SendMessageRequest`` proto,
        serialized to JSON for the 1.0 JSON-RPC transport, carries the
        injected idempotency_key exactly once in the DataPart.parameters.

        The test builds the outbound request shape by hand (protobuf
        :meth:`MessageToDict`) from an ``A2AAdapter`` call that intercepts
        the outbound ``SendMessageRequest`` at the client boundary, so it
        doesn't depend on a real JSON-RPC transport round-trip.
        """
        from google.protobuf.json_format import MessageToDict, MessageToJson

        from adcp.protocols.a2a import A2AAdapter
        from tests.a2a_compat_shim import (
            Artifact,
            DataPart,
            SendMessageSuccessResponse,
            Task,
        )
        from tests.a2a_compat_shim import (
            TaskStatus as A2ATaskStatus,
        )

        captured: dict[str, Any] = {}

        mock_a2a_client = AsyncMock()

        async def fake_send(request: Any) -> Any:  # noqa: D401
            # Capture the wire-format JSON of the outbound SendMessageRequest
            captured["body"] = MessageToJson(request, preserving_proto_field_name=False)
            captured["dict"] = MessageToDict(request, preserving_proto_field_name=False)
            # Return a minimal successful response (Task in the result slot).
            task = Task(
                id="t1",
                context_id="c1",
                status=A2ATaskStatus(state="completed"),
                artifacts=[
                    Artifact(
                        artifact_id="a1",
                        parts=[DataPart(data=_media_buy_data("mb_1"))],
                    )
                ],
            )
            return SendMessageSuccessResponse(result=task)

        mock_a2a_client.send_message = fake_send
        adapter = A2AAdapter(_cfg(Protocol.A2A))
        with patch.object(adapter, "_get_a2a_client", return_value=mock_a2a_client):
            await adapter._call_a2a_tool("create_media_buy", {"brand": "acme"})

        # The outbound body must carry exactly one idempotency_key, full
        # (not redacted), inside the DataPart ``parameters`` object.
        assert captured
        parts = captured["dict"]["message"]["parts"]
        data_part = next(p for p in parts if "data" in p)
        params = data_part["data"]["parameters"]
        assert "idempotency_key" in params
        assert UUID_RE.match(params["idempotency_key"])
        # And the full key appears exactly once in the body.
        assert captured["body"].count(params["idempotency_key"]) == 1
        # And it is NOT redacted (redacted form ends with "...").
        assert "..." not in params["idempotency_key"]


def _cfg(protocol: Protocol = Protocol.A2A) -> AgentConfig:
    return AgentConfig(id="t", agent_uri="https://example.test", protocol=protocol)


def _fake_err(code: str, message: str) -> Any:
    class _E:
        pass

    e = _E()
    e.code = code
    e.message = message
    return e
