"""No module reaches into a built model to change what one of its fields accepts.

Two mechanisms used to do this, and both are gone:

* ``_ergonomic.py`` wrote ``model.__annotations__[field] = Annotated[...]`` and
  called ``model_rebuild(force=True)``. 70 of its 74 patch calls were INERT --
  ``model_rebuild`` rebuilds the schema from the existing ``model_fields``, not
  from ``__annotations__``, so the ``BeforeValidator`` never reached it. The
  coercion appeared to work because pydantic already did it: a generated enum is
  a ``StrEnum``, so a string is accepted natively, and a model field is built
  from a dict natively. Measured: with ``_apply_coercion()`` disabled, the full
  validation surface of all 14 models it named was byte-identical except the four
  fields patched by the OTHER mechanism (salesagent-1jj5e).
* ``_forward_compat.py`` wrote ``model.model_fields[field]`` and rebuilt. That
  one did take effect -- and reached only the class it named, which is how a
  parsed ``create_media_buy`` request ended up with an un-bridged
  ``targeting_overlay`` and how a hand-written subclass kept demanding a bundled
  clone its parent had stopped demanding (salesagent-wftrb).

Both failure modes have the same cause: a model's accepted shape was decided
somewhere other than where the field is declared. So this grades the rule rather
than either instance -- a new module doing it in a third spelling fails here.

The declaration site is the generator's: ``PIN_GAP_FIELDS`` in
``scripts/generate_types.py``, and ``COMPATIBILITY_ANNOTATIONS`` /
``READBACK_CLONES`` / ``_OPEN_ASSET_UNIONS`` in
``scripts/post_generate_fixes.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src" / "adcp"

#: ``aliases.py`` attaches the publisher-selector XOR validator to two generated
#: classes and forces a rebuild (salesagent-z1zqn). It is the last one, it is a
#: validator rather than a field annotation, and it is tracked rather than
#: tolerated: this set only shrinks.
_KNOWN_PATCHERS = frozenset({"src/adcp/types/aliases.py"})


def _patch_sites(tree: ast.Module) -> list[str]:
    """Writes into a built model's field machinery, in any spelling.

    Four shapes. The two deleted mechanisms used two of them, the third is the
    nearest variant of those, and the fourth is the one spelling still in the
    repo -- which is how this grading was found to be incomplete: the allowlist
    entry below did not match the first three, and
    ``test_every_known_patcher_still_patches`` said so.

    * ``X.__annotations__[...] = ...``  -- what ``_ergonomic`` did
    * ``X.model_fields[...] = ...``     -- what ``_forward_compat`` did
    * ``X.model_fields[...].metadata.append(...)`` / ``.extend(...)``
    * ``X.__pydantic_decorators__.<kind>[...] = ...`` -- a validator, rather
      than a field, grafted onto a class from outside it
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Subscript):
                    continue
                value = target.value
                if isinstance(value, ast.Attribute) and value.attr in {
                    "__annotations__",
                    "model_fields",
                }:
                    found.append(f"line {node.lineno}: {ast.unparse(target)[:60]} = ...")
                elif "__pydantic_decorators__" in ast.unparse(value):
                    found.append(f"line {node.lineno}: {ast.unparse(target)[:60]} = ...")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in {"append", "extend"}:
                continue
            owner = node.func.value
            if not (isinstance(owner, ast.Attribute) and owner.attr == "metadata"):
                continue
            if "model_fields" in ast.unparse(owner):
                found.append(f"line {node.lineno}: {ast.unparse(node)[:60]}")
    return found


def test_no_module_patches_a_built_model_field() -> None:
    """Derived over the whole package, so a new patcher in a new module fails."""
    repo_root = SRC_ROOT.parent.parent
    scanned = 0
    violators: dict[str, list[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        scanned += 1
        rel = path.relative_to(repo_root).as_posix()
        sites = _patch_sites(ast.parse(path.read_text(encoding="utf-8")))
        if sites and rel not in _KNOWN_PATCHERS:
            violators[rel] = sites
    # A derivation that matches nothing is green, so the population is asserted.
    assert scanned > 1000, f"the walk found only {scanned} modules under {SRC_ROOT}"
    assert not violators, (
        "these modules change what a built model's field accepts, instead of "
        f"declaring it where the field is declared: {violators}"
    )


def test_every_known_patcher_still_patches() -> None:
    """A fixed entry has to be removed, so the set cannot outlive its reason."""
    repo_root = SRC_ROOT.parent.parent
    stale = [
        rel
        for rel in sorted(_KNOWN_PATCHERS)
        if not _patch_sites(ast.parse((repo_root / rel).read_text(encoding="utf-8")))
    ]
    assert not stale, f"no longer patches, remove from _KNOWN_PATCHERS: {stale}"
