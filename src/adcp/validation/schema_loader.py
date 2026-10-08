"""JSON Schema loader for AdCP tool request/response validation.

Loads the bundled per-tool schemas shipped with the SDK plus the ``core/``
schemas that async response variants ``$ref``, then prepares validator specs
lazily by ``(tool_name, direction, bundle_key)``.

Schemas live under a per-version bundle key (see
:func:`adcp.validation.version.resolve_bundle_key`) so multiple AdCP spec
versions can coexist. Callers pass an optional ``version`` to
:func:`get_validator`; ``None`` defaults to the SDK's compile-time pin
(``ADCP_VERSION``). Each bundle key gets its own ``_LoaderState`` — file
index, validator specs, core registry — so cross-version traffic
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
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, unquote, urldefrag, urljoin, urlparse, urlsplit

from pydantic import AnyUrl, TypeAdapter, ValidationError

from adcp._deferred_adapters import deferred_adapter
from adcp.validation.version import resolve_bundle_key

logger = logging.getLogger(__name__)


@deferred_adapter
def _uri_adapter() -> TypeAdapter[AnyUrl]:
    """Built on first use: the generated models validate every ``format: uri``
    field through this same adapter."""
    return TypeAdapter(AnyUrl)


# Serialize first-time init and validator-spec preparation. Concurrent callers
# on a fresh process can otherwise both walk the schema tree or prepare
# the same spec twice. Result is the same either way, but the lock
# keeps behaviour deterministic and avoids redundant filesystem walks.
_init_lock = threading.Lock()
_compile_lock = threading.Lock()
_resolver_import_lock = threading.Lock()
_ref_resolver_type: Any = None

ResponseVariant = Literal["sync", "submitted", "working", "input-required"]
Direction = Literal["request", "sync", "submitted", "working", "input-required"]


_RFC3339_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)$",
    re.ASCII,
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
    try:
        # The grammar already checks time and offset ranges. Validate only
        # the calendar here: Python 3.10's datetime parser accepts only three
        # or six fractional digits, whereas RFC 3339 permits any positive
        # number. Validation must neither coerce nor truncate the wire value.
        date.fromisoformat(instance[:10])
    except ValueError:
        return False
    return True


# Hostname grammar from RFC 1123 section 2.1: dot-separated labels of
# alphanumerics and hyphens, each label 1-63 characters and neither starting
# nor ending with a hyphen. This is the same grammar the generated models
# carry as a ``pattern`` constraint on every ``format: hostname`` field, so
# both validators accept the same set of strings.
_HOSTNAME = re.compile(
    r"(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])\.)*"
    r"([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]{0,61}[A-Za-z0-9])",
    re.ASCII,
)


def _is_uri(instance: Any) -> bool:
    """Validate URI syntax while retaining AnyUrl's internationalized URLs.

    Check the original syntax before applying the ``pydantic.AnyUrl`` parser
    used by generated models. AnyUrl alone accepts malformed percent escapes
    and normalizes whitespace/backslashes; that would bypass the explicit
    ad-server-token branch in macro-bearing-url. Retaining its check also
    prevents values passing schema validation but failing model validation.
    """
    if not isinstance(instance, str):
        return True
    from rfc3986_validator import validate_rfc3986

    if any(char.isspace() or 0x7F <= ord(char) <= 0x9F for char in instance):
        return False
    try:
        # Escape only non-ASCII characters for the RFC 3986 syntax check.
        # Leave original ASCII untouched so normalization cannot hide malformed
        # percent escapes or backslashes. AnyUrl still checks IDN validity.
        syntax = (
            instance
            if instance.isascii()
            else "".join(char if char.isascii() else quote(char, safe="") for char in instance)
        )
    except UnicodeEncodeError:
        return False
    if not validate_rfc3986(syntax):
        return False
    try:
        _uri_adapter().validate_python(instance)
    except ValidationError:
        return False
    return True


def _is_hostname(instance: Any) -> bool:
    """Return whether ``instance`` is an RFC 1123 hostname."""
    if not isinstance(instance, str):
        return True
    return _HOSTNAME.fullmatch(instance) is not None


# RFC 6570 sections 2.1-2.4: literal percent escapes, variable names and
# modifiers. Optional template-expansion libraries do not consistently check
# literal syntax; supply the format checker rather than depending on them.
_TEMPLATE_VARCHAR = r"(?:[a-z0-9_]|%[0-9a-f]{2})"
_TEMPLATE_VARNAME = rf"{_TEMPLATE_VARCHAR}+(?:\.{_TEMPLATE_VARCHAR}+)*"
_TEMPLATE_VARSPEC = rf"{_TEMPLATE_VARNAME}(?::[1-9][0-9]{{0,3}}|\*)?"
_URI_TEMPLATE = re.compile(
    r"(?:[^\x00-\x20\x7f-\x9f\"'<>%\\^`{|}]|%[0-9a-f]{2}|"
    rf"\{{[+#./;?&=,!@|]?{_TEMPLATE_VARSPEC}(?:,{_TEMPLATE_VARSPEC})*\}})*",
    re.IGNORECASE | re.ASCII,
)


def _is_uri_template(instance: Any) -> bool:
    if not isinstance(instance, str):
        return True
    return _URI_TEMPLATE.fullmatch(instance) is not None


#: Formats whose checker this module supplies. ``jsonschema`` resolves the
#: rest of the formats the AdCP bundle uses (``date``, ``date-time``,
#: ``email``, ``uuid``) from its own registry, and treats a format with no
#: checker as a bare annotation — accepting every string. The bundle uses
#: ``format: uri`` more than any other keyword, and ``jsonschema`` only
#: checks it when the optional ``rfc3987`` package happens to be installed,
#: which would make validation strength depend on the adopter's environment.
#: Supplying the checker here pins it to the SDK instead.
_SUPPLIED_FORMAT_CHECKS: dict[str, Any] = {
    "date-time": _is_rfc3339_date_time,
    "uri": _is_uri,
    "uri-template": _is_uri_template,
    "hostname": _is_hostname,
}


def _build_format_checker() -> Any:
    """Return the format checker every bundled-schema validator uses.

    One builder for every validator this module compiles: task validators and
    named-document validators enforce the same formats, so a format added
    here participates in both.
    """
    from jsonschema import FormatChecker

    checker = FormatChecker()
    for name, check in _SUPPLIED_FORMAT_CHECKS.items():
        checker.checks(name)(check)
    return checker


@dataclass(frozen=True)
class _ValidatorSpec:
    """Cached schema inputs; each caller gets its own resolver scope stack."""

    schema: dict[str, Any]
    base_uri: str
    store: dict[str, dict[str, Any]]
    format_checker: Any
    bundle_key: str | None = None

    def make(self) -> Any:
        return _draft7_validator_type()(
            self.schema,
            resolver=_ref_resolver_from_store(
                self.base_uri, self.schema, self.store, bundle_key=self.bundle_key
            ),
            format_checker=self.format_checker,
        )


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
        self.compiled: dict[tuple[str, Direction], _ValidatorSpec] = {}
        self.named_compiled: dict[str, _ValidatorSpec] = {}
        self.portable: dict[tuple[str, Direction], dict[str, Any]] = {}
        # Serialized JSON keeps the immutable cached value private and makes
        # every returned tree independent, including any repeated branches.
        self.mcp_schemas: dict[tuple[str, Direction], str] = {}
        self.mcp_schema_lock = threading.Lock()
        self.registry: dict[str, dict[str, Any]] = {}
        self._registry_loaded = False
        #: Version segments that canonical ``/schemas/{segment}/`` references
        #: in this bundle may use. A stable bundle is cached under
        #: ``MAJOR.MINOR`` while its documents reference the exact release,
        #: for example ``/schemas/3.2.1/`` inside the ``3.2`` cache.
        self.path_versions: tuple[str, ...] = (bundle_key,)


def _bundle_path_versions(root: _SchemaRoot, bundle_key: str) -> tuple[str, ...]:
    """Return the bundle key plus the exact release its ``index.json`` declares.

    The declared release is accepted only when it collapses to the same
    bundle key, so a mislabelled cache can never alias another version.
    """
    versions = [bundle_key]
    try:
        index = json.loads((root.root / "index.json").read_text())
    except (OSError, json.JSONDecodeError):
        return tuple(versions)
    declared = index.get("adcp_version") if isinstance(index, dict) else None
    if isinstance(declared, str) and declared != bundle_key:
        try:
            if resolve_bundle_key(declared) == bundle_key:
                versions.append(declared)
        except ValueError:
            pass
    return tuple(versions)


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
    """Resolve a version, preferring a retained exact stable source bundle.

    Minor selectors receive maintenance fixes. Retained patch snapshots keep
    persisted compatibility continuations on their original source contract.
    Versions without a retained snapshot still use the minor-line bundle.
    """
    if version is None:
        return _sdk_pinned_bundle_key()
    from adcp._version import resolve_adcp_version_alias

    resolved = resolve_adcp_version_alias(version)
    bundle_key = resolve_bundle_key(resolved)
    if resolved != bundle_key and _resolve_schema_root(resolved) is not None:
        return resolved
    return bundle_key


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
        new_state.path_versions = _bundle_path_versions(root, bundle_key)
        new_state.file_index = _build_index(root)
        new_state.source_index = _build_source_index(root)
        new_state.mcp_index = _build_mcp_index(root)
        _states[bundle_key] = new_state
        return new_state


def _load_schema_registry(state: _LoaderState) -> None:
    """Register every modular schema by canonical path and optional ``$id``.

    Older bundles mostly referenced files under ``core/``, so loading only
    that directory was sufficient. AdCP 3.2.0-beta.4 introduced canonical
    cross-directory references (for example ``core/protocol-envelope.json``
    references ``enums/task-status.json``). Register the complete modular
    tree so validation remains offline and never resolves a protocol schema
    over the network. Bundled and MCP projections are excluded because they
    duplicate modular identifiers and contain their own internal ``$defs``.
    """
    if state._registry_loaded:
        return
    for file in _walk_json(state.root.root):
        relative = file.relative_to(state.root.root)
        if relative.parts[0] in {"bundled", "mcp"}:
            continue
        try:
            schema = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load core schema %s: %s", file, exc)
            continue
        # A modular schema need not declare an $id. Canonical references to
        # its bundle path must still resolve locally (for example rc.6's
        # core/version-envelope.json), without relying on CDN availability.
        for path_version in state.path_versions:
            canonical_path = f"/schemas/{path_version}/{relative.as_posix()}"
            state.registry[f"https://adcontextprotocol.org{canonical_path}"] = schema
            state.registry[canonical_path] = schema
            state.registry[f"file://{canonical_path}"] = schema
        state.registry[file.resolve().as_uri()] = schema
        schema_id = schema.get("$id")
        if isinstance(schema_id, str):
            state.registry[schema_id] = schema
            # Some exact legacy bundles publish root-relative canonical IDs.
            # RefResolver resolves those against our ``file://`` base before
            # consulting its store, producing ``file:///schemas/...``. Seed
            # that equivalent URI as an offline alias so it never falls
            # through to urllib and attempts to open an absolute host path.
            if schema_id.startswith("/schemas/"):
                state.registry[f"file://{schema_id}"] = schema
    state._registry_loaded = True


def _make_ref_resolver(state: _LoaderState, base_file: Path, schema: dict[str, Any]) -> Any:
    """Build a task resolver with the version-scoped local schema registry."""
    _load_schema_registry(state)
    return _ref_resolver_from_store(
        base_file.resolve().as_uri(), schema, state.registry, bundle_key=state.bundle_key
    )


def _get_ref_resolver_type() -> Any:
    """Resolve the deprecated class once, without racing warning filters."""
    global _ref_resolver_type
    if _ref_resolver_type is not None:
        return _ref_resolver_type
    with _resolver_import_lock:
        if _ref_resolver_type is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                try:
                    from jsonschema import RefResolver
                except ImportError as exc:  # pragma: no cover - guarded by dep install
                    raise RuntimeError(
                        "jsonschema is required for AdCP schema validation. "
                        "Install with: pip install 'jsonschema>=4.20.0'"
                    ) from exc
            _ref_resolver_type = RefResolver
    return _ref_resolver_type


def _ref_resolver_from_store(
    base_uri: str,
    schema: dict[str, Any],
    store: dict[str, dict[str, Any]],
    *,
    bundle_key: str | None = None,
) -> Any:
    """Build a jsonschema ``RefResolver`` rooted at the file's directory.

    Async variant schemas use relative refs like ``../core/context.json``;
    giving the resolver a ``file://`` base URI lets those resolve against
    disk. Uses the cached local schema store so bundled schemas that
    reference a core type by canonical id still resolve.

    Sets ``referrer=schema`` (not ``{}``) so fragment-only refs like
    ``#/$defs/MediaChannel`` inside the bundled per-tool schema resolve
    against the schema being validated. Without this, the resolver
    walks the empty-dict referrer and raises
    ``Unresolvable JSON pointer: '$defs/MediaChannel'`` on any
    bundled schema that uses internal ``$defs`` (every bundled
    capabilities-style schema does).

    ``RefResolver`` is deprecated in jsonschema 4.18+ (to be replaced by
    the ``referencing`` library). Suppress the warning on first import so
    downstream projects running ``-W error::DeprecationWarning`` don't
    crash on import. Cached lookups do not change process warning filters;
    migration tracked as a follow-up.
    """
    resolver_type = _get_ref_resolver_type()

    def missing_local_reference(uri: str) -> Any:
        raise ValueError(f"schema reference is not in bundle {bundle_key}: {uri}")

    return resolver_type(
        base_uri=base_uri,
        referrer=schema,
        store=dict(store),
        handlers=(
            {"http": missing_local_reference, "https": missing_local_reference}
            if bundle_key is not None
            else {}
        ),
    )


def _has_external_refs(value: Any) -> bool:
    """Whether a schema needs documents beyond its own fragment references."""
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            return True
        return any(_has_external_refs(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_external_refs(child) for child in value)
    return False


def _reachable_registry_store(
    registry: dict[str, dict[str, Any]], base_uri: str, schema: dict[str, Any] | bool
) -> dict[str, dict[str, Any]]:
    """Keep every alias of documents reached through absolute references.

    Relative references and nested IDs depend on resolution scopes, so leave
    their registry intact. Unknown targets also retain the original store and
    fail at validation through the existing offline resolver handlers.
    """
    if not isinstance(schema, dict):
        return dict(registry)
    # Match RefResolver's normalized alias pass, document-ID pass, and final
    # referrer override. Filtering below preserves that same insertion order.
    lookup = {urlsplit(uri).geturl(): document for uri, document in registry.items()}
    for document in registry.values():
        identifier = document.get("$id")
        if isinstance(identifier, str):
            lookup[urlsplit(identifier).geturl()] = document
    lookup[urlsplit(base_uri).geturl()] = schema

    selected: set[int] = set()
    pending = [schema]
    while pending:
        document = pending.pop()
        if id(document) in selected:
            continue
        selected.add(id(document))
        identifier = document.get("$id")
        if isinstance(identifier, str):
            parsed_id = urlsplit(identifier)
            if not parsed_id.scheme or not parsed_id.netloc or parsed_id.fragment:
                return dict(registry)
            # Retain an ID's winning document even if this document was
            # reached through another alias, including on fragment-only refs.
            target = lookup.get(parsed_id.geturl())
            if target is not None:
                pending.append(target)

        values: list[Any] = [document]
        while values:
            value = values.pop()
            if isinstance(value, dict):
                if value is not document and "$id" in value:
                    return dict(registry)
                reference = value.get("$ref")
                if isinstance(reference, str) and reference and not reference.startswith("#"):
                    parsed = urlsplit(reference)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        return dict(registry)
                    uri, _ = urldefrag(reference.rstrip("/"))
                    target = lookup.get(urlsplit(uri).geturl())
                    if target is None:
                        return dict(registry)
                    pending.append(target)
                values.extend(value.values())
            elif isinstance(value, list):
                values.extend(value)

    return {uri: document for uri, document in registry.items() if id(document) in selected}


def _bundle_relative_path(state: _LoaderState, path: str) -> Path:
    """Map a canonical ``/schemas/{version}/...`` path into this bundle."""
    for path_version in state.path_versions:
        prefix = f"/schemas/{path_version}/"
        if path.startswith(prefix):
            return Path(unquote(path[len(prefix) :]))
    raise ValueError("schema reference is outside the local version bundle")


def _reachable_schema_store(
    state: _LoaderState,
    base_file: Path,
    schema: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Load only the local schemas reachable from one standalone document."""
    root = state.root.root.resolve()
    resolved_base = base_file.resolve()
    store: dict[str, dict[str, Any]] = {
        resolved_base.as_uri(): schema,
    }
    schema_id = schema.get("$id")
    if isinstance(schema_id, str):
        store[schema_id] = schema
    visited: set[Path] = {resolved_base}

    def referenced_file(reference: str, current_file: Path) -> tuple[str, Path] | None:
        target = reference.split("#", 1)[0]
        if not target:
            return None
        parsed = urlparse(target)
        if parsed.scheme in {"http", "https"}:
            if parsed.hostname != "adcontextprotocol.org":
                raise ValueError("schema reference is outside the local version bundle")
            candidate = root / _bundle_relative_path(state, parsed.path)
        elif parsed.scheme:
            raise ValueError("schema reference uses a non-local scheme")
        elif parsed.path.startswith("/schemas/"):
            candidate = root / _bundle_relative_path(state, parsed.path)
        else:
            candidate = current_file.parent / unquote(parsed.path)
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError("schema reference is outside the local version bundle")
        return target, resolved

    def visit(value: Any, current_file: Path) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str):
                resolved_ref = referenced_file(reference, current_file)
                if resolved_ref is not None:
                    target, file = resolved_ref
                    if file not in visited:
                        visited.add(file)
                        child = json.loads(file.read_text())
                        if not isinstance(child, dict):
                            raise ValueError("referenced schema is not an object")
                        store[target] = child
                        store[file.as_uri()] = child
                        child_id = child.get("$id")
                        if isinstance(child_id, str):
                            store[child_id] = child
                        visit(child, file)
                    else:
                        child = store.get(file.as_uri())
                        if child is not None:
                            store[target] = child
            for child_value in value.values():
                visit(child_value, current_file)
        elif isinstance(value, list):
            for child_value in value:
                visit(child_value, current_file)

    visit(schema, base_file)
    return store


def _validate_ecma_pattern(validator: Any, pattern: str, instance: Any, schema: Any) -> Any:
    """Apply ECMA-262 final-$ semantics without rewriting schema documents."""
    from jsonschema import Draft7Validator

    aligned = pattern
    if pattern.endswith("$"):
        prefix = pattern[:-1]
        if (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 0:
            aligned = prefix + r"$(?![\s\S])"
    for error in Draft7Validator.VALIDATORS["pattern"](validator, aligned, instance, schema):
        error.message = f"{instance!r} does not match {pattern!r}"
        yield error


@lru_cache(maxsize=1)
def _draft7_validator_type() -> Any:
    from jsonschema import Draft7Validator
    from jsonschema.validators import extend, validator_for

    cls = extend(Draft7Validator, {"pattern": _validate_ecma_pattern})
    original_evolve = cls.evolve

    def evolve(self: Any, **changes: Any) -> Any:
        schema = changes.get("schema", self.schema)
        if isinstance(schema, dict) and validator_for(schema, default=cls) is Draft7Validator:
            # A referenced document's Draft 7 declaration must not switch back
            # to jsonschema's default Python pattern checker. Omit the dialect
            # metadata only while selecting the evolved class, then expose the
            # original document. All validation keywords stay identical.
            selection = {key: value for key, value in schema.items() if key != "$schema"}
            evolved = original_evolve(self, **{**changes, "schema": selection})
            evolved.schema = schema
            return evolved
        return original_evolve(self, **changes)

    cls.evolve = evolve
    return cls


def _normalize_bundled_schema_for_validation(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove nested ``$id`` scope changes from a flattened bundle.

    Flattened schemas rewrite external references to root ``#/$defs`` pointers.
    AdCP 3.2.0-beta.4 also retained the inlined documents' canonical ``$id``
    values, which makes draft-07 resolution switch away from the bundle root
    before following those pointers. Work on a copy so public schema reads and
    the signed cache remain untouched.
    """

    normalized = deepcopy(schema)

    def visit(value: Any, *, root: bool = False) -> None:
        if isinstance(value, dict):
            if not root:
                value.pop("$id", None)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(normalized, root=True)
    return normalized


def _effective_task_schema(
    schema: dict[str, Any], tool_name: str, direction: Direction, *, bundle_key: str
) -> dict[str, Any]:
    """Apply the SDK's captured-schedule contract without rewriting signed bundles.

    Reporting health describes existing evidence; it does not cancel a frozen
    generation's future commitment (#1179). The 3.2.0-rc.3 status schema couples
    the two at /allOf/2/then/not. Correct only that exact known rule and version.
    Its if, scope closure and coverage requirements remain unchanged. A changed
    or different-version rule is left intact for explicit compatibility review.
    """
    if (tool_name, direction, bundle_key) != ("get_reporting_status", "sync", "3.2.0-rc.3"):
        return schema
    known_rule = {
        "if": {
            "properties": {"health": {"const": "complete"}},
            "required": ["health"],
        },
        "then": {
            "properties": {
                "scope": {
                    "properties": {
                        "scope_closed": {"const": True},
                        "coverage_complete": {"const": True},
                    },
                    "required": ["scope_closed", "coverage_complete"],
                }
            },
            "not": {"required": ["next_expected_at"]},
        },
    }
    conditions = schema.get("allOf")
    if not isinstance(conditions, list) or len(conditions) < 3 or conditions[2] != known_rule:
        return schema
    result = deepcopy(schema)
    del result["allOf"][2]["then"]["not"]
    return result


def get_validator(
    tool_name: str,
    direction: Direction,
    *,
    version: str | None = None,
) -> Any | None:
    """Return a fresh validator for ``(tool_name, direction, version)``.

    Schema loading and reference discovery are cached. Each lookup creates
    an independent resolver so callers can validate concurrently. Obtain a
    validator per caller rather than sharing one instance between threads.

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
        return cached.make()
    file = state.file_index.get(key)
    if file is None:
        return None
    try:
        schema = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load schema %s for %s: %s", file, key, exc)
        return None
    if file.is_relative_to(state.root.bundled):
        schema = _normalize_bundled_schema_for_validation(schema)
    schema = _effective_task_schema(schema, tool_name, direction, bundle_key=state.bundle_key)

    try:
        from jsonschema.exceptions import SchemaError
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "jsonschema is required for AdCP schema validation. "
            "Install with: pip install 'jsonschema>=4.20.0'"
        ) from exc

    with _compile_lock:
        # Re-check: another thread may have prepared the validator spec for
        # this key while we were loading the schema off disk.
        cached = state.compiled.get(key)
        if cached is not None:
            return cached.make()
        try:
            _load_schema_registry(state)
            base_uri = file.resolve().as_uri()
            schema_id = schema.get("$id") if isinstance(schema, dict) else None
            # Flattened bundles have no nested $id scopes after normalization.
            # Their fragment refs only need the root document. Avoid copying
            # the entire modular registry into every fresh resolver.
            if (
                file.is_relative_to(state.root.bundled)
                and isinstance(schema, dict)
                and not _has_external_refs(schema)
                and not (isinstance(schema_id, str) and urlsplit(schema_id).fragment)
            ):
                store = {base_uri: schema}
                if isinstance(schema_id, str):
                    store[schema_id] = schema
                    store[urljoin(base_uri, schema_id)] = schema
            else:
                store = _reachable_registry_store(state.registry, base_uri, schema)
            spec = _ValidatorSpec(
                schema=schema,
                base_uri=base_uri,
                store=store,
                format_checker=_build_format_checker(),
                bundle_key=state.bundle_key,
            )
            validator = spec.make()
        except SchemaError as exc:
            logger.warning("Invalid schema %s for %s: %s", file, key, exc)
            return None
        state.compiled[key] = spec
        return validator


def get_named_validator(
    relative_path: str,
    *,
    version: str | None = None,
) -> Any | None:
    """Return a fresh offline validator for a non-task schema in the bundle.

    Schema loading and reference discovery are cached. Each lookup creates
    an independent resolver so callers can validate concurrently. Obtain a
    validator per caller rather than sharing one instance between threads.

    Task validation normally goes through :func:`get_validator`.  Some SDK
    helpers also consume standalone protocol documents (for example a
    seller-hosted acceptance-policy catalog) and must validate them against
    the exact schema version shipped by the SDK.  This loader uses the same
    canonical-ID registry and format checker as task validation, so external
    ``$ref`` values are resolved only from the signed local bundle.

    ``relative_path`` is relative to the versioned schema root, such as
    ``"media-buy/acceptance-policy-catalog.json"``.  Invalid or missing paths
    return ``None`` rather than escaping the bundle root.
    """
    path = Path(relative_path)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return None
    state = _ensure_state(version)
    if state is None:
        return None
    cache_key = path.as_posix()
    cached = state.named_compiled.get(cache_key)
    if cached is not None:
        return cached.make()
    file = state.root.root.joinpath(*path.parts)
    try:
        if not file.is_file() or not file.resolve().is_relative_to(state.root.root.resolve()):
            return None
        schema = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(schema, dict):
        return None

    try:
        from jsonschema.exceptions import SchemaError
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "jsonschema is required for AdCP schema validation. "
            "Install with: pip install 'jsonschema>=4.20.0'"
        ) from exc

    with _compile_lock:
        cached = state.named_compiled.get(cache_key)
        if cached is not None:
            return cached.make()
        try:
            spec = _ValidatorSpec(
                schema=schema,
                base_uri=file.resolve().as_uri(),
                store=_reachable_schema_store(state, file, schema),
                format_checker=_build_format_checker(),
            )
            validator = spec.make()
        except (OSError, json.JSONDecodeError, SchemaError, ValueError):
            return None
        state.named_compiled[cache_key] = spec
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
    return deepcopy(
        _effective_task_schema(schema, tool_name, direction, bundle_key=state.bundle_key)
    )


def get_named_schema_document(
    relative_path: str,
    *,
    version: str | None = None,
) -> dict[str, Any] | None:
    """Return a bundled non-task schema document, uncompiled.

    The document counterpart to :func:`get_named_validator`. Some SDK helpers
    need a schema's *data* rather than its validation behavior -- notably the
    normative ``enumMetadata`` blocks the spec requires SDKs to dispatch on,
    which carry no validation semantics at all.

    ``relative_path`` is relative to the versioned schema root, e.g.
    ``"enums/media-buy-valid-action.json"``. Path traversal is refused and a
    missing document returns ``None``, so an older pin degrades rather than
    raising.
    """
    path = Path(relative_path)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return None
    state = _ensure_state(version)
    if state is None:
        return None
    file = state.root.root.joinpath(*path.parts)
    try:
        if not file.is_file() or not file.resolve().is_relative_to(state.root.root.resolve()):
            return None
        document = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return deepcopy(document) if isinstance(document, dict) else None


def get_bundle_adcp_version(*, version: str | None = None) -> str | None:
    """Return the exact AdCP release declared by a resolved schema bundle.

    Stable validation normally uses a shared ``MAJOR.MINOR`` cache key;
    explicitly retained patch snapshots take precedence for exact selectors.
    Exact-source compatibility coordinators compare this value with the
    negotiated patch and fail closed on a mismatch.
    """

    state = _ensure_state(version)
    if state is None:
        return None
    index_file = state.root.root / "index.json"
    try:
        index = json.loads(index_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load schema bundle index %s: %s", index_file, exc)
        return None
    declared = index.get("adcp_version")
    return declared if isinstance(declared, str) else None


def _reference_file(state: _LoaderState, current_file: Path, reference: str) -> Path:
    parsed = urlparse(reference)
    if parsed.scheme or parsed.path.startswith("/schemas/"):
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
        portable = _self_contained_schema(
            state,
            file,
            _effective_task_schema(schema, tool_name, direction, bundle_key=state.bundle_key),
        )
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
                rewritten["$ref"] = f"#/$defs/{pointer_segment(current_key)}{parsed.fragment}"
            return rewritten
        target_file = _reference_file(state, current_file, reference).resolve()
        if target_file == file.resolve():
            # An absolute self-reference still points into the root document.
            # Re-importing that document duplicates its entire definition graph.
            rewritten["$ref"] = f"#{parsed.fragment}"
            return rewritten
        key = definition_key(target_file)
        ensure_definition(target_file, key)
        rewritten["$ref"] = f"#/$defs/{pointer_segment(key)}{parsed.fragment}"
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

    Successful materializations belong to the immutable versioned loader
    state. Each caller receives an independent, alias-free JSON tree; missing
    or invalid schemas are not added to the materialization cache.
    """
    state = _ensure_state(version)
    if state is None:
        return None
    key = (tool_name, direction)
    cached = state.mcp_schemas.get(key)
    if cached is None:
        with state.mcp_schema_lock:
            # Only one concurrent first caller traverses the reference graph.
            cached = state.mcp_schemas.get(key)
            if cached is None:
                file = (
                    state.mcp_index.get(key)
                    or state.source_index.get(key)
                    or state.file_index.get(key)
                )
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
                    portable = _self_contained_schema(
                        state,
                        file,
                        _effective_task_schema(
                            schema, tool_name, direction, bundle_key=state.bundle_key
                        ),
                    )
                except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
                    logger.warning("Failed to make MCP schema %s portable: %s", file, exc)
                    return None
                compact = _strip_schema_annotations(portable)
                if not isinstance(compact, dict):
                    return None
                cached = json.dumps(compact, separators=(",", ":"))
                state.mcp_schemas[key] = cached
    return cast(dict[str, Any], json.loads(cached))


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
