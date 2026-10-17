"""Every asset type the bundled schema declares must be ROUTABLE by the models.

WHY THE OBVIOUS COMPARISON IS THE WRONG ONE. The first version of this file
compared the bundled schema's ``asset_type`` consts against the generated
classes' ``asset_type`` defaults. That comparison passes on this tree, and it
would have passed at EVERY upstream release point including the broken ones,
because the generated classes always covered the vocabulary. The generated
artifact — ``adcp/types/domains/core/format.py`` — reads 42 ``asset_type:
Literal`` declarations (21 distinct, two arms) at v9.0.0-beta.1 through the
beta.4 release commit. Codegen was never the defect.

THE DEFECT IS A THIRD SIDE: WHAT THE MODELS ACTUALLY ROUTE. ``types/aliases.py``
carries two HAND-WRITTEN unions, and ``types/_forward_compat.py`` patches them
over the correct generated annotations at import. Upstream those unions carry 14
real arms individually and 12 in-group against the schema's 21, so seven
declared asset types (``card``, ``daast_tracker``, ``display_tag``,
``pixel_tracker``, ``published_post``, ``vast_tracker``, ``zip``) and nine
in-group fall through to the unknown arm. A schema-valid document deserializes as
``UnknownFormatAsset``.

SO THE ASSERTION IS: schema vocabulary == what ``Format.assets`` routes AFTER
forward-compat has been applied. Not what the generated classes declare, which is
correct and always was.

THE PATCH IS APPLIED BY A MODULE BODY, AND WHO TRIGGERS IT IS NOT FULLY PINNED.
``adcp.types._forward_compat`` calls ``_apply_forward_compat()`` when imported, so
this module imports it explicitly to guarantee the patched state is the one
graded. What is NOT established is that the explicit import is the only route:
measured on the 9.0.0b2 wheel, the walk reads 14 routable with or without it, so
something in the import chain already applies it there. The explicit import is
therefore a guarantee rather than the mechanism, and a reader should not take it
as evidence about which import arms the patch.

FIRE-PROBED IN BOTH DIRECTIONS ON REAL ARTIFACTS, not on a mutation. On this
fork it passes — schema 21, individual 21, group 21 — because ``0b8b194ab``
generates both unions and derives the vocabulary they route by. Run against the
``9.0.0b2+prs1435.1415.1447.1456`` wheel it FAILS, reading 14 individually and 12
in-group and naming exactly ``card``, ``daast_tracker``, ``display_tag``,
``pixel_tracker``, ``published_post``, ``vast_tracker``, ``zip`` (plus ``brief``
and ``catalog`` in-group).

That pair is what the first version of this file could not produce. Its probe
renamed a const in the bundled schema and watched the comparison go red, which
proves the comparison WORKS and says nothing about whether it is pointed at the
defect — the first version passed on the broken wheel too.

``routed_by`` is taken from the measurement in acp-branch-runs/repro, which is
where the walk was worked out. Its schema reader is not: that one resolves
``adcp.__file__ / "_schemas"``, which exists only in an installed wheel, so it
cannot run from an editable checkout. The reader here goes through the SDK's own
``_resolve_schema_root``, which prefers the packaged copy and falls back to the
repo layout — the same bytes in either case.

THE SECOND DIRECTION, AND WHY THE ARM COMPARISON ABOVE IS NOT ENOUGH. Everything
described so far compares SETS: the schema's consts against the arms the union
carries. That caught the real defect, and the b2-wheel run above proves it. It is
still blind in one direction, because a callable ``Discriminator`` chooses the arm
and the vocabulary it chooses by need not be the arm set. Upstream holds that
vocabulary in two further hand-maintained frozensets, so a fix replacing only the
unions there changes routing not at all. This fork derives both from the same
emitted branches, which is why it passes on both counts — but nothing was
GRADING the second count.

So ``_parses_to`` asks pydantic to deserialize one schema-valid document per
declared type and reports the class it produced. Fire-probed as a pair, on this
tree, by deleting ``'pixel_tracker'`` from ``_KNOWN_BASEINDIVIDUALASSET_TYPES`` in
the generated ``core/format.py`` and leaving all 21 arms intact:

    test_every_declared_asset_type_is_routable[individual]            PASSED
    test_every_declared_asset_type_parses_to_a_typed_class[individual] FAILED
                                                        -> ['pixel_tracker']

That pair is the justification for the second direction existing: the arm
comparison cannot see a vocabulary held outside the arms, and this can.

The control is load-bearing and is its own test. An instrument that refuses every
document reports "nothing downgraded" exactly as cleanly as one where everything
routes, and the first two drafts of ``_DOCUMENTS`` here did refuse all 21 rows,
control included. Removing the required ``asset_id`` from the document reproduces
that state, and both the control and the parse assertion go red naming it, rather
than one of them passing quietly.

Refs: salesagent-8qela.168, salesagent-raqvy
"""

from __future__ import annotations

import json
import typing
from typing import Any

import pytest

# THE IMPORT THAT ARMS THE PATCH. Not a convenience and not reorderable: without
# it the generated annotations measure a correct 21/21 and this file grades
# nothing. Imported for its module-body side effect, hence the noqa.
import adcp.types._forward_compat  # noqa: F401
from adcp.types.aliases import RepeatableAssetGroup
from adcp.types.domains.core.format import Format
from adcp.validation.schema_loader import _resolve_schema_root


def _declared_by_schema(
    doc: Any, found: set[str] | None = None, key: str | None = None
) -> set[str]:
    """Every ``asset_type`` ``const`` anywhere under ``doc``.

    Whole-subtree rather than a fixed path: ``core/format.json`` discriminates on
    ``item_type`` first and ``asset_type`` second, and the two arms nest to
    DIFFERENT depths, so a fixed-depth walk derives the full set for one arm and
    the empty set for the other.
    """
    found = set() if found is None else found
    if isinstance(doc, dict):
        if key == "asset_type" and isinstance(doc.get("const"), str):
            found.add(doc["const"])
        for inner_key, value in doc.items():
            _declared_by_schema(value, found, inner_key)
    elif isinstance(doc, list):
        for value in doc:
            _declared_by_schema(value, found, key)
    return found


def _routed_by(model: type, field: str = "assets") -> set[str]:
    """Every ``asset_type`` the union on ``model.<field>`` carries an arm for.

    Walks the annotation rather than the class list, because the annotation is
    what forward-compat replaces. ``Annotated`` is unwrapped to its first
    argument so a discriminated union carrying field metadata is followed rather
    than treated as a leaf.

    THIS IS THE NECESSARY HALF, NOT THE SUFFICIENT ONE, and an earlier version of
    this docstring claimed otherwise — that the annotation is "what pydantic
    dispatches on". With a CALLABLE ``Discriminator`` pydantic calls the
    discriminator first and selects an arm by the tag it returns, so a vocabulary
    held somewhere other than the arms can route a declared type to the fallback
    while every arm for it exists. Upstream holds exactly that: two further
    hand-maintained frozensets feed the callable discriminator, which is why
    replacing only the unions there changes routing not at all. An arm set alone
    cannot see that, so ``_parses_to`` below asks a document instead.
    """
    out: set[str] = set()
    stack = [model.model_fields[field].annotation]
    while stack:
        node = stack.pop()
        args = typing.get_args(node)
        if typing.get_origin(node) is typing.Annotated:
            stack.append(args[0])
            continue
        if args:
            stack.extend(args)
            continue
        declared = getattr(node, "model_fields", {}).get("asset_type")
        if declared is not None and isinstance(declared.default, str):
            out.add(declared.default)
    return out


@pytest.fixture(scope="module")
def declared() -> set[str]:
    """The schema's vocabulary, from the bundle the wheel ships."""
    root = _resolve_schema_root()
    assert root is not None, "no bundled schema tree resolved for the SDK's pinned version"
    path = root.core / "format.json"
    if not path.exists():
        path = root.bundled / "core" / "format.json"
    assert path.exists(), f"bundled core/format.json not found under {root.root}"
    return _declared_by_schema(json.loads(path.read_text()))


def test_the_schema_declares_asset_types(declared: set[str]) -> None:
    """The authority side is non-empty, so the comparisons below grade something.

    An empty authority compares cleanly against any subject, including an empty
    one, so this is the assertion that makes the others mean anything.
    """
    assert declared, "the bundled schema declared no asset_type const"


@pytest.mark.parametrize(
    ("label", "model"),
    [("Format.assets", Format), ("RepeatableAssetGroup.assets", RepeatableAssetGroup)],
    ids=["individual", "repeatable_group"],
)
def test_the_models_route_asset_types(label: str, model: type) -> None:
    """Each subject side is non-empty too, for the same reason in reverse."""
    assert _routed_by(model), f"{label} routes no asset_type; the walk found nothing to compare"


@pytest.mark.parametrize(
    ("label", "model"),
    [("Format.assets", Format), ("RepeatableAssetGroup.assets", RepeatableAssetGroup)],
    ids=["individual", "repeatable_group"],
)
def test_every_declared_asset_type_is_routable(declared: set[str], label: str, model: type) -> None:
    """The assertion the shipped wheel could not make about itself.

    One direction: the schema is the authority, so a type it declares MUST be
    routable. A routable type with no const would be a different question and is
    not asserted here.
    """
    unroutable = sorted(declared - _routed_by(model))
    assert not unroutable, (
        f"{len(unroutable)} of {len(declared)} asset types the bundled "
        f"core/format.json declares cannot be routed by {label}, so a schema-valid "
        f"document deserializes them as UnknownFormatAsset: {unroutable}"
    )


#: Minimal schema-valid documents, one per routing path, with the ``asset_type``
#: left to the caller. Both shapes are lifted from the passing cases in
#: ``test_forward_compat_assets.py`` rather than invented: a document missing
#: ``item_type``, or carrying ``asset_id`` where a group wants ``asset_group_id``,
#: is REFUSED, and a refusal on every row reads as "nothing downgraded".
_DOCUMENTS: dict[str, typing.Callable[[str], tuple[type, dict[str, Any]]]] = {
    "Format.assets": lambda asset_type: (
        Format,
        {
            "format_id": {"agent_url": "https://test.example.com", "id": "parses_to"},
            "name": "parses_to",
            "assets": [
                {
                    "asset_id": "slot",
                    "asset_type": asset_type,
                    "item_type": "individual",
                    "required": True,
                }
            ],
        },
    ),
    "RepeatableAssetGroup.assets": lambda asset_type: (
        RepeatableAssetGroup,
        {
            "asset_group_id": "slide",
            "item_type": "repeatable_group",
            "required": True,
            "min_count": 1,
            "max_count": 5,
            "assets": [{"asset_id": "slot", "asset_type": asset_type, "required": True}],
        },
    ),
}


def _parses_to(label: str, asset_type: str) -> str:
    """The class name a schema-valid document for ``asset_type`` lands in.

    A refusal is returned as a value rather than raised, because "this document
    was rejected" and "this type was downgraded" are different answers and the
    control below has to be able to tell them apart.
    """
    model, document = _DOCUMENTS[label](asset_type)
    try:
        parsed = model.model_validate(document)
    except Exception as error:  # noqa: BLE001 — a refusal is a result here
        return f"<refused: {type(error).__name__}>"
    assets = parsed.assets
    assert assets, f"{label}: the parsed model carries no assets to inspect"
    return type(assets[0]).__name__


@pytest.mark.parametrize("label", sorted(_DOCUMENTS), ids=["individual", "repeatable_group"])
def test_an_undeclared_asset_type_reaches_the_fallback(label: str) -> None:
    """The control, and it must run before any row of the next test is believed.

    An instrument that REFUSES every document reports "nothing downgraded" just
    as cleanly as one where everything routes, so the next test's verdict is only
    readable once this one has shown the fallback being reached on a type the
    schema does not declare. Both of the first two drafts of ``_DOCUMENTS``
    refused all 21 rows, control included.
    """
    landed = _parses_to(label, "x_future_asset_type_no_schema_declares")
    assert "Unknown" in landed, (
        f"{label}: an undeclared asset_type landed in {landed!r} rather than the "
        "Unknown fallback. The document shape is wrong, so this file is measuring "
        "acceptance and not routing — fix it before reading the parse assertions."
    )


@pytest.mark.parametrize("label", sorted(_DOCUMENTS), ids=["individual", "repeatable_group"])
def test_every_declared_asset_type_parses_to_a_typed_class(declared: set[str], label: str) -> None:
    """The sufficient half: a real document reaches its class, not just an arm.

    ``test_every_declared_asset_type_is_routable`` compares arm sets, which is
    blind to a vocabulary held outside the arms — see ``_routed_by``. This asks
    pydantic to deserialize one document per declared type and reports the class
    it actually produced, so a discriminator that answers ``_unknown`` ahead of a
    present arm is a failure here and invisible there.
    """
    downgraded = sorted(a for a in declared if "Unknown" in _parses_to(label, a))
    refused = sorted(a for a in declared if "refused" in _parses_to(label, a))
    assert not refused, (
        f"{label}: {len(refused)} of {len(declared)} declared asset types produced a "
        f"schema-INVALID document, so they were graded on nothing: {refused}"
    )
    assert not downgraded, (
        f"{label}: {len(downgraded)} of {len(declared)} asset types the bundled "
        f"core/format.json declares deserialize to the Unknown fallback even though "
        f"the union carries an arm for them, which means the routing vocabulary and "
        f"the arm set disagree: {downgraded}"
    )
