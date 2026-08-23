"""Sync-completion compatibility and async task webhook coverage.

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
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import WebhookSigning
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.task_registry import InMemoryTaskRegistry
from adcp.decisioning.webhook_emit import (
    _BACKGROUND_WEBHOOK_TASKS,
    SPEC_WEBHOOK_TASK_TYPES,
    _extract_push_notification_url_and_token,
    emit_terminal_completion_webhook,
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
    from adcp._version import _read_packaged_version
    from adcp.validation.version import resolve_bundle_key

    bundle_key = resolve_bundle_key(_read_packaged_version())
    schema_path = (
        Path(__file__).parent.parent / "schemas" / "cache" / bundle_key / "enums" / "task-type.json"
    )
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
async def test_maybe_emit_warns_when_sender_none_but_buyer_registered_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``sender=None`` while buyer DID register
    ``push_notification_config.url`` → log a WARNING. Adopters often
    ship without wiring ``webhook_sender`` and only discover the
    misconfig when buyers complain about missing notifications. The
    warning surfaces this on first call. Regression for Emma
    sales-direct backend test (verdict 2/10) — the prior silent-skip
    branch hid the gap entirely."""

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    with pytest.warns(DeprecationWarning, match="ignored under AdCP 3.2"):
        maybe_emit_sync_completion(
            sender=None,
            enabled=True,
            method_name="create_media_buy",
            params=_Params(),
            result={"media_buy_id": "mb_1"},
        )
    assert caplog.records == []


@pytest.mark.asyncio
async def test_maybe_emit_skips_silently_when_sender_none_and_no_url() -> None:
    """``sender=None`` AND no ``push_notification_config.url`` → silent
    skip. Buyers who don't register webhooks aren't a misconfig — no
    point warning."""

    class _Bare:
        pass

    # Smoke — must not raise, must not warn (no caplog capture).
    maybe_emit_sync_completion(
        sender=None,
        enabled=True,
        method_name="create_media_buy",
        params=_Bare(),
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

    sender = AsyncMock()

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    with pytest.warns(DeprecationWarning, match="ignored under AdCP 3.2"):
        maybe_emit_sync_completion(
            sender=sender,
            enabled=True,
            method_name="custom_adopter_tool",  # Not in spec enum
            params=_Params(),
            result={"x": 1},
        )
    await asyncio.sleep(0)
    sender.send_mcp.assert_not_called()
    assert caplog.records == []


@pytest.mark.asyncio
async def test_maybe_emit_fires_when_url_set() -> None:
    """Even the retired opt-in stays silent for inline terminal results."""
    sender = AsyncMock()

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    with pytest.warns(DeprecationWarning, match="ignored under AdCP 3.2"):
        maybe_emit_sync_completion(
            sender=sender,
            enabled=True,
            method_name="create_media_buy",
            params=_Params(),
            result={"media_buy_id": "mb_1"},
        )
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_emit_echoes_token_via_payload_field() -> None:
    """Buyer-supplied ``push_notification_config.token`` round-trips
    on the payload's ``token`` field per spec
    (``schemas/cache/core/push_notification_config.json``: "Echoed
    back in webhook payload to validate request authenticity").
    Cross-language wire-parity with the JS reference impl
    (``buildTaskWebhookPayload`` in ``from-platform.ts``)."""
    sender = AsyncMock()

    class _Config:
        url = "https://buyer.example.com/wh"
        token = "echo-this-back-1234567890"

    class _Params:
        push_notification_config = _Config()

    with pytest.warns(DeprecationWarning, match="ignored under AdCP 3.2"):
        maybe_emit_sync_completion(
            sender=sender,
            enabled=True,
            method_name="create_media_buy",
            params=_Params(),
            result={"media_buy_id": "mb_1"},
        )
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_emit_swallows_delivery_failure(caplog) -> None:
    """Webhook delivery failure must NOT propagate — sync response
    has already returned to the buyer."""

    sender = AsyncMock()
    sender.send_mcp.side_effect = RuntimeError("receiver down")

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    with pytest.warns(DeprecationWarning, match="ignored under AdCP 3.2"):
        maybe_emit_sync_completion(
            sender=sender,
            enabled=True,
            method_name="create_media_buy",
            params=_Params(),
            result={"media_buy_id": "mb_1"},
        )
    sender.send_mcp.assert_not_called()
    assert caplog.records == []


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


# ---- emit_terminal_completion_webhook spec-enum gate ----

_SILENTLY_DROPPED = "silently dropped"


@pytest.mark.asyncio
async def test_terminal_emit_skips_non_spec_task_type_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SDK-internal, non-spec task types (e.g. ``finalize_proposal``, an
    interception of ``get_products`` in ``proposal_dispatch.py``) flow
    through ``_project_handoff`` like any async task. They legitimately
    have no webhook target wired, so the spec gate must skip them
    SILENTLY — no emission AND no "silently dropped" misconfig warning,
    even when the buyer registered a push config. Regression: #931 — the
    target-None warning fired on every async finalize on a
    correctly-configured server."""

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    target = AsyncMock()

    with caplog.at_level("WARNING", logger="adcp.decisioning.webhook_emit"):
        await emit_terminal_completion_webhook(
            target=None,  # no target wired — the finalize_proposal reality
            enabled=True,
            method_name="finalize_proposal",  # NOT in SPEC_WEBHOOK_TASK_TYPES
            params=_Params(),
            status="completed",
            task_id="task_finalize_1",
            result={"proposal_id": "prop_1"},
        )

    # No emission attempted.
    target.send_mcp.assert_not_awaited()
    # And crucially: the spurious misconfig warning is ABSENT.
    messages = [r.message for r in caplog.records]
    assert not any(_SILENTLY_DROPPED in m for m in messages), (
        f"non-spec task type must skip silently, but a 'silently dropped' "
        f"warning was logged: {messages}"
    )


@pytest.mark.asyncio
async def test_terminal_emit_fires_for_spec_task_type_with_target() -> None:
    """A spec-eligible task type with a real target still emits the
    terminal completion webhook unchanged — the gate only short-circuits
    non-spec types."""

    class _Config:
        url = "https://buyer.example.com/wh"
        token = "echo-back-token"
        operation_id = "op-terminal-emit"

    class _Params:
        push_notification_config = _Config()

    target = AsyncMock()

    await emit_terminal_completion_webhook(
        target=target,
        enabled=True,
        method_name="create_media_buy",  # in SPEC_WEBHOOK_TASK_TYPES
        params=_Params(),
        status="completed",
        task_id="task_mb_1",
        result={"media_buy_id": "mb_1"},
    )

    target.send_mcp.assert_awaited_once()
    call_kwargs = target.send_mcp.await_args.kwargs
    assert call_kwargs["url"] == "https://buyer.example.com/wh"
    assert call_kwargs["task_type"] == "create_media_buy"
    assert call_kwargs["status"] == "completed"
    assert call_kwargs["task_id"] == "task_mb_1"
    assert call_kwargs["result"] == {"media_buy_id": "mb_1"}
    assert call_kwargs["token"] == "echo-back-token"


@pytest.mark.asyncio
async def test_terminal_emit_warns_for_spec_task_type_with_target_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A SPEC-eligible task type with a push config registered but
    ``target=None`` is a genuine misconfig — the buyer's terminal
    notification is being dropped. That warning MUST still fire; the
    spec gate only suppresses the warning for non-spec types."""

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    with caplog.at_level("WARNING", logger="adcp.decisioning.webhook_emit"):
        await emit_terminal_completion_webhook(
            target=None,  # genuine misconfig for a spec task type
            enabled=True,
            method_name="create_media_buy",  # in SPEC_WEBHOOK_TASK_TYPES
            params=_Params(),
            status="completed",
            task_id="task_mb_2",
            result={"media_buy_id": "mb_2"},
        )

    messages = [r.message for r in caplog.records]
    assert any(
        "neither webhook_sender nor webhook_supervisor" in m
        and _SILENTLY_DROPPED in m
        and "buyer.example.com/wh" in m
        for m in messages
    ), f"expected target-None misconfig warning citing the buyer URL; got {messages}"


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
            "operation_id": f"op_create_media_buy_{idem_suffix}",
            "token": "echo-back-xxxxxxxxxxxxx",
        }
    return CreateMediaBuyRequest(**payload)


class _SyncSuccessPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="hello")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return CreateMediaBuySuccessResponse(
            media_buy_id="mb_1",
            confirmed_at="2026-05-01T00:00:00Z",
            revision=1,
            packages=[],
            status="active",
        )

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "revision": 2, "status": "active"}

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
                media_buy_id="mb_after_review",
                confirmed_at="2026-05-01T00:00:00Z",
                revision=1,
                packages=[],
                status="active",
            )

        return ctx.handoff_to_task(_review)

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "revision": 2, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"deliveries": []}


@pytest.mark.asyncio
async def test_handler_explicit_compatibility_opt_in_emits_on_sync_success(executor) -> None:
    """The retired opt-in warns but cannot violate sync-channel silence."""
    sender = AsyncMock()
    with pytest.warns(DeprecationWarning, match="deprecated and ignored"):
        handler = PlatformHandler(
            _SyncSuccessPlatform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            webhook_sender=sender,
            auto_emit_completion_webhooks=True,
        )
    await handler.create_media_buy(_make_request(with_url=True), ToolContext())
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_handler_rejects_sdk_managed_push_even_with_sender(executor) -> None:
    """A transport alone cannot satisfy the beta.5 durable-outbox contract."""
    sender = AsyncMock()
    registry = InMemoryTaskRegistry()
    handler = PlatformHandler(
        _HandoffPlatform(),
        executor=executor,
        registry=registry,
        webhook_sender=sender,
    )

    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(_make_request(with_url=True), ToolContext())

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.field == "push_notification_config"
    assert registry._records == {}
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_handler_rejects_push_handoff_without_webhook_transport(executor) -> None:
    """A submitted task must not promise push delivery with no transport."""
    registry = InMemoryTaskRegistry()
    handler = PlatformHandler(
        _HandoffPlatform(),
        executor=executor,
        registry=registry,
    )

    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(_make_request(with_url=True), ToolContext())

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.field == "push_notification_config"
    assert registry._records == {}


@pytest.mark.asyncio
async def test_handler_rejects_push_handoff_without_operation_id(executor) -> None:
    """A schema-optional legacy field is runtime-required before handoff."""
    registry = InMemoryTaskRegistry()
    sender = AsyncMock()
    handler = PlatformHandler(
        _HandoffPlatform(),
        executor=executor,
        registry=registry,
        webhook_sender=sender,
    )
    request = _make_request(with_url=True)
    assert request.push_notification_config is not None
    request.push_notification_config.operation_id = None

    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(request, ToolContext())

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.field == "push_notification_config.operation_id"
    assert registry._records == {}
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_handler_task_webhook_opt_out_suppresses_framework_delivery(executor) -> None:
    """A declared external outbox can own terminal task publication."""
    sender = AsyncMock()
    registry = InMemoryTaskRegistry()

    class _ExternalHandoffPlatform(_HandoffPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            webhook_signing=WebhookSigning(
                supported=True,
                delivery_retry_horizon_seconds=86400,
            ),
            webhook_signing_managed_externally=True,
        )

    handler = PlatformHandler(
        _ExternalHandoffPlatform(),
        executor=executor,
        registry=registry,
        auto_emit_task_webhooks=False,
    )

    result = await handler.create_media_buy(_make_request(with_url=True), ToolContext())
    for _ in range(40):
        record = await registry.get(result["task_id"])
        if record is not None and record["state"] == "completed":
            break
        await asyncio.sleep(0.02)

    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_handler_rejects_self_asserted_registry_outbox(executor) -> None:
    """Marker attributes cannot impersonate the SDK's atomic PostgreSQL pair."""

    class _OutboxSender:
        signs_with_rfc9421 = True

    class _AtomicOutbox:
        delivery_state_is_durable = True
        supports_atomic_task_outbox = True
        delivery_retry_horizon_seconds = 86_400
        _sender = _OutboxSender()

    class _AtomicRegistry(InMemoryTaskRegistry):
        task_webhook_outbox = _AtomicOutbox()

        def __init__(self) -> None:
            super().__init__()
            self.issue_extra: dict[str, Any] = {}

        async def issue(self, *, account_id, task_type, request_context=None, **extra):
            self.issue_extra = extra
            return await super().issue(
                account_id=account_id,
                task_type=task_type,
                request_context=request_context,
            )

    class _SdkOutboxPlatform(_HandoffPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            webhook_signing=WebhookSigning(
                supported=True,
                delivery_retry_horizon_seconds=86_400,
            ),
        )

    registry = _AtomicRegistry()
    handler = PlatformHandler(
        _SdkOutboxPlatform(),
        executor=executor,
        registry=registry,
    )

    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(_make_request(with_url=True), ToolContext())

    assert exc_info.value.code == "INVALID_REQUEST"
    assert registry.issue_extra == {}


@pytest.mark.asyncio
async def test_scoped_capabilities_can_admit_external_push_owner(executor) -> None:
    """Push admission uses the tenant-scoped capability set, not static defaults."""
    registry = InMemoryTaskRegistry()

    class _ScopedExternalPlatform(_HandoffPlatform):
        def get_adcp_capabilities_for_request(self, params=None, context=None):
            assert context is not None
            assert context.tenant_id == "ready-tenant"
            return DecisioningCapabilities(
                specialisms=["sales-non-guaranteed"],
                webhook_signing=WebhookSigning(
                    supported=True,
                    delivery_retry_horizon_seconds=86400,
                ),
                webhook_signing_managed_externally=True,
            )

    handler = PlatformHandler(
        _ScopedExternalPlatform(),
        executor=executor,
        registry=registry,
        auto_emit_task_webhooks=False,
    )

    result = await handler.create_media_buy(
        _make_request(with_url=True),
        ToolContext(tenant_id="ready-tenant"),
    )
    assert result["status"] == "submitted"


@pytest.mark.asyncio
async def test_scoped_capabilities_can_deny_static_external_push_owner(executor) -> None:
    """A static owner declaration cannot override the selected tenant's denial."""
    registry = InMemoryTaskRegistry()

    class _ScopedNoPublisherPlatform(_HandoffPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            webhook_signing=WebhookSigning(
                supported=True,
                delivery_retry_horizon_seconds=86400,
            ),
            webhook_signing_managed_externally=True,
        )

        def get_adcp_capabilities_for_request(self, params=None, context=None):
            assert context is not None
            assert context.tenant_id == "polling-only-tenant"
            return DecisioningCapabilities(specialisms=["sales-non-guaranteed"])

    handler = PlatformHandler(
        _ScopedNoPublisherPlatform(),
        executor=executor,
        registry=registry,
        auto_emit_task_webhooks=False,
    )

    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(
            _make_request(with_url=True),
            ToolContext(tenant_id="polling-only-tenant"),
        )

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.field == "push_notification_config"
    assert registry._records == {}


@pytest.mark.asyncio
async def test_handler_rejects_opt_out_with_wired_sdk_sender(executor) -> None:
    """Disabling auto-emission cannot silently strand promised callbacks."""
    sender = AsyncMock()
    registry = InMemoryTaskRegistry()
    handler = PlatformHandler(
        _HandoffPlatform(),
        executor=executor,
        registry=registry,
        webhook_sender=sender,
        auto_emit_task_webhooks=False,
    )

    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(_make_request(with_url=True), ToolContext())

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.field == "push_notification_config"
    assert registry._records == {}
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_handler_explicit_false_suppresses_sync_compatibility_emit(executor) -> None:
    """An explicit ``False`` suppresses legacy sync delivery."""
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
async def test_handler_default_does_not_emit_for_sync_terminal_response(executor) -> None:
    """A synchronous terminal response emits no webhook by default."""
    sender = AsyncMock()
    handler = PlatformHandler(
        _SyncSuccessPlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
        # NOT passing auto_emit_completion_webhooks — testing default.
    )
    await handler.create_media_buy(_make_request(with_url=True), ToolContext())
    await asyncio.sleep(0.05)
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_handler_no_sender_no_emit(executor) -> None:
    """Explicit compatibility mode without a sender cannot deliver."""
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
    """Sync-channel silence also applies to ``sync_creatives``.

    Uses ``model_construct`` to bypass creative-payload validation
    (the F12 behavior is what's under test, not the request shape)."""
    sender = AsyncMock()
    with pytest.warns(DeprecationWarning, match="deprecated and ignored"):
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
    sender.send_mcp.assert_not_called()


# ---- Round-2 expert review: non-blocking + concurrency + adopter-loose-shape ----


@pytest.mark.asyncio
async def test_handler_returns_before_webhook_delivers(executor) -> None:
    """A buyer callback cannot delay an inline terminal response."""

    sender = AsyncMock()
    sender.send_mcp.side_effect = AssertionError("sync webhook must stay silent")

    with pytest.warns(DeprecationWarning, match="deprecated and ignored"):
        handler = PlatformHandler(
            _SyncSuccessPlatform(),
            executor=executor,
            registry=InMemoryTaskRegistry(),
            webhook_sender=sender,
            auto_emit_completion_webhooks=True,
        )

    # Sync response returns even though the webhook is still blocked.
    response = await handler.create_media_buy(
        _make_request(with_url=True, idem_suffix="nb"), ToolContext()
    )
    assert response.media_buy_id == "mb_1"
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_emissions_dont_corrupt_strong_ref_set(executor) -> None:
    """Repeated retired calls never schedule synthetic webhook tasks."""

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    sender = AsyncMock()
    sender.send_mcp.return_value = None

    with pytest.warns(DeprecationWarning):
        for _ in range(100):
            maybe_emit_sync_completion(
                sender=sender,
                enabled=True,
                method_name="create_media_buy",
                params=_Params(),
                result={"media_buy_id": "mb"},
            )
    assert sender.send_mcp.await_count == 0
    assert len(_BACKGROUND_WEBHOOK_TASKS) == 0


@pytest.mark.asyncio
async def test_handler_rejects_loose_submitted_shape(executor) -> None:
    """A loose sync shape cannot manufacture task or webhook authority."""

    class _LooseSubmittedPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="hello")

        def get_products(self, req, ctx):
            return {"products": []}

        def create_media_buy(self, req, ctx):
            # Adopter returns a dict with "status": "submitted" PLUS
            # extra fields — NOT a TaskHandoff projection.
            return {
                "task_id": "mb_xyz",
                "status": "submitted",
                "media_buy_id": "mb_xyz",
                "queued_at": "2026-04-30T23:00:00Z",
            }

        def update_media_buy(self, media_buy_id, patch, ctx):
            return {}

        def sync_creatives(self, req, ctx):
            return {}

        def get_media_buy_delivery(self, req, ctx):
            return {}

    sender = AsyncMock()
    registry = InMemoryTaskRegistry()
    with pytest.warns(DeprecationWarning, match="deprecated and ignored"):
        handler = PlatformHandler(
            _LooseSubmittedPlatform(),
            executor=executor,
            registry=registry,
            webhook_sender=sender,
            auto_emit_completion_webhooks=True,
        )
    with pytest.raises(AdcpError, match="hand-rolled 'submitted'"):
        await handler.create_media_buy(
            _make_request(with_url=True, idem_suffix="ls"),
            ToolContext(),
        )

    assert registry._records == {}
    sender.send_mcp.assert_not_called()


@pytest.mark.asyncio
async def test_gate_swallows_unexpected_exceptions(caplog) -> None:
    """Round-2 expert review (P0): the gate's body MUST never propagate
    an exception to the handler shim. Test by passing a sender whose
    method-resolution raises (simulating a broken duck-typed sender).
    The handler returns successfully and the gate logs the failure."""

    # Sender that raises on attribute access — simulates a misconfigured
    # duck-typed object that passes the ``sender is None`` check but
    # explodes inside ``send_mcp`` lookup.
    class _ExplodingSender:
        @property
        def send_mcp(self):
            raise RuntimeError("synthetic sender access failure")

    class _Config:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Config()

    with pytest.warns(DeprecationWarning, match="ignored under AdCP 3.2"):
        maybe_emit_sync_completion(
            sender=_ExplodingSender(),  # type: ignore[arg-type]
            enabled=True,
            method_name="create_media_buy",
            params=_Params(),
            result={"media_buy_id": "mb_1"},
        )
    assert caplog.records == []
