"""A keyword the grammar compiles must reach the grammar.

``scripts/schema_constraints.py`` has a production for 36 JSON Schema keywords.
A production is worth nothing if the keyword never gets handed to it, and twice
now it has not been: the ``allOf`` gate closed by a2989c205, and the
whole-object keywords of salesagent-09ghh. Both were a compiler with a shut
door, and in both cases the content was never EXAMINED rather than examined and
skipped -- which is why nothing failed.

09ghh had FOUR doors, each independently sufficient to lose the constraint, and
the first three were the ones filed:

1. the gate -- ``_states_logic`` did not admit a root carrying one, so the root
   was reported as stating nothing;
2. the reader -- ``_Parser.read`` looked at the combinator, ``if``,
   ``dependencies``, ``dependentRequired``, ``not`` and ``allOf`` members, and a
   top-level ``minProperties`` is in none of those positions. Widening the gate
   ALONE was measured to produce ten admitted roots and ten EMPTY validators;
3. the restatement rule -- ``_declaration_of`` kept them as "what the generator
   renders", so ``_keyword``'s equality test reported them already enforced;
4. the PRIMITIVE -- ``PropCount`` rendered to ``_c.length``, which is ``len()``,
   which raises ``TypeError`` on a pydantic object and so returned ``True`` for
   every object. With doors 1-3 open and this one shut, ten validators were
   emitted and ``minProperties``/``maxProperties`` still graded nothing. That
   door was not on the bead; it was found by probing the fix.

One live assertion per door, so a regression in any one of them fails here.
"""

from __future__ import annotations

import pytest

import adcp.types._conditions as _c

# ``from scripts.x import y``, like the other 25 test files that reach into
# scripts/, and NOT a ``sys.path`` insert onto scripts/ with a top-level import.
# ``scripts/compile_conditions.py`` and ``scripts/branch_classes.py`` both PREFER
# the top-level spelling and fall back to the package one, so putting scripts/ on
# ``sys.path`` makes BOTH spellings importable -- and then ``compile_condition``
# tests ``isinstance`` against ``schema_constraints.AllOf`` while a caller holds
# ``scripts.schema_constraints.AllOf``. Two classes, same name, so the check is
# False and the emitter reports a node it cannot render. Measured: with this file
# collected, ``AllOf`` had 4 bindings over 2 distinct class objects, and
# test_branch_classes and test_root_constraint_emission failed for that reason
# alone.
from scripts.compile_conditions import compile_condition
from scripts.schema_constraints import (
    COMPILED_KEYWORDS,
    DROPPED_KEYWORDS,
    LOGIC_KEYS,
    OPAQUE,
    PropCount,
    _declaration_of,
    read_root_constraints,
    root_carries_constraints,
)

#: The keywords ``datamodel-code-generator`` RENDERS, as a field constraint or as
#: the field itself. The gate's silence costs nothing for these: the generated
#: class already carries them, so a root restating one is restating something
#: true of every instance.
#:
#: Named rather than derived because the evidence is a spelling, not a fact about
#: this module -- ``minLength`` becomes ``min_length=``, ``minimum`` becomes
#: ``ge=``, ``type`` becomes the annotation itself -- and a derivation over those
#: spellings would be a second, looser statement of what codegen does. What IS
#: derived is the partition: this set plus the two dropped sets must be exactly
#: ``COMPILED_KEYWORDS``, so a production added to the grammar has to be
#: classified before this file passes.
RENDERED_BY_CODEGEN = frozenset(
    {
        "$ref",
        "additionalProperties",
        "const",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "type",
    }
)

#: A value that makes each keyword's node non-trivial, so the gate is asked a
#: real question rather than one about an empty node.
_SAMPLE = {
    "allOf": [{"type": "object"}],
    "anyOf": [{"type": "object"}],
    "oneOf": [{"type": "object"}],
    "contains": {"type": "string"},
    "dependencies": {"a": ["b"]},
    "dependentRequired": {"a": ["b"]},
    "dependentSchemas": {"a": {"type": "object"}},
    "else": {"type": "object"},
    "if": {"required": ["a"]},
    "maxProperties": 16,
    "minProperties": 1,
    "not": {"type": "string"},
    "patternProperties": {"^a": {"type": "string"}},
    "propertyNames": {"maxLength": 8},
    "then": {"type": "object"},
    "uniqueItems": True,
}


def test_every_compiled_keyword_is_either_dropped_or_rendered() -> None:
    """The partition is total, so a new production must be classified.

    This is the assertion that would have caught both instances of the defect
    class before they shipped: a keyword with a production and no home in either
    set is a compiler nothing feeds.
    """
    classified = LOGIC_KEYS | DROPPED_KEYWORDS | RENDERED_BY_CODEGEN
    unclassified = COMPILED_KEYWORDS - classified
    assert not unclassified, (
        f"{sorted(unclassified)} have a production in the grammar but are neither "
        "dropped by codegen (so the gate must admit them) nor rendered by it (so a "
        "root restating them is already enforced). A keyword in neither is a "
        "compiler with a shut door."
    )
    invented = classified - COMPILED_KEYWORDS - LOGIC_KEYS
    assert not invented, f"{sorted(invented)} are classified but the grammar compiles nothing"


#: Dropped keywords that cannot stand alone at a root, with the reason. ``then``
#: and ``else`` assert nothing without the ``if`` that selects them, and a root
#: ``allOf`` is admitted through its MEMBERS -- ``root_carries_constraints``
#: recurses, so ``{allOf: [{if: ...}]}`` is admitted while an ``allOf`` of
#: declarations correctly is not. Both are checked below instead of here.
_NOT_ALONE = frozenset({"allOf", "then", "else"})


@pytest.mark.parametrize("keyword", sorted((LOGIC_KEYS | DROPPED_KEYWORDS) - _NOT_ALONE))
def test_door_one_the_gate_admits_every_keyword_codegen_drops(keyword: str) -> None:
    """The ADMISSION gate says yes to a root carrying it.

    ``root_carries_constraints``, not ``_states_logic``: the gate is the public
    predicate and it recurses into ``allOf`` members, which is the distinction
    the first version of this test got wrong by asserting the inner helper.

    Live, not declarative. Before salesagent-09ghh this failed for all six
    members of ``DROPPED_KEYWORDS``, and it also failed for
    ``dependentSchemas`` -- a keyword with a production, named by
    ``LOGIC_KEYS``, that neither the gate nor the reader looked at. No pinned
    schema uses it, so that one was a LATENT instance of the same defect, found
    by this test rather than by a probe.
    """
    sample = _SAMPLE.get(keyword)
    assert sample is not None, f"{keyword} has no sample; add one to _SAMPLE"
    assert root_carries_constraints({"type": "object", keyword: sample}), (
        f"the gate reports a root carrying {keyword!r} as stating nothing, so its "
        "content is never examined"
    )


def test_door_one_admits_a_dropped_keyword_inside_an_all_of_member() -> None:
    """The position the gate was widened to read, and the one it must not over-read.

    A root ``allOf`` is inheritance for the declarative keys and a carrier of
    logic for the dropped ones, so a member carrying either is admitted while a
    member carrying only declarations is not.
    """
    for keyword in sorted(DROPPED_KEYWORDS | {"if"}):
        member = {"type": "object", keyword: _SAMPLE[keyword]}
        assert root_carries_constraints({"allOf": [member]}), keyword
    assert not root_carries_constraints(
        {"allOf": [{"type": "object", "properties": {"a": {"type": "string"}}}]}
    ), "an allOf of pure declarations states nothing and must not be admitted"
    for keyword in sorted(_NOT_ALONE - {"allOf"}):
        assert not root_carries_constraints(
            {"type": "object", keyword: _SAMPLE[keyword]}
        ), f"{keyword!r} alone asserts nothing -- it needs the 'if' that selects it"


@pytest.mark.parametrize("keyword", sorted(DROPPED_KEYWORDS))
def test_door_three_a_dropped_keyword_is_not_part_of_the_declaration(keyword: str) -> None:
    """``_declaration_of`` leaves it out, so the restatement rule cannot claim it.

    The generator renders none of these, so a root stating one is not restating
    the class. Keeping them made ``minProperties`` inside an ``allOf`` member
    compile against a declaration that had absorbed it.
    """
    node = {"type": "object", "properties": {"a": {"type": "string"}}, keyword: _SAMPLE[keyword]}
    assert keyword not in _declaration_of(node)
    # And through an allOf member, which is the position that made this visible.
    assert keyword not in _declaration_of({"allOf": [node]})


def test_door_two_the_reader_compiles_a_root_s_own_whole_object_keyword() -> None:
    """A top-level ``minProperties`` becomes a conjunct, not an empty validator.

    The reader used to look only at the combinator, the conditional positions
    and ``allOf`` members. Admission without this produced a validator block
    with nothing in it, which is indistinguishable from a passing check.
    """
    constraints = read_root_constraints(
        {"type": "object", "properties": {"a": {"type": "string"}}, "minProperties": 1},
        OPAQUE,
        branches_render_as_classes=False,
        schema_file="probe/min-properties.json",
    )
    assert constraints.conjuncts, "the root was admitted and compiled to no conjunct at all"
    assert any(
        isinstance(conjunct, PropCount) for conjunct in constraints.conjuncts
    ), f"minProperties compiled to {constraints.conjuncts!r}, not to a PropCount"


def test_door_four_prop_count_renders_to_the_primitive_that_can_see_an_object() -> None:
    """The WIRING, which is the half door 4 actually was.

    The first version of this file graded only the two primitives' behaviour,
    and reverting the renderer left all of it green -- the guard tested that
    ``_c.members`` works, not that ``minProperties`` reaches it. Found by
    reverting each door in turn and watching which tests moved; this one did
    not, so it was not yet grading its own door.
    """
    rendered = compile_condition(PropCount(minimum=1))
    assert "_c.members(" in rendered, (
        f"minProperties renders to {rendered!r}; _c.length cannot measure a "
        "pydantic object, so that is a validator which grades nothing"
    )
    assert "_c.length(" not in rendered
    assert "_c.members(" in compile_condition(PropCount(maximum=16))


def test_door_four_the_primitive_counts_a_model_s_members() -> None:
    """``PropCount`` renders to ``_c.members``, and that grades an object.

    ``_c.length`` cannot: it is ``len()``, a pydantic object has no ``__len__``,
    and the ``TypeError`` branch returns True. So the emitted validator passed
    every object while looking like a check. Graded on both primitives, because
    the defect was routing one keyword to the other.
    """

    class OneMember:
        model_fields_set = {"a"}
        model_extra = None

    instance = OneMember()
    assert _c.members(instance, minimum=1) is True
    assert _c.members(instance, minimum=2) is False, (
        "_c.members does not count a model's supplied members, so minProperties "
        "grades nothing on an object root"
    )
    assert _c.members({f"k{index}": 1 for index in range(22)}, maximum=16) is False
    # The primitive this was wrongly routed through still cannot see an object,
    # which is why the split was necessary rather than tidy.
    assert _c.length(instance, minimum=2) is True
    # And it still measures the four sizes it is for.
    assert _c.length("ab", minimum=3) is False
    assert _c.length([1], minimum=2) is False
