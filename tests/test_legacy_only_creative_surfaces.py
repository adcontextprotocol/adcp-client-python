"""Parity checks for creative tools that remain legacy-only in TypeScript RC3."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

import adcp
import adcp.types
import adcp.types.aliases
import adcp.types.creative
from adcp.client import ADCPClient
from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.types.core import AgentConfig, Protocol
from tests.a2a_compat_shim import Artifact, DataPart, Part, Task
from tests.a2a_compat_shim import TaskStatus as A2ATaskStatus

_UNQUALIFIED = {
    "BuildCreativeErrorResponse",
    "BuildCreativeRequest",
    "BuildCreativeResponse",
    "BuildCreativeResponse1",
    "BuildCreativeResponse2",
    "BuildCreativeResponse3",
    "BuildCreativeResponse4",
    "BuildCreativeResponse5",
    "BuildCreativeResponse6",
    "BuildCreativeSubmittedResponse",
    "BuildCreativeSuccessResponse",
    "PreviewCreativeBatchResponse",
    "PreviewCreativeInteractiveResponse",
    "PreviewCreativeRequest",
    "PreviewCreativeResponse",
    "PreviewCreativeResponse1",
    "PreviewCreativeResponse2",
    "PreviewCreativeResponse3",
    "PreviewCreativeSingleResponse",
    "PreviewCreativeStaticResponse",
    "PreviewCreativeVariantResponse",
}

_EXPLICIT_LEGACY = {
    "LegacyBuildCreativeErrorResponse",
    "LegacyBuildCreativeRequest",
    "LegacyBuildCreativeResponse",
    "LegacyBuildCreativeSubmittedResponse",
    "LegacyBuildCreativeSuccessResponse",
    "LegacyPreviewCreativeBatchResponse",
    "LegacyPreviewCreativeRequest",
    "LegacyPreviewCreativeResponse",
    "LegacyPreviewCreativeSingleResponse",
    "LegacyPreviewCreativeVariantResponse",
}

_NUMBERED_LEGACY = {
    "LegacyBuildCreativeResponse1",
    "LegacyBuildCreativeResponse2",
    "LegacyBuildCreativeResponse3",
    "LegacyBuildCreativeResponse4",
    "LegacyBuildCreativeResponse5",
    "LegacyBuildCreativeResponse6",
    "LegacyPreviewCreativeResponse1",
    "LegacyPreviewCreativeResponse2",
    "LegacyPreviewCreativeResponse3",
}


@pytest.mark.parametrize("module", [adcp, adcp.types, adcp.types.creative, adcp.types.aliases])
def test_raw_build_and_preview_types_are_explicitly_legacy(module: object) -> None:
    for name in _UNQUALIFIED:
        assert not hasattr(module, name), f"{module.__name__}.{name} must not expose raw identity"
    for name in _EXPLICIT_LEGACY:
        assert hasattr(module, name), f"{module.__name__}.{name} must remain available"
    for name in _NUMBERED_LEGACY:
        assert hasattr(module, name) is (module is not adcp.types.creative)


def test_direct_clients_expose_only_explicit_legacy_methods() -> None:
    client = ADCPClient(
        AgentConfig(id="creative", agent_uri="https://creative.example", protocol=Protocol.MCP)
    )

    assert not hasattr(client, "build_creative")
    assert not hasattr(client, "preview_creative")
    assert hasattr(client, "build_creative_legacy")
    assert hasattr(client, "preview_creative_legacy")
    assert not hasattr(client.simple, "build_creative")
    assert not hasattr(client.simple, "preview_creative")
    assert hasattr(client.simple, "build_creative_legacy")
    assert hasattr(client.simple, "preview_creative_legacy")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_name", ["build_creative", "list_creative_formats", "preview_creative"]
)
async def test_generic_primary_execution_rejects_legacy_only_tasks(task_name: str) -> None:
    client = ADCPClient(
        AgentConfig(id="creative", agent_uri="https://creative.example", protocol=Protocol.MCP)
    )

    with pytest.raises(ValueError, match="legacy-only"):
        await client.execute_task(task_name, adcp.LegacyBuildCreativeRequest.model_construct())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_name", ["build_creative", "list_creative_formats", "preview_creative"]
)
async def test_generic_primary_webhook_rejects_legacy_only_tasks(task_name: str) -> None:
    client = ADCPClient(
        AgentConfig(id="creative", agent_uri="https://creative.example", protocol=Protocol.MCP)
    )

    with pytest.raises(ValueError, match="handle_webhook_legacy"):
        await client.handle_webhook({}, task_name, "operation-1")


@pytest.mark.asyncio
async def test_legacy_webhook_entrypoint_rejects_noncreative_tasks() -> None:
    client = ADCPClient(
        AgentConfig(id="creative", agent_uri="https://creative.example", protocol=Protocol.MCP)
    )

    with pytest.raises(ValueError, match="handle_webhook"):
        await client.handle_webhook_legacy({}, "get_signals", "operation-1")


@pytest.mark.asyncio
async def test_legacy_webhook_accepts_projectable_creative_tasks() -> None:
    client = ADCPClient(
        AgentConfig(id="creative", agent_uri="https://creative.example", protocol=Protocol.MCP),
        allow_unauthenticated_webhooks=True,
    )
    payload = {
        "idempotency_key": "webhook-event-legacy",
        "task_id": "task-legacy",
        "task_type": "get_products",
        "status": "working",
        "timestamp": "2026-07-29T00:00:00Z",
        "result": {"format_id": {"agent_url": "https://legacy.example", "id": "display"}},
    }

    with pytest.warns(DeprecationWarning):
        result = await client.handle_webhook_legacy(payload, "get_products", "operation-1")

    assert _contains_legacy_identity(result.model_dump(mode="python"))


def _completed_legacy_result() -> dict[str, object]:
    return {
        "products": [],
        "format_ids": [
            {
                "agent_url": "https://creative.adcontextprotocol.org",
                "id": "display_300x250",
                "width": 300,
                "height": 250,
            }
        ],
    }


@pytest.mark.asyncio
async def test_legacy_mcp_webhook_preserves_completed_identity() -> None:
    client = ADCPClient(
        AgentConfig(id="creative", agent_uri="https://creative.example", protocol=Protocol.MCP),
        allow_unauthenticated_webhooks=True,
    )
    payload = {
        "idempotency_key": "webhook-event-completed-legacy",
        "task_id": "task-completed-legacy",
        "task_type": "get_products",
        "status": "completed",
        "timestamp": "2026-07-29T00:00:00Z",
        "result": _completed_legacy_result(),
    }

    with pytest.warns(DeprecationWarning):
        result = await client.handle_webhook_legacy(payload, "get_products", "operation-1")

    assert _contains_legacy_identity(result.model_dump(mode="python"))


@pytest.mark.asyncio
async def test_legacy_a2a_webhook_preserves_completed_identity() -> None:
    client = ADCPClient(
        AgentConfig(id="creative", agent_uri="https://creative.example", protocol=Protocol.A2A)
    )
    task = Task(
        id="task-completed-legacy",
        context_id="context-completed-legacy",
        status=A2ATaskStatus(state="completed", timestamp="2026-07-29T00:00:00Z"),
        artifacts=[
            Artifact(
                artifact_id="legacy-result",
                parts=[Part(root=DataPart(data=_completed_legacy_result()))],
            )
        ],
    )

    with pytest.warns(DeprecationWarning):
        result = await client.handle_webhook_legacy(task, "get_products", "operation-1")

    assert _contains_legacy_identity(result.model_dump(mode="python"))


def _contains_legacy_identity(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"format_id", "format_ids"} or _contains_legacy_identity(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_legacy_identity(item) for item in value)
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "working", "input-required", "completed"])
async def test_primary_webhook_sanitizes_every_status_and_activity(status: str) -> None:
    activities = []
    client = ADCPClient(
        AgentConfig(id="creative", agent_uri="https://creative.example", protocol=Protocol.MCP),
        on_activity=activities.append,
        allow_unauthenticated_webhooks=True,
    )
    payload = {
        "idempotency_key": "webhook-event-0001",
        "task_id": "task-1",
        "task_type": "get_products",
        "status": status,
        "timestamp": "2026-07-29T00:00:00Z",
        # Deliberately malformed for completed discovery and raw for every
        # non-terminal path: the primary boundary must sanitize even when
        # task-specific parsing cannot produce a canonical model.
        "result": {
            "format_id": {"agent_url": "https://legacy.example", "id": "display"},
            "unexpected": True,
        },
    }

    result = await client.handle_webhook(payload, "get_products", "operation-1")

    assert not _contains_legacy_identity(result.model_dump(mode="python"))
    received = [activity for activity in activities if activity.type.value == "webhook_received"]
    assert len(received) == 1
    assert not _contains_legacy_identity(received[0].metadata)


def test_optional_legacy_creative_tools_are_advertised_only_when_implemented() -> None:
    class CreativeBuilder(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["creative-generative"])
        accounts = SingletonAccounts(account_id="creative")

        def build_creative_legacy(self, req, ctx):
            return {}

    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            CreativeBuilder(), executor=executor, registry=InMemoryTaskRegistry()
        )
        advertised = handler.advertised_tools_for_instance()

    assert "build_creative" in advertised
    assert "preview_creative" not in advertised


@pytest.mark.asyncio
async def test_missing_legacy_format_catalog_shim_fails_with_adcp_error() -> None:
    class SalesPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="sales")

    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            SalesPlatform(), executor=executor, registry=InMemoryTaskRegistry()
        )
        assert "list_creative_formats" not in handler.advertised_tools_for_instance()
        with pytest.raises(AdcpError, match="list_creative_formats_legacy"):
            await handler.list_creative_formats_legacy(
                adcp.LegacyListCreativeFormatsRequest.model_construct()
            )
