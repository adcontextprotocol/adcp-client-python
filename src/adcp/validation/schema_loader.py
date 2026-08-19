"""JSON Schema loader for AdCP tool request/response validation.

Loads the bundled per-tool schemas shipped with the SDK plus the ``core/``
schemas that async response variants ``$ref``, then compiles validators
lazily by ``(tool_name, direction, bundle_key)``.

Schemas live under a per-version bundle key (see
:func:`adcp.validation.version.resolve_bundle_key`) so multiple AdCP spec
versions can coexist. Callers pass an optional ``version`` to
:func:`get_validator`; ``None`` defaults to the SDK's compile-time pin
(``ADCP_VERSION``). Each bundle key gets its own ``_LoaderState`` — file
index, compiled validators, core registry — so cross-version traffic
doesn't share compilation state.

Discovery paths (first hit wins, per bundle key):

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
import re
import threading
import warnings
from copy import deepcopy
from datetime import datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

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


_RFC3339_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)


def _is_rfc3339_date_time(instance: Any) -> bool:
    """Return whether ``instance`` is an RFC3339 date-time string.

    ``jsonschema`` treats unknown formats as annotations. Its optional
    ``date-time`` checker is not always installed, but AdCP schemas use
    ``format: date-time`` inside ``oneOf`` branches, so the format must
    participate in validation for values like ``"asap"`` to select the
    intended branch.
    """
    if not isinstance(instance, str):
        return True
    if _RFC3339_DATE_TIME.fullmatch(instance) is None:
        return False
    normalized = instance[:-1] + "+00:00" if instance.endswith(("Z", "z")) else instance
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


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
    def __init__(self, root: _SchemaRoot, bundle_key: str) -> None:
        self.root = root
        self.bundle_key = bundle_key
        self.file_index: dict[tuple[str, Direction], Path] = {}
        self.source_index: dict[tuple[str, Direction], Path] = {}
        self.mcp_index: dict[tuple[str, Direction], Path] = {}
        self.compiled: dict[tuple[str, Direction], Any] = {}
        self.portable: dict[tuple[str, Direction], dict[str, Any]] = {}
        self.registry: dict[str, dict[str, Any]] = {}
        self._core_loaded = False


# Per-bundle-key state. Each version (``3.0``, ``2.5``, ``3.1.0-beta.1``)
# gets its own file index, compiled validator cache, and core registry —
# shared compilation state across versions would let a ``$ref`` from a
# v2.5 schema resolve to a v3.0 core type with the same ``$id``.
_states: dict[str, _LoaderState] = {}
# Negative cache: bundle keys we've already tried to resolve and found
# nothing on disk for. Distinguishes a true negative from "not yet looked
# up" so we don't walk the filesystem twice per missing version.
_state_misses: set[str] = set()


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
        if not entry.is_dir() or entry.name in ("bundled", "core", "mcp"):
            continue
        for file in _walk_json(entry):
            base = file.stem
            if base.endswith("-request"):
                tool = base[: -len("-request")].replace("-", "_")
                index.setdefault((tool, "request"), file)
            elif base.endswith("-response"):
                tool = base[: -len("-response")].replace("-", "_")
                index.setdefault((tool, "sync"), file)
            elif base.endswith("-async-response-submitted"):
                tool = base[: -len("-async-response-submitted")].replace("-", "_")
                index[(tool, "submitted")] = file
            elif base.endswith("-async-response-working"):
                tool = base[: -len("-async-response-working")].replace("-", "_")
                index[(tool, "working")] = file
            elif base.endswith("-async-response-input-required"):
                tool = base[: -len("-async-response-input-required")].replace("-", "_")
                index[(tool, "input-required")] = file

    return index


def _build_source_index(root: _SchemaRoot) -> dict[tuple[str, Direction], Path]:
    """Index modular request/response schemas before bundled duplication."""
    index: dict[tuple[str, Direction], Path] = {}
    for entry in sorted(root.root.iterdir()):
        if not entry.is_dir() or entry.name in ("bundled", "core", "mcp"):
            continue
        for file in _walk_json(entry):
            base = file.stem
            if base.endswith("-request"):
                tool = base[: -len("-request")].replace("-", "_")
                index.setdefault((tool, "request"), file)
            elif base.endswith("-response"):
                tool = base[: -len("-response")].replace("-", "_")
                index.setdefault((tool, "sync"), file)
    return index


def _build_mcp_index(root: _SchemaRoot) -> dict[tuple[str, Direction], Path]:
    """Index compact, self-contained schemas generated for MCP discovery."""
    index: dict[tuple[str, Direction], Path] = {}
    mcp_root = root.root / "mcp"
    files = _walk_json(mcp_root)
    for file in files:
        relative_parts = file.relative_to(mcp_root).parts
        if "profiles" not in relative_parts or "production" not in relative_parts:
            continue
        base = file.stem
        if base.endswith("-request"):
            tool = base[: -len("-request")].replace("-", "_")
            index[(tool, "request")] = file
        elif base.endswith("-response"):
            tool = base[: -len("-response")].replace("-", "_")
            index[(tool, "sync")] = file
    return index


def _resolve_bundle_key_for_version(version: str | None) -> str:
    """Resolve a caller-supplied version (or ``None``) to a bundle key."""
    if version is None:
        return _sdk_pinned_bundle_key()
    return resolve_bundle_key(version)


def _ensure_state(version: str | None = None) -> _LoaderState | None:
    """Return the loader state for ``version`` (default: SDK pin).

    Each bundle key is initialized once and cached for the process
    lifetime. ``None`` is returned when the bundle isn't on disk for
    this version — callers degrade to ``skipped`` validation, same as
    pre-Stage-2 behaviour when the cache is missing entirely.
    """
    bundle_key = _resolve_bundle_key_for_version(version)
    cached = _states.get(bundle_key)
    if cached is not None:
        return cached
    if bundle_key in _state_misses:
        return None
    with _init_lock:
        # Double-checked pattern: re-read inside the lock in case another
        # thread initialized while we were waiting.
        cached = _states.get(bundle_key)
        if cached is not None:
            return cached
        if bundle_key in _state_misses:
            return None
        root = _resolve_schema_root(bundle_key)
        if root is None:
            log_missing = logger.warning if version is None else logger.debug
            log_missing(
                "AdCP schemas not found for bundle_key=%s; validation will skip "
                "all tools for this version",
                bundle_key,
            )
            _state_misses.add(bundle_key)
            return None
        new_state = _LoaderState(root, bundle_key)
        new_state.file_index = _build_index(root)
        new_state.source_index = _build_source_index(root)
        new_state.mcp_index = _build_mcp_index(root)
        _states[bundle_key] = new_state
        return new_state


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


def get_validator(
    tool_name: str,
    direction: Direction,
    *,
    version: str | None = None,
) -> Any | None:
    """Return a compiled validator for ``(tool_name, direction, version)``.

    Returns ``None`` when no schema ships for this pair — callers should
    skip validation (e.g., custom tools outside the AdCP catalog, or
    sync-only tools asked for an async variant that doesn't exist, or a
    version whose bundle isn't on disk).

    ``version=None`` resolves to the SDK's compile-time pin
    (``ADCP_VERSION``). Pass a wire-version string (e.g. ``"3.0.7"``,
    ``"2.5"``, ``"3.1.0-beta.1"``) to validate against a non-current
    schema — :func:`adcp.validation.version.resolve_bundle_key` collapses
    it to the cache key.
    """
    state = _ensure_state(version)
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
        from jsonschema import Draft7Validator, FormatChecker
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
            format_checker = FormatChecker()
            format_checker.checks("date-time")(_is_rfc3339_date_time)
            validator = Draft7Validator(
                schema,
                resolver=resolver,
                format_checker=format_checker,
            )
        except SchemaError as exc:
            logger.warning("Invalid schema %s for %s: %s", file, key, exc)
            return None
        state.compiled[key] = validator
        return validator


def get_schema(
    tool_name: str,
    direction: Direction,
    *,
    version: str | None = None,
) -> dict[str, Any] | None:
    """Return a defensive copy of a bundled version-specific JSON Schema.

    This is the non-compiled counterpart to :func:`get_validator`. It powers
    version-scoped public models and MCP ``tools/list`` advertisement, both of
    which need the schema document itself rather than only a validator.
    """
    state = _ensure_state(version)
    if state is None:
        return None
    file = state.file_index.get((tool_name, direction))
    if file is None:
        return None
    try:
        schema = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Failed to load schema %s for %s::%s: %s",
            file,
            tool_name,
            direction,
            exc,
        )
        return None
    if not isinstance(schema, dict):
        logger.warning("Schema %s is not a JSON object", file)
        return None
    return deepcopy(schema)


def _reference_file(state: _LoaderState, current_file: Path, reference: str) -> Path:
    parsed = urlparse(reference)
    if parsed.scheme:
        marker = "/schemas/"
        if marker not in parsed.path:
            raise ValueError(f"unsupported external schema reference: {reference}")
        version_and_path = parsed.path.split(marker, 1)[1]
        _, separator, relative_path = version_and_path.partition("/")
        if not separator:
            raise ValueError(f"schema reference has no document path: {reference}")
        return state.root.root / unquote(relative_path)
    return (current_file.parent / unquote(parsed.path)).resolve()


def get_portable_schema(
    tool_name: str,
    direction: Direction,
    *,
    version: str | None = None,
) -> dict[str, Any] | None:
    """Return a self-contained schema safe outside its source directory."""
    state = _ensure_state(version)
    if state is None:
        return None
    key = (tool_name, direction)
    cached = state.portable.get(key)
    if cached is not None:
        return deepcopy(cached)
    file = state.source_index.get(key) or state.file_index.get(key)
    if file is None:
        return None
    try:
        schema = json.loads(file.read_text())
        if not isinstance(schema, dict):
            raise ValueError("schema root is not an object")
        portable = _self_contained_schema(state, file, schema)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to make schema %s portable for %s: %s", file, key, exc)
        return None
    state.portable[key] = portable
    return deepcopy(portable)


def _self_contained_schema(
    state: _LoaderState,
    file: Path,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Rebase file/URL refs into local ``$defs`` without weakening them."""
    result = deepcopy(schema)
    root_definitions = result.pop("$defs", {})
    if not isinstance(root_definitions, dict):
        raise ValueError("schema $defs must be an object")
    definitions: dict[str, Any] = {}
    loading: set[str] = set()

    def definition_key(path: Path) -> str:
        try:
            relative = path.resolve().relative_to(state.root.root.resolve())
            return f"external:{relative.as_posix()}"
        except ValueError:
            return f"external:{path.name}"

    def pointer_segment(value: str) -> str:
        return value.replace("~", "~0").replace("/", "~1")

    def ensure_definition(target_file: Path, key: str) -> None:
        if key in definitions or key in loading:
            return
        loading.add(key)
        loaded = json.loads(target_file.read_text())
        if not isinstance(loaded, dict):
            raise ValueError(f"referenced schema is not an object: {target_file}")
        definitions[key] = rewrite(loaded, target_file, key)
        loading.remove(key)

    def rewrite(value: Any, current_file: Path, current_key: str | None) -> Any:
        if isinstance(value, list):
            return [rewrite(item, current_file, current_key) for item in value]
        if not isinstance(value, dict):
            return value
        rewritten = {
            name: rewrite(item, current_file, current_key)
            for name, item in value.items()
            if name != "$ref"
        }
        reference = value.get("$ref")
        if not isinstance(reference, str):
            return rewritten
        parsed = urlparse(reference)
        if not parsed.scheme and not parsed.path:
            if current_key is None:
                rewritten["$ref"] = reference
            else:
                rewritten["$ref"] = f"#/$defs/{pointer_segment(current_key)}" f"{parsed.fragment}"
            return rewritten
        target_file = _reference_file(state, current_file, reference).resolve()
        key = definition_key(target_file)
        ensure_definition(target_file, key)
        rewritten["$ref"] = f"#/$defs/{pointer_segment(key)}" f"{parsed.fragment}"
        return rewritten

    rewritten_root = rewrite(result, file.resolve(), None)
    if not isinstance(rewritten_root, dict):
        raise ValueError("schema root is not an object")
    for name, definition in root_definitions.items():
        definitions.setdefault(name, rewrite(definition, file.resolve(), None))
    if definitions:
        rewritten_root["$defs"] = definitions
    return rewritten_root


def _strip_schema_annotations(
    value: Any,
    *,
    preserve_description: bool = True,
    preserve_direct_properties: bool = True,
) -> Any:
    if isinstance(value, list):
        return [
            _strip_schema_annotations(
                item,
                preserve_description=False,
                preserve_direct_properties=False,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    omitted = {"title", "examples", "$comment", "_bundled"}
    if not preserve_description:
        omitted.add("description")
    stripped: dict[str, Any] = {}
    for key, item in value.items():
        if key in omitted:
            continue
        if key == "properties" and preserve_direct_properties and isinstance(item, dict):
            stripped[key] = {
                name: _strip_schema_annotations(
                    schema,
                    preserve_description=True,
                    preserve_direct_properties=False,
                )
                for name, schema in item.items()
            }
        else:
            stripped[key] = _strip_schema_annotations(
                item,
                preserve_description=False,
                preserve_direct_properties=False,
            )
    return stripped


def get_mcp_schema(
    tool_name: str,
    direction: Literal["request", "sync"],
    *,
    version: str | None = None,
) -> dict[str, Any] | None:
    """Return the compact transport schema used for MCP ``tools/list``.

    Newer bundles provide self-contained production-profile schemas that
    remove duplicated descriptions and definitions. Releases without those
    artifacts fall back to their canonical versioned schema.
    """
    state = _ensure_state(version)
    if state is None:
        return None
    key = (tool_name, direction)
    file = state.mcp_index.get(key) or state.source_index.get(key) or state.file_index.get(key)
    if file is None:
        return None
    try:
        schema = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Failed to load MCP schema %s for %s::%s: %s",
            file,
            tool_name,
            direction,
            exc,
        )
        return None
    if not isinstance(schema, dict):
        logger.warning("MCP schema %s is not a JSON object", file)
        return None
    try:
        portable = _self_contained_schema(state, file, schema)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to make MCP schema %s portable: %s", file, exc)
        return None
    compact = _strip_schema_annotations(portable)
    return compact if isinstance(compact, dict) else None


def list_validator_keys(*, version: str | None = None) -> list[str]:
    """Every ``tool::direction`` pair with a shipped schema. Used by tests."""
    state = _ensure_state(version)
    if state is None:
        return []
    return sorted(f"{tool}::{direction}" for (tool, direction) in state.file_index)


def _reset_for_tests() -> None:
    """Clear cached state so a fresh resolve runs. Test-only."""
    _states.clear()
    _state_misses.clear()
