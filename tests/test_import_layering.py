"""Enforce the type-import layering rule documented in CLAUDE.md.

Only ``aliases.py``, ``_ergonomic.py``, ``_generated.py``, and the public
``adcp.types/__init__.py`` composer may import from the auto-generated layer
(``adcp.types._generated`` / ``adcp.types.generated_poc``). Every other module
under ``src/adcp/`` should import types via the public surface (``adcp.types``
or ``adcp``).

The rule exists because schema-regen rewrites generated class names. Public
imports are forwarded through ``aliases.py`` and survive regen; direct
``generated_poc`` imports break silently when datamodel-code-generator picks
a different name.

This test enforces a **frozen baseline**: existing violations are listed in
``_KNOWN_VIOLATIONS`` so refactor-them-away can be tackled separately. Any
*new* file or *new* import that bypasses the public surface fails the test.

To shrink the baseline:
- Re-route the import through ``adcp.types`` or ``adcp``; if the symbol
  isn't exported there, add it to ``aliases.py`` and ``__init__.py``.
- Then remove the file from ``_KNOWN_VIOLATIONS``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src" / "adcp"

ALLOWED_FILES = {
    SRC_ROOT / "types" / "aliases.py",
    SRC_ROOT / "types" / "_ergonomic.py",
    SRC_ROOT / "types" / "_generated.py",
    SRC_ROOT / "types" / "__init__.py",
    # ``capabilities.py`` is a re-export layer for the bundled
    # ``get_adcp_capabilities_response`` sub-models — it disambiguates
    # the ``Account`` / ``MediaBuy`` / ``Creative`` name collisions
    # before adopters import them via :mod:`adcp.decisioning.capabilities`.
    # Same architectural role as ``aliases.py`` (re-exports + renames),
    # so the same direct ``generated_poc`` import access applies.
    SRC_ROOT / "types" / "capabilities.py",
    # ``_forward_compat.py`` patches Format.assets and Assets94.assets at
    # import time with open union types (issue #742). It must import the
    # generated classes in-place to call model_rebuild() on them, giving it
    # the same architectural role as ``_ergonomic.py``.
    SRC_ROOT / "types" / "_forward_compat.py",
}

# Frozen baseline of pre-existing violations — paths relative to repo root.
# Add a file here only as a temporary measure; prefer fixing the import.
# Remove a file here when its violation is fixed (the test will fail-closed
# if you forget to update the list, which is the desired behavior).
_KNOWN_VIOLATIONS = frozenset(
    {
        "src/adcp/capabilities.py",
        "src/adcp/client.py",
        "src/adcp/signing/autosign.py",
        "src/adcp/signing/client.py",
        "src/adcp/utils/format_assets.py",
        "src/adcp/utils/preview_cache.py",
        "src/adcp/webhook_receiver.py",
        "src/adcp/webhook_sender.py",
    }
)

_FORBIDDEN_PREFIXES = (
    "adcp.types._generated",
    "adcp.types.generated_poc",
)


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
        if path in ALLOWED_FILES or "generated_poc" in path.parts:
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


def test_claude_md_documents_the_rule() -> None:
    """The architectural rule this test enforces must be documented in CLAUDE.md."""
    claude_md = SRC_ROOT.parent.parent / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8")
    # Loose match — we care that someone reading the test can find the rationale.
    assert re.search(r"generated_poc/.*may import|import.*generated_poc", text), (
        "CLAUDE.md doesn't reference the generated_poc import layering rule. "
        "If the rule moved, update this test's docstring with the new pointer."
    )
