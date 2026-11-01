"""A generated enum names each value once (#1782).

datamodel-code-generator merges an ``anyOf`` arm's narrower ``enum`` into the
root enum without deduplicating, so ``postal_country_system.System`` carried
``zip = 'zip'`` and ``zip_1 = 'zip'`` (19 enums on 10.0.0b4). Python makes the
second member an alias of the first, so nothing breaks at runtime, but
``__members__``, autocomplete and ``member.name`` then offer names that are not
AdCP values. ``post_generate_fixes.dedupe_enum_member_values`` removes the
repeats; these tests hold the whole generated tree to it.
"""

from __future__ import annotations

import importlib
import pkgutil
from enum import Enum
from pathlib import Path

import adcp.types.domains as domains
from scripts import post_generate_fixes as fixes


def _generated_enums() -> list[type[Enum]]:
    found: list[type[Enum]] = []
    for info in pkgutil.walk_packages(domains.__path__, domains.__name__ + "."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:  # a removed-schema shim that raises on import
            continue
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, Enum)
                and obj.__module__ == module.__name__
            ):
                found.append(obj)
    return found


def test_no_generated_enum_has_an_alias_member() -> None:
    offenders = {
        f"{enum.__module__}.{enum.__name__}": [
            alias for alias, member in enum.__members__.items() if alias != member.name
        ]
        for enum in _generated_enums()
    }
    offenders = {name: aliases for name, aliases in offenders.items() if aliases}
    assert offenders == {}, offenders


def test_the_postal_system_enum_names_zip_once() -> None:
    from adcp.types.domains.core.postal_country_system import System

    assert [m.value for m in System].count("zip") == 1
    assert "zip_1" not in System.__members__
    assert System("zip") is System.zip


def test_the_pass_keeps_the_first_member_for_each_value(tmp_path: Path, monkeypatch) -> None:
    generated = tmp_path / "domains"
    (generated / "core").mkdir(parents=True)
    target = generated / "core" / "thing.py"
    target.write_text(
        "from adcp.types._str_enum import StrEnum\n\n\n"
        "class System(StrEnum):\n"
        "    zip = 'zip'\n"
        "    zip_plus_four = 'zip_plus_four'\n"
        "    zip_1 = 'zip'\n"
        "    zip_plus_four_1 = 'zip_plus_four'\n\n\n"
        "class Untouched(StrEnum):\n"
        "    a = 'a'\n"
        "    b = 'b'\n\n\n"
        "class NotAnEnum:\n"
        "    x = 'a'\n"
        "    y = 'a'\n"
    )
    monkeypatch.setattr(fixes, "OUTPUT_DIR", generated)
    fixes.dedupe_enum_member_values()
    once = target.read_text()
    fixes.dedupe_enum_member_values()
    assert target.read_text() == once, "the pass must be idempotent"
    assert "zip_1" not in once and "zip_plus_four_1" not in once
    assert "    zip = 'zip'\n    zip_plus_four = 'zip_plus_four'\n" in once
    assert "    a = 'a'\n    b = 'b'\n" in once
    assert "    x = 'a'\n    y = 'a'\n" in once
