"""Contract for generated types whose JSON Schema root is a scalar.

A schema like ``core/property-tag.json`` — ``{"type": "string", "pattern":
"^[a-z0-9_]+$"}`` — describes a value. ``datamodel-code-generator`` emits
``RootModel[str]`` for it, which is not a ``str`` anywhere Python treats one:
``str(x)`` renders ``"root='sports'"``, ``x == "sports"`` is ``False``, and
``{x} & {"sports"}`` raises ``TypeError: unhashable type``. Four reports in
eight months (#138, #145, #155, #1077, #1086, #1087) were each fixed by
patching individual instances, and the construct came back somewhere else.

``post_generate_fixes.rewrite_scalar_rootmodels`` now rewrites every scalar
root to a ``str``/``int``/``float`` subclass. These tests hold both halves of
that line: scalar roots are their Python scalar, and roots that *compose* keep
their ``RootModel``, which is correct for those.

See: https://github.com/adcontextprotocol/adcp-client-python/issues/1277
"""

from __future__ import annotations

import ast
import json
import warnings
from pathlib import Path

import pytest
from pydantic import BaseModel, RootModel, TypeAdapter, ValidationError

from adcp.types._scalar import ScalarFloat, ScalarInt, ScalarStr
from adcp.validation.version import resolve_bundle_key

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GENERATED_DIR = _REPO_ROOT / "src" / "adcp" / "types" / "generated_poc"
_BUNDLE_KEY = resolve_bundle_key((_REPO_ROOT / "src" / "adcp" / "ADCP_VERSION").read_text().strip())
_SCHEMA_DIR = _REPO_ROOT / "schemas" / "cache" / _BUNDLE_KEY

_SCALAR_BASES = (ScalarStr, ScalarInt, ScalarFloat)

# Keywords the rewriter is allowed to carry onto a generated subclass. A new
# schema keyword must be wired into the core schema, not silently dropped.
_ALLOWED_CONSTRAINTS = {
    "pattern",
    "min_length",
    "max_length",
    "ge",
    "gt",
    "le",
    "lt",
    "multiple_of",
}
_ALLOWED_DOCUMENTATION = {"title", "description", "examples"}


def _generated_scalar_roots() -> dict[str, type]:
    """Every exported generated type that is now a scalar subclass."""
    from adcp.types import _generated as gen

    return {
        name: obj
        for name in dir(gen)
        if isinstance(obj := getattr(gen, name), type) and issubclass(obj, _SCALAR_BASES)
    }


# ---------------------------------------------------------------------------
# The ratchet: no scalar root may regress to a RootModel wrapper
# ---------------------------------------------------------------------------


def test_no_scalar_rootmodels_remain_in_generated_tree() -> None:
    """No generated class wraps a bare ``str``/``int``/``float`` in a RootModel.

    This is the guard that keeps the fix from eroding. ``rewrite_scalar_rootmodels``
    reports — rather than rewrites — any scalar root it does not recognize, so a
    new schema shape lands here as a failure instead of as a quietly broken type.
    """
    offenders: list[str] = []
    wrapped = {"RootModel[str]", "RootModel[int]", "RootModel[float]"}

    for path in sorted(_GENERATED_DIR.rglob("*.py")):
        source = path.read_text()
        if "RootModel[" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ClassDef) or len(node.bases) != 1:
                continue
            base = ast.get_source_segment(source, node.bases[0])
            if base in wrapped:
                offenders.append(f"{path.relative_to(_GENERATED_DIR)}::{node.name}")

    assert offenders == [], (
        "These generated classes wrap a scalar in a RootModel, so they are not "
        "their own Python scalar. Teach scripts/post_generate_fixes.py's "
        f"_scalar_root_spec() to handle their shape: {offenders}"
    )


def test_every_scalar_root_has_exactly_one_class_behind_it() -> None:
    """No split identity: every surface exposes the same class object.

    The trap flagged in #1277 is fixing this in ``aliases.py`` instead of in the
    generator: generated modules import these names directly from
    ``generated_poc``, so a redefinition one layer up leaves two classes with
    one name and `isinstance` fails across the seam. The rewrite happens in
    ``generated_poc`` itself, and this holds the line.
    """
    import importlib

    import adcp
    import adcp.types
    from adcp.types import _generated as gen
    from adcp.types import aliases

    roots = _generated_scalar_roots()
    assert len(roots) > 100, f"expected the full scalar population, found {len(roots)}"

    failures: list[str] = []
    for name, cls in sorted(roots.items()):
        if not cls.__module__.startswith("adcp.types.generated_poc."):
            failures.append(f"{name}: defined in {cls.__module__}, not under generated_poc")
            continue
        owner = importlib.import_module(cls.__module__)
        if getattr(owner, cls.__name__, None) is not cls:
            failures.append(f"{name}: {cls.__module__}.{cls.__name__} is a different object")
        for surface in (adcp, adcp.types, aliases, gen):
            exposed = getattr(surface, name, None)
            if (
                isinstance(exposed, type)
                and issubclass(exposed, _SCALAR_BASES)
                and exposed is not cls
            ):
                failures.append(f"{name}: {surface.__name__}.{name} is a second scalar class")

    assert failures == [], "\n".join(failures)


def test_plain_scalar_normalizes_only_scalar_roots() -> None:
    """``plain_scalar`` is the seam for code that must not leak the subclass."""
    from adcp import PropertyTag
    from adcp.types import ViewThreshold
    from adcp.types._scalar import plain_scalar

    assert type(plain_scalar(PropertyTag("sports"))) is str
    assert plain_scalar(PropertyTag("sports")) == "sports"
    assert type(plain_scalar(ViewThreshold(0.5))) is float

    for untouched in ("sports", 3, 0.5, True, None, ["a"], {"a": 1}):
        assert plain_scalar(untouched) is untouched


def test_composing_roots_keep_their_rootmodel() -> None:
    """A root that composes is left alone — RootModel is right for those.

    Pins the other half of the generation condition: an over-eager rewrite that
    collapsed unions, arrays, or ``AnyUrl`` onto a scalar would fail here.
    """
    from adcp.types import _generated as gen

    composing = {
        "AccountReference": "anyOf of two object variants",
        "AcceptancePolicyProfileIds": "array of ids",
        "PostalArea": "anyOf of two object variants",
    }

    checked = 0
    for name, shape in composing.items():
        obj = getattr(gen, name, None)
        if obj is None or not isinstance(obj, type):
            continue
        assert issubclass(obj, RootModel), f"{name} ({shape}) should still be a RootModel"
        checked += 1

    assert checked, "none of the composing reference types resolved; update the names above"


# ---------------------------------------------------------------------------
# The three reproductions from the issue
# ---------------------------------------------------------------------------


def test_scalar_str_root_is_a_str() -> None:
    """``PropertyTag`` is a ``str``: isinstance, ``str()``, and f-strings."""
    from adcp import PropertyTag

    tag = PropertyTag("sports")

    assert isinstance(tag, str)
    assert str(tag) == "sports"
    assert f"{tag}" == "sports"
    assert tag.upper() == "SPORTS"
    assert json.dumps({"tag": tag}) == '{"tag": "sports"}'


def test_scalar_str_root_equals_a_bare_str() -> None:
    """Equality holds in both directions against a plain ``str``."""
    from adcp import PropertyTag

    tag = PropertyTag("sports")

    assert tag == "sports"
    assert "sports" == tag
    assert tag != "news"
    assert PropertyTag("sports") == PropertyTag("sports")


def test_scalar_str_root_is_hashable_and_set_compatible() -> None:
    """Hashing matches the wrapped value, so sets and dicts interoperate."""
    from adcp import PropertyTag

    tag = PropertyTag("sports")

    assert hash(tag) == hash("sports")
    assert {tag} & {"sports"} == {"sports"}
    assert {"sports": 1}[tag] == 1
    assert len({PropertyTag("sports"), "sports"}) == 1


def test_geo_include_exclude_overlap_is_detectable() -> None:
    """The production consequence from #1277: a set intersection over geo targeting.

    ``TargetingOverlay.geo_countries`` is ``list[GeoCountry]`` and detecting an
    include/exclude overlap is a set intersection. While ``GeoCountry`` was a
    ``RootModel``, that raised ``TypeError: unhashable type``, which escaped the
    typed validation path and turned a correctable ``INVALID_REQUEST`` into an
    ``INTERNAL_ERROR`` on a field the buyer controls.
    """
    from adcp.types import TargetingOverlay

    overlay = TargetingOverlay.model_validate(
        {"geo_countries": ["US", "CA"], "geo_countries_exclude": ["US"]}
    )
    assert overlay.geo_countries is not None
    assert overlay.geo_countries_exclude is not None

    overlap = set(overlay.geo_countries) & set(overlay.geo_countries_exclude)

    assert overlap == {"US"}


def test_scalar_int_and_float_roots_are_their_numbers() -> None:
    """Numeric roots arithmetic- and compare-equal to plain numbers."""
    from adcp.types import ViewThreshold
    from adcp.types import _generated as gen

    threshold = ViewThreshold(0.5)
    assert isinstance(threshold, float)
    assert threshold == 0.5
    assert threshold * 2 == 1.0

    version = gen.MajorVersion(3)
    assert isinstance(version, int)
    assert version == 3
    assert version + 1 == 4
    assert {version} & {3} == {3}


# ---------------------------------------------------------------------------
# Validation survives the rewrite
# ---------------------------------------------------------------------------


def test_construction_enforces_the_string_pattern() -> None:
    """A bad value is rejected at construction, not silently accepted.

    This is why the type is a ``str`` subclass and not an
    ``Annotated[str, StringConstraints(...)]`` alias: an annotated alias only
    enforces its constraints inside a Pydantic field, so ``PropertyTag
    ("Invalid-Tag")`` would succeed.
    """
    from adcp import PropertyTag

    with pytest.raises(ValidationError) as excinfo:
        PropertyTag("Invalid-Tag")

    assert excinfo.value.errors()[0]["type"] == "string_pattern_mismatch"


def test_construction_enforces_numeric_bounds() -> None:
    """``ge``/``le`` from the schema still reject out-of-range numbers."""
    from adcp.types import ViewThreshold
    from adcp.types import _generated as gen

    with pytest.raises(ValidationError):
        ViewThreshold(1.5)
    with pytest.raises(ValidationError):
        ViewThreshold(-0.1)
    with pytest.raises(ValidationError):
        gen.MajorVersion(0)


def test_construction_rejects_a_wrong_scalar_type() -> None:
    """A string root still refuses a number, as the RootModel wrapper did."""
    from adcp import PropertyTag

    with pytest.raises(ValidationError) as excinfo:
        PropertyTag(5)

    assert excinfo.value.errors()[0]["type"] == "string_type"


def test_missing_value_raises_type_error() -> None:
    """Constructing with no value is a programming error, not an empty string."""
    from adcp import PropertyTag

    with pytest.raises(TypeError, match="missing required argument"):
        PropertyTag()


# ---------------------------------------------------------------------------
# Behaviour inside a Pydantic model
# ---------------------------------------------------------------------------


def test_model_field_validates_coerces_and_round_trips() -> None:
    """A scalar root used as a model field validates, dumps, and reloads."""
    from adcp import PropertyTag

    class Holder(BaseModel):
        tag: PropertyTag
        tags: list[PropertyTag] = []

    holder = Holder.model_validate({"tag": "sports", "tags": ["news", "ctv"]})

    assert isinstance(holder.tag, PropertyTag)
    assert holder.tag == "sports"
    assert holder.tags == ["news", "ctv"]

    # The wire form is a bare string, both in Python and JSON mode.
    assert holder.model_dump() == {"tag": "sports", "tags": ["news", "ctv"]}
    assert type(holder.model_dump()["tag"]) is str
    assert json.loads(holder.model_dump_json()) == {"tag": "sports", "tags": ["news", "ctv"]}
    assert Holder.model_validate_json(holder.model_dump_json()) == holder

    with pytest.raises(ValidationError):
        Holder.model_validate({"tag": "Not A Tag"})


def test_model_field_preserves_instance_identity() -> None:
    """A typed value composes into a field without a round trip.

    ``RootModel`` defaulted to ``revalidate_instances="never"``, so composing an
    already-built value kept its identity. Adopters rely on that when they build
    a capability model out of public scalars — see
    ``tests/test_composability_invariant.py``.
    """
    from adcp import PropertyTag

    class Holder(BaseModel):
        tag: PropertyTag
        tags: list[PropertyTag] = []

    tag = PropertyTag("sports")
    holder = Holder(tag=tag, tags=[tag])

    assert holder.tag is tag
    assert holder.tags[0] is tag


def test_json_schema_matches_the_source_schema() -> None:
    """The emitted JSON Schema still carries the schema's own keywords."""
    from adcp import PropertyTag

    source = json.loads((_SCHEMA_DIR / "core" / "property-tag.json").read_text())
    generated = TypeAdapter(PropertyTag).json_schema()

    assert generated == {
        "type": "string",
        "pattern": source["pattern"],
        "title": source["title"],
        "description": source["description"],
        "examples": source["examples"],
    }


# ---------------------------------------------------------------------------
# Backwards compatibility for the RootModel call sites
# ---------------------------------------------------------------------------


def test_root_keyword_still_constructs_and_warns() -> None:
    """``PropertyTag(root=...)`` keeps working behind a DeprecationWarning."""
    from adcp import PropertyTag

    with pytest.warns(DeprecationWarning, match=r"PropertyTag\(root=\.\.\.\) is deprecated"):
        tag = PropertyTag(root="sports")

    assert tag == "sports"
    assert isinstance(tag, PropertyTag)

    with pytest.raises(ValidationError):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            PropertyTag(root="Invalid-Tag")


def test_root_attribute_still_reads_and_warns() -> None:
    """``instance.root`` keeps working and yields the plain scalar."""
    from adcp import PropertyTag

    tag = PropertyTag("sports")

    with pytest.warns(DeprecationWarning, match=r"PropertyTag\.root is deprecated"):
        value = tag.root

    assert value == "sports"
    assert type(value) is str


def test_positional_value_and_root_keyword_conflict() -> None:
    """Supplying both forms is a mistake worth naming."""
    from adcp import PropertyTag

    with pytest.raises(TypeError, match="not both"):
        PropertyTag("sports", root="news")


# ---------------------------------------------------------------------------
# The whole population, not just the sample above
# ---------------------------------------------------------------------------


def test_every_generated_scalar_root_is_its_python_scalar() -> None:
    """Sweep the exported surface: each scalar root behaves like its scalar."""
    roots = _generated_scalar_roots()
    assert len(roots) > 100, f"expected the full scalar population, found {len(roots)}"

    failures: list[str] = []
    for name, cls in sorted(roots.items()):
        if not issubclass(cls, cls._scalar_type):
            failures.append(f"{name}: not a {cls._scalar_type.__name__} subclass")
        if issubclass(cls, BaseModel):
            failures.append(f"{name}: still a Pydantic model")
        unknown = set(cls._constraints) - _ALLOWED_CONSTRAINTS
        if unknown:
            failures.append(f"{name}: unknown constraint keyword(s) {sorted(unknown)}")
        unknown = set(cls._json_schema_extra) - _ALLOWED_DOCUMENTATION
        if unknown:
            failures.append(f"{name}: unknown documentation keyword(s) {sorted(unknown)}")

    assert failures == [], "\n".join(failures)


def test_every_scalar_root_example_round_trips() -> None:
    """Where the schema supplies examples, each one constructs and compares equal.

    Exercises the real constraints of every scalar root that documents a value,
    so a mis-translated ``pattern`` or bound shows up as a rejected example.
    """
    failures: list[str] = []
    for name, cls in sorted(_generated_scalar_roots().items()):
        for example in cls._json_schema_extra.get("examples", ()):
            if not isinstance(example, cls._scalar_type):
                continue  # schema example of another type; the field tests cover those
            try:
                built = cls(example)
            except ValidationError as exc:
                failures.append(f"{name}({example!r}) rejected its own example: {exc}")
                continue
            if built != example or cls._scalar_type(built) != example:
                failures.append(f"{name}({example!r}) != {example!r}")

    assert failures == [], "\n".join(failures)


def test_scalar_root_takes_no_arbitrary_attributes() -> None:
    """``__slots__`` keeps a schema value closed, as the RootModel wrapper was.

    Without ``__slots__`` on the generated subclass it regains a ``__dict__``
    and silently accepts any attribute.
    """
    from adcp import PropertyTag

    tag = PropertyTag("sports")

    with pytest.raises(AttributeError):
        tag.workflow_id = "wf-42"


def test_scalar_root_survives_copy_and_pickle() -> None:
    """The classic str-subclass hazards: both reconstruct through ``__new__``."""
    import copy
    import pickle

    from adcp import PropertyTag

    tag = PropertyTag("sports")

    assert copy.deepcopy(tag) == tag
    restored = pickle.loads(pickle.dumps(tag))
    assert restored == tag
    assert type(restored) is PropertyTag


def test_scalar_root_repr_names_its_type() -> None:
    """``repr`` stays useful in logs: the type name plus the value."""
    from adcp import PropertyTag

    assert repr(PropertyTag("sports")) == "PropertyTag('sports')"
