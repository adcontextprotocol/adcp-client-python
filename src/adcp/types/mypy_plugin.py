"""Mypy plugin that powers :data:`adcp.types.SchemaVariant`.

Without this plugin, mypy reports ``SchemaVariant[X]`` as an unanalyzed
generic and raises override-compat errors. With it, every annotation
of the form ``SchemaVariant[X]`` resolves to ``Any`` at type-check
time, suppressing the Liskov check on cross-class field overrides
(see :mod:`adcp.types.variants`).

**Activation**. Add the plugin to ``[tool.mypy]`` in your adopter
project's ``pyproject.toml``::

    [tool.mypy]
    plugins = ["adcp.types.mypy_plugin"]

Or in ``mypy.ini``::

    [mypy]
    plugins = adcp.types.mypy_plugin

The plugin is a no-op for code that doesn't reference
``adcp.types.SchemaVariant`` — adopters can enable it globally without
side effects on unrelated annotations.

**Why the Any rewrite is safe**. The override-compat check fires on
Pydantic field overrides where the child substitutes a sibling class
for the parent's declared type. Mypy correctly flags this as a Liskov
violation under strict typing. Adopters who reach for ``SchemaVariant``
are explicitly opting out of the LSP check for that field — the type
system semantics inside the override body widen to ``Any``, but the
runtime contract (Pydantic validation against the wrapped type) is
unchanged. The plugin makes the opt-out greppable and tied to a
specific named marker rather than scattered ``# type: ignore``.
"""

from __future__ import annotations

from collections.abc import Callable

from mypy.plugin import AnalyzeTypeContext, Plugin
from mypy.types import AnyType, Type, TypeOfAny

_SCHEMA_VARIANT_FULLNAME = "adcp.types.variants.SchemaVariant"


def _analyze_schema_variant(ctx: AnalyzeTypeContext) -> Type:
    """Rewrite ``SchemaVariant[T]`` annotations to ``Any`` for mypy.

    The runtime collapses ``SchemaVariant[T]`` to ``T`` (see
    :class:`adcp.types.variants.SchemaVariant`), but at type-check time
    we want Liskov-permissive behavior on override sites — mypy treats
    ``Any`` as bivariant with any concrete type, so the override-compat
    check passes regardless of the parent field's declared type.

    The wrapped type ``T`` is intentionally dropped from the mypy view:
    adopters using ``SchemaVariant`` have explicitly opted out of
    static checking on the field. If they want precise inference back
    inside the override body, the pattern is
    ``typing.cast(list[MyT], self.field)``.
    """
    return AnyType(TypeOfAny.from_omitted_generics)


class AdcpTypesPlugin(Plugin):
    """Entry-point plugin class.

    Registers a single type-analyze hook for
    ``adcp.types.variants.SchemaVariant``. All other types pass through
    mypy's default analyzer unchanged.
    """

    def get_type_analyze_hook(self, fullname: str) -> Callable[[AnalyzeTypeContext], Type] | None:
        if fullname == _SCHEMA_VARIANT_FULLNAME:
            return _analyze_schema_variant
        return None


def plugin(version: str) -> type[AdcpTypesPlugin]:
    """Mypy plugin factory — returns the plugin class.

    ``version`` is mypy's reported plugin API version (a string like
    ``"1.13.0"``). The plugin doesn't currently branch on it; if a
    future mypy release changes ``AnalyzeTypeContext`` semantics in a
    way we need to handle, branch here.
    """
    del version  # unused — kept for the mypy plugin protocol
    return AdcpTypesPlugin
