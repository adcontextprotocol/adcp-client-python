"""Compile a ``Cond`` tree into the Python a generated model carries.

``scripts/schema_constraints.py`` reads a schema root's constraint content into
a ``Cond`` tree. This module turns one of those trees into an expression over
``src/adcp/types/_conditions.py``'s primitives and ordinary Python, and wraps a
root's branches and conjuncts into the ``model_validator`` body the generator
appends to the class.

It invents no identifiers. Per-branch predicates are expressions inside the one
validator, every reusable part is a fixed name in the vocabulary module, and a
lambda parameter is named after the field it binds -- so the only names the
generator mints are class names, which come from the branch-identity rule. Titles
ride as schema-derived text in the refusal message and in comments.
"""

from __future__ import annotations

import keyword
from collections.abc import Sequence
from typing import Any

#: The JSON Schema keywords an injected runtime check may name.
#:
#: ``core/error.json`` requires every ``issues[]`` entry to carry a ``keyword``
#: "drawn from the JSON Schema vocabulary" and says not to invent values there,
#: so a check names the keyword of the construct it restores and nothing else.
#: Membership is checked rather than documented because an invented keyword
#: reaches the wire looking exactly like a real one.
SCHEMA_KEYWORDS = frozenset(
    {
        "allOf",
        "anyOf",
        "const",
        "contains",
        "dependentRequired",
        "else",
        "enum",
        "format",
        "if",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "not",
        "oneOf",
        "pattern",
        "propertyNames",
        "required",
        "then",
        "type",
        "uniqueItems",
    }
)


def schema_keyword_raise(
    keyword: str, message: str, *, indent: str = " " * 8, fields: Sequence[str] = ()
) -> str:
    """Render the ``raise`` statement an injected runtime check refuses with.

    ``PydanticCustomError`` sets pydantic's error ``type`` to its first argument
    and keeps the message verbatim, so ``e.errors()[0]["type"]`` is the schema
    keyword that rejected the payload — which is what a consumer needs for
    ``error.issues[].keyword``. A bare ``ValueError`` becomes ``value_error``,
    which names no keyword, and a consumer assembling a conformant envelope then
    has to drop the issue and refuse the buyer with no structured reason.

    The caller passes the keyword the SCHEMA states, never one read off the
    message: the English is written for a human and two different keywords can
    share a phrasing.

    *fields* are the field names the refusal cites, and they go in the error
    CONTEXT rather than only in the message. The authored sentence carries them
    too, but a consumer cannot read them out of it: the same string also names
    the model class, which is producer-internal, so the projection that strips
    the class name has to discard the whole sentence and render the keyword
    alone (``adcp.validation.pydantic_errors._message``). The context is the one
    place the names arrive structured, and the projection reads them from
    ``ctx['fields']`` — a comma-joined string, which is the spelling
    ``adcp.types._conditions`` already uses for its own forbidden-name refusal.

    Omitted entirely when there are no names. A condition that reads no field is
    ordinary, and ``{'fields': ''}`` would put an empty parenthetical in front of
    the buyer where the keyword alone reads correctly.
    """
    if keyword not in SCHEMA_KEYWORDS:
        raise ValueError(f"{keyword!r} is not a JSON Schema keyword an issue may name")
    context = f"{indent}    {{'fields': {', '.join(fields)!r}}},\n" if fields else ""
    return (
        f"{indent}raise PydanticCustomError(\n"
        f"{indent}    {keyword!r},\n"
        f"{indent}    {message!r},\n"
        f"{context}"
        f"{indent})"
    )


try:
    from schema_constraints import (
        AllOf,
        AnyOf,
        Branch,
        Cond,
        DependentRequired,
        EveryItem,
        IfThen,
        KeysSatisfy,
        MatchingKeysSatisfy,
        NoOtherProps,
        Not,
        OneOf,
        OtherPropsSatisfy,
        PropCount,
        PropSatisfies,
        Restated,
        RootConstraints,
        SomeItem,
        Supplied,
        Undeclarable,
        ValueBounds,
        ValueConst,
        ValueEnum,
        ValueItems,
        ValueLen,
        ValuePattern,
        ValueType,
        ValueUnique,
    )
except ModuleNotFoundError:  # Imported as ``scripts.compile_conditions`` in tests.
    from scripts.schema_constraints import (  # type: ignore[assignment]
        AllOf,
        AnyOf,
        Branch,
        Cond,
        DependentRequired,
        EveryItem,
        IfThen,
        KeysSatisfy,
        MatchingKeysSatisfy,
        NoOtherProps,
        Not,
        OneOf,
        OtherPropsSatisfy,
        PropCount,
        PropSatisfies,
        Restated,
        RootConstraints,
        SomeItem,
        Supplied,
        Undeclarable,
        ValueBounds,
        ValueConst,
        ValueEnum,
        ValueItems,
        ValueLen,
        ValuePattern,
        ValueType,
        ValueUnique,
    )

__all__ = [
    "CONDITIONS_IMPORT",
    "COMBINATOR_FIELDS",
    "GROUP_MESSAGE_PREFIX",
    "ROOT_CONSTRAINT_VALIDATOR",
    "branch_label",
    "compile_condition",
    "named_fields",
    "render_root_validator",
]

#: The import a module carrying a compiled validator needs.
CONDITIONS_IMPORT = "from adcp.types import _conditions as _c"

#: The validator's name. Kept across the change from a required-names-only
#: check to the whole root constraint, because the name is what
#: ``tests/test_validation_parity.py`` derives its graded population from --
#: membership is "the generator put the validator on this model", read off
#: ``__pydantic_decorators__``, not a list of ids kept there.
ROOT_CONSTRAINT_VALIDATOR = "_require_schema_required_group"

#: Two contract tests match on this wording
#: (``tests/test_codegen_contract_compatibility.py``). The branches' schema titles
#: are appended to it; changing the prefix breaks those tests for a
#: non-behavioural reason.
GROUP_MESSAGE_PREFIX = "requires at least one of these field groups"

#: The subject expression at the top of a validator body.
_SELF = "self"

#: The lambda parameter bound to an array element or a mapping member. A fixed
#: vocabulary name rather than a singular invented from the field's name.
_ELEMENT = "item"
_KEY = "name"

#: Where a compiled expression is wrapped onto further lines.
_WIDTH = 96


def _parameter(name: str) -> str:
    """A lambda parameter named after the field it binds."""
    return f"{name}_" if keyword.iskeyword(name) or name in (_SELF, _ELEMENT, _KEY) else name


def _grouped(expression: str) -> str:
    """Parenthesise an expression only where precedence needs it.

    A conjunction of simple membership tests reads as one; wrapping every
    operand turns the emitted branch into brackets.
    """
    depth = 0
    for index, character in enumerate(expression):
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif depth == 0 and expression.startswith((" and ", " or ", " if "), index):
            return f"({expression})"
    return expression


def _bounds(call: str, subject: str, **bounds: Any) -> str:
    named = ", ".join(f"{key}={value!r}" for key, value in bounds.items() if value is not None)
    return f"_c.{call}({subject}{', ' + named if named else ''})"


def compile_condition(condition: Cond, subject: str = _SELF) -> str:  # noqa: C901, PLR0911, PLR0912
    """One ``Cond`` as a Python expression over ``subject``."""
    if isinstance(condition, Restated):
        # Already enforced by the class's own declaration, so true of every
        # instance -- which is what makes substituting it sound here and under
        # a negation alike.
        return "True"
    if isinstance(condition, Undeclarable):
        # A key no field holds on a model that FORBIDS extras: pydantic refuses
        # the document before the validator runs, so the predicate is false of
        # every instance that reaches here. Which is what makes the branch
        # forbidding the key satisfiable and the branch requiring it dead.
        return "False"

    if isinstance(condition, Supplied):
        # ``supplied`` is membership; ``sent`` adds the non-null half where the
        # schema forbids null, because the client drops such a key on the way
        # out and the document that arrives does not carry it.
        primitive = "supplied" if condition.nullable else "sent"
        return f"_c.{primitive}({subject}, {condition.name!r})"

    if isinstance(condition, Not):
        return f"not ({compile_condition(condition.operand, subject)})"
    if isinstance(condition, AllOf):
        return " and ".join(
            _grouped(compile_condition(operand, subject)) for operand in condition.operands
        )
    if isinstance(condition, AnyOf):
        return " or ".join(
            _grouped(compile_condition(operand, subject)) for operand in condition.operands
        )
    if isinstance(condition, OneOf):
        branches = ", ".join(compile_condition(operand, subject) for operand in condition.operands)
        return f"[{branches}].count(True) == 1"
    if isinstance(condition, IfThen):
        consequent = (
            compile_condition(condition.consequent, subject)
            if condition.consequent is not None
            else "True"
        )
        alternative = (
            compile_condition(condition.alternative, subject)
            if condition.alternative is not None
            else "True"
        )
        test = compile_condition(condition.condition, subject)
        return f"({_grouped(consequent)} if {_grouped(test)} else {_grouped(alternative)})"

    if isinstance(condition, PropSatisfies):
        bound = _parameter(condition.name)
        inner = compile_condition(condition.operand, bound)
        return f"_c.descend({subject}, {condition.name!r}, lambda {bound}: {inner})"
    if isinstance(condition, EveryItem):
        inner = compile_condition(condition.operand, _ELEMENT)
        return f"_c.every({subject}, lambda {_ELEMENT}: {inner})"
    if isinstance(condition, SomeItem):
        inner = compile_condition(condition.operand, _ELEMENT)
        return f"_c.some({subject}, lambda {_ELEMENT}: {inner})"
    if isinstance(condition, KeysSatisfy):
        inner = compile_condition(condition.operand, _KEY)
        return f"_c.keys({subject}, lambda {_KEY}: {inner})"
    if isinstance(condition, MatchingKeysSatisfy):
        inner = compile_condition(condition.operand, _ELEMENT)
        return f"_c.matching_keys({subject}, {condition.pattern!r}, lambda {_ELEMENT}: {inner})"
    if isinstance(condition, NoOtherProps):
        return f"_c.only({subject}, {list(condition.names)!r})"
    if isinstance(condition, OtherPropsSatisfy):
        inner = compile_condition(condition.operand, _ELEMENT)
        return f"_c.others({subject}, {list(condition.names)!r}, lambda {_ELEMENT}: {inner})"

    if isinstance(condition, ValueConst):
        return f"{subject} == {condition.value!r}"
    if isinstance(condition, ValueEnum):
        return f"{subject} in {list(condition.values)!r}"
    if isinstance(condition, ValueType):
        names = ", ".join(repr(name) for name in condition.types)
        return f"_c.json_type({subject}, {names})"
    if isinstance(condition, ValuePattern):
        return f"_c.matches({subject}, {condition.pattern!r})"
    if isinstance(condition, (ValueLen, ValueItems)):
        return _bounds("length", subject, minimum=condition.minimum, maximum=condition.maximum)
    if isinstance(condition, PropCount):
        # A count of members, not a ``len``. ``_c.length`` cannot measure a
        # pydantic object at all, so routing these two through it emitted a
        # validator that graded nothing -- the fourth closed door behind
        # salesagent-09ghh, and the one that made the other three look sufficient.
        return _bounds("members", subject, minimum=condition.minimum, maximum=condition.maximum)
    if isinstance(condition, ValueBounds):
        return _bounds(
            "within", subject, ge=condition.ge, le=condition.le, gt=condition.gt, lt=condition.lt
        )
    if isinstance(condition, ValueUnique):
        return f"_c.unique({subject})"

    if isinstance(condition, DependentRequired):
        trigger = f"_c.supplied({subject}, {condition.name!r})"
        dependents = " and ".join(f"_c.supplied({subject}, {name!r})" for name in condition.names)
        return f"not ({trigger}) or {_grouped(dependents)}"

    raise TypeError(f"no emission for {type(condition).__name__}")


def named_fields(condition: Cond) -> tuple[str, ...]:
    """Every field name a condition reads, sorted, for the refusal to name.

    Schema-derived: these are the properties the node itself mentions, not text
    this generator composes. A refusal that names only the schema file and the
    conjunct's index tells a caller which rule fired and nothing about where to
    look; ``agent-notification-config.json``'s conditional reads ``event_types``
    and ``all_authorized_accounts``, and naming them is the difference between a
    diagnostic and a citation.
    """
    found: set[str] = set()

    def walk(node: Cond) -> None:
        if isinstance(node, (Supplied, Undeclarable)):
            found.add(node.name)
        elif isinstance(node, PropSatisfies):
            found.add(node.name)
            walk(node.operand)
        elif isinstance(node, DependentRequired):
            found.add(node.name)
            found.update(node.names)
        elif isinstance(node, (NoOtherProps, OtherPropsSatisfy)):
            found.update(node.names)
            if isinstance(node, OtherPropsSatisfy):
                walk(node.operand)
        elif isinstance(node, Not):
            walk(node.operand)
        elif isinstance(node, (AllOf, AnyOf, OneOf)):
            for operand in node.operands:
                walk(operand)
        elif isinstance(node, IfThen):
            walk(node.condition)
            for branch in (node.consequent, node.alternative):
                if branch is not None:
                    walk(branch)
        elif isinstance(node, (EveryItem, SomeItem, KeysSatisfy, MatchingKeysSatisfy)):
            walk(node.operand)

    walk(condition)
    return tuple(sorted(found))


def branch_label(branch: Branch) -> str:
    """How an branch names itself in a refusal message.

    Schema-derived text in all three cases: the branch's own title, else the field
    group it requires, else its pointer. A title is both more readable than a
    synthesized field list and the thing the schema actually says.
    """
    if branch.title:
        return branch.title
    if branch.required:
        return "+".join(branch.required)
    return branch.pointer


def _wrap(expression: str, indent: str) -> str:
    """Break a long expression at its top-level ``and``/``or`` boundaries."""
    if len(expression) + len(indent) <= _WIDTH:
        return f"{indent}{expression}"
    parts: list[str] = []
    depth = 0
    current = ""
    for token in expression.split(" "):
        if token in {"and", "or"} and depth == 0 and current:
            parts.append(current)
            current = token
            continue
        current = f"{current} {token}" if current else token
        depth += token.count("(") - token.count(")")
        depth += token.count("[") - token.count("]")
    if current:
        parts.append(current)
    if len(parts) == 1:
        return f"{indent}{expression}"
    return f"\n{indent}".join(parts).join((indent, ""))


def render_root_validator(
    constraints: RootConstraints,
    class_name: str,
    schema_file: str,
    subject: str = _SELF,
) -> str:
    """The ``model_validator`` body a flat root's class carries.

    One validator per root. The combinator check comes first, then each
    conjunct beside it, each with the refusal the schema's own words support.
    Returns the empty string when the root states nothing a predicate can
    carry, so a class gains no validator for a constraint that is already a
    type.

    ``subject`` is what the document is on this class. For a model it is the
    model; for a ``RootModel`` over an open value it is ``self.root``, and the
    predicates then read that value's members as a mapping -- which is what the
    vocabulary's two-shape handling is for.
    """
    body: list[str] = []
    if constraints.branches:
        body.extend(_render_combinator(constraints, class_name, subject))
    for index, conjunct in enumerate(constraints.conjuncts):
        body.extend(_render_conjunct(conjunct, class_name, schema_file, index, subject))
    if not body:
        return ""

    return (
        "\n".join(
            [
                *_render_root_constraint_fields(constraints),
                "",
                "    @model_validator(mode='after')",
                f"    def {ROOT_CONSTRAINT_VALIDATOR}(self) -> {class_name}:",
                f"        # Compiled from {schema_file}: everything the root states beyond",
                "        # its merged properties, which datamodel-code-generator drops.",
                "        #",
                "        # Presence is membership in model_fields_set, which is what",
                "        # ``required`` asks. An explicit null is a supplied value -- on a",
                "        # mutation input it is the command to clear -- and a default the",
                "        # caller never sent is not.",
                *body,
                "        return self",
            ]
        )
        + "\n"
    )


#: The attribute a root carries naming the fields its COMBINATOR reads. Named for the
#: combinator specifically, NOT for root constraints in general: see
#: :func:`_render_root_constraint_fields` for what it excludes and why the narrower
#: name is the point rather than an abbreviation.
COMBINATOR_FIELDS = "_COMBINATOR_FIELDS"


def _render_root_constraint_fields(constraints: RootConstraints) -> list[str]:
    """The combinator's fields, declared on the class for consumers to read.

    A root ``anyOf``/``oneOf`` says "at least one of these groups", so each named
    field is INDIVIDUALLY OPTIONAL and COLLECTIVELY REQUIRED -- a distinction
    ``model_fields[...].is_required()`` cannot express, because it is per field.
    Any consumer deriving a keep-set from per-field requiredness therefore drops
    one of them and produces a document the model refuses.

    Measured on ``get_products``: the fields projection kept the 7 fields
    ``Product`` requires individually, dropped ``format_options`` because it is
    individually optional, and the re-validation then failed the root's own
    ``sent(format_ids) or sent(format_options)`` -- ten tests, one cause
    (salesagent-8qela.86).

    THE COMBINATOR'S FIELDS ONLY, not every field the constraints read. Measured
    on ``Product``: its 12 conjuncts read 20 of its 59 fields, so declaring all
    of them would have a buyer asking for ``product_id`` alone receive 27 fields
    -- it passes the suite and defeats the feature. The combinator's 2 are
    sufficient: all ten tests pass, and the eight that already passed stay
    passing, including the one grading that an unrequested field IS dropped.

    THE NAME IS THE BOUND, and it is narrow on purpose. This is NOT the complete
    set of fields a root constraint can require. A conjunct can also flip on
    absence -- a disjunction with a NEGATED presence arm,

        (not supplied(A)) or supplied(B)

    goes false when ``B`` is dropped and ``A`` is kept. Measured on the pin: 27
    per-domain schemas carry a presence-implication keyword (``dependencies``,
    ``dependentRequired``, ``dependentSchemas``) -- ``core/media-buy.json`` over
    ``accepted_proposal_id``, ``core/error.json`` over ``buyer_reason``,
    ``core/delivery-recipient.json`` over ``cloud`` and ``region``, among others
    -- and ``core/product.json``, the one this was built for, carries NONE. So
    the gap is a fact about the pin and not a theoretical ceiling, and a consumer
    reading this attribute for any of those 27 would be reading an incomplete
    answer under a complete-sounding name.

    Hence ``_COMBINATOR_FIELDS`` rather than ``_ROOT_CONSTRAINT_FIELDS``. The
    complete rule is "every field whose absence can flip a predicate", which
    needs a polarity walk over the condition tree; if anyone ever needs it, it
    arrives as a SECOND attribute without changing what this one means. Nobody
    has to discover the bound by being bitten by it.
    """
    fields = sorted(
        {name for branch in constraints.branches for name in named_fields(branch.condition)}
    )
    if not fields:
        return []
    names = ", ".join(repr(name) for name in fields)
    return [
        "",
        f"    {COMBINATOR_FIELDS}: ClassVar[frozenset[str]] = frozenset({{{names}}})",
    ]


def _render_combinator(constraints: RootConstraints, class_name: str, subject: str) -> list[str]:
    labels = [branch_label(branch) for branch in constraints.branches]
    # The branch's own condition, not the branch conjoined with the root's conjuncts:
    # each conjunct is rendered once below, with the refusal naming it. The
    # conjoined form is what the disjointness prover and the branch-class
    # emitter need, where a conjunct has to distribute into an branch's shape.
    predicates = [compile_condition(branch.condition, subject) for branch in constraints.branches]

    lines = ["        if not ("]
    for index, (label, predicate) in enumerate(zip(labels, predicates, strict=True)):
        lines.append(f"            # {label}")
        lines.append(f"            {'' if index == 0 else 'or '}(")
        lines.append(_wrap(predicate, " " * 16))
        lines.append("            )")
    lines.append("        ):")
    message = f"{class_name} {GROUP_MESSAGE_PREFIX}: {' | '.join(labels)}"
    # The keyword the SCHEMA states, never one read off the message: a consumer
    # needs it for ``error.issues[].keyword``, which ``core/error.json`` requires
    # and tells it not to invent. A bare ValueError arrives as ``value_error``,
    # names no keyword, and the consumer then drops the issue and refuses the
    # buyer with nothing structured. This is #1435, which landed in beta.3 on
    # the function this series replaced -- the keyword is still known here,
    # because the reader records which combinator the branches came from.
    lines.append(schema_keyword_raise(constraints.combinator or "anyOf", message, indent=" " * 12))

    # ``oneOf`` is exactly one, which is derivable now that each branch's
    # predicate is its whole condition. The required-names-only reading could
    # not derive it, and that is the reason its docstring gave for not trying.
    # An branch carrying ``x-adcp-validation`` is excepted: its cardinality
    # depends on rules declared outside the structural models' promise.
    if (
        constraints.combinator == "oneOf"
        and len(constraints.branches) > 1
        and not constraints.declares_behavioral_rules
    ):
        satisfied = ", ".join(f"({predicate})" for predicate in predicates)
        exclusive = (
            f"{class_name} satisfies more than one of these mutually exclusive "
            f"field groups: {' | '.join(labels)}"
        )
        lines.append(f"        if [{satisfied}].count(True) != 1:")
        lines.append(schema_keyword_raise("oneOf", exclusive, indent=" " * 12))
    return lines


def _render_conjunct(
    conjunct: Cond, class_name: str, schema_file: str, index: int, subject: str
) -> list[str]:
    predicate = compile_condition(conjunct, subject)
    if predicate == "True":
        return []
    named = named_fields(conjunct)
    over = f" over {', '.join(named)}" if named else ""
    message = (
        f"{class_name} violates a constraint its schema states outside its "
        f"properties ({schema_file}, root conjunct {index}){over}"
    )
    # A root conjunct is an ``allOf`` member: that is the keyword a consumer
    # reads for a constraint the root states beside its combinator.
    #
    # ``named`` goes to the context as well as into the sentence. The sentence
    # cannot deliver them: it also names the model class, so the projection that
    # strips producer-internal text drops the whole string and the buyer was
    # told "allOf composition failed" with no place to look. One derivation,
    # both destinations (salesagent-8qela.49).
    return [
        "        if not (",
        _wrap(predicate, " " * 12),
        "        ):",
        schema_keyword_raise("allOf", message, indent=" " * 12, fields=named),
    ]
