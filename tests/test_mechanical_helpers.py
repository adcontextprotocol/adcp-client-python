"""Tests for state-machine helpers, typed exception classes, and ref_account_id."""

from __future__ import annotations

import pytest

from adcp.decisioning import (
    CREATIVE_ASSET_TRANSITIONS,
    MEDIA_BUY_TRANSITIONS,
    AccountNotFoundError,
    AdcpError,
    AuthRequiredError,
    BillingNotPermittedForAgentError,
    MediaBuyNotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ServiceUnavailableError,
    UnsupportedFeatureError,
    ValidationError,
    assert_creative_transition,
    assert_media_buy_transition,
    ref_account_id,
)
from adcp.types import (
    AccountReference,
    AccountReferenceById,
    AccountReferenceByNaturalKey,
    BrandReference,
)

# ---------------------------------------------------------------------------
# Media-buy state-machine helper
# ---------------------------------------------------------------------------


class TestMediaBuyTransitions:
    def test_table_terminal_states_have_no_outgoing_edges(self) -> None:
        assert MEDIA_BUY_TRANSITIONS["completed"] == frozenset()
        assert MEDIA_BUY_TRANSITIONS["canceled"] == frozenset()
        assert MEDIA_BUY_TRANSITIONS["rejected"] == frozenset()

    def test_pending_creatives_to_pending_start_legal(self) -> None:
        assert_media_buy_transition("pending_creatives", "pending_start")

    def test_pending_start_to_active_legal(self) -> None:
        assert_media_buy_transition("pending_start", "active")

    def test_active_to_paused_legal(self) -> None:
        assert_media_buy_transition("active", "paused")

    def test_paused_to_active_legal(self) -> None:
        assert_media_buy_transition("paused", "active")

    def test_active_to_completed_legal(self) -> None:
        assert_media_buy_transition("active", "completed")

    def test_active_to_pending_creatives_illegal(self) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_media_buy_transition("active", "pending_creatives")
        err = exc_info.value
        assert err.code == "INVALID_STATE"
        assert err.recovery == "correctable"
        assert err.field == "status"
        assert err.details["from_state"] == "active"
        assert err.details["to_state"] == "pending_creatives"

    def test_terminal_completed_raises(self) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_media_buy_transition("completed", "active")
        assert exc_info.value.code == "INVALID_STATE"
        assert "terminal" in str(exc_info.value).lower()

    def test_terminal_canceled_raises(self) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_media_buy_transition("canceled", "active")
        assert exc_info.value.code == "INVALID_STATE"

    def test_terminal_rejected_raises(self) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_media_buy_transition("rejected", "active")
        assert exc_info.value.code == "INVALID_STATE"

    def test_unknown_from_state_raises(self) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_media_buy_transition("nonsense", "active")
        assert exc_info.value.code == "INVALID_STATE"
        assert "Unknown" in str(exc_info.value)

    def test_media_buy_id_propagated_to_details(self) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_media_buy_transition("completed", "active", media_buy_id="mb_xyz")
        assert exc_info.value.details["media_buy_id"] == "mb_xyz"


# ---------------------------------------------------------------------------
# Creative state-machine helper
# ---------------------------------------------------------------------------


class TestCreativeTransitions:
    def test_table_archived_is_terminal(self) -> None:
        assert CREATIVE_ASSET_TRANSITIONS["archived"] == frozenset()

    def test_processing_to_pending_review_legal(self) -> None:
        assert_creative_transition("processing", "pending_review")

    def test_processing_to_approved_legal(self) -> None:
        # Auto-approval short-circuits review.
        assert_creative_transition("processing", "approved")

    def test_pending_review_to_approved_legal(self) -> None:
        assert_creative_transition("pending_review", "approved")

    def test_pending_review_to_rejected_legal(self) -> None:
        assert_creative_transition("pending_review", "rejected")

    def test_approved_to_archived_legal(self) -> None:
        assert_creative_transition("approved", "archived")

    def test_archived_terminal_raises(self) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_creative_transition("archived", "approved")
        assert exc_info.value.code == "INVALID_STATE"
        assert exc_info.value.recovery == "correctable"

    def test_approved_to_processing_illegal(self) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_creative_transition("approved", "processing")
        assert exc_info.value.code == "INVALID_STATE"

    def test_creative_id_propagated_to_details(self) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_creative_transition("archived", "approved", creative_id="cr_abc")
        assert exc_info.value.details["creative_id"] == "cr_abc"


# ---------------------------------------------------------------------------
# Typed exception classes
# ---------------------------------------------------------------------------


class TestPermissionDeniedError:
    def test_subclass_of_adcp_error(self) -> None:
        assert issubclass(PermissionDeniedError, AdcpError)

    def test_default_code_and_recovery(self) -> None:
        err = PermissionDeniedError()
        assert err.code == "PERMISSION_DENIED"
        assert err.recovery == "correctable"

    def test_scope_and_status_in_details(self) -> None:
        err = PermissionDeniedError(scope="agent", status="sandbox_only")
        assert err.details["scope"] == "agent"
        assert err.details["status"] == "sandbox_only"

    def test_billing_scope(self) -> None:
        err = PermissionDeniedError(scope="billing")
        assert err.details["scope"] == "billing"

    def test_message_override(self) -> None:
        err = PermissionDeniedError(message="custom denial reason")
        assert "custom denial reason" in str(err)

    def test_extra_details_forwarded(self) -> None:
        err = PermissionDeniedError(scope="agent", reason="sandbox_only")
        assert err.details["reason"] == "sandbox_only"

    def test_field_forwarded(self) -> None:
        err = PermissionDeniedError(field="governance_context")
        assert err.field == "governance_context"


class TestAuthRequiredError:
    def test_subclass_and_code(self) -> None:
        assert issubclass(AuthRequiredError, AdcpError)
        err = AuthRequiredError()
        assert err.code == "AUTH_REQUIRED"
        assert err.recovery == "correctable"

    def test_message_override(self) -> None:
        err = AuthRequiredError(message="bring credentials")
        assert "bring credentials" in str(err)


class TestServiceUnavailableError:
    def test_subclass_and_recovery_transient(self) -> None:
        assert issubclass(ServiceUnavailableError, AdcpError)
        err = ServiceUnavailableError()
        assert err.code == "SERVICE_UNAVAILABLE"
        assert err.recovery == "transient"

    def test_retry_after_forwarded(self) -> None:
        err = ServiceUnavailableError(retry_after=30)
        assert err.retry_after == 30


class TestRateLimitedError:
    def test_subclass_and_recovery_transient(self) -> None:
        err = RateLimitedError(retry_after=10)
        assert err.code == "RATE_LIMITED"
        assert err.recovery == "transient"
        assert err.retry_after == 10


class TestMediaBuyNotFoundError:
    def test_subclass_and_recovery_correctable(self) -> None:
        err = MediaBuyNotFoundError(media_buy_id="mb_1")
        assert err.code == "MEDIA_BUY_NOT_FOUND"
        assert err.recovery == "correctable"
        assert err.details["media_buy_id"] == "mb_1"


class TestAccountNotFoundError:
    def test_subclass_and_recovery_terminal(self) -> None:
        err = AccountNotFoundError()
        assert err.code == "ACCOUNT_NOT_FOUND"
        assert err.recovery == "terminal"


class TestBillingNotPermittedForAgentError:
    def test_subclass_and_details_shape(self) -> None:
        err = BillingNotPermittedForAgentError(
            rejected_billing=["agent", "advertiser"],
            suggested_billing=["operator"],
        )
        assert err.code == "BILLING_NOT_PERMITTED_FOR_AGENT"
        assert err.recovery == "correctable"
        assert err.details["rejected_billing"] == ["agent", "advertiser"]
        assert err.details["suggested_billing"] == ["operator"]

    def test_optional_suggested_billing_omitted(self) -> None:
        err = BillingNotPermittedForAgentError(rejected_billing=["agent"])
        assert "suggested_billing" not in err.details
        assert err.details["rejected_billing"] == ["agent"]


class TestValidationError:
    def test_subclass_and_recovery_correctable(self) -> None:
        err = ValidationError(field="total_budget", message="too low")
        assert err.code == "VALIDATION_ERROR"
        assert err.recovery == "correctable"
        assert err.field == "total_budget"


class TestUnsupportedFeatureError:
    def test_subclass_and_recovery_correctable(self) -> None:
        err = UnsupportedFeatureError(field="some.feature")
        assert err.code == "UNSUPPORTED_FEATURE"
        assert err.recovery == "correctable"
        assert err.field == "some.feature"


# ---------------------------------------------------------------------------
# ref_account_id
# ---------------------------------------------------------------------------


class TestRefAccountId:
    def test_dict_with_account_id(self) -> None:
        assert ref_account_id({"account_id": "acc_acme_001"}) == "acc_acme_001"

    def test_dict_natural_key_returns_none(self) -> None:
        ref = {
            "brand": {"domain": "acme-corp.com"},
            "operator": "acme-corp.com",
        }
        assert ref_account_id(ref) is None

    def test_none_returns_none(self) -> None:
        assert ref_account_id(None) is None

    def test_pydantic_account_reference_by_id(self) -> None:
        ref = AccountReference.model_validate({"account_id": "acc_acme_001"})
        assert ref_account_id(ref) == "acc_acme_001"

    def test_pydantic_account_reference_by_natural_key(self) -> None:
        ref = AccountReference.model_validate(
            {
                "brand": {"domain": "acme-corp.com"},
                "operator": "acme-corp.com",
            }
        )
        assert ref_account_id(ref) is None

    def test_inner_pydantic_by_id_variant(self) -> None:
        # Adopters who instantiate the inner variant directly via the
        # public alias should also work.
        ref = AccountReferenceById(account_id="acc_xyz")
        assert ref_account_id(ref) == "acc_xyz"

    def test_inner_pydantic_natural_key_variant(self) -> None:
        ref = AccountReferenceByNaturalKey(
            brand=BrandReference(domain="acme-corp.com"),
            operator="acme-corp.com",
        )
        assert ref_account_id(ref) is None

    def test_dict_with_non_string_account_id_returns_none(self) -> None:
        # Defensive: a malformed dict shouldn't crash.
        assert ref_account_id({"account_id": 123}) is None  # type: ignore[dict-item]
