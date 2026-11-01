"""Types the AdCP ``aao`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.aao import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.aao.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:44:03 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.types.domains.aao.agent_publishers import (
        AaoDirectoryAgentPublishersInverseLookupResponse,
        DiscoveryMethod,
        PublisherEntry,
        Status,
    )

# Explicit exports
__all__ = [
    "AaoDirectoryAgentPublishersInverseLookupResponse",
    "DiscoveryMethod",
    "PublisherEntry",
    "Status",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "AaoDirectoryAgentPublishersInverseLookupResponse": "adcp.types.domains.aao.agent_publishers",
    "DiscoveryMethod": "adcp.types.domains.aao.agent_publishers",
    "PublisherEntry": "adcp.types.domains.aao.agent_publishers",
    "Status": "adcp.types.domains.aao.agent_publishers",
}

if not TYPE_CHECKING:
    import importlib as _importlib

    def __getattr__(name: str) -> object:
        module_name = _DECLARED_IN.get(name)
        if module_name is None:
            # Not a declared name: let the import system try a submodule, or fail.
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(_importlib.import_module(module_name), name)
        globals()[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted(set(globals()) | set(__all__))
