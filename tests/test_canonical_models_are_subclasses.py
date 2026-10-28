"""A canonical public model is a SUBCLASS of the generated wire model (#1368).

The canonical layer used to build its 32 public classes with ``create_model``,
copying ``model_fields`` off the generated model and inheriting nothing from it.
That is why PR #1368's injected ``enforce_root_required_groups`` validator
reached the generated class and never the public one — the blocking review
finding on that PR. ARCH-ONE-SURFACE §6 refuses a second injection mechanism
for the copy layer ("it fixes the finding by doubling the thing that caused
it"), so the copy is gone instead.

These tests grade the replacement as a property rather than as a diff:

* every public canonical model descends from a generated wire model;
* every ``model_validator`` the generated model declares is present on the
  public model — which is the #1368 contract, stated for ALL validators rather
  than for one;
* the two concerns the copy had to re-attach per class (the boundary's
  ``extra="allow"`` and the legacy-identity strip) still hold.

Reverting any canonical model to a copy fails the first test, because
``create_model`` stamps the calling module onto what it builds and the copy has
no generated ancestor at all.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from adcp.types import canonical_creative
from adcp.types.canonical_creative import (
    PRIMARY_CANONICAL_MODELS,
    CanonicalBoundaryModel,
    is_legacy_creative_identity_key,
)

_GENERATED_PREFIX = "adcp.types.domains."

#: ``Format`` is the one canonical model with no generated counterpart: it is
#: hand-written from the canonical format declaration and declares its own
#: fields. Everything else refines a generated wire model.
_HAND_WRITTEN = {"Format"}


def _generated_ancestors(model: type[BaseModel]) -> list[type[BaseModel]]:
    return [
        base
        for base in model.__mro__
        if base.__module__.startswith(_GENERATED_PREFIX) and issubclass(base, BaseModel)
    ]


def _ids(models: tuple[type[BaseModel], ...]) -> list[str]:
    return [model.__name__ for model in models]


def test_the_canonical_model_list_is_not_empty() -> None:
    """A later refactor must not satisfy these tests by emptying the list."""
    assert len(PRIMARY_CANONICAL_MODELS) == 33


@pytest.mark.parametrize("model", PRIMARY_CANONICAL_MODELS, ids=_ids(PRIMARY_CANONICAL_MODELS))
def test_every_canonical_model_descends_from_a_generated_model(model: type[BaseModel]) -> None:
    """The anti-copy property, stated structurally."""
    if model.__name__ in _HAND_WRITTEN:
        pytest.skip(f"{model.__name__} has no generated counterpart")
    ancestors = _generated_ancestors(model)
    assert ancestors, (
        f"{model.__name__} has no generated ancestor — it is a copy, not a "
        f"refinement. MRO: {[base.__name__ for base in model.__mro__]}"
    )


@pytest.mark.parametrize("model", PRIMARY_CANONICAL_MODELS, ids=_ids(PRIMARY_CANONICAL_MODELS))
def test_every_generated_validator_reaches_the_public_model(model: type[BaseModel]) -> None:
    """#1368's contract, for every validator rather than for one.

    A validator injected into a generated class by a generation pass — which is
    what ``enforce_root_required_groups`` is — must be live on the public model
    an adopter imports. Pydantic carries inherited decorators on the subclass's
    ``__pydantic_decorators__``, so the subclass's validator set is a superset
    of each generated ancestor's. A copy's is disjoint.
    """
    if model.__name__ in _HAND_WRITTEN:
        pytest.skip(f"{model.__name__} has no generated counterpart")
    public = set(model.__pydantic_decorators__.model_validators)
    for ancestor in _generated_ancestors(model):
        inherited = set(ancestor.__pydantic_decorators__.model_validators)
        assert inherited <= public, (
            f"{model.__name__} lost {sorted(inherited - public)} declared on its "
            f"generated ancestor {ancestor.__name__}"
        )


@pytest.mark.parametrize("model", PRIMARY_CANONICAL_MODELS, ids=_ids(PRIMARY_CANONICAL_MODELS))
def test_every_canonical_model_declares_no_legacy_identity(model: type[BaseModel]) -> None:
    """Inheritance brings the legacy fields along; the one declared rule removes them."""
    leaked = [name for name in model.model_fields if is_legacy_creative_identity_key(name)]
    assert leaked == [], f"{model.__name__} declares legacy creative identity: {leaked}"


@pytest.mark.parametrize("model", PRIMARY_CANONICAL_MODELS, ids=_ids(PRIMARY_CANONICAL_MODELS))
def test_every_canonical_model_keeps_the_boundary_policy(model: type[BaseModel]) -> None:
    """``CanonicalBoundaryModel`` is the LAST base, so its config wins the merge."""
    assert issubclass(model, CanonicalBoundaryModel), model.__name__
    assert model.model_config["extra"] == "allow", model.__name__


@pytest.mark.parametrize("model", PRIMARY_CANONICAL_MODELS, ids=_ids(PRIMARY_CANONICAL_MODELS))
def test_every_canonical_model_runs_the_wrap_serializer(model: type[BaseModel]) -> None:
    """The strip serializer is declared once on the boundary and inherited.

    It used to be re-attached per class through ``create_model``'s
    ``__validators__``; a class the copy helper missed would serialize legacy
    identity from a nested ``TypeAdapter`` path.
    """
    serializers = model.__pydantic_decorators__.model_serializers
    assert "_serialize_canonical" in serializers, model.__name__


def test_the_removal_rule_is_declared_once_and_derived() -> None:
    """The strip predicate has one owner, and the rule reads it.

    Four behaviors strip legacy identity — input rejection, JSON-schema
    sanitation, serialization, and now the declared-field removal — and all
    four call ``is_legacy_creative_identity_key``. A second spelling of the
    key list is the drift channel this asserts shut.
    """

    class Probe(canonical_creative.Package):
        pass

    assert "format_ids" not in Probe.model_fields
    assert "format_ids" in _generated_ancestors(Probe)[0].model_fields


def test_an_inherited_validator_that_reads_a_removed_field_still_works() -> None:
    """The one interaction between inheritance and the removal rule.

    ``list-creatives-response.json`` carries an injected
    ``_validate_format_reference_xor`` that reads ``self.format_id``. Inheritance
    is the point of this layer, so that validator now runs on the canonical
    model — and with the field merely deleted it raised ``AttributeError``
    instead of validating. The removal binds the name to ``None``, which is the
    removal's own truth, and the XOR then reduces to the invariant the canonical
    model should hold: ``format_kind`` must be set.

    Both directions are graded, because a reduced invariant that accepts
    everything is not an invariant.
    """
    import pytest as _pytest
    from pydantic import ValidationError

    from adcp.types import CanonicalFormatKind

    row = {
        "creative_id": "creative-1",
        "name": "Image",
        "format_kind": "image",
        "status": "approved",
        "created_date": "2026-09-01T00:00:00Z",
        "updated_date": "2026-09-01T00:00:00Z",
    }
    accepted = canonical_creative.Creative.model_validate(row)
    assert accepted.format_kind == CanonicalFormatKind.image
    assert "format_id" not in canonical_creative.Creative.model_fields
    assert accepted.format_id is None

    with _pytest.raises(ValidationError):
        canonical_creative.Creative.model_validate(
            {k: v for k, v in row.items() if k != "format_kind"}
        )


def _canonical_branch_pairs() -> list[tuple[type[BaseModel], type[BaseModel]]]:
    """Every canonical class paired with the generated BRANCH it refines.

    Derived from the MRO rather than listed: a canonical class refines a
    generated branch when a generated class sits in its bases and that class is
    itself a subclass of a generated split root's base. A schema refresh that
    splits a fourteenth root is covered without editing this.

    The branch set comes from ``_branch_models`` -- the method the dispatch hook
    itself consults -- not from the ``<Root>Branches`` alias. Reading the alias
    covered only roots that export one, which excluded the five public response
    bases: their branches are the numbered classes the generator writes, and
    those are exported individually rather than behind a union alias. Nothing
    canonical refines one today, so the gap was in the derivation rather than in
    the coverage, and a derivation with a hole in it is the kind that goes stale
    without a failure.
    """
    import adcp.types._generated as generated

    branches: set[type[BaseModel]] = set()
    for name in dir(generated):
        candidate = getattr(generated, name)
        if (
            isinstance(candidate, type)
            and issubclass(candidate, BaseModel)
            and "_branch_models" in candidate.__dict__
        ):
            branches.update(candidate._branch_models())

    pairs: list[tuple[type[BaseModel], type[BaseModel]]] = []
    for name in dir(canonical_creative):
        candidate = getattr(canonical_creative, name)
        if not isinstance(candidate, type) or not issubclass(candidate, BaseModel):
            continue
        refined = [base for base in candidate.__mro__[1:] if base in branches]
        if refined:
            pairs.append((candidate, refined[0]))
    return pairs


def test_the_canonical_branch_population_is_not_empty() -> None:
    """A derivation that selects nothing passes for free."""
    pairs = _canonical_branch_pairs()
    assert len(pairs) >= 3, f"only {len(pairs)} canonical classes refine a generated branch"


@pytest.mark.parametrize(
    "canonical,generated",
    _canonical_branch_pairs(),
    ids=lambda model: getattr(model, "__name__", str(model)),
)
def test_a_canonical_branch_keeps_its_generated_branchs_requiredness(
    canonical: type[BaseModel], generated: type[BaseModel]
) -> None:
    """Requiredness is the branch's own, field by field.

    A canonical class refines the TYPE of a field; it must not change whether
    the field is required. Requiredness is what distinguishes one branch from
    another, so relaxing it here reinstates the flattening that branch classes
    exist to remove -- a document satisfying no branch would be accepted at the
    canonical surface, which is the one a consumer touches.

    The mechanism that keeps this true is base ORDER, and it is a two-token
    edit away from being wrong, which is why this is asserted rather than
    documented.
    """
    divergent = [
        name
        for name, field in generated.model_fields.items()
        if name in canonical.model_fields
        and canonical.model_fields[name].is_required() != field.is_required()
    ]
    assert not divergent, (
        f"{canonical.__name__} diverges from {generated.__name__} on the requiredness of "
        f"{divergent}. The usual cause is base ORDER: the generated branch must be listed "
        f"FIRST and CanonicalBoundaryModel LAST, so the branch's requiredness wins by MRO "
        f"position and model_config still resolves from the canonical side. With the "
        f"boundary first, every field the branch does not re-declare reverts to optional."
    )


@pytest.mark.parametrize(
    "canonical,generated",
    _canonical_branch_pairs(),
    ids=lambda model: getattr(model, "__name__", str(model)),
)
def test_a_canonical_branch_declares_every_field_its_generated_branch_declares(
    canonical: type[BaseModel], generated: type[BaseModel]
) -> None:
    """No field is lost on the way to the canonical surface, except by the rule.

    ``CanonicalBoundaryModel`` removes legacy creative identity from a canonical
    model's DECLARED fields, which is the layer's purpose. That exception is
    read from the same predicate the production strip uses, so the test cannot
    drift from the rule it is excepting -- a hand-listed field name would admit
    a second removal nobody decided on.
    """
    from adcp.types.canonical_creative import is_legacy_creative_identity_key

    missing = sorted(
        name
        for name in set(generated.model_fields) - set(canonical.model_fields)
        if not is_legacy_creative_identity_key(name)
    )
    assert not missing, f"{canonical.__name__} drops {missing} declared by {generated.__name__}"


def _where_base_order_is_expressible(
    canonical: type[BaseModel], generated: type[BaseModel], boundary: type[BaseModel]
) -> set[str]:
    """Fields on which reordering the bases would change requiredness.

    Three conditions, and the third took two corrections to state: the generated
    branch REQUIRES the field, the canonical class does not re-declare it in its
    own body, and the boundary-side base CARRIES that field with a different
    requiredness.

    Both looser versions demanded failures that cannot happen. Dropping the
    third condition entirely named fields required on both sides, which order
    cannot change. Phrasing it as "the boundary does not also require it" named
    fields the boundary does not carry at all -- pydantic takes a field only
    from a class that declares it, so a base contributing nothing for a name
    cannot affect it whatever the order.

    Where this set is empty the defect cannot appear, and a probe aimed there
    passes while proving nothing -- which is what happened the first time this
    was probed by hand, on the one branch that re-declares every field it
    requires.
    """
    own = set(getattr(canonical, "__annotations__", {}))
    return {
        name
        for name, field in generated.model_fields.items()
        if field.is_required()
        and name not in own
        and name in boundary.model_fields
        and not boundary.model_fields[name].is_required()
    }


def test_the_requiredness_test_fails_wherever_the_defect_can_appear() -> None:
    """A probe over the POPULATION, so it cannot be aimed at a blind spot.

    For each canonical branch where base order is expressible, rebuild the class
    with the boundary base FIRST -- carrying its own body across, so order is the
    only difference -- and require that the requiredness comparison fails. A
    probe that changed the body as well would report flattening from the wrong
    cause.
    """
    pairs = _canonical_branch_pairs()
    assert pairs, "the derivation selected nothing"

    probed = 0
    for canonical, generated in pairs:
        # The DECLARED bases, swapped -- not "the first non-generated class in the
        # MRO", which picked a sibling canonical model and produced an
        # inconsistent MRO rather than a measurement.
        others = [base for base in canonical.__bases__ if base is not generated]
        if len(others) != 1:
            continue
        boundary = others[0]
        expressible = _where_base_order_is_expressible(canonical, generated, boundary)
        if not expressible:
            continue
        namespace: dict[str, object] = {
            "__annotations__": dict(getattr(canonical, "__annotations__", {}))
        }
        for name in namespace["__annotations__"]:  # type: ignore[attr-defined]
            field = canonical.model_fields.get(name)
            if field is not None and not field.is_required():
                namespace[name] = field.default
        reordered = type(f"Reordered{canonical.__name__}", (boundary, generated), namespace)
        diverged = [
            name
            for name, field in generated.model_fields.items()
            if name in reordered.model_fields
            and reordered.model_fields[name].is_required() != field.is_required()
        ]
        assert diverged, (
            f"reordering {canonical.__name__}'s bases changed nothing, yet the defect is "
            f"expressible on {sorted(expressible)}. Either the probe no longer changes "
            f"what it thinks it changes, or requiredness no longer comes from MRO position."
        )
        probed += 1

    assert probed, (
        "no canonical branch can express the base-order defect, so "
        "test_a_canonical_branch_keeps_its_generated_branchs_requiredness grades nothing"
    )
