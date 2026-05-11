"""Per-tool adapters for buyers on legacy AdCP wire shapes.

The dispatcher consults :func:`get_legacy_adapter` when the buyer's
``adcp_version`` / ``adcp_major_version`` resolves into
:data:`LEGACY_ADAPTER_VERSIONS`. If an adapter is registered for the
``(version, tool)`` pair, the request is translated to the current
wire shape before validation + handler dispatch. If no adapter is
registered for that tool at that version, the dispatcher surfaces
``INVALID_REQUEST`` — the legacy version doesn't expose the tool.

Architecturally this replaces the heuristic
:func:`adcp.server.spec_compat.spec_compat_hooks` model. Each adapter
is hand-written (or, in time, codegen'd from declarative wire-delta
specs) and tested end-to-end so the translation is auditable rather
than implicit.

Adapters register themselves at import time via
:func:`register_adapter`. Importing :mod:`adcp.compat.legacy.v2_5`
populates the v2.5 registry; see that submodule's docstring for the
list of supported tools.

Mirrors ``src/lib/adapters/legacy/v2-5/`` in the TypeScript SDK
(``getV25Adapter`` / ``listV25AdapterTools``).
"""

from __future__ import annotations

from typing import Final

from adcp.compat.legacy.types import AdapterPair

#: Versions handled via the legacy-adapter path. Distinct from
#: ``COMPATIBLE_ADCP_VERSIONS`` in :mod:`adcp._version`, which lists the
#: versions the SDK natively validates against.
LEGACY_ADAPTER_VERSIONS: Final[tuple[str, ...]] = ("2.5",)

# Per-version adapter module list. Data, not control flow, so adding a
# tool to a version is a one-line append in this dict. Mapping is
# ``wire_version`` → ``(package_segment, (tool_module, ...))`` where
# ``package_segment`` is the Python-safe subpackage under
# ``adcp.compat.legacy`` (we use ``v2_5`` because Python identifiers
# can't start with a digit). ``_ensure_loaded`` imports each tool
# module and reads its top-level ``ADAPTER`` constant.
_VERSION_MODULES: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "2.5": ("v2_5", ("sync_creatives",)),
}


_REGISTRY: dict[tuple[str, str], AdapterPair] = {}


def register_adapter(version: str, adapter: AdapterPair) -> None:
    """Register an :class:`AdapterPair` under ``(version, tool_name)``.

    Idempotent — re-registering the same pair (same callables) is a
    no-op. Re-registering a *different* pair for the same key raises
    :class:`ValueError`; tests should call :func:`_reset_registry_for_tests`
    if they need to swap an adapter mid-suite.

    Adapters self-register at module import time; the framework imports
    :mod:`adcp.compat.legacy.v2_5` lazily on first dispatch so adopters
    that don't speak legacy don't pay the cost.
    """
    if version not in LEGACY_ADAPTER_VERSIONS:
        raise ValueError(
            f"register_adapter: version {version!r} is not in "
            f"LEGACY_ADAPTER_VERSIONS={list(LEGACY_ADAPTER_VERSIONS)}. "
            "Add the version to the constant first."
        )
    key = (version, adapter.tool_name)
    existing = _REGISTRY.get(key)
    if existing is not None and existing is not adapter:
        raise ValueError(
            f"register_adapter: an adapter is already registered for "
            f"{key!r} ({existing!r}); refusing to overwrite with "
            f"{adapter!r}."
        )
    _REGISTRY[key] = adapter


def get_legacy_adapter(version: str, tool_name: str) -> AdapterPair | None:
    """Return the adapter for ``(version, tool_name)`` or ``None``.

    ``None`` means "no translation registered for this tool at this
    version" — the dispatcher converts that into ``INVALID_REQUEST``
    because the buyer claimed a legacy version this seller doesn't
    serve the tool on.
    """
    _ensure_loaded(version)
    return _REGISTRY.get((version, tool_name))


def list_legacy_adapter_tools(version: str) -> list[str]:
    """Tools with a registered adapter at this legacy version."""
    _ensure_loaded(version)
    return sorted(tool for (v, tool) in _REGISTRY if v == version)


def _ensure_loaded(version: str) -> None:
    """Lazily ensure the per-version adapter package's ``AdapterPair``
    constants are registered.

    Checks the live registry rather than a one-shot ``_LOADED`` marker
    so :func:`_reset_registry_for_tests` can wipe state and the next
    call re-registers from the already-imported modules. Each adapter
    module exposes ``ADAPTER`` as its registration constant; that's
    the contract the dispatcher relies on.

    Driven by ``_VERSION_MODULES`` — adding a tool to a version is a
    one-line append to that dict, no control-flow change here.
    """
    if any(v == version for (v, _tool) in _REGISTRY):
        return
    entry = _VERSION_MODULES.get(version)
    if entry is None:
        return
    pkg_segment, modules = entry
    import importlib

    for mod_name in modules:
        module = importlib.import_module(f"adcp.compat.legacy.{pkg_segment}.{mod_name}")
        register_adapter(version, module.ADAPTER)


def _reset_registry_for_tests() -> None:
    """Test-only: drop all registrations. Subsequent lookups re-register
    from the per-version adapter modules."""
    _REGISTRY.clear()


__all__ = [
    "AdapterPair",
    "LEGACY_ADAPTER_VERSIONS",
    "get_legacy_adapter",
    "list_legacy_adapter_tools",
    "register_adapter",
]
