"""v1↔v2 canonical mapping registry loader + matchers.

Implements the registry contract from
``registries/v1-canonical-mapping.json``. Two match modes:

* **Glob** — exact / wildcard match against a v1 ``format_id.id`` value.
  As of 3.1 the registry carries zero literal entries; the AAO-published
  IAB-standard formats project via catalog ``canonical:`` annotations
  (resolution-order step 2). The matcher is implemented to handle
  ``*`` wildcards anywhere in the pattern so future literal entries
  work without further code change.
* **Structural** — match against the v1 format's slot shape, asset types,
  and VAST/DAAST version constraints. The primary fallback for v1 wire
  traffic.

Directional invariant: this registry is authoritative for **v1 → v2 projection
only**. The v2 → v1 path in :mod:`adcp.canonical_formats.projection` does NOT
consult the registry; it relies on the seller-asserted ``v1_format_ref[]`` on
the v2 declaration.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from adcp.types import V1V2CanonicalFormatMappingRegistry

_REGISTRY_RELATIVE = Path("registries") / "v1-canonical-mapping.json"


def _read_registry_json() -> str:
    """Return the raw JSON for the registry from packaged or dev-checkout layout.

    Mirrors :mod:`adcp.validation.schema_loader`'s resolution order:

    1. Packaged: ``importlib.resources.files("adcp") / "_schemas" / <version> / …``
       (populated by ``scripts/bundle_schemas.py`` before wheel build).
    2. Dev checkout: ``<repo>/schemas/cache/<version>/…`` walking up from
       this module, used by editable installs that haven't bundled yet.

    Raises :class:`FileNotFoundError` when neither layout exposes the registry —
    the canonical-formats projection cannot operate without the registry, so
    fail fast rather than degrade silently.
    """
    adcp_version = (files("adcp") / "ADCP_VERSION").read_text().strip()

    try:
        packaged = files("adcp") / "_schemas" / adcp_version / str(_REGISTRY_RELATIVE)
        with as_file(packaged) as p:
            packaged_path = Path(p)
            if packaged_path.is_file():
                return packaged_path.read_text()
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass

    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "schemas" / "cache" / adcp_version / _REGISTRY_RELATIVE
        if candidate.is_file():
            return candidate.read_text()
        if ancestor.parent == ancestor:
            break

    raise FileNotFoundError(
        f"v1-canonical-mapping registry not found for ADCP_VERSION={adcp_version} "
        f"in either packaged (_schemas/) or dev-checkout (schemas/cache/) layout."
    )


class RegistryLoadError(RuntimeError):
    """Raised when the bundled v1↔v2 registry cannot be loaded or parsed.

    Wraps the underlying :class:`FileNotFoundError`,
    :class:`json.JSONDecodeError`, or :class:`pydantic.ValidationError`
    with a contextual message naming the registry path + ADCP version so
    adopters can diagnose a corrupt bundle.
    """


@lru_cache(maxsize=1)
def _load_registry_uncopied() -> V1V2CanonicalFormatMappingRegistry:
    """Cached parsed registry — DO NOT call directly; use :func:`load_default_registry`.

    Wraps all read/parse failures in :class:`RegistryLoadError` with the
    bundle context. The cache stores the parsed model once per process;
    :func:`load_default_registry` returns a deep copy so multi-tenant
    callers cannot mutate each other's view.
    """
    adcp_version = (files("adcp") / "ADCP_VERSION").read_text().strip()
    try:
        raw = _read_registry_json()
    except FileNotFoundError as exc:
        raise RegistryLoadError(
            f"v1-canonical-mapping registry not found " f"(ADCP_VERSION={adcp_version!r}): {exc}"
        ) from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryLoadError(
            f"v1-canonical-mapping registry has invalid JSON "
            f"(ADCP_VERSION={adcp_version!r}, position {exc.pos}): {exc.msg}"
        ) from exc

    try:
        return V1V2CanonicalFormatMappingRegistry.model_validate(parsed)
    except Exception as exc:
        raise RegistryLoadError(
            f"v1-canonical-mapping registry failed schema validation "
            f"(ADCP_VERSION={adcp_version!r}): {exc}"
        ) from exc


def load_default_registry() -> V1V2CanonicalFormatMappingRegistry:
    """Load and parse the AAO-published v1↔v2 mapping registry.

    Returns a fresh deep copy of the cached parsed registry — callers
    can safely mutate the returned instance without affecting other
    callers in the same process. The underlying parsed registry is
    cached per process (the registry is immutable for a given SDK
    build, keyed by ``ADCP_VERSION``).

    Raises:
        RegistryLoadError: when the bundle is missing, malformed JSON,
            or fails schema validation.
    """
    return _load_registry_uncopied().model_copy(deep=True)


def glob_match(value: str, pattern: str) -> bool:
    """Glob-match ``value`` against a registry ``format_id_glob`` pattern.

    Per the registry schema: ``*`` matches any segment. Patterns are
    compared against the v1 ``format_id.id`` (NOT the ``{agent_url, id}``
    pair — the registry mantra is family identification, not full
    namespace resolution).

    Treats ``*`` as a permissive wildcard (any chars including ``_``).
    Other regex metacharacters are escaped — the pattern language is
    glob, not regex.
    """
    if pattern == "*":
        return True
    regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
    return re.fullmatch(regex, value) is not None


# Constraint operator prefixes the registry's version DSL recognises.
# A constraint that does not match any of these AND does not end in
# ``.x`` is treated as a bare exact version (e.g., ``"4.2"``). Order
# matters: longer prefixes must come before shorter (``">="`` before
# ``">"``) so the dispatch finds the right operator first.
_VERSION_OPERATORS: tuple[str, ...] = (">=", "<=", ">", "<", "==", "!=")


def _versions_overlap(have: str, want_constraints: list[str]) -> bool:
    """True iff a single concrete version satisfies any of the constraints.

    The registry's ``vast_versions`` / ``daast_versions`` constraints use
    a small DSL: ``"4.2"`` (exact), ``"4.x"`` (any 4-major), ``">=4.0"``
    (semver-style range, supports ``<``, ``<=``, ``>``, ``>=``, ``==``,
    ``!=``). Constraints are matched against a single concrete version
    like ``"3.0"`` or ``"4.2"``.

    Constraints are OR-joined — any matching entry returns ``True``.

    Raises:
        ValueError: when a constraint string starts with an unrecognised
            operator prefix (e.g., ``"~>4.0"``). Silently ignoring would
            mask a registry-publishing mistake — the registry MUST stick
            to the documented DSL.
    """
    try:
        have_major, have_minor = _parse_version_pair(have)
    except ValueError:
        return False

    for constraint in want_constraints:
        c = constraint.strip()

        if c.endswith(".x"):
            try:
                want_major = int(c[:-2])
            except ValueError as exc:
                raise ValueError(f"Unparseable .x version constraint: {constraint!r}") from exc
            if have_major == want_major:
                return True
            continue

        op: str | None = None
        for candidate in _VERSION_OPERATORS:
            if c.startswith(candidate):
                op = candidate
                break

        # Looks like an operator (starts with one of ``<>=!~^``) but
        # didn't match the recognised set — fail loudly so a typo or
        # unsupported DSL extension in a registry entry surfaces during
        # loading rather than silently never matching.
        if op is None and c and c[0] in "<>=!~^":
            raise ValueError(
                f"Unrecognised version-constraint operator in {constraint!r}; "
                f"supported operators are {_VERSION_OPERATORS!r} and the ``.x`` "
                f"suffix."
            )

        rest = c[len(op) :].strip() if op else c
        try:
            want_major, want_minor = _parse_version_pair(rest)
        except ValueError:
            continue

        have_pair = (have_major, have_minor)
        want_pair = (want_major, want_minor)
        if op is None or op == "==":
            if have_pair == want_pair:
                return True
        elif op == "!=":
            if have_pair != want_pair:
                return True
        elif op == ">=":
            if have_pair >= want_pair:
                return True
        elif op == "<=":
            if have_pair <= want_pair:
                return True
        elif op == ">":
            if have_pair > want_pair:
                return True
        elif op == "<":
            if have_pair < want_pair:
                return True
    return False


def _parse_version_pair(s: str) -> tuple[int, int]:
    """Parse ``"4.2"`` / ``"3"`` / ``"3.0"`` into a ``(major, minor)`` int pair.

    Leading/trailing whitespace stripped; a missing minor component
    defaults to ``0``. Raises :class:`ValueError` on unparseable input.
    """
    parts = s.strip().split(".")
    if not parts or not parts[0]:
        raise ValueError(s)
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) >= 2 and parts[1] else 0
    return major, minor


def structural_match(
    *,
    asset_types: list[str],
    vast_versions: list[str] | None = None,
    daast_versions: list[str] | None = None,
    width: int | None = None,
    height: int | None = None,
    pattern: Any,
) -> bool:
    """Check whether a v1 format's structural shape matches a registry entry.

    ``pattern`` is the registry entry's ``structural`` block (a
    :class:`adcp.types.V1CanonicalStructural` or equivalent dict). All
    constraints declared on the pattern MUST match; constraints absent
    from the pattern do not narrow the match.

    Args:
        asset_types: Asset types appearing in the v1 format's slots.
            The pattern's ``asset_types`` is a *subset* requirement —
            every type the pattern lists must be present in ``asset_types``.
        vast_versions: VAST version(s) declared on the v1 format (typically
            a single value like ``"4.2"``). Each must satisfy at least one
            constraint in the pattern's ``vast_versions`` list.
        daast_versions: DAAST version(s); same matching semantics as VAST.
        width: Slot dimension width (pixels), if applicable.
        height: Slot dimension height (pixels), if applicable.
        pattern: The registry entry's ``structural`` block.

    Returns:
        ``True`` iff every constraint declared on ``pattern`` is satisfied
        by the v1 format's structural shape.
    """
    if hasattr(pattern, "model_dump"):
        p = pattern.model_dump(exclude_none=True)
    else:
        p = dict(pattern) if pattern else {}

    want_types = p.get("asset_types")
    if want_types:
        for t in want_types:
            if t not in asset_types:
                return False

    want_vast = p.get("vast_versions")
    if want_vast:
        if not vast_versions:
            return False
        if not any(_versions_overlap(v, want_vast) for v in vast_versions):
            return False

    want_daast = p.get("daast_versions")
    if want_daast:
        if not daast_versions:
            return False
        if not any(_versions_overlap(v, want_daast) for v in daast_versions):
            return False

    want_dims = p.get("dimensions") or {}
    if want_dims.get("width") is not None and want_dims["width"] != width:
        return False
    if want_dims.get("height") is not None and want_dims["height"] != height:
        return False

    return True


__all__ = [
    "RegistryLoadError",
    "glob_match",
    "load_default_registry",
    "structural_match",
]
