"""Mypy plugin that powers :data:`adcp.types.SchemaVariant` (#710).

Rewrites every ``SchemaVariant[T]`` annotation to ``Any`` at type-check
time so cross-class Pydantic field overrides pass the Liskov check
without ``# type: ignore``. No-op for unrelated annotations.

Activate in adopter projects::

    [tool.mypy]
    plugins = ["adcp.types.mypy_plugin"]
"""

from __future__ import annotations

from collections.abc import Callable

from mypy.plugin import AnalyzeTypeContext, Plugin
from mypy.types import AnyType, Type, TypeOfAny

_SCHEMA_VARIANT_FULLNAME = "adcp.types.variants.SchemaVariant"


def _analyze_schema_variant(ctx: AnalyzeTypeContext) -> Type:
    # ``TypeOfAny.special_form`` is the right flavor — the marker is a
    # typing primitive whose ``Any`` reduction is intentional design.
    # ``from_omitted_generics`` would misclassify under adopter
    # diagnostics like ``--disallow-any-generics``.
    return AnyType(TypeOfAny.special_form)


class AdcpTypesPlugin(Plugin):
    def get_type_analyze_hook(self, fullname: str) -> Callable[[AnalyzeTypeContext], Type] | None:
        if fullname == _SCHEMA_VARIANT_FULLNAME:
            return _analyze_schema_variant
        return None


def plugin(_version: str) -> type[AdcpTypesPlugin]:
    return AdcpTypesPlugin
