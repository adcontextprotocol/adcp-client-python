#!/usr/bin/env python3
"""Diff Pydantic-model field shape between two generated_poc/ trees.

Codegen lands as ``chore(schemas): sync …`` with hundreds of file changes, so
downstream consumers (salesagent, ad servers) can't tell from release notes
which model gained or lost a field. This script walks a generated tree, AST-
parses each module, and emits per-class field sets — then diffs two such
snapshots to produce a markdown report consumers read to know whether they can
shrink their schema-mismatch allowlists.

Usage:
    # Capture a snapshot of the current tree to JSON
    python scripts/diff_generated_types.py snapshot \\
        src/adcp/types/generated_poc/ /tmp/before.json

    # After regen, write a markdown delta against the snapshot
    python scripts/diff_generated_types.py diff \\
        /tmp/before.json src/adcp/types/generated_poc/ \\
        --output SCHEMA_DELTAS.md

The library API (``snapshot``, ``format_diff``) is what ``generate_types.py``
calls in-process to avoid temp files.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

# {relative_posix_path: {ClassName: [field_name, ...]}}
Snapshot = dict[str, dict[str, list[str]]]


def snapshot(root: Path) -> Snapshot:
    """Walk ``root`` for .py files and return per-class member-name lists.

    Captures both Pydantic field declarations (``AnnAssign``: ``name: Annotated
    [...] = default``) and plain class-level assignments (``Assign``:
    ``NAME = 'NAME'``). The latter is required to track Enum members — without
    it, deleted error codes or status values slip through the diff invisibly.
    Module-level configuration assignments (``model_config = ConfigDict(...)``)
    are excluded by capturing only the simple-name targets we care about, but
    all such targets at the class body level are kept since Pydantic does not
    use bare ``Assign`` for fields.
    """
    out: Snapshot = {}
    for py_file in sorted(root.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        rel = py_file.relative_to(root).as_posix()
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        per_file: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            fields: list[str] = []
            for child in node.body:
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    if child.target.id != "model_config":
                        fields.append(child.target.id)
                elif isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id != "model_config":
                            fields.append(target.id)
            if fields:
                per_file[node.name] = fields
        if per_file:
            out[rel] = per_file
    return out


def format_diff(before: Snapshot, after: Snapshot) -> str:
    """Render the difference between two snapshots as Markdown.

    The report has three top-level sections: files added, files removed, and
    per-file class/field changes. Empty sections are omitted so the report is
    blank when codegen produced no semantic delta.
    """
    files_added = sorted(set(after) - set(before))
    files_removed = sorted(set(before) - set(after))
    files_common = sorted(set(before) & set(after))

    file_changes: list[tuple[str, list[str]]] = []
    for rel in files_common:
        b_classes = before[rel]
        a_classes = after[rel]
        classes_added = sorted(set(a_classes) - set(b_classes))
        classes_removed = sorted(set(b_classes) - set(a_classes))
        classes_common = sorted(set(b_classes) & set(a_classes))

        lines: list[str] = []
        if classes_added:
            lines.append(f"  - **classes added**: {', '.join(classes_added)}")
        if classes_removed:
            lines.append(f"  - **classes removed**: {', '.join(classes_removed)}")
        for cls in classes_common:
            b_fields = set(b_classes[cls])
            a_fields = set(a_classes[cls])
            added = sorted(a_fields - b_fields)
            removed = sorted(b_fields - a_fields)
            if added or removed:
                bits: list[str] = []
                if added:
                    bits.append(f"`+{'`, `+'.join(added)}`")
                if removed:
                    bits.append(f"`-{'`, `-'.join(removed)}`")
                lines.append(f"  - `{cls}`: {' '.join(bits)}")
        if lines:
            file_changes.append((rel, lines))

    parts: list[str] = ["# Generated-types delta", ""]
    if not (files_added or files_removed or file_changes):
        parts.append("_No field-shape changes detected._")
        parts.append("")
        return "\n".join(parts)

    if files_added:
        parts.append("## Files added")
        parts.append("")
        for rel in files_added:
            classes = ", ".join(sorted(after[rel]))
            parts.append(f"- `{rel}` — {classes}")
        parts.append("")

    if files_removed:
        parts.append("## Files removed")
        parts.append("")
        for rel in files_removed:
            classes = ", ".join(sorted(before[rel]))
            parts.append(f"- `{rel}` — {classes}")
        parts.append("")

    if file_changes:
        parts.append("## Field changes")
        parts.append("")
        for rel, lines in file_changes:
            parts.append(f"- `{rel}`")
            parts.extend(lines)
        parts.append("")

    return "\n".join(parts)


def _cmd_snapshot(args: argparse.Namespace) -> int:
    snap = snapshot(Path(args.root))
    Path(args.output).write_text(json.dumps(snap, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote snapshot of {len(snap)} files to {args.output}")
    return 0


def _file_signature(classes: dict[str, list[str]]) -> frozenset[frozenset[str]]:
    """Per-file signature: the multiset of field-name sets, class-name-agnostic.

    datamodel-code-generator produces numbered-variant class names
    (``PackageUpdate1`` vs ``PackageUpdate4``, ``Status16`` vs ``Status17``)
    whose numbering depends on filesystem-iteration order — APFS on macOS
    sorts differently than ext4 on Linux CI, so naive class-name diffs
    flag every regen. The signature collapses ``{"PackageUpdate1": [a,b]}``
    and ``{"PackageUpdate4": [a,b]}`` to the same value, so renaming
    without semantic change is invisible. A real change (added or removed
    field, added or removed class) shifts the multiset of frozensets and
    is caught.
    """
    return frozenset(frozenset(fields) for fields in classes.values())


def find_drift(before: Snapshot, after: Snapshot) -> list[tuple[str, str]]:
    """List ``(path, reason)`` for files whose semantic signature changed.

    Returns the empty list when the two snapshots are semantically
    equivalent — class-name renumbering is collapsed by
    :func:`_file_signature`.
    """
    drifted: list[tuple[str, str]] = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            drifted.append((path, "file added"))
            continue
        if path not in after:
            drifted.append((path, "file removed"))
            continue
        if _file_signature(before[path]) != _file_signature(after[path]):
            drifted.append((path, "field set changed"))
    return drifted


def _cmd_diff(args: argparse.Namespace) -> int:
    before_path = Path(args.before)
    if not before_path.exists():
        print(f"Snapshot not found: {before_path}", file=sys.stderr)
        return 1
    before: Snapshot = json.loads(before_path.read_text(encoding="utf-8"))
    after = snapshot(Path(args.after_root))
    report = format_diff(before, after)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Wrote delta report to {args.output}")
    else:
        print(report)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    before_path = Path(args.before)
    if not before_path.exists():
        print(f"Snapshot not found: {before_path}", file=sys.stderr)
        return 1
    before: Snapshot = json.loads(before_path.read_text(encoding="utf-8"))
    after = snapshot(Path(args.after_root))
    drifted = find_drift(before, after)
    if not drifted:
        print("✓ No semantic drift (class-name renumbering ignored).")
        return 0

    sys.stderr.write(format_diff(before, after))
    sys.stderr.write("\n")
    sys.stderr.write(f"\n✗ {len(drifted)} file(s) drift between committed tree and regen output:\n")
    for path, reason in drifted[:50]:
        sys.stderr.write(f"  - {path}  ({reason})\n")
    if len(drifted) > 50:
        sys.stderr.write(f"  ... {len(drifted) - 50} more\n")
    sys.stderr.write(
        "\nThe committed src/adcp/types/generated_poc/ does not match what\n"
        "scripts/generate_types.py produces from schemas/cache/. Either:\n"
        "  - re-run sync + regen and commit the result:\n"
        "      python scripts/sync_schemas.py && python scripts/generate_types.py\n"
        "  - or, if upstream changed, bump src/adcp/ADCP_VERSION and regen\n"
        "  - and never hand-edit generated_poc/ — see scripts/post_generate_fixes.py\n"
        "    for the sanctioned post-regen patch path.\n"
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="Capture per-class field snapshot to JSON.")
    p_snap.add_argument("root", help="generated_poc/ directory to walk")
    p_snap.add_argument("output", help="JSON file to write")
    p_snap.set_defaults(func=_cmd_snapshot)

    p_diff = sub.add_parser("diff", help="Diff a JSON snapshot against a current tree.")
    p_diff.add_argument("before", help="JSON snapshot from `snapshot` command")
    p_diff.add_argument("after_root", help="generated_poc/ directory to walk")
    p_diff.add_argument("--output", help="Write markdown report to this path (default: stdout)")
    p_diff.set_defaults(func=_cmd_diff)

    p_check = sub.add_parser(
        "check",
        help=(
            "Strict CI gate: exit non-zero if a tree's per-file field signature "
            "differs from a JSON snapshot. Class-name renumbering is ignored."
        ),
    )
    p_check.add_argument("before", help="JSON snapshot from `snapshot` command")
    p_check.add_argument("after_root", help="generated_poc/ directory to walk")
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args()
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
