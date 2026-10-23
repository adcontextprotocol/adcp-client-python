"""A split root's branch constraint survives subclassing.

THE DEFECT THIS EXISTS FOR. A root whose schema offers a choice between branches
is rendered as a shared base plus one class per branch, and the refusal for "this
document satisfies no branch" lives in the base's CORE SCHEMA -- the union of the
branches plus the wrapping validator that names the combinator. A subclass is
handed its own flat core schema instead, so it never consults that union and
accepts what the root refuses. Measured before the fix, one bare
``class Sub(Root): pass`` per root:

    roots that refuse by a combinator type   12
    subclasses that still refuse              1   <- and that one for the wrong reason
    LOST                                     11

The single apparent survivor was ``verify-brand-claim-request``, whose four
branches all require the same pair of keys: "at least one branch matches" is then
equivalent to "those keys are present", so ordinary field requiredness refuses and
nothing about the combinator is inherited. That is why this test grades the ERROR
TYPE and not the fact of refusal -- a refusal of type ``missing`` is evidence of
nothing (salesagent-8qela.76).

WHY IT MATTERS. ``CanonicalBoundaryModel``'s own docstring says inheritance is the
point of that layer, and every adopter subclass is in the same position. A lost
constraint produces a MISSING failure -- the model accepts what the pin refuses --
so it is invisible to a regression count built from pass-to-fail transitions, to a
green suite, and to the parity corpus, which passes with a known defect re-armed.
Nothing would have caught this, which is why the guard is part of the fix.

The population is DERIVED from the mixin's own marker, so a root added by a schema
bump is graded without editing this file, and it is asserted non-empty AND to
contain the known case before anything is compared.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import pkgutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import adcp.types.domains as domains
from adcp.validation.version import resolve_bundle_key
from scripts.post_generate_fixes import flat_root_shape
from scripts.schema_constraints import bundle_document_resolver, root_required_names
from tests.conformance import _schema_parity as parity

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLE_KEY = resolve_bundle_key((_REPO_ROOT / "src" / "adcp" / "ADCP_VERSION").read_text().strip())
_SCHEMA_DIR = _REPO_ROOT / "schemas" / "cache" / _BUNDLE_KEY

#: The error types a combinator refusal carries. A refusal of any other type --
#: ``missing`` above all -- says nothing about whether the constraint was
#: inherited, which is the distinction that turned an apparent survivor into an
#: artifact.
_COMBINATOR_TYPES = frozenset({"oneOf", "anyOf", "allOf"})

#: Measured at the 3.2.1 pin: 12 split roots carry a branch combinator and a
#: document satisfying no branch can be derived for each. A floor, because the
#: bundle grows between AdCP releases, and the population assertion this file
#: needs: an empty derivation would compare nothing and pass.
_MIN_SPLIT_ROOTS = 8


def _split_roots() -> list[tuple[str, type[BaseModel]]]:
    """Every generated class that IS a split root, by the mixin's own marker.

    ``BranchDispatchMixin.__get_pydantic_core_schema__`` decides whether a class
    is the root or one of its branches by ``"_branch_models" in cls.__dict__``.
    Reading the same marker means this population cannot drift from the thing it
    grades: a class the mixin treats as a root is a class this test grades.
    """
    found: dict[str, type[BaseModel]] = {}
    for info in pkgutil.walk_packages(domains.__path__, domains.__name__ + "."):
        try:
            module = importlib.import_module(info.name)
        except Exception:  # noqa: BLE001 - an unimportable module is its own test's problem
            continue
        for name in dir(module):
            obj = getattr(module, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and "_branch_models" in obj.__dict__
            ):
                found.setdefault(f"{info.name}.{name}", obj)
    return sorted(found.items())


def _refusal_types(model: type[BaseModel], document: Any) -> frozenset[str] | None:
    """The error types ``model`` refuses ``document`` with, or ``None`` if it accepts."""
    try:
        model.model_validate(document)
    except ValidationError as error:
        return frozenset(issue["type"] for issue in error.errors())
    return None


def _document_satisfying_no_branch(model: type[BaseModel]) -> Any | None:
    """A document the ROOT refuses by a combinator, or ``None`` if none is found.

    The document must ISOLATE the combinator: the root's refusal has to be a
    combinator type and nothing else. ``{}`` is the tempting probe and the wrong
    one -- it also omits the root's genuinely required fields, so a subclass
    refuses it by ``missing`` and the verdict says nothing about the constraint.
    This helper first asked ``{}`` and reported four roots as broken whose fix
    demonstrably works; the document was choosing the answer.

    So: take a schema-valid document from the repo's own synthesizer, strip only
    the keys the branches select by, and accept it only if the root then refuses
    by a combinator ALONE. Finding nothing is a skip, not a pass: a root this
    helper cannot reach is ungraded rather than clean.

    "The keys the branches select by" is the branches' required UNION MINUS WHAT
    THE ROOT REQUIRES. Without that subtraction this helper reproduced the very
    ``{}`` its docstring warns against: ``list-account-changes-response``'s root
    requires ``status`` through its ``allOf`` of ``core/protocol-envelope``, every
    branch requires it too, so stripping the bare union emptied the document and
    the subclass refused by ``missing`` (salesagent-8qela.137).

    A candidate also has to carry values the base's own FIELDS accept, or the
    subclass refuses by ``enum`` and the keyword is again unreachable for an
    unrelated reason. That filter is per field and says nothing about the
    combinator, which is what this file asserts -- but it does mean a base that
    over-narrows a field has the probe route around the narrowing rather than
    report it (salesagent-8qela.138).
    """
    candidates: list[Any] = []
    relative = _schema_for(model)
    if relative is not None:
        schema = json.loads((_SCHEMA_DIR / relative).read_text())
        resolver = parity._make_ref_resolver(
            parity._ensure_state(None), _SCHEMA_DIR / relative, schema
        )
        selectors: set[str] = set()
        for keyword in ("oneOf", "anyOf"):
            for branch in schema.get(keyword) or []:
                if isinstance(branch, dict):
                    selectors.update(branch.get("required") or [])
        selectors -= set(root_required_names(schema, bundle_document_resolver(_SCHEMA_DIR)))
        for instance in parity.instances(schema, resolver, 0, 64):
            if isinstance(instance, dict):
                candidates.append({k: v for k, v in instance.items() if k not in selectors})
    for candidate in candidates:
        if not _fields_accept_every_value(model, candidate):
            continue
        types = _refusal_types(model, candidate)
        if types is not None and types <= _COMBINATOR_TYPES and types:
            return candidate
    return None


def _fields_accept_every_value(model: type[BaseModel], document: dict[str, Any]) -> bool:
    """Whether each of ``document``'s values is accepted by its own field's type.

    Per field and nothing more: the question is whether this document has a fault
    OTHER than satisfying no branch, and the whole document cannot be asked --
    a split root validates against the union of its branches and so answers
    ``oneOf`` for every value, which is the verdict this file is here to grade.
    """
    for name, value in document.items():
        field = model.model_fields.get(name)
        if field is None:
            continue
        try:
            TypeAdapter(field.annotation).validate_python(value)
        except ValidationError:
            return False
    return True


def _schema_for(model: type[BaseModel]) -> Path | None:
    """The bundled schema a generated module came from, by its module path."""
    parts = model.__module__.split(".")
    if "domains" not in parts:
        return None
    relative = Path(*parts[parts.index("domains") + 1 :]).with_suffix(".json")
    candidate = Path(str(relative).replace("_", "-"))
    return candidate if (_SCHEMA_DIR / candidate).is_file() else None


def _generator_can_compile_a_check(model: type[BaseModel]) -> bool:
    """Whether the GENERATOR can compile a field-group check for this root.

    Asks ``flat_root_shape`` -- the generator's own predicate -- rather than
    looking at what was emitted. That distinction is the whole point: an
    exemption keyed on "no validator is present" would skip exactly the roots
    that lack one, so deleting an emitted validator would turn this test green by
    skipping instead of red by failing. Keyed on the SHAPE, a root the generator
    could have served and did not is a FAILURE, and only a root it structurally
    cannot serve is skipped.

    ``None`` means the root has no single flat shape to read fields from -- for
    the five response roots here, the branches genuinely are the types
    (salesagent-8qela.81).
    """
    relative = _schema_for(model)
    if relative is None:
        return False
    schema = json.loads((_SCHEMA_DIR / relative).read_text())
    return flat_root_shape(schema, relative) is not None


def _carries_compiled_check(model: type[BaseModel]) -> bool:
    """Whether the root CLASS itself defines the compiled field-group validator.

    Class-scoped, because the split copies that validator onto every BRANCH: a
    module-wide search answers a different question, and asking the module rather
    than the class is what made the first version of this fix a silent no-op.
    """
    try:
        source = Path(inspect.getsourcefile(model) or "").read_text()
    except (OSError, TypeError):
        return False
    return any(
        isinstance(node, ast.ClassDef)
        and node.name == model.__name__
        and any(
            isinstance(child, ast.FunctionDef) and child.name == "_require_schema_required_group"
            for child in node.body
        )
        for node in ast.parse(source).body
    )


def _branch_keys_the_schema_root_does_not_require(model: type[BaseModel]) -> set[str]:
    """Keys every branch requires and the SCHEMA ROOT does not — the schema side.

    This is a fact about the pinned schema alone, with no reference to what was
    generated, and it is what makes the guard below falsifiable: if no root in
    the bundle has this shape then there is nothing for an over-requirement to be
    measured against, and a green result would mean the derivation matched
    nothing rather than that the generator is right.

    What the root requires comes from ``root_required_names``, the same owner the
    branch emitter relaxes against, so the guard and the generator cannot hold
    two answers. This read ``schema["required"]`` once and that is NOT what a
    root requires: a root ``allOf: [{"$ref": ...}]`` is inheritance, and
    ``account/list-account-changes-response`` declares none of its own while
    ``allOf``-ing ``core/protocol-envelope``, which requires ``status``. Asked
    against jsonschema, a document omitting ``status`` fails with ``required``,
    so the generated class requiring it is correct and this helper reported it as
    a defect (salesagent-8qela.137).
    """
    relative = _schema_for(model)
    if relative is None:
        return set()
    schema = json.loads((_SCHEMA_DIR / relative).read_text())
    root_required = set(root_required_names(schema, bundle_document_resolver(_SCHEMA_DIR)))
    shared_across_branches: set[str] = set()
    for keyword in ("oneOf", "anyOf"):
        branches = [b for b in schema.get(keyword) or [] if isinstance(b, dict)]
        if not branches:
            continue
        shared_across_branches |= set.intersection(
            *(set(b.get("required") or []) for b in branches)
        )
    return shared_across_branches - root_required


def _over_required_keys(model: type[BaseModel]) -> list[str]:
    """Of those keys, the ones the generated CLASS marks required anyway.

    When every branch requires the same key the generator merges it onto the root
    carrying that requiredness, so the root over-requires exactly the keys its
    branches select by -- salesagent-8qela.80. Computed from the pinned schema so
    what it reports is a measurement rather than a list.
    """
    return sorted(
        key
        for key in _branch_keys_the_schema_root_does_not_require(model)
        if (field := model.model_fields.get(key)) is not None and field.is_required()
    )


def _root_over_requires_a_branch_key(model: type[BaseModel]) -> bool:
    """Whether the generated root requires a key its SCHEMA root does not.

    Used as an EXEMPTION by the subclass-refusal test below, because such a root
    refuses a document omitting the key by ordinary field requiredness and never
    reaches the after-validator -- which says nothing about whether the
    combinator was inherited, this file's actual subject. The exemption
    disappears on its own when salesagent-8qela.80 is fixed, so no list goes
    stale.
    """
    return bool(_over_required_keys(model))


_POPULATION = _split_roots()


def test_the_split_root_population_is_derived_and_not_empty() -> None:
    """The marker resolves, so the test below grades something."""
    assert len(_POPULATION) >= _MIN_SPLIT_ROOTS, (
        f"only {len(_POPULATION)} split roots found, expected at least {_MIN_SPLIT_ROOTS} — "
        "the `_branch_models` marker has moved and this file no longer grades anything"
    )
    assert any(name.endswith(".CreativeManifest") for name, _ in _POPULATION), (
        "CreativeManifest is the known case this guard was written for and it is absent "
        f"from the derived population: {[n for n, _ in _POPULATION]}"
    )


@pytest.mark.parametrize(("name", "root"), _POPULATION, ids=[name for name, _ in _POPULATION])
def test_a_subclass_still_refuses_what_its_split_root_refuses(
    name: str, root: type[BaseModel]
) -> None:
    if not _generator_can_compile_a_check(root):
        # No merged shape, so there are no root FIELDS for a compiled field-group
        # check to read: the branches genuinely ARE the types, which is one of the
        # reasons ``flat_root_shape`` names for declining a root. The union
        # wrapper is the only mechanism available for these, and a subclass loses
        # it with no compiled form to fall back on (salesagent-8qela.81).
        # Derived from the pin, not listed.
        pytest.skip(
            f"{name}: the schema root declares no properties, so no compiled "
            "field-group check is expressible (salesagent-8qela.81)"
        )

    document = _document_satisfying_no_branch(root)
    if document is None:
        pytest.skip(f"{name}: no document found that the root refuses by a combinator")

    root_types = _refusal_types(root, document)
    assert root_types is not None and root_types & _COMBINATOR_TYPES  # the helper's contract

    subclass = type(root.__name__ + "Subclass", (root,), {})
    subclass_types = _refusal_types(subclass, document)
    assert subclass_types is not None, (
        f"{name}: a bare subclass ACCEPTS a document its root refuses by "
        f"{sorted(root_types & _COMBINATOR_TYPES)}. The branch constraint lives in the "
        "root's core schema and a subclass is handed its own, so it consults no union. "
        "Emit the compiled constraint on the shared base (salesagent-8qela.76)."
    )
    if _root_over_requires_a_branch_key(root):
        # The keyword cannot survive here, and the reason is a DIFFERENT defect:
        # the generated root requires a key its schema root does not, so a
        # document omitting it fails FIELD validation and no after-validator
        # runs (salesagent-8qela.80). The refusal above is still asserted --
        # what cannot be asserted is which keyword carries it. Derived from the
        # pin rather than listed, so this exemption disappears by itself when
        # that bead is fixed and no allowlist goes stale.
        return
    assert subclass_types & _COMBINATOR_TYPES, (
        f"{name}: a bare subclass refuses by {sorted(subclass_types)} rather than by the "
        f"combinator its root uses ({sorted(root_types & _COMBINATOR_TYPES)}). A refusal "
        "for another reason — field requiredness above all — is not the constraint being "
        "inherited, and a consumer reading the error type to learn WHY gets a different "
        "answer than the root gives."
    )


#: Measured after the fix at the 3.2.1 pin: 13 of the 18 split roots carry the
#: compiled check; the five that do not declare no merged shape of their own
#: (salesagent-8qela.81). A floor, because the skip above would otherwise be
#: tautological -- it skips exactly the roots without a compiled check, so an
#: emitter that stopped emitting would turn every case into a pass.
_MIN_ROOTS_CARRYING_THE_COMPILED_CHECK = 11


def test_the_compiled_check_is_emitted_widely() -> None:
    """The emission still reaches most split roots, so the skip above stays honest."""
    carrying = [name for name, root in _POPULATION if _carries_compiled_check(root)]
    assert len(carrying) >= _MIN_ROOTS_CARRYING_THE_COMPILED_CHECK, (
        f"only {len(carrying)} of {len(_POPULATION)} split roots carry the compiled "
        f"field-group check, expected at least {_MIN_ROOTS_CARRYING_THE_COMPILED_CHECK} — "
        "the emission in compose_branch_module has stopped reaching them, and the "
        "inheritance test above would SKIP rather than fail"
    )


def test_a_split_root_requires_only_what_its_schema_root_requires() -> None:
    """The merged root must not carry its branches' requiredness.

    THE DEFECT. The generator merges the branches' properties onto the root class
    and carries their requiredness with them, so a root requires exactly the keys
    its branches SELECT BY while its schema root requires none of them
    (salesagent-8qela.80). Two consequences, and the second is why this is a
    precondition rather than a tidy-up:

    * a document omitting such a key fails FIELD validation, so the root's
      after-validator never runs and the combinator refusal is masked -- which is
      the exemption ``_root_over_requires_a_branch_key`` grants above, and the
      reason the test it exempts cannot grade those roots at all;
    * "which keys is this document missing that the ROOT requires" stops being
      answerable, because the class's own requiredness no longer matches the
      schema's. salesagent-8qela.116 needs exactly that question answered, and an
      intersection over the branches' errors cannot answer it while this stands:
      measured against each root's pinned ``required`` list, that derivation
      agrees on 8 of 18 roots, and TWO of its disagreements are this defect.

    THE POPULATION IS ASSERTED ON THE SCHEMA SIDE, which is what keeps a green
    result meaningful. ``_branch_keys_the_schema_root_does_not_require`` reads the
    pin and nothing else, so it reports the same keys before and after any
    generator change. If it ever returns nothing for every root, this test is
    comparing an empty set and passes while grading nothing -- so that is checked
    first and fails loudly instead.

    ``list-account-changes-response`` IS NOT ONE OF THE VIOLATORS and must not be
    restored as one. Its root requires ``status`` through its ``allOf`` of
    ``core/protocol-envelope``, so the generated root requiring it is CORRECT.
    This guard asserted it as a defect for as long as its schema side read
    ``schema["required"]`` alone (salesagent-8qela.137) -- which meant the guard
    could only be satisfied by relaxing a field the pin requires. It is still
    GRADED, which is the assertion below: the probe must keep reaching it and
    keep finding nothing, because "absent from the violator set" and "absent from
    the population" are different facts and only the first is wanted.
    """
    shape = {
        name: sorted(_branch_keys_the_schema_root_does_not_require(root))
        for name, root in _POPULATION
    }
    with_shape = {name: keys for name, keys in shape.items() if keys}
    assert with_shape, (
        "no split root in the pinned bundle has a key every branch requires and its "
        "schema root does not, so there is nothing for an over-requirement to be "
        "measured against and this test grades nothing — the shape has moved, not "
        "the defect"
    )
    assert any(name.endswith(".VerifyBrandClaimRequest") for name in with_shape), (
        "verify-brand-claim-request is the case this test exists for: its root requires "
        "neither `claim` nor `claim_type` and all four of its branches require both. It "
        "is the root whose relaxation the assertion below grades, and it is absent from "
        f"the schema-side shape: {sorted(with_shape)}"
    )
    assert any(name.endswith(".ListAccountChangesResponse") for name, _ in _POPULATION), (
        "list-account-changes-response has left the derived population, so this test no "
        "longer looks at the root whose `status` is root-required ONLY through an allOf "
        "$ref — the one shape that distinguishes a correct derivation from one reading "
        f"`schema['required']` alone: {[name for name, _ in _POPULATION]}"
    )

    over = {name: _over_required_keys(root) for name, root in _POPULATION}
    violations = {name: keys for name, keys in over.items() if keys}
    assert not violations, (
        "generated split root(s) require keys their schema root does not, so the "
        "branches' requiredness has been merged onto the root: "
        + "; ".join(f"{name} requires {keys}" for name, keys in sorted(violations.items()))
        + f" — the schema-side shape exists for {sorted(with_shape)}"
    )
