"""Shared lazy-facade machinery for the curated partial type modules.

Each curated partial module (:mod:`adcp.types.media_buy`, ``creative``, ...) is
a thin, lazy re-export of a domain-grouped subset of :mod:`adcp.types`. They all
need the same runtime behaviour — resolve a name through ``adcp.types`` on first
access and cache it — so that behaviour lives here once rather than being copied
into each module (where it would drift).

The partial modules call this from a ``if not TYPE_CHECKING`` block so type
checkers never see a module-level ``__getattr__`` (which would silence
missing-attribute errors); they get the surface from each module's explicit
``TYPE_CHECKING`` re-export block instead.
"""

from __future__ import annotations

from collections.abc import Callable


def lazy_partial_surface(
    module_name: str,
    all_names: list[str],
    module_globals: dict[str, object],
) -> tuple[Callable[[str], object], Callable[[], list[str]]]:
    """Build the ``(__getattr__, __dir__)`` pair for a curated partial module.

    Names in ``all_names`` resolve through :mod:`adcp.types` (the single source
    of truth) and are cached into ``module_globals`` so each fires at most once;
    anything else raises :class:`AttributeError`.
    """
    resolvable = frozenset(all_names)

    # Inner functions are named without dunders (N807) and bound to the
    # module's ``__getattr__`` / ``__dir__`` at the call site; PEP 562 only
    # cares about the bound names, not the functions' own ``__name__``.
    def module_getattr(name: str) -> object:
        if name in resolvable:
            import adcp.types

            value = getattr(adcp.types, name)
            module_globals[name] = value  # cache: fires once per name
            return value
        raise AttributeError(f"module {module_name!r} has no attribute {name!r}")

    def module_dir() -> list[str]:
        return sorted(all_names)

    return module_getattr, module_dir
