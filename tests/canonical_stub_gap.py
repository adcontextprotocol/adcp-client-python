"""Which runtime fields ``canonical_creative.pyi`` does not declare.

``canonical_creative.py`` builds its public models with ``create_model``, so a
type checker cannot see their fields and reads the hand-written
``canonical_creative.pyi`` instead. A field the stub omits does not exist as far
as mypy is concerned: reading it is an attribute error, and passing it to the
constructor the pydantic plugin synthesizes is an unexpected keyword argument.

This module computes the gap. ``test_protocol_envelope_inheritance`` holds it to
a ledger that may only shrink, and ``scripts/update_canonical_stub_ledger.py``
rewrites the ledger from the current gap.
"""

from __future__ import annotations

import ast
from pathlib import Path

from pydantic import BaseModel

from adcp.types import canonical_creative

LEDGER_FILE = Path(__file__).parent / "canonical_stub_field_gap.json"


def _stub_path() -> Path:
    return Path(canonical_creative.__file__).with_suffix(".pyi")


def _public_models() -> dict[str, type[BaseModel]]:
    return {
        name: obj
        for name, obj in vars(canonical_creative).items()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and not name.startswith("_")
    }


def stub_field_gap() -> dict[str, list[str]]:
    """Runtime fields each public model carries that the stub never declares.

    Classes the stub resolves by import are excluded: their fields are declared
    wherever they are defined, not here.
    """
    stub = ast.parse(_stub_path().read_text(encoding="utf-8"))

    imported: set[str] = {
        alias.asname or alias.name
        for node in ast.walk(stub)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    }
    declared: dict[str, set[str]] = {}
    bases: dict[str, list[str]] = {}
    for node in stub.body:
        if not isinstance(node, ast.ClassDef):
            continue
        declared[node.name] = {
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        }
        bases[node.name] = [ast.unparse(base) for base in node.bases]

    def chain(name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        fields = set(declared.get(name, ()))
        for base in bases.get(name, []):
            if base in declared and base not in seen:
                fields |= chain(base, seen | {name})
        return fields

    gap: dict[str, list[str]] = {}
    for name, model in _public_models().items():
        if name in imported:
            continue
        undeclared = set(model.model_fields) - chain(name)
        if undeclared:
            gap[name] = sorted(undeclared)
    return dict(sorted(gap.items()))
