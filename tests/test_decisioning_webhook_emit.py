"""F12: auto-emit completion webhook on sync-success arm.

Mirrors the JS test file
``test/server-decisioning-auto-emit-completion.test.js`` (commits
``8dc427f9`` + ``7a887dfa``) plus Python-specific concerns:

* TaskHandoff projection path doesn't double-fire (registry completion
  emits its own webhook on terminal state).
* Fire-and-forget non-blocking — sync response returns before webhook
  delivery.
* Tools outside ``SPEC_WEBHOOK_TASK_TYPES`` skip with a warning.
* No-running-loop branch is silent (sync test paths).
* SPEC_WEBHOOK_TASK_TYPES drift-guard against the on-disk schema cache.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.task_registry import InMemoryTaskRegistry
from adcp.decisioning.webhook_emit import (
    _BACKGROUND_WEBHOOK_TASKS,
    SPEC_WEBHOOK_TASK_TYPES,
    _extract_push_notification_url_and_token,
    maybe_emit_sync_completion,
)
from adcp.server.base import ToolContext
from adcp.types import (
    CreateMediaBuyRequest,
    CreateMediaBuySuccessResponse,
    SyncCreativesRequest,
)

# ---- SPEC_WEBHOOK_TASK_TYPES drift-guard ----


def test_spec_webhook_task_types_matches_schema_cache() -> None:
    """Pin the constant to the on-disk spec enum. CI catches
    out-of-band drift when the schema cache refreshes from upstream."""
    schema_path = Path(__file__).parent.parent / "schemas" / "cache" / "enums" / "task-type.json"
    with schema_path.open() as f:
        on_disk = frozenset(json.load(f)["enum"])
    assert SPEC_WEBHOOK_TASK_TYPES == on_disk, (
        f"SPEC_WEBHOOK_TASK_TYPES drifted from on-disk task-type enum. "
        f"Missing from constant: {sorted(on_disk - SPEC_WEBHOOK_TASK_TYPES)}; "
        f"extra in constant: {sorted(SPEC_WEBHOOK_TASK_TYPES - on_disk)}."
    )


# ---- _extract_push_notification_url_and_token ----


def test_extract_returns_none_when_config_missing() -> None:
    """No ``push_notification_config`` field → no auto-emit."""

    class _Bare:
        pass

    assert _extract_push_notification_url_and_token(_Bare()) is None


def test_extract_returns_none_when_config_is_none() -> None:
    """Field present but ``None`` → no auto-emit."""

    class _NullConfig:
        push_notification_config = None

    assert _extract_push_notification_url_and_token(_NullConfig()) is None


def test_extract_returns_url_and_token_when_present() -> None:
    """Field with URL + token → both pulled out."""

    class _Config:
        url = "https://buyer.example.com/webhooks"
        token = "echo-back-this-token"

    class _Params:
        push_notification_config = _Config()

    extracted = _extract_push_notification_url_and_token(_Params())
    assert extracted == ("https://buyer.example.com/webhooks", "echo-back-this-token")


def test_extract_returns_url_with_none_token() -> None:
    """Field with URL only → token is None."""

    class _Config:
        url = "https://buyer.example.com/webhooks"
        token = None

    class _Params:
        push_notification_config = _Config()

    extracted = _extract_push_notification_url_and_token(_Params())
    assert extracted == ("https://buyer.example.com/webhooks", None)


def test_extract_handles_dict_params() -> None:
    """Test fixtures using plain-dict params still work."""
    params = {
        "push_notification_config": {
            "url": "https://buyer.example.com/webhooks",
            "token": "tok",
        }
    }
    extracted = _extract_push_notification_url_and_token(params)
    assert extracted == ("https://buyer.example.com/webhooks", "tok")


# ---- maybe_emit_sync_completion gate ----


@pytest.mark.asyncio
async def test_maybe_emit_skips_when_disabled() -> None:
    """``enabled=False`` → no delivery, no background task."""
    sender = AsyncMock()

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    maybe_emit_sync_completion(
        sender=sender,
        enabled=False,
        method_name="create_media_buy",
        params=_Params(),
        result={"media_buy_id": "mb_1"},
    )
    await asyncio.sleep(0)
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_emit_skips_when_sender_none() -> None:
    """``sender=None`` → silent skip (no emitter wired)."""

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    # Smoke — must not raise.
    maybe_emit_sync_completion(
        sender=None,
        enabled=True,
        method_name="create_media_buy",
        params=_Params(),
        result={"media_buy_id": "mb_1"},
    )


@pytest.mark.asyncio
async def test_maybe_emit_skips_when_no_push_url() -> None:
    """Request without ``push_notification_config.url`` → no delivery."""
    sender = AsyncMock()

    class _Params:
        push_notification_config = None

    maybe_emit_sync_completion(
        sender=sender,
        enabled=True,
        method_name="create_media_buy",
        params=_Params(),
        result={"media_buy_id": "mb_1"},
    )
    await asyncio.sleep(0)
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_emit_skips_tool_outside_spec_enum(caplog) -> None:
    """Tool not in ``SPEC_WEBHOOK_TASK_TYPES`` → skip + warn.

    Spec-validating receivers reject envelopes with non-spec
    ``task_type`` values; the framework logs once per skip so adopters
    notice they extended the tool surface beyond the spec enum."""
    import logging

    sender = AsyncMock()

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    with caplog.at_level(logging.WARNING, logger="adcp.decisioning.webhook_emit"):
        maybe_emit_sync_completion(
            sender=sender,
            enabled=True,
            method_name="custom_adopter_tool",  # Not in spec enum
            params=_Params(),
            result={"x": 1},
        )
    await asyncio.sleep(0)
    sender.send_mcp.assert_not_called()
    assert any("not in spec task-type enum" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_maybe_emit_fires_when_url_set() -> None:
    """Happy path — URL set + tool in enum + enabled → background
    delivery via ``WebhookSender.send_mcp``."""
    sender = AsyncMock()

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    maybe_emit_sync_completion(
        sender=sender,
        enabled=True,
        method_name="create_media_buy",
        params=_Params(),
        result={"media_buy_id": "mb_1"},
    )
    # Drain background task.
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)
    sender.send_mcp.assert_awaited_once()
    call_kwargs = sender.send_mcp.await_args.kwargs
    assert call_kwargs["url"] == "https://buyer.example.com/wh"
    assert call_kwargs["task_type"] == "create_media_buy"
    assert call_kwargs["status"] == "completed"
    assert call_kwargs["result"] == {"media_buy_id": "mb_1"}
    assert call_kwargs["task_id"].startswith("sync-")


@pytest.mark.asyncio
async def test_maybe_emit_echoes_token_via_extra_headers() -> None:
    """Buyer-supplied token round-trips on the ``X-AdCP-Push-Token``
    extra header. Receivers verify the token without parsing URL paths.

    Note: the spec wire field is ``payload.token``; the SDK's
    ``WebhookSender.send_mcp`` doesn't currently expose ``token`` on
    the payload directly, so we round-trip via header. F12 round-3 may
    add a payload-level wiring."""
    sender = AsyncMock()

    class _Config:
        url = "https://buyer.example.com/wh"
        token = "echo-this-back-1234567890"

    class _Params:
        push_notification_config = _Config()

    maybe_emit_sync_completion(
        sender=sender,
        enabled=True,
        method_name="create_media_buy",
        params=_Params(),
        result={"media_buy_id": "mb_1"},
    )
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)
    extra_headers = sender.send_mcp.await_args.kwargs["extra_headers"]
    assert extra_headers == {"X-AdCP-Push-Token": "echo-this-back-1234567890"}


@pytest.mark.asyncio
async def test_maybe_emit_swallows_delivery_failure(caplog) -> None:
    """Webhook delivery failure must NOT propagate — sync response
    has already returned to the buyer."""
    import logging

    sender = AsyncMock()
    sender.send_mcp.side_effect = RuntimeError("receiver down")

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    with caplog.at_level(logging.WARNING, logger="adcp.decisioning.webhook_emit"):
        maybe_emit_sync_completion(
            sender=sender,
            enabled=True,
            method_name="create_media_buy",
            params=_Params(),
            result={"media_buy_id": "mb_1"},
        )
        while _BACKGROUND_WEBHOOK_TASKS:
            await asyncio.sleep(0)
    sender.send_mcp.assert_awaited_once()
    assert any(
        "sync completion webhook" in rec.message and "failed" in rec.message
        for rec in caplog.records
    )


def test_maybe_emit_skips_silently_with_no_running_loop() -> None:
    """Sync test paths that call the gate outside an event loop get a
    silent skip — surfacing this would be strictly worse than the
    quiet best-effort behavior."""
    sender = AsyncMock()

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    # No asyncio.run wrapping this — must not raise.
    maybe_emit_sync_completion(
        sender=sender,
        enabled=True,
        method_name="create_media_buy",
        params=_Params(),
        result={"media_buy_id": "mb_1"},
    )
    sender.send_mcp.assert_not_called()


# ---- PlatformHandler integration: sync-success fires, handoff doesn't ----


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-f12-")
    yield pool
    pool.shutdown(wait=True)


def _make_request(*, with_url: bool = True, idem_suffix: str = "x") -> CreateMediaBuyRequest:
    """Build a minimal CreateMediaBuyRequest with optional push config."""
    payload: dict[str, Any] = {
        "account": {"account_id": "acct_a"},
        "brand": {"domain": "example.com"},
        "idempotency_key": f"idem_aaaa12345678{idem_suffix}",
        "start_time": "2026-05-01T00:00:00Z",
        "end_time": "2026-05-31T23:59:59Z",
    }
    if with_url:
        payload["push_notification_config"] = {
            "url": "https://buyer.example.com/wh",
            "token": "echo-back-xxxxxxxxxxxxx",
        }
    return CreateMediaBuyRequest(**payload)


class _SyncSuccessPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="hello")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return CreateMediaBuySuccessResponse(media_buy_id="mb_1", packages=[], status="active")

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"deliveries": []}


class _HandoffPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="hello")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        async def _review(task_ctx):
            return CreateMediaBuySuccessResponse(
                media_buy_id="mb_after_review", packages=[], status="active"
            )

        return ctx.handoff_to_task(_review)

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"deliveries": []}


@pytest.mark.asyncio
async def test_handler_fires_auto_emit_on_sync_success(executor) -> None:
    """End-to-end: sync mutating tool with push URL → auto-emit fires."""
    sender = AsyncMock()
    handler = PlatformHandler(
        _SyncSuccessPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
        auto_emit_completion_webhooks=True,
    )
    await handler.create_media_buy(_make_request(with_url=True), ToolContext())
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)
    sender.send_mcp.assert_awaited_once()
    assert sender.send_mcp.await_args.kwargs["task_type"] == "create_media_buy"


@pytest.mark.asyncio
async def test_handler_does_not_double_fire_on_handoff_path(executor) -> None:
    """TaskHandoff projection returns the Submitted envelope; the
    registry completion path emits its own webhook on terminal state.
    The auto-emit MUST NOT fire on this arm — buyer would receive
    duplicate webhooks."""
    sender = AsyncMock()
    handler = PlatformHandler(
        _HandoffPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
        auto_emit_completion_webhooks=True,
    )
    result = await handler.create_media_buy(_make_request(with_url=True), ToolContext())
    # Drain any background tasks (handoff fn runs in background).
    for _ in range(20):
        await asyncio.sleep(0.05)
    # The auto-emit must NOT have fired — handoff path is responsible
    # for its own webhook.
    sender.send_mcp.assert_not_called()
    # Sanity check: result is the Submitted envelope.
    assert isinstance(result, dict)
    assert result["status"] == "submitted"


@pytest.mark.asyncio
async def test_handler_opt_out_suppresses_auto_emit(executor) -> None:
    """``auto_emit_completion_webhooks=False`` → no delivery on sync
    success, even with URL set. Adopter middleware emits manually."""
    sender = AsyncMock()
    handler = PlatformHandler(
        _SyncSuccessPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
        auto_emit_completion_webhooks=False,
    )
    await handler.create_media_buy(_make_request(with_url=True), ToolContext())
    await asyncio.sleep(0.05)
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_handler_no_url_no_emit(executor) -> None:
    """Request without ``push_notification_config`` → no auto-emit."""
    sender = AsyncMock()
    handler = PlatformHandler(
        _SyncSuccessPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
        auto_emit_completion_webhooks=True,
    )
    await handler.create_media_buy(_make_request(with_url=False), ToolContext())
    await asyncio.sleep(0.05)
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_handler_default_is_enabled(executor) -> None:
    """``auto_emit_completion_webhooks`` defaults to True — adopter
    not setting the flag still gets webhook delivery."""
    sender = AsyncMock()
    handler = PlatformHandler(
        _SyncSuccessPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
        # NOT passing auto_emit_completion_webhooks — testing default.
    )
    await handler.create_media_buy(_make_request(with_url=True), ToolContext())
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)
    sender.send_mcp.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_no_sender_no_emit(executor) -> None:
    """No webhook_sender wired (the default for ``serve()``) → silent
    skip. Adopters who don't want webhooks just don't pass one."""
    handler = PlatformHandler(
        _SyncSuccessPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=None,
        auto_emit_completion_webhooks=True,
    )
    # Smoke — must not raise.
    await handler.create_media_buy(_make_request(with_url=True), ToolContext())


@pytest.mark.asyncio
async def test_handler_sync_creatives_also_fires(executor) -> None:
    """The auto-emit isn't create_media_buy-only — sync_creatives is
    also a mutating tool in the spec enum and triggers identically.

    Uses ``model_construct`` to bypass creative-payload validation
    (the F12 behavior is what's under test, not the request shape)."""
    sender = AsyncMock()
    handler = PlatformHandler(
        _SyncSuccessPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
        auto_emit_completion_webhooks=True,
    )
    from adcp.types import PushNotificationConfig

    req = SyncCreativesRequest.model_construct(
        account={"account_id": "acct_a"},  # type: ignore[arg-type]
        creatives=[],
        idempotency_key="idem_aaaa1234567890",
        push_notification_config=PushNotificationConfig.model_construct(
            url="https://buyer.example.com/wh",
            token="echo-back-xxxxxxxxxxxxx",
        ),
    )
    await handler.sync_creatives(req, ToolContext())
    while _BACKGROUND_WEBHOOK_TASKS:
        await asyncio.sleep(0)
    sender.send_mcp.assert_awaited_once()
    assert sender.send_mcp.await_args.kwargs["task_type"] == "sync_creatives"
