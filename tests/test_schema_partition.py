"""The emit rule: where a root's branches partition, and where they only overlap.

One prover answers two questions, and both are decided from ``Branch`` records
rather than from a list kept here. ``provably_disjoint`` asks whether any
document can satisfy two conditions at once; ``partitions_by_requiredness``
asks whether a whole root's branches are therefore distinct types. A root that
partitions is emitted as one class per branch plus a union, because the class a
parsed document comes back as is then schema information. A root that does not
is emitted as one class and a compiled check, because minting a class there
would make branch ORDER an observable class identity -- a channel the flat
class did not have.

The prover is sound and deliberately incomplete: every node kind it has no
literal for weakens the condition, and a refutation of a weaker conjunction
refutes the stronger one, so an unmodelled kind can only cost a proof. The
measured consequence over the pinned bundle is graded by
:func:`test_the_pinned_bundle_splits_where_the_measurement_says`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts.schema_constraints import (
    AnyOf,
    Branch,
    DeclaredShape,
    Not,
    PropSatisfies,
    RootConstraints,
    Supplied,
    ValueConst,
    branch_conditions,
    partitions_by_requiredness,
    provably_disjoint,
    read_root_constraints,
    root_carries_constraints,
)


def _read(root: dict[str, Any], shape: DeclaredShape | None = None) -> RootConstraints:
    return read_root_constraints(
        root,
        shape if shape is not None else DeclaredShape.from_schema(root),
        branches_render_as_classes=False,
    )


# --------------------------------------------------------------------------- #
# What the prover decides
# --------------------------------------------------------------------------- #


def test_an_branch_requiring_what_another_bans_is_disjoint() -> None:
    """``create-media-buy``'s proposal branch bans ``packages``; two branches require it."""
    assert provably_disjoint(
        Supplied("packages"),
        Not(AnyOf((Supplied("packages"), Supplied("budget_allocation")))),
    )


def test_two_branches_pinning_one_property_to_different_values_are_disjoint() -> None:
    """The ``status`` discriminator, and ``budget_allocation.mode`` one level down.

    The value literal only ever reaches a clause alongside the presence literal
    for its own path, which is what makes an empty value intersection a
    refutation rather than a guess about an absent member.
    """
    completed = PropSatisfies("status", ValueConst("completed"))
    failed = PropSatisfies("status", ValueConst("failed"))

    assert not provably_disjoint(
        completed, failed
    ), "neither branch requires status, so a document omitting it satisfies both"
    assert provably_disjoint(_and(Supplied("status"), completed), _and(Supplied("status"), failed))


def test_an_overlapping_cardinality_pair_is_not_disjoint() -> None:
    """``control-media-buy``'s branches: require one field, permit the others."""
    assert not provably_disjoint(Supplied("paused"), Supplied("name"))


def _and(*operands: Any) -> Any:
    from scripts.schema_constraints import AllOf

    return AllOf(operands)


# --------------------------------------------------------------------------- #
# What the emit rule decides
# --------------------------------------------------------------------------- #


def test_a_root_whose_branches_are_mutually_exclusive_partitions() -> None:
    root = {
        "title": "Thing",
        "type": "object",
        "properties": {"a": {}, "b": {}},
        "anyOf": [
            {"required": ["a"], "not": {"required": ["b"]}},
            {"required": ["b"], "not": {"required": ["a"]}},
        ],
    }

    assert partitions_by_requiredness(_read(root))


def test_the_at_least_one_of_idiom_does_not_partition() -> None:
    """Twelve branches requiring one field each and permitting the rest is one shape.

    ``control-media-buy-request`` spells "name at least one field to change"
    this way, because JSON Schema has no ``atLeastOneOf``. A document carrying
    ``paused`` and ``name`` satisfies two branches, so the class a left-to-right
    union returns is decided by the order the branches appear in a JSON file.
    """
    root = {
        "title": "Thing",
        "type": "object",
        "properties": {"a": {}, "b": {}},
        "anyOf": [{"required": ["a"]}, {"required": ["b"]}],
    }

    assert not partitions_by_requiredness(_read(root))


def test_an_branch_with_no_required_does_not_partition() -> None:
    """Requiredness is what the rule partitions BY, and disjointness is not enough.

    Two reasons, and the second is the one that is easy to lose. Without
    ``required`` an branch grades only members that are present, so
    ``postal-country-system``'s ten country branches are all satisfied by a
    document carrying no ``country`` and a left-to-right union would hand back
    the US class for it -- the first root below. And even a pair the prover DOES
    separate is refused when neither branch requires anything: the required set is
    the branch's identity, the thing rung 3 of the naming rule spells, and an branch
    with none names no shape.
    """
    vacuous = {
        "title": "Thing",
        "type": "object",
        "properties": {"country": {}, "system": {}},
        "anyOf": [
            {"properties": {"country": {"const": "US"}}},
            {"properties": {"country": {"const": "GB"}}},
        ],
    }

    assert not partitions_by_requiredness(_read(vacuous))

    disjoint_but_unnamed = RootConstraints(
        combinator="anyOf",
        branches=(
            Branch(
                pointer="anyOf/0",
                title=None,
                discriminator=None,
                required=("a",),
                declares_fields=False,
                condition=Supplied("a"),
            ),
            Branch(
                pointer="anyOf/1",
                title=None,
                discriminator=None,
                required=(),
                declares_fields=False,
                condition=Not(Supplied("a")),
            ),
        ),
        conjuncts=(),
    )

    assert provably_disjoint(
        disjoint_but_unnamed.branches[0].condition, disjoint_but_unnamed.branches[1].condition
    )
    assert not partitions_by_requiredness(disjoint_but_unnamed)


def test_a_root_conjunct_is_what_separates_two_otherwise_overlapping_branches() -> None:
    """``webhook-challenge-response`` bans ``challenge`` and ``token`` together.

    The ban sits beside the combinator, not inside either branch, and each branch
    requires one of the two names. Proving the branches disjoint without it would
    be wrong, and emitting classes without distributing it would admit a
    document carrying both.
    """
    branches = [{"required": ["challenge"]}, {"required": ["token"]}]
    overlapping = {
        "title": "Thing",
        "type": "object",
        "properties": {"challenge": {}, "token": {}},
        "anyOf": branches,
    }
    separated = dict(overlapping, **{"not": {"required": ["challenge", "token"]}})

    assert not partitions_by_requiredness(_read(overlapping))
    assert partitions_by_requiredness(_read(separated))
    assert len(branch_conditions(_read(separated))[0].operands) == 2


# --------------------------------------------------------------------------- #
# The measured split over the pinned bundle
# --------------------------------------------------------------------------- #


def _flat_combinator_roots() -> list[tuple[str, RootConstraints]]:
    """Every flat-root combinator schema in the pinned bundle, read once.

    Membership is derived: a schema whose root carries a combinator and whose
    generated module holds ONE class over the root's declared properties, which
    is what ``flat_root_shape`` answers. The alternative -- a list of file names
    kept here -- would stop grading a schema the next bundle adds.
    """
    from scripts.post_generate_fixes import SCHEMA_DIR, flat_root_shape
    from scripts.schema_constraints import bundle_document_resolver

    resolve = bundle_document_resolver(SCHEMA_DIR)
    roots: list[tuple[str, RootConstraints]] = []
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
        if constraints.combinator:
            roots.append((str(relative), constraints))
    return roots


@pytest.fixture(scope="module")
def flat_combinator_roots() -> list[tuple[str, RootConstraints]]:
    return _flat_combinator_roots()


def test_the_population_is_not_empty(
    flat_combinator_roots: list[tuple[str, RootConstraints]],
) -> None:
    """A derivation that selects nothing passes for free."""
    assert len(flat_combinator_roots) > 30, (
        "the walk found almost no flat-root combinator schema; the split below "
        "would then grade nothing"
    )


#: The flat-root combinator schemas of the pinned bundle whose branches partition,
#: so each branch is emitted as its own class behind a union alias. Written down
#: because this set decides which public names the SDK has -- and because it
#: is how the two roots that declare their properties ONLY inside their
#: branches were found. ``search-brands-response`` and
#: ``verify-brand-claim-request`` carry no root ``properties`` of their own;
#: the generator merges the branches' properties onto one class, and the
#: reader did not look there, so both went unsplit while this set said
#: eleven: a silent change to
#: it is a silent change to the surface. The counts move with a bundle sync and
#: are therefore re-derived rather than asserted.
PARTITIONING_ROOTS = frozenset(
    {
        "account/list-account-changes-response.json",
        "brand/search-brands-response.json",
        "brand/verify-brand-claim-request.json",
        "core/audience-characteristic.json",
        "core/catchment.json",
        "core/creative-asset.json",
        "core/creative-manifest.json",
        "core/frequency-cap-impression-constraints.json",
        "core/frequency-cap-interval-constraints.json",
        "core/get-geo-place-resolution-request.json",
        "core/webhook-challenge-response.json",
        "media-buy/create-media-buy-request.json",
        "media-buy/request-proposals-request.json",
    }
)


def test_the_pinned_bundle_splits_where_the_measurement_says(
    flat_combinator_roots: list[tuple[str, RootConstraints]],
) -> None:
    """Both sides of the split are populated, and the partitioning set is named.

    A prover that proved everything, or nothing, would pass a rule that only
    counted. So this asserts which schemas partition, and that the other side is
    the larger one -- which is what the cardinality idiom predicts.
    """
    partitioning = {
        name
        for name, constraints in flat_combinator_roots
        if partitions_by_requiredness(constraints)
    }

    assert partitioning == set(PARTITIONING_ROOTS)
    assert (
        len(flat_combinator_roots) - len(partitioning) > 20
    ), "almost every root partitioned, which the cardinality idiom says is wrong"


def test_every_partitioning_one_of_is_pairwise_disjoint(
    flat_combinator_roots: list[tuple[str, RootConstraints]],
) -> None:
    """``oneOf`` is exactly-one and a union is at-least-one.

    Emitting a ``oneOf`` whose branches are not provably disjoint as a union would
    ship anyOf-weaker semantics, so the emitter refuses with
    ``UndisjointOneOfError``. This is the bundle's standing evidence that every
    ``oneOf`` reaching the union path clears the stronger bar, independently of
    the emit rule that selected it.
    """
    graded = 0
    for name, constraints in flat_combinator_roots:
        if constraints.combinator != "oneOf" or name not in PARTITIONING_ROOTS:
            continue
        conditions = branch_conditions(constraints)
        graded += 1
        for index, left in enumerate(conditions):
            for right in conditions[index + 1 :]:
                assert provably_disjoint(left, right), name

    assert graded >= 5, f"only {graded} oneOf roots reached the union path"
