"""Stage 4b2 tests: pre-adapter validation against the legacy schema.

When the buyer's claimed version routes through the legacy adapter,
``create_tool_caller`` validates the input against that legacy version's
schema *before* the adapter runs. Structural errors surface with the
legacy schema's field paths — far easier for the buyer than a v3 field
path after a confusing translation.

These tests exercise the strict + warn modes, plus the no-validation
fallthrough.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.validation.client_hooks import ValidationHookConfig


class _SyncCreativesHandler(ADCPHandler[Any]):
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def sync_creatives(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.received.append(params)
        return {"creatives": []}


# ---------------------------------------------------------------------------
# Strict mode — v2.5 schema violations raise INVALID_REQUEST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_rejects_v2_5_payload_with_v2_5_field_path() -> None:
    """A v2.5 buyer's payload that fails v2.5 validation is rejected
    *before* the adapter runs. The error reports the v2.5 schema's
    field path — far easier for the buyer to act on than a v3 field
    path after a confusing translation.

    v2.5 ``sync_creatives`` types ``creatives`` as ``array``; sending
    an int triggers a type error from the v2.5 validator.
    """
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(
        handler,
        "sync_creatives",
        validation=ValidationHookConfig(requests="strict"),
    )

    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({"adcp_version": "2.5", "creatives": 42})

    err = exc_info.value.errors[0]
    # ``build_adcp_validation_error_payload`` returns
    # ``VALIDATION_ERROR`` (matches the post-adapter contract).
    assert err.code == "VALIDATION_ERROR"
    # The v2.5 schema reported the type error at /creatives.
    assert "/creatives" in err.message
    # Wire version preserved in details so adopter telemetry can
    # attribute the failure to a legacy claim.
    assert err.details is not None
    assert err.details.get("claimed_version") == "2.5"
    # Handler never ran.
    assert handler.received == []


@pytest.mark.asyncio
async def test_strict_rejects_payload_valid_in_v2_5_but_missing_v3_required() -> None:
    """A v2.5 buyer's payload that's valid in v2.5 but missing a
    v3-required field passes pre-adapter validation, gets translated,
    and is then rejected by post-adapter v3 validation. The error
    surfaces the v3 schema's field path so the buyer knows what to
    supply.
    """
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(
        handler,
        "sync_creatives",
        validation=ValidationHookConfig(requests="strict"),
    )

    # v2.5 only requires ``creatives``; this is structurally fine.
    # v3 also requires ``idempotency_key`` + ``account``, so the
    # post-adapter v3 check fails.
    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({"adcp_version": "2.5", "creatives": []})

    err = exc_info.value.errors[0]
    assert err.code == "VALIDATION_ERROR"
    # v3 schema reported the missing-field error.
    assert "idempotency_key" in err.message or "account" in err.message
    assert handler.received == []


# ---------------------------------------------------------------------------
# Warn mode — validation failures log but don't block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warn_logs_v2_5_validation_failure_and_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In ``warn`` mode, a v2.5 schema violation logs but lets the
    adapter try anyway. Matches the existing post-adapter warn semantics
    so adopters have a consistent escalation path strict ← warn ← off."""
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(
        handler,
        "sync_creatives",
        validation=ValidationHookConfig(requests="warn"),
    )

    with caplog.at_level(logging.WARNING):
        await caller({"adcp_version": "2.5", "creatives": 42})

    # Warning logged about pre-adapter validation failure.
    assert any("pre-adapter 2.5" in rec.message.lower() for rec in caplog.records), [
        rec.message for rec in caplog.records
    ]


# ---------------------------------------------------------------------------
# Off — no validation runs (default behaviour preserved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_validation_config_skips_pre_adapter_check() -> None:
    """The zero-overhead path: ``validation=None`` (default) bypasses
    both pre- and post-adapter validation. Stage 4b2 must not pull
    schema-loading into the hot path for adopters who haven't opted in.
    """
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(handler, "sync_creatives")  # no validation=

    # Garbage payload — would fail v2.5 validation if it ran. The
    # adapter still gets a chance because validation is off; the v2.5
    # sync_creatives adapter is permissive when ``creatives`` isn't a
    # list (returns args unchanged), so the handler sees the original.
    await caller({"adcp_version": "2.5", "creatives": 42})

    assert len(handler.received) == 1
    assert handler.received[0]["creatives"] == 42


# ---------------------------------------------------------------------------
# Adapter-output validation against v3 still runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_5_payload_valid_against_v2_5_but_translator_produces_invalid_v3() -> None:
    """Belt + braces: even if pre-adapter validation passes, the
    *post*-adapter v3 validation catches translator bugs (the contract
    Stage 4 already ships). This test pins that the v2.5 pre-check
    doesn't replace the v3 post-check."""

    from adcp.compat.legacy import AdapterPair, _reset_registry_for_tests, register_adapter

    # Register a rogue adapter that returns a v3-invalid dict so the
    # post-adapter validator should catch it.
    def rogue(payload: dict[str, Any]) -> dict[str, Any]:
        return {"creatives": "not-a-list-after-adapt"}

    _reset_registry_for_tests()
    try:
        register_adapter(
            "2.5",
            AdapterPair(tool_name="sync_creatives", adapt_request=rogue),
        )

        handler = _SyncCreativesHandler()
        # ``strict`` so a v3 validation failure raises; the v2.5
        # pre-check needs to pass (empty creatives is fine in v2.5).
        caller = create_tool_caller(
            handler,
            "sync_creatives",
            validation=ValidationHookConfig(requests="strict"),
        )

        with pytest.raises(ADCPTaskError) as exc_info:
            await caller({"adcp_version": "2.5", "creatives": []})

        # The v3 post-adapter validator caught the bad output.
        err = exc_info.value.errors[0]
        assert err.code in ("INVALID_REQUEST", "VALIDATION_ERROR")
        assert handler.received == []
    finally:
        _reset_registry_for_tests()
