"""Types the AdCP ``collection`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.collection import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.collection.<schema>``.
Nothing here is renamed.

This facade is LAZY (PEP 562). It binds no name at import time, because an
eager one makes every leaf of this domain depend on every other leaf: a
submodule import runs this ``__init__``, so one leaf's dependencies become
the whole domain's. That turned a benign submodule-level cycle between
``core`` and ``formats.canonical`` -- two domains whose schemas reference
each other, which Python handles -- into a package-level one that made all
16 modules under ``formats/canonical/`` unimportable on their own.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-16 22:14:49 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The whole importable surface, so type checkers and IDEs see it. Runtime
    # resolution is ``__getattr__`` below; mypy never sees that, so a typo'd
    # import is still an error rather than ``object``.
    from adcp.types.domains.collection.base_collection_source import (
        BaseCollectionSource,
        BaseCollectionSource1,
        BaseCollectionSource2,
        BaseCollectionSource3,
        Identifier,
    )
    from adcp.types.domains.collection.collection_list import CollectionList
    from adcp.types.domains.collection.collection_list_changed_webhook import (
        ChangeSummary,
        CollectionListChangedWebhook,
    )
    from adcp.types.domains.collection.collection_list_filters import (
        CollectionListFilters,
        ExcludeDistributionId,
    )
    from adcp.types.domains.collection.create_collection_list_request import (
        CreateCollectionListRequest,
    )
    from adcp.types.domains.collection.create_collection_list_response import (
        CreateCollectionListResponse,
    )
    from adcp.types.domains.collection.delete_collection_list_request import (
        DeleteCollectionListRequest,
    )
    from adcp.types.domains.collection.delete_collection_list_response import (
        DeleteCollectionListResponse,
    )
    from adcp.types.domains.collection.get_collection_list_request import (
        GetCollectionListRequest,
        Pagination,
    )
    from adcp.types.domains.collection.get_collection_list_response import (
        Collection,
        CoverageGap,
        DistributionId,
        GetCollectionListResponse,
    )
    from adcp.types.domains.collection.list_collection_lists_request import (
        ListCollectionListsRequest,
    )
    from adcp.types.domains.collection.list_collection_lists_response import (
        ListCollectionListsResponse,
    )
    from adcp.types.domains.collection.update_collection_list_request import (
        UpdateCollectionListRequest,
    )
    from adcp.types.domains.collection.update_collection_list_response import (
        UpdateCollectionListResponse,
    )

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "collection.base_collection_source": (
        "BaseCollectionSource",
        "BaseCollectionSource1",
        "BaseCollectionSource2",
        "BaseCollectionSource3",
        "Identifier",
    ),
    "collection.collection_list": ("CollectionList",),
    "collection.collection_list_changed_webhook": (
        "ChangeSummary",
        "CollectionListChangedWebhook",
    ),
    "collection.collection_list_filters": (
        "CollectionListFilters",
        "ExcludeDistributionId",
    ),
    "collection.create_collection_list_request": ("CreateCollectionListRequest",),
    "collection.create_collection_list_response": ("CreateCollectionListResponse",),
    "collection.delete_collection_list_request": ("DeleteCollectionListRequest",),
    "collection.delete_collection_list_response": ("DeleteCollectionListResponse",),
    "collection.get_collection_list_request": (
        "GetCollectionListRequest",
        "Pagination",
    ),
    "collection.get_collection_list_response": (
        "Collection",
        "CoverageGap",
        "DistributionId",
        "GetCollectionListResponse",
    ),
    "collection.list_collection_lists_request": ("ListCollectionListsRequest",),
    "collection.list_collection_lists_response": ("ListCollectionListsResponse",),
    "collection.update_collection_list_request": ("UpdateCollectionListRequest",),
    "collection.update_collection_list_response": ("UpdateCollectionListResponse",),
}

if not TYPE_CHECKING:
    import importlib

    _DECLARING_MODULE = {name: module for module, names in _DECLARED_BY.items() for name in names}

    def __getattr__(name: str) -> object:
        """Resolve a re-exported name by importing the ONE module declaring it."""
        module = _DECLARING_MODULE.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(f"adcp.types.domains.{module}"), name)
        globals()[name] = value  # cache: __getattr__ fires once per name
        return value

    def __dir__() -> list[str]:
        return sorted(set(__all__) | set(globals()))


# Explicit exports
__all__ = [
    "BaseCollectionSource",
    "BaseCollectionSource1",
    "BaseCollectionSource2",
    "BaseCollectionSource3",
    "ChangeSummary",
    "Collection",
    "CollectionList",
    "CollectionListChangedWebhook",
    "CollectionListFilters",
    "CoverageGap",
    "CreateCollectionListRequest",
    "CreateCollectionListResponse",
    "DeleteCollectionListRequest",
    "DeleteCollectionListResponse",
    "DistributionId",
    "ExcludeDistributionId",
    "GetCollectionListRequest",
    "GetCollectionListResponse",
    "Identifier",
    "ListCollectionListsRequest",
    "ListCollectionListsResponse",
    "Pagination",
    "UpdateCollectionListRequest",
    "UpdateCollectionListResponse",
]
