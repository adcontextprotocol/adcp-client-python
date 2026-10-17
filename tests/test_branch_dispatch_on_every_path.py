"""A root with branches resolves to a branch on all three validation paths.

A ``model_validate`` override reaches ONE of them. Pydantic validates a nested
field and a ``TypeAdapter`` through the core schema, so an override is invisible
on both -- and both are production paths here: most roots with branches are
nested object schemas, and ``adcp.types.validation.validate_union`` (which the
server framework calls on a handler's return value) is a ``TypeAdapter``.

The response side shipped an override whose docstring said "runtime validators
continue to enforce the cross-field rules". It did not. Measured on the base it
guarded, with the override in place:

    validate_union(CreateContentStandardsResponse, {"totally": "wrong"})
    -> CreateContentStandardsResponse(model_extra={'totally': 'wrong'})

The base is ``extra="allow"``, so every field of an unrecognised document was
filed as an untyped extra and the document was accepted. This grades the
property that makes that docstring true, over every root with branches, on each
path -- so a mechanism that covers one path and not the others fails here rather
than passing three times on the one path it covers.

It also grades the ACCEPTED document, which it did not for a long time, and that
omission is why salesagent-setka went unnoticed for 23 commits. The constructed
path WAS in the population below -- but only under ``pytest.raises``, so a
successful construction could not fail anything. ``Base(**data)`` returned a
loose base whose serializer is the union of its branches, which matches no branch
and recurses: ``model_dump(mode="json")`` raised ``Circular reference detected``.
An instrument that can only express one outcome reports clean on the other.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

# Importing the consolidated module is what loads every generated domain module,
# and the canonical layer is a separate import. The derivation below walks what
# is loaded, so a population derived before these imports is empty -- which is
# how the first version of this file parametrised over nothing and passed.
import adcp.types._generated  # noqa: F401
import adcp.types.canonical_creative  # noqa: F401
from adcp.types._branch_dispatch import BranchDispatchMixin
from tests.conformance import _schema_parity as parity


def _roots_with_branches() -> dict[str, type[BaseModel]]:
    """Every shipped class that DECLARES its branches.

    Declaring ``_branch_models`` is what makes a class the base -- the hook asks
    ``"_branch_models" in cls.__dict__``, not a registry -- so the same question
    the hook asks is the question that selects the population. All three sources
    are in it: a split root the branch emitter compiled, a response base whose
    numbered branches the generator wrote, and the canonical create request,
    which re-declares the method to dispatch to canonical branches.

    Keyed by module and class, not by exported name. Deriving over
    ``adcp.types.__all__`` found 11 of these, because ``CreativeAsset`` and
    ``CreativeManifest`` resolve to canonical REFINEMENTS of a single branch --
    which inherit the method, correctly keep their own shape, and are not bases
    over branches at all.
    """
    walked: dict[str, type[BaseModel]] = {}
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("adcp.types"):
            continue
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and "_branch_models" in value.__dict__
            ):
                walked[f"{value.__module__}.{value.__name__}"] = value
    return walked


def _no_branch_accepts_the_empty_document(root: type[BaseModel]) -> bool:
    """Whether ``{}`` satisfies no branch of this root.

    The precondition of the probes below, asserted rather than assumed: a root
    with a branch that requires nothing accepts ``{}``, and expecting a refusal
    of it would be expecting the wrong thing. Three derived probes in this
    series passed vacuously because a precondition like this one went unchecked.
    """
    branches = root._branch_models()  # type: ignore[attr-defined]
    return bool(branches) and all(
        any(field.is_required() for field in branch.model_fields.values()) for branch in branches
    )


def _graded_roots() -> dict[str, type[BaseModel]]:
    return {
        name: root
        for name, root in _roots_with_branches().items()
        if _no_branch_accepts_the_empty_document(root)
    }


def _keywords(error: ValidationError) -> set[str]:
    return {entry["type"] for entry in error.errors()}


def test_a_document_satisfying_no_branch_is_refused_on_all_three_paths() -> None:
    """By name, nested in another model, and through a ``TypeAdapter``.

    Direct construction is graded alongside them, because it is the same core
    schema and because the loose base it used to produce is what let a root
    accept by construction the same empty document it refused by validation.

    The refusal names the root's own combinator on every path: ``core/error.json``
    requires each ``issues[]`` entry to carry a keyword from the JSON Schema
    vocabulary, and the ``missing`` pydantic reports is not in it.
    """
    graded = _graded_roots()
    assert graded, "the derivation selected no root to grade"

    for name, root in sorted(graded.items()):
        combinator = root._branch_combinator()  # type: ignore[attr-defined]
        assert combinator in {"anyOf", "oneOf"}, (name, combinator)

        class Holder(BaseModel):
            value: root  # type: ignore[valid-type]

        paths = {
            "by name": lambda root=root: root.model_validate({}),
            "nested": lambda holder=Holder: holder.model_validate({"value": {}}),
            "TypeAdapter": lambda root=root: TypeAdapter(root).validate_python({}),
            "constructed": root,
        }
        for path, call in paths.items():
            with pytest.raises(ValidationError) as caught:
                call()
            assert combinator in _keywords(caught.value), (
                f"{name} on the {path} path refused with "
                f"{sorted(_keywords(caught.value))}, not {combinator!r}"
            )


def test_the_population_covers_every_source_of_branches() -> None:
    """A derivation that matches nothing is green, so the floor is asserted.

    Thirteen split roots the branch emitter compiles, the five public response
    names whose numbered branches the generator writes, and the canonical create
    request. The floor is their sum; it is a floor because a schema refresh can
    only add roots that partition.
    """
    roots = _roots_with_branches()
    assert len(roots) >= 19, f"the derivation found only {len(roots)} roots with branches"

    graded = _graded_roots()
    # Measured: all 19. A root that stopped refusing it would mean some branch
    # requires nothing, which is a branch that accepts any document at all.
    assert len(graded) == len(
        roots
    ), f"{sorted(set(roots) - set(graded))} have a branch that requires nothing"

    # The five public response names are what this change adds to the
    # population. Named, not counted: a count of graded names ending in
    # "Response" is already satisfied by the split roots that happen to be
    # responses, so it would stay green with all five of these missing. The
    # generator's own list of them is hand-written, so this is auditable against
    # it rather than derived from the same place.
    for module, name in (
        ("compliance.comply_test_controller_response", "ComplyTestControllerResponse"),
        ("content_standards.create_content_standards_response", "CreateContentStandardsResponse"),
        ("content_standards.list_content_standards_response", "ListContentStandardsResponse"),
        ("content_standards.update_content_standards_response", "UpdateContentStandardsResponse"),
        ("account.sync_governance_response", "SyncGovernanceResponse"),
    ):
        key = f"adcp.types.domains.{module}.{name}"
        assert key in graded, f"{name} is not dispatching through its branches"

    # The canonical create request runs on the same mechanism. It used to carry
    # a second copy of the hook, keyed on class identity through two module
    # globals, and its own ``_branch_models`` -- inherited, not declared --
    # reported the GENERATED branches while that schema was built from the
    # canonical ones.
    canonical = "adcp.types.canonical_creative.CreateMediaBuyRequest"
    assert canonical in roots
    assert [branch.__module__ for branch in roots[canonical]._branch_models()] == [  # type: ignore[attr-defined]
        "adcp.types.canonical_creative"
    ] * 3


def test_a_branch_keeps_its_own_shape_rather_than_its_siblings() -> None:
    """A branch inherits the hook and must not validate as the union.

    The deleted override tested ``cls in arms`` for this, which is correct for
    an override and silently wrong for a core-schema hook: membership admits
    both "is a branch" and "is an unrelated subclass of the base", and only the
    second matters once the schema itself is being replaced. The hook instead
    asks whether the class DECLARES the method, which separates them.

    Graded by what a refusal SAYS. A branch asked to validate ``{}`` reports the
    fields IT requires; a branch that had picked up the union's schema would
    report the combinator, having tried every sibling.
    """
    graded = _graded_roots()
    assert graded, "the derivation selected no root to grade"

    branches = 0
    for name, root in sorted(graded.items()):
        combinator = root._branch_combinator()  # type: ignore[attr-defined]
        for branch in root._branch_models():  # type: ignore[attr-defined]
            assert "_branch_models" not in branch.__dict__, (name, branch.__name__)
            with pytest.raises(ValidationError) as caught:
                branch.model_validate({})
            assert combinator not in _keywords(caught.value), (
                f"{branch.__name__} refused with {combinator!r}, so it validates as "
                "the union rather than as itself"
            )
            branches += 1

    assert branches >= 49, f"only {branches} branches graded"


def test_ignoring_unknown_fields_cannot_make_a_branch_vacuously_satisfiable() -> None:
    """``extra="ignore"`` must not turn an unrecognised document into a valid one.

    Most of these models ignore extras: ``AdCPBaseModel`` is
    ``extra="ignore"``, and the generated response bases are ``extra="allow"``
    where their schemas say ``additionalProperties: true``. Under either
    setting, a branch that requires NOTHING matches every document -- pydantic
    drops or files the unknown keys and the branch is satisfied -- so one such
    branch would make its root accept anything while still looking dispatched.

    Asserted on the live models rather than reasoned about, because the property
    depends on the requiredness each branch ended up with after every pass that
    edits it, and a redeclaration that re-added a default would take it away.
    """
    roots = _roots_with_branches()
    assert roots, "the derivation selected no root"

    ignoring = {
        name: root for name, root in roots.items() if root.model_config.get("extra") != "forbid"
    }
    assert ignoring, "no root with branches tolerates an unknown field"

    for name, root in sorted(ignoring.items()):
        for branch in root._branch_models():  # type: ignore[attr-defined]
            required = {field for field, info in branch.model_fields.items() if info.is_required()}
            assert required, f"{name}: {branch.__name__} requires nothing, so it matches anything"

        # And the whole root, end to end: a document of nothing but unknown keys
        # is refused rather than emptied into a branch.
        with pytest.raises(ValidationError):
            root.model_validate({"totally": "wrong", "unknown": [1, 2]})


class _LocalDefs:
    """A ``$ref`` resolver over one pydantic schema's own ``$defs``.

    ``parity.instances`` needs a resolver; a model's ``model_json_schema()``
    keeps every nested model under ``$defs`` and refers to it locally, so
    resolving is a dict lookup. Reusing that synthesizer rather than writing a
    second one keeps one owner for "a document this schema accepts".
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self._defs = schema.get("$defs", {})

    @contextmanager
    def resolving(self, ref: str) -> Any:
        yield self._defs[ref.rsplit("/", 1)[-1]]


def _accepted_document(root: type[BaseModel]) -> dict[str, Any] | None:
    """A document ``root`` accepts, synthesized from its schema, or ``None``.

    Candidates come from the root's schema and then from each branch's, and the
    MODEL is the oracle for which candidate is valid -- the same
    synthesize-then-ask shape ``tests/conformance`` uses, rather than a
    hand-written payload per root that would go stale against its schema.
    """
    for source in (root, *root._branch_models()):  # type: ignore[attr-defined]
        schema = source.model_json_schema(mode="validation")
        try:
            candidates = parity.instances(schema, _LocalDefs(schema))
        except Exception:
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                root.model_validate(candidate)
            except ValidationError:
                continue
            return candidate
    return None


#: ``VerifyBrandClaimRequest`` is the one root the synthesizer cannot satisfy: its
#: ``claim`` property is a nested object whose own requirements are stated outside
#: its ``required`` list, so every candidate arrives as ``claim: {}`` and the
#: required-group validator refuses it. Named rather than silently absent, and the
#: floor below is asserted so the set cannot quietly grow.
_WITHOUT_A_SYNTHESIZABLE_DOCUMENT = frozenset(
    {"adcp.types.domains.brand.verify_brand_claim_request.VerifyBrandClaimRequest"}
)


def test_every_root_with_branches_dispatches_its_construction() -> None:
    """The mixin's ``__new__`` is what every root resolves for construction.

    Exhaustive over all 19, and it costs nothing: a root that lost the dispatch
    -- by a subclass defining its own ``__new__``, or by the mixin's being
    deleted -- fails here even where no document can be synthesized for it.
    """
    roots = _roots_with_branches()
    assert roots, "the derivation selected no root"

    for name, root in sorted(roots.items()):
        assert root.__new__ is BranchDispatchMixin.__new__, (
            f"{name} does not construct through the dispatch, so Base(**data) "
            "returns the base rather than the branch"
        )


def test_an_accepted_document_constructs_as_a_branch_and_serializes() -> None:
    """The other half of the constructed path: what a SUCCESS produces.

    Two assertions per root, and the second is the one salesagent-setka needed.
    A base instance is not merely loosely typed -- its serializer is the union of
    its branches, so it cannot be serialized at all on the JSON paths.
    """
    graded = {
        name: root
        for name, root in _roots_with_branches().items()
        if name not in _WITHOUT_A_SYNTHESIZABLE_DOCUMENT
    }
    # A derivation that matches nothing is green, so the floor is asserted, and
    # the root whose crash prompted this is named rather than counted.
    assert len(graded) >= 18, f"only {len(graded)} roots left after the exclusion"
    assert "adcp.types.canonical_creative.CreateMediaBuyRequest" in graded

    for name, root in sorted(graded.items()):
        document = _accepted_document(root)
        assert document is not None, (
            f"{name}: no document could be synthesized, so this root is graded "
            "vacuously -- add it to _WITHOUT_A_SYNTHESIZABLE_DOCUMENT with the reason"
        )
        branches = root._branch_models()  # type: ignore[attr-defined]

        constructed = root(**document)
        assert type(constructed) in branches, (
            f"{name} constructed as {type(constructed).__name__}, which is not one "
            f"of its branches -- Base(**data) is not dispatching"
        )
        # model_validate already resolves to a branch; asserting both agree is
        # what makes "the root's name resolves to the branch" one property rather
        # than two that can drift apart.
        assert type(root.model_validate(document)) is type(constructed)

        for mode in ("python", "json"):
            constructed.model_dump(mode=mode)
        constructed.model_dump_json()


#: An error type that NAMES A KEY THE DOCUMENT DOES NOT CARRY. Its last ``loc``
#: element is the absent key, so only the prefix can be resolved against the
#: submitted document. Every other type points at something that is there.
_NAMES_AN_ABSENT_KEY = frozenset({"missing"})


def _copy_with(document: Any, path: tuple[Any, ...], value: Any) -> Any:
    """``document`` with ``path`` replaced by ``value``, copied along the way."""
    if not path:
        return value
    head, rest = path[0], path[1:]
    if isinstance(document, dict):
        return {**document, head: _copy_with(document[head], rest, value)}
    return [
        _copy_with(item, rest, value) if index == head else item
        for index, item in enumerate(document)
    ]


def _leaf_paths(document: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Every scalar position in ``document``, deepest first.

    Deepest first because depth is where the defect this grades lived: a
    one-element path cannot tell a prepended element from the key itself in
    every spelling, and a path three levels down can.

    An EMPTY container is a leaf: three of these roots accept a document whose
    only value is ``[]`` (``{"accounts": []}``), and descending into it yields
    no position at all -- which left them with nothing to corrupt and dropped
    them out of the grading silently.
    """
    if isinstance(document, (dict, list)) and not document:
        return [prefix]
    if isinstance(document, dict):
        found = [
            path for key, value in document.items() for path in _leaf_paths(value, (*prefix, key))
        ]
    elif isinstance(document, list):
        found = [
            path
            for index, value in enumerate(document)
            for path in _leaf_paths(value, (*prefix, index))
        ]
    else:
        return [prefix]
    return sorted(found, key=len, reverse=True)


def _addresses(document: Any, loc: tuple[Any, ...], error_type: str) -> bool:
    """Whether ``loc`` OPENS with a path INTO ``document``.

    Resolution, not spelling. A synthetic element is a plain string
    indistinguishable from a legitimate key by inspection -- a union member is
    spelled by its class name, and a wrapped one by a core-schema expression --
    so a predicate that recognised them would be a list of spellings that the
    next spelling escapes. Walking the submitted document cannot be escaped: an
    element that is not a key or an index it carries does not resolve.

    THE FIRST ELEMENT, which is where the label of a split root's own union sits
    by construction: the hook installs that union AT the root, so its choice
    label is prepended to every error the root reports and lands at position
    zero. A deeper element can also fail to resolve, from a union that is a
    FIELD's declared type -- measured as ``/account/AccountReference1/account_id``
    on the canonical create request, with this root cause already fixed. That is
    salesagent-8qela.46, and it is a different question: the dotted
    ``error.field`` a pydantic loc reaches a buyer as is graded by
    ``test_genuine_union_keeps_both_variant_errors_and_locations`` to KEEP a
    genuine union's member name, so widening this to the whole path is that
    issue's acceptance test rather than this one's assertion.
    """
    opening = loc[:-1] if error_type in _NAMES_AN_ABSENT_KEY else loc
    if not opening:
        return True
    element = opening[0]
    if isinstance(document, dict):
        return element in document
    return isinstance(element, int) and isinstance(document, list) and element < len(document)


def test_a_refusal_points_at_a_path_in_the_submitted_document() -> None:
    """Every ``loc`` a root with branches reports resolves against the document.

    A pydantic union does not report a document path: it prefixes every error
    with the label of the choice that produced it, which for these roots is a
    class this package generated, and for a branch carrying a compiled validator
    is pydantic's own spelling of the wrapper --
    ``function-after[_require_schema_required_group(), <Branch>]``. That element
    reaches a buyer: the three places that build an INVALID_REQUEST join a loc
    into ``error.field``, and ``core/error.json`` specifies ``issues[].pointer``
    as an RFC 6901 JSON Pointer to the offending field in the request payload.
    A buyer resolving either against what it sent gets nothing.

    The element's source is the UNION, not the validator. Measured: a union of
    two plain models reports ``('PlainA', 'packages', 0, 'geo', 0)`` for a
    document error at ``/packages/0/geo/0``, the same models carrying a
    model-level validator report the ``function-after`` spelling, and a FLAT
    model with that validator adds nothing at all. So this grades the union
    every root with branches installs, over all of them, rather than the one
    root whose pointer was reported.

    The document is the one the sibling tests synthesize, with its DEEPEST leaf
    corrupted, so a resolvable path here is a path through a real request rather
    than through a one-key probe.
    """
    graded = {
        name: root
        for name, root in _roots_with_branches().items()
        if name not in _WITHOUT_A_SYNTHESIZABLE_DOCUMENT
    }
    # A derivation that matches nothing is green, so the floor is asserted and
    # the root whose pointer carried a validator's name is named.
    assert len(graded) >= 18, f"only {len(graded)} roots left after the exclusion"
    assert "adcp.types.canonical_creative.CreateMediaBuyRequest" in graded

    refused = 0
    for name, root in sorted(graded.items()):
        document = _accepted_document(root)
        assert document is not None, (
            f"{name}: no document could be synthesized, so this root is graded "
            "vacuously -- add it to _WITHOUT_A_SYNTHESIZABLE_DOCUMENT with the reason"
        )
        # A value no field accepts, at the deepest position that produces a
        # refusal: a leaf typed ``Any`` accepts anything, so which position
        # refuses is a fact about the schema and is found rather than assumed.
        entries: list[Any] = []
        for path in _leaf_paths(document):
            corrupted = _copy_with(document, path, object())
            try:
                root.model_validate(corrupted)
            except ValidationError as exc:
                entries = exc.errors()
                break
        if not entries:
            continue
        refused += 1
        for entry in entries:
            loc = tuple(entry["loc"])
            assert _addresses(corrupted, loc, entry["type"]), (
                f"{name}: issues[].pointer would be "
                f"/{'/'.join(str(element) for element in loc)}, which does not "
                f"resolve against the submitted document -- its keys are "
                f"{sorted(corrupted)}. Corrupted {path}, error {entry['type']}."
            )
    # The corruption has to actually refuse, or every assertion above is skipped
    # by a loop that never runs.
    assert refused >= 18, f"only {refused} of {len(graded)} roots refused the corrupted document"
