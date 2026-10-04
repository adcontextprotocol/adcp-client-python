"""Contract for the class hierarchy codegen renders from ``allOf`` composition.

A schema whose root is ``{"allOf": [{"$ref": "core/version-envelope.json"}, ...]}``
says "this message *is* a versioned envelope, plus the following". The Python
rendering of that sentence is inheritance:
``class GetProductsRequest(AdcpVersionEnvelope)``. Flattening the referenced
schema's fields into the composing class keeps the data and loses the sentence —
and with it any public type a consumer can name to mean "an AdCP request", so a
mixin, a generic boundary, or an ``isinstance`` check has nothing to bind to.

That rendering has changed shape twice in two majors without the schemas
changing. Requests descending from ``AdcpVersionEnvelope`` went 75/80 in 6.6.0
to **0/77** in 7.0.2 to 78/92 in 8.0.0, while
``media-buy/get-products-request.json`` is byte-identical across the 6.6.0 and
7.0.2 bundles and composes the envelope by root ``allOf`` in both.

These tests walk the schema tree and the generated tree together and hold the
rendering: every schema that composes by root ``allOf`` + ``$ref`` renders as a
subclass of the referenced schema's type. There is no allowlist. Where
inheritance is not expressible at all the pair is *refused* with a reason
derived from the referenced schema's own generated shape — never named in a
table, so a flattened class cannot hide in it.

See: https://github.com/adcontextprotocol/adcp-client-python/issues/1313
"""

from __future__ import annotations

import ast
import functools
import importlib
import json
import typing
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, RootModel

from adcp.validation.version import resolve_bundle_key
from scripts import generate_types as codegen
from scripts.post_generate_fixes import _resolve_schema_ref, _schema_title_to_class_name

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GENERATED_DIR = _REPO_ROOT / "src" / "adcp" / "types" / "generated_poc"
_BUNDLE_KEY = resolve_bundle_key((_REPO_ROOT / "src" / "adcp" / "ADCP_VERSION").read_text().strip())
_SCHEMA_DIR = _REPO_ROOT / "schemas" / "cache" / _BUNDLE_KEY

# Floors, not equalities: the schema bundle grows between AdCP versions, and a
# count pinned to the day would churn without grading anything new. What these
# catch is the resolver quietly matching nothing — a regression that would
# otherwise turn every assertion below into a vacuous pass. Measured at the
# 3.2.1 pin: 316 pairs, 306 graded, 153 against version-envelope, 77 against
# protocol-envelope. A pin that moves *backwards* legitimately lowers them (the
# 3.1 bundle carries 228 pairs), so lower them with the pin in that one case.
_MIN_COMPOSITION_PAIRS = 250
_MIN_GRADED_PAIRS = 250
_MIN_ENVELOPE_DESCENDANTS = {
    Path("core/version-envelope.json"): 120,
    Path("core/protocol-envelope.json"): 60,
}
# 50 at the 3.2.1 pin: 25 SuccessResponse plus 25 ErrorResponse. #1136 counted
# 24 of them when it was filed.
_MIN_RESPONSE_ARM_ALIASES = 40


# ---------------------------------------------------------------------------
# Walking the schema tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Composition:
    """One ``(schema, root $ref)`` pair: a schema composing another schema."""

    schema: Path  # bundle-relative path of the composing schema
    ref: str  # the literal ``$ref`` string, for the failure message
    target: Path | None  # bundle-relative path of the referenced schema

    def __str__(self) -> str:
        return f"{self.schema} -> {self.target or self.ref}"


def _generated_schemas() -> list[Path]:
    """Bundle-relative schemas the generator turns into a Python module.

    Uses the generator's own exclusions rather than a second opinion about
    them: MCP transport artifacts and all but a few bundled schemas produce no
    Python types (see ``scripts/generate_types.py``), so they have no rendering
    to grade.
    """
    kept: list[Path] = []
    for path in sorted(_SCHEMA_DIR.rglob("*.json")):
        rel = path.relative_to(_SCHEMA_DIR)
        if set(rel.parts[:-1]) & codegen.GENERATED_SCHEMA_EXCLUDE_DIRS:
            continue
        if rel in codegen.GENERATED_SCHEMA_EXCLUDE_FILES:
            if rel not in codegen.ROOT_DISCOVERY_SCHEMAS:
                continue
        if rel.parts[0] == codegen.BUNDLED_DIR_NAME:
            if Path(*rel.parts[1:]).with_suffix(".py") not in codegen.BUNDLED_KEEP:
                continue
        kept.append(rel)
    return kept


@functools.cache
def _compositions() -> tuple[_Composition, ...]:
    """Every root-level ``allOf`` ``$ref`` in the generated schema population."""
    out: list[_Composition] = []
    for rel in _generated_schemas():
        schema = json.loads((_SCHEMA_DIR / rel).read_text())
        if not isinstance(schema, dict):
            continue
        all_of = schema.get("allOf")
        if not isinstance(all_of, list):
            continue
        for entry in all_of:
            if not isinstance(entry, dict):
                continue
            ref = entry.get("$ref")
            if not isinstance(ref, str):
                continue  # an inline constraint arm, not a composition
            fragment = ref.split("#", 1)[1] if "#" in ref else ""
            if ref.startswith("#") or fragment not in ("", "/"):
                # A JSON pointer names a sub-schema, which has no generated
                # type of its own to inherit from. One such ref at the 3.2.1
                # pin: core/package-delivery-metric-value.json.
                out.append(_Composition(rel, ref, None))
                continue
            out.append(_Composition(rel, ref, _resolve_schema_ref(rel, ref)))
    return tuple(out)


# ---------------------------------------------------------------------------
# Walking the generated tree
# ---------------------------------------------------------------------------


def _module_path(rel: Path) -> Path:
    mapped = codegen.ROOT_DISCOVERY_SCHEMAS.get(rel, rel.with_suffix(".py"))
    return _GENERATED_DIR / Path(*(part.replace("-", "_") for part in mapped.parts))


def _module_name(rel: Path) -> str:
    relative = _module_path(rel).relative_to(_GENERATED_DIR).with_suffix("")
    return "adcp.types.generated_poc." + ".".join(relative.parts)


@functools.cache
def _root_symbol(rel: Path) -> str | None:
    """The module-level names the generator gave this schema's root, or ``None``.

    ``"Name"`` for a plain root, ``"Name|Name1|Name2"`` when the root also
    fans out — the bare name first, then the branch names. See
    :func:`_root_names` for how the two halves are chosen.
    """
    names = _root_names(rel)
    return None if names is None else "|".join([*names[0], *names[1]])


@functools.cache
def _root_names(rel: Path) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """``(root names, branch names)`` for this schema's root.

    The generator derives the name from the schema ``title`` (``Get Products
    Request`` -> ``GetProductsRequest``), so that is what we look for — as a
    class, as an alias assignment, or both.

    A root with ``oneOf`` beside its ``allOf`` also emits a ``NameN`` branch
    class per arm, and those are collected **in addition to** the bare name,
    not instead of it. That is the whole of #1136: the composition was lost on
    19 of 24 response branches while the top-level name was fine, so a resolver
    that stops at the name matching the schema file grades none of the classes
    the regression touched. The generator currently routes some branch families
    through a shared root (``CreateContentStandardsResponse1`` inherits
    ``CreateContentStandardsResponse``) and declares others independently
    (``CreateMediaBuyResponse1`` inherits the two envelopes directly); which of
    those it picks must not decide whether the branch is graded.

    ``N`` is the whole numeric run after the candidate, not one digit: a root
    that fans out into ten or more arms emits ``Name10`` upward, and this tree
    carries 341 classes with a multi-digit suffix (``AcceptancePolicyRequirement17``,
    ``AdcpAgentsAuthorization213``). Matching a single trailing character would
    drop those arms from the grading silently, which is the under-grading
    #1136 is about.

    Returning ``None`` rather than guessing at the last class in the file is
    deliberate: an unresolved root is reported by
    :func:`test_every_composition_resolves_to_a_generated_type`, not silently
    dropped from the grading.
    """
    module = _module_path(rel)
    if not module.exists():
        return None
    tree = ast.parse(module.read_text())
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    bound = [
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ] + [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]

    schema = json.loads((_SCHEMA_DIR / rel).read_text())
    title = schema.get("title")
    candidates = []
    if isinstance(title, str) and title.strip():
        candidates.append(_schema_title_to_class_name(title))
    candidates.append(_schema_title_to_class_name(rel.stem.replace("-", " ")))

    for candidate in candidates:
        root = tuple(name for name in (*classes, *bound) if name.lower() == candidate.lower())
        branches = tuple(
            sorted(
                name
                for name in classes
                if name[len(candidate) :].isdigit()
                and name[: len(candidate)].lower() == candidate.lower()
            )
        )
        if root or branches:
            return root, branches
    return None


def _resolve(rel: Path, names: tuple[str, ...]) -> tuple[type, ...]:
    """The classes behind module-level names, with unions flattened to arms.

    Deduplicated: a root that fans out is reachable both as its union alias and
    as the branch classes the alias is built from, and counting a class twice
    would overstate the obligations.
    """
    module = importlib.import_module(_module_name(rel))
    found: dict[type, None] = {}
    for name in names:
        obj = getattr(module, name)
        if isinstance(obj, type):
            found[obj] = None
            continue
        # A composing root is ``Annotated[A | B, Field(...)]``: the metadata
        # carries the schema's discriminator, and the union is its first arg.
        if typing.get_origin(obj) is typing.Annotated:
            obj = typing.get_args(obj)[0]
        arms = typing.get_args(obj)
        assert arms, f"{_module_name(rel)}.{name} is neither a class nor a union: {obj!r}"
        for arm in arms:
            assert isinstance(arm, type), f"{_module_name(rel)}.{name} arm is not a class: {arm!r}"
            found[arm] = None
    return tuple(found)


@functools.cache
def _generated_type(rel: Path) -> tuple[type, ...] | None:
    """Every class that renders this schema's root, branch classes included.

    A ``NameN`` class is a rendering of the root when it *is* the root (a
    branch of its union alias) or descends from it. When it is a **base** of
    the root instead, the generator has split one root across fragments and
    fused them — ``CheckGovernanceRequest3(CheckGovernanceRequest1,
    CheckGovernanceRequest2)``, where fragment 1 holds the inline properties
    and fragment 2 holds the composed envelope. Neither fragment renders the
    root on its own, and requiring the composition of a fragment that was
    never meant to carry it would be a false failure. The distinction is read
    off the class graph, not off the names.
    """
    names = _root_names(rel)
    if names is None:
        return None
    root = _resolve(rel, names[0])
    renders = dict.fromkeys(root)
    for branch in _resolve(rel, names[1]):
        if any(cls is not branch and issubclass(cls, branch) for cls in root):
            continue  # a fragment the root is fused from, not a rendering of it
        renders[branch] = None
    return tuple(renders)


def _refusal(referenced: tuple[type, ...]) -> str | None:
    """Why inheritance from this type is not expressible, or ``None``.

    Both reasons are read off the *referenced* schema's generated shape, so a
    flattening regression in a composing schema cannot land in here: it would
    have to change the parent to hide.
    """
    if len(referenced) != 1:
        names = ", ".join(cls.__name__ for cls in referenced)
        return f"referenced type fans out into {len(referenced)} arms ({names})"
    (parent,) = referenced
    if issubclass(parent, RootModel):
        return f"referenced type is a value wrapper, {parent.__mro__[1]!s}"
    assert issubclass(parent, BaseModel), f"{parent!r} is not a Pydantic model"
    return None


@functools.cache
def _graded() -> tuple[tuple[_Composition, tuple[type, ...], type], ...]:
    """Compositions where inheritance is expressible, with their two ends."""
    out: list[tuple[_Composition, tuple[type, ...], type]] = []
    for composition in _compositions():
        if composition.target is None:
            continue
        referenced = _generated_type(composition.target)
        composing = _generated_type(composition.schema)
        if referenced is None or composing is None:
            continue
        if _refusal(referenced) is None:
            out.append((composition, composing, referenced[0]))
    return tuple(out)


# ---------------------------------------------------------------------------
# The ratchet
# ---------------------------------------------------------------------------


def test_root_allof_ref_renders_as_inheritance() -> None:
    """Every composed root renders as a subclass of what it composes.

    This is the guard. It walks the whole schema tree against the whole
    generated tree, so a regression in the rendering shows up here whatever it
    renames — and there is no list of known-good pairs it could go stale
    against.
    """
    flattened: list[str] = []
    graded = _graded()

    for composition, composing, parent in graded:
        for cls in composing:
            if not issubclass(cls, parent):
                flattened.append(
                    f"{composition.schema}::{cls.__name__} is not a subclass of "
                    f"{parent.__name__} ({composition.target}), which its root allOf "
                    f"composes via {composition.ref}"
                )

    assert flattened == [], (
        "These schemas compose another schema at their root but the generated "
        "class does not inherit from it, so the composition survives only as "
        "copied fields and no type says what the message is:\n"
        + "\n".join(f"  {line}" for line in flattened)
    )
    assert len(graded) >= _MIN_GRADED_PAIRS, (
        f"only {len(graded)} composition pairs were graded, expected at least "
        f"{_MIN_GRADED_PAIRS} — the resolver has stopped matching the generated tree, "
        "so this test is passing vacuously"
    )


def test_every_composition_resolves_to_a_generated_type() -> None:
    """No composition pair may fall out of the grading unexplained.

    The ratchet above can only grade what it resolves, so the two ways it could
    quietly stop grading something — a schema whose root symbol no longer
    matches, and a refusal reason we have not accounted for — are failures here
    rather than silence there.
    """
    compositions = _compositions()
    assert len(compositions) >= _MIN_COMPOSITION_PAIRS, (
        f"found only {len(compositions)} root allOf $ref pairs, expected at least "
        f"{_MIN_COMPOSITION_PAIRS} — the schema walk has stopped finding compositions"
    )

    unresolved: list[str] = []
    refused: list[str] = []
    for composition in compositions:
        if composition.target is None:
            refused.append(f"{composition}: ref names a sub-schema, not a schema")
            continue
        for end, rel in (("composing", composition.schema), ("referenced", composition.target)):
            if not _module_path(rel).exists():
                unresolved.append(f"{composition}: no generated module for the {end} schema {rel}")
            elif _root_symbol(rel) is None:
                unresolved.append(f"{composition}: no root symbol for the {end} schema {rel}")

        referenced = _generated_type(composition.target)
        if referenced is not None and (reason := _refusal(referenced)) is not None:
            refused.append(f"{composition}: {reason}")

    assert unresolved == [], (
        "A schema's root no longer maps onto a generated symbol. Either the "
        "generator renamed it or the module is gone; until it resolves, its "
        "rendering is ungraded:\n" + "\n".join(f"  {line}" for line in unresolved)
    )
    # Every refusal is one of the three derived kinds above. There is no
    # allowlist to extend: a pair whose referenced schema renders as a single
    # object model is graded, with no exception available to it.
    assert all(
        ("fans out into" in line) or ("value wrapper" in line) or ("sub-schema" in line)
        for line in refused
    ), "unaccounted refusal reason:\n" + "\n".join(f"  {line}" for line in refused)


def test_envelope_composition_is_rendered_as_a_base_class() -> None:
    """The envelopes specifically: the base every request and response composes.

    This is the regression in #1313 stated as its own assertion. 7.0.2 rendered
    ``core/version-envelope.json`` as copied fields on all 77 requests that
    compose it, leaving no type that means "an AdCP request". A rendering that
    inherits from the envelopes in only a handful of places would satisfy the
    test above and still leave consumers with nothing to bind to.
    """
    counted: dict[Path, int] = {path: 0 for path in _MIN_ENVELOPE_DESCENDANTS}
    for composition, composing, parent in _graded():
        if composition.target not in counted:
            continue
        for cls in composing:
            assert issubclass(cls, parent), (
                f"{composition.schema}::{cls.__name__} does not inherit {parent.__name__}"
            )
        counted[composition.target] += 1

    short = {
        path: (found, floor)
        for path, floor in _MIN_ENVELOPE_DESCENDANTS.items()
        if (found := counted[path]) < floor
    }
    assert short == {}, (
        "Fewer schemas inherit the protocol envelopes than compose them. The "
        "generator has stopped rendering the composition as inheritance for "
        f"most of the tree: {short} (found, expected at least)"
    )


def test_public_surface_keeps_the_composed_ancestry() -> None:
    """A name exported from ``adcp.types`` keeps the ancestry its schema declares.

    Inheritance inside ``generated_poc`` is worth nothing to a consumer if the
    layer that re-exports the name drops the base. ``canonical_creative``
    rebuilds some boundary models with ``create_model``, which is exactly where
    a base can go missing without any schema changing.
    """
    import adcp.types as public

    lost: list[str] = []
    checked = 0
    for composition, composing, parent in _graded():
        for cls in composing:
            exported = getattr(public, cls.__name__, None)
            if not isinstance(exported, type):
                continue  # internal to the generated tree; nothing public to hold
            checked += 1
            if not issubclass(exported, parent):
                lost.append(
                    f"adcp.types.{cls.__name__} ({exported.__module__}) is not a subclass "
                    f"of {parent.__name__}, which {composition.schema} composes at its root"
                )

    assert lost == [], (
        "These public types lost a base their schema declares, so the "
        "composition is invisible to consumers:\n" + "\n".join(f"  {line}" for line in lost)
    )
    assert checked, "no graded class is exported from adcp.types; the lookup is broken"


def test_response_arm_aliases_keep_the_protocol_envelope() -> None:
    """Every ``*SuccessResponse`` / ``*ErrorResponse`` alias is a ProtocolEnvelope.

    This is #1136 stated as its own assertion: ``oneOf`` response branches lost
    the root ``allOf`` ``ProtocolEnvelope`` on 19 of 24 ``SuccessResponse``
    aliases. That was fixed in August and has held — but the aliases are a
    layer above the generated tree, and two of them
    (``CreateMediaBuyErrorResponse``, ``UpdateMediaBuyErrorResponse``) bind
    ``canonical_creative`` clones that are reachable under no other name, so
    the two tests above would not notice if they lost the base again.

    ``ProtocolEnvelope`` is named here rather than derived. Deriving it would
    mean mapping an alias name back to a schema file by convention, which is
    the guesswork the rest of this module avoids; the obligation is the same
    for every arm of every task response, so naming it once is auditable.
    """
    from adcp.types import aliases
    from adcp.types.generated_poc.core.protocol_envelope import ProtocolEnvelope

    arms = sorted(
        name for name in aliases.__all__ if name.endswith(("SuccessResponse", "ErrorResponse"))
    )
    assert len(arms) >= _MIN_RESPONSE_ARM_ALIASES, (
        f"found only {len(arms)} response arm aliases, expected at least "
        f"{_MIN_RESPONSE_ARM_ALIASES} — the alias surface has moved and this test no "
        "longer grades it"
    )

    lost = [
        f"adcp.types.aliases.{name} ({cls.__module__}.{cls.__name__})"
        for name in arms
        if not issubclass(cls := getattr(aliases, name), ProtocolEnvelope)
    ]
    assert lost == [], (
        "These response arm aliases are not ProtocolEnvelope subclasses, so the "
        "root allOf composition is lost on the surface a consumer binds to — the "
        "#1136 regression:\n" + "\n".join(f"  {line}" for line in lost)
    )
