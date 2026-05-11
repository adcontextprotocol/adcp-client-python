"""JSON Schema loader for AdCP tool request/response validation.

Loads the bundled per-tool schemas shipped with the SDK plus the ``core/``
schemas that async response variants ``$ref``, then compiles validators
lazily by ``(tool_name, direction)``.

Schemas live under a per-version bundle key (see
:func:`adcp.validation.version.resolve_bundle_key`) so multiple AdCP spec
versions can coexist on disk. The loader today resolves the cache for the
SDK's pinned ``ADCP_VERSION``; the per-request version arg lands in
Stage 2.

Discovery paths (first hit wins):

* **Installed package** — ``importlib.resources.files("adcp") / "_schemas"
  / {bundle_key}`` populated by ``scripts/bundle_schemas.py`` before wheel
  build.
* **Dev checkout** — ``<repo>/schemas/cache/{bundle_key}/`` (where
  ``scripts/sync_schemas.py`` writes the canonical bundle). Tried when the
  packaged copy is absent, so editable installs against a fresh clone
  validate against the repo's schemas.
"""

from __future__ import annotations

import json
import logging
import threading
import warnings
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Literal

from adcp.validation.version import resolve_bundle_key

logger = logging.getLogger(__name__)

# Serialize first-time init and validator compilation. Concurrent callers
# on a fresh process can otherwise both walk the schema tree or compile
# the same validator twice. Result is the same either way, but the lock
# keeps behaviour deterministic and avoids redundant filesystem walks.
_init_lock = threading.Lock()
_compile_lock = threading.Lock()

ResponseVariant = Literal["sync", "submitted", "working", "input-required"]
Direction = Literal["request", "sync", "submitted", "working", "input-required"]


class _SchemaRoot:
    """Filesystem view of the schema tree, regardless of packaged vs dev."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bundled = root / "bundled"
        self.core = root / "core"

    def exists(self) -> bool:
        return self.bundled.is_dir()


def _sdk_pinned_bundle_key() -> str:
    """Bundle key for the SDK's compile-time-pinned AdCP version.

    Reads the packaged ``ADCP_VERSION`` file and collapses it via
    :func:`resolve_bundle_key`. Cached at import time so the lookup
    happens once.
    """
    from adcp._version import _read_packaged_version

    return resolve_bundle_key(_read_packaged_version())


def _resolve_schema_root(bundle_key: str | None = None) -> _SchemaRoot | None:
    """Locate the schema tree for ``bundle_key`` (default: SDK pin).

    Packaged copy wins; falls back to repo layout for editable installs.
    Returns ``None`` when neither location is populated — the validator
    degrades to ``skipped`` for every tool, matching the TS behavior for
    tools outside the AdCP catalog.
    """
    key = bundle_key if bundle_key is not None else _sdk_pinned_bundle_key()
    try:
        packaged = files("adcp") / "_schemas" / key
        with as_file(packaged) as p:
            packaged_path = Path(p)
            if (packaged_path / "bundled").is_dir():
                return _SchemaRoot(packaged_path)
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass

    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "schemas" / "cache" / key
        if (candidate / "bundled").is_dir():
            return _SchemaRoot(candidate)
        if ancestor.parent == ancestor:
            break
    return None


class _LoaderState:
    def __init__(self, root: _SchemaRoot) -> None:
        self.root = root
        self.file_index: dict[tuple[str, Direction], Path] = {}
        self.compiled: dict[tuple[str, Direction], Any] = {}
        self.registry: dict[str, dict[str, Any]] = {}
        self._core_loaded = False


_state: _LoaderState | None = None


def _walk_json(dir_: Path) -> list[Path]:
    if not dir_.is_dir():
        return []
    return sorted(p for p in dir_.rglob("*.json") if p.is_file())


def _build_index(root: _SchemaRoot) -> dict[tuple[str, Direction], Path]:
    index: dict[tuple[str, Direction], Path] = {}

    for file in _walk_json(root.bundled):
        base = file.stem
        if base.endswith("-request"):
            tool = base[: -len("-request")].replace("-", "_")
            index[(tool, "request")] = file
        elif base.endswith("-response"):
            tool = base[: -len("-response")].replace("-", "_")
            index[(tool, "sync")] = file

    for entry in sorted(root.root.iterdir()):
        if not entry.is_dir() or entry.name in ("bundled", "core"):
            continue
        for file in _walk_json(entry):
            base = file.stem
            if base.endswith("-async-response-submitted"):
                tool = base[: -len("-async-response-submitted")].replace("-", "_")
                index[(tool, "submitted")] = file
            elif base.endswith("-async-response-working"):
                tool = base[: -len("-async-response-working")].replace("-", "_")
                index[(tool, "working")] = file
            elif base.endswith("-async-response-input-required"):
                tool = base[: -len("-async-response-input-required")].replace("-", "_")
                index[(tool, "input-required")] = file

    return index


def _ensure_state() -> _LoaderState | None:
    global _state
    if _state is not None:
        return _state
    with _init_lock:
        # Double-checked pattern: re-read inside the lock in case another
        # thread initialized while we were waiting.
        if _state is not None:
            return _state
        root = _resolve_schema_root()
        if root is None:
            logger.debug("AdCP schemas not found; schema validation will skip all tools")
            return None
        new_state = _LoaderState(root)
        new_state.file_index = _build_index(root)
        _state = new_state
        return _state


def _load_core_registry(state: _LoaderState) -> None:
    if state._core_loaded:
        return
    for file in _walk_json(state.root.core):
        try:
            schema = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load core schema %s: %s", file, exc)
            continue
        schema_id = schema.get("$id")
        if isinstance(schema_id, str):
            state.registry[schema_id] = schema
    state._core_loaded = True


def _make_ref_resolver(state: _LoaderState, base_file: Path, schema: dict[str, Any]) -> Any:
    """Build a jsonschema ``RefResolver`` rooted at the file's directory.

    Async variant schemas use relative refs like ``../core/context.json``;
    giving the resolver a ``file://`` base URI lets those resolve against
    disk. Also seeds the core ``$id``-keyed registry so bundled schemas
    that reference a core type by canonical id still resolve.

    Sets ``referrer=schema`` (not ``{}``) so fragment-only refs like
    ``#/$defs/MediaChannel`` inside the bundled per-tool schema resolve
    against the schema being validated. Without this, the resolver
    walks the empty-dict referrer and raises
    ``Unresolvable JSON pointer: '$defs/MediaChannel'`` on any
    bundled schema that uses internal ``$defs`` (every bundled
    capabilities-style schema does).

    ``RefResolver`` is deprecated in jsonschema 4.18+ (to be replaced by
    the ``referencing`` library). Suppress the warning locally so
    downstream projects running ``-W error::DeprecationWarning`` don't
    crash on import; migration tracked as a follow-up.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            from jsonschema import RefResolver
        except ImportError as exc:  # pragma: no cover - guarded by dep install
            raise RuntimeError(
                "jsonschema is required for AdCP schema validation. "
                "Install with: pip install 'jsonschema>=4.0.0'"
            ) from exc

        _load_core_registry(state)
        base_uri = base_file.resolve().parent.as_uri() + "/"
        return RefResolver(base_uri=base_uri, referrer=schema, store=dict(state.registry))


def get_validator(tool_name: str, direction: Direction) -> Any | None:
    """Return a compiled validator for ``(tool_name, direction)`` or ``None``.

    ``None`` means no schema ships for this pair — callers should skip
    validation (e.g., custom tools outside the AdCP catalog, or sync-only
    tools asked for an async variant that doesn't exist).
    """
    state = _ensure_state()
    if state is None:
        return None
    key = (tool_name, direction)
    cached = state.compiled.get(key)
    if cached is not None:
        return cached
    file = state.file_index.get(key)
    if file is None:
        return None
    try:
        schema = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load schema %s for %s: %s", file, key, exc)
        return None

    try:
        from jsonschema import Draft7Validator
        from jsonschema.exceptions import SchemaError
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "jsonschema is required for AdCP schema validation. "
            "Install with: pip install 'jsonschema>=4.0.0'"
        ) from exc

    with _compile_lock:
        # Re-check: another thread may have compiled the validator for
        # this key while we were loading the schema off disk.
        cached = state.compiled.get(key)
        if cached is not None:
            return cached
        try:
            resolver = _make_ref_resolver(state, file, schema)
            validator = Draft7Validator(schema, resolver=resolver)
        except SchemaError as exc:
            logger.warning("Invalid schema %s for %s: %s", file, key, exc)
            return None
        state.compiled[key] = validator
        return validator


def list_validator_keys() -> list[str]:
    """Every ``tool::direction`` pair with a shipped schema. Used by tests."""
    state = _ensure_state()
    if state is None:
        return []
    return sorted(f"{tool}::{direction}" for (tool, direction) in state.file_index)


def _reset_for_tests() -> None:
    """Clear cached state so a fresh resolve runs. Test-only."""
    global _state
    _state = None
