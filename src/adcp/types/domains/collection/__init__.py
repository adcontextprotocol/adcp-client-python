"""Types the AdCP ``collection`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.collection import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.collection.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:49:37 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "BaseCollectionSource": "adcp.types.domains.collection.base_collection_source",
    "BaseCollectionSource1": "adcp.types.domains.collection.base_collection_source",
    "BaseCollectionSource2": "adcp.types.domains.collection.base_collection_source",
    "BaseCollectionSource3": "adcp.types.domains.collection.base_collection_source",
    "ChangeSummary": "adcp.types.domains.collection.collection_list_changed_webhook",
    "Collection": "adcp.types.domains.collection.get_collection_list_response",
    "CollectionList": "adcp.types.domains.collection.collection_list",
    "CollectionListChangedWebhook": "adcp.types.domains.collection.collection_list_changed_webhook",
    "CollectionListFilters": "adcp.types.domains.collection.collection_list_filters",
    "CoverageGap": "adcp.types.domains.collection.get_collection_list_response",
    "CreateCollectionListRequest": "adcp.types.domains.collection.create_collection_list_request",
    "CreateCollectionListResponse": "adcp.types.domains.collection.create_collection_list_response",
    "DeleteCollectionListRequest": "adcp.types.domains.collection.delete_collection_list_request",
    "DeleteCollectionListResponse": "adcp.types.domains.collection.delete_collection_list_response",
    "DistributionId": "adcp.types.domains.collection.get_collection_list_response",
    "ExcludeDistributionId": "adcp.types.domains.collection.collection_list_filters",
    "GetCollectionListRequest": "adcp.types.domains.collection.get_collection_list_request",
    "GetCollectionListResponse": "adcp.types.domains.collection.get_collection_list_response",
    "Identifier": "adcp.types.domains.collection.base_collection_source",
    "ListCollectionListsRequest": "adcp.types.domains.collection.list_collection_lists_request",
    "ListCollectionListsResponse": "adcp.types.domains.collection.list_collection_lists_response",
    "Pagination": "adcp.types.domains.collection.get_collection_list_request",
    "UpdateCollectionListRequest": "adcp.types.domains.collection.update_collection_list_request",
    "UpdateCollectionListResponse": "adcp.types.domains.collection.update_collection_list_response",
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
