"""``error.field`` is ``issues[0].pointer`` translated, and one owner does it.

``core/error.json`` requires it on ``field``, on ``issues`` and on
``issues[].pointer``: ``field`` carries ``packages[0].targeting`` where the
pointer carries ``/packages/0/targeting``.

Three case populations, none of them derived from the translator:

* the arrow examples parsed out of the pinned schema bundle — the spec is the
  authority for what a translation should produce;
* whatever pointers the jsonschema oracle and pydantic emit on real malformed
  payloads, round-tripped;
* hand-authored pointer shapes the spec's examples do not cover.

The first two are asserted non-empty, since an extraction that matched nothing
yields a parametrize that passes.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from jsonschema import Draft7Validator
from pydantic import ValidationError

from adcp.types import CreateMediaBuyRequest
from adcp.validation.pydantic_errors import _keyword, issues_from_validation_error
from adcp.validation.schema_errors import build_adcp_validation_error_payload
from adcp.validation.schema_validator import (
    ValidationIssue,
    field_to_pointer,
    pointer_to_field,
    validate_request,
)

_SCHEMA_ROOT = pathlib.Path(__file__).resolve().parents[1] / "schemas" / "cache" / "3.2"

#: ``'/packages/0/targeting' -> 'packages[0].targeting'`` as the descriptions
#: write it, in either arrow spelling.
_ARROW = re.compile(r"[`']((?:/[^`' ]+)+)[`']\s*(?:→|->)\s*[`']([^`' ]+)[`']")


def _spec_example_pairs() -> list[tuple[str, str]]:
    """Every pointer -> JSONPath-lite example the pinned bundle states."""
    strings: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            strings.append(node)

    for path in sorted(_SCHEMA_ROOT.rglob("*.json")):
        try:
            walk(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):  # pragma: no cover
            continue
    return sorted(set(_ARROW.findall("\n".join(strings))))


def _real_validation_pointers() -> list[str]:
    """Pointers the two validators emit on real malformed payloads.

    Nested shapes are the point: a translator that only sees one-segment
    pointers is untested.
    """
    payloads: list[object] = [
        {"packages": [{"targeting_overlay": {"geo_countries": [5]}}]},
        {"packages": "not-a-list"},
        {"buyer_ref": 7, "packages": [{"products": [{}]}]},
        {},
        "not-an-object",
        [1, 2],
    ]
    pointers: set[str] = set()
    for payload in payloads:
        outcome = validate_request("create_media_buy", payload)
        pointers.update(issue.pointer for issue in outcome.issues)
        try:
            CreateMediaBuyRequest.model_validate(payload)
        except ValidationError as error:
            pointers.update(issue.pointer for issue in issues_from_validation_error(error))
    return sorted(pointers)


#: Hand-authored. The three shapes with no dotted rendering, plus root.
AUTHORED_EDGE_CASES = [
    "/",
    "/a.b",
    "/a~1b",
    "/a~0b",
    "/0",
    "/x/a.b/y",
    "/ext/vendor.key/value",
    "/details/a'b",
    "/packages/0/optimization_goals/1/event_sources/0",
]

SPEC_PAIRS = _spec_example_pairs()
REAL_POINTERS = _real_validation_pointers()


@pytest.mark.parametrize(("pointer", "expected_field"), SPEC_PAIRS)
def test_spec_stated_examples(pointer, expected_field):
    """The translator reproduces every example the pinned spec states."""
    assert pointer_to_field(pointer) == expected_field


def test_spec_examples_were_actually_found():
    """An extraction that matched nothing yields a parametrize that passes."""
    assert SPEC_PAIRS, f"no arrow examples extracted from {_SCHEMA_ROOT}"


@pytest.mark.parametrize("pointer", REAL_POINTERS)
def test_round_trip_on_real_validation_pointers(pointer):
    """Translating a real pointer and back returns the same pointer."""
    assert field_to_pointer(pointer_to_field(pointer)) == pointer


def test_real_validation_pointers_are_not_empty_and_are_nested():
    """The validators must actually have produced pointers, including deep ones."""
    assert REAL_POINTERS, "no pointers collected from real validation failures"
    assert any(p.count("/") >= 3 for p in REAL_POINTERS), REAL_POINTERS


@pytest.mark.parametrize("pointer", AUTHORED_EDGE_CASES)
def test_round_trip_on_authored_edge_cases(pointer):
    assert field_to_pointer(pointer_to_field(pointer)) == pointer


def test_translation_is_not_the_identity():
    """Every round-trip above would also pass for an identity pair."""
    assert pointer_to_field("/packages/0/targeting") != "/packages/0/targeting"


def _issue(pointer: str) -> ValidationIssue:
    return ValidationIssue(pointer=pointer, message="m", keyword="type", schema_path="")


@pytest.mark.parametrize("pointer", AUTHORED_EDGE_CASES + REAL_POINTERS)
def test_builder_field_is_the_translated_first_pointer(pointer):
    """The dual-write the spec demands, asserted on the builder's output.

    ONE RULE FOR EVERY POINTER, the root included. ``core/error.json`` conditions
    the dual-write on the presence of ``issues``, not on the shape of the
    pointer -- "When `issues` is present, sellers MUST also populate `field` from
    `issues[0]` ... MUST (not SHOULD) so consumers reading `field` get
    deterministic behavior across sellers" -- and ``field`` carries no
    ``minLength``, so the root's empty translation is a legal value.

    This used to branch: a root pointer asserted ``"field" not in payload``, on
    the grounds that the root has no field path to name and ``field`` is not in
    error.json's ``required`` list. Both are true and neither is the condition
    the pin states, so that branch asserted the one thing the MUST forbids and is
    gone rather than re-tuned (salesagent-8qela.125).
    """
    payload = build_adcp_validation_error_payload("create_media_buy", "request", [_issue(pointer)])
    assert payload["field"] == pointer_to_field(pointer)
    assert not payload["field"].startswith("/"), "field is still an RFC 6901 pointer"


def test_builder_emits_top_level_issues_and_recovery():
    payload = build_adcp_validation_error_payload(
        "create_media_buy", "request", [_issue("/packages/0/targeting")]
    )
    assert payload["issues"] == payload["details"]["issues"]
    assert payload["recovery"] == "correctable"


def test_builder_omits_field_and_issues_when_there_are_none():
    """``issues`` has no ``minItems``, so an empty list is legal.

    With no ``issues[0]`` there is no source for ``field``, and both fields are
    optional — so neither is emitted.
    """
    payload = build_adcp_validation_error_payload("create_media_buy", "request", [])
    assert "field" not in payload
    assert "issues" not in payload
    assert payload["details"]["issues"] == []


def _handler_request_models() -> list[type]:
    """The request models the handler classes validate ``params`` into.

    Derived from the class bodies, so a handler added later is covered.
    """
    import inspect

    from adcp import types
    from adcp.server.content_standards import ContentStandardsHandler
    from adcp.server.governance import GovernanceHandler
    from adcp.server.sponsored_intelligence import SponsoredIntelligenceHandler

    found: dict[str, type] = {}
    for handler_cls in (ContentStandardsHandler, GovernanceHandler, SponsoredIntelligenceHandler):
        for name, member in vars(handler_cls).items():
            if name.startswith(("_", "handle_")) or not inspect.iscoroutinefunction(member):
                continue
            match = re.search(r"(\w+)\.model_validate\(params\)", inspect.getsource(member))
            if match and (model := getattr(types, match.group(1), None)) is not None:
                found[match.group(1)] = model
    return list(found.values())


HANDLER_REQUEST_MODELS = _handler_request_models()


def test_handler_request_models_were_discovered():
    assert len(HANDLER_REQUEST_MODELS) >= 21, HANDLER_REQUEST_MODELS


@pytest.mark.parametrize("model", HANDLER_REQUEST_MODELS, ids=lambda m: m.__name__)
def test_no_reachable_pointer_names_a_pydantic_internal(model):
    """A pointer must address the buyer's document, not pydantic's validators.

    pydantic's ``loc`` is not a document path: a model-level ``function-after``
    wrapper or a wrapped union prefixes synthetic elements naming the validator
    and the generated branch class, so the pointer no longer resolves against
    what the buyer sent and ``field`` inherits it. No model here does that;
    ``CreateMediaBuyRequest`` does. Wiring such a model to ``invalid_request``
    must fail here rather than on the wire (salesagent-8qela.28).
    """
    for payload in ("x", {}, {"account": "x"}, [1]):
        try:
            model.model_validate(payload)
        except ValidationError as error:
            for issue in issues_from_validation_error(error):
                assert "function-" not in issue.pointer, issue.pointer
                assert "[" not in issue.pointer, issue.pointer
                assert pointer_to_field(issue.pointer).count("['") == 0, issue.pointer


def _generated_custom_error_types() -> list[str]:
    """The error types the generated models raise, read from their source.

    The generator emits ``PydanticCustomError('allOf', ...)`` for every schema
    conditional, so these names come from the generated tree rather than from
    a list kept here.
    """
    import ast

    found: set[str] = set()
    for path in (pathlib.Path(__file__).resolve().parents[1] / "src/adcp/types").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name != "PydanticCustomError" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return sorted(found)


#: Of the types the generator raises, the ones that ARE JSON Schema keywords.
#: Filtered rather than skipped at runtime, so the parametrize carries only
#: applicable cases and its emptiness is an assertable fact.
GENERATED_KEYWORD_TYPES = [
    t for t in _generated_custom_error_types() if t in Draft7Validator.VALIDATORS
]


def test_generated_keyword_types_were_found():
    """Population check: an empty scan would make the test below vacuous."""
    assert GENERATED_KEYWORD_TYPES, "no JSON Schema keyword types found under src/adcp/types"


@pytest.mark.parametrize("error_type", GENERATED_KEYWORD_TYPES)
def test_a_keyword_the_generator_raises_survives_the_projection(error_type):
    """A pydantic type that IS a JSON Schema keyword must not be flattened.

    ``core/error.json`` asks ``issues[].keyword`` for a name "drawn from the
    JSON Schema vocabulary … do not invent custom values here". The generated
    models raise exactly such names, so mapping them to a fallback throws away
    the field's whole purpose — and ``safe_message_for`` already renders them.
    """
    assert _keyword(error_type) == error_type


def test_both_lanes_emit_the_same_envelope():
    """The jsonschema oracle and the pydantic projection must not disagree.

    Two routes reach ``VALIDATION_ERROR`` — the oracle through
    ``validate_request``, a handler's pydantic rejection through
    ``invalid_request`` — and both must produce one envelope.

    RED, and ledgered as salesagent-8qela.93. Ordering is not what it is
    waiting for: the oracle names ``/idempotency_key`` and four more located
    failures here, while the pydantic lane emits ONE entry at the document root,
    so there is no located entry for any rule to promote. That is the branch
    dispatch electing the branch whose ``mode="before"`` validator fired, and it
    is fixed in ``_conditions.py``.

    ``core/error.json`` requires ``field`` whenever ``issues`` is present, so
    the lane omitting it is non-conformant rather than exercising a legitimate
    root-level case — do not satisfy this by choosing a payload that keeps both
    lanes field-level, which would retire the only check that sees the defect.
    """
    oracle = build_adcp_validation_error_payload(
        "create_media_buy", "request", validate_request("create_media_buy", {"packages": 1}).issues
    )
    try:
        CreateMediaBuyRequest.model_validate({"packages": 1})
        raise AssertionError("payload validated when the test needs it rejected")
    except ValidationError as error:
        handler = build_adcp_validation_error_payload(
            "create_media_buy", "request", issues_from_validation_error(error)
        )

    assert sorted(oracle) == sorted(handler), (sorted(oracle), sorted(handler))
    for payload in (oracle, handler):
        assert payload["recovery"] == "correctable"
        assert payload["issues"] == payload["details"]["issues"]
        assert payload["field"] == pointer_to_field(payload["issues"][0]["pointer"])
        assert not payload["field"].startswith("/")

    # THE SUBSTANTIVE AGREEMENT, and the assertion this test exists for. The two
    # above are about the envelope's SHAPE, and both lanes satisfy them once
    # ``field`` is dual-written unconditionally (salesagent-8qela.125) -- while
    # the lanes still disagree about WHERE the document is wrong: the oracle
    # names ``/idempotency_key`` and four more located failures, the pydantic
    # lane one entry at the document root.
    #
    # DO NOT DELETE THIS TO REACH GREEN. It is the only check that sees
    # salesagent-8qela.116 -- a root-required field's absence read as
    # branch-selecting requiredness, so no branch is ever reached and the refusal
    # collapses to the combinator with nothing located to promote. Closing .116
    # needs the generator to emit the root's required set; until then this is RED
    # for a named, filed cause, which is the honest state. The docstring's
    # warning against satisfying this test by keeping both lanes field-level
    # applies to removing this assertion too.
    for lane, payload in (("oracle", oracle), ("pydantic", handler)):
        assert payload["field"], (
            f"the {lane} lane located no failure: issues[0] is at the document "
            f"root, so `field` is the empty string and a pre-3.1 consumer reading "
            f"it alone learns nothing — {payload['issues']}"
        )


def test_version_unsupported_field_comes_from_the_translator():
    """VERSION_UNSUPPORTED gets its ``field`` from the translator too."""
    issue = ValidationIssue(
        pointer="/adcp_version",
        message="no bundled request validator",
        keyword="schema_unavailable",
        schema_path="",
    )
    payload = build_adcp_validation_error_payload("create_media_buy", "request", [issue])
    assert payload["code"] == "VERSION_UNSUPPORTED"
    assert payload["field"] == "adcp_version"


#: Payloads reaching ``create_media_buy``, the one request root whose schema
#: carries a root ``anyOf``. "Packages only" is the realistic case: a buyer
#: omits each package's ``budget`` (arm 0 requires it) and sends a bad
#: ``geo_countries`` element, so the pin refuses the document at the ROOT and
#: at a field at once.
ROOT_AND_FIELD_PAYLOADS = [
    {
        "adcp_version": "3.2",
        "idempotency_key": "11111111-2222-3333-4444-555555555555",
        "account": {"account_id": "acct_1"},
        "brand": {"domain": "example.com"},
        "start_time": "2026-02-01T00:00:00Z",
        "end_time": "2026-02-28T00:00:00Z",
        "packages": [
            {
                "buyer_ref": "pkg_1",
                "product_id": "p1",
                "pricing_option_id": "po_1",
                "targeting_overlay": {"geo_countries": [5]},
            }
        ],
    },
    {"packages": [{"targeting_overlay": {"geo_countries": [5]}}]},
    {"buyer_ref": 7, "packages": [{"products": [{}]}]},
    {},
]


def _envelopes_with_both_kinds_of_issue() -> list[tuple[str, dict[str, object]]]:
    """Envelopes whose issue set holds a root entry AND a located one.

    Derived by running both lanes over the payloads above rather than authored,
    so the cases are whatever the validators really produce. An entry is
    located when its pointer translates to a JSONPath-lite field path; the
    document root translates to the empty string, which is not one.
    """
    found: list[tuple[str, dict[str, object]]] = []
    for index, payload in enumerate(ROOT_AND_FIELD_PAYLOADS):
        lanes: list[tuple[str, list[ValidationIssue]]] = [
            ("oracle", list(validate_request("create_media_buy", payload).issues))
        ]
        try:
            CreateMediaBuyRequest.model_validate(payload)
        except ValidationError as error:
            lanes.append(("pydantic", issues_from_validation_error(error)))
        for lane, issues in lanes:
            if not issues:
                continue
            located = [i for i in issues if pointer_to_field(i.pointer)]
            rooted = [i for i in issues if not pointer_to_field(i.pointer)]
            if located and rooted:
                found.append(
                    (
                        f"{lane}-{index}",
                        build_adcp_validation_error_payload("create_media_buy", "request", issues),
                    )
                )
    return found


ROOT_AND_FIELD_ENVELOPES = _envelopes_with_both_kinds_of_issue()


def test_a_case_with_both_kinds_of_issue_exists():
    """Without one, the test below asserts nothing: every set would be located."""
    assert ROOT_AND_FIELD_ENVELOPES, (
        "no payload produced an issue set holding BOTH a root entry and a located "
        "one, so the ordering rule is ungraded"
    )


@pytest.mark.parametrize(
    ("label", "payload"), ROOT_AND_FIELD_ENVELOPES, ids=[c[0] for c in ROOT_AND_FIELD_ENVELOPES]
)
def test_field_comes_from_a_located_issue_whenever_one_exists(label, payload):
    """``issues[0]`` names a field in the buyer's document, never the root.

    ``core/error.json`` requires ``field`` to be ``issues[0].pointer``
    translated, and types it as a "Field path … in JSONPath-lite format (e.g.
    'packages[0].targeting')". The document root has no such spelling, so an
    entry pointing at the root cannot be ``issues[0]`` without leaving ``field``
    unpopulated — and ``field`` is graded, not advisory:
    ``compliance/3.2.1/domains/media-buy/scenarios/refine_finalize_exclusivity.yaml``
    checks ``field_present`` on ``adcp_error.field``.

    A root entry may still APPEAR in ``issues[]``: the pin's own envelope in
    ``protocol/calling-an-agent.mdx`` lists ``anyOf``/``oneOf`` among the legal
    keywords and calls a union entry's ``variants[]`` "the fastest recovery
    path". What the pin never does is put one at the document root — in its
    example the combinator entry's pointer is ``/account``, the union-valued
    FIELD. Only ordering is constrained here; nothing is dropped.
    """
    issues = payload["issues"]
    assert pointer_to_field(issues[0]["pointer"]), issues
    assert payload["field"] == pointer_to_field(issues[0]["pointer"])
    assert {i["pointer"] for i in issues} == {i["pointer"] for i in payload["details"]["issues"]}
