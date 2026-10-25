"""Differential guard: the asset fallbacks change nothing 8.x already accepts.

The open asset unions are patched in at import time, and three validation
paths see them differently (see ``_apply_asset_fallbacks`` in
``adcp.types._forward_compat``). This module parses a corpus through every path
and compares the result with a baseline recorded on ``release/8.x`` before the
fallbacks were added (``tests/fixtures/forward_compat_asset_baseline.json``):

- a document that parsed there must parse to the same classes, field sets,
  extras, ``model_dump()`` and ``model_dump_json()``;
- a document that failed there must fail with the same errors (location and
  type), unless it names an asset_type the generated unions have no arm for,
  in which case it must now parse;
- each path must advertise the asset types it advertised there, and no
  fallback that 8.x did not already advertise.

Everything recorded is the SDK's own: class names, field sets, extras, plain
values and serialized output. Pydantic's message text, URL reprs and generated
schema text are left out, so a Pydantic release cannot change the baseline.

The corpus covers every generated individual and group asset type, at top level
and inside a repeatable group, plus the AAO reference formats.

To re-record the baseline, run this module as a script against the
``release/8.x`` source tree (only the asset-fallback patch may differ)::

    PYTHONPATH=<release-8.x checkout>/src python -m tests.test_forward_compat_assets_baseline \\
        --record tests/fixtures/forward_compat_asset_baseline.json
"""

from __future__ import annotations

import hashlib
import json
import sys
import warnings
from collections.abc import Callable, Iterator
from enum import Enum
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

_ROOT = Path(__file__).parent.parent
_BASELINE = _ROOT / "tests/fixtures/forward_compat_asset_baseline.json"
_REFERENCE_FORMATS = (
    _ROOT / "tests/fixtures/aao-reference-formats.json",
    _ROOT
    / "tests/fixtures/canonical/typescript-13.0.0-rc.3/test/lib/v2-projection-fixtures"
    / "aao-reference-formats.json",
)

#: An asset_type no generated class declares; asserted below.
_UNDECLARED = "x_future_asset_type"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=repr).encode("utf-8")
    ).hexdigest()[:20]


def _plain(value: Any) -> Any:
    """A JSON value with no Pydantic- or Python-version-specific reprs."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return {
            "enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": value.value,
        }
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    # URLs and other scalar objects Pydantic owns: their value, not their repr.
    return {"object": str(value)}


def _tree(value: Any) -> Any:
    """Classes, field sets and extras of a parsed value, recursively."""
    if isinstance(value, BaseModel):
        node: dict[str, Any] = {
            "__class__": f"{type(value).__module__}.{type(value).__qualname__}",
            "__fields_set__": sorted(value.model_fields_set),
            "__extra__": sorted((value.__pydantic_extra__ or {}).keys()),
        }
        with warnings.catch_warnings():
            # Some generated fields are deprecated; reading them warns.
            warnings.simplefilter("ignore", DeprecationWarning)
            fields = {name: getattr(value, name) for name in type(value).model_fields}
        for name, field in fields.items():
            node[name] = _tree(field)
        for name, extra in (value.__pydantic_extra__ or {}).items():
            node[f"extra:{name}"] = _tree(extra)
        return node
    if isinstance(value, (list, tuple)):
        return [_tree(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _tree(item) for key, item in value.items()}
    return _plain(value)


def _observe(parse: Callable[[], Any], dump: Callable[[Any], tuple[Any, Any]]) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            parsed = parse()
    except ValidationError as exc:
        errors = [[list(err["loc"]), err["type"]] for err in exc.errors()]
        return f"err:{_digest(errors)}"
    try:
        with warnings.catch_warnings():
            # A serializer that meets a value it was not built for warns; that
            # is a difference too.
            warnings.simplefilter("error")
            python, as_json = dump(parsed)
    except Warning:
        return f"warn:{_digest(_tree(parsed))}"
    return f"ok:{_digest([_tree(parsed), python, as_json])}"


def _model_dump(value: Any) -> tuple[Any, Any]:
    return _plain(value.model_dump()), json.loads(value.model_dump_json())


def _adapter_dump(adapter: TypeAdapter[Any]) -> Callable[[Any], tuple[Any, Any]]:
    def dump(value: Any) -> tuple[Any, Any]:
        return _plain(adapter.dump_python(value)), json.loads(adapter.dump_json(value))

    return dump


def _generated_asset_types() -> tuple[list[str], list[str]]:
    from adcp.types.generated_poc.core import format as format_module

    def declared(base: type) -> list[str]:
        return sorted(
            {
                value.model_fields["asset_type"].default
                for value in vars(format_module).values()
                if isinstance(value, type)
                and issubclass(value, base)
                and value is not base
                and isinstance(value.model_fields["asset_type"].default, str)
            }
        )

    individual = declared(format_module.BaseIndividualAsset)
    group = declared(format_module.BaseGroupAsset)
    assert individual and group
    assert _UNDECLARED not in individual and _UNDECLARED not in group
    return individual, group


def _individual(asset_type: Any, **fields: Any) -> dict[str, Any]:
    return {
        "asset_id": f"slot_{asset_type}",
        "asset_type": asset_type,
        "item_type": "individual",
        "required": True,
        **fields,
    }


def _group_item(asset_type: Any, **fields: Any) -> dict[str, Any]:
    return {"asset_id": f"item_{asset_type}", "asset_type": asset_type, "required": False, **fields}


def _group(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_type": "repeatable_group",
        "asset_group_id": "card",
        "required": True,
        "min_count": 1,
        "max_count": 4,
        "assets": list(items),
    }


def _format(name: str, *assets: dict[str, Any]) -> dict[str, Any]:
    format_id = "".join(char if char.isalnum() or char in "_-" else "_" for char in name)
    return {
        "format_id": {"agent_url": "https://creative.example.com", "id": format_id},
        "name": name,
        "assets": list(assets),
    }


_EXTRA = {"x_vendor_hint": {"weight": 2}}


def _corpus() -> tuple[
    dict[str, tuple[dict[str, Any], bool]],
    dict[str, tuple[dict[str, Any], bool]],
    dict[str, tuple[dict[str, Any], bool]],
]:
    """(individual assets, group items, formats), each id -> (document, undeclared)."""
    individual_types, group_types = _generated_asset_types()
    individuals: dict[str, tuple[dict[str, Any], bool]] = {}
    for asset_type in [*individual_types, _UNDECLARED]:
        undeclared = asset_type == _UNDECLARED
        individuals[f"{asset_type}"] = (_individual(asset_type), undeclared)
        individuals[f"{asset_type}+extra"] = (_individual(asset_type, **_EXTRA), undeclared)
    # Malformed documents must keep failing the way they did.
    individuals["image+bad_required"] = (_individual("image", required="nope"), False)
    individuals["pixel_tracker+bad_required"] = (
        _individual("pixel_tracker", required="nope"),
        False,
    )
    individuals["non_string_type"] = (_individual(7), False)
    individuals["missing_type"] = (
        {"asset_id": "x", "item_type": "individual", "required": True},
        False,
    )
    individuals["undeclared+missing_id"] = (
        {"asset_type": _UNDECLARED, "item_type": "individual", "required": True},
        True,
    )
    individuals["undeclared+bad_item_type"] = (
        _individual(_UNDECLARED, item_type="something_else"),
        False,
    )

    items: dict[str, tuple[dict[str, Any], bool]] = {}
    for asset_type in [*group_types, _UNDECLARED]:
        undeclared = asset_type == _UNDECLARED
        items[f"{asset_type}"] = (_group_item(asset_type), undeclared)
        items[f"{asset_type}+extra"] = (_group_item(asset_type, **_EXTRA), undeclared)
    items["image+bad_required"] = (_group_item("image", required="nope"), False)
    items["non_string_type"] = (_group_item(7), False)
    items["undeclared+missing_id"] = ({"asset_type": _UNDECLARED, "required": True}, True)

    formats: dict[str, tuple[dict[str, Any], bool]] = {}
    for key, (doc, undeclared) in individuals.items():
        formats[f"individual:{key}"] = (_format(key, doc), undeclared)
    for key, (doc, undeclared) in items.items():
        formats[f"group:{key}"] = (_format(key, _group(doc)), undeclared)
    formats["mixed:all_declared"] = (
        _format(
            "all_declared",
            *(_individual(t) for t in individual_types),
            _group(*(_group_item(t) for t in group_types)),
        ),
        False,
    )
    formats["mixed:with_undeclared"] = (
        _format(
            "with_undeclared",
            *(_individual(t) for t in individual_types),
            _individual(_UNDECLARED, **_EXTRA),
            _group(*(_group_item(t) for t in group_types), _group_item(_UNDECLARED, **_EXTRA)),
        ),
        True,
    )
    for path in _REFERENCE_FORMATS:
        for index, doc in enumerate(json.loads(path.read_text())):
            formats[f"reference:{path.parent.name}:{index}"] = (doc, False)
    return individuals, items, formats


def _cases() -> Iterator[tuple[str, Callable[[], str], bool]]:
    """(case id, observe, undeclared) for every path and document."""
    from adcp.types import (
        FormatAssetUnion,
        GroupFormatAssetUnion,
        LegacyFormat,
        LegacyListCreativeFormatsResponse,
        RepeatableAssetGroup,
    )
    from adcp.types.generated_poc.core.format import Format
    from adcp.types.generated_poc.creative.list_creative_formats_response import (
        ListCreativeFormatsResponseCreativeAgent,
    )

    assert LegacyFormat is Format
    individuals, items, formats = _corpus()
    format_union = TypeAdapter(FormatAssetUnion)
    group_union = TypeAdapter(GroupFormatAssetUnion)

    def model_paths(model: type[BaseModel], wrap: Callable[[Any], Any]) -> list[tuple[str, Any]]:
        return [
            ("python", lambda doc: model.model_validate(wrap(doc))),
            ("json", lambda doc: model.model_validate_json(json.dumps(wrap(doc)))),
        ]

    paths: list[tuple[str, dict[str, tuple[dict[str, Any], bool]], Any, Any]] = []
    for mode, parse in model_paths(Format, lambda doc: doc):
        paths.append((f"Format.{mode}", formats, parse, _model_dump))
    for mode, parse in model_paths(
        LegacyListCreativeFormatsResponse, lambda doc: {"formats": [doc]}
    ):
        paths.append((f"ListCreativeFormatsResponse.{mode}", formats, parse, _model_dump))
    for mode, parse in model_paths(
        ListCreativeFormatsResponseCreativeAgent, lambda doc: {"formats": [doc]}
    ):
        paths.append(
            (f"ListCreativeFormatsResponseCreativeAgent.{mode}", formats, parse, _model_dump)
        )
    for mode, parse in model_paths(RepeatableAssetGroup, _group):
        paths.append((f"RepeatableAssetGroup.{mode}", items, parse, _model_dump))
    paths.append(
        ("FormatAssetUnion", individuals, format_union.validate_python, _adapter_dump(format_union))
    )
    paths.append(
        ("GroupFormatAssetUnion", items, group_union.validate_python, _adapter_dump(group_union))
    )

    for path_name, documents, parse, dump in paths:
        for key, (doc, undeclared) in documents.items():

            def observe(parse: Any = parse, doc: Any = doc, dump: Any = dump) -> str:
                return _observe(lambda: parse(doc), dump)

            yield f"{path_name}|{key}", observe, undeclared


def observe_all() -> dict[str, str]:
    return {case: observe() for case, observe, _ in _cases()}


#: Undeclared documents that are invalid for another reason too. They must
#: still fail, though no longer necessarily with the closed union's errors.
_INVALID_ANYWAY = "undeclared+missing_id"


@pytest.fixture(scope="module")
def observed() -> dict[str, str]:
    return observe_all()


@pytest.fixture(scope="module")
def baseline() -> dict[str, str]:
    recorded: dict[str, str] = json.loads(_BASELINE.read_text())
    return recorded


def test_corpus_matches_the_baseline(observed: dict[str, str], baseline: dict[str, str]) -> None:
    assert set(observed) == set(baseline), "the corpus changed; re-record the baseline"


def test_nothing_8x_decided_changes(observed: dict[str, str], baseline: dict[str, str]) -> None:
    """Results and errors are identical, except for undeclared assets."""
    undeclared = {case for case, _, flag in _cases() if flag}
    changed = sorted(case for case in baseline if observed.get(case) != baseline[case])
    unexpected = [case for case in changed if case not in undeclared]
    assert not unexpected, f"{len(unexpected)} results changed: {unexpected[:20]}"
    for case in changed:
        assert baseline[case].startswith("err:"), f"{case} parsed on 8.x and differs now"


def test_undeclared_assets_now_parse(observed: dict[str, str], baseline: dict[str, str]) -> None:
    undeclared = [case for case, _, flag in _cases() if flag]
    assert undeclared
    still_failing = [
        case
        for case in undeclared
        if not case.endswith(_INVALID_ANYWAY) and not observed[case].startswith("ok:")
    ]
    assert not still_failing, still_failing
    accepted_invalid = [
        case
        for case in undeclared
        if case.endswith(_INVALID_ANYWAY) and not observed[case].startswith("err:")
    ]
    assert not accepted_invalid, accepted_invalid


def test_baseline_records_the_crashes_this_fixes(baseline: dict[str, str]) -> None:
    """The baseline is from before the fix: undeclared nested assets failed there."""
    for case in (
        f"Format.python|group:{_UNDECLARED}",
        f"ListCreativeFormatsResponse.python|individual:{_UNDECLARED}",
        f"ListCreativeFormatsResponse.python|group:{_UNDECLARED}",
        f"ListCreativeFormatsResponse.json|group:{_UNDECLARED}+extra",
        f"ListCreativeFormatsResponseCreativeAgent.python|group:{_UNDECLARED}",
    ):
        assert baseline[case].startswith("err:"), case
    # And the paths that already accepted them did.
    for case in (
        f"Format.python|individual:{_UNDECLARED}",
        f"RepeatableAssetGroup.python|{_UNDECLARED}",
        f"FormatAssetUnion|{_UNDECLARED}",
    ):
        assert baseline[case].startswith("ok:"), case


def _union_asset_types(union: Any) -> set[str]:
    """The asset types a public open union routes to a typed class."""
    tagged = get_args(union)[0]
    found: set[str] = set()
    for arm in get_args(tagged):
        field = get_args(arm)[0].model_fields.get("asset_type")
        if field is not None and isinstance(field.default, str):
            found.add(field.default)
    return found


def _advertised_asset_types(schema: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(individual, group) asset types a JSON schema advertises.

    A fallback class advertises no asset_type constant; it is recorded by name.
    Individual slots carry ``item_type``; group items do not.
    """
    individual: set[str] = set()
    group: set[str] = set()
    for name, definition in schema.get("$defs", {}).items():
        properties = definition.get("properties", {})
        if "asset_type" not in properties or "asset_id" not in properties:
            continue
        asset_type = properties["asset_type"].get("const", f"<{name}>")
        (individual if "item_type" in properties else group).add(asset_type)
    return individual, group


def test_each_path_advertises_what_8x_advertised() -> None:
    """No fallback added for undeclared assets appears in an advertised schema.

    The fallbacks behind the closed generated unions are validation-only. Each
    path still advertises what 8.x advertised: the closed generated unions where
    8.x used them, and the 14/12-type vocabularies with their pre-existing
    fallbacks where 8.x patched those in.
    """
    from adcp.types import (
        FormatAssetUnion,
        GroupFormatAssetUnion,
        LegacyListCreativeFormatsResponse,
        RepeatableAssetGroup,
    )
    from adcp.types.generated_poc.core.format import Format
    from adcp.types.generated_poc.creative.list_creative_formats_response import (
        ListCreativeFormatsResponseCreativeAgent,
    )

    individual_types, group_types = _generated_asset_types()
    generated_individual, generated_group = set(individual_types), set(group_types)
    vocabulary_individual = _union_asset_types(FormatAssetUnion)
    vocabulary_group = _union_asset_types(GroupFormatAssetUnion)
    assert vocabulary_individual < generated_individual
    assert vocabulary_group < generated_group
    fallback_individual = vocabulary_individual | {"<UnknownFormatAsset>"}
    fallback_group = vocabulary_group | {"<UnknownGroupAsset>"}

    def model(cls: type[BaseModel]) -> list[dict[str, Any]]:
        return [cls.model_json_schema(mode=mode) for mode in ("validation", "serialization")]

    def adapter(union: Any) -> list[dict[str, Any]]:
        type_adapter: TypeAdapter[Any] = TypeAdapter(union)
        return [type_adapter.json_schema(mode=mode) for mode in ("validation", "serialization")]

    expected: dict[str, tuple[list[dict[str, Any]], set[str], set[str]]] = {
        # Built before the patch: the closed generated unions, and no fallback.
        "ListCreativeFormatsResponse": (
            model(LegacyListCreativeFormatsResponse),
            generated_individual,
            generated_group,
        ),
        # Format, and the creative-agent response built lazily from it: the
        # 14-type vocabulary with its fallback; the group arm is closed.
        "Format": (model(Format), fallback_individual, generated_group),
        "ListCreativeFormatsResponseCreativeAgent": (
            model(ListCreativeFormatsResponseCreativeAgent),
            fallback_individual,
            generated_group,
        ),
        # The group on its own and the public unions: the vocabularies and
        # their fallbacks.
        "RepeatableAssetGroup": (model(RepeatableAssetGroup), set(), fallback_group),
        "FormatAssetUnion": (adapter(FormatAssetUnion), fallback_individual, fallback_group),
        "GroupFormatAssetUnion": (adapter(GroupFormatAssetUnion), set(), fallback_group),
    }
    for path, (schemas, individual, group) in expected.items():
        for schema in schemas:
            assert _advertised_asset_types(schema) == (individual, group), path


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--record":
        raise SystemExit(f"usage: {sys.argv[0]} --record PATH")
    import adcp

    print(f"recording against {Path(adcp.__file__).parent}", file=sys.stderr)
    Path(sys.argv[2]).write_text(json.dumps(observe_all(), indent=1, sort_keys=True) + "\n")
