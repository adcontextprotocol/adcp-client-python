"""One reader for a schema root's constraint content.

``datamodel-code-generator`` renders a schema root's ``properties`` and drops the
rest of what the root states: a root ``anyOf``/``oneOf`` over overlay branches, a root
``if``/``then``/``else``, root ``dependencies``, a root ``not``. Each dropped
keyword is a document the generated model accepts and the canonical schema
refuses.

This module is the only code that reads that content. It produces one
:class:`RootConstraints` record per schema root, and every consumer -- the
validator pass, the response-branch emitter, the union-branch naming rule -- takes the
record and nothing else from the node, so a second divergent reading of the same
node has nothing to call.

Parsing is key-consuming
------------------------

For every node it visits the parser accounts for every key as exactly one of:

* **Compiled** -- consumed by a grammar production, producing a :data:`Cond`.
* **Non-asserting** -- a member of :data:`NON_ASSERTING_KEYS`, a named frozen
  set of keys that carry no validation semantics by JSON Schema's own
  definition (annotations, identifiers, the ``$defs`` container, an ``ajv``
  message override).
* **Declared behavioral** -- exactly ``x-adcp-validation``, declared outside the
  structural models' promise by ``docs/validation-contract.md`` and carried by
  canonical wire validation plus targeted hand-written validators. The parser
  records the classification on the record; it is a contract citation, not a
  per-schema tolerance entry.
* **Proven restatement** -- a declarative keyword (``type``, ``$ref``,
  ``format``, ``additionalProperties``) or a whole property re-declaration that
  resolves equal to what the generated class already declares for that
  position. Equality is checked, never assumed; a narrowing compiles as a value
  predicate instead.

Anything else raises :class:`UnexpressibleKeywordError`, which aborts generation
naming the file, the JSON pointer and the keyword. There is no list of
known-dropped keywords anywhere in this module: a keyword is compiled,
classified by a named rule, or fatal. That is what makes "the model is weaker
than its schema, silently" unconstructible rather than detected.

What "what the generated class already declares" means here
-----------------------------------------------------------

``datamodel-code-generator`` renders a root's merged ``properties`` and nothing
else, so the declaration for a position inside root constraint content is the
schema node at the same position under the root's own ``properties`` -- which is
what the parser descends in parallel with the constraint node. Field *names* are
a separate question, because ``model_fields_set`` holds field names and a schema
states wire names; :class:`DeclaredShape` carries that map, taken from the
generated class's ``model_fields``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Union

#: Resolves the document half of a ``$ref`` to a parsed schema, or ``None``.
ResolveDocument = Callable[[str], Any]

__all__ = [
    "AllOf",
    "AnyOf",
    "Branch",
    "bundle_document_resolver",
    "COMPILED_KEYWORDS",
    "DROPPED_KEYWORDS",
    "Cond",
    "DECLARED_BEHAVIORAL_KEY",
    "DeclaredShape",
    "DependentRequired",
    "EveryItem",
    "IfThen",
    "KeysSatisfy",
    "MatchingKeysSatisfy",
    "NON_ASSERTING_KEYS",
    "NoOtherProps",
    "Not",
    "OtherPropsSatisfy",
    "OneOf",
    "branch_conditions",
    "partitions_by_requiredness",
    "permits_null",
    "PropCount",
    "PropSatisfies",
    "provably_disjoint",
    "read_root_constraints",
    "Restated",
    "root_carries_constraints",
    "root_combinator",
    "root_required_names",
    "RootConstraints",
    "SomeItem",
    "Supplied",
    "Undeclarable",
    "UndisjointOneOfError",
    "UnexpressibleKeywordError",
    "UnplaceableConstraintError",
    "ValueBounds",
    "ValueConst",
    "ValueEnum",
    "ValueItems",
    "ValueLen",
    "ValuePattern",
    "ValueType",
    "ValueUnique",
]


class UnexpressibleKeywordError(Exception):
    """A keyword inside root constraint content that the grammar cannot compile.

    Raising this aborts generation. ``generate_types.py`` generates into a
    staging tree and installs only on success, so the checkout stays clean and
    the previous generated tree stays in place.
    """

    def __init__(self, schema_file: str, pointer: str, keyword: str) -> None:
        self.schema_file = schema_file
        self.pointer = pointer
        self.keyword = keyword
        super().__init__(
            f"{schema_file}#{pointer or '/'}: root constraint content carries "
            f"{keyword!r}, which the constraint grammar cannot compile. Either add a "
            f"grammar production for it in scripts/schema_constraints.py (one Cond "
            f"node, one compiler case, one constructed-node test), or classify it by "
            f"name if it carries no validation semantics. There is no skip lane."
        )


class UnplaceableConstraintError(Exception):
    """A root constraint names a field no class for that schema declares.

    That is a generator field-drop surfacing loudly, not something to work
    around: the constraint cannot be placed because the field it grades does not
    exist on the model.
    """

    def __init__(self, schema_file: str, pointer: str, name: str) -> None:
        self.schema_file = schema_file
        self.pointer = pointer
        self.name = name
        super().__init__(
            f"{schema_file}#{pointer or '/'}: root constraint content names property "
            f"{name!r}, which the generated class for this schema does not declare. "
            f"A constraint that cannot be placed is a dropped field, not a keyword to "
            f"classify."
        )


#: Keys that carry no validation semantics at the position they appear, each
#: named for the reason it is inert. This is the whole set: a key outside it is
#: compiled or fatal.
NON_ASSERTING_KEYS = frozenset(
    {
        # Annotations: JSON Schema defines these as having no validation effect.
        "title",
        "description",
        "deprecated",
        "examples",
        "$comment",
        "default",
        "readOnly",
        "writeOnly",
        # Identifiers and the subschema container. ``$defs``/``definitions``
        # hold subschemas reachable only through a ``$ref``; at their own
        # position they assert nothing.
        "$schema",
        "$id",
        "$defs",
        "definitions",
        # OpenAPI's serialization hint for a union. It selects which branch a
        # document is read as; it adds no constraint of its own.
        "discriminator",
        # ``ajv``'s message override for a sibling keyword's failure. The
        # sibling keyword is what asserts.
        "errorMessage",
        # JSON Schema makes ``format`` an annotation by default -- an assertion
        # only under the format-assertion vocabulary -- and the SDK's models
        # carry format semantics in a field's own annotation where pydantic has
        # them at all. The formats pydantic lacks (``uri-template``) are a
        # pre-existing type-level looseness carried by canonical wire
        # validation, outside this defect class and named in the design's
        # coverage table rather than silently absorbed here.
        "format",
        # Per-enum-value documentation, and a maintainer's note. Neither is a
        # JSON Schema keyword; both are text alongside the sibling that
        # asserts.
        "enumDescriptions",
        "enumMetadata",
        "notes",
        # AdCP annotations.
        "x-entity",
        "x-status",
        "x-added-in",
        "x-deprecated-in",
        "x-removed-in",
        # Two boolean markers on a position, both ``true`` wherever the bundle
        # writes them: ``x-accessibility`` marks a field as accessibility
        # material, ``x-extensible`` marks an enum as open to registry
        # additions. Neither says anything about an instance -- the sibling
        # ``enum`` is what asserts.
        "x-accessibility",
        "x-extensible",
        # Marks a field whose object value is carried through unmodelled.
        # A rendering instruction, not a constraint on the document.
        "x-adcp-open-payload",
        # Instructions to THIS generator and to a reader of the bundle, not
        # statements about an instance: where to hoist a nested model, what to
        # inline into model context, which schema URI a node came from, which
        # schema is SDK-local, and which result schema a task type overrides.
        "x-adcp-hoist",
        "x-adcp-model-context-inline",
        "x-adcp-schema-uri",
        "x-adcp-sdk-local",
        "x-task-result-schema-overrides",
        # Protocol and registry metadata about the TOOL or the enum, each with
        # a sibling that does the asserting. ``x-pattern`` is the shape a
        # registry addition to an ``x-extensible`` enum should take -- the
        # sibling ``enum`` is what the canonical validator applies, so treating
        # it as an assertion would make the model STRICTER than the bundled
        # schema, which the differential parity suite grades.
        "x-pattern",
        "x-enum-descriptions",
        "x-deprecated-enum-values",
        "x-deprecated-enum-values-doc",
        "x-entity-definitions",
        "x-mutates-state",
        "x-governed-commitment",
        "x-idempotency-key-required",
        "x-operation-family",
        "x-tool-summary",
        "x-superseded-by",
        "x-legacy-fallback",
        "x-compatibility-projection-only",
        # Units for a sibling size bound, and how to canonicalize a value for
        # signing. Neither bounds what the document may contain.
        "x-adcp-file-size-units",
        "x-canonicalization",
        # A note stating, in prose, that the canonical format schemas do NOT
        # mark parameter fields required. It declares a non-enforcement.
        "x-canonical-policy-required-params-not-enforced",
    }
)

#: The one key whose rules are declared outside the structural models' promise.
DECLARED_BEHAVIORAL_KEY = "x-adcp-validation"

#: Every keyword the grammar has a production for. Each member is graded twice
#: by ``tests/test_schema_constraints.py``: a constructed node carrying it must
#: compile, and the fixture map must cover exactly this set, so a production
#: added without a test and a claim made without a production both fail.
COMPILED_KEYWORDS = frozenset(
    {
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "contains",
        "dependencies",
        "dependentRequired",
        "dependentSchemas",
        "else",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "if",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "not",
        "oneOf",
        "pattern",
        "patternProperties",
        "properties",
        "propertyNames",
        "required",
        "then",
        "type",
        "uniqueItems",
    }
)

#: Whole-object and whole-array keywords ``datamodel-code-generator`` DROPS: it
#: has no field to hang them on, so nothing in the generated class carries them.
#:
#: They are the reason this set has to be named at all. Everything else the
#: grammar compiles is either a logic key -- which the generator also drops, and
#: which :data:`LOGIC_KEYS` already names -- or a declarative key the generator
#: renders as a field constraint, where the restatement rule correctly reports it
#: as already enforced. These six are in neither category: declarative in form,
#: dropped in fact. Treating them as declarative is what shut all three doors
#: below, each independently sufficient to lose the constraint:
#:
#: * :func:`_states_logic` did not admit a root carrying one, so its content was
#:   never examined (salesagent-09ghh, and the same shape as the ``allOf`` gate
#:   closed by a2989c205);
#: * :meth:`_Parser.read` read the root's combinator, ``if``, ``dependencies``,
#:   ``dependentRequired``, ``not`` and ``allOf`` members -- so a top-level
#:   ``minProperties`` sat in no position it looked at, and widening the gate
#:   alone produced ten admitted roots and ten EMPTY validators;
#: * :func:`_declaration_of` kept them as "what the generator renders", so the
#:   restatement rule at :meth:`_Parser._keyword` reported them already enforced
#:   -- which would have defeated a fix to the first two for every ``allOf``
#:   member case.
#:
#: ``uniqueItems`` is in the set because the gate must admit its roots, not
#: because a root conjunct can carry it: an array root renders as a ``RootModel``
#: with no object field, so ``flat_root_shape`` returns ``None`` and no root
#: validator is emitted. Putting it on the element type is salesagent-pizni.
DROPPED_KEYWORDS = frozenset(
    {
        "contains",
        "maxProperties",
        "minProperties",
        "patternProperties",
        "propertyNames",
        "uniqueItems",
    }
)

#: Keys whose presence makes a node a constraint rather than a declaration. The
#: generator renders a node's declarative keys and drops these.
LOGIC_KEYS = frozenset(
    {
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "dependencies",
        "dependentRequired",
        "dependentSchemas",
    }
)

#: The key :func:`_declaration_of` adds to carry every ``$ref`` a node declares,
#: its own and its ``allOf`` members'. A class inherits from every member, so a
#: restatement of any member's reference is already declared. Not a JSON Schema
#: keyword, so it collides with nothing a schema can write.
_DECLARED_REFS = "$refs"


# --------------------------------------------------------------------------- #
# The grammar
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Supplied:
    """``required``: the field is present on the document that is sent.

    Presence is ``model_fields_set`` membership, never an ``is None`` test: a
    default the caller never sent is not supplied even when it is not ``None``.

    ``nullable`` says whether the SCHEMA permits ``null`` at this position, and
    it is what membership alone cannot carry. Where null is permitted, an
    explicit null is the caller's value -- on a mutation input, the command to
    clear -- and the client keeps the key on the wire, so membership is the
    whole question. Where it is not, ``exclude_none`` drops the key and
    ``preserve_explicit_nulls`` does not restore it, so the document that is
    SENT does not carry it and a check reading it as present refuses a request
    no seller receives. Measured on ``control-media-buy-request``: ``canceled``
    is ``Literal[True]``, and ``canceled=None`` with a real mutation beside it
    is dropped on the way out.

    Defaults to ``False`` because that is what most positions declare -- a
    property with a plain ``type`` forbids null -- so a constructed node reads
    as the common case and only a nullable position spells it out. The reader
    never relies on the default: it derives the value from the declaration it
    is already walking.
    """

    name: str
    nullable: bool = False


@dataclass(frozen=True)
class Not:
    operand: Cond


@dataclass(frozen=True)
class AllOf:
    operands: tuple[Cond, ...]


@dataclass(frozen=True)
class AnyOf:
    operands: tuple[Cond, ...]


@dataclass(frozen=True)
class OneOf:
    operands: tuple[Cond, ...]


@dataclass(frozen=True)
class IfThen:
    """``if``/``then``/``else`` as an implication over the condition."""

    condition: Cond
    consequent: Cond | None
    alternative: Cond | None


@dataclass(frozen=True)
class PropSatisfies:
    """Descend into a supplied value.

    Vacuously true when the property was not supplied or is an explicit null:
    JSON Schema's ``properties`` grades present object members only.
    """

    name: str
    operand: Cond


@dataclass(frozen=True)
class ValueConst:
    value: Any


@dataclass(frozen=True)
class ValueEnum:
    values: tuple[Any, ...]


@dataclass(frozen=True)
class ValueType:
    types: tuple[str, ...]


@dataclass(frozen=True)
class ValuePattern:
    pattern: str


@dataclass(frozen=True)
class ValueLen:
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class ValueBounds:
    ge: float | None = None
    le: float | None = None
    gt: float | None = None
    lt: float | None = None


@dataclass(frozen=True)
class ValueItems:
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class ValueUnique:
    pass


@dataclass(frozen=True)
class EveryItem:
    """``items``: every element satisfies the operand."""

    operand: Cond


@dataclass(frozen=True)
class SomeItem:
    """``contains``: at least one element satisfies the operand."""

    operand: Cond


@dataclass(frozen=True)
class KeysSatisfy:
    """``propertyNames``: every key satisfies the operand."""

    operand: Cond


@dataclass(frozen=True)
class MatchingKeysSatisfy:
    """``patternProperties``: every value under a matching key satisfies it."""

    pattern: str
    operand: Cond


@dataclass(frozen=True)
class PropCount:
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class DependentRequired:
    """``dependencies``/``dependentRequired``: presence implies presence."""

    name: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class NoOtherProps:
    """``additionalProperties: false``: only the named members may be supplied.

    The sibling ``properties`` names what may be present, and a generated class
    normally declares more fields than one branch's ``properties`` names, so this
    is a bound on what the caller supplied rather than on what the class can
    hold.
    """

    names: tuple[str, ...]


@dataclass(frozen=True)
class OtherPropsSatisfy:
    """``additionalProperties`` as a subschema: unnamed members satisfy it.

    With no names, every member does -- which is how the bundle spells a map
    type, ``{"additionalProperties": {"enum": [...]}}``.
    """

    names: tuple[str, ...]
    operand: Cond


@dataclass(frozen=True)
class Undeclarable:
    """``required`` naming a property the schema itself never declares.

    An AdCP request root that forbids ``creatives`` names a key the document
    may not carry at all. A generated model has no field for it and cannot be
    given one, so the predicate is false for every instance -- which is what
    makes the branch forbidding it satisfiable and the branch requiring it dead.
    """

    name: str


@dataclass(frozen=True)
class Restated:
    """A keyword proven identical to what the generated class declares.

    True of every instance of that class, which makes substituting it sound in
    a negated position as much as a positive one. Carries the keyword so a
    reader of the record can see what was accounted for rather than inferring
    it from an absence.
    """

    keyword: str


Cond = Union[
    "Supplied",
    "NoOtherProps",
    "OtherPropsSatisfy",
    "Undeclarable",
    "Not",
    "AllOf",
    "AnyOf",
    "OneOf",
    "IfThen",
    "PropSatisfies",
    "ValueConst",
    "ValueEnum",
    "ValueType",
    "ValuePattern",
    "ValueLen",
    "ValueBounds",
    "ValueItems",
    "ValueUnique",
    "EveryItem",
    "SomeItem",
    "KeysSatisfy",
    "MatchingKeysSatisfy",
    "PropCount",
    "DependentRequired",
    "Restated",
]


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeclaredShape:
    """The wire-to-field map the generated class declares for one object.

    ``model_fields_set`` holds field names and a schema states wire names; a
    field aliased ``schema`` appears as ``schema_``. Every schema-named property
    in a compiled predicate passes through this map before it reaches the
    record, so no emitter has to re-derive it.

    ``nested`` carries the same map for a field whose value is itself an object
    the parser descends into. A field with no entry is :data:`OPAQUE`: the
    parser could not resolve the nested class, so it carries wire names through
    unchanged. ``OPAQUE`` also answers "declares every name", which is what
    keeps an unresolved nesting from manufacturing a field-drop refusal.

    ``closed`` says no key outside ``fields`` can reach the object at all --
    ``additionalProperties: false``, which the generator renders as
    ``extra='forbid'``. It decides what a constraint naming an undeclared key
    means: under a closed shape the key can never be present, so the predicate
    is statically false (:class:`Undeclarable`); under an open one pydantic
    carries the key as an extra, puts it in ``model_fields_set`` and resolves it
    through ``getattr``, so the predicate reads it off the document under its
    wire name. Both were measured: ``control-media-buy-request`` is closed and
    forbids ``creatives``; ``get-reporting-status-response`` is open and its
    nested branch names ``failure_kind``, which no field holds.
    """

    fields: Mapping[str, str] = field(default_factory=dict)
    nested: Mapping[str, DeclaredShape] = field(default_factory=dict)
    opaque: bool = False
    closed: bool = False

    def attribute(self, wire_name: str) -> str | None:
        """The attribute for a wire name, or ``None`` when undeclared."""
        if self.opaque:
            return wire_name
        return self.fields.get(wire_name)

    def descend(self, wire_name: str) -> DeclaredShape:
        return self.nested.get(wire_name, OPAQUE)

    @classmethod
    def from_schema(cls, schema: Any) -> DeclaredShape:
        """The map a schema's own declarations imply, with identity names.

        Every property any part of a root declares, because the generator emits
        one class per declaring branch and a root-level constraint may name a field
        a sibling branch declares. Used where no generated class is available --
        the bundle-closure test, and a nested object the parser cannot resolve
        to a class. It answers the keyword-accounting question faithfully, which
        is what the closure rule grades, and makes no claim about aliases, which
        only ``model_fields`` can answer.
        """
        properties = _declared_properties(schema)
        if not properties:
            return OPAQUE
        return cls(
            fields={name: name for name in properties},
            nested={name: cls.from_schema(sub) for name, sub in properties.items()},
            closed=isinstance(schema, dict) and schema.get("additionalProperties") is False,
        )


#: A shape the parser could not resolve: identity names, declares everything.
OPAQUE = DeclaredShape(opaque=True)


@dataclass(frozen=True)
class Branch:
    """One branch of a schema root's ``anyOf``/``oneOf``."""

    pointer: str
    """``"anyOf/0"`` -- provenance, never identity."""

    title: str | None
    """Identity source 1."""

    discriminator: tuple[str, str] | None
    """Identity source 2: ``(property, const)``."""

    required: tuple[str, ...]
    """Identity source 3, and a constraint. Attribute names, not wire names."""

    declares_fields: bool
    """``properties`` introducing new fields: a class-creating branch."""

    condition: Cond
    """The WHOLE branch as one constraint tree."""

    declares_behavioral_rules: bool = False
    """The branch carries ``x-adcp-validation``, so its cardinality is not derivable."""


@dataclass(frozen=True)
class RootConstraints:
    """Everything a schema root states beyond its merged ``properties``."""

    combinator: Literal["anyOf", "oneOf"] | None
    branches: tuple[Branch, ...]
    conjuncts: tuple[Cond, ...]
    """Root ``if``/``then``/``else``, ``dependencies``, ``dependentRequired``,
    root ``not``, and the constraint branches of a root ``allOf``."""

    root_required: frozenset[str] = frozenset()
    """What the SCHEMA ROOT requires unconditionally. Attribute names.

    The branch emitter needs this because ``datamodel-code-generator`` merges the
    branches' properties onto one flat class and carries their requiredness with
    them, so the merged class requires exactly the keys its branches SELECT BY
    while the root requires none of them (salesagent-8qela.80). Reading this set
    is what lets the shared base keep the root's requiredness and leave each
    branch its own.

    It is NOT :attr:`Branch.required` intersected: a branch may omit a key the
    root requires, so an intersection under-requires. It is also not
    ``schema["required"]`` alone -- see :func:`root_required_names`.
    """

    declares_behavioral_rules: bool = False

    def __bool__(self) -> bool:
        return bool(self.branches or self.conjuncts)


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #


def root_combinator(schema: Any) -> Literal["anyOf", "oneOf"] | None:
    """Which combinator the root declares over its branches, if either.

    One owner, because two emitters read it and a refusal NAMES it: the branch
    emitter puts it on the split root's wrapping validator, and the response
    emitter puts it on the compatibility base's. A keyword that disagreed
    between them would be a refusal citing a keyword the schema does not use.

    ``anyOf`` is tested first and the first match wins, which is the precedence
    the reader has always applied: a root declaring both states the stricter
    rule in its ``oneOf``, and the branches are read from one node.
    """
    if not isinstance(schema, dict):
        return None
    candidates: tuple[Literal["anyOf", "oneOf"], ...] = ("anyOf", "oneOf")
    for candidate in candidates:
        node = schema.get(candidate)
        if isinstance(node, list) and node:
            return candidate
    return None


def root_required_names(
    schema: Any,
    resolve_document: ResolveDocument | None = None,
    _seen: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """The WIRE names a schema root requires unconditionally.

    One owner, because two callers compare the same two things and a
    disagreement between them is unreadable: the branch emitter decides which
    of the merged class's required fields to relax, and the guard that grades
    the result decides which are over-required. A second derivation of this
    would be a second answer.

    ``schema["required"]`` IS NOT THAT SET. A root ``allOf`` is inheritance for
    the declarative keys the generator merges, and a ``required`` inside a
    member -- or inside the schema a member ``$ref``s -- binds exactly as the
    root's own key does. ``account/list-account-changes-response`` declares no
    ``required`` of its own and ``allOf``s ``core/protocol-envelope``, which
    requires ``status``; asked against jsonschema, a document omitting
    ``status`` fails with ``required``. So the generated class requiring it is
    right, and a derivation reading only the root's own key calls that a defect
    and relaxes a field the pin requires.

    ``allOf`` and nothing else: a ``oneOf``/``anyOf`` branch's ``required`` is
    what the branch SELECTS BY, and ``if``/``then`` is conditional. Over the 18
    required fields on the 13 split-root bases at the 3.2.1 pin, this derivation
    agrees with jsonschema on all 18 and the root's own key alone on 17.
    """
    if not isinstance(schema, dict):
        return frozenset()
    names = {name for name in schema.get("required") or () if isinstance(name, str)}
    for member in schema.get("allOf") or ():
        if not isinstance(member, dict):
            continue
        ref = member.get("$ref")
        if isinstance(ref, str) and ref not in _seen:
            document, fragment = (ref.split("#", 1) + [""])[:2]
            target = resolve_document(document) if document and resolve_document else schema
            if target is not None:
                names |= root_required_names(
                    _follow_pointer(target, fragment), resolve_document, _seen | {ref}
                )
        names |= root_required_names(
            {key: value for key, value in member.items() if key != "$ref"},
            resolve_document,
            _seen,
        )
    return frozenset(names)


def root_carries_constraints(schema: Any) -> bool:
    """Whether a schema root states anything beyond its merged ``properties``.

    This predicate is the reader's ADMISSION gate, so what it misses is content
    nothing examines -- not content examined and skipped. It used to test only
    the root's own keys plus a root ``anyOf``/``oneOf``, and this bundle puts
    conditional rules inside ``allOf`` MEMBERS: 112 roots carried
    ``allOf: [{if, then}, ...]`` and were reported as carrying nothing.
    ``core/reporting-obligation`` has ten such members, one of which requires
    ``pending_adjustment_count``, and the model accepted documents the schema
    refused for exactly that reason.

    So an ``allOf`` member is examined too. A root ``allOf`` is inheritance for
    the DECLARATIVE keys the generator merges, and a carrier of logic for the
    keys it drops -- the same list either way, read for two purposes.
    """
    if not isinstance(schema, dict):
        return False
    if _states_logic(schema):
        return True
    members = schema.get("allOf")
    return isinstance(members, list) and any(_states_logic(member) for member in members)


def _states_logic(node: Any) -> bool:
    """Whether a node carries a keyword the generator drops."""
    if not isinstance(node, dict):
        return False
    if any(
        key in node
        for key in ("if", "dependencies", "dependentRequired", "dependentSchemas", "not")
    ):
        return True
    # A whole-object keyword is dropped exactly as a logic key is, so a root
    # carrying one states something no field of the class expresses.
    if any(key in node for key in DROPPED_KEYWORDS):
        return True
    return any(isinstance(node.get(key), list) and node[key] for key in ("anyOf", "oneOf"))


#: Positions whose subschema DECLARES the same object as its parent, so a
#: property named there is a property of the parent. The conditional and
#: negated positions (``if``/``then``/``else``/``not``) describe the same object
#: too, but they declare nothing -- see :func:`_declared_properties`.
_SAME_OBJECT_LISTS = ("allOf", "anyOf", "oneOf")


def _declaration_of(node: Any) -> dict[str, Any]:
    """The part of a node the generator renders: its declarative keys.

    A node's logic keys are exactly what the generator drops, so they are never
    part of the declaration a restatement is proven against -- with one
    exception the generator does render: a root ``allOf`` is inheritance, and
    its members' declarations are merged into the class. So the declaration a
    restatement is proven against includes them.
    """
    if not isinstance(node, dict):
        return {}
    # ``DROPPED_KEYWORDS`` leave with the logic keys: the generator renders
    # neither, so neither is part of the declaration a restatement is proven
    # against. Keeping them here made ``minProperties`` inside an ``allOf``
    # member compile to ``Restated('declaration')`` against a class that
    # declares no such thing.
    declaration = {
        key: value
        for key, value in node.items()
        if key not in LOGIC_KEYS and key not in DROPPED_KEYWORDS
    }
    refs = [declaration["$ref"]] if isinstance(declaration.get("$ref"), str) else []
    members = node.get("allOf")
    if isinstance(members, list):
        properties = dict(declaration.get("properties") or {})
        for member in members:
            inherited = _declaration_of(member)
            properties.update(inherited.get("properties") or {})
            refs.extend(inherited.get(_DECLARED_REFS, ()))
            for key, value in inherited.items():
                if key not in ("properties", _DECLARED_REFS):
                    declaration.setdefault(key, value)
        if properties:
            declaration["properties"] = properties
    if refs:
        declaration[_DECLARED_REFS] = tuple(refs)
    return declaration


def _declared_properties(node: Any) -> dict[str, Any]:
    """Every property name any part of one object's schema declares.

    A root constraint may name a field a sibling branch declares -- an branch that
    forbids ``errors`` is the branch whose sibling declares it -- so the set of
    names a root's constraint content may reference is the union over the
    positions that DECLARE the object: its own ``properties``, its ``allOf``
    members' (inheritance, which the generator merges) and its branches'.

    A negated or conditional position declares nothing. ``not``, ``if``,
    ``then``, ``else`` and the ``dependencies`` maps are constraint content --
    the generator renders no field from any of them -- so a property name
    reachable only through one of those is a key the document may carry and no
    class declares, which is :class:`Undeclarable` and decidable, not a dropped
    field. ``get-creative-features-terminal-success.json`` is the measured case:
    its root ``not`` forbids ``status: submitted``, and reading that as a
    declaration turned a compilable absence ban into a field-drop refusal.
    """
    if not isinstance(node, dict):
        return {}
    collected: dict[str, Any] = {}
    properties = node.get("properties")
    if isinstance(properties, dict):
        collected.update(properties)
    for key in _SAME_OBJECT_LISTS:
        members = node.get(key)
        if isinstance(members, list):
            for member in members:
                collected.update(_declared_properties(member))
    return collected


class _Parser:
    """Compiles one schema root. One instance per root, so errors carry the file."""

    def __init__(
        self,
        schema_file: str,
        declared: DeclaredShape,
        resolve_document: ResolveDocument | None,
        *,
        branches_render_as_classes: bool,
    ) -> None:
        self._file = schema_file
        self._root_shape = declared
        self._resolve_document = resolve_document
        self._branches_render_as_classes = branches_render_as_classes
        self._schema_names: frozenset[str] = frozenset()
        self._root_names: frozenset[str] = frozenset()
        self._document: Any = {}
        self._resolving: frozenset[str] = frozenset()
        self.behavioral = False

    # -- entry point -------------------------------------------------------- #

    def read(self, schema: dict[str, Any]) -> RootConstraints:
        combinator: Literal["anyOf", "oneOf"] | None = None
        branches: tuple[Branch, ...] = ()
        root_declaration = _declaration_of(schema)
        self._root_names = frozenset(root_declaration.get("properties") or {})
        self._schema_names = frozenset(_declared_properties(schema))
        self._document = schema

        combinator = root_combinator(schema)
        if combinator is not None:
            branches = tuple(
                self._read_branch(branch, f"{combinator}/{index}", root_declaration)
                for index, branch in enumerate(schema[combinator])
            )

        conjuncts: list[Cond] = []
        if "if" in schema:
            conjuncts.append(self._if_then(schema, "", root_declaration, self._root_shape))
        # ``dependentSchemas`` beside its two siblings: all three are dropped by
        # codegen, all three compile through ``_dependencies``, and the gate and
        # this loop both named only two. No schema in the pinned bundle uses it,
        # so this closes a latent hole rather than changing a verdict -- which is
        # the same defect class as the other four doors and is why
        # tests/test_dropped_keyword_doors.py grades the partition rather than
        # the instances.
        for keyword in ("dependencies", "dependentRequired", "dependentSchemas"):
            if keyword in schema:
                conjuncts.extend(self._dependencies(schema[keyword], keyword, "", self._root_shape))
        if "not" in schema:
            conjuncts.append(
                Not(self._node(schema["not"], "not", root_declaration, self._root_shape))
            )
        # A root ``allOf`` is two things at once, and both are read. Its
        # ``$ref`` members are inheritance, which the generator renders and
        # ``_declaration_of`` therefore counts as declared, so they compile to
        # ``Restated`` and drop out here. Its inline members are conjuncts
        # beside whatever else the root states -- including beside a
        # combinator: ``create-media-buy-request``'s ``allOf`` carries the
        # opportunity-implies-proposal rule and the bidding rule next to its
        # three-branch ``anyOf``, and reading them only in the absence of a
        # combinator dropped both.
        if isinstance(schema.get("allOf"), list):
            for index, member in enumerate(schema["allOf"]):
                compiled = self._node(member, f"allOf/{index}", root_declaration, self._root_shape)
                if not isinstance(compiled, Restated):
                    conjuncts.append(compiled)
        # The root's OWN whole-object keywords. They sit in none of the positions
        # above -- not in the combinator, not in ``if``, not in an ``allOf``
        # member -- so without this the gate admits the root and the reader hands
        # the grammar nothing. Compiled as a node of just those keys, which is
        # what they constrain: the object the root declares.
        dropped = {key: schema[key] for key in sorted(DROPPED_KEYWORDS) if key in schema}
        if dropped:
            compiled = self._node(dropped, "", root_declaration, self._root_shape)
            if not isinstance(compiled, Restated):
                conjuncts.append(compiled)

        # The root's own requiredness, as FIELD names. A required wire name no
        # field holds is not a field an emitter can relax, so it drops out here
        # rather than refusing: a dropped field is its own defect and aborting
        # generation for it would report this one in its place.
        root_required = frozenset(
            attribute
            for name in root_required_names(schema, self._resolve_document)
            if (attribute := self._root_shape.attribute(name)) is not None
        )

        return RootConstraints(
            combinator=combinator,
            branches=branches,
            conjuncts=tuple(conjuncts),
            root_required=root_required,
            declares_behavioral_rules=self.behavioral,
        )

    # -- branches --------------------------------------------------------------- #

    def _read_branch(self, branch: Any, pointer: str, root_declaration: dict[str, Any]) -> Branch:
        if not isinstance(branch, dict):
            raise UnexpressibleKeywordError(self._file, pointer, type(branch).__name__)

        root_properties = root_declaration.get("properties")
        root_names = set(root_properties) if isinstance(root_properties, dict) else set()
        branch_declaration = _declaration_of(branch)
        branch_properties = branch_declaration.get("properties")
        branch_names = set(branch_properties) if isinstance(branch_properties, dict) else set()
        declares_fields = bool(branch_names - root_names)

        # An branch the generator renders as ITS OWN TYPE states its own
        # declaration: a bare ``$ref`` becomes the referenced type and an branch of
        # a different scalar type becomes that type, so a keyword restating
        # either is already enforced. An OVERLAY branch on a FLAT root states
        # constraint content instead, and is proven against the ROOT's
        # declaration -- one class holds the merged properties, and what the branch
        # says about a position is enforced only where the root says the same.
        #
        # Whether an branch that introduces a property becomes a class is the one
        # part that is NOT inferable from the schema, which is why the caller
        # supplies it. Inferring it from ``declares_fields`` is what
        # ``list-account-changes-response`` measured wrong: one flat class over
        # both branches, each branch introducing ``status``, so each branch's ``required``
        # and ``properties`` were proven restatements of themselves and the
        # model was left enforcing nothing at all.
        renders_own_shape = (
            (declares_fields and self._branches_render_as_classes)
            or "$ref" in branch_declaration
            or branch_declaration.get("type", root_declaration.get("type"))
            != root_declaration.get("type")
        )
        declaration = branch_declaration if renders_own_shape else root_declaration

        before = self.behavioral
        self.behavioral = False
        condition = self._node(branch, pointer, declaration, self._root_shape)
        branch_behavioral = self.behavioral
        self.behavioral = before or branch_behavioral

        # Identity reads the names the branch requires OF THE CLASS; a name the
        # document may not carry identifies nothing.
        required = tuple(
            attribute
            for attribute in (
                self._attribute(name, self._root_shape, pointer)
                for name in branch.get("required", ())
                if isinstance(name, str)
            )
            if attribute is not None
        )
        return Branch(
            pointer=pointer,
            title=branch.get("title") if isinstance(branch.get("title"), str) else None,
            discriminator=self._discriminator(branch),
            required=required,
            declares_fields=declares_fields,
            condition=condition,
            declares_behavioral_rules=branch_behavioral,
        )

    @staticmethod
    def _discriminator(branch: dict[str, Any]) -> tuple[str, str] | None:
        """The ``(property, const)`` pair an branch pins exactly one property to."""
        properties = branch.get("properties")
        if not isinstance(properties, dict):
            return None
        pinned = [
            (name, sub["const"])
            for name, sub in properties.items()
            if isinstance(sub, dict) and isinstance(sub.get("const"), str)
        ]
        if len(pinned) != 1:
            return None
        return pinned[0]

    # -- the key-consuming walk --------------------------------------------- #

    def _node(
        self,
        node: Any,
        pointer: str,
        declaration: Any,
        shape: DeclaredShape,
    ) -> Cond:
        """Compile one schema node, accounting for every key it carries."""
        if node is True or node == {}:
            return Restated("true")
        if node is False:
            # ``false`` admits nothing. Spelled as a logic node so a caller
            # never has to special-case a bare boolean.
            return Not(Restated("true"))
        if not isinstance(node, dict):
            raise UnexpressibleKeywordError(self._file, pointer, type(node).__name__)

        declaration = declaration if isinstance(declaration, dict) else {}
        operands: list[Cond] = []

        for keyword in sorted(node):
            value = node[keyword]
            if keyword in NON_ASSERTING_KEYS:
                continue
            if keyword == DECLARED_BEHAVIORAL_KEY:
                self.behavioral = True
                continue
            compiled = self._keyword(keyword, value, node, pointer, declaration, shape)
            if compiled is None:
                raise UnexpressibleKeywordError(self._file, pointer, keyword)
            if isinstance(compiled, Restated):
                continue
            operands.append(compiled)

        if not operands:
            return Restated("declaration")
        if len(operands) == 1:
            return operands[0]
        return AllOf(tuple(operands))

    def _keyword(  # noqa: C901, PLR0911, PLR0912
        self,
        keyword: str,
        value: Any,
        node: dict[str, Any],
        pointer: str,
        declaration: dict[str, Any],
        shape: DeclaredShape,
    ) -> Cond | None:
        """One grammar production, or ``None`` when the grammar has none."""
        here = f"{pointer}/{keyword}" if pointer else keyword

        # The restatement rule, in one place and for every keyword: a keyword
        # whose value is exactly what the generated class declares at this
        # position is already enforced by the class, so it is true of every
        # instance. A keyword that NARROWS the declaration is not equal to it
        # and falls through to a production below -- equality is checked, never
        # assumed.
        if keyword in declaration and _equal(value, declaration[keyword]):
            return Restated(keyword)

        if keyword == "required":
            return _conjoin(
                self._supplied(name, shape, pointer, declaration)
                for name in value
                if isinstance(name, str)
            )

        if keyword == "properties":
            parts: list[Cond] = []
            declared_properties = declaration.get("properties")
            declared_properties = (
                declared_properties if isinstance(declared_properties, dict) else {}
            )
            for name in sorted(value):
                sub = value[name]
                declared_sub = declared_properties.get(name)
                if declared_sub is not None and _equal(sub, declared_sub):
                    continue
                attribute = self._attribute(name, shape, pointer)
                if attribute is None:
                    # ``properties`` grades present members only, and a key the
                    # document may not carry is never present.
                    continue
                inner = self._node(sub, f"{here}/{name}", declared_sub, shape.descend(name))
                if isinstance(inner, Restated):
                    continue
                parts.append(PropSatisfies(attribute, inner))
            return _conjoin(parts)

        if keyword == "patternProperties":
            parts = []
            for pattern in sorted(value):
                inner = self._node(value[pattern], f"{here}/{pattern}", None, OPAQUE)
                if isinstance(inner, Restated):
                    continue
                parts.append(MatchingKeysSatisfy(pattern, inner))
            return _conjoin(parts)

        if keyword == "additionalProperties":
            if value is True:
                return Restated(keyword)
            if value is False:
                # Only the sibling ``properties`` members may be supplied. A
                # generated class normally declares more fields than one branch
                # names, so this bounds what the caller supplied.
                if "patternProperties" in node:
                    # The admissible key set is then not enumerable.
                    return None
                return NoOtherProps(self._sibling_names(node, shape, pointer))
            if "patternProperties" in node:
                return None
            inner = self._node(value, here, None, OPAQUE)
            if isinstance(inner, Restated):
                return Restated(keyword)
            return OtherPropsSatisfy(self._sibling_names(node, shape, pointer), inner)

        if keyword == "not":
            # ``not``, ``if``, ``then`` and ``else`` grade the SAME object as
            # their parent, so a restatement inside one is proven against the
            # parent's declaration. A predicate the declaration already
            # enforces is true of every instance, which is exactly as sound
            # under a negation (where it makes the branch unsatisfiable) as it is
            # on the positive path.
            return Not(self._node(value, here, declaration, shape))
        if keyword == "allOf":
            return _conjoin(
                self._node(member, f"{here}/{index}", declaration, shape)
                for index, member in enumerate(value)
            )
        if keyword == "anyOf":
            return AnyOf(
                tuple(
                    self._node(member, f"{here}/{index}", declaration, shape)
                    for index, member in enumerate(value)
                )
            )
        if keyword == "oneOf":
            return OneOf(
                tuple(
                    self._node(member, f"{here}/{index}", declaration, shape)
                    for index, member in enumerate(value)
                )
            )
        if keyword == "if":
            return self._if_then(node, pointer, declaration, shape)
        if keyword in ("then", "else"):
            # Consumed by the sibling ``if``; without one, JSON Schema's own
            # reading is that the keyword asserts nothing.
            return Restated(keyword)

        if keyword in ("dependencies", "dependentRequired"):
            return _conjoin(self._dependencies(value, keyword, pointer, shape))
        if keyword == "dependentSchemas":
            return _conjoin(self._dependencies(value, keyword, pointer, shape))

        if keyword == "const":
            return ValueConst(value)
        if keyword == "enum":
            return ValueEnum(tuple(value))
        if keyword == "type":
            return ValueType((value,) if isinstance(value, str) else tuple(value))
        if keyword == "pattern":
            return ValuePattern(value)
        if keyword == "$ref":
            if value in declaration.get(_DECLARED_REFS, ()):
                return Restated(keyword)
            # A reference the declaration does not name is a narrowing: the
            # position has to satisfy the referenced schema as well. Resolve it
            # and compile what it says. An unresolvable reference refuses --
            # assuming an unproven equality is the one thing this module will
            # not do.
            return self._reference(value, here, declaration, shape)

        if keyword == "minLength":
            return ValueLen(minimum=value)
        if keyword == "maxLength":
            return ValueLen(maximum=value)
        if keyword == "minimum":
            return ValueBounds(ge=value)
        if keyword == "maximum":
            return ValueBounds(le=value)
        if keyword == "exclusiveMinimum":
            return ValueBounds(gt=value)
        if keyword == "exclusiveMaximum":
            return ValueBounds(lt=value)
        if keyword == "minItems":
            return ValueItems(minimum=value)
        if keyword == "maxItems":
            return ValueItems(maximum=value)
        if keyword == "uniqueItems":
            return ValueUnique() if value else Restated(keyword)
        if keyword == "minProperties":
            return PropCount(minimum=value)
        if keyword == "maxProperties":
            return PropCount(maximum=value)

        if keyword == "items":
            declared_items = declaration.get("items")
            if declared_items is not None and _equal(value, declared_items):
                return Restated(keyword)
            return EveryItem(self._node(value, here, declared_items, OPAQUE))
        if keyword == "contains":
            return SomeItem(self._node(value, here, None, OPAQUE))
        if keyword == "propertyNames":
            return KeysSatisfy(self._node(value, here, None, OPAQUE))

        return None

    def _sibling_names(
        self, node: dict[str, Any], shape: DeclaredShape, pointer: str
    ) -> tuple[str, ...]:
        """The members a node's own ``properties`` names, as field names."""
        declared = node.get("properties")
        if not isinstance(declared, dict):
            return ()
        resolved = (self._attribute(name, shape, pointer) for name in sorted(declared))
        return tuple(name for name in resolved if name is not None)

    # -- references --------------------------------------------------------- #

    def _reference(
        self, ref: str, pointer: str, declaration: dict[str, Any], shape: DeclaredShape
    ) -> Cond | None:
        """Compile the schema a ``$ref`` names, or ``None`` when unresolvable.

        The referenced schema lands at the position the reference occupies, so
        it is graded against the same declaration -- the target is constraint
        content, not a declaration of its own.

        Descending into another document makes that document the one a nested
        local pointer resolves against. ``change-term-constraints.json`` spells
        a money amount ``{"$ref": "#/definitions/money"}``, and resolving it
        against whichever root the walk started from finds nothing.
        """
        if ref in self._resolving:
            # A recursive reference states nothing new at this depth.
            return Restated("$ref")
        document, target = self._resolve_reference(ref)
        if target is None:
            return None
        previous = self._document
        self._resolving = self._resolving | {ref}
        self._document = document
        try:
            return self._node(target, pointer, declaration, shape)
        finally:
            self._resolving = self._resolving - {ref}
            self._document = previous

    def _resolve_reference(self, ref: str) -> tuple[Any, Any | None]:
        """The document the reference lands in, and the node it names."""
        document, fragment = (ref.split("#", 1) + [""])[:2] if "#" in ref else (ref, "")
        if document:
            resolved = self._resolve_document(document) if self._resolve_document else None
            if resolved is None:
                return self._document, None
        else:
            resolved = self._document
        return resolved, _follow_pointer(resolved, fragment)

    # -- productions needing two keys --------------------------------------- #

    def _if_then(
        self, node: dict[str, Any], pointer: str, declaration: Any, shape: DeclaredShape
    ) -> Cond:
        base = f"{pointer}/" if pointer else ""
        condition = self._node(node["if"], f"{base}if", declaration, shape)
        consequent = (
            self._node(node["then"], f"{base}then", declaration, shape) if "then" in node else None
        )
        alternative = (
            self._node(node["else"], f"{base}else", declaration, shape) if "else" in node else None
        )
        return IfThen(condition, consequent, alternative)

    def _dependencies(
        self, value: Any, keyword: str, pointer: str, shape: DeclaredShape
    ) -> list[Cond]:
        here = f"{pointer}/{keyword}" if pointer else keyword
        parts: list[Cond] = []
        for name in sorted(value):
            dependents = value[name]
            trigger = self._attribute(name, shape, pointer)
            if trigger is None:
                # A key the document may not carry never triggers.
                continue
            if isinstance(dependents, list):
                required = [
                    self._supplied(dependent, shape, pointer)
                    for dependent in dependents
                    if isinstance(dependent, str)
                ]
                if all(isinstance(part, Supplied) for part in required):
                    parts.append(
                        DependentRequired(
                            trigger,
                            tuple(part.name for part in required),  # type: ignore[union-attr]
                        )
                    )
                else:
                    parts.append(IfThen(Supplied(trigger), _conjoin(required), None))
                continue
            # The schema form of ``dependencies``: presence implies a subschema.
            parts.append(
                IfThen(
                    Supplied(trigger),
                    self._node(dependents, f"{here}/{name}", None, shape),
                    None,
                )
            )
        return parts

    # -- names -------------------------------------------------------------- #

    def _attribute(self, wire_name: str, shape: DeclaredShape, pointer: str) -> str | None:
        """The field name for a wire name, or ``None`` when the key cannot exist.

        Three different absences, and each decided rather than assumed.

        A name the root DECLARES that the generated class does not carry is a
        dropped field, and refuses: the constraint grades a position the model
        does not have.

        A name no field holds on an OPEN object is a key the document may carry
        as an extra. Pydantic puts an extra in ``model_fields_set`` and resolves
        it through ``getattr``, so the predicate reads it under its wire name --
        which is how ``get-reporting-status-response``'s nested branch grades
        ``failure_kind``, a property only that branch declares and the generator
        renders no field for.

        A name no field holds on a CLOSED object is a key the document may not
        carry at all -- an AdCP request root that forbids ``creatives`` behind
        ``additionalProperties: false`` -- and that is statically decidable.
        """
        attribute = shape.attribute(wire_name)
        if attribute is not None:
            return attribute
        at_root = shape is self._root_shape
        if at_root and wire_name in self._root_names:
            raise UnplaceableConstraintError(self._file, pointer, wire_name)
        if not shape.closed:
            return wire_name
        if at_root and wire_name in self._schema_names:
            # Declared by an branch, no field holds it, and the document may not
            # carry it either: the constraint grades a position no instance can
            # ever have. Unplaceable for the same reason a dropped root field is.
            raise UnplaceableConstraintError(self._file, pointer, wire_name)
        return None

    def _supplied(
        self,
        wire_name: str,
        shape: DeclaredShape,
        pointer: str,
        declaration: Any = None,
    ) -> Cond:
        """``required`` on one name, read as presence on the document SENT.

        ``declaration`` is the node the position declares, which is where
        nullability lives. Without one -- a ``dependencies`` map names a
        dependent the parser has no declaration in hand for -- the position is
        read as nullable, which is membership alone and the weaker of the two
        readings.
        """
        attribute = self._attribute(wire_name, shape, pointer)
        if attribute is None:
            return Undeclarable(wire_name)
        node = self._declared_node(declaration, wire_name)
        return Supplied(attribute, nullable=node is None or permits_null(node))

    def _declared_node(self, declaration: Any, wire_name: str, depth: int = 0) -> Any:
        """The schema node a declaration states for one property, or ``None``.

        A declaration often states a position by reference -- the root declares
        ``budget_allocation`` as a ``$ref`` and ``packages`` as an array of
        them -- so reading only the inline ``properties`` finds nothing one
        level down and every nested ``required`` falls back to the weaker
        reading. Following the reference to read ONE property's node is bounded:
        no keyword is accounted for here and no condition is compiled, so this
        cannot widen the parse.
        """
        if not isinstance(declaration, dict) or depth > 4:
            return None
        properties = declaration.get("properties")
        if isinstance(properties, dict) and wire_name in properties:
            return properties[wire_name]
        reference = declaration.get("$ref")
        if isinstance(reference, str):
            _document, target = self._resolve_reference(reference)
            if target is not None:
                return self._declared_node(target, wire_name, depth + 1)
        for keyword in ("allOf", "anyOf", "oneOf"):
            members = declaration.get(keyword)
            if isinstance(members, list):
                for member in members:
                    found = self._declared_node(member, wire_name, depth + 1)
                    if found is not None:
                        return found
        return None


def permits_null(schema: Any) -> bool:
    """True when a JSON Schema node explicitly permits ``null``.

    ``required`` and nullability are independent axes: a property listed in
    ``required`` whose ``type`` array contains ``"null"`` must be PRESENT and
    may be ``null``. Both spellings count -- the ``type: ["string", "null"]``
    array form and a ``oneOf``/``anyOf`` branch of ``{"type": "null"}``.

    This is what separates the two readings of presence. Where a position
    permits ``null``, an explicit null is a value the caller sent and the
    client keeps it on the wire. Where it does not, the client's
    ``exclude_none`` drops it and ``preserve_explicit_nulls`` does not put it
    back, so the key is ABSENT from the document that is sent -- and a
    constraint that read it as present would refuse a request no seller ever
    receives.
    """
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    for keyword in ("oneOf", "anyOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list) and any(
            isinstance(variant, dict) and variant.get("type") == "null" for variant in variants
        ):
            return True
    return False


def _equal(left: Any, right: Any) -> bool:
    """Structural equality ignoring keys that assert nothing."""
    if isinstance(left, dict) and isinstance(right, dict):
        kept_left = {k: v for k, v in left.items() if k not in NON_ASSERTING_KEYS}
        kept_right = {k: v for k, v in right.items() if k not in NON_ASSERTING_KEYS}
        if set(kept_left) != set(kept_right):
            return False
        return all(_equal(kept_left[k], kept_right[k]) for k in kept_left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right) and type(left) is type(right)


def _conjoin(operands: Iterable[Cond]) -> Cond:
    kept: list[Cond] = [operand for operand in operands if not isinstance(operand, Restated)]
    if not kept:
        return Restated("conjunction")
    if len(kept) == 1:
        return kept[0]
    return AllOf(tuple(kept))


def _follow_pointer(document: Any, fragment: str) -> Any | None:
    """Resolve a JSON pointer fragment, honouring ``~0``/``~1`` escapes."""
    node = document
    for raw in fragment.lstrip("#").split("/"):
        if not raw:
            continue
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None
            continue
        if not isinstance(node, dict) or token not in node:
            return None
        node = node[token]
    return node


def bundle_document_resolver(schema_dir: Path) -> ResolveDocument:
    """Resolve a ``$ref``'s document half against a schema bundle directory.

    The bundle spells the same reference two ways -- an absolute
    ``https://adcontextprotocol.org/schemas/<version>/<path>`` URL in the plain
    schemas and a ``#/$defs/external:<escaped path>`` pointer in the bundled
    ``mcp/`` variants -- so matching the longest path suffix that names a file
    under ``schema_dir`` covers both without a version table to keep in step.
    """
    cache: dict[str, Any | None] = {}

    def resolve(document: str) -> Any | None:
        if document not in cache:
            cache[document] = _load_bundle_document(schema_dir, document)
        return cache[document]

    return resolve


def _load_bundle_document(schema_dir: Path, document: str) -> Any | None:
    parts = [part for part in document.split("/") if part and not part.endswith(":")]
    for start in range(len(parts)):
        candidate = schema_dir.joinpath(*parts[start:])
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
    return None


def read_root_constraints(
    schema: dict[str, Any],
    declared: DeclaredShape,
    *,
    branches_render_as_classes: bool,
    schema_file: str = "<schema>",
    resolve_document: ResolveDocument | None = None,
) -> RootConstraints:
    """Read everything a schema root states beyond its merged ``properties``.

    ``declared`` is the wire-to-field map of the class the generator emits for
    this root; compiled predicates name attributes, not wire names.

    ``branches_render_as_classes`` says whether the generator rendered each branch that
    introduces a property as its own class. On a FLAT root it is ``False``: one
    class holds the merged properties, so every branch's content is constraint
    content. On a root the generator already split into per-branch classes it is
    ``True``, and an branch's own ``required`` and ``properties`` are then
    declarations the classes carry. Nothing in the schema distinguishes the two,
    so it is not inferred; the caller reads it off the generated module.

    ``resolve_document`` resolves the document half of a ``$ref`` the schema's
    own ``$defs`` do not hold. Without one, such a reference refuses -- the
    parser does no I/O of its own.

    Raises :class:`UnexpressibleKeywordError` for a keyword the grammar cannot
    compile, and :class:`UnplaceableConstraintError` for a constraint naming a
    property no class declares. Both abort generation.
    """
    return _Parser(
        schema_file,
        declared,
        resolve_document,
        branches_render_as_classes=branches_render_as_classes,
    ).read(schema)


# --------------------------------------------------------------------------- #
# Disjointness: the one prover, used for the emit rule and for ``oneOf``
# --------------------------------------------------------------------------- #


class UndisjointOneOfError(Exception):
    """A root ``oneOf`` whose branches this module cannot prove pairwise disjoint.

    ``oneOf`` is exactly-one. A union renders at-least-one, so rendering an
    unproven ``oneOf`` as a union would ship anyOf-weaker semantics. Refusing
    aborts generation in the same family as :class:`UnexpressibleKeywordError`.
    """

    def __init__(self, schema_file: str, left: str, right: str) -> None:
        self.schema_file = schema_file
        self.left = left
        self.right = right
        super().__init__(
            f"{schema_file}: oneOf branches {left} and {right} are not provably disjoint, so "
            f"rendering them as a union would admit a document satisfying both -- which "
            f"oneOf forbids. Either the schema means anyOf, or the prover in "
            f"scripts/schema_constraints.py needs the production that separates them."
        )


@dataclass(frozen=True)
class _Present:
    """``path`` is a member the document carries (negated: does not carry)."""

    path: tuple[str, ...]
    positive: bool


@dataclass(frozen=True)
class _Admits:
    """The value at ``path`` is one of ``values`` (negated: is none of them)."""

    path: tuple[str, ...]
    values: frozenset[Any]
    positive: bool


_Literal = _Present | _Admits

#: One conjunction of literals, and a disjunction of those: DNF.
_Clause = frozenset[_Literal]

#: TRUE is the one empty clause; FALSE is no clause at all.
_TRUE: tuple[_Clause, ...] = (frozenset(),)
_FALSE: tuple[_Clause, ...] = ()

#: Past this many clauses the pair is reported unproven rather than expanded.
#: Every branch in the pinned bundle stays three orders of magnitude under it; the
#: cap exists so an adversarial future schema costs a lost proof, not a hang.
_CLAUSE_CEILING = 2048


class _TooBigError(Exception):
    """The DNF of one branch exceeded :data:`_CLAUSE_CEILING`."""


def _hashable(value: Any) -> Any:
    """A hashable stand-in for a JSON value, for equality only."""
    if isinstance(value, dict):
        return ("object", tuple(sorted((key, _hashable(sub)) for key, sub in value.items())))
    if isinstance(value, list):
        return ("array", tuple(_hashable(element) for element in value))
    return value


def _prefix(clauses: tuple[_Clause, ...], name: str) -> tuple[_Clause, ...]:
    """Re-root every literal's path under ``name``."""
    return tuple(
        frozenset(
            (
                _Present((name, *literal.path), literal.positive)
                if isinstance(literal, _Present)
                else _Admits((name, *literal.path), literal.values, literal.positive)
            )
            for literal in clause
        )
        for clause in clauses
    )


def _product(left: tuple[_Clause, ...], right: tuple[_Clause, ...]) -> tuple[_Clause, ...]:
    """Conjunction in DNF: every pairing, contradictory pairings dropped."""
    clauses = tuple(
        merged for one in left for other in right if _satisfiable(merged := one | other)
    )
    if len(clauses) > _CLAUSE_CEILING:
        raise _TooBigError
    return clauses


def _union(parts: Iterable[tuple[_Clause, ...]]) -> tuple[_Clause, ...]:
    clauses: list[_Clause] = []
    for part in parts:
        clauses.extend(part)
    if len(clauses) > _CLAUSE_CEILING:
        raise _TooBigError
    return tuple(clauses)


def _satisfiable(clause: _Clause) -> bool:
    """Whether one conjunction of literals can hold.

    Two decisions, both first-order. A member cannot be both carried and not
    carried. And a value cannot lie in two value sets with no member in common
    -- sound because a value literal only ever reaches a clause alongside the
    presence literal for its own path, so an absent member never clashes.
    """
    present: dict[tuple[str, ...], bool] = {}
    for literal in clause:
        if not isinstance(literal, _Present):
            continue
        if present.setdefault(literal.path, literal.positive) is not literal.positive:
            return False
    admitted: dict[tuple[str, ...], set[Any]] = {}
    for literal in clause:
        if not isinstance(literal, _Admits) or not literal.positive:
            continue
        current = admitted.get(literal.path)
        admitted[literal.path] = (
            set(literal.values) if current is None else current & literal.values
        )
    for literal in clause:
        if not isinstance(literal, _Admits) or literal.positive:
            continue
        if literal.path in admitted:
            admitted[literal.path] -= literal.values
    return all(values for values in admitted.values())


def _clauses(
    condition: Cond, negated: bool = False
) -> tuple[_Clause, ...]:  # noqa: C901, PLR0911, PLR0912
    """One ``Cond`` in disjunctive normal form over presence and value literals.

    Every node kind the prover has no literal for compiles to TRUE, under
    negation as well. That WEAKENS the branch, and a refutation of a weaker
    conjunction refutes the stronger one, so an unmodelled kind can only cost a
    proof -- never manufacture one.
    """
    if isinstance(condition, Restated):
        return _FALSE if negated else _TRUE
    if isinstance(condition, Supplied):
        return (frozenset({_Present((condition.name,), not negated)}),)
    if isinstance(condition, Undeclarable):
        # False of every instance of a class that cannot carry the key.
        return _TRUE if negated else _FALSE
    if isinstance(condition, Not):
        return _clauses(condition.operand, not negated)
    if isinstance(condition, AllOf):
        if negated:
            return _union(_clauses(operand, True) for operand in condition.operands)
        clauses = _TRUE
        for operand in condition.operands:
            clauses = _product(clauses, _clauses(operand))
        return clauses
    if isinstance(condition, (AnyOf, OneOf)):
        # ``oneOf`` is read as its ``anyOf`` weakening here, which is the safe
        # direction: a bigger satisfying set, so a refutation still holds.
        if negated:
            clauses = _TRUE
            for operand in condition.operands:
                clauses = _product(clauses, _clauses(operand, True))
            return clauses
        return _union(_clauses(operand) for operand in condition.operands)
    if isinstance(condition, IfThen):
        condition_true = _clauses(condition.condition)
        condition_false = _clauses(condition.condition, True)
        consequent = _TRUE if condition.consequent is None else _clauses(condition.consequent)
        alternative = _TRUE if condition.alternative is None else _clauses(condition.alternative)
        if negated:
            negated_consequent = (
                _FALSE if condition.consequent is None else _clauses(condition.consequent, True)
            )
            negated_alternative = (
                _FALSE if condition.alternative is None else _clauses(condition.alternative, True)
            )
            return _union(
                (
                    _product(condition_true, negated_consequent),
                    _product(condition_false, negated_alternative),
                )
            )
        return _union(
            (_product(condition_true, consequent), _product(condition_false, alternative))
        )
    if isinstance(condition, PropSatisfies):
        # ``properties`` grades a present member only, so the node is "absent,
        # or present and satisfying" -- which is what makes a value literal
        # always arrive with its own presence literal.
        inner = _prefix(_clauses(condition.operand), condition.name)
        if negated:
            return _product(
                (frozenset({_Present((condition.name,), True)}),),
                _prefix(_clauses(condition.operand, True), condition.name),
            )
        return _union(
            (
                (frozenset({_Present((condition.name,), False)}),),
                _product((frozenset({_Present((condition.name,), True)}),), inner),
            )
        )
    if isinstance(condition, ValueConst):
        return (frozenset({_Admits((), frozenset({_hashable(condition.value)}), not negated)}),)
    if isinstance(condition, ValueEnum):
        values = frozenset(_hashable(value) for value in condition.values)
        return (frozenset({_Admits((), values, not negated)}),)
    # Every other production bounds a value rather than naming it. Weakening
    # them to TRUE keeps the prover first-order; see the docstring.
    return _TRUE


def branch_conditions(constraints: RootConstraints) -> tuple[Cond, ...]:
    """What a document satisfying each branch must satisfy, conjuncts included.

    A root's conjuncts hold of every document the root accepts, so each branch's
    real condition is the branch conjoined with them. Distributing them is what
    turns three both-present bans into disjointness rather than leaving them as
    a check beside branches that overlap without them: ``webhook-challenge-response``
    bans ``challenge`` and ``token`` together at the root, and its two branches
    require one each.
    """
    return tuple(
        _conjoin((branch.condition, *constraints.conjuncts)) for branch in constraints.branches
    )


def provably_disjoint(left: Cond, right: Cond) -> bool:
    """Whether no document can satisfy both conditions.

    Sound and deliberately incomplete: it decides presence and named-value
    clashes, which is what the branches of the pinned bundle are separated by, and
    reports anything else unproven.
    """
    try:
        return all(
            not _satisfiable(one | other) for one in _clauses(left) for other in _clauses(right)
        )
    except _TooBigError:
        return False


def partitions_by_requiredness(constraints: RootConstraints) -> bool:
    """Whether the branches partition the document space, so each branch is a type.

    Two conditions. Every branch must require something: without ``required`` an
    branch grades only members that are present, so it is vacuously satisfiable and
    a document mentioning none of its fields satisfies it
    (``postal-country-system``'s ten branches, all satisfied by a document with no
    ``country``). And the branches must be provably pairwise disjoint, which is what
    makes the class a parsed document comes back as schema information rather
    than branch order (``control-media-buy-request``'s twelve branches each require
    one field and permit the other eleven -- "name at least one field to
    change", one shape, where an ``isinstance`` on the returned class reads the
    order of a JSON file and nothing about the document).

    The approved rule reads "partition by requiredness into DISTINCT, mutually
    exclusive field sets", and distinctness of the required sets is deliberately
    not a third condition here. Measured over the pinned bundle it decides
    nothing -- no root has two branches with the same required set that the prover
    separates -- and as a rule it would refuse a const-discriminated pair that
    requires the same group, which is a partition the design asks to emit. What
    it was guarding is that two branches needing the same rung-3 name cannot both be
    named, and the naming rule refuses that where it arises.
    """
    branches = constraints.branches
    if len(branches) < 2:
        return False
    if any(not branch.required for branch in branches):
        return False
    conditions = branch_conditions(constraints)
    return all(
        provably_disjoint(conditions[index], conditions[other])
        for index in range(len(conditions))
        for other in range(index + 1, len(conditions))
    )
