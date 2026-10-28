"""Record every document that reaches a root constraint, without touching the subject.

An opt-in pytest plugin. It arms PEP 669 local monitoring events
(``sys.monitoring``) on the code objects the compiled-condition vocabulary
already carries and reads the first argument out of the monitored frame.
Nothing in ``adcp.types._conditions`` is rebound, so identity, ``__module__``,
``__name__``, signature and ``__all__`` are the ones an uninstrumented run
sees, and the artifact carries that as a re-measured verdict rather than a
claim (see ``instrument_report``).

Observation rather than replacement, because a recording wrapper IS a change to
the module under test: the predecessor ``setattr``-ed one over all 18 primitives
and ``tests/test_conditions_vocabulary.py``, which derives the primitive list by
filtering ``vars(_conditions)`` on ``__module__``, then found none and failed for
a reason unrelated to the code under test. ``functools.wraps`` hides that symptom
and leaves a distinct function object, which a comparison of identities still
sees. salesagent-8qela.17.

TWO CAPTURE POINTS, because the series enforces root content two ways and one
hook sees one of them:

* a FLAT root carries a compiled ``model_validator`` that calls the vocabulary
  with the model as its first argument -- PY_START on each primitive's code
  object sees those. Measured at this base: 223 of 226 generated validators call
  the vocabulary, and the three that do not compiled to constants
  (``not (False or False or False)``) because their conjunct forbids fields the
  model does not declare, so ``extra="forbid"`` refuses and no document reaches
  them;
* a SPLIT root validates through the ``WrapValidator`` the emitter attaches to
  its branches' union -- PY_START on the ``_wrap`` closure inside
  ``name_the_combinator`` sees those. All 13 split roots pass through that one
  closure, so one code object covers every one of them.

A flat root's document is reconstructed with ``model_dump(exclude_unset=True)``:
the compiled validator runs in ``mode="after"``, so the raw input is gone by
then, and this is the document as the model understood it. A split root's
document is the raw input, which the union's wrapper still holds.

KNOWN DEPENDENCY: the closure's name. ``name_the_combinator`` must contain
exactly one nested code object named ``_wrap``, and :func:`split_capture_point`
refuses at import if it does not. Depending on a production function's internal
name is the price of not putting a named observation hook in production, and a
loud local refusal is the cheaper half of that trade.

LOAD IT WITH ``-p``, NEVER FROM ``conftest.py``. Auto-loading would make every
suite run an instrumented run, and an instrumented run is a different
configuration: a plain run differenced against one is a configuration delta, not
a code delta, which is the provenance problem the capture corpus's manifest
exists to solve. ``python -m pytest`` puts the invocation directory on
``sys.path``, which is what makes the module name importable at plugin-load
time::

    ROOT_DOCUMENT_CAPTURE=/path/to/rootdocs.json \\
        python -m pytest -p tests.capture_root_documents <selection>

A SPLIT RECORD'S ``names`` ARE READ AT ENTRY: all of a root's branch class names,
from the ``branches`` the wrapper closes over, rather than the one branch that
matched. Naming the matched branch needs the outcome, and the outcome needs
PY_UNWIND, which cannot be armed on one code object -- only globally, over every
unwinding frame in the process. Recording at entry also records a document that
satisfies NO branch, which leaves ``_wrap`` as a ``PydanticCustomError`` -- a
``ValueError``, not a ``ValidationError``. ``names`` is descriptive and is not
part of the dedup key, so a record present in two corpora carries the same
document in both either way.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import adcp.types._conditions as conditions

if not hasattr(sys, "monitoring"):  # pragma: no cover - a refusal, not a branch
    raise RuntimeError(
        f"this instrument observes through PEP 669, which is CPython 3.12+, and "
        f"{sys.version.split()[0]} has no sys.monitoring. The alternative is a mechanism "
        "that rebinds the module under test, which is what this one replaced."
    )

E = sys.monitoring.events

#: Tool ids 0, 1, 2 and 5 are spoken for by name -- debugger, coverage, profiler,
#: optimizer -- and ``sys.settrace`` is built on the debugger id, so a tool that
#: must coexist with coverage takes one of the two unreserved ids.
_CANDIDATE_TOOL_IDS = (3, 4)

#: The environment variable naming the artifact. There is no default: an unset
#: path means the operator did not say where the corpus goes, and a silent
#: fallback is how one lane's corpus overwrites another's.
_ARTIFACT_ENV = "ROOT_DOCUMENT_CAPTURE"


def split_capture_point() -> types.CodeType:
    """The ``_wrap`` closure inside ``name_the_combinator``.

    Read off the primitive's own constants, so it is the object a running union
    executes rather than a re-derivation that could name a different function.
    Refuses unless there is exactly one: zero or several means the primitive has
    been restructured and the split capture point is no longer identified, which
    has to be found out here and not in a corpus missing every split root.
    """
    candidates = [
        const
        for const in conditions.name_the_combinator.__code__.co_consts
        if isinstance(const, types.CodeType) and const.co_name == "_wrap"
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"name_the_combinator carries {len(candidates)} nested '_wrap' code objects, "
            "so the split capture point cannot be identified"
        )
    return candidates[0]


def flat_capture_points() -> dict[types.CodeType, str]:
    """Every public primitive the vocabulary defines, keyed by code object.

    Derived from the module rather than from ``__all__``, because a primitive can
    be defined before it is exported and the predecessor iterated ``__all__`` and
    so never saw ``members``. Refuses on an empty derivation, which would arm
    nothing and report a clean empty corpus, and on an exported primitive the
    derivation missed, which would be a hole in a capture point that looks whole.
    """
    found = {
        value.__code__: name
        for name, value in vars(conditions).items()
        if not name.startswith("_")
        and isinstance(value, types.FunctionType)
        and value.__module__ == conditions.__name__
    }
    if not found:
        raise RuntimeError(
            f"no primitives found in {conditions.__name__}: the derivation would arm nothing"
        )
    exported = {
        name
        for name in getattr(conditions, "__all__", ())
        if isinstance(getattr(conditions, name, None), types.FunctionType)
    }
    missing = exported - set(found.values())
    if missing:
        raise RuntimeError(f"exported primitives the derivation missed: {sorted(missing)}")
    return found


_WRAP_CODE = split_capture_point()
_PRIMITIVES = flat_capture_points()

#: What the subject looked like before anything was armed, so the "nothing was
#: rebound" verdict is a comparison and not an assertion. Taken at import, which
#: is before any arming: ``install`` is called from ``pytest_configure``.
_SUBJECT_BEFORE = {
    name: (id(value), getattr(value, "__module__", None), getattr(value, "__name__", None))
    for name, value in vars(conditions).items()
    if isinstance(value, types.FunctionType)
}

_SEEN: dict[str, dict[str, Any]] = {}
_OUT: Path | None = None
_TOOL_ID: int | None = None

#: The item running right now, so a refused document names the test that built
#: it; without it a classifier reports a document and no way to reach the fixture.
_NODEID = ""

#: Observation faults, reported in the artifact rather than raised: a callback
#: that raises propagates into the monitored frame and changes the run, which is
#: the one thing this instrument exists to avoid. A fault is counted and the pass
#: continues with a record of how blind it was.
_FRAME_MISMATCHES = 0
_CALLBACK_FAULTS = 0

#: ``_record`` must not re-enter the callback. Nothing it calls reaches an armed
#: code object today -- serialization is not validation -- and this keeps that
#: from becoming silent recursion if it ever does.
_RECORDING = False


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _record(document: Any, source: str, names: list[str]) -> None:
    if not isinstance(document, dict) or not document:
        return
    try:
        payload = _jsonable(document)
        key = f"{source}:{json.dumps(payload, sort_keys=True)}"
    except (TypeError, ValueError):
        return
    _SEEN.setdefault(
        key,
        {
            "source": source,
            "names": sorted(set(names)),
            "document": payload,
            "nodeid": _NODEID,
        },
    )


def _on_start(code: types.CodeType, instruction_offset: int) -> None:  # noqa: ARG001
    """A monitored frame has started, with its parameters already bound.

    ``sys._getframe(1)`` is that frame: the callback runs at the monitored
    instruction, so the frame below it is the one that started. The identity
    check keeps a wrong reading from being recorded as a document -- a mismatch
    is counted into the artifact instead.
    """
    global _FRAME_MISMATCHES, _CALLBACK_FAULTS, _RECORDING
    if _RECORDING:
        return
    try:
        frame = sys._getframe(1)
        if frame.f_code is not code:
            _FRAME_MISMATCHES += 1
            return
        local = frame.f_locals
        _RECORDING = True
        try:
            if code is _WRAP_CODE:
                branches = local.get("branches") or ()
                _record(
                    local.get("value"),
                    "split",
                    [getattr(branch, "__name__", str(branch)) for branch in branches]
                    or [str(local.get("keyword"))],
                )
                return
            names = code.co_varnames[: code.co_argcount]
            subject = local.get(names[0]) if names else None
            if isinstance(subject, BaseModel):
                _record(
                    subject.model_dump(exclude_unset=True, mode="json"),
                    "flat",
                    [type(subject).__name__],
                )
        finally:
            _RECORDING = False
    except Exception:  # noqa: BLE001 - an observation fault must not fail the test
        _CALLBACK_FAULTS += 1
        _RECORDING = False


def install(out: Path) -> int:
    """Arm both capture points and return the monitoring tool id.

    Separate from import, so importing this module instruments nothing: a test
    that imports it to grade it would otherwise arm monitoring for the rest of
    the session.
    """
    global _OUT, _TOOL_ID
    if _TOOL_ID is not None:
        raise RuntimeError(f"already installed on tool id {_TOOL_ID}")
    for tool_id in _CANDIDATE_TOOL_IDS:
        if sys.monitoring.get_tool(tool_id) is None:
            break
    else:
        raise RuntimeError(
            "no unreserved sys.monitoring tool id is free: "
            + ", ".join(f"{i}={sys.monitoring.get_tool(i)}" for i in _CANDIDATE_TOOL_IDS)
        )
    sys.monitoring.use_tool_id(tool_id, "root-document-capture")
    sys.monitoring.register_callback(tool_id, E.PY_START, _on_start)
    for code in (*_PRIMITIVES, _WRAP_CODE):
        sys.monitoring.set_local_events(tool_id, code, E.PY_START)
    _OUT, _TOOL_ID = Path(out), tool_id
    return tool_id


def uninstall() -> None:
    """Disarm every capture point and release the tool id."""
    global _TOOL_ID
    if _TOOL_ID is None:
        return
    for code in (*_PRIMITIVES, _WRAP_CODE):
        sys.monitoring.set_local_events(_TOOL_ID, code, 0)
    sys.monitoring.register_callback(_TOOL_ID, E.PY_START, None)
    sys.monitoring.free_tool_id(_TOOL_ID)
    _TOOL_ID = None


def records() -> list[dict[str, Any]]:
    """The documents captured so far, deduped on source plus document."""
    return list(_SEEN.values())


def _subject_changes() -> list[str]:
    changed = []
    now = vars(conditions)
    for name, before in _SUBJECT_BEFORE.items():
        value = now.get(name)
        if value is None:
            changed.append(f"{name}: gone")
            continue
        after = (id(value), getattr(value, "__module__", None), getattr(value, "__name__", None))
        if after != before:
            changed.append(f"{name}: {before} -> {after}")
    return changed


def instrument_report() -> dict[str, Any]:
    """What the instrument did, and whether its subject is still intact.

    ``subject_unchanged`` compares identity as well as ``__module__`` and
    ``__name__``, so a ``functools.wraps``-transparent rebind -- which restores
    both names and leaves a different function object -- loses the claim.
    """
    changed = _subject_changes()
    return {
        "mechanism": "sys.monitoring PY_START local events (PEP 669)",
        "tool_id": _TOOL_ID,
        "armed_primitives": sorted(_PRIMITIVES.values()),
        "armed_split_point": f"{conditions.name_the_combinator.__qualname__}.<locals>._wrap",
        "subject_unchanged": not changed,
        "subject_changes": changed,
        "frame_mismatches": _FRAME_MISMATCHES,
        "callback_faults": _CALLBACK_FAULTS,
    }


# ---- the artifact, written as the run proceeds ----------------------------
# Not at session finish: a pass that is killed, times out, or dies on a
# contended box would then produce no file at all rather than a partial one. The
# run is dominated by test execution, so rewriting the records every 25 items
# costs nothing measurable.
_FLUSH_EVERY = 25
_COLLECTED = 0
_COMPLETED = 0


def flush() -> None:
    """Write the artifact. Refuses before :func:`install` has named a path."""
    if _OUT is None:
        raise RuntimeError(f"no artifact path: {_ARTIFACT_ENV} was never read")
    captured = records()
    split = sum(1 for record in captured if record["source"] == "split")
    _OUT.write_text(
        json.dumps(
            {
                "complete": _COLLECTED > 0 and _COMPLETED >= _COLLECTED,
                "items_collected": _COLLECTED,
                "items_completed": _COMPLETED,
                "instrument": instrument_report(),
                "documents": captured,
                "by_source": {"split": split, "flat": len(captured) - split},
            },
            indent=1,
        )
    )


def pytest_configure(config: Any) -> None:  # noqa: ARG001
    destination = os.environ.get(_ARTIFACT_ENV)
    if not destination:
        # UsageError, not RuntimeError: pytest renders this one as a usage
        # message, where an exception from pytest_configure arrives as
        # INTERNALERROR and reads like a crash in the plugin.
        raise pytest.UsageError(
            f"{_ARTIFACT_ENV} is unset, so there is nowhere to write the corpus. "
            "Name the artifact explicitly; a default path silently overwrites "
            "another lane's capture."
        )
    install(Path(destination))


def pytest_collection_modifyitems(
    session: Any, config: Any, items: list[Any]
) -> None:  # noqa: ARG001
    global _COLLECTED
    _COLLECTED = len(items)
    flush()


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:  # noqa: ARG001
    global _NODEID
    _NODEID = nodeid


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:  # noqa: ARG001
    global _COMPLETED
    _COMPLETED += 1
    if _COMPLETED % _FLUSH_EVERY == 0:
        flush()


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:  # noqa: ARG001
    flush()
    captured = records()
    split = sum(1 for record in captured if record["source"] == "split")
    report = instrument_report()
    print(
        f"\n[capture] {len(captured)} distinct documents "
        f"({split} split-root, {len(captured) - split} flat-root) "
        f"from {_COMPLETED} of {_COLLECTED} items -> {_OUT}"
    )
    print(
        f"[capture] subject unchanged: {report['subject_unchanged']}"
        f"{'' if report['subject_unchanged'] else ' ' + str(report['subject_changes'])}"
        f"; frame mismatches {report['frame_mismatches']}"
        f"; callback faults {report['callback_faults']}"
    )


def pytest_unconfigure(config: Any) -> None:  # noqa: ARG001
    uninstall()
