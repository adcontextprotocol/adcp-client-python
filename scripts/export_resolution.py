#!/usr/bin/env python3
"""What each exported name resolves to, as a comparable key.

``tests/fixtures/public_api_snapshot.json`` records these keys so
``tests/test_public_api.py`` can fail when a name starts meaning a different
object. A set of names cannot: ``_generated`` resolves a bare type name that
several generated modules define by sort order and stem preference, so a schema
addition can silently take a name an adopter already imports, and the only
trace is a line in a regenerated file.

The keys are derived from the live objects, so nothing here is enumerated and a
new export is covered the moment it exists.
"""

from __future__ import annotations

import inspect
import types
import typing

__all__ = ["expand_key", "resolution_key", "snapshot_entry", "snapshot_modules"]


#: Namespaces the snapshot covers: the two public surfaces an adopter imports
#: from, the consolidated generated namespace behind them, and every generated
#: domain module. Built on first use so this module imports nothing from
#: ``adcp`` until asked.
#:
#: ``error_details`` is deliberately absent. Every name it carries is a class
#: its own domain module also exports, and
#: ``tests/test_export_surface_is_derived.py`` asserts the identity against the
#: generated class from the tree. A snapshot of it would record what a derived
#: test already proves.
def snapshot_modules() -> tuple[tuple[str, str], ...]:
    """The namespaces to snapshot, in a stable order."""
    import adcp.types.domains

    return (
        ("adcp", "adcp"),
        ("adcp.types", "adcp.types"),
        ("adcp.types._generated", "adcp.types._generated"),
        *(
            (f"adcp.types.domains.{domain}", f"adcp.types.domains.{domain}")
            for domain in adcp.types.domains.DOMAINS
        ),
    )


#: Stripped from a key for brevity. Every generated class lives under it.
_GENERATED_PREFIX = "adcp.types.generated_poc."


def resolution_key(obj: object) -> str:
    """A stable, comparable description of what a name is bound to.

    A class or function is its defining module and qualname — the pair that
    changes when a name repoints. A submodule is its own path. A union is the
    sorted keys of its members, so a union gaining, losing or repointing an arm
    shows up too. Anything else falls back to its type, which tracks presence
    without inventing a value that churns between runs.
    """
    if inspect.ismodule(obj):
        return f"module:{obj.__name__}"
    # Parameterized aliases first: on Python 3.10 ``isinstance(list[int], type)``
    # is ``True``, so ``inspect.isclass`` would key ``list[X]`` or
    # ``Callable[..., X]`` by its origin alone and the snapshot would disagree
    # between interpreter versions.
    if isinstance(obj, types.UnionType) or typing.get_origin(obj) is not None:
        args = typing.get_args(obj)
        return "union[" + ",".join(sorted(resolution_key(arg) for arg in args)) + "]"
    if inspect.isclass(obj) or inspect.isfunction(obj):
        module = getattr(obj, "__module__", "?")
        if module.startswith(_GENERATED_PREFIX):
            module = module[len(_GENERATED_PREFIX) :]
        return f"{module}:{obj.__qualname__}"
    if isinstance(obj, (str, int, float, bool, type(None))):
        return f"value:{obj!r}"
    return f"instance:{type(obj).__module__}:{type(obj).__qualname__}"


def snapshot_entry(module_path: str) -> dict[str, object]:
    """The snapshot record for one namespace: its names, and what they resolve to.

    ``names`` is the sorted ``__all__`` verbatim, duplicates included — the
    half the removal and addition checks have always read. ``resolves`` maps
    each name to its resolution key; a dict cannot carry a duplicate name, so
    the two halves are kept separately rather than one derived from the other.

    A name in ``__all__`` that does not resolve is recorded as ``"<missing>"``
    rather than skipped: a promise the module stops keeping is the kind of
    change the snapshot exists to catch.
    """
    import importlib

    module = importlib.import_module(module_path)
    resolves: dict[str, str] = {}
    for name in sorted(module.__all__):
        try:
            key = resolution_key(getattr(module, name))
        except AttributeError:
            key = "<missing>"
        # The common case by far is a class whose own name is the exported one.
        # Dropping the redundant ``:<name>`` suffix keeps the file readable and
        # roughly halves it; ``expand_key`` puts it back.
        resolves[name] = key[: -len(name) - 1] if key.endswith(f":{name}") else key
    duplicates = sorted({n for n in module.__all__ if module.__all__.count(n) > 1})
    entry: dict[str, object] = {"resolves": resolves}
    if duplicates:
        # ``__all__`` is a list and may repeat a name; a dict cannot. Recording
        # the repeats keeps a NEW duplicate visible in the diff.
        entry["duplicate_names"] = duplicates
    return entry


def expand_key(name: str, stored: str) -> str:
    """Undo ``snapshot_entry``'s suffix elision for comparison and messages."""
    if stored.startswith(("module:", "union[", "value:", "instance:", "<")) or ":" in stored:
        return stored
    return f"{stored}:{name}"
