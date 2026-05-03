"""Tests for adcp.decisioning state-machine helpers and typed exceptions.

Covers:
* MEDIA_BUY_TRANSITIONS / validate_media_buy_transition
* CREATIVE_TRANSITIONS / validate_creative_transition
* ref_account_id
* All eight typed AdcpError subclasses: error codes, recovery semantics
"""

from __future__ import annotations

import pytest

from adcp.decisioning import (
    CREATIVE_TRANSITIONS,
    MEDIA_BUY_TRANSITIONS,
    AccountNotFoundError,
    AdcpError,
    AuthRequiredError,
    BillingNotPermittedForAgentError,
    MediaBuyNotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    RequestValidationError,
    ServiceUnavailableError,
    ref_account_id,
    validate_creative_transition,
    validate_media_buy_transition,
)
from adcp.types import CreativeStatus, MediaBuyStatus

# ---------------------------------------------------------------------------
# MEDIA_BUY_TRANSITIONS map shape
# ---------------------------------------------------------------------------


def test_media_buy_transitions_covers_all_statuses() -> None:
    assert set(MEDIA_BUY_TRANSITIONS.keys()) == set(MediaBuyStatus)


def test_media_buy_transitions_terminal_states_are_empty() -> None:
    for terminal in (
        MediaBuyStatus.completed,
        MediaBuyStatus.rejected,
        MediaBuyStatus.canceled,
    ):
        assert MEDIA_BUY_TRANSITIONS[terminal] == set(), f"{terminal} should be terminal"


def test_media_buy_transitions_active_can_pause_complete_cancel() -> None:
    allowed = MEDIA_BUY_TRANSITIONS[MediaBuyStatus.active]
    assert MediaBuyStatus.paused in allowed
    assert MediaBuyStatus.completed in allowed
    assert MediaBuyStatus.canceled in allowed


# ---------------------------------------------------------------------------
# validate_media_buy_transition — valid paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        ("pending_creatives", "pending_start"),
        ("pending_creatives", "canceled"),
        ("pending_start", "active"),
        ("pending_start", "canceled"),
        ("active", "paused"),
        ("active", "completed"),
        ("active", "canceled"),
        ("paused", "active"),
        ("paused", "canceled"),
    ],
)
def test_validate_media_buy_transition_valid_strings(from_state: str, to_state: str) -> None:
    validate_media_buy_transition(from_state, to_state)  # must not raise


def test_validate_media_buy_transition_valid_enum_members() -> None:
    validate_media_buy_transition(MediaBuyStatus.active, MediaBuyStatus.paused)


# ---------------------------------------------------------------------------
# validate_media_buy_transition — invalid paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        ("completed", "active"),
        ("rejected", "pending_start"),
        ("canceled", "active"),
        ("paused", "completed"),  # paused cannot complete directly
        ("pending_creatives", "active"),  # must go through pending_start
    ],
)
def test_validate_media_buy_transition_raises_on_disallowed(
    from_state: str, to_state: str
) -> None:
    with pytest.raises(AdcpError) as exc_info:
        validate_media_buy_transition(from_state, to_state)
    err = exc_info.value
    assert err.code == "INVALID_REQUEST"
    assert err.recovery == "correctable"


def test_validate_media_buy_transition_unknown_from_state() -> None:
    with pytest.raises(AdcpError) as exc_info:
        validate_media_buy_transition("totally_unknown", "active")
    assert exc_info.value.code == "INVALID_REQUEST"


def test_validate_media_buy_transition_unknown_to_state() -> None:
    with pytest.raises(AdcpError) as exc_info:
        validate_media_buy_transition("active", "totally_unknown")
    assert exc_info.value.code == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# CREATIVE_TRANSITIONS map shape
# ---------------------------------------------------------------------------


def test_creative_transitions_covers_all_statuses() -> None:
    assert set(CREATIVE_TRANSITIONS.keys()) == set(CreativeStatus)


def test_creative_rejected_allows_resubmit() -> None:
    assert CreativeStatus.processing in CREATIVE_TRANSITIONS[CreativeStatus.rejected]


def test_creative_archived_allows_unarchive() -> None:
    assert CreativeStatus.approved in CREATIVE_TRANSITIONS[CreativeStatus.archived]


# ---------------------------------------------------------------------------
# validate_creative_transition — valid paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        ("processing", "pending_review"),
        ("processing", "rejected"),
        ("pending_review", "approved"),
        ("pending_review", "rejected"),
        ("approved", "archived"),
        ("rejected", "processing"),   # resubmit
        ("archived", "approved"),     # unarchive
    ],
)
def test_validate_creative_transition_valid(from_state: str, to_state: str) -> None:
    validate_creative_transition(from_state, to_state)  # must not raise


def test_validate_creative_transition_valid_enum_members() -> None:
    validate_creative_transition(CreativeStatus.processing, CreativeStatus.pending_review)


# ---------------------------------------------------------------------------
# validate_creative_transition — invalid paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state, to_state",
    [
        ("approved", "processing"),   # cannot go backwards
        ("pending_review", "processing"),
        ("archived", "processing"),   # archived only unarchives to approved
    ],
)
def test_validate_creative_transition_raises_on_disallowed(
    from_state: str, to_state: str
) -> None:
    with pytest.raises(AdcpError) as exc_info:
        validate_creative_transition(from_state, to_state)
    err = exc_info.value
    assert err.code == "INVALID_REQUEST"
    assert err.recovery == "correctable"


def test_validate_creative_transition_unknown_from_state() -> None:
    with pytest.raises(AdcpError) as exc_info:
        validate_creative_transition("mystery_state", "approved")
    assert exc_info.value.code == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# ref_account_id
# ---------------------------------------------------------------------------


def test_ref_account_id_extracts_string() -> None:
    assert ref_account_id({"account_id": "acct_123"}) == "acct_123"


def test_ref_account_id_none_input() -> None:
    assert ref_account_id(None) is None


def test_ref_account_id_missing_key() -> None:
    assert ref_account_id({"something_else": "x"}) is None


def test_ref_account_id_non_string_value() -> None:
    assert ref_account_id({"account_id": 42}) is None  # type: ignore[dict-item]


def test_ref_account_id_empty_dict() -> None:
    assert ref_account_id({}) is None


# ---------------------------------------------------------------------------
# Typed exception classes
# ---------------------------------------------------------------------------


def test_permission_denied_error_code_and_recovery() -> None:
    err = PermissionDeniedError("update_media_buy")
    assert err.code == "PERMISSION_DENIED"
    assert err.recovery == "terminal"
    assert "update_media_buy" in str(err)


def test_permission_denied_error_no_action() -> None:
    err = PermissionDeniedError()
    assert err.code == "PERMISSION_DENIED"
    assert "Permission denied" in str(err)


def test_auth_required_error() -> None:
    err = AuthRequiredError()
    assert err.code == "AUTH_REQUIRED"
    assert err.recovery == "correctable"


def test_service_unavailable_error() -> None:
    err = ServiceUnavailableError()
    assert err.code == "SERVICE_UNAVAILABLE"
    assert err.recovery == "transient"


def test_service_unavailable_error_custom_message() -> None:
    err = ServiceUnavailableError("DB is down")
    assert "DB is down" in str(err)


def test_rate_limited_error() -> None:
    err = RateLimitedError()
    assert err.code == "RATE_LIMITED"
    assert err.recovery == "transient"
    assert err.retry_after is None


def test_rate_limited_error_retry_after() -> None:
    err = RateLimitedError(retry_after=30)
    assert err.retry_after == 30


def test_media_buy_not_found_error() -> None:
    err = MediaBuyNotFoundError()
    assert err.code == "MEDIA_BUY_NOT_FOUND"
    assert err.recovery == "correctable"


def test_account_not_found_error() -> None:
    err = AccountNotFoundError()
    assert err.code == "ACCOUNT_NOT_FOUND"
    assert err.recovery == "terminal"


def test_billing_not_permitted_for_agent_error() -> None:
    err = BillingNotPermittedForAgentError()
    assert err.code == "BILLING_NOT_PERMITTED_FOR_AGENT"
    assert err.recovery == "terminal"


def test_request_validation_error() -> None:
    err = RequestValidationError()
    assert err.code == "VALIDATION_ERROR"
    assert err.recovery == "correctable"


def test_request_validation_error_custom_message() -> None:
    err = RequestValidationError("budget field is required")
    assert "budget field is required" in str(err)


def test_typed_exceptions_are_adcp_error_subclasses() -> None:
    for cls in (
        PermissionDeniedError,
        AuthRequiredError,
        ServiceUnavailableError,
        RateLimitedError,
        MediaBuyNotFoundError,
        AccountNotFoundError,
        BillingNotPermittedForAgentError,
        RequestValidationError,
    ):
        assert issubclass(cls, AdcpError), f"{cls.__name__} must subclass AdcpError"


def test_typed_exceptions_wire_projection() -> None:
    err = RateLimitedError(retry_after=60)
    wire = err.to_wire()
    assert wire["code"] == "RATE_LIMITED"
    assert wire["recovery"] == "transient"
    assert wire["retry_after"] == 60


def test_typed_exception_details_forwarded() -> None:
    err = PermissionDeniedError("read", reason="suspended")
    assert err.details == {"reason": "suspended"}


def test_typed_exception_no_details_is_empty() -> None:
    err = AccountNotFoundError()
    assert err.details == {}
