"""Exact received JSON numbers must survive reporting filter binding."""

import json
from copy import deepcopy
from decimal import Decimal

import pytest

from adcp.reporting.feed.request import FeedRequest, transport_parameters
from adcp.reporting.receipts.transport import _raw_json, _receipt_numbers


def wire_request(lexeme):
    return _raw_json(
        '{"view":"periods","account":{"account_id":"acct_a"},"ext":{"vendor":{"n":' + lexeme + "}}}"
    )


def binding(lexeme):
    return FeedRequest.parse(transport_parameters(wire_request(lexeme))).filters_json


@pytest.mark.parametrize(
    "left,right",
    [
        ("9007199254740993.0", "9007199254740992.0"),
        ("9007199254740995.0", "9007199254740996.0"),
        ("9.007199254740993e15", "9007199254740992"),
    ],
)
def test_received_distinct_large_decimals_never_alias(left, right):
    assert binding(left) != binding(right)


@pytest.mark.parametrize(
    "integer,spelling",
    [
        (1, "1.0"),
        (10, "1e1"),
        (9007199254740993, "9007199254740993.0"),
        (9007199254740995, "9.007199254740995e15"),
        (10**23, "1e23"),
    ],
)
def test_integral_spellings_keep_the_exact_received_integer(integer, spelling):
    assert binding(str(integer)) == binding(spelling)
    decoded = transport_parameters(wire_request(spelling))
    assert type(decoded["ext"]["vendor"]["n"]) is int
    assert decoded["ext"]["vendor"]["n"] == integer


@pytest.mark.parametrize("value", ["0.5", "4.8", "4.80", "-0.125", "1.25e-4"])
def test_ordinary_finite_fractions_round_trip_without_mutating_the_request(value):
    raw = wire_request(value)
    before = deepcopy(raw)
    normalized = transport_parameters(raw)
    assert Decimal(repr(normalized["ext"]["vendor"]["n"])) == Decimal(value)
    assert FeedRequest.parse(normalized).filters["ext"]["vendor"]["n"] == json.loads(value)
    assert raw == before


@pytest.mark.parametrize(
    "value",
    [
        "0.100000000000000000001",
        "9007199254740993.25",
        "1.000000000000000000001",
        "1e-400",
        "1e400",
        "NaN",
        "Infinity",
        "-Infinity",
    ],
)
def test_unrepresentable_fraction_or_nonfinite_number_is_rejected(value):
    with pytest.raises(ValueError):
        transport_parameters(wire_request(value))


@pytest.mark.parametrize(
    "left,right,equal",
    [
        (1, 1.0, True),
        ({"n": [10]}, {"n": [10.0]}, True),
        (0.5, 1, False),
        (4.8, 4.80, True),
        (True, 1, False),
        (False, 0, False),
        ({"b": True}, {"b": 1}, False),
        (1, "1", False),
        (1, 2, False),
        ({"v": None}, {}, False),
        (2**53 - 1, 2**53, False),
        (2**53 + 1, 2**53, False),
    ],
)
def test_existing_in_memory_json_equivalence_is_unchanged(left, right, equal):
    def direct(value):
        return FeedRequest.parse(
            {"view": "periods", "account": {"account_id": "acct_a"}, "ext": {"v": value}}
        ).filters_json

    assert (direct(left) == direct(right)) is equal


@pytest.mark.parametrize("value,expected", [("1", 1), ("1.0", 1), ("1e2", 100)])
def test_page_limits_keep_exact_integer_equivalence(value, expected):
    raw = wire_request("4.8")
    raw["pagination"] = {"max_results": Decimal(value)}
    result = transport_parameters(raw)
    assert type(result["pagination"]["max_results"]) is int
    assert FeedRequest.parse(result).limit == expected


@pytest.mark.parametrize("value", ["0", "101", "1.000000000000000000001", "1.5", "1e400", "NaN"])
def test_page_limits_still_reject_out_of_range_or_inexact_values(value):
    raw = wire_request("0.5")
    raw["pagination"] = {"max_results": Decimal(value)}
    with pytest.raises(ValueError):
        transport_parameters(raw)


@pytest.mark.parametrize("value", ["9007199254740993.0", "0.5", "NaN", "1e400"])
def test_financial_receipt_numbers_keep_their_stricter_contract(value):
    with pytest.raises(ValueError):
        _receipt_numbers(_raw_json(value))
