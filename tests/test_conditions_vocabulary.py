"""One test per primitive in the compiled-condition vocabulary.

These are the exception the design names to "no unit tests for behaviour a
parity case can grade": the primitives are pure functions over one value, the
parity corpus grades their COMPOSITION on real documents, and the two
conventions they share -- presence as what the DOCUMENT carries, and vacuity
off-type -- are exactly where a semantic slip between a compiled predicate and
JSON Schema would land first.

Each test states the keyword the primitive carries, the satisfied case, the
refused case, and the vacuous case. A primitive with no vacuity to state says
so.
"""

from __future__ import annotations

import datetime
import decimal
import inspect
import ipaddress
import types
import uuid
from typing import Any

import pytest
from pydantic import AnyUrl, BaseModel, ConfigDict, model_serializer
from pydantic_core.core_schema import SerializerFunctionWrapHandler

from adcp.types import _conditions as c


class _Nested(BaseModel):
    mode: str | None = None
    scale: int | None = None


class _Subject(BaseModel):
    """A model whose defaults are deliberately not ``None``.

    ``pacing`` is what separates the three readings of presence: it is not
    ``None``, the caller never sent it, and the document carries it anyway
    because this class emits it. The last is the one the schema asks about.
    """

    packages: list[str] | None = None
    proposal_id: str | None = None
    pacing: str = "even"
    nested: _Nested | None = None


class _Dropping(BaseModel):
    """A model whose serializer drops the unset default, so the wire lacks it.

    The shape of ``GetMediaBuyDeliveryRequest``, which defaults two booleans and
    pops them when unset. Here the default exists and the document does NOT
    carry it, so presence must come out False -- which is why the primitives ask
    the serializer instead of inspecting the default.
    """

    tier: str = "standard"
    reason: str | None = None

    @model_serializer(mode="wrap")
    def _drop_unset_tier(self, handler: SerializerFunctionWrapHandler) -> Any:
        value = handler(self)
        if "tier" not in self.model_fields_set:
            value.pop("tier", None)
        return value


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")

    declared: int | None = None


def test_supplied_is_what_the_document_carries_not_a_null_check() -> None:
    """``required``: the document carried the property.

    The cases that separate it from every other spelling: an explicit null IS
    supplied, an absent nullable field is not, and a default the class EMITS is
    -- because the dump carries it whether or not the caller typed it, so a
    ``required`` over it is satisfied either way (salesagent-k37fs).

    The last clause is the serializer's answer, not the default's: ``_Dropping``
    has the same non-None default and pops it when unset, and there the document
    really is missing it.
    """
    subject = _Subject.model_validate({"packages": ["pkg_1"], "proposal_id": None})

    assert c.supplied(subject, "packages")
    assert c.supplied(subject, "proposal_id"), "an explicit null is a supplied value"
    assert c.supplied(subject, "pacing"), "a default the class emits is on the document"
    assert not c.supplied(subject, "nested")

    assert not c.supplied(_Dropping(), "tier"), "a default the serializer drops is not carried"
    assert c.supplied(_Dropping(tier="standard"), "tier"), "the same value, typed, is"


def test_sent_is_membership_plus_the_non_null_half() -> None:
    """Presence on the document that is SENT, for a position forbidding null.

    ``supplied`` is membership and is the right reading where the schema marks
    the position nullable. Where it does not, the client's ``exclude_none``
    drops a null-valued key and ``preserve_explicit_nulls`` restores only
    nullable paths, so the document that arrives does not carry it --
    ``control-media-buy-request``'s ``canceled`` is ``Literal[True]`` and
    ``canceled=None`` is dropped on the way out. Reading it as present refused
    a request no seller receives.

    It adds only that half: whether the document carries the name at all is
    :func:`c.supplied`'s question, and a default the class emits is carried.
    """
    subject = _Subject.model_validate({"packages": ["pkg_1"], "proposal_id": None})

    assert c.sent(subject, "packages")
    assert not c.sent(subject, "proposal_id"), "an explicit null is not sent at a closed position"
    assert c.supplied(subject, "proposal_id"), "and supplied still answers presence"
    assert c.sent(subject, "pacing"), "a default the class emits reaches the seller"
    assert not c.sent(_Dropping(), "tier"), "one its serializer drops does not"
    assert not c.sent("not an object", "packages")


def test_absent_refuses_a_key_the_shape_forbids_and_passes_everything_else() -> None:
    """``not: {required: [x]}`` on a branch class, at wire validation.

    A branch class types a field its arm forbids as ``None``, which is what
    makes the combination unspellable at construction. Under ``extra='allow'``
    that is not what refuses a raw document, so the forbidden names are refused
    here -- and presence is read the way ``sent`` reads it, because the negation
    of ``required`` is about the same document. At a position the schema forbids
    null, the client drops a null-valued key, so only a non-null value is
    carried; at a nullable position a null survives and its presence is what
    the ban forbids. A document carrying none of them, and anything that is not
    a mapping, pass through unchanged.
    """
    assert c.absent({"packages": ["pkg_1"]}, ["proposal_id"]) == {"packages": ["pkg_1"]}
    assert c.absent("not an object", ["proposal_id"]) == "not an object"
    assert c.absent({"proposal_id": None}, ["proposal_id"]) == {
        "proposal_id": None
    }, "a null at a position forbidding null never reaches the document sent"

    with pytest.raises(ValueError, match="proposal_id is not accepted"):
        c.absent({"packages": [], "proposal_id": "prop_1"}, ["proposal_id"])

    with pytest.raises(ValueError, match="daily_budget_cap is not accepted"):
        c.absent({"daily_budget_cap": None}, [], ["daily_budget_cap"])

    with pytest.raises(ValueError, match="budget_allocation, packages"):
        c.absent({"packages": [1], "budget_allocation": {}}, ["packages", "budget_allocation"])


def test_supplied_reads_a_mapping_by_its_keys() -> None:
    """An open payload reaches a predicate as a mapping, not a model."""
    assert c.supplied({"a": None}, "a")
    assert not c.supplied({"a": None}, "b")
    assert not c.supplied("not an object", "a")


def test_descend_is_vacuous_for_a_member_the_document_did_not_carry() -> None:
    """``properties`` grades present object members only -- including a null one.

    An arm constraining ``nested.mode`` says nothing about a request that sent
    no ``nested`` at all. It does say something about one that sent ``null``:
    JSON Schema applies the subschema to whatever value is there, and a
    subschema expecting an object is false for ``null``. Reading the null as
    vacuous instead let ``creative-manifest.json``'s audio-VAST conjunct fire
    on a manifest whose ``format_kind`` was null, refusing a document under a
    rule the schema does not apply to it.
    """
    predicate = lambda value: getattr(value, "mode", None) == "fixed"  # noqa: E731

    assert c.descend(_Subject.model_validate({"nested": {"mode": "fixed"}}), "nested", predicate)
    assert not c.descend(
        _Subject.model_validate({"nested": {"mode": "seller_optimized"}}), "nested", predicate
    )
    assert c.descend(_Subject(), "nested", predicate), "absent is unconstrained"
    assert not c.descend(
        _Subject.model_validate({"nested": None}), "nested", predicate
    ), "an explicit null is a value the subschema grades, and this one fails it"
    # And a predicate JSON Schema reads as vacuous off-type stays vacuous on a
    # null, which is what keeps the composition faithful rather than strict.
    assert c.descend(
        _Subject.model_validate({"nested": None}), "nested", lambda value: c.matches(value, "^x")
    )


def test_every_grades_each_element_and_nothing_else() -> None:
    """``items``."""
    assert c.every([2, 4], lambda value: value % 2 == 0)
    assert not c.every([2, 3], lambda value: value % 2 == 0)
    assert c.every([], lambda value: False), "an empty array satisfies items"
    assert c.every("not an array", lambda value: False)


def test_some_grades_whether_one_element_matches() -> None:
    """``contains``."""
    assert c.some([1, 2], lambda value: value == 2)
    assert not c.some([1, 3], lambda value: value == 2)
    assert not c.some([], lambda value: True), "an empty array contains nothing"
    assert c.some(None, lambda value: False)


def test_keys_grades_the_member_names() -> None:
    """``propertyNames``."""
    assert c.keys({"ab": 1, "ac": 2}, lambda name: name.startswith("a"))
    assert not c.keys({"ab": 1, "zz": 2}, lambda name: name.startswith("a"))
    assert c.keys(7, lambda name: False)


def test_matching_keys_grades_only_the_matching_members() -> None:
    """``patternProperties``."""
    assert c.matching_keys({"x_a": 1, "other": "text"}, "^x_", lambda value: value == 1)
    assert not c.matching_keys({"x_a": 2, "other": "text"}, "^x_", lambda value: value == 1)
    assert c.matching_keys({"other": "text"}, "^x_", lambda value: False)


def test_only_bounds_what_the_document_carries() -> None:
    """``additionalProperties: false``.

    The bound is on what the document carried, not on what the class declares:
    a class normally has more fields than one arm's ``properties`` names, and a
    field the caller left out is not an additional property -- unless the class
    emits its default, which puts it on the document and so within the bound.
    """
    assert c.only(_Subject.model_validate({"packages": []}), ["packages", "pacing"])
    assert not c.only(
        _Subject.model_validate({"packages": []}), ["packages"]
    ), "pacing's emitted default is a member the bound has to name"
    assert not c.only(_Subject.model_validate({"packages": [], "proposal_id": "p"}), ["packages"])
    assert not c.only(
        _Open.model_validate({"declared": 1, "undeclared": 2}), ["declared"]
    ), "a key the model carried through is supplied"
    assert c.only("not an object", [])


def test_others_grades_the_unnamed_members() -> None:
    """``additionalProperties: <schema>``, which is how a map type is spelled."""
    assert c.others({"a": "pass", "b": "fail"}, [], lambda value: value in {"pass", "fail"})
    assert not c.others({"a": "pass", "b": "x"}, [], lambda value: value in {"pass", "fail"})
    assert c.others({"a": "x"}, ["a"], lambda value: False), "a named member is exempt"


@pytest.mark.parametrize(
    ("value", "names", "expected"),
    [
        ("text", ("string",), True),
        (1, ("string",), False),
        (True, ("integer",), False),
        (True, ("boolean",), True),
        (1, ("number",), True),
        (1.5, ("integer",), False),
        ([], ("array",), True),
        ({}, ("object",), True),
        (_Subject(), ("object",), True),
        (None, ("null",), True),
        ("text", ("integer", "string"), True),
        # A compiled ``type`` check runs AFTER validation, so the value it sees
        # is what pydantic parsed, not what the document carried. Every one of
        # these arrived as a JSON string.
        (datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc), ("string",), True),
        (datetime.date(2026, 1, 1), ("string",), True),
        (datetime.timedelta(seconds=5), ("string",), True),
        (uuid.UUID(int=1), ("string",), True),
        (AnyUrl("https://seller.example"), ("string",), True),
        (ipaddress.IPv4Address("10.0.0.1"), ("string",), True),
        # And a JSON number can arrive as a Decimal, which is neither int nor
        # float -- so defining "string" by excluding the other five JSON types
        # would call this one a string.
        (decimal.Decimal("2.5"), ("number",), True),
        (decimal.Decimal("2.5"), ("string",), False),
        (decimal.Decimal("2.5"), ("integer",), False),
        (decimal.Decimal("2"), ("integer",), True),
    ],
)
def test_json_type_answers_the_json_type_names(
    value: Any, names: tuple[str, ...], expected: bool
) -> None:
    """``type``, including the Python-versus-JSON mismatches.

    ``bool`` is a subclass of ``int`` in Python and a separate type in JSON, and
    a pydantic model is a JSON object without being a mapping.

    The third mismatch is the one that shipped broken: a ``format``-bearing
    string field is a ``datetime`` or an ``AnyUrl`` by the time the check runs,
    so an ``isinstance(value, str)`` test is false for every document and the
    model refuses what its schema accepts. Seven fields over six modules were in
    that state on the pin -- ``reporting-revision``'s ``data_through`` among
    them, which made ``tests/test_reporting_consumer_loop.py`` fail at
    COLLECTION.
    """
    assert c.json_type(value, *names) is expected


def test_matches_is_an_unanchored_search() -> None:
    """``pattern``: JSON Schema specifies a partial match, not a full one."""
    assert c.matches("prefix-mid-suffix", "mid")
    assert not c.matches("prefix", "mid")
    assert c.matches(7, "mid"), "pattern says nothing about a non-string"


@pytest.mark.parametrize(
    ("value", "bounds", "expected"),
    [
        ("abc", {"minimum": 3}, True),
        ("ab", {"minimum": 3}, False),
        ([1, 2, 3], {"maximum": 2}, False),
        ([1, 2], {"maximum": 2}, True),
        ({"a": 1}, {"minimum": 1, "maximum": 1}, True),
        ({}, {"minimum": 1}, False),
        (7, {"minimum": 1}, True),
    ],
)
def test_length_bounds_anything_sized_and_nothing_else(
    value: Any, bounds: dict[str, int], expected: bool
) -> None:
    """``minLength``/``maxLength``, ``minItems``/``maxItems``, ``min``/``maxProperties``."""
    assert c.length(value, **bounds) is expected


@pytest.mark.parametrize(
    ("value", "bounds", "expected"),
    [
        (0, {"ge": 0}, True),
        (-1, {"ge": 0}, False),
        (0, {"gt": 0}, False),
        (100, {"le": 100}, True),
        (101, {"le": 100}, False),
        (1, {"lt": 1}, False),
        (True, {"ge": 5}, True),
        ("5", {"ge": 5}, True),
    ],
)
def test_within_bounds_only_numbers(value: Any, bounds: dict[str, float], expected: bool) -> None:
    """``minimum``/``maximum``/``exclusiveMinimum``/``exclusiveMaximum``.

    ``True`` is excluded on purpose: it is an ``int`` subclass in Python and a
    boolean in JSON, so a numeric bound says nothing about it.
    """
    assert c.within(value, **bounds) is expected


def test_unique_compares_unhashable_elements() -> None:
    """``uniqueItems``, over the array of objects that is the common case."""
    assert c.unique([{"a": 1}, {"a": 2}])
    assert not c.unique([{"a": 1}, {"a": 1}])
    assert c.unique([])
    assert c.unique("not an array")


def test_members_counts_what_the_document_carried_not_what_the_model_declares() -> None:
    """``minProperties``/``maxProperties``, which ``length`` cannot measure.

    A pydantic object has no ``__len__``, so routing these two through
    :func:`c.length` sent every object down its ``TypeError`` branch and
    returned True -- the validator existed and graded nothing. The count is
    ``model_fields_set`` plus the extras a model kept plus the defaults the
    class emits, which is the question :func:`c.supplied` asks, counted instead
    of tested.

    That third source is ``minProperties`` stated over a class with an
    ergonomic default: ``core/deadline-policy.json`` asks for one member and
    ``DeadlinePolicy`` emits ``business_days_only``, so the empty construction
    carries one and the count had read nothing (salesagent-k37fs).

    The open-model case is here because it is where this went wrong a second
    time. pydantic records a kept extra in BOTH ``model_fields_set`` and
    ``model_extra``, so naming them by concatenation counted every extra twice
    and ``maxProperties`` refused documents the schema accepts.
    """
    subject = _Subject.model_validate({"packages": ["pkg_1"], "proposal_id": None})

    assert c.members(subject, minimum=3), "packages, proposal_id, and pacing's emitted default"
    assert not c.members(subject, minimum=4)
    assert c.members(subject, maximum=3)
    assert not c.members(subject, maximum=2)
    assert c.members(_Dropping(), maximum=0), "a default the serializer drops is not a member"

    open_subject = _Open.model_validate({"declared": 1, "undeclared": 2})
    assert c.members(open_subject, minimum=2), "an extra the model kept is a member"
    assert not c.members(open_subject, minimum=3), "but it is ONE member, not two"
    assert c.members(open_subject, maximum=2)

    assert c.members({"a": 1, "b": 2}, minimum=2), "a mapping answers from its keys"
    assert not c.members({}, minimum=1)
    assert c.members("not an object", minimum=5), "vacuous off its own type"


def test_every_primitive_the_vocabulary_exports_is_graded_here() -> None:
    """A primitive the generator can emit but nothing grades would be a hole.

    Both halves are derived: the export list against what the module defines,
    and the export list against which primitives the tests above actually call.
    """
    defined = {
        name
        for name, value in vars(c).items()
        if not name.startswith("_")
        and isinstance(value, types.FunctionType)
        and value.__module__ == c.__name__
    }
    # Both populations are asserted non-empty BEFORE they are compared. A set
    # equality whose sides can both go empty grades nothing on that axis, and
    # this one has already been empty: a pytest plugin that wrapped each
    # primitive rebound ``__module__`` to its own module, every entry failed the
    # module test, and ``defined`` came out as ``set()``. It was caught only
    # because ``__all__`` happened to be non-empty at the time.
    assert defined, (
        "the derivation found no primitives in "
        f"{c.__name__} at all, so comparing it against __all__ would grade "
        "nothing. Something has rebound them -- check __module__ on a few: a "
        "wrapper that is not functools.wraps-transparent looks exactly like this"
    )
    assert c.__all__, "__all__ is empty, so both halves below are vacuous"
    assert defined == set(c.__all__)
    graded = {
        name
        for test in globals()
        if test.startswith("test_")
        for name in c.__all__
        if f"c.{name}(" in inspect.getsource(globals()[test])
    }
    assert graded == set(c.__all__), sorted(set(c.__all__) - graded)


def test_name_the_combinator_names_the_keyword_and_keeps_the_fields() -> None:
    """The wrapper a split root's union carries.

    A document that satisfies no branch fails each branch at FIELD validation,
    so pydantic reports ``missing`` and nothing names the root's combinator.
    ``core/error.json`` requires a keyword drawn from the JSON Schema
    vocabulary, so the wrapper supplies one -- and carries the branch TITLES in
    the message and the absent field names beside them in the error context, so
    the refusal says more than the keyword alone.

    The sentence is the one a flat root's compiled ``_require_schema_required_group``
    validator writes, so a root moving between the two mechanisms does not
    change what a reader sees.
    """
    from typing import Annotated, TypeAlias

    from pydantic import BaseModel, TypeAdapter, ValidationError, WrapValidator

    class Shape(BaseModel):
        a: str | None = None
        b: str | None = None

    class First(Shape):
        """Carries a."""

        a: str
        b: None = None

    class Second(Shape):
        """Carries b."""

        b: str
        a: None = None

    Wrapped: TypeAlias = Annotated[
        First | Second, WrapValidator(c.name_the_combinator("anyOf", (First, Second)))
    ]
    adapter: TypeAdapter[First | Second] = TypeAdapter(Wrapped)

    with pytest.raises(ValidationError) as refusal:
        adapter.validate_python({})
    errors = refusal.value.errors()
    assert [error["type"] for error in errors] == ["anyOf"]
    assert errors[0]["ctx"] == {"groups": "Carries a | Carries b", "fields": "a, b"}
    assert "requires at least one of these field groups: Carries a | Carries b" in errors[0]["msg"]

    # With no branches to title, the absent field names stand in for the groups
    # rather than leaving the sentence with an empty list. The emitters all pass
    # the branches; a hand-written wrapper may not.
    bare: TypeAlias = Annotated[First | Second, WrapValidator(c.name_the_combinator("anyOf"))]
    with pytest.raises(ValidationError) as untitled:
        TypeAdapter(bare).validate_python({})
    assert untitled.value.errors()[0]["ctx"] == {"groups": "a, b", "fields": "a, b"}

    # The success path is untouched: a document that satisfies a branch comes
    # back as that branch rather than as the base.
    assert type(adapter.validate_python({"a": "x"})) is First

    # A refusal that is not about an absent field passes through unchanged, so
    # the wrapper never relabels a failure it did not diagnose.
    with pytest.raises(ValidationError) as other:
        adapter.validate_python({"a": 1, "b": 2})
    assert "anyOf" not in {error["type"] for error in other.value.errors()}


def test_name_the_combinator_keeps_a_branchs_content_failure() -> None:
    """A branch that passed its required fields owns the refusal.

    The wrapper names the combinator only when the document satisfies NO
    branch's requiredness. If one branch got past its required fields and then
    failed on content -- a bound, a pattern, a nested rule -- that failure is
    the informative one, and replacing it with ``anyOf`` would discard the only
    error telling the caller what is wrong. Pydantic re-wraps a raise from
    inside a validator, so the original is not recoverable afterwards either.
    """
    from typing import Annotated, TypeAlias

    from pydantic import BaseModel, Field, TypeAdapter, ValidationError, WrapValidator

    class Shape(BaseModel):
        a: str | None = None
        b: str | None = None

    class First(Shape):
        a: Annotated[str, Field(min_length=5)]

    class Second(Shape):
        b: str

    Wrapped: TypeAlias = Annotated[First | Second, WrapValidator(c.name_the_combinator("anyOf"))]
    adapter: TypeAdapter[First | Second] = TypeAdapter(Wrapped)

    # ``a`` is supplied, so First passes requiredness and fails on the bound;
    # Second reports ``b`` missing. The bound is what the caller needs.
    with pytest.raises(ValidationError) as refusal:
        adapter.validate_python({"a": "xy"})
    types = {error["type"] for error in refusal.value.errors()}
    assert "string_too_short" in types, refusal.value.errors()
    assert "anyOf" not in types, "a branch's content failure was replaced by the keyword"

    # And a document satisfying no branch's requiredness still names the keyword.
    with pytest.raises(ValidationError) as none_matched:
        adapter.validate_python({})
    assert [error["type"] for error in none_matched.value.errors()] == ["anyOf"]


def test_a_branch_that_forbids_a_carried_key_is_not_the_branch_the_document_meant() -> None:
    """``_c.absent`` firing is the opposite of getting past one's requiredness.

    ``absent`` is a ``mode="before"`` validator, so a branch forbidding a key the
    document carries raises AHEAD of field validation and reports no requiredness
    error at all. The election rule used to test requiredness alone, so that
    silence read as "this branch got past its required fields" and the branch the
    document meant LEAST owned the refusal -- stating that a key is unacceptable
    when it is exactly what the sibling branch requires (salesagent-klblw).

    THE DOCUMENT HERE IS THE POINT. It carries ``value``, which ``Value``
    requires and ``Range`` forbids, and omits ``dimension``, which both require.
    An EMPTY document cannot grade this: ``absent`` never fires, so no branch is
    elected and the keyword is named correctly either way. Any probe for this
    defect has to satisfy one branch PARTIALLY.
    """
    from typing import Annotated, TypeAlias

    from pydantic import BaseModel, TypeAdapter, ValidationError, WrapValidator, model_validator

    class Shape(BaseModel):
        dimension: str | None = None
        value: int | None = None
        range: int | None = None

    class Value(Shape):
        dimension: str
        value: int

        @model_validator(mode="before")
        @classmethod
        def _refuse(cls, data: Any) -> Any:
            return c.absent(data, ["range"])

    class Range(Shape):
        dimension: str
        range: int

        @model_validator(mode="before")
        @classmethod
        def _refuse(cls, data: Any) -> Any:
            return c.absent(data, ["value"])

    Wrapped: TypeAlias = Annotated[Value | Range, WrapValidator(c.name_the_combinator("anyOf"))]
    adapter: TypeAdapter[Value | Range] = TypeAdapter(Wrapped)

    with pytest.raises(ValidationError) as refusal:
        adapter.validate_python({"value": 1})
    errors = refusal.value.errors()
    assert [error["type"] for error in errors] == ["anyOf"], errors
    # The sentence that used to come out is a statement about the one key the
    # document got RIGHT, so it is named here rather than only excluded by type.
    assert "value is not accepted" not in str(refusal.value), str(refusal.value)

    # A key BOTH branches forbid leaves no field group to report, and then the
    # forbidden-key refusal is the informative one and is kept -- with a ``loc``
    # that is a path in the document, not pydantic's choice label.
    class OnlyValue(Shape):
        dimension: str

        @model_validator(mode="before")
        @classmethod
        def _refuse(cls, data: Any) -> Any:
            return c.absent(data, ["range"])

    Both: TypeAlias = Annotated[
        OnlyValue | OnlyValue, WrapValidator(c.name_the_combinator("anyOf"))
    ]
    both: TypeAdapter[OnlyValue] = TypeAdapter(Both)
    with pytest.raises(ValidationError) as forbidden:
        both.validate_python({"dimension": "d", "range": 1})
    kept = forbidden.value.errors()
    assert {error["type"] for error in kept} == {"not"}, kept
    assert all(error["loc"] == () for error in kept), [error["loc"] for error in kept]


def test_branch_title_reads_the_arm_title_off_the_branchs_docstring() -> None:
    """The one place a branch's schema-derived text lives.

    The emitter opens each branch class's docstring with the arm's ``title``,
    which is why no identifier has to be invented to carry it. A trailing period
    is dropped because the title is interpolated mid-sentence, and a branch with
    no docstring falls back to its class name -- which is itself derived from the
    same title, so the sentence stays readable rather than empty.
    """

    class Titled:
        """Explicit packages with fixed allocation.

        The rest of the docstring is the emitter's explanation and is not part
        of the title.
        """

    class Undocumented:
        pass

    assert c.branch_title(Titled) == "Explicit packages with fixed allocation"
    assert c.branch_title(Undocumented) == "Undocumented"


def _counting_fallback(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count calls to the degenerate-label fallback without changing its answer.

    Whether it FIRED is the only way to tell the two paths apart from outside:
    both end in the same restated document error, and the labels the grouping
    reads are gone from that result by design.
    """
    calls: list[int] = []
    original = c._errors_of_the_reached_branch

    def counting(value: Any, branches: Any, merged: Any) -> Any:
        calls.append(1)
        return original(value, branches, merged)

    monkeypatch.setattr(c, "_errors_of_the_reached_branch", counting)
    return calls


def test_name_the_combinator_survives_degenerate_branch_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A branch's content failure survives when every label is the SAME string.

    The wrapper groups a union's errors by their first ``loc`` element to decide
    which branch the document reached. That assumes pydantic gives each choice a
    DISTINCT label, which nobody guarantees: a branch carrying a WRAP validator
    is spelled ``function-after[..., function-wrap[<validator>()]]`` -- a name
    with no class in it -- so sibling branches inheriting one wrap collapse to a
    single label and a single bucket. The bucket then holds one branch's
    ``missing`` beside another's content failure, every candidate looks
    unreached, and the combinator keyword displaces the informative error.

    Not hypothetical: making the canonical layer's legacy-identity refusal
    ``mode="wrap"`` did exactly this and took 23 tests in
    ``tests/test_targeting_overlay_errors.py`` from passing to a root-level
    ``anyOf`` where a field error belonged.
    """
    from typing import Annotated, TypeAlias

    from pydantic import (
        BaseModel,
        Field,
        TypeAdapter,
        ValidationError,
        WrapValidator,
        model_validator,
    )

    calls = _counting_fallback(monkeypatch)

    class Shape(BaseModel):
        a: str | None = None
        b: str | None = None

        @model_validator(mode="wrap")
        @classmethod
        def _collapses_the_label(cls, value: Any, handler: Any) -> Any:
            # Inherited by both branches, so both are spelled by THIS wrapper
            # and neither name carries its own class.
            return handler(value)

    class First(Shape):
        a: Annotated[str, Field(min_length=5)]

    class Second(Shape):
        b: str

    Wrapped: TypeAlias = Annotated[
        First | Second, WrapValidator(c.name_the_combinator("anyOf", (First, Second)))
    ]
    adapter: TypeAdapter[First | Second] = TypeAdapter(Wrapped)

    # First passes requiredness and fails on the bound; Second reports b missing.
    with pytest.raises(ValidationError) as refusal:
        adapter.validate_python({"a": "xy"})

    # The fixture really is degenerate, asserted rather than assumed: the
    # fallback runs ONLY when one bucket covers several branches. If a future
    # pydantic spells a wrapped choice with its class name this goes to zero and
    # the test stops exercising the case it is named for.
    assert calls == [1], "the fixture is no longer degenerate -- the fallback never ran"

    types = {error["type"] for error in refusal.value.errors()}
    assert "string_too_short" in types, refusal.value.errors()
    assert "anyOf" not in types, "a branch's content failure was replaced by the keyword"
    # And the reported path is the DOCUMENT's, with no label left on it.
    assert [tuple(error["loc"]) for error in refusal.value.errors()] == [("a",)]

    # A document satisfying no branch is still refused by the keyword.
    with pytest.raises(ValidationError) as none_matched:
        adapter.validate_python({})
    assert [error["type"] for error in none_matched.value.errors()] == ["anyOf"]


def test_the_degenerate_label_fallback_does_not_fire_when_labels_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With distinct labels the grouping decides and the fallback never runs.

    Asserted on whether it was CALLED, not on the result. A fallback that fired
    unconditionally would satisfy every other test here while replacing the
    grouping it exists to back up, so the result alone cannot tell the two
    apart -- which is the failure mode this pair is about. Same models as the
    sibling above, minus the wrap validator that collapses the labels.
    """
    from typing import Annotated, TypeAlias

    from pydantic import BaseModel, Field, TypeAdapter, ValidationError, WrapValidator

    calls = _counting_fallback(monkeypatch)

    class Shape(BaseModel):
        a: str | None = None
        b: str | None = None

    class First(Shape):
        a: Annotated[str, Field(min_length=5)]

    class Second(Shape):
        b: str

    Wrapped: TypeAlias = Annotated[
        First | Second, WrapValidator(c.name_the_combinator("anyOf", (First, Second)))
    ]
    adapter: TypeAdapter[First | Second] = TypeAdapter(Wrapped)

    with pytest.raises(ValidationError) as refusal:
        adapter.validate_python({"a": "xy"})

    assert calls == [], "the degenerate-label fallback fired where the grouping was sufficient"
    assert "string_too_short" in {error["type"] for error in refusal.value.errors()}
    assert [tuple(error["loc"]) for error in refusal.value.errors()] == [("a",)]
