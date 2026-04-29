"""Tests for ADCP server DX helpers."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.server.helpers import (
    STANDARD_ERROR_CODES,
    adcp_error,
    cancel_media_buy_response,
    inject_context,
    is_terminal_status,
    resolve_account,
    resolve_account_into_context,
    valid_actions_for_status,
)


class TestAdcpError:
    """Tests for adcp_error() structured error builder."""

    def test_standard_code_auto_recovery(self) -> None:
        result = adcp_error("BUDGET_TOO_LOW", "Budget $50 is below $500")
        assert result["errors"][0]["code"] == "BUDGET_TOO_LOW"
        assert result["errors"][0]["recovery"] == "correctable"
        assert "below" in result["errors"][0]["message"]

    def test_standard_code_default_message(self) -> None:
        result = adcp_error("RATE_LIMITED")
        assert result["errors"][0]["message"] == "Too many requests"
        assert result["errors"][0]["recovery"] == "transient"

    def test_custom_code_defaults_terminal(self) -> None:
        result = adcp_error("MY_CUSTOM_ERROR", "Something broke")
        assert result["errors"][0]["recovery"] == "terminal"

    def test_recovery_override(self) -> None:
        result = adcp_error("BUDGET_TOO_LOW", recovery="transient")
        assert result["errors"][0]["recovery"] == "transient"

    def test_field_and_suggestion(self) -> None:
        result = adcp_error(
            "PRODUCT_NOT_FOUND",
            field="product_id",
            suggestion="Use get_products first",
        )
        err = result["errors"][0]
        assert err["field"] == "product_id"
        assert err["suggestion"] == "Use get_products first"

    def test_retry_after(self) -> None:
        result = adcp_error("RATE_LIMITED", retry_after=30)
        assert result["errors"][0]["retry_after"] == 30

    def test_details(self) -> None:
        result = adcp_error("BUDGET_TOO_LOW", details={"minimum": 500, "actual": 50})
        assert result["errors"][0]["details"]["minimum"] == 500

    def test_all_standard_codes_have_recovery(self) -> None:
        for code, info in STANDARD_ERROR_CODES.items():
            assert "recovery" in info, f"{code} missing recovery"
            assert info["recovery"] in ("transient", "correctable", "terminal")

    def test_importable_from_server_package(self) -> None:
        from adcp.server import adcp_error as imported

        assert callable(imported)


class TestValidActionsForStatus:
    """Tests for media buy state machine."""

    def test_active_has_pause_and_cancel(self) -> None:
        actions = valid_actions_for_status("active")
        assert "pause" in actions
        assert "cancel" in actions

    def test_paused_has_resume(self) -> None:
        actions = valid_actions_for_status("paused")
        assert "resume" in actions

    def test_terminal_statuses_empty(self) -> None:
        for status in ("completed", "rejected", "canceled"):
            assert valid_actions_for_status(status) == []

    def test_pending_start_allows_cancel(self) -> None:
        actions = valid_actions_for_status("pending_start")
        assert "cancel" in actions
        assert "update_packages" in actions

    def test_pending_creatives_allows_sync_creatives(self) -> None:
        actions = valid_actions_for_status("pending_creatives")
        assert "sync_creatives" in actions
        assert "cancel" in actions

    def test_pending_activation_is_not_recognized(self) -> None:
        # AdCP v3 renamed pending_activation to pending_creatives + pending_start.
        # Lock the rename so a copy-paste from old docs / old SDK code returns
        # an empty action list rather than silently matching a stale entry.
        from adcp.server.helpers import MEDIA_BUY_STATE_MACHINE

        assert valid_actions_for_status("pending_activation") == []
        assert "pending_activation" not in MEDIA_BUY_STATE_MACHINE

    def test_state_machine_keys_match_spec_enum(self) -> None:
        # Keys must exactly match enums/media-buy-status.json. Terminal
        # statuses are present with empty action lists. Guards against
        # future silent drift between the spec enum and the dispatcher.
        from adcp.server.helpers import MEDIA_BUY_STATE_MACHINE

        assert set(MEDIA_BUY_STATE_MACHINE) == {
            "pending_creatives",
            "pending_start",
            "active",
            "paused",
            "completed",
            "rejected",
            "canceled",
        }

    def test_unknown_status_empty(self) -> None:
        assert valid_actions_for_status("nonexistent") == []


class TestIsTerminalStatus:
    def test_terminal(self) -> None:
        assert is_terminal_status("completed") is True
        assert is_terminal_status("rejected") is True
        assert is_terminal_status("canceled") is True

    def test_non_terminal(self) -> None:
        assert is_terminal_status("active") is False
        assert is_terminal_status("paused") is False


class TestResolveAccount:
    @pytest.mark.asyncio
    async def test_no_resolver(self) -> None:
        account, error = await resolve_account({"account": {"account_id": "a1"}}, None)
        assert account is None
        assert error is None

    @pytest.mark.asyncio
    async def test_no_account_field(self) -> None:
        async def resolver(ref: dict) -> dict:
            return {"id": "resolved"}

        account, error = await resolve_account({"brief": "test"}, resolver)
        assert account is None
        assert error is None

    @pytest.mark.asyncio
    async def test_successful_resolution(self) -> None:
        async def resolver(ref: dict) -> dict:
            return {"id": ref["account_id"], "name": "Acme"}

        account, error = await resolve_account({"account": {"account_id": "a1"}}, resolver)
        assert account == {"id": "a1", "name": "Acme"}
        assert error is None

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        async def resolver(ref: dict) -> None:
            return None

        account, error = await resolve_account({"account": {"account_id": "bad"}}, resolver)
        assert account is None
        assert error is not None
        assert error["errors"][0]["code"] == "ACCOUNT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_suspended_via_account_error(self) -> None:
        from adcp.server.helpers import AccountError

        async def resolver(ref: dict) -> None:
            raise AccountError("ACCOUNT_SUSPENDED", "Account is suspended")

        account, error = await resolve_account({"account": {"account_id": "a1"}}, resolver)
        assert account is None
        assert error is not None
        assert error["errors"][0]["code"] == "ACCOUNT_SUSPENDED"

    @pytest.mark.asyncio
    async def test_payment_required_with_suggestion(self) -> None:
        from adcp.server.helpers import AccountError

        async def resolver(ref: dict) -> None:
            raise AccountError(
                "ACCOUNT_PAYMENT_REQUIRED",
                suggestion="Update payment at https://billing.example.com",
            )

        account, error = await resolve_account({"account": {"account_id": "a1"}}, resolver)
        assert error["errors"][0]["code"] == "ACCOUNT_PAYMENT_REQUIRED"
        assert "billing" in error["errors"][0]["suggestion"]


class TestResolveAccountIntoContext:
    """Tests for resolve_account_into_context — the context-populating variant."""

    @pytest.mark.asyncio
    async def test_populates_from_spec_account_shape(self) -> None:
        """Default attr is account_id — matches the spec's Account type."""
        from dataclasses import dataclass

        from adcp.server import AccountAwareToolContext

        @dataclass
        class _SpecAccount:
            account_id: str
            name: str

        async def resolver(ref: dict) -> _SpecAccount:
            return _SpecAccount(account_id=ref["account_id"], name="Acme")

        ctx = AccountAwareToolContext(caller_identity="alice")
        err = await resolve_account_into_context({"account": {"account_id": "a1"}}, ctx, resolver)

        assert err is None
        assert ctx.account_id == "a1"
        assert ctx.account is not None
        assert ctx.account.name == "Acme"

    @pytest.mark.asyncio
    async def test_not_found_returns_error_and_leaves_context_untouched(self) -> None:
        from adcp.server import AccountAwareToolContext

        async def resolver(ref: dict) -> None:
            return None

        ctx = AccountAwareToolContext(caller_identity="alice")
        err = await resolve_account_into_context({"account": {"account_id": "bad"}}, ctx, resolver)

        assert err is not None
        assert err["errors"][0]["code"] == "ACCOUNT_NOT_FOUND"
        assert ctx.account_id is None
        assert ctx.account is None

    @pytest.mark.asyncio
    async def test_no_account_field_is_noop(self) -> None:
        from adcp.server import AccountAwareToolContext

        async def resolver(ref: dict) -> dict:
            return {"id": "x"}

        ctx = AccountAwareToolContext(caller_identity="alice")
        err = await resolve_account_into_context({"brief": "test"}, ctx, resolver)

        assert err is None
        assert ctx.account_id is None

    @pytest.mark.asyncio
    async def test_plain_tool_context_warns_on_silent_skip(self) -> None:
        """Passing a plain ToolContext (not Account-aware) MUST emit a
        UserWarning — silent-skip would break the multi-tenant scope
        contract by scoping downstream caches on ``None``."""
        from dataclasses import dataclass

        from adcp.server import ToolContext

        @dataclass
        class _Account:
            account_id: str

        async def resolver(ref: dict) -> _Account:
            return _Account(account_id="a1")

        ctx = ToolContext(caller_identity="alice")
        with pytest.warns(UserWarning, match="AccountAwareToolContext"):
            err = await resolve_account_into_context(
                {"account": {"account_id": "a1"}},
                ctx,  # type: ignore[arg-type]
                resolver,
            )

        assert err is None
        assert not hasattr(ctx, "account_id")

    @pytest.mark.asyncio
    async def test_missing_id_attr_raises(self) -> None:
        """Wrong account_id_attr must raise rather than silently setting
        None — silent-None scopes downstream keys to None, masking bugs."""
        from dataclasses import dataclass

        from adcp.server import AccountAwareToolContext

        @dataclass
        class _Account:
            name: str  # deliberately no id field

        async def resolver(ref: dict) -> _Account:
            return _Account(name="Acme")

        ctx = AccountAwareToolContext()
        with pytest.raises(ValueError, match="account_id_attr"):
            await resolve_account_into_context({"account": {"account_id": "a1"}}, ctx, resolver)

    @pytest.mark.asyncio
    async def test_resolver_runtime_error_propagates(self) -> None:
        """Non-AccountError exceptions propagate — resolver bugs must not be
        silently converted to ACCOUNT_NOT_FOUND."""
        from adcp.server import AccountAwareToolContext

        async def resolver(ref: dict) -> None:
            raise RuntimeError("DB outage")

        ctx = AccountAwareToolContext()
        with pytest.raises(RuntimeError, match="DB outage"):
            await resolve_account_into_context({"account": {"account_id": "a1"}}, ctx, resolver)

    @pytest.mark.asyncio
    async def test_custom_id_attr(self) -> None:
        from dataclasses import dataclass

        from adcp.server import AccountAwareToolContext

        @dataclass
        class _Account:
            account_pk: str

        async def resolver(ref: dict) -> _Account:
            return _Account(account_pk="pk-123")

        ctx = AccountAwareToolContext()
        err = await resolve_account_into_context(
            {"account": {"account_id": "a1"}},
            ctx,
            resolver,
            account_id_attr="account_pk",
        )

        assert err is None
        assert ctx.account_id == "pk-123"


class TestInjectContext:
    def test_injects_context(self) -> None:
        params = {"brief": "test", "context": {"correlation_id": "abc"}}
        response: dict[str, Any] = {"products": []}
        inject_context(params, response)
        assert response["context"] == {"correlation_id": "abc"}

    def test_no_context_no_injection(self) -> None:
        params = {"brief": "test"}
        response: dict[str, Any] = {"products": []}
        inject_context(params, response)
        assert "context" not in response

    def test_does_not_overwrite_existing(self) -> None:
        params = {"context": {"new": True}}
        response: dict[str, Any] = {"context": {"existing": True}}
        inject_context(params, response)
        assert response["context"] == {"existing": True}


class TestCancelMediaBuyResponse:
    def test_basic_cancellation(self) -> None:
        resp = cancel_media_buy_response("mb_123", "buyer")
        assert resp["media_buy_id"] == "mb_123"
        assert resp["status"] == "canceled"
        assert resp["canceled_by"] == "buyer"
        assert resp["valid_actions"] == []
        assert "canceled_at" in resp

    def test_with_reason(self) -> None:
        resp = cancel_media_buy_response("mb_123", "seller", reason="Policy violation")
        assert resp["reason"] == "Policy violation"

    def test_auto_timestamp(self) -> None:
        resp = cancel_media_buy_response("mb_123", "buyer")
        assert resp["canceled_at"].endswith("+00:00") or "Z" in resp["canceled_at"]

    def test_custom_timestamp(self) -> None:
        resp = cancel_media_buy_response("mb_123", "buyer", canceled_at="2026-01-01T00:00:00Z")
        assert resp["canceled_at"] == "2026-01-01T00:00:00Z"

    def test_invalid_canceled_by_raises(self) -> None:
        with pytest.raises(ValueError, match="canceled_by must be"):
            cancel_media_buy_response("mb_123", "system")


class TestMediaBuyResponseAutoActions:
    """Test that response builders auto-populate valid_actions."""

    def test_media_buy_response_auto_actions(self) -> None:
        from adcp.server.responses import media_buy_response

        resp = media_buy_response("mb_1", [], status="active")
        assert "valid_actions" in resp
        assert "pause" in resp["valid_actions"]
        assert "cancel" in resp["valid_actions"]

    def test_media_buy_response_terminal_empty_actions(self) -> None:
        from adcp.server.responses import media_buy_response

        resp = media_buy_response("mb_1", [], status="completed")
        assert resp["valid_actions"] == []

    def test_media_buy_response_auto_revision(self) -> None:
        from adcp.server.responses import media_buy_response

        resp = media_buy_response("mb_1", [])
        assert resp["revision"] == 1

    def test_update_response_auto_actions(self) -> None:
        from adcp.server.responses import update_media_buy_response

        resp = update_media_buy_response("mb_1", status="paused")
        assert "resume" in resp["valid_actions"]

    def test_explicit_actions_override(self) -> None:
        from adcp.server.responses import media_buy_response

        resp = media_buy_response("mb_1", [], status="active", valid_actions=["cancel"])
        assert resp["valid_actions"] == ["cancel"]
