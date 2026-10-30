"""Local refusals carry ``dispatched = False``; every SDK error classifies its recovery.

Python parity for adcontextprotocol/adcp-client#3507.
"""

from __future__ import annotations

import pytest

from adcp import (
    ADCPConnectionError,
    ADCPError,
    ADCPFeatureUnsupportedError,
    ADCPTaskError,
    ADCPTimeoutError,
    IdempotencyUnsupportedError,
    RequestValidationError,
    SchemaValidationError,
    is_pre_dispatch_error,
)
from adcp.compat.versions import negotiate_version
from adcp.task_options import TaskRecoveryMetadata
from adcp.types.core import TaskResult, TaskStatus
from adcp.validation import validate_outgoing_request

SENTINEL = "sentinel-value-7c1d"


def test_a_strict_request_refusal_is_a_typed_pre_dispatch_error() -> None:
    with pytest.raises(RequestValidationError) as info:
        validate_outgoing_request(
            "create_media_buy", {"buyer_ref": 7, "po_number": SENTINEL, "x": 1}, "strict"
        )
    error = info.value
    # Existing handlers still match, and the error is an ADCPError.
    assert isinstance(error, SchemaValidationError) and isinstance(error, ADCPError)
    assert error.dispatched is False and is_pre_dispatch_error(error)
    assert error.code == "VALIDATION_ERROR" and error.recovery == "correctable"
    assert error.task == "create_media_buy" and error.side == "request"
    assert error.fields and error.field == error.fields[0]
    assert len(set(error.fields)) == len(error.fields)
    assert all(pointer.startswith("/") for pointer in error.fields)
    # Pointers only: the submitted values never reach the error.
    assert SENTINEL not in str(error) and SENTINEL not in repr(error.details)


def test_a_response_validation_error_is_not_marked() -> None:
    from adcp.validation import build_validation_error

    error = build_validation_error("get_products", "response", [])
    assert type(error) is SchemaValidationError
    assert not is_pre_dispatch_error(error)


def test_feature_refusals_are_marked_only_where_nothing_was_sent() -> None:
    with pytest.raises(ADCPFeatureUnsupportedError) as negotiated:
        negotiate_version(["2.5"], maximum="3.1")
    assert is_pre_dispatch_error(negotiated.value)
    assert negotiated.value.recovery == "correctable"
    # A refusal that follows a response (for example an unconvertible one) is not.
    assert ADCPFeatureUnsupportedError(["x"]).dispatched is None
    assert is_pre_dispatch_error(ADCPFeatureUnsupportedError(["x"], dispatched=False))


def test_idempotency_refusal_is_local() -> None:
    error = IdempotencyUnsupportedError()
    assert is_pre_dispatch_error(error) and error.recovery == "correctable"


def test_agent_failures_are_never_marked() -> None:
    for error in (
        ADCPConnectionError("refused"),
        ADCPTaskError("get_products", [{"code": "SERVICE_UNAVAILABLE"}]),
        ADCPTimeoutError("slow"),
    ):
        assert not is_pre_dispatch_error(error)


def test_is_pre_dispatch_error_checks_the_attribute() -> None:
    result = TaskResult(status=TaskStatus.FAILED, success=False, error="x", dispatched=False)
    assert is_pre_dispatch_error(result)
    assert not is_pre_dispatch_error(TaskResult(status=TaskStatus.FAILED, success=False))

    class ForeignError(Exception):
        dispatched = False

    assert is_pre_dispatch_error(ForeignError())
    for value in (None, object(), {"dispatched": False}, ValueError(), ADCPError("x")):
        assert not is_pre_dispatch_error(value)


@pytest.mark.parametrize(
    ("errors", "recovery"),
    [
        ([{"code": "RATE_LIMITED", "recovery": "transient"}], "transient"),
        ([{"code": "RATE_LIMITED"}, {"code": "INVALID_REQUEST"}], "correctable"),
        ([{"code": "INVALID_REQUEST"}, {"code": "X", "recovery": "terminal"}], "terminal"),
        ([{"code": "X_VENDOR_NEW"}], "transient"),  # unknown code, no wire recovery
        ([], None),
    ],
)
def test_task_errors_classify_by_their_most_demanding_entry(errors, recovery) -> None:
    error = ADCPTaskError("create_media_buy", errors)
    assert error.recovery == recovery
    assert error.is_retryable is (recovery == "transient")


def test_recovery_defaults() -> None:
    assert ADCPError("x").recovery is None
    assert ADCPConnectionError("x").recovery == "transient"
    # A timeout keeps its safe-retry identity under ``recovery``.
    metadata = TaskRecoveryMetadata("create_media_buy", "op-1", "key-1")
    assert ADCPTimeoutError("slow", recovery=metadata).recovery is metadata
