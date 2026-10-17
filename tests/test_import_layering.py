"""Enforce the type-import layering rule documented in CLAUDE.md.

``adcp.types._generated`` is the one internal namespace left: it binds a bare
type name that several generated modules define to a single winner, chosen by
module sort order. Only the type facade/override modules that build the public
surface — ``aliases.py``, ``capabilities.py``, ``_eager.py``, and the public
``adcp.types/__init__.py`` composer — may import from it. Every other module
under ``src/adcp/`` imports types via ``adcp.types``, or from the domain module
that declares the name when the flat namespace cannot bind it.

The rule exists because that single winner is not the schema's choice: a schema
addition can repoint a bare name, and the only trace is a line in a regenerated
file.

The generated tree is NOT on this list. ``adcp.types.domains.<domain>`` and
``adcp.types.domains.<domain>.<schema>`` are where the generator defines every
class, and they are a public address — there is no private tree to reach into,
which is why the eight files that used to be listed below as violations now
import legally.

This test enforces a **frozen baseline**: existing violations are listed in
``_KNOWN_VIOLATIONS`` so refactor-them-away can be tackled separately. Any
*new* file or *new* import that bypasses the public surface fails the test.

To shrink the baseline:
- Re-route the import. Only ``adcp.types._generated`` is forbidden; ``adcp``,
  ``adcp.types``, ``adcp.types.domains.<domain>[.<schema>]``,
  ``adcp.types.legacy``, ``adcp.types.aliases``, ``adcp.types.error_details``
  and ``adcp.types.capabilities`` are public and all pass.
- Which one depends on WHY the import is deep, and the two answers differ. If
  the name is merely absent from the flat surface, the surface is incomplete:
  every generated type is declared by the module for the schema that declares
  it, so ``adcp.types.domains...`` serves it. If the name is QUARANTINED — the
  legacy v1 ``format_id`` world is reachable only under explicitly named
  ``Legacy*`` paths, which ``tests/test_canonical_creatives_rc3.py`` asserts —
  then ``adcp.types.legacy`` is the door, and adding the name to the root
  surface is the wrong fix.
- Then remove the file from ``_KNOWN_VIOLATIONS``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src" / "adcp"

#: The generated tree. Skipped wholesale: it is a thousand modules the
#: generator writes, and it imports its own siblings relatively.
GENERATED_DIR = SRC_ROOT / "types" / "domains"

ALLOWED_FILES = {
    SRC_ROOT / "types" / "aliases.py",
    SRC_ROOT / "types" / "_generated.py",
    SRC_ROOT / "types" / "__init__.py",
    # ``_eager.py`` holds the eager realization of the public type surface
    # (the former ``__init__.py`` body): it binds every exported name from
    # ``_generated`` and runs the import-time patchers. ``__init__.py`` is now
    # a thin lazy facade that imports ``_eager`` on first attribute access, so
    # the same direct ``_generated`` access applies.
    SRC_ROOT / "types" / "_eager.py",
    # ``capabilities.py`` is a re-export layer for the bundled
    # ``get_adcp_capabilities_response`` sub-models — it disambiguates
    # the ``Account`` / ``MediaBuy`` / ``Creative`` name collisions
    # before adopters import them via :mod:`adcp.decisioning.capabilities`.
    # Same architectural role as ``aliases.py`` (re-exports + renames),
    # so the same direct ``_generated`` access applies.
    SRC_ROOT / "types" / "capabilities.py",
}

# Frozen baseline of pre-existing violations — paths relative to repo root.
# Add a file here only as a temporary measure; prefer fixing the import.
# Remove a file here when its violation is fixed (the test will fail-closed
# if you forget to update the list, which is the desired behavior).
#: Empty: the last entry (``src/adcp/utils/preview_cache.py``) no longer imports
#: ``adcp.types._generated``, and the stale-entry half of the assertion below had
#: been failing for it. A baseline only shrinks.
_KNOWN_VIOLATIONS: frozenset[str] = frozenset()

_FORBIDDEN_PREFIXES = ("adcp.types._generated",)


def _module_imports_forbidden(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module.startswith(p) for p in _FORBIDDEN_PREFIXES):
                bad.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(p) for p in _FORBIDDEN_PREFIXES):
                    bad.append(f"line {node.lineno}: import {alias.name}")
    return bad


def test_no_new_layering_violations() -> None:
    """No new source module may bypass the public type surface.

    Pre-existing violations are listed in ``_KNOWN_VIOLATIONS``; only
    additions trip this assertion.
    """
    repo_root = SRC_ROOT.parent.parent
    violators: dict[str, list[str]] = {}
    for path in SRC_ROOT.rglob("*.py"):
        if path in ALLOWED_FILES or path.is_relative_to(GENERATED_DIR):
            continue
        rel = str(path.relative_to(repo_root))
        bad = _module_imports_forbidden(path)
        if bad:
            violators[rel] = bad

    new = {f: violators[f] for f in violators if f not in _KNOWN_VIOLATIONS}
    stale = sorted(_KNOWN_VIOLATIONS - violators.keys())

    msgs: list[str] = []
    if new:
        msgs.append("New layering violation — import via adcp.types instead:")
        for file, bad in sorted(new.items()):
            msgs.append(f"  {file}")
            for b in bad:
                msgs.append(f"    {b}")
    if stale:
        msgs.append("Stale entries in _KNOWN_VIOLATIONS — remove from this test:")
        for file in stale:
            msgs.append(f"  {file}")
    if msgs:
        msgs.append("")
        msgs.append("See CLAUDE.md → 'Import Architecture for Generated Types' for the rule.")
        raise AssertionError("\n".join(msgs))


def test_generated_tree_is_not_forbidden() -> None:
    """The generated tree's own address must stay off the forbidden list.

    The generator writes ``adcp.types.domains`` and defines every public class
    there, so forbidding that prefix would forbid the definition site. This
    assertion is what makes the shrink above deliberate rather than a lapse:
    re-adding the prefix to buy back the old allowlist fails here.
    """
    assert not any(p.startswith("adcp.types.domains") for p in _FORBIDDEN_PREFIXES), (
        "adcp.types.domains is where the generator defines the public classes — "
        "see docs/type-surface.md. Forbid only namespaces that resolve a name by "
        "something other than the schema."
    )


def test_claude_md_documents_the_rule() -> None:
    """The architectural rule this test enforces must be documented in CLAUDE.md."""
    claude_md = SRC_ROOT.parent.parent / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8")
    # Loose match — we care that someone reading the test can find the rationale.
    assert re.search(r"may import from `_generated\.py`|import.*_generated\.py", text), (
        "CLAUDE.md doesn't reference the generated-layer import layering rule. "
        "If the rule moved, update this test's docstring with the new pointer."
    )
