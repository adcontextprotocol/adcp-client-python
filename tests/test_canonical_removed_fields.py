"""The removed legacy-identity fields, graded on every surface that shows them.

A canonical model is a real subclass of the generated wire model, so it
INHERITS that model's legacy creative identity (``format_id``, ``format_ids``,
``format_ids_pending``, ``format_ids_to_provide``).
``CanonicalBoundaryModel.__pydantic_init_subclass__`` removes those inherited
declarations, and it does so through pydantic internals: it deletes from
``cls.model_fields``, pops ``__annotations__``, binds the name to ``None`` and
forces ``model_rebuild``. None of that is a supported pydantic API. It works on
2.13; a minor release could change any of it without saying so.

This module is the alarm for that. For every canonical model that inherits a
legacy-identity field it grades the removal on all four runtime surfaces an
adopter can observe —

* ``model_fields``,
* ``model_json_schema()``,
* ``model_dump()`` / ``model_dump_json()``,
* ``model_validate`` of a document carrying the name,

— and on the two static ones, because a type checker that still sees the
field disagrees with a runtime that refuses it:

* the ``TYPE_CHECKING`` redeclaration exists, says ``init=False``, and restates
  the generated parent's field type — compared as pydantic RESOLVES both sides,
  since the written spelling and the ``FieldInfo.annotation`` differ for ``None``;
* mypy and pyright both refuse the constructor keyword.

Nothing here is a hand-written table. The model list is
``PRIMARY_CANONICAL_MODELS``; the removed names per model are every field name
on a generated ancestor that satisfies ``is_legacy_creative_identity_key`` —
the one predicate that already owns input rejection, schema sanitation,
serialization and the removal rule itself. In particular the names are derived
from the ANCESTORS rather than by subtracting the canonical model's own
``model_fields``: a subtraction would silently go empty the day the removal
stops working, and every test here would then pass with nothing to grade.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError, create_model

from adcp.types import canonical_creative
from adcp.types.canonical_creative import (
    PRIMARY_CANONICAL_MODELS,
    is_legacy_creative_identity_key,
)
from adcp.types.domains.core.format_id import FormatReferenceStructuredObject

_GENERATED_PREFIX = "adcp.types.domains."
_SOURCE = Path(canonical_creative.__file__)

#: The two owned refusals. Asserting on the text keeps the ``model_validate``
#: test from passing because of a missing required field — and asserting on the
#: text of the mechanism that OWNS the name on that class keeps it from passing
#: because the other mechanism happened to catch it.
#:
#: ``_REFUSAL_BOUNDARY`` is ``CanonicalBoundaryModel._reject_legacy_creative_identity``.
#: ``_REFUSAL_BRANCH`` is ``adcp.types._conditions``' forbidden-names check, which
#: runs first on a class bound to a branch that forbids the name — ``CreativeAsset``
#: is bound to the canonical-format branch of ``core/creative-asset.json`` and that
#: branch bans ``format_id`` outright.
_REFUSAL_BOUNDARY = "contains legacy creative identity"
_REFUSAL_BRANCH = "is not accepted by this request shape"


def _generated_ancestors(model: type[BaseModel]) -> list[type[BaseModel]]:
    return [
        base
        for base in model.__mro__
        if base.__module__.startswith(_GENERATED_PREFIX) and issubclass(base, BaseModel)
    ]


def _removed_names(model: type[BaseModel]) -> list[str]:
    """Every legacy-identity field *model* inherits from a generated ancestor."""

    inherited: set[str] = set()
    for ancestor in _generated_ancestors(model):
        inherited |= {
            name for name in ancestor.model_fields if is_legacy_creative_identity_key(name)
        }
    return sorted(inherited)


def _declaring_ancestor(model: type[BaseModel], name: str) -> type[BaseModel]:
    for ancestor in _generated_ancestors(model):
        if name in ancestor.model_fields:
            return ancestor
    raise AssertionError(f"{name} is on no generated ancestor of {model.__name__}")


def _pairs() -> list[tuple[type[BaseModel], str]]:
    return [(model, name) for model in PRIMARY_CANONICAL_MODELS for name in _removed_names(model)]


REMOVED = _pairs()
REMOVED_IDS = [f"{model.__name__}.{name}" for model, name in REMOVED]
MODELS_WITH_REMOVALS = sorted({model.__name__ for model, _ in REMOVED})


def test_the_removed_field_surface_is_not_empty() -> None:
    """A later refactor must not satisfy this module by emptying the surface.

    Eleven canonical models inherit fifteen legacy-identity declarations. The
    numbers are pinned exactly, the way
    ``test_the_canonical_model_list_is_not_empty`` pins the model count: a drop
    means a generated parent stopped declaring the field (then this number
    moves deliberately), and a rise means a new one needs hiding too.
    """
    assert len(MODELS_WITH_REMOVALS) == 11, MODELS_WITH_REMOVALS
    assert len(REMOVED) == 15, REMOVED_IDS


@pytest.mark.parametrize(("model", "name"), REMOVED, ids=REMOVED_IDS)
def test_the_removed_field_is_absent_from_model_fields(model: type[BaseModel], name: str) -> None:
    """``__pydantic_init_subclass__``'s own surface."""
    assert name in _declaring_ancestor(model, name).model_fields
    assert name not in model.model_fields


def _schema_keys(node: Any) -> Iterator[str]:
    """Every property name and ``required`` entry anywhere under *node*."""

    if isinstance(node, list):
        for item in node:
            yield from _schema_keys(item)
        return
    if not isinstance(node, dict):
        return
    properties = node.get("properties")
    if isinstance(properties, dict):
        yield from (key for key in properties if isinstance(key, str))
    required = node.get("required")
    if isinstance(required, list):
        yield from (key for key in required if isinstance(key, str))
    for value in node.values():
        yield from _schema_keys(value)


@pytest.mark.parametrize(("model", "name"), REMOVED, ids=REMOVED_IDS)
def test_the_removed_field_is_absent_from_the_json_schema(
    model: type[BaseModel], name: str
) -> None:
    """At every nesting depth, not only at the root.

    ``sanitize_canonical_schema`` walks the whole document, so a nested
    ``$defs`` entry re-introducing the name is as much a leak as a root
    property.
    """
    keys = list(_schema_keys(model.model_json_schema()))
    assert keys, model.__name__
    assert name not in keys, f"{model.__name__} publishes {name} in its JSON schema"


@pytest.mark.parametrize(("model", "name"), REMOVED, ids=REMOVED_IDS)
def test_the_removed_field_is_absent_from_model_dump(model: type[BaseModel], name: str) -> None:
    """Including when an instance really is carrying the key.

    A canonical model is ``extra="allow"``, so the name does land in
    ``__pydantic_extra__`` when it bypasses validation — which is exactly the
    case the strip serializer exists for. Asserting the extra arrived first is
    what keeps this from passing merely because nothing was ever set.
    """
    carrier = model.model_construct(**{name: "legacy-value"})
    assert name in (carrier.__pydantic_extra__ or {}), (
        f"{model.__name__}.{name} did not land in __pydantic_extra__ — either the "
        f"boundary's extra policy changed or the field is a declared field again"
    )

    assert name not in carrier.model_dump()
    assert name not in carrier.model_dump(mode="json")
    assert name not in json.loads(carrier.model_dump_json())
    assert name not in model.model_construct().model_dump()


def _expected_refusal(model: type[BaseModel], name: str) -> str:
    """Which mechanism owns *name* on *model* — DERIVED, not listed.

    A branch that forbids a name types it ``None``, so the generated ancestor's
    resolved field type is the whole rule. Measured over all fifteen pairs:
    exactly one, ``CreativeAsset.format_id``, resolves to ``NoneType`` and is
    refused by the branch; the other fourteen inherit a real type and reach the
    boundary validator.
    """

    inherited = _declaring_ancestor(model, name).model_fields[name].annotation
    if _as_pydantic_resolves_it(inherited) is type(None):
        return _REFUSAL_BRANCH
    return _REFUSAL_BOUNDARY


@pytest.mark.parametrize(("model", "name"), REMOVED, ids=REMOVED_IDS)
def test_model_validate_rejects_a_document_carrying_the_removed_field(
    model: type[BaseModel], name: str
) -> None:
    """Refused, and refused by the mechanism that owns the name on this class.

    A document holding only the removed name would also fail for missing
    required fields on most of these models, so the refusal text is asserted:
    without it this test would stay green if the refusing validator were
    deleted outright.

    Naming the EXPECTED mechanism rather than accepting either text is what
    keeps both deletions red. Delete the branch's forbidden-names check and
    ``CreativeAsset`` falls through to the boundary's text, which is not the one
    expected; delete the boundary validator and the fourteen root-bound models
    fall through to ``missing``.
    """
    expected = _expected_refusal(model, name)
    with pytest.raises(ValidationError) as caught:
        model.model_validate({name: "legacy-value"})
    assert expected in str(caught.value), str(caught.value)


# ---------------------------------------------------------------------------
# The static half: a type checker must not offer a keyword the runtime refuses.
# ---------------------------------------------------------------------------


def _type_checking_redeclarations() -> dict[tuple[str, str], ast.AnnAssign]:
    """Map ``(class name, field name)`` to its ``TYPE_CHECKING`` redeclaration."""

    tree = ast.parse(_SOURCE.read_text())
    found: dict[tuple[str, str], ast.AnnAssign] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not (
                isinstance(statement, ast.If)
                and isinstance(statement.test, ast.Name)
                and statement.test.id == "TYPE_CHECKING"
            ):
                continue
            for declaration in statement.body:
                if isinstance(declaration, ast.AnnAssign) and isinstance(
                    declaration.target, ast.Name
                ):
                    found[node.name, declaration.target.id] = declaration
    return found


def _alias_namespace() -> dict[str, Any]:
    """The module's globals plus the aliases its ``TYPE_CHECKING`` block defines.

    Those aliases never execute, so they are read out of the source and
    evaluated here. Evaluating them rather than restating them is the point:
    the comparison below then has no second copy of the annotation to drift
    from.
    """

    namespace: dict[str, Any] = dict(vars(canonical_creative))
    namespace["FormatReferenceStructuredObject"] = FormatReferenceStructuredObject
    namespace["Sequence"] = Sequence
    tree = ast.parse(_SOURCE.read_text())
    for node in tree.body:
        if not (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            continue
        for statement in node.body:
            if isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name):
                namespace[statement.targets[0].id] = eval(ast.unparse(statement.value), namespace)
    return namespace


def _as_pydantic_resolves_it(annotation: Any) -> Any:
    """*annotation* as pydantic records it on a ``FieldInfo``.

    The declared side is read out of the source and evaluated, so a field
    annotated ``None`` arrives as the VALUE ``None`` while the ancestor's
    ``FieldInfo.annotation`` holds the TYPE ``NoneType`` — pydantic normalises
    one to the other while building the field. Comparing the two spellings
    directly reports ``CreativeAsset.format_id`` as disagreeing with its parent
    at the moment it restates the parent exactly, and NO spelling in the source
    can settle it: ``NoneType`` is the spelling the comparison wants and mypy
    refuses that outright (*NoneType should not be used as a type, please use
    None instead*). So both sides go through pydantic's own normalisation and
    the comparison is between RESOLVED types rather than between annotation
    text.

    Measured idempotent on every other annotation this module compares —
    ``FormatReferenceStructuredObject``, its optional, ``list[...]``,
    ``Sequence[...]`` and that optional all come back unchanged — so the
    normalisation collapses the one representation difference and nothing else.
    """

    return create_model("_Resolved", field=(annotation, None)).model_fields["field"].annotation


@pytest.mark.parametrize(("model", "name"), REMOVED, ids=REMOVED_IDS)
def test_the_removed_field_is_hidden_from_the_type_checker(
    model: type[BaseModel], name: str
) -> None:
    """Every removal has a ``TYPE_CHECKING`` redeclaration, and it is honest.

    ``init=False`` is what removes the keyword from the ``__init__`` mypy and
    pyright synthesize through ``dataclass_transform``. The annotation must be
    the generated parent's field type: a narrowed one is an incompatible
    override (pyright says so by name), and a widened one hands the caller a
    read type the runtime never produces. ``_as_pydantic_resolves_it`` is what
    makes "the same type" mean the resolved type rather than the written one —
    a branch-bound class annotates ``None`` where its parent's ``FieldInfo``
    reads ``NoneType``, and mypy refuses the second spelling.
    """
    declaration = _type_checking_redeclarations().get((model.__name__, name))
    assert declaration is not None, (
        f"{model.__name__}.{name} is removed at runtime but still visible to a "
        f"type checker — add a TYPE_CHECKING redeclaration with init=False"
    )
    assert isinstance(declaration.value, ast.Call), ast.unparse(declaration)
    keywords = {
        keyword.arg: ast.literal_eval(keyword.value)
        for keyword in declaration.value.keywords
        if keyword.arg is not None
    }
    assert keywords.get("init") is False, ast.unparse(declaration)

    declared = eval(ast.unparse(declaration.annotation), _alias_namespace())
    inherited = _declaring_ancestor(model, name).model_fields[name].annotation
    assert _as_pydantic_resolves_it(declared) == _as_pydantic_resolves_it(inherited), (
        f"{model.__name__}.{name} is annotated {declared!r}, which resolves to "
        f"{_as_pydantic_resolves_it(declared)!r}, where its generated parent's field "
        f"resolves to {_as_pydantic_resolves_it(inherited)!r}"
    )


def _construction(model: type[BaseModel], names: list[str]) -> str:
    """``Model(<removed>=<correctly typed value>, ...)`` for a checker to refuse."""

    arguments = []
    for name in names:
        annotation = str(_declaring_ancestor(model, name).model_fields[name].annotation)
        arguments.append(
            f"{name}=[_REF]" if annotation.startswith(("list[", "collections")) else f"{name}=_REF"
        )
    return f"({', '.join(arguments)})"


def _refusal_snippet() -> tuple[str, dict[int, tuple[str, str]], dict[int, tuple[str, str]]]:
    """Build a snippet, plus the line → (class, field) maps for both halves.

    The control half constructs the GENERATED parent with the same keyword. A
    checker must accept it there — otherwise a misspelled keyword would make
    the canonical half refuse for the wrong reason and this test would pass
    while the hiding did nothing.
    """

    lines = [
        "from adcp.types.domains.core.format_id import FormatReferenceStructuredObject",
        '_REF = FormatReferenceStructuredObject(agent_url="https://a.example", id="b")',
    ]
    canonical_lines: dict[int, tuple[str, str]] = {}
    control_lines: dict[int, tuple[str, str]] = {}

    for index, class_name in enumerate(MODELS_WITH_REMOVALS):
        model = next(m for m, _ in REMOVED if m.__name__ == class_name)
        names = _removed_names(model)
        lines.append(f"from {model.__module__} import {class_name} as _Canonical{index}")
        ancestor = _declaring_ancestor(model, names[0])
        lines.append(f"from {ancestor.__module__} import {ancestor.__name__} as _Wire{index}")
        for name in names:
            canonical_lines[len(lines) + 1] = (class_name, name)
            lines.append(f"_Canonical{index}{_construction(model, [name])}")
            control_lines[len(lines) + 1] = (ancestor.__name__, name)
            lines.append(f"_Wire{index}{_construction(model, [name])}")

    return "\n".join(lines) + "\n", canonical_lines, control_lines


_UNKNOWN_KEYWORD = {
    "mypy": 'Unexpected keyword argument "{name}"',
    "pyright": 'No parameter named "{name}"',
}


def _diagnostics(checker: str, snippet: Path, root: Path) -> list[tuple[int, str]]:
    if checker == "mypy":
        command = [sys.executable, "-m", "mypy", "--strict", str(snippet)]
    else:
        command = [
            sys.executable,
            "-m",
            "pyright",
            "--outputjson",
            "--pythonpath",
            sys.executable,
            str(snippet),
        ]
    environment = dict(os.environ, MYPYPATH=str(root / "src"))
    result = subprocess.run(
        command, cwd=root, env=environment, capture_output=True, text=True, timeout=600
    )
    if checker == "pyright":
        report = json.loads(result.stdout)
        return [
            (entry["range"]["start"]["line"] + 1, entry["message"])
            for entry in report["generalDiagnostics"]
        ]
    output: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) == 4 and parts[1].isdigit():
            output.append((int(parts[1]), parts[3].strip()))
    assert output, result.stdout + result.stderr
    return output


@pytest.mark.parametrize("checker", ["mypy", "pyright"])
def test_both_checkers_refuse_the_removed_constructor_keyword(checker: str, tmp_path: Path) -> None:
    """The disagreement in review item 3, pinned shut on both checkers.

    Before the ``TYPE_CHECKING`` redeclarations, every one of these fifteen
    constructions type-checked clean on mypy AND pyright and then raised
    ``ValidationError`` at runtime. The keyword is now unavailable statically,
    with no suppression anywhere: ``init=False`` is read by both checkers
    through pydantic's ``dataclass_transform``.
    """
    root = Path(__file__).resolve().parents[1]
    snippet_text, canonical_lines, control_lines = _refusal_snippet()
    snippet = tmp_path / "removed_field_keywords.py"
    snippet.write_text(snippet_text)

    diagnostics = _diagnostics(checker, snippet, root)
    phrase = _UNKNOWN_KEYWORD[checker]

    def on(line: int) -> list[str]:
        return [message for number, message in diagnostics if number == line]

    for line, (class_name, name) in canonical_lines.items():
        wanted = phrase.format(name=name)
        assert any(
            wanted in message for message in on(line)
        ), f"{checker} accepted {class_name}({name}=...) — it reported {on(line)}"

    for line, (class_name, name) in control_lines.items():
        wanted = phrase.format(name=name)
        assert not any(wanted in message for message in on(line)), (
            f"{checker} does not know {name} on the wire model {class_name} either, "
            f"so the refusal above proves nothing: {on(line)}"
        )
