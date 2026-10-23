"""What an branch becomes when it is a type rather than a rule.

``scripts/branch_classes.py`` reads one ``Branch`` record and says three things
about the class that branch becomes: which fields it must not declare, which it
must require, and what is left over for a compiled check. The first two are why
a branch class makes a combination unspellable instead of refusing it, so each
is graded here on its own, and the leftover is graded for what it KEEPS rather
than only for what it drops -- a planner that quietly swallowed an branch's nested
content would read as a stronger model and be a weaker one.

The emitter is not wired into the generation pipeline. Four places used a split
root's public name as a BASE, and a union cannot be subclassed. The shared base
settles two of them -- ``Creative(CreativeAsset)`` and
``CreativeRepresentation(CreativeManifest)`` are generated, and the emitter
repoints them onto the base, which is graded below. The remaining two are the
hand-written create-media-buy layers (``canonical_creative`` narrows
``packages``, and ``_forward_compat`` rebuilds the model and declares a
static view of a runtime-patched field); those narrow PER BRANCH, so each needs a
class per branch and its own union, and that is the integration step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from adcp.types._split_root import branch_members
from scripts.branch_classes import (
    plan_branches,
    render_absence_hook,
    repoint_subclasses_of,
    split_into_branch_classes,
)
from scripts.schema_constraints import (
    DeclaredShape,
    PropSatisfies,
    Restated,
    ValueConst,
    read_root_constraints,
)


def _constraints(root: dict[str, Any], fields: dict[str, str]) -> Any:
    return read_root_constraints(
        root,
        DeclaredShape(fields=fields, nested={name: DeclaredShape() for name in fields}),
        branches_render_as_classes=False,
    )


_TWO_ARMS: dict[str, Any] = {
    "title": "Thing",
    "type": "object",
    "properties": {"a": {}, "b": {}, "c": {}},
    "anyOf": [
        {"required": ["a"], "not": {"required": ["b"]}},
        {"required": ["b"], "not": {"required": ["a"]}},
    ],
}


def test_an_branch_forbids_a_field_by_not_declaring_it() -> None:
    """``not: {required: [b]}`` is the absence of the field, not a check on it."""
    first, second = plan_branches(
        _constraints(_TWO_ARMS, {"a": "a", "b": "b", "c": "c"}),
        "Thing",
        "core/thing.json",
        frozenset({"a", "b", "c"}),
    )

    assert first.absent == frozenset({"b"})
    assert first.required == frozenset({"a"})
    assert second.absent == frozenset({"a"})
    assert second.required == frozenset({"b"})
    assert first.residual == Restated("branch") and second.residual == Restated(
        "branch"
    ), "requiredness and absence are the whole of these branches, so no check is left"


def test_a_banned_pair_stays_a_check_because_neither_name_is_forbidden() -> None:
    """``not: {required: [a, b]}`` forbids the COMBINATION, not either field.

    Dropping either field would refuse a document the schema accepts, so the
    planner leaves the conjunct to the compiled check. ``webhook-challenge-
    response`` states its ban this way.
    """
    root = {
        "title": "Thing",
        "type": "object",
        "properties": {"a": {}, "b": {}},
        "not": {"required": ["a", "b"]},
        "anyOf": [{"required": ["a"]}, {"required": ["b"]}],
    }

    first, _second = plan_branches(
        _constraints(root, {"a": "a", "b": "b"}), "Thing", "core/thing.json", frozenset({"a", "b"})
    )

    assert first.absent == frozenset()
    assert first.residual != Restated("branch"), "the ban must survive as a check"


def test_a_const_on_a_declared_field_becomes_a_literal_not_a_check() -> None:
    """A discriminator is the field's type on that branch.

    The sibling ``type`` is dropped only because the ``const`` it accompanies
    becomes the annotation; without that, dropping it would lose a constraint.
    """
    root = {
        "title": "Thing",
        "type": "object",
        "properties": {"status": {}, "a": {}},
        "oneOf": [
            {
                "required": ["status", "a"],
                "properties": {"status": {"type": "string", "const": "completed"}},
            },
            {"required": ["status"], "properties": {"status": {"const": "failed"}}},
        ],
    }

    first, second = plan_branches(
        _constraints(root, {"status": "status", "a": "a"}),
        "Thing",
        "core/thing.json",
        frozenset({"status", "a"}),
    )

    assert first.literals == {"status": "completed"}
    assert second.literals == {"status": "failed"}
    assert first.residual == Restated("branch")


def test_a_nested_const_stays_a_check_because_the_field_is_not_the_subject() -> None:
    """``budget_allocation.mode`` is one level down, so the branch's field set cannot carry it.

    Named as a check rather than silently dropped: this is the measured limit of
    what the branch class's own shape expresses, and what the branch's compiled
    validator is for.
    """
    root = {
        "title": "Thing",
        "type": "object",
        "properties": {"budget_allocation": {}, "packages": {}},
        "anyOf": [
            {
                "required": ["packages"],
                "properties": {"budget_allocation": {"properties": {"mode": {"const": "fixed"}}}},
            },
            {"required": ["budget_allocation"]},
        ],
    }

    first, _ = plan_branches(
        _constraints(root, {"budget_allocation": "budget_allocation", "packages": "packages"}),
        "Thing",
        "core/thing.json",
        frozenset({"budget_allocation", "packages"}),
    )

    assert first.literals == {}
    assert first.residual == PropSatisfies(
        "budget_allocation", PropSatisfies("mode", ValueConst("fixed"))
    )


def test_an_absence_set_is_also_refused_on_the_raw_document() -> None:
    """A field that is not declared is unspellable; a raw key still needs refusing.

    The generated models are ``extra='allow'`` where their schemas say
    ``additionalProperties: true``, so pydantic would file the forbidden key
    under extras and accept. The hook is what the design names as the one place
    the type system alone cannot carry an branch's content.
    """
    first, _ = plan_branches(
        _constraints(_TWO_ARMS, {"a": "a", "b": "b", "c": "c"}),
        "Thing",
        "core/thing.json",
        frozenset({"a", "b", "c"}),
    )

    hook = render_absence_hook(first)

    assert "_ABSENT: ClassVar[frozenset[str]] = frozenset({'b'})" in hook
    assert "mode='before'" in hook
    assert (
        "_c.absent(\n            data, sorted(cls._ABSENT), sorted(cls._ABSENT_NULLABLE)\n        )"
    ) in hook


_FLAT_MODULE = '''from __future__ import annotations

from pydantic import Field

from adcp.types.base import AdCPBaseModel


class Thing(AdCPBaseModel):
    """A flat root over both branches."""

    a: str | None = None
    b: str | None = None
    c: str | None = None
'''


def test_the_split_module_declares_each_branch_and_a_union_over_them() -> None:
    """The root's public name becomes the alias; a member is what one constructs."""
    rewritten = split_into_branch_classes(
        _FLAT_MODULE,
        "Thing",
        _constraints(_TWO_ARMS, {"a": "a", "b": "b", "c": "c"}),
        "core/thing.json",
        frozenset({"a", "b", "c"}),
    )

    assert rewritten is not None
    assert "class Thing(BranchDispatchMixin, AdCPBaseModel):" in rewritten
    assert "class ThingA(Thing):" in rewritten
    assert "class ThingB(Thing):" in rewritten
    assert "ThingBranches: TypeAlias = Annotated[" in rewritten
    assert "    ThingA | ThingB," in rewritten
    # No assertion that a module-level ``ThingBranchesAdapter`` is emitted: it is
    # not, because a ``TypeAdapter`` built at import captures schema state at
    # import, which ``test_sdk_has_no_module_level_type_adapter_construction``
    # forbids. The union alias above is what the emitter owes a caller; building
    # an adapter over it is one line at the call site.
    base = rewritten.split("class Thing(BranchDispatchMixin, AdCPBaseModel):")[1].split(
        "class ThingA"
    )[0]
    assert "a: str | None = None" in base
    assert "b: str | None = None" in base
    assert "c: str | None = None" in base, "a field no branch constrains is declared once"
    # The branch that requires ``a`` restates only what it changes: ``a`` loses its
    # default, ``b`` becomes unpassable, and ``c`` is left to the base.
    branch = rewritten.split("class ThingA(Thing):")[1].split("class ThingB")[0]
    assert "a: str\n" in branch
    assert "b: None = None" in branch
    assert "c:" not in branch


def test_every_branch_is_a_subclass_of_one_base_so_the_union_is_one_type() -> None:
    """The branches differ in requiredness, so they are one type -- not several.

    Measured over the eleven roots the emitter splits: every one has properties
    shared by all of its branches, and eight of the eleven have branches that constrain
    no property at all. A union of unrelated classes would say the branches are
    different types, and nothing in the schemas says that. This is also what
    gives the union a usable base: a caller can hold the shared fields, and a
    class elsewhere can subclass what the branches share.
    """
    from scripts.post_generate_fixes import compose_branch_module

    rewritten = compose_branch_module(
        _FLAT_MODULE,
        "Thing",
        _constraints(_TWO_ARMS, {"a": "a", "b": "b", "c": "c"}),
        "core/thing.json",
        frozenset({"a", "b", "c"}),
    )

    assert rewritten is not None
    namespace: dict[str, Any] = {}
    exec(  # noqa: S102 - the emitter's output is the subject; importing it is the test
        rewritten.replace("from __future__ import annotations", ""), namespace
    )
    base = namespace["Thing"]
    members = branch_members(namespace["ThingBranches"])

    assert len(members) == 2
    assert all(issubclass(member, base) for member in members)
    assert {"a", "b", "c"} <= set(base.model_fields), "the base carries what the branches share"
    # The adapter is built HERE rather than read out of the module, because the
    # emitter no longer declares one at module level. The assertions are
    # unchanged: the union parses a single-branch document into that branch and
    # refuses one satisfying two, which is what "the union is one type" means.
    # The adapter was only ever the instrument for asking.
    adapter = TypeAdapter(namespace["ThingBranches"])
    assert adapter.validate_python({"a": "x"}).a == "x"
    with pytest.raises(ValidationError):
        adapter.validate_python({"a": "x", "b": "y"})


def test_the_base_is_public_because_a_consumer_needs_to_name_it() -> None:
    """Stated as a test so the visibility decision is recorded where it binds.

    The base exists so the branches are ONE type, and a type nobody can name or
    annotate is not one -- a method that behaves per branch has to take something,
    and an ``isinstance`` check has to test something. So the base is public,
    and this asserts the two things that makes true of the emitted module: no
    leading underscore, and a name the next pass's ``__all__`` can carry.

    The earlier version of this test asserted the opposite. It was decided on
    who does ``isinstance`` against a split root TODAY, which is a measurement
    of current usage of a type that did not exist yet -- it could not answer the
    question. Four consumers appeared as soon as the split landed.
    """
    rewritten = split_into_branch_classes(
        _FLAT_MODULE,
        "Thing",
        _constraints(_TWO_ARMS, {"a": "a", "b": "b", "c": "c"}),
        "core/thing.json",
        frozenset({"a", "b", "c"}),
    )

    assert rewritten is not None
    assert (
        "class Thing(BranchDispatchMixin," in rewritten
    ), "the base takes the root's own name and mixes in the one shared hook"
    assert not rewritten.count("class _Thing"), "nothing module-internal is emitted for the root"


def test_a_class_that_subclassed_the_flat_root_is_repointed_onto_the_base() -> None:
    """A union cannot be subclassed, so a subclass has to name the base.

    Two generated classes do this on the pin -- ``Creative(CreativeAsset)`` and
    ``CreativeRepresentation(CreativeManifest)`` -- and the rewrite is what lets
    the split ship without either of them losing the fields it was reaching for.
    """
    module = (
        "from ..core.thing import Thing\n"
        "\n"
        "\n"
        "class Extended(Thing):\n"
        "    extra: str | None = None\n"
    )

    rewritten = repoint_subclasses_of(module, "Thing")

    assert rewritten is not None
    assert "class Extended(Thing):" in rewritten
    assert "from ..core.thing import Thing" in rewritten
    assert repoint_subclasses_of(module, "Other") is None, "a module naming it in no base list"

    # A base nothing imports is a module that fails at import, so the rewrite
    # refuses rather than emitting one. The shape it understands is a single-line
    # import, which is what both sites on the pin use.
    with pytest.raises(RuntimeError, match="to extend with Thing"):
        repoint_subclasses_of(
            "from ..core.thing import (\n    Thing,\n)\n\n\nclass Extended(Thing):\n    pass\n",
            "Thing",
        )


def test_the_split_drops_a_combinator_check_the_branches_shape_already_answers() -> None:
    """A flat validator left by the constraint pass must not be copied per branch.

    Copied, it would re-ask the whole combinator on every branch and would carry a
    return annotation naming the union. Its decorator is what makes it run, and
    an earlier version of the copier dropped decorators while keeping bodies --
    a validator that reads as one and enforces nothing.
    """
    with_validator = _FLAT_MODULE + """
    @model_validator(mode='after')
    def _require_schema_required_group(self) -> Thing:
        if not (True):
            raise ValueError('nope')
        return self
"""

    rewritten = split_into_branch_classes(
        with_validator,
        "Thing",
        _constraints(_TWO_ARMS, {"a": "a", "b": "b", "c": "c"}),
        "core/thing.json",
        frozenset({"a", "b", "c"}),
    )

    assert rewritten is not None
    assert "raise ValueError('nope')" not in rewritten


def test_create_media_buys_three_branches_plan_as_the_design_measured_them() -> None:
    """The worked example, read off the pinned bundle rather than restated.

    Each branch's absence set is what makes the two red parity cases that combine
    branches unspellable: no class carries both ``packages`` and ``proposal_id``.
    """
    from scripts.post_generate_fixes import SCHEMA_DIR, flat_root_shape
    from scripts.schema_constraints import bundle_document_resolver

    relative = Path("media-buy/create-media-buy-request.json")
    schema = json.loads((SCHEMA_DIR / relative).read_text())
    shape = flat_root_shape(schema, relative)
    assert shape is not None
    constraints = read_root_constraints(
        schema,
        shape,
        branches_render_as_classes=False,
        schema_file=str(relative),
        resolve_document=bundle_document_resolver(SCHEMA_DIR),
    )

    plans = plan_branches(
        constraints, "CreateMediaBuyRequest", str(relative), frozenset(shape.fields.values())
    )

    by_name = {plan.name: plan for plan in plans}
    fixed = by_name["CreateMediaBuyRequestExplicitPackagesWithFixedAllocation"]
    proposal = by_name["CreateMediaBuyRequestCommittedProposalExecution"]

    assert fixed.absent == frozenset({"proposal_id"})
    assert "packages" in fixed.required
    assert proposal.absent == frozenset({"packages", "budget_allocation", "frequency_cap"})
    assert proposal.required >= frozenset({"proposal_id", "total_budget"})
    assert not (fixed.absent & fixed.required), "a field cannot be both forbidden and required"


def test_no_plan_forbids_a_field_it_also_requires() -> None:
    """Over the whole partitioning population, not just the worked example."""
    from scripts.post_generate_fixes import (
        SCHEMA_DIR,
        _schema_root_class_name,
        flat_root_shape,
    )
    from scripts.schema_constraints import (
        bundle_document_resolver,
        partitions_by_requiredness,
        root_carries_constraints,
    )

    resolve = bundle_document_resolver(SCHEMA_DIR)
    graded = 0
    for schema_file in sorted(SCHEMA_DIR.rglob("*.json")):
        relative = schema_file.relative_to(SCHEMA_DIR)
        if relative.parts[0] in ("mcp", "bundled"):
            continue
        try:
            schema = json.loads(schema_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(schema, dict) or not root_carries_constraints(schema):
            continue
        shape = flat_root_shape(schema, relative)
        if shape is None:
            continue
        constraints = read_root_constraints(
            schema,
            shape,
            branches_render_as_classes=False,
            schema_file=str(relative),
            resolve_document=resolve,
        )
        if not constraints.combinator or not partitions_by_requiredness(constraints):
            continue
        # The root's NAME, which the schema states. Not the class the module
        # declares: after the split there is no flat class, and the shape that
        # carries the properties is called <Root>Shape -- deriving branch names
        # from that spells them <Root>Shape<Branch>.
        parent = _schema_root_class_name(schema)
        assert parent is not None
        for plan in plan_branches(
            constraints, parent, str(relative), frozenset(shape.fields.values())
        ):
            graded += 1
            assert not (plan.absent & plan.required), (relative, plan.name)
            assert plan.required, (relative, plan.name)

    assert graded > 15, f"only {graded} branches planned"


def test_a_module_without_the_flat_class_is_reported_not_guessed() -> None:
    assert (
        split_into_branch_classes(
            _FLAT_MODULE,
            "Missing",
            _constraints(_TWO_ARMS, {"a": "a", "b": "b", "c": "c"}),
            "core/thing.json",
            frozenset({"a"}),
        )
        is None
    )


def test_unused_import_guard() -> None:
    """Keeps ``pytest`` imported for the collection-time fixtures above."""
    assert pytest is not None


def test_every_branch_of_every_split_root_names_its_root() -> None:
    """The subclass relation, read off the SHIPPED tree rather than a re-render.

    ``isinstance(branch, shape)`` is the property the public base exists to give a
    consumer, and the population it has to hold over is whichever roots the
    partition prover finds -- not a list written here, which would go stale the
    moment a schema refresh adds or drops a partitioning root.

    This reads the generated modules as they ship. The earlier version rendered
    each root again and graded the render, which it had to while the emitter was
    unwired: the committed tree still carried the flat class, and the render was
    the only place the branches existed. Now the branches are in the tree, so grading a
    second render would grade the renderer twice and the artifact never -- and
    would not notice a later pass that edits what the emitter wrote.
    """
    import ast as _ast

    from scripts.post_generate_fixes import (
        SCHEMA_DIR,
        flat_root_shape,
        generated_module_for,
    )
    from scripts.schema_constraints import (
        bundle_document_resolver,
        partitions_by_requiredness,
        root_carries_constraints,
    )
    from scripts.union_branch_names import branch_class_names

    resolve = bundle_document_resolver(SCHEMA_DIR)
    roots = 0
    branches = 0
    for schema_file in sorted(SCHEMA_DIR.rglob("*.json")):
        relative = schema_file.relative_to(SCHEMA_DIR)
        if relative.parts[0] in ("mcp", "bundled"):
            continue
        try:
            schema = json.loads(schema_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(schema, dict) or not root_carries_constraints(schema):
            continue
        module = generated_module_for(relative)
        if not module.exists():
            continue
        content = module.read_text()
        tree = _ast.parse(content)
        bases = {
            node.name: [_ast.unparse(base) for base in node.bases]
            for node in tree.body
            if isinstance(node, _ast.ClassDef)
        }
        # The root's own name is bound to a union here, so the shape is what the
        # module declares for it. A root the emitter did not split has no shape.
        root = _schema_root_name(schema)
        # The base takes the root's own name, so a split module declares the
        # root as a class and binds the union to <Root>Branches.
        shape = root
        if f"{root}Branches: TypeAlias" not in content:
            continue
        # The prover has to agree the root partitions: a shape with no proof
        # behind it would mean the emitter split something it should not have.
        parsed = flat_root_shape(schema, relative)
        assert parsed is not None, relative
        constraints = read_root_constraints(
            schema,
            parsed,
            branches_render_as_classes=False,
            schema_file=str(relative),
            resolve_document=resolve,
        )
        assert partitions_by_requiredness(constraints), relative
        for name in branch_class_names(root, constraints.branches, str(relative)):
            assert bases.get(name) == [shape], (relative, name, bases.get(name))
            branches += 1
        assert f"{root}Branches: TypeAlias" in content, relative
        # And NOT a module-level adapter beside it -- asserted in the negative,
        # so the emission this removed cannot come back unnoticed.
        assert f"{root}BranchesAdapter" not in content, relative
        roots += 1

    assert roots >= 11, f"the prover found only {roots} split roots in the shipped tree"
    assert branches >= 24, f"only {branches} branches graded"


def _schema_root_name(schema: dict[str, Any]) -> str:
    """The class name the generator derives from a schema title."""
    from scripts.post_generate_fixes import _schema_root_class_name

    root = _schema_root_class_name(schema)
    assert root is not None, f"schema carries no usable title: {schema.get('title')!r}"
    return root


def _split_roots_from_the_prover() -> dict[str, str]:
    """Every root the partition prover splits, as ``{root class: combinator}``.

    The authoritative population comes from the schemas, not from matching
    shapes at runtime: a derivation over runtime shapes cannot tell a split
    root's union from any other union whose members happen to share a base,
    and this repo exports several of those.
    """
    from scripts.post_generate_fixes import (
        SCHEMA_DIR,
        _schema_root_class_name,
        flat_root_shape,
    )
    from scripts.schema_constraints import (
        bundle_document_resolver,
        partitions_by_requiredness,
        root_carries_constraints,
    )

    resolve = bundle_document_resolver(SCHEMA_DIR)
    roots: dict[str, str] = {}
    for schema_file in sorted(SCHEMA_DIR.rglob("*.json")):
        relative = schema_file.relative_to(SCHEMA_DIR)
        if relative.parts[0] in ("mcp", "bundled"):
            continue
        try:
            schema = json.loads(schema_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(schema, dict) or not root_carries_constraints(schema):
            continue
        shape = flat_root_shape(schema, relative)
        if shape is None:
            continue
        constraints = read_root_constraints(
            schema,
            shape,
            branches_render_as_classes=False,
            schema_file=str(relative),
            resolve_document=resolve,
        )
        if not partitions_by_requiredness(constraints):
            continue
        root = _schema_root_class_name(schema)
        assert root is not None
        roots[root] = constraints.combinator or "anyOf"
    return roots


def test_isinstance_against_the_public_base_holds_for_every_branch() -> None:
    """The property the public base exists for, asserted at RUNTIME.

    Not by parsing: this imports the shipped package, walks every root the
    prover splits, and checks ``issubclass`` for each branch. A parse can say
    the base list names the base; only an import can say the MRO holds and that
    the name resolves off ``adcp.types``.
    """
    import adcp.types._generated as generated

    roots = _split_roots_from_the_prover()
    assert len(roots) >= 11, f"the prover found only {len(roots)} split roots"

    branches = 0
    for root in sorted(roots):
        base = getattr(generated, root, None)
        assert isinstance(base, type), f"{root} does not resolve to a class"
        members = branch_members(getattr(generated, f"{root}Branches", None))
        assert members, f"{root}Branches is not a union over branches"
        for member in members:
            assert issubclass(member, base), f"{member.__name__} is not a {root}"
            # Every field the base declares is reachable on the branch, which is
            # what makes a method written against the base work per branch.
            assert set(base.model_fields) <= set(member.model_fields), (root, member.__name__)
            branches += 1

    assert branches >= 24, f"only {branches} branches graded"


def test_a_unions_visibility_follows_its_bases() -> None:
    """``<Root>Branches`` is exported exactly where ``<Root>`` is.

    The base takes the root's own name, so it is exported wherever the root
    was. The union is the extra name, and it earns its place only beside a
    public base -- a union nobody can pair with a nameable base is a name with
    nothing to annotate against.

    Both directions, because each is silent on its own.
    """
    import adcp.types as public

    exported = set(public.__all__)
    for root in sorted(_split_roots_from_the_prover()):
        union = f"{root}Branches"
        if union not in exported:
            continue
        # The invariant, not the provenance: whatever the public name denotes,
        # the exported union's members must subclass it. That holds for the
        # generated base and for a canonical base with its own branches, and it
        # fails for a name bound to something the union does not cover.
        base = getattr(public, root, None)
        assert isinstance(base, type), f"{union} is public but {root} is not a class"
        members = branch_members(getattr(public, union))
        assert members, f"{union} is not a union over branches"
        for member in members:
            assert issubclass(
                member, base
            ), f"{union} has a member {member.__name__} that is not a {root}"

    for name in sorted(n for n in exported if n.endswith("Branches")):
        root = name[: -len("Branches")]
        assert root in exported, f"{name} is public but {root} is not"
        assert branch_members(getattr(public, name)), f"{name} is not a union over branches"

    assert any(n.endswith("Branches") for n in exported), "the derivation selected nothing"
