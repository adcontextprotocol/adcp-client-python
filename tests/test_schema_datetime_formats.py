"""Public date-time format validation is independent of Python parser precision.

These exercise the registered named and task validators on the actual cached
schemas. Persisted reporting timestamps have a separate, lossless decoder;
its microsecond precision and historical offset rules are not this contract.
"""

from copy import deepcopy

import pytest

from adcp.validation.schema_loader import get_named_validator, get_validator
from adcp.validation.schema_validator import validate_request

PIN = "3.2.0-rc.3"
FRACTIONS = ("", ".1", ".12", ".123", ".1234", ".12345", ".123456", ".123456789012")
OFFSETS = ("Z", "z", "+00:00", "-00:00", "+05:45", "-03:30", "+23:59")
VALID = tuple(
    "2026-04-01T12:00:00" + fraction + offset for fraction in FRACTIONS for offset in OFFSETS
) + (
    "2024-02-29t12:00:00.00001z",
    "2000-02-29T12:00:00Z",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59.999999999Z",
)
INVALID = (
    "2026-02-29T12:00:00Z",
    "1900-02-29T12:00:00Z",
    "2026-02-30T12:00:00.12345Z",
    "0000-01-01T00:00:00Z",
    "2026-00-01T00:00:00Z",
    "2026-13-01T00:00:00Z",
    "2026-04-00T00:00:00Z",
    "2026-04-31T00:00:00Z",
    "2026-04-01T12:00:00.12345",
    "2026-04-01T12:00:00",
    "2026-04-01",
    "2026-04-01T12:00:00.Z",
    "2026-04-01T12:00:00,12345Z",
    "2026-04-01T24:00:00Z",
    "2026-04-01T12:60:00Z",
    # Keep the existing checker's seconds 00..59 boundary; this correction
    # does not commission leap-second support or new offset syntax.
    "2016-12-31T23:59:60Z",
    "2026-04-01T12:00:00+24:00",
    "2026-04-01T12:00:00+05:60",
    "2026-04-01T12:00:00+05:45:03",
    "2026-04-01T12:00:00+0545",
    "2026-04-01 12:00:00Z",
    "20260401T120000Z",
    "2026-W14-3T12:00:00Z",
    "2026-04-01T12:00:00.\u0661Z",
    "2026-04-01T1\u0662:00:00Z",
    "2026-04-01T12:00:00+0\u0661:00",
    "2026-04-01T12:00:00Z\n",
    "2026-04-01T12:00:00Z trailing",
    "not-a-timestamp",
    None,
    5,
    True,
    {},
)


def reporting_timestamp_field():
    validator = get_named_validator("core/reporting-delivery-config-state.json", version=PIN)
    assert validator is not None
    field = validator.schema["properties"]["activated_at"]
    assert field["type"] == "string" and field["format"] == "date-time"
    return validator.evolve(schema=field)


def request_with_timestamp(value):
    return {
        "proposal_id": "format-contract-proposal",
        "total_budget": {"amount": 50000, "currency": "USD"},
        "start_time": value,
        "end_time": "2030-06-30T23:59:59Z",
        "idempotency_key": "format-contract-0001",
        "brand": {"domain": "advertiser.example.test"},
        "account": {"account_id": "acct_format"},
    }


@pytest.mark.parametrize("value", VALID)
def test_named_reporting_format_accepts_fractional_precision_without_coercion(value):
    original = value.encode()
    reporting_timestamp_field().validate(value)
    assert value.encode() == original


@pytest.mark.parametrize("value", INVALID)
def test_named_reporting_format_preserves_invalid_input_rejection(value):
    assert not reporting_timestamp_field().is_valid(value)


@pytest.mark.parametrize("version", ["3.1", PIN])
@pytest.mark.parametrize("value", VALID)
def test_public_request_format_preserves_exact_value_and_oneof(version, value):
    payload = request_with_timestamp(value)
    original = deepcopy(payload)
    validator = get_validator("create_media_buy", "request", version=version)
    assert validator is not None
    validator.validate(payload)
    outcome = validate_request("create_media_buy", payload, version=version)
    assert outcome.valid, outcome.issues
    assert payload == original


@pytest.mark.parametrize("version", ["3.1", PIN])
@pytest.mark.parametrize("value", INVALID)
def test_public_request_format_preserves_invalid_input_rejection(version, value):
    payload = request_with_timestamp(value)
    original = deepcopy(payload)
    outcome = validate_request("create_media_buy", payload, version=version)
    assert not outcome.valid
    assert any(issue.pointer == "/start_time" for issue in outcome.issues)
    assert payload == original


@pytest.mark.parametrize("version", ["3.1", PIN])
def test_public_request_asap_still_selects_only_the_const_branch(version):
    payload = request_with_timestamp("asap")
    assert validate_request("create_media_buy", payload, version=version).valid
    payload["end_time"] = "asap"
    assert not validate_request("create_media_buy", payload, version=version).valid
    assert not reporting_timestamp_field().is_valid("asap")


@pytest.mark.parametrize("value", [None, 5, True, {}, []])
def test_format_annotation_does_not_impose_a_string_type(value):
    # JSON Schema format applies to strings. The actual cached field's type
    # constraint, rather than a new format coercion, rejects nonstrings.
    field = reporting_timestamp_field()
    assert field.evolve(schema={"format": "date-time"}).is_valid(value)
    assert not field.is_valid(value)
