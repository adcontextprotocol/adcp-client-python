"""A ``Legacy*`` name is the generated class, and there is one per response branch.

Two defects, one cause: the ``Legacy*`` set was curated by hand, so what it
named drifted from what the generator emits.

#1398 — ``adcp.types.LegacyFormatId`` was a SUBCLASS of the generated
``FormatReferenceStructuredObject``, and no public name on ``adcp.types``
resolved to the generated class at all. The subclass redeclared four fields and
every redeclaration had become a weakening of the parent it was written to
narrow: ``width: 300.0`` validated against the class the SDK's own model fields
declare and was refused by the public name for it. A public name more permissive
(or less) than the library's own contract fails late and for no visible reason.

#1399 — ``LegacyUpdateMediaBuy{Success,Error}Response`` shipped while
``LegacyUpdateMediaBuySubmittedResponse`` and all three
``LegacyCreateMediaBuy*Response`` branches did not, so four response branches could only
be named through ``adcp.types.domains.media_buy.*_response`` — the private tree
this release exists to retire. Adding four names would have fixed four
instances: the generator had ALREADY opened a fifth, by adding a fourth
``preview_creative`` branch that nobody named.

So neither test reads a list. Both derive what the surface must carry from the
generated tree:

* the branches of a response union come from ``typing.get_args`` of the union the
  generated module itself declares — not from a name pattern, and not from
  ``__module__`` (which names the canonical spec even for a duplicate, and so
  cannot tell a copy from the original);
* a response is "on the ``Legacy*`` surface" when its tool has any ``Legacy*``
  name at all, which is what an adopter migrating through those names has;
* and the obligation is stated as object identity against ``adcp.types``, which
  is the only thing an adopter's ``isinstance`` cares about.
"""

from __future__ import annotations

import importlib
import pathlib
import typing

import pytest
from pydantic import BaseModel

import adcp.types as types

_DOMAINS_DIR = pathlib.Path(importlib.import_module("adcp.types.domains").__file__).parent
_CANONICAL_ROOT = "adcp.types.domains"

LEGACY_NAMES = sorted(name for name in types.__all__ if name.startswith("Legacy"))
PLAIN_NAMES = sorted(name for name in types.__all__ if not name.startswith("Legacy"))

#: The ``Legacy*`` names bound to a model class. The rest are union aliases
#: (``LegacyBuildCreativeResponse``, ``LegacyProductFormatDeclaration``, ...),
#: which have no field set to weaken — filtered out rather than skipped, so a
#: class that stops being one is a parametrization change and not a quiet skip.
LEGACY_CLASSES = sorted(
    name
    for name in LEGACY_NAMES
    if isinstance(getattr(types, name, None), type) and issubclass(getattr(types, name), BaseModel)
)


def _camel(stem: str) -> str:
    return "".join(part.title() for part in stem.split("_"))


def _response_branches() -> list[tuple[str, str, type[BaseModel]]]:
    """Every branch of every response union whose tool is on the ``Legacy*`` surface.

    ``(tool, branch name, branch class)``. The branches come from the generated module's
    own ``<Tool>Response`` union, so a schema that gains or loses an branch changes
    this list with no edit here. A response that is one class rather than a
    union contributes that class as its only branch.
    """

    branches: list[tuple[str, str, type[BaseModel]]] = []
    for path in sorted(_DOMAINS_DIR.rglob("*_response.py")):
        relative = path.relative_to(_DOMAINS_DIR)
        if relative.parts[0] == "bundled":
            # Compiled transport bundles inline a per-message copy of the source
            # models. They are not the tool's response.
            continue
        tool = _camel(relative.stem.removesuffix("_response"))
        if not any(name.startswith(f"Legacy{tool}") for name in LEGACY_NAMES):
            continue
        module = importlib.import_module(
            ".".join([_CANONICAL_ROOT, *relative.parts[:-1], relative.stem])
        )
        union = getattr(module, f"{tool}Response", None)
        if union is None:
            continue
        members = typing.get_args(union) or (union,)
        for member in members:
            if isinstance(member, type) and issubclass(member, BaseModel):
                branches.append((tool, member.__name__, member))
    return branches


BRANCHES = _response_branches()
BRANCH_IDS = [f"{tool}:{name}" for tool, name, _ in BRANCHES]


def test_the_derived_sets_are_not_empty() -> None:
    """Neither obligation below may go quiet when the tree is reorganized.

    A derivation that silently resolves to nothing is worse than a hand-written
    list, because a list at least fails loudly when it stops matching.
    """
    assert len(LEGACY_NAMES) > 40, LEGACY_NAMES
    assert len(LEGACY_CLASSES) > 35, LEGACY_CLASSES
    assert len(BRANCHES) > 12, BRANCH_IDS
    assert {tool for tool, _, _ in BRANCHES} >= {
        "BuildCreative",
        "CreateMediaBuy",
        "PreviewCreative",
        "UpdateMediaBuy",
    }, sorted({tool for tool, _, _ in BRANCHES})


# ---------------------------------------------------------------------------
# #1398 — a Legacy name is the generated class, never a shim over it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", LEGACY_CLASSES)
def test_a_legacy_name_is_the_generated_class_not_a_subclass_of_it(name: str) -> None:
    """``Legacy<X>`` means "the class codegen emitted", with nothing in between.

    The load-bearing assertion is the ROUND TRIP: the class must be the
    attribute the module it claims actually binds under its own qualname.
    Neither half of it is sufficient alone, and a fire probe proved it —

    * ``__module__.startswith(domains)`` alone is defeated by one line
      (``Clone.__module__ = "adcp.types.domains.core.format_id"``), and
      ``__module__`` is a weak witness by construction here: ``module_from_spec``
      stamps the canonical spec's name onto a duplicate too, so it cannot tell a
      copy from the original;
    * the MRO check alone misses the shape this repo actually shipped. #1398's
      clone was ``class LegacyFormatId(FormatReferenceStructuredObject)`` — a
      different class NAME, so a "shadows a same-named base" rule reports
      nothing. That rule is kept below for the subclass that does reuse the
      name, but it is not what catches a renamed shim.

    Together they do: a shim declared anywhere but the generated tree fails the
    first, and a shim that lies about where it was declared fails the second,
    because the module it names binds no such attribute.

    A ``Legacy*`` name bound to a union alias rather than a class is out of the
    parametrization: there is no field set to weaken.
    """
    value = getattr(types, name)

    assert value.__module__.startswith(_CANONICAL_ROOT), (
        f"adcp.types.{name} is {value.__module__}.{value.__qualname__}, declared "
        f"outside the generated tree. A Legacy name is the generated class "
        f"itself, so a class in between reshapes the contract the SDK's own "
        f"model fields carry (#1398)"
    )
    declaring = importlib.import_module(value.__module__)
    assert getattr(declaring, value.__qualname__, None) is value, (
        f"adcp.types.{name} reports {value.__module__} but that module binds no "
        f"{value.__qualname__} — it is a local class claiming a generated address"
    )

    shadowed = [
        base
        for base in value.__mro__[1:]
        if isinstance(base, type)
        and issubclass(base, BaseModel)
        and base.__name__ == value.__name__
    ]
    assert shadowed == [], f"adcp.types.{name} shadows the same-named {shadowed[0]!r}"


def test_the_format_reference_is_reachable_under_a_public_name() -> None:
    """#1398's first half, on the one class it was measured on.

    ``[n for n in adcp.types.__all__ if getattr(adcp.types, n) is
    FormatReferenceStructuredObject]`` was empty: generated model fields are
    annotated with this class, and nothing on the flat surface named it. Both
    ``Legacy*`` spellings now do.
    """
    generated = importlib.import_module(f"{_CANONICAL_ROOT}.core.format_id")
    reference = generated.FormatReferenceStructuredObject

    public = [name for name in types.__all__ if getattr(types, name, None) is reference]
    assert public == [
        "LegacyFormatId",
        "LegacyFormatReferenceStructuredObject",
    ], public


def test_the_format_reference_accepts_what_the_sdk_s_own_model_fields_accept() -> None:
    """The measured consequence of the dropped validators, stated as behaviour.

    ``width``/``height`` are ``SchemaInt`` on the generated class, which carries
    a ``BeforeValidator`` for the JSON-schema spelling of an integer. The
    subclass declared ``StrictInt`` and refused ``300.0`` — a payload every
    model that declares the real type accepts.
    """
    wire = {"agent_url": "https://creative.adcontextprotocol.org", "id": "display_300x250"}

    reference = types.LegacyFormatId.model_validate({**wire, "width": 300.0, "height": 250.0})
    assert (reference.width, reference.height) == (300, 250)

    assert reference.model_json_schema()["properties"]["agent_url"]["format"] == "uri"


# ---------------------------------------------------------------------------
# #1399 — one Legacy name per branch, derived from the generated unions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tool", "branch_name", "branch"), BRANCHES, ids=BRANCH_IDS)
def test_every_branch_of_a_response_on_the_legacy_surface_is_publicly_nameable(
    tool: str, branch_name: str, branch: type[BaseModel]
) -> None:
    """The generated branch itself, not only the canonical subclass that shadows it.

    A seller reading a response off the wire holds an instance of the GENERATED
    branch. ``adcp.types.CreateMediaBuySuccessResponse`` is the canonical subclass,
    so an ``isinstance`` against it is False for that instance and the only
    public spelling for the thing in hand was a ``domains`` path. The
    ``Legacy*`` name is how the flat surface reaches it, and every branch of a
    response whose tool is on that surface needs one.
    """
    direct = [name for name in PLAIN_NAMES if getattr(types, name, None) is branch]
    through_legacy = [name for name in LEGACY_NAMES if getattr(types, name, None) is branch]

    assert direct or through_legacy, (
        f"{branch.__module__}.{branch_name} is a branch of {tool}Response and no public "
        f"name on adcp.types resolves to it, so it can only be named through "
        f"adcp.types.domains — which is the tree 9.0 retires. Add "
        f"Legacy{tool}<Branch>Response in src/adcp/types/legacy.py (#1399)"
    )
