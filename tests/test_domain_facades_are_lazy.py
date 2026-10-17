"""Every generated domain facade binds its names lazily.

A domain facade (``adcp/types/domains/<domain>/__init__.py``, written by
``scripts/consolidate_exports.py``) re-exports the names its domain declares
exactly once. When it bound them EAGERLY, importing any submodule of the domain
ran the facade and therefore imported every leaf -- so one leaf's dependencies
became the whole domain's. ``core`` and ``formats.canonical`` reference each
other's schemas, which is a benign submodule-level cycle that Python resolves by
import order; the eager facade amplified it into a package-level one, and all 16
modules under ``formats/canonical/`` raised ``ImportError`` on a direct import,
as did ``content_standards`` entire, ``core.product``,
``core.product_allowed_action`` and four top-level modules.

This guard is STRUCTURAL: it reads the facades and refuses an eager import of a
domain's own submodules. The behavioural half -- that a leaf of every package
still imports first in a fresh interpreter -- belongs to
``test_submodule_imports_standalone`` in ``tests/test_lazy_types.py``, which
already owned that check for the top-level modules and now derives the
``adcp.types`` tree as well. Two checks, not two populations of one check: this
one is exact about the property the fix establishes and no choice of population
can dodge it, while that one catches an amplifier appearing somewhere this AST
rule does not look.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_TYPES = Path(__file__).resolve().parent.parent / "src" / "adcp" / "types"
_DOMAINS = _TYPES / "domains"


def _module_name(path: Path) -> str:
    parts = path.resolve().relative_to(_TYPES.parent.parent).with_suffix("").parts
    return ".".join(parts).removesuffix(".__init__")


def _facades() -> list[Path]:
    found = sorted(path for path in _DOMAINS.glob("*/__init__.py"))
    assert (
        len(found) > 10
    ), f"found {len(found)} domain facades, so this guard grades almost nothing"
    return found


def _eager_submodule_imports(path: Path) -> list[str]:
    """Imports of this domain's own submodules that run at import time.

    An import nested under ``if TYPE_CHECKING:`` does not run, which is where a
    facade keeps the surface type checkers need. Anything at module level does.
    """
    tree = ast.parse(path.read_text())
    domain = _module_name(path)
    eager: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(f"{domain}."):
            eager.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            eager.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name.startswith(f"{domain}.")
            )
    return eager


@pytest.mark.parametrize("facade", _facades(), ids=_module_name)
def test_every_domain_facade_is_lazy(facade: Path) -> None:
    """No facade may bind its own submodules at import time.

    The one that did made every leaf of its domain depend on every other leaf,
    and 38 modules could not be imported on their own because of it.
    """
    eager = _eager_submodule_imports(facade)
    assert not eager, (
        f"{_module_name(facade)} imports its own submodules eagerly, which makes every "
        f"leaf of the domain depend on every other leaf:\n  " + "\n  ".join(eager)
    )
