#!/usr/bin/env python3
"""Resolve the pinned bundle's task messages to their generated root classes.

ONE home for that resolution, with three consumers: the marker-insertion pass in
``post_generate_fixes.py``, the ratchet in ``tests/test_task_message_lattice.py``, and the
``--package`` measurement below, which the adoption run uses to re-measure the ancestry
counts against an installed wheel instead of trusting a number in a report.

Run it against whatever ``adcp`` is importable:

    python scripts/task_message_lattice.py --package

or against a repo checkout's generated tree:

    python scripts/task_message_lattice.py

It needs only ``adcp``'s shipped ``_schemas/<bundle>/index.json`` and the generated
modules, both of which are in the wheel -- so copying this one file next to a venv that has
the wheel installed is enough to reproduce the table.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import sys
import typing

try:
    from generated_names import generated_root_class_name
except ModuleNotFoundError:  # Imported as ``scripts.task_message_lattice`` in tests.
    from scripts.generated_names import generated_root_class_name  # type: ignore[assignment]
from pathlib import Path

_CANONICAL_REF = re.compile(r"^https://adcontextprotocol\.org/schemas/[^/]+/(.+)$")


def registry_ref_to_path(ref: str) -> Path:
    """The schema path a task-registry ``$ref`` names.

    Narrower than ``post_generate_fixes._resolve_schema_ref`` on purpose: registry refs are
    always absolute, so a relative one means the registry's shape changed and that should
    stop the build rather than be resolved against a guessed base.
    """
    file_ref = ref.split("#", 1)[0]
    canonical = _CANONICAL_REF.match(file_ref)
    if canonical:
        return Path(canonical.group(1))
    if file_ref.startswith("/schemas/"):
        return Path(file_ref.removeprefix("/schemas/"))
    raise RuntimeError(f"task registry $ref is not absolute: {ref!r}")


def task_registry_messages(schema_dir: Path) -> list[tuple[str, str, Path]]:
    """Every task message in the pinned bundle: (task, "request"|"response", schema path).

    The registry in ``index.json`` is the authority for what IS a task message. A filename
    pattern is not: 10 of the 87 ``*-request.json`` files are components or para-protocol
    schemas that no task names, and treating those as task messages would claim registry
    membership the bundle does not grant.
    """
    index = json.loads((schema_dir / "index.json").read_text())
    messages: list[tuple[str, str, Path]] = []
    for domain, entry in index["schemas"].items():
        for task, refs in (entry.get("tasks") or {}).items():
            for kind in ("request", "response"):
                messages.append(
                    (f"{domain}.{task}", kind, registry_ref_to_path(refs[kind]["$ref"]))
                )
    return messages


def generated_module_by_schema(output_dir: Path) -> dict[Path, Path]:
    """Map each schema to the module generated from it, by the codegen ``filename:`` header.

    The header is the generator's own statement of provenance, which is why it is read
    instead of recomputed: a path convention would have to re-derive the hyphen conversion,
    the directory layout and the root-discovery special cases.
    """
    modules: dict[Path, Path] = {}
    for path in sorted(output_dir.rglob("*.py")):
        if "bundled" in path.parts:
            continue
        for line in path.read_text().splitlines()[:6]:
            if line.startswith("#   filename:"):
                modules[Path(line.split(":", 1)[1].strip())] = path
                break
    return modules


def underscored(path: Path) -> Path:
    return Path(*(part.replace("-", "_") for part in path.parts))


def module_top_level(tree: ast.Module) -> tuple[dict[str, ast.ClassDef], dict[str, ast.expr]]:
    """Top-level classes and top-level assignments (the union aliases) of one module."""
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    aliases: dict[str, ast.expr] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        if names and node.value is not None:
            for name in names:
                aliases[name] = node.value
    return classes, aliases


def _alias_branches_expr(node: ast.expr) -> ast.expr:
    """The type an alias names, with any ``Annotated[...]`` metadata dropped.

    A composing root is emitted as ``Annotated[A | B, Field(...)]``, so the branches are
    the first subscript element and nothing else. Walking the whole expression reads
    ``Annotated`` -- and the ``Field`` call beside it -- as union branches, and then fails
    closed on the first one because no module defines a class by that name.
    """
    while (
        isinstance(node, ast.Subscript)
        and (
            (isinstance(node.value, ast.Name) and node.value.id == "Annotated")
            or (isinstance(node.value, ast.Attribute) and node.value.attr == "Annotated")
        )
        and isinstance(node.slice, ast.Tuple)
        and node.slice.elts
    ):
        node = node.slice.elts[0]
    return node


def root_class_names(schema_rel: Path, module: Path, tree: ast.Module, root: str) -> list[str]:
    """The class(es) a task message can validate into, in the module that defines them.

    A plain root is one class. A union root is a type alias, and the alias is not a class --
    so the marker lands on each member branch, which is what makes
    ``isinstance(resp, AdcpResponse)`` hold for every value the task can answer with.
    Nested aliases are followed. Fails closed: a root that resolves to neither a class nor
    an alias, or an alias naming something this module does not define, is an error with
    the schema named.
    """
    classes, aliases = module_top_level(tree)
    if root in classes:
        return [root]
    if root not in aliases:
        raise RuntimeError(
            f"{schema_rel.as_posix()}: generated module {module.name} defines neither a "
            f"class nor a type alias named {root!r}"
        )
    pending = [
        node.id
        for node in ast.walk(_alias_branches_expr(aliases[root]))
        if isinstance(node, ast.Name)
    ]
    resolved: list[str] = []
    seen: set[str] = set()
    while pending:
        name = pending.pop(0)
        if name in seen:
            continue
        seen.add(name)
        if name in classes:
            resolved.append(name)
        elif name in aliases:
            pending.extend(
                node.id
                for node in ast.walk(_alias_branches_expr(aliases[name]))
                if isinstance(node, ast.Name)
            )
        else:
            raise RuntimeError(
                f"{schema_rel.as_posix()}: union branch {name!r} of {root} is not defined in "
                f"{module.name}; the marker pass cannot reach it (one mechanism only)"
            )
    if not resolved:
        raise RuntimeError(f"{schema_rel.as_posix()}: {root} resolved to no branch classes")
    return resolved


def task_message_roots(
    schema_dir: Path, output_dir: Path
) -> list[tuple[str, str, Path, Path, list[str]]]:
    """(task, kind, schema path, module path, root class names) for every task message."""
    modules = generated_module_by_schema(output_dir)
    rows = []
    for task, kind, schema_rel in task_registry_messages(schema_dir):
        module = modules.get(underscored(schema_rel))
        if module is None:
            raise RuntimeError(
                f"{task} {kind}: no generated module declares filename "
                f"{underscored(schema_rel).as_posix()}"
            )
        title = json.loads((schema_dir / schema_rel).read_text()).get("title")
        if not isinstance(title, str) or not title.strip():
            raise RuntimeError(f"{schema_rel.as_posix()}: no title to derive a root class from")
        tree = ast.parse(module.read_text())
        rows.append(
            (
                task,
                kind,
                schema_rel,
                module,
                root_class_names(schema_rel, module, tree, generated_root_class_name(title)),
            )
        )
    return rows


# --------------------------------------------------------------------------------------
# Measurement (``--package``): import the classes and report the ancestry at task scope.
# --------------------------------------------------------------------------------------


def _branches(obj: object) -> list[type]:
    """A union alias's member classes, or the single class itself."""
    members = list(typing.get_args(obj)) or [obj]
    return [m for m in members if isinstance(m, type)]


def measure(schema_dir: Path, output_dir: Path, package_root: Path) -> int:
    """Print the ancestry table and return a process exit code.

    Ancestry means EVERY root class the message can validate into descends from the
    ancestor -- all branches, not any branch. A union whose submitted branch lacks the envelope
    cannot answer the envelope question for every value the task returns, so counting it as
    composed overstates what a caller may rely on.
    """
    from adcp.types.domains.core.protocol_envelope import ProtocolEnvelope
    from adcp.types.domains.core.version_envelope import AdcpVersionEnvelope

    try:
        from adcp.types.base import AdcpRequest, AdcpResponse

        markers: dict[str, type | None] = {"request": AdcpRequest, "response": AdcpResponse}
    except ImportError:
        print("adcp.types.base exposes no AdcpRequest/AdcpResponse (pre-lattice wheel)")
        markers = {"request": None, "response": None}

    rows = task_message_roots(schema_dir, output_dir)
    resolved: list[tuple[str, str, str, list[type]]] = []
    for task, kind, schema_rel, module, names in rows:
        dotted = ".".join(module.relative_to(package_root).with_suffix("").parts)
        imported = importlib.import_module(dotted)
        classes = [c for name in names for c in _branches(getattr(imported, name))]
        resolved.append((task, kind, schema_rel.as_posix(), classes))

    failures = 0
    print(f"task messages resolved: {len(resolved)} (77 tasks x request + response)\n")
    for kind in ("request", "response"):
        subset = [r for r in resolved if r[1] == kind]
        print(f"=== task {kind}s, n={len(subset)} ===")
        checks = [
            (f"Adcp{kind.capitalize()}", markers[kind]),
            ("AdcpVersionEnvelope", AdcpVersionEnvelope),
        ]
        if kind == "response":
            # The protocol envelope wraps responses. On a request it is expected absent,
            # and listing 77 "residue" lines for that would bury the two real numbers.
            checks.append(("ProtocolEnvelope", ProtocolEnvelope))
        for label, ancestor in checks:
            if ancestor is None:
                print(f"  {label:<22} n/a (absent from this wheel)")
                continue
            residue = sorted(
                {s for _, _, s, cs in subset if not all(issubclass(c, ancestor) for c in cs)}
            )
            print(f"  {label:<22} {len(subset) - len(residue)}/{len(subset)}")
            for schema in residue:
                print(f"       residue: {schema}")
    unmarked = [
        s
        for _, kind, s, cs in resolved
        if markers[kind] is not None and not all(issubclass(c, markers[kind]) for c in cs)
    ]
    if unmarked:
        failures += len(unmarked)
        print(f"\nFAIL: {len(unmarked)} task message(s) carry no marker")
    return 1 if failures else 0


def _package_roots() -> tuple[Path, Path, Path]:
    """Schema dir, generated tree and package parent, read off the IMPORTED adcp package."""
    import adcp
    from adcp.validation.version import resolve_bundle_key

    package = Path(adcp.__file__).parent
    bundle = resolve_bundle_key((package / "ADCP_VERSION").read_text().strip())
    return package / "_schemas" / bundle, package / "types" / "domains", package.parent


def _repo_roots() -> tuple[Path, Path, Path]:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "src"))
    from adcp.validation.version import resolve_bundle_key

    bundle = resolve_bundle_key((repo / "src" / "adcp" / "ADCP_VERSION").read_text().strip())
    return (
        repo / "schemas" / "cache" / bundle,
        repo / "src" / "adcp" / "types" / "domains",
        repo / "src",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package",
        action="store_true",
        help="measure the installed adcp package (a wheel) instead of a repo checkout",
    )
    args = parser.parse_args(argv)
    schema_dir, output_dir, package_root = _package_roots() if args.package else _repo_roots()
    print(f"schemas: {schema_dir}\ngenerated: {output_dir}\n")
    return measure(schema_dir, output_dir, package_root)


if __name__ == "__main__":
    raise SystemExit(main())
