"""Differential contract between structural models and canonical schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import adcp.types as public_types
from adcp import get_adcp_spec_version
from adcp.validation import validate_request, validate_response

_FIXTURES = Path(__file__).parent / "fixtures"
_CASES: list[dict[str, Any]] = json.loads((_FIXTURES / "validation-parity-cases.json").read_text())
_ALLOWED_DIVERGENCES: dict[str, dict[str, str]] = json.loads(
    (_FIXTURES / "validation-model-divergences.json").read_text()
)


def _pydantic_accepts(model: Any, payload: dict[str, Any]) -> bool:
    """Whether the public name accepts the document, however it is shaped.

    A root whose branches partition is a union over branch classes, which has no
    ``model_validate`` of its own -- parsing a union is what a ``TypeAdapter``
    is for. Reaching for ``model_validate`` unconditionally is what stopped this
    whole module from COLLECTING, which left the series with no acceptance gate
    while every other number in the run looked fine.
    """
    try:
        TypeAdapter(model).validate_python(payload)
    except ValidationError:
        return False
    return True


def _canonical_accepts(case: dict[str, Any]) -> bool:
    validate = validate_request if case["direction"] == "request" else validate_response
    return validate(case["tool"], case["payload"]).valid


def test_validation_parity_fixtures_match_both_runtime_paths() -> None:
    """Each fixture pins the current Pydantic and canonical outcomes."""
    for case in _CASES:
        model = getattr(public_types, case["model"])
        assert _pydantic_accepts(model, case["payload"]) is case["pydantic_valid"], case["id"]
        assert _canonical_accepts(case) is case["canonical_valid"], case["id"]


def test_every_model_schema_divergence_is_explicitly_allowlisted() -> None:
    """No canonical negative may silently pass Pydantic without a rationale."""
    actual = {
        case["id"] for case in _CASES if case["pydantic_valid"] and not case["canonical_valid"]
    }
    assert actual == set(_ALLOWED_DIVERGENCES)
    for case_id, decision in _ALLOWED_DIVERGENCES.items():
        assert decision["keyword"], case_id
        assert decision["reason"], case_id


def test_no_fixture_is_stricter_in_canonical_schema_than_declared() -> None:
    """A changed generator outcome requires updating the reviewed decision."""
    declared = {case["id"]: case for case in _CASES}
    assert set(_ALLOWED_DIVERGENCES) <= set(declared)
    for case_id in _ALLOWED_DIVERGENCES:
        case = declared[case_id]
        assert case["pydantic_valid"] is True
        assert case["canonical_valid"] is False


def test_high_risk_behavioral_extension_has_targeted_runtime_validator() -> None:
    """Commitment-critical x-adcp-validation rules also fail at construction."""
    spec_version = get_adcp_spec_version()
    bundle = spec_version if "-" in spec_version else ".".join(spec_version.split(".")[:2])
    schema_path = (
        Path(__file__).parents[1] / "schemas" / "cache" / bundle / "media-buy" / "change-term.json"
    )
    schema = json.loads(schema_path.read_text())
    constraints = schema["x-adcp-validation"]["verifier_constraints"]
    assert "constraint_action_compatibility" in constraints

    with pytest.raises(ValidationError, match="incompatible with action"):
        public_types.MediaBuyChangeTerm.model_validate(
            {
                "term_id": "right_pause",
                "action": "pause",
                "service_mode": "self_serve",
                "constraints": {"kind": "budget", "max_delta_percent": 10},
            }
        )


#: The generated validator whose completeness this module grades. The
#: post-processor emits it for a schema root whose ``anyOf``/``oneOf`` branches all
#: declare ``required`` (``_root_required_groups`` in
#: ``scripts/post_generate_fixes.py``), and it checks only that one branch's
#: ``required`` names are present. A root conditional (``if``/``then``/``else``)
#: gets no such validator, which is why ``get_signals`` is outside this
#: population and its declared divergences are not graded here.
_REQUIRED_GROUP_VALIDATOR = "_require_schema_required_group"


def _carries_required_group_validator(model: Any) -> bool:
    """Whether the generator made this root refuse a document satisfying no branch.

    Two shapes answer yes, and the question is the same for both: does the
    generated model carry the root's combinator at all?

    * a flat root carries the compiled validator;
    * a SPLIT root carries it as the branches' own shapes -- each branch requires what
      its branch requires and types what it forbids as ``None``, and the residual
      conditions are compiled onto each branch. So the refusal is there, it just
      is not spelled as one validator on one class.

    Asking only the first question excluded every split root from the population
    the moment the emitter was wired -- including ``create_media_buy``, which is
    the root this module exists to grade.
    """
    from adcp.types._split_root import branch_members

    members = branch_members(model)
    if members:
        return all(
            _REQUIRED_GROUP_VALIDATOR in member.__pydantic_decorators__.model_validators
            or _branch_shape_refuses(member)
            for member in members
        )
    return _REQUIRED_GROUP_VALIDATOR in model.__pydantic_decorators__.model_validators


def _branch_shape_refuses(branch: type[BaseModel]) -> bool:
    """Whether an branch's own shape is what refuses a foreign document.

    An branch requires at least one field with no default, or types at least one
    field ``None`` to forbid it. Either makes a document from another branch
    unacceptable to this class without any validator running.
    """
    return any(
        field.is_required() or field.annotation is type(None)
        for field in branch.model_fields.values()
    )


def _required_group_cases() -> list[dict[str, Any]]:
    """Fixture cases whose model carries the emitted required-group validator.

    Derived from the live classes: membership is whether the generator put the
    validator on the model, not a list of ids maintained here.
    """
    return [
        case
        for case in _CASES
        if _carries_required_group_validator(getattr(public_types, case["model"]))
    ]


def test_the_required_group_population_is_not_empty() -> None:
    """A derivation that selects nothing passes for free."""
    cases = _required_group_cases()
    assert cases, (
        "no fixture case exercises a model carrying "
        f"{_REQUIRED_GROUP_VALIDATOR}; the rule below grades nothing"
    )
    assert any(
        not case["canonical_valid"] for case in cases
    ), "every such case is schema-valid, so the validator's refusals go ungraded"


@pytest.mark.parametrize("case", _required_group_cases(), ids=lambda case: str(case["id"]))
def test_emitted_required_group_validator_refuses_what_the_schema_refuses(
    case: dict[str, Any],
) -> None:
    """The emitted validator must be the whole root constraint, not part of it.

    ``enforce_root_required_groups`` states that a presence check over each
    branch's ``required`` names "is the whole constraint for ``anyOf``". Where an
    branch also carries ``not``, a nested ``allOf`` or a discriminating ``const``,
    it is not: the model then accepts a document satisfying no branch of its own
    schema, and ``model_validate`` is what a caller reads as the contract.

    Both verdicts are computed here rather than read off the fixture, and this
    rule consults no divergence ledger. Recording the difference does not
    satisfy it and neither does editing a recorded outcome — only the two
    validators agreeing on the document does.
    """
    model = getattr(public_types, case["model"])
    canonical_accepts = _canonical_accepts(case)
    pydantic_accepts = _pydantic_accepts(model, case["payload"])

    if canonical_accepts:
        assert (
            pydantic_accepts
        ), f"{case['id']}: the bundled schema accepts this document and {case['model']} refuses it"
        return

    assert not pydantic_accepts, (
        f"{case['id']}: the bundled schema refuses this document and "
        f"{case['model']}.model_validate accepts it. The root combinator's arms "
        f"carry conditions beyond their 'required' names, and "
        f"{_REQUIRED_GROUP_VALIDATOR} checks only the names."
    )
