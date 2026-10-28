"""``format_kind`` is one open ``str``; the vocabulary is exported, not enforced.

``core/canonical-format-kind.json`` declares a closed 16-member ``enum`` and,
in the same file, requires a consumer to retain an unknown value and not fail
the payload — "the producer-side enum stays closed; the consumer-side enum
stays open for forward compatibility." The schema knows the rule is directional
and then encodes it as one closed enum, which cannot carry that.

So every reference generates ``str`` and **no model refuses a value**: a pinned
SDK cannot tell a kind a seller invented from a kind defined after its pin, and
refusing the second to prevent the first would make this library's version a
ceiling on what the protocol permits. The vocabulary is not discarded, it is
relocated — :class:`CanonicalFormatKind` stays a first-class export for
comparison, and :func:`is_canonical_format_kind` answers membership against a
vocabulary the CALLER supplies. Upstream ask: adcontextprotocol/adcp#7929.

Where the behaviour is graded:

* ``tests/test_forward_compat_format_kind.py`` — an unrecognised value
  survives construction, validation, serialization and re-parsing on every
  model that carries the field;
* ``tests/test_delivery_manifest_readback.py`` and
  ``tests/test_manifest_response_readback.py`` — what is left of #1241 once
  there is no strict side: the structural rules about instances;
* **this module** — the properties of the OVERRIDE itself: it is applied in one
  place, nothing re-closes the vocabulary, the enum stays exported, and the
  predicate takes the caller's vocabulary.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

import pytest
from pydantic import BaseModel

import adcp.types
from adcp.types.canonical_creative import PRIMARY_CANONICAL_MODELS, is_canonical_format_kind

# From the DEFINITION module, deliberately: ``test_the_vocabulary_enum_stays_exported``
# asserts that ``adcp.types`` re-exports it, and that assertion can only run if
# importing this module does not itself depend on the export being there.
from adcp.types.domains.core.canonical_format_kind import CanonicalFormatKind
from adcp.validation.schema_loader import _ensure_state

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_GENERATED = _REPO_ROOT / "src" / "adcp" / "types" / "domains"

sys.path.insert(0, str(_REPO_ROOT))
from scripts.generate_types import OPEN_VOCABULARY_SCHEMAS  # noqa: E402

_VOCABULARY_SCHEMA = pathlib.Path("core/canonical-format-kind.json")
#: The two field names a model can carry the vocabulary under.
_KIND_FIELDS = ("format_kind", "format_kinds")


# ---------------------------------------------------------------------------
# The override lives in one place.
# ---------------------------------------------------------------------------


def test_the_override_is_declared_once_and_names_the_schema_it_overrides() -> None:
    """One entry in one set, in the generator — not a widening per call site."""
    assert OPEN_VOCABULARY_SCHEMAS == {_VOCABULARY_SCHEMA}


def test_no_generated_model_types_a_field_with_the_closed_enum() -> None:
    """The property the single transform buys: nothing re-closes the vocabulary.

    A per-site widening leaves every site it missed closed, which is what the
    scaffolding this replaced was for. Asserted over the whole generated tree:
    the enum may be IMPORTED — it is the vocabulary, and ``core/__init__``
    re-exports it — but may not annotate a field.
    """
    offenders: list[str] = []
    for path in sorted(_GENERATED.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # The IDENTIFIER, not the substring. A longer generated name that
            # merely ENDS in it re-closes nothing: branch-class names are built
            # from their branch's discriminating condition, so
            # ``CreativeAssetV2CreativeCanonicalFormatKind`` -- the oneOf/1
            # branch of core/creative-asset.json -- contains these 18 characters
            # and annotates no field with the enum. A substring test reported all
            # seven of its occurrences as re-closures while the real count was
            # zero, which is a guard grading a spelling rather than the property
            # this docstring states.
            if not re.search(r"\bCanonicalFormatKind\b", stripped):
                continue
            if (
                stripped.startswith(("import ", "from ", "#", '"'))
                or '"CanonicalFormatKind"' in stripped
                # the vocabulary's own declaration, which is the point
                or stripped == "class CanonicalFormatKind(StrEnum):"
            ):
                continue
            offenders.append(f"{path.relative_to(_GENERATED)}:{number}  {stripped}")
    assert offenders == [], "\n".join(
        ["these generated declarations re-close the open vocabulary:", *offenders]
    )


def _referencing_property_names() -> set[str]:
    """Property names whose declaration references the vocabulary schema."""
    state = _ensure_state(None)
    names: set[str] = set()

    def walk(node: object, enclosing: str | None) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.endswith(_VOCABULARY_SCHEMA.as_posix()) and enclosing:
                names.add(enclosing)
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for name, declaration in value.items():
                        walk(declaration, name)
                else:
                    walk(value, enclosing)
        elif isinstance(node, list):
            for item in node:
                walk(item, enclosing)

    for file in sorted(state.root.root.rglob("*.json")):
        if file.name != "index.json":
            walk(json.loads(file.read_text()), None)
    return names


def test_every_reference_to_the_schema_generated_a_string() -> None:
    """Read from the bundle, not from a list: every ``$ref`` is accounted for."""
    referencing = _referencing_property_names()
    assert referencing, "no property references the schema — it moved"

    graded = 0
    for path in sorted(_GENERATED.rglob("*.py")):
        source = path.read_text()
        for name in referencing:
            for marker in (f"    {name}: Annotated[", f"    {name}: "):
                index = source.find(marker)
                if index == -1:
                    continue
                declaration = source[index : index + 400]
                assert (
                    "CanonicalFormatKind" not in declaration.split("Field(")[0]
                ), f"{path.relative_to(_GENERATED)} declares {name} with the closed enum"
                graded += 1
                break
    assert graded > 15, f"only {graded} declarations examined"


# ---------------------------------------------------------------------------
# The vocabulary is relocated, not discarded.
# ---------------------------------------------------------------------------


def test_the_vocabulary_enum_stays_exported() -> None:
    """The one thing the override must not take away.

    No field references ``CanonicalFormatKind`` any more, so nothing in the
    generated tree forces it to keep existing — a later regeneration could drop
    it as unused, and then a string field has no constants and every consumer
    writes literals. This is the test that keeps that honest. It is also what
    makes ``creative.format_kind == CanonicalFormatKind.image`` the sanctioned
    comparison: the enum is a ``StrEnum``, so the member equals the plain
    string a model carries.
    """
    assert "CanonicalFormatKind" in adcp.types.__all__
    assert adcp.types.CanonicalFormatKind is CanonicalFormatKind
    assert issubclass(CanonicalFormatKind, str)
    assert len(list(CanonicalFormatKind)) == 16
    assert "image" == CanonicalFormatKind.image


def test_the_exported_vocabulary_is_the_pinned_bundle_s() -> None:
    """Derived from the schema, so it cannot drift from the pin."""
    bundle = json.loads((_ensure_state(None).root.core / "canonical-format-kind.json").read_text())
    assert {kind.value for kind in CanonicalFormatKind} == set(bundle["enum"])


# ---------------------------------------------------------------------------
# One helper, whose vocabulary is the caller's.
# ---------------------------------------------------------------------------


def test_the_predicate_is_exported_and_defaults_to_the_pinned_vocabulary() -> None:
    assert "is_canonical_format_kind" in adcp.types.__all__
    assert adcp.types.is_canonical_format_kind is is_canonical_format_kind
    assert is_canonical_format_kind("image")
    assert is_canonical_format_kind(CanonicalFormatKind.image)
    assert not is_canonical_format_kind("holographic_banner")


@pytest.mark.parametrize("value", [None, 7, b"image", ["image"]])
def test_the_predicate_answers_false_for_a_non_string(value: object) -> None:
    """A yes/no question answers yes or no — it does not raise.

    ``CanonicalFormatKind(value)`` raising ``ValueError`` is a working test and
    an awkward API; a consumer should not write ``try``/``except`` to ask
    whether a value is canonical.
    """
    assert is_canonical_format_kind(value) is False


def test_the_vocabulary_is_the_callers() -> None:
    """The parameter is the point: a seller's set is not this SDK's.

    It can be LARGER — the seller speaks a newer spec and handles a kind this
    pin has never heard of — or SMALLER — the seller implements four of the
    sixteen. Neither is expressible by anything the library knows, so the
    vocabulary is supplied rather than assumed.
    """
    newer_spec = {*(kind.value for kind in CanonicalFormatKind), "holographic_banner"}
    assert is_canonical_format_kind("holographic_banner", newer_spec)
    assert is_canonical_format_kind("image", newer_spec)

    seller_subset = {"image", "html5"}
    assert is_canonical_format_kind("image", seller_subset)
    assert not is_canonical_format_kind("video_vast", seller_subset)


# ---------------------------------------------------------------------------
# One type, no per-direction model.
# ---------------------------------------------------------------------------


def _carries_a_kind(model: type[BaseModel]) -> bool:
    return any(name in model.model_fields for name in _KIND_FIELDS)


KIND_BEARING = [model for model in PRIMARY_CANONICAL_MODELS if _carries_a_kind(model)]
KIND_BEARING_IDS = [model.__name__ for model in KIND_BEARING]


def test_the_kind_bearing_model_set_is_not_empty() -> None:
    assert len(KIND_BEARING) == 10, KIND_BEARING_IDS


@pytest.mark.parametrize("model", KIND_BEARING, ids=KIND_BEARING_IDS)
def test_every_kind_bearing_model_types_the_kind_as_a_string(
    model: type[BaseModel],
) -> None:
    """One type on every model. That is the whole override."""
    for name in _KIND_FIELDS:
        field = model.model_fields.get(name)
        if field is None:
            continue
        rendered = str(field.annotation)
        assert "CanonicalFormatKind" not in rendered, f"{model.__name__}.{name}: {rendered}"
        assert "str" in rendered, f"{model.__name__}.{name}: {rendered}"


@pytest.mark.parametrize("model", KIND_BEARING, ids=KIND_BEARING_IDS)
def test_no_model_carries_a_vocabulary_validator(model: type[BaseModel]) -> None:
    """No per-direction strictness anywhere — the SDK does not discriminate.

    Stated structurally rather than by probing values, so a validator
    reintroduced under any name on any of these models fails here. A field
    validator bound to ``format_kind`` or ``format_kinds`` is what a
    reintroduced ``_StrictFormatKind`` would look like.
    """
    bound = {
        name
        for name, decorator in model.__pydantic_decorators__.field_validators.items()
        if set(decorator.info.fields) & set(_KIND_FIELDS)
    }
    assert bound == set(), f"{model.__name__} validates the vocabulary: {sorted(bound)}"
