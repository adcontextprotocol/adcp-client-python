"""The vocabulary compiled root constraints are written in.

A schema root states more than its merged ``properties``: a root
``anyOf``/``oneOf`` over overlay branches, a root ``if``/``then``/``else``, root
``dependencies``, a root ``not``. ``scripts/schema_constraints.py`` reads that
content and the generator compiles it into a ``model_validator`` on the class,
as compositions of the primitives below. Nothing here is generated; every
function is a few lines, and each is graded one by one by
``tests/test_conditions_vocabulary.py``.

Two conventions run through all of them, and both are measured rather than
chosen.

**Presence is what the DOCUMENT carries.** ``required`` asks whether the document
carries the property, so ``model_fields_set`` answers it for everything the
caller typed -- and an explicit null is one of those, because on an AdCP 3.2
mutation input it is the command to clear a stored value. It is not the whole
answer, because a default the class EMITS is on the document too, and the
document is all a seller can see: the two dumps are byte-identical, so a
constraint whose verdict differs between them is grading something the wire
cannot express (salesagent-k37fs). So an unset field is asked of the serializer,
which is the only authority on what the class emits -- see :func:`_emitted`.

The alternatives were measured and both are wrong: an ``is None`` guard is
correct only when the field's default happens to be ``None``, because a non-None
default makes it false for an absent field and the check over-refuses; and an
unguarded attribute read raises on absence and on an explicit null alike.

**Every predicate is vacuous off-type.** JSON Schema applies a keyword only to
instances of the type it grades: ``minLength`` says nothing about a number,
``properties`` grades present object members only, ``items`` says nothing about
a non-array. So a primitive handed a value of the wrong shape returns ``True``.
That is the schema's own reading, and it is what lets the generator compile a
nested predicate without first resolving the nested annotation.
"""

from __future__ import annotations

import datetime
import decimal
import ipaddress
import pathlib
import re
import typing
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from pydantic import AnyUrl, ValidationError
from pydantic_core import PydanticCustomError, PydanticUndefined
from pydantic_core.core_schema import ErrorType

__all__ = [
    "branch_title",
    "name_the_combinator",
    "absent",
    "descend",
    "every",
    "json_type",
    "keys",
    "length",
    "matches",
    "matching_keys",
    "members",
    "only",
    "others",
    "sent",
    "some",
    "supplied",
    "unique",
    "within",
]

#: What a predicate over one value looks like.
Predicate = Callable[[Any], bool]

#: Python types a JSON STRING arrives as once pydantic has parsed it. A
#: compiled ``type`` check runs after validation, so ``isinstance(value, str)``
#: alone is false for every ``format``-bearing string field -- the model then
#: refuses a document its schema accepts, permanently and silently.
#:
#: Measured on the pin: seven fields over six modules were in exactly that
#: state -- ``reporting-revision``'s ``data_through``, ``search-brands-response``'s
#: ``relationship_verified_at`` and its logo ``url``, and four ``timestamp``
#: fields -- each annotated ``AwareDatetime`` or ``AnyUrl`` and each carrying a
#: root ``type: "string"`` rule it could never satisfy.
#:
#: Enumerated rather than defined by exclusion. "Not object, array, number,
#: boolean or null, therefore string" is true of JSON and wrong here, because
#: a JSON NUMBER also arrives as a non-``float``: pydantic parses one into
#: ``Decimal`` where the field says so, and exclusion would call that a string.
_STRING_BACKED = (
    str,
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    uuid.UUID,
    AnyUrl,
    pathlib.PurePath,
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
)

#: Python types a JSON NUMBER arrives as. ``Decimal`` is neither ``int`` nor
#: ``float``, and a field declared with a multiple-of or a money precision gets
#: one.
_NUMBER_BACKED = (int, float, decimal.Decimal)

#: JSON type names, and the Python instances that answer to them. ``integer``
#: excludes ``bool``, which is a subclass of ``int`` in Python and a separate
#: type in JSON.
_JSON_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda value: isinstance(value, Mapping) or _is_model(value),
    "array": lambda value: isinstance(value, (list, tuple)),
    "string": lambda value: isinstance(value, _STRING_BACKED),
    "integer": lambda value: (
        isinstance(value, (int, decimal.Decimal))
        and not isinstance(value, bool)
        and int(value) == value
    ),
    "number": lambda value: isinstance(value, _NUMBER_BACKED) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _is_model(value: Any) -> bool:
    return hasattr(value, "model_fields_set")


def _emitted(value: Any, name: str) -> bool:
    """Whether ``value``'s own serialization carries ``name`` the caller did not set.

    The serializer is ASKED rather than the default inspected, because the two
    disagree and only the serializer is right. ``GetMediaBuyDeliveryRequest``
    defaults ``include_window_breakdown`` to ``False`` and its wrap serializer
    drops that field when it is unset beside a ``reporting_revision_id``: the
    default exists, the wire does not carry it, and a constraint reading it as
    present would accept a document the schema refuses. Inspecting the default
    gets that case backwards; one ``model_dump`` gets it right, and gets a
    ``default_factory`` and ``exclude_none`` right with it.

    ``include`` narrows the dump to the one field, so this costs that field's
    serialization and not the model's. Narrowing could only change an answer
    where a serializer decides about THIS field by reading another one out of the
    dict it was handed, so it was checked against all three wrap serializers in
    the tree that drop a field, and the narrowed dump agrees with the full one on
    each: two decide from ``self`` -- ``AdCPBaseModel`` reads ``event_types``, and
    flipping it flips this answer in both directions -- and ``Scope``'s reads only
    the key it is deciding about.

    The default check ahead of it is a cost guard, not the question: a field with
    nothing to emit is dropped by ``exclude_none`` anyway, and that is the common
    case on every optional field. Identity rather than ``in``, because a default
    is an arbitrary object and ``==`` against it is the caller's code.
    """
    fields = getattr(type(value), "model_fields", None)
    info = fields.get(name) if fields else None
    if info is None:
        return False
    if info.default_factory is None and (info.default is None or info.default is PydanticUndefined):
        return False
    return name in value.model_dump(mode="json", exclude_none=True, include={name})


def supplied(value: Any, name: str) -> bool:
    """Whether the document carried ``name`` on ``value``.

    A pydantic model answers from ``model_fields_set`` -- the fields the caller
    actually set -- and then from its serializer, because a default the class
    emits is carried by the document just as a supplied value is. A mapping -- an
    open payload the models carry through unmodelled -- answers from its keys.
    """
    if _is_model(value):
        return name in value.model_fields_set or _emitted(value, name)
    if isinstance(value, Mapping):
        return name in value
    return False


def sent(value: Any, name: str) -> bool:
    """Whether ``name`` would be PRESENT on the document that is sent.

    ``supplied`` answers membership, which is the whole question at a position
    the schema marks nullable: there an explicit null is the caller's value and
    the client keeps the key. Where the schema does NOT permit null, the client
    drops the key -- ``model_dump(exclude_none=True)`` removes it and
    ``preserve_explicit_nulls`` restores only nullable paths -- so a null-valued
    field is absent from what is sent, and a constraint reading it as present
    refuses a request no seller receives.

    Still presence, not an ``is None`` test: :func:`supplied` decides whether the
    document carries the name, and this adds only the non-null half.
    """
    return supplied(value, name) and _member(value, name) is not None


def _member(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def descend(value: Any, name: str, predicate: Predicate) -> bool:
    """Apply ``predicate`` to a supplied member of ``value``.

    Vacuous when the member was NOT SUPPLIED, because ``properties`` grades
    present object members and says nothing about a missing one. An explicit
    null is supplied, and the subschema applies to it like any other value:
    ``{"properties": {"format_kind": {"const": "audio_vast"}}}`` is false for
    ``{"format_kind": null}``, not vacuous. Each primitive already reads a
    ``None`` the way JSON Schema does -- ``const`` and ``enum`` compare and
    fail, ``type`` answers ``null``, and a bound on a string or an array is
    vacuous off its own type -- so passing the null through is what makes the
    composition match the schema.

    Reading a null as vacuous here was measured wrong on
    ``creative-manifest.json``: its audio-VAST conjunct tests
    ``format_kind == 'audio_vast'``, a manifest carrying
    ``format_kind: null`` satisfied that test vacuously, and the model refused
    the document under a rule its schema does not apply to it.
    """
    if not supplied(value, name):
        return True
    return predicate(_member(value, name))


def every(value: Any, predicate: Predicate) -> bool:
    """``items``: every element satisfies ``predicate``."""
    if not isinstance(value, (list, tuple)):
        return True
    return all(predicate(element) for element in value)


def some(value: Any, predicate: Predicate) -> bool:
    """``contains``: at least one element satisfies ``predicate``."""
    if not isinstance(value, (list, tuple)):
        return True
    return any(predicate(element) for element in value)


def _named_members(value: Any) -> Iterable[str]:
    """The names the document carried, each once.

    pydantic puts an extra a model KEPT into ``model_fields_set`` as well as
    ``model_extra``, so concatenating the two names every extra twice. Four of
    the five callers quantify over these names and cannot see a duplicate;
    :func:`members` lengths them, and for it a duplicate is a wrong count -- an
    open model carrying two members read as three, so ``maxProperties: 2``
    refused a document the schema accepts. Both sources are still read, so a
    name present in only one of them still counts.

    A default the class emits is a third source, for the reason :func:`supplied`
    gives: ``core/deadline-policy.json`` states ``minProperties: 1`` and
    ``DeadlinePolicy`` defaults ``business_days_only`` to ``False``, so the empty
    construction dumps one member and ``_c.members`` counted none.

    The declared fields are read through ``getattr``, the same way :func:`_emitted`
    reads them, because :func:`_is_model` is duck-typed: a subject can carry
    ``model_fields_set`` and declare no fields, and then :func:`supplied` answers
    for it while an unguarded ``type(value).model_fields`` raises. Every primitive
    has to answer for every subject the predicate admits.
    """
    if isinstance(value, Mapping):
        return list(value)
    if _is_model(value):
        extras = getattr(value, "model_extra", None) or ()
        declared = getattr(type(value), "model_fields", None) or {}
        unset = (name for name in declared if name not in value.model_fields_set)
        emitted = [name for name in unset if _emitted(value, name)]
        return list(dict.fromkeys([*value.model_fields_set, *extras, *emitted]))
    return ()


def keys(value: Any, predicate: Predicate) -> bool:
    """``propertyNames``: every member name satisfies ``predicate``."""
    if not (isinstance(value, Mapping) or _is_model(value)):
        return True
    return all(predicate(name) for name in _named_members(value))


def matching_keys(value: Any, pattern: str, predicate: Predicate) -> bool:
    """``patternProperties``: every member whose name matches satisfies it."""
    if not (isinstance(value, Mapping) or _is_model(value)):
        return True
    expression = re.compile(pattern)
    return all(
        predicate(_member(value, name)) for name in _named_members(value) if expression.search(name)
    )


def only(value: Any, names: Sequence[str]) -> bool:
    """``additionalProperties: false``: only ``names`` were supplied.

    A generated class normally declares more fields than one branch's
    ``properties`` names, so this bounds what the caller supplied rather than
    what the class can hold. An extra key the model carried through (an open
    payload) counts as supplied.
    """
    if not (isinstance(value, Mapping) or _is_model(value)):
        return True
    return set(_named_members(value)) <= set(names)


def others(value: Any, names: Sequence[str], predicate: Predicate) -> bool:
    """``additionalProperties: <schema>``: unnamed members satisfy it."""
    if not (isinstance(value, Mapping) or _is_model(value)):
        return True
    permitted = set(names)
    return all(
        predicate(_member(value, name)) for name in _named_members(value) if name not in permitted
    )


def json_type(value: Any, *names: str) -> bool:
    """``type``: the value is an instance of one of the named JSON types."""
    return any(_JSON_TYPES[name](value) for name in names if name in _JSON_TYPES)


def matches(value: Any, pattern: str) -> bool:
    """``pattern``: an unanchored match, which is what JSON Schema specifies."""
    if not isinstance(value, str):
        return True
    return re.search(pattern, value) is not None


def length(value: Any, minimum: int | None = None, maximum: int | None = None) -> bool:
    """``minLength``/``maxLength`` and ``minItems``/``maxItems``.

    Four bounds on a size, differing only in the type they grade -- and each is
    vacuous off its own type, which a single ``len`` test over anything sized
    reproduces exactly.

    NOT ``min``/``maxProperties``, which this docstring used to claim. A pydantic
    object model has no ``__len__``, so those two returned ``True`` for every
    object: the production existed, the renderer pointed here, and nothing was
    graded. They are :func:`members` now, and the split is necessary rather than
    tidy -- adding a model branch HERE would measure a ``RootModel``'s own single
    field against an array's ``minItems``.
    """
    try:
        size = len(value)
    except TypeError:
        return True
    if minimum is not None and size < minimum:
        return False
    return not (maximum is not None and size > maximum)


def members(value: Any, minimum: int | None = None, maximum: int | None = None) -> bool:
    """``minProperties``/``maxProperties``: how many members the document carried.

    The same question :func:`supplied` asks, counted instead of tested: a
    model answers from ``model_fields_set`` plus its extras plus the defaults it
    emits, a mapping from its keys.

    Vacuous off its own type, like every primitive here. A ``RootModel`` does not
    reach this from a root conjunct -- ``flat_root_shape`` returns ``None`` for a
    map or array root, so no root validator is emitted for one, and its count
    renders as a field constraint on the key or element type instead.
    """
    if not (isinstance(value, Mapping) or _is_model(value)):
        return True
    size = len(list(_named_members(value)))
    if minimum is not None and size < minimum:
        return False
    return not (maximum is not None and size > maximum)


def within(
    value: Any,
    ge: float | None = None,
    le: float | None = None,
    gt: float | None = None,
    lt: float | None = None,
) -> bool:
    """``minimum``/``maximum``/``exclusiveMinimum``/``exclusiveMaximum``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    if ge is not None and value < ge:
        return False
    if le is not None and value > le:
        return False
    if gt is not None and value <= gt:
        return False
    return not (lt is not None and value >= lt)


def unique(value: Any) -> bool:
    """``uniqueItems``: no element equals another.

    Quadratic on purpose. A JSON array's elements need not be hashable -- an
    array of objects is the common case here -- so a set cannot do it, and the
    arrays this grades are bounded by their own ``maxItems``.
    """
    if not isinstance(value, (list, tuple)):
        return True
    seen: list[Any] = []
    for element in value:
        if element in seen:
            return False
        seen.append(element)
    return True


def absent(data: Any, names: Sequence[str], nullable_names: Sequence[str] = ()) -> Any:
    """``not: {required: [...]}``: these keys may not reach the document sent.

    A branch class types a field its branch forbids as ``None``, which is what
    makes the combination unspellable at construction. It is not what refuses
    a raw document: the generated models are ``extra='allow'`` where their
    schemas say ``additionalProperties: true``, so pydantic would file an
    undeclared key under extras and accept. So the shape's forbidden names are
    refused here, before validation.

    Presence is read the same way ``sent`` reads it, because the negation of
    ``required`` is about the same document. A name in ``names`` sits at a
    position the schema forbids ``null``, so the client drops a null-valued key
    and only a non-null value is carried. A name in ``nullable_names`` sits at
    a position where a null survives, so its mere presence is what the ban
    forbids.

    Returned unchanged when the document carries none of them, and when it is
    not a mapping at all (a model being revalidated has already been through
    this).
    """
    if not isinstance(data, Mapping):
        return data
    carried = sorted(
        [name for name in names if data.get(name) is not None]
        + [name for name in nullable_names if name in data]
    )
    if carried:
        # The keyword is the schema construct this compiles, named at the raise
        # site because only the raise site knows it. A bare ``ValueError``
        # reaches pydantic as ``value_error``, which has no JSON Schema
        # equivalent, so the projection falls back to ``validation`` -- and
        # ``core/error.json`` requires every ``issues[]`` entry's keyword to be
        # "drawn from the JSON Schema vocabulary", which ``error-handling.mdx``
        # states as a MUST. The forbidden names ride in the context rather than
        # in a sentence, the way ``name_the_combinator`` carries the absent
        # ones, so a consumer reads them without parsing text and the wire
        # message stays the one ``safe_message_for`` authors.
        raise PydanticCustomError(
            "not",
            "{fields} is not accepted by this request shape",
            {"fields": ", ".join(carried)},
        )
    return data


def branch_title(branch: type) -> str:
    """The schema title of the branch a branch class was compiled from.

    The emitter opens each branch's docstring with the branch's ``title``, which is
    where schema-derived text rides -- no identifier is invented for it, and the
    title is the only phrase a reader can match against the schema. A branch
    with no docstring falls back to its class name, which is itself derived from
    that title.
    """
    first_line = (branch.__doc__ or "").strip().splitlines()
    return first_line[0].rstrip(".") if first_line else branch.__name__


#: The error types ``ValidationError.from_exception_data`` resolves by name.
#: ``ErrorType`` is the public enumeration of them; a type outside it is a
#: ``PydanticCustomError`` someone minted, and has to be re-minted rather than
#: named.
_KNOWN_ERROR_TYPES = frozenset(typing.get_args(ErrorType))


def _is_known(error: Any) -> bool:
    """Whether ``from_exception_data`` can rebuild this error FROM ITS NAME.

    The name is not enough on its own. A custom error may REUSE a known name
    with its own context: ``EmailStr`` reports ``value_error`` carrying
    ``{'reason': ...}``, while pydantic's own ``value_error`` carries
    ``{'error': <exception>}`` and refuses to be built without it. Naming such
    an error raises ``TypeError: ValueError: 'error' required in context``,
    which is how this was found -- three parity tests, after the rest of the
    suite was green.

    So the record itself is asked: ``errors()`` attaches ``url``, the
    documentation link for a known type, and omits it for a custom one.
    Measured on all four combinations -- ``int_parsing`` (known, url),
    ``EmailStr``'s ``value_error`` (custom, no url), a compiled ``anyOf``
    (custom, no url), and a bare ``ValueError`` (known, url). The name is kept
    as a second condition, so an error with neither falls to the re-mint, which
    always builds.
    """
    return error["type"] in _KNOWN_ERROR_TYPES and "url" in error


def _is_an_absent_root_key(error: Any) -> bool:
    """Whether a union error says a key of the ROOT OBJECT is absent.

    Inside a union every ``loc`` opens with the label of the choice that
    produced it, so a branch's own root properties sit at length two and
    anything longer is a position inside the document. A ``missing`` deeper
    than that is content, not requiredness: an object nested three levels down
    lacking a field ITS schema requires says this branch was reached and the
    document is wrong inside it.

    This says WHERE, not WHY. Whether such a key is required by the SCHEMA ROOT
    or only by the branch that reported it is a fact about the schema, not about
    the error -- which is why the root's required set is passed in rather than
    inferred here. This was named ``_is_branch_requiredness`` while it was read
    as the latter.
    """
    return error["type"] == "missing" and len(error["loc"]) == 2


def _declines_the_document(error: Any, *, label_depth: int) -> bool:
    """Whether this error says the branch was never REACHED.

    Two ways a branch declines the document rather than finding a fault inside
    it, and both report at the branch's own root:

    * a required field of its own is absent -- ``missing`` one level below that
      root;
    * it FORBIDS a key the document carries -- ``_c.absent`` raises
      ``PydanticCustomError("not", ...)`` as a whole-document error AT that
      root.

    ``label_depth`` is the only difference between the two coordinate systems
    this is asked in, which is why it is one function. A branch asked DIRECTLY
    reports a document path, so its root is ``()`` and depth is 0. Inside the
    union pydantic prefixes every ``loc`` with the choice's label, so the same
    root is ``(label,)`` and depth is 1. Writing the rule twice is how the two
    call sites came to disagree: the union path tested only requiredness, so a
    branch whose ``_c.absent`` had fired read as REACHED and its refusal
    displaced the error of the branch the document actually meant, and the
    root's combinator keyword with it (salesagent-klblw).

    THE TYPE TEST ON ``not`` IS LOAD-BEARING, and the previous rule's untyped
    "any error at the branch's own root" is why. A branch that IS reached and
    fails its inherited root conjunct ALSO reports at that root -- measured,
    ``PydanticCustomError("oneOf", ...)`` at ``loc ()`` on six branches when
    asked directly. Reading that as "declined" is backwards: the branch got
    past its own requiredness and the conjunct is the informative failure.
    ``_c.absent`` is the one site that raises ``not`` (``absent`` above names
    the keyword at the raise site precisely so it is identifiable here), and a
    conjunct raises ``allOf``/``anyOf``/``oneOf``, so the keyword separates
    them exactly.
    """
    loc = error["loc"]
    if error["type"] == "missing" and len(loc) == label_depth + 1:
        return True
    return error["type"] == "not" and len(loc) == label_depth


def _errors_of_the_reached_branch(
    value: Any, branches: Sequence[type], merged: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """The subset of ``merged`` belonging to the branch the document reached.

    Used only when the union's labels are degenerate, so the merged list holds
    every branch's errors under one key and there is nothing to group by. The
    branches are asked DIRECTLY to decide which of them got past its own
    requiredness; their own errors then identify which merged entries are that
    branch's, matched on the error's type and its path below the label.

    Selection only: what is reported is still the union's own errors, in the
    caller's input mode. A branch asked directly reports a document path, so its
    ``loc`` is the merged ``loc`` minus the label -- which is the match key.

    Returns ``None`` when it cannot decide, and the caller then keeps the
    ungrouped bucket: no branch was reached, two were, or a branch's direct
    verdict disagrees with the union's. A wrong narrowing is worse than none.
    """
    reached: list[Counter[tuple[Any, ...]]] = []
    for branch in branches:
        try:
            branch.__pydantic_validator__.validate_python(value)  # type: ignore[attr-defined]
        except ValidationError as branch_exc:
            own = branch_exc.errors()
            if any(_declines_the_document(error, label_depth=0) for error in own):
                continue
            reached.append(Counter(tuple(error["loc"]) for error in own))
        else:
            return None  # accepts on its own while the union refused: do not guess
    if len(reached) != 1:
        return None
    # A MULTISET, not a set. Sibling branches report the SAME nested error when
    # they share the field it is under -- two branches of the create request
    # both validate ``packages``, so the merged list holds two identical
    # ``string_type`` entries for one bad ``geo_countries`` element. Matching on
    # membership kept both and the caller received the one real fault twice.
    # Taking at most as many copies as the branch itself reported drops the
    # sibling's duplicate and keeps a genuine repeat -- a document with a
    # repeated JSON key does produce two errors at one path, and that is the
    # branch's own count.
    remaining = reached[0]
    selected: list[dict[str, Any]] = []
    for error in merged:
        key = tuple(error["loc"])[1:]
        if remaining[key] > 0:
            remaining[key] -= 1
            selected.append(error)
    return selected or None


def _in_the_document(error: Any) -> Any:
    """One union error restated with a ``loc`` that is a path in the document.

    Drops the choice label positionally, which is where it always is, rather
    than recognising it by spelling. Everything else -- the type, the context a
    message is rendered from, the input -- is pydantic's own, so the restated
    error is identical to the one the branch would have reported had it been
    asked alone, in the mode the caller used.
    """
    detail: dict[str, Any] = {
        "type": error["type"],
        "loc": tuple(error["loc"][1:]),
        "input": error["input"],
    }
    context = error.get("ctx")
    if _is_known(error):
        if context is not None:
            detail["ctx"] = context
    else:
        # A custom refusal -- a compiled root constraint, or a library's own.
        # Its context is already substituted into the message it rendered, so
        # the message IS the template; a brace left in it stays literal, which
        # is measured rather than assumed.
        detail["type"] = (
            PydanticCustomError(error["type"], error["msg"], context)
            if context is not None
            else PydanticCustomError(error["type"], error["msg"])
        )
    return detail


def name_the_combinator(
    keyword: str,
    branches: Sequence[type] = (),
    root_required: frozenset[str] = frozenset(),
) -> Callable[[Any, Any], Any]:
    """Build the wrapping validator a split root's union carries.

    The refusal reads the same whether the root was SPLIT into branch classes or
    left flat with a compiled ``_require_schema_required_group`` validator:
    "requires at least one of these field groups: <title> | <title>". A root
    moving from one mechanism to the other must not change what a reader sees,
    and when the split landed this message did -- which is what
    ``test_create_media_buy_request_requires_one_root_anyof_group`` caught.

    ``core/error.json`` requires every ``issues[]`` entry to carry a ``keyword``
    drawn from the JSON Schema vocabulary, and tells a consumer not to invent
    one. A document that satisfies no branch fails each branch at FIELD
    validation, so pydantic reports ``missing`` and nothing names the root's
    combinator. ``missing`` is not in that vocabulary, so a consumer assembling
    a conformant envelope has nothing valid to put in the field.

    A model validator on the shared base cannot supply it: field validation
    fails first and an after-validator never runs. A wrapper around the UNION
    runs, because it sits where the union's own validation happens.

    The absent field names travel with the keyword, in the message and in the
    error context, so the refusal says more than a bare combinator keyword
    says. The success path is untouched: a document that satisfies a branch
    returns that branch's instance.

    A REFUSAL IS THE BRANCH'S OWN, NOT THE UNION'S. A pydantic union prefixes
    every error's ``loc`` with the label of the choice that produced it: a plain
    model's class name, and for a branch carrying a compiled validator
    pydantic's own schema spelling,
    ``function-after[_require_schema_required_group(), <Branch>]``. ``loc`` is
    read as a path through the buyer's document -- ``core/error.json`` specifies
    ``issues[].pointer`` as an RFC 6901 pointer into the request payload -- and
    a choice label is a fact about this package's classes, not a key the buyer
    sent. Measured: a union of two plain models reports
    ``('PlainA', 'packages', 0, 'geo', 0)`` for a document error at
    ``/packages/0/geo/0``, and the same union over the same models carrying a
    model-level validator reports
    ``('function-after[_require_schema_required_group(), WrappedA]', ...)``. So
    the union is the element's source and the validator only changes its
    spelling.

    So a refusal is restated as the refusal of ONE branch -- the one the
    document meant -- with the label dropped. Nothing downstream has to
    recognise a label, and the two spellings stop being a distinction anything
    depends on.

    ``root_required`` NAMES THE FIELDS THE SCHEMA ROOT REQUIRES, and without it
    this function cannot tell the two reasons a key is absent apart. A branch
    reports ``missing`` at the root object for a key it SELECTS BY and for a key
    the ROOT requires alike, and the second says the document is wrong at the
    root rather than that no branch was elected: refusing it with the combinator
    alone puts the only issue at the document root, so ``field`` translates to
    the empty string and is omitted, while ``core/error.json`` says a seller
    "MUST also populate field from issues[0]" whenever ``issues`` is present
    (salesagent-8qela.116).

    It is a PARAMETER rather than something read off a branch because the
    distinction is not derivable from the errors -- measured against each root's
    pinned ``required`` list, "absent in every branch implies root-required"
    agrees on 8 of 18 roots and fails in both directions. The generated base
    declares it as ``_ROOT_REQUIRED`` and both the mixin and the emitted union
    alias pass that one declaration; a hand-written wrapper passes nothing and
    gets today's behaviour, exactly as it does for ``branches``.
    """

    def _wrap(value: Any, handler: Any) -> Any:
        try:
            return handler(value)
        except ValidationError as exc:
            # Group by branch: in a union's errors the first ``loc`` element
            # names the branch that produced them.
            per_branch: dict[str, list[dict[str, Any]]] = {}
            for error in exc.errors():
                if not error["loc"]:
                    raise
                per_branch.setdefault(str(error["loc"][0]), []).append(dict(error))
            if len(per_branch) == 1 and len(branches) > 1:
                # DEGENERATE LABELS: the grouping above carries no information.
                # It assumes pydantic gives each union choice a DISTINCT label,
                # which is not a property anyone guarantees. A branch that gains
                # a WRAP validator is spelled
                # ``function-after[..., function-wrap[<validator>()]]`` -- a name
                # with no class in it -- so every branch collapses to one label
                # and one bucket. Measured: making the canonical layer's legacy
                # refusal ``mode="wrap"`` did exactly that, all three branches of
                # the create request arrived under one key, the bucket contained
                # another branch's ``missing``, and the combinator refusal
                # displaced the document error in 23 tests.
                #
                # WHICH SPELLINGS COLLAPSE, measured on the same two branches, so
                # the next validator added to a branch can be checked against
                # this rather than against the one case that bit us:
                #
                #     plain branches         ['First', 'Second']          distinct
                #     after-validator only   ['function-after[f(), First]',
                #                             'function-after[f(), Second]']
                #                                                         distinct
                #     WRAP only              ['function-wrap[f()]']       DEGENERATE
                #     after over wrap        ['function-after[f(),
                #                              function-wrap[f()]]']      DEGENERATE
                #
                # So the hazard is ANY wrap in a branch's schema, not one
                # particular validator.
                #
                # So when the labels are degenerate, the branches are asked
                # directly -- for SELECTION ONLY. The reported errors still come
                # from the union, which is what keeps them in the caller's input
                # mode; a branch's own errors are used to recognise WHICH of the
                # merged errors are its.
                selected = _errors_of_the_reached_branch(
                    value, branches, next(iter(per_branch.values()))
                )
                if selected is not None:
                    per_branch = {"": selected}
            # Report ONE branch's errors when the document satisfies that
            # branch's requiredness. A branch that failed on fields IT requires
            # was never reached; one that got past its own requiredness and
            # failed on CONTENT is the branch the document meant, and that
            # failure is what the caller needs -- replacing it with the
            # combinator keyword would discard the only informative error, and
            # merging it with the other branches' complaints would publish
            # contradictory pointers, because branches differ by which fields
            # they FORBID.
            reached = [
                errors
                for errors in per_branch.values()
                if not any(_declines_the_document(error, label_depth=1) for error in errors)
            ]
            if reached:
                # CEILING: the branches are pairwise disjoint, so at most one
                # can get past its own requiredness. If two ever did, the first
                # in the root's own branch order is reported.
                raise ValidationError.from_exception_data(
                    exc.title, [_in_the_document(error) for error in reached[0]]
                ) from exc
            # One entry per absent root key, keeping the first branch's error
            # for it: every branch reports the same ``missing`` for a key the
            # ROOT requires, and the records are interchangeable.
            at_the_root: dict[str, dict[str, Any]] = {}
            for errors in per_branch.values():
                for entry in errors:
                    if _is_an_absent_root_key(entry):
                        at_the_root.setdefault(str(entry["loc"][1]), entry)
            missing_root_keys = [
                entry for name, entry in sorted(at_the_root.items()) if name in root_required
            ]
            if missing_root_keys:
                # THE DOCUMENT IS WRONG AT THE ROOT, which no branch can fix by
                # being elected -- so this is not "no branch matched" and must
                # not be reported as it. Those misses go out LOCATED, which is
                # what gives ``issues[0]`` a pointer and ``field`` a value, with
                # the combinator beside them so the keyword is not lost: the
                # jsonschema oracle reports the same two things for the same
                # document, and the two lanes disagreeing about WHERE a document
                # is wrong is the defect (salesagent-8qela.116).
                raise ValidationError.from_exception_data(
                    exc.title,
                    [_in_the_document(error) for error in missing_root_keys]
                    + [
                        _no_branch_matched(
                            keyword,
                            branches,
                            [name for name in sorted(at_the_root) if name not in root_required],
                        )
                    ],
                ) from exc
            absent = sorted(at_the_root)
            if not absent:
                # NO branch was reached and none of them named an absent field, so
                # there is no field group to report -- every branch declined for a
                # key the document CARRIES. That is the forbidden-key refusal, and
                # it is the informative one, so it is reported rather than replaced.
                #
                # Restated through ``_in_the_document`` rather than re-raised bare.
                # A bare ``raise`` republishes the union's own errors, whose ``loc``
                # still carries pydantic's choice label -- measured on the create
                # request's both-keys document:
                # ``loc=('function-after[_require_schema_required_group(),
                # function-wrap[_reject_legacy_creative_identity()]]',)``. That
                # label is a fact about this package's schema spelling, not a key
                # the buyer sent, and ``core/error.json`` specifies
                # ``issues[].pointer`` as a pointer into the request payload
                # (salesagent-8qela.28). Before the reached-filter was corrected
                # this path was unreachable for that document -- the bucket read as
                # REACHED and went out label-stripped -- so fixing the filter is
                # what exposed the bare raise.
                raise ValidationError.from_exception_data(
                    exc.title,
                    [_in_the_document(error) for errors in per_branch.values() for error in errors],
                ) from exc
            raise _no_branch_matched(keyword, branches, absent)["type"] from exc

    return _wrap


def _no_branch_matched(
    keyword: str, branches: Sequence[type], absent: Sequence[str]
) -> dict[str, Any]:
    """The combinator refusal, as an error detail.

    A detail rather than a bare ``PydanticCustomError`` because it is raised on
    its own AND appended beside the located root-required misses, and writing
    the sentence twice is how the two spellings would come to differ.

    ``absent`` is the BRANCH-SELECTING keys only when a root-required key is
    also absent, because a reader shown "requires at least one of these field
    groups" alongside a list mixing the two cannot tell which keys would have
    elected a branch. That mixing is what the refusal published before:
    ``fields: account, brand, budget_allocation, end_time, idempotency_key,
    start_time, total_budget`` for the create request, five root-required and
    two branch-required in one list.
    """
    groups = " | ".join(branch_title(branch) for branch in branches)
    names = ", ".join(absent)
    return {
        "type": PydanticCustomError(
            keyword,
            "requires at least one of these field groups: {groups}",
            # The absent field names travel beside the titles in the error
            # context, so a consumer can report which keys were missing
            # without parsing the sentence.
            {"groups": groups or names, "fields": names},
        ),
        "loc": (),
        "input": None,
    }
