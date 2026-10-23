"""A governance refusal names its rejection class and its location on the wire.

``schemas/cache/3.2/core/error.json`` (AdCP 3.2.1) makes ``keyword`` required on
every ``issues[]`` row, describes it as "drawn from the JSON Schema vocabulary
... do not invent custom values here", and states the purpose: an agent can
"pattern-match on rejection class without parsing message text". ``pointer`` is
"RFC 6901 JSON Pointer to the offending field".

WHY THIS FILE EXISTS. ``invalid_request`` stamped ``keyword="validation"`` --
not a vocabulary term -- and ``pointer="/"`` on every refusal that is not a
pydantic ``ValidationError``. That survived salesagent-8qela.106, which fixed one
path and left the shared emitter, and the reason it survived is that NOTHING
GRADED EITHER FIELD: the 26-site fix moved zero test outcomes across 19,291
items, which proved no regression and equally proved no coverage. A one-off wire
measurement is not a guard, so this is the guard.

It asserts on the ENVELOPE the peer receives, not on the exception, because the
exception is not what the pin constrains.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from jsonschema import Draft7Validator

from adcp.governance import validate_governance_outcome_request, validate_governance_request
from adcp.server.base import invalid_request
from adcp.types import CheckGovernanceRequest, ReportPlanOutcomeRequest

#: The admissible set, read from the validator that implements the vocabulary
#: rather than restated here, so a term this SDK emits cannot drift from the set
#: the pin points at.
VOCABULARY = frozenset(Draft7Validator.VALIDATORS)


def _rows(error: Exception, model: type[Any]) -> list[tuple[str, str]]:
    """``(keyword, pointer)`` per issue, off the built envelope."""
    envelope = invalid_request(error, payload={}, request_model=model).model_dump(mode="json")
    return [(issue["keyword"], issue["pointer"]) for issue in envelope["error"]["issues"]]


def _refuse(fn: Callable[..., Any], payload: dict[str, Any]) -> Exception:
    with pytest.raises(ValueError) as raised:
        fn(payload)
    return raised.value


_INTENT_AND_EXECUTION = {"tool": "x", "planned_delivery": {}}
_MISSING_AND_FORBIDDEN = {"plan_id": "p1", "tool": "buy_products", "governance_context": {"a": 1}}
_NEEDS_COMMITMENT = {
    "plan_id": "p1",
    "tool": "buy_products",
    "payload": {},
    "target_agent": "https://a.example",
}
_DELIVERY_NO_MEDIA_BUY = {
    "planned_delivery": {},
    "phase": "delivery",
    "governance_context": {"a": 1},
}
_BUDGET_NO_CURRENCY = {
    "planned_delivery": {"media_buy_id": "mb", "total_budget": 5},
    "governance_context": {"a": 1},
}

#: One case per vocabulary term this surface emits, each naming the EXACT rows.
#: Spelling the full row list rather than "contains" is deliberate: a regression
#: that collapses a compound refusal back to one row has to fail here.
CASES: tuple[
    tuple[str, Callable[..., Any], dict[str, Any], type[Any], list[tuple[str, str]]], ...
] = (
    (
        "oneOf: the two shapes are mutually exclusive",
        validate_governance_request,
        _INTENT_AND_EXECUTION,
        CheckGovernanceRequest,
        [("oneOf", "/")],
    ),
    (
        "anyOf: neither shape is selected",
        validate_governance_request,
        {"adcp_version": "3.2.1"},
        CheckGovernanceRequest,
        [("anyOf", "/")],
    ),
    (
        "required + not: ONE site, TWO rules, two rows",
        validate_governance_request,
        _MISSING_AND_FORBIDDEN,
        CheckGovernanceRequest,
        [
            ("required", "/payload"),
            ("required", "/target_agent"),
            ("not", "/governance_context"),
        ],
    ),
    (
        "dependencies: a conditional required key",
        validate_governance_request,
        _NEEDS_COMMITMENT,
        CheckGovernanceRequest,
        [("dependencies", "/proposed_commitment")],
    ),
    (
        "dependencies: a NESTED pointer, not the document root",
        validate_governance_request,
        _DELIVERY_NO_MEDIA_BUY,
        CheckGovernanceRequest,
        [("dependencies", "/planned_delivery/media_buy_id")],
    ),
    (
        "dependencies: one key required by a sibling's presence",
        validate_governance_request,
        _BUDGET_NO_CURRENCY,
        CheckGovernanceRequest,
        [("dependencies", "/planned_delivery/currency")],
    ),
    (
        "type: the field is the wrong primitive",
        validate_governance_outcome_request,
        {"outcome": 5},
        ReportPlanOutcomeRequest,
        [("type", "/outcome")],
    ),
    (
        "enum: the value is outside the permitted set",
        validate_governance_outcome_request,
        {"outcome": "bogus"},
        ReportPlanOutcomeRequest,
        [("enum", "/outcome")],
    ),
    (
        "required: ONE site, TWO missing keys, two rows",
        validate_governance_outcome_request,
        {"outcome": "completed", "seller_response": {"a": 1}},
        ReportPlanOutcomeRequest,
        [("required", "/check_id"), ("required", "/governance_context")],
    ),
)


@pytest.mark.parametrize(
    ("fn", "payload", "model", "expected"),
    [(fn, payload, model, expected) for _, fn, payload, model, expected in CASES],
    ids=[label for label, *_ in CASES],
)
def test_a_refusal_names_its_rejection_class_and_its_location(
    fn: Callable[..., Any],
    payload: dict[str, Any],
    model: type[Any],
    expected: list[tuple[str, str]],
) -> None:
    """The exact ``(keyword, pointer)`` rows the peer receives."""
    assert _rows(_refuse(fn, payload), model) == expected


def test_every_emitted_keyword_is_a_vocabulary_term() -> None:
    """No invented value, graded against the vocabulary rather than a local list.

    ``validation`` was the invented one and is the regression this names: it is
    not in ``Draft7Validator.VALIDATORS``, so it fails here without being
    mentioned, and so would any other invention.
    """
    emitted = {
        keyword
        for _, fn, payload, model, _ in CASES
        for keyword, _ in _rows(_refuse(fn, payload), model)
    }
    assert emitted, "the case table produced no rows, so this asserts nothing"
    outside = sorted(emitted - VOCABULARY)
    assert not outside, f"these are not JSON Schema keywords: {outside}"


def test_a_compound_refusal_keeps_one_row_per_rule() -> None:
    """``issues`` is an array, and a two-rule site must not collapse to one row.

    Stated separately from the table because this is the half no pre-existing
    test could see, and the half most likely to regress: a single-row emitter
    passes every message assertion in the suite.
    """
    compound = [
        (label, fn, payload, model, expected)
        for label, fn, payload, model, expected in CASES
        if len(expected) > 1
    ]
    assert len(compound) >= 2, "a compound site must stay in the table for this to grade anything"
    for label, fn, payload, model, expected in compound:
        rows = _rows(_refuse(fn, payload), model)
        assert len(rows) == len(expected), f"{label}: expected {len(expected)} rows, got {rows}"
        assert len({pointer for _, pointer in rows}) == len(
            rows
        ), f"{label}: two rows share a pointer, so one rule's location was lost: {rows}"
