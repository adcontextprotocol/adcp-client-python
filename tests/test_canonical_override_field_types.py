"""The canonical override fields are precise under every checker (#1416).

``tests/type_checks/canonical_override_field_types.py`` iterates each canonical
list override and reads an element attribute. CI grades the fixture directory
with mypy and ``adcp.types.mypy_plugin`` on, which is the one configuration
under which the old ``SchemaVariant[...]`` annotations did not error — they
read as ``Any``. These tests grade the fixture under the other two
configurations an adopter actually runs, and pin that every list override in
``canonical_creative`` is one the fixture covers.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Annotated, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from adcp.types import canonical_creative

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "type_checks" / "canonical_override_field_types.py"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, MYPYPATH=str(ROOT / "src"))
    return subprocess.run(
        command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=600
    )


def test_fixture_passes_mypy_strict_with_the_plugin() -> None:
    result = _run([sys.executable, "-m", "mypy", "--strict", str(FIXTURE)])
    assert result.returncode == 0, result.stdout + result.stderr


def test_fixture_passes_mypy_strict_without_the_plugin(tmp_path: Path) -> None:
    """An adopter who does not install ``adcp.types.mypy_plugin`` sees real types."""
    config = tmp_path / "mypy.ini"
    config.write_text("[mypy]\nstrict = True\n")
    result = _run(
        [sys.executable, "-m", "mypy", "--config-file", str(config), "--strict", str(FIXTURE)]
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_fixture_passes_pyright() -> None:
    result = _run([sys.executable, "-m", "pyright", "--pythonpath", sys.executable, str(FIXTURE)])
    assert result.returncode == 0, result.stdout + result.stderr


def _list_element(annotation: object) -> object | None:
    """The ``T`` of a ``list[T]`` or ``list[T] | None`` annotation, else ``None``.

    ``Annotated`` metadata on ``T`` is stripped so a discriminator re-spelling of
    the same union does not read as a retyped element.
    """
    if get_origin(annotation) in (Union, types.UnionType):
        arms = [arm for arm in get_args(annotation) if arm is not type(None)]
        if len(arms) != 1:
            return None
        annotation = arms[0]
    if get_origin(annotation) is not list:
        return None
    element = get_args(annotation)[0]
    while get_origin(element) is Annotated:
        element = get_args(element)[0]
    return element


def _list_overrides() -> set[tuple[str, str]]:
    """Every ``(model, field)`` where a canonical model retypes an inherited list."""
    overrides: set[tuple[str, str]] = set()
    for name, model in vars(canonical_creative).items():
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        if not issubclass(model, canonical_creative.CanonicalBoundaryModel):
            continue
        for field in vars(model).get("__annotations__", {}):
            if field not in model.model_fields:
                continue
            base = next(
                (b for b in model.__mro__[1:] if field in getattr(b, "model_fields", {})),
                None,
            )
            if base is None:
                continue
            own = _list_element(model.model_fields[field].annotation)
            inherited = _list_element(base.model_fields[field].annotation)
            if own is not None and inherited is not None and own != inherited:
                overrides.add((name, field))
    return overrides


def _fixture_coverage() -> set[tuple[str, str]]:
    """The ``(parameter type, attribute)`` pairs the fixture's ``assert_type`` calls read."""
    covered: set[tuple[str, str]] = set()
    for node in ast.parse(FIXTURE.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        parameter = node.args.args[0]
        assert parameter.annotation is not None
        model = ast.unparse(parameter.annotation)
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "assert_type"
                and isinstance(call.args[0], ast.Attribute)
            ):
                covered.add((model, call.args[0].attr))
    return covered


def test_fixture_covers_every_list_override() -> None:
    overrides = _list_overrides()
    assert overrides, "no canonical list overrides discovered"
    missing = overrides - _fixture_coverage()
    assert not missing, f"add these fields to {FIXTURE.name}: {sorted(missing)}"


def test_no_canonical_field_is_annotated_schema_variant() -> None:
    """``SchemaVariant`` is for adopter overrides; the library declares real types."""
    source = (ROOT / "src" / "adcp" / "types" / "canonical_creative.py").read_text("utf-8")
    assert "SchemaVariant" not in source


@pytest.mark.parametrize(("model", "field"), sorted(_list_overrides()), ids=str)
def test_override_keeps_the_generated_constraints(model: str, field: str) -> None:
    """Retyping the element must not silently drop ``min_length`` and friends."""
    canonical = getattr(canonical_creative, model)
    base = next(b for b in canonical.__mro__[1:] if field in getattr(b, "model_fields", {}))
    inherited = base.model_fields[field]
    own = canonical.model_fields[field]
    inherited_min = [m for m in inherited.metadata if type(m).__name__ == "MinLen"]
    own_min = [m for m in own.metadata if type(m).__name__ == "MinLen"]
    assert own_min == inherited_min or (own_min and not inherited_min), (model, field)
