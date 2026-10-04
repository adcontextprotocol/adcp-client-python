"""Types the AdCP ``collection`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.collection import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.collection.<schema>``.
Nothing here is renamed.

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:33:59 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.collection.base_collection_source import (
    BaseCollectionSource,
    BaseCollectionSource1,
    BaseCollectionSource2,
    BaseCollectionSource3,
    Identifier,
)
from adcp.types.generated_poc.collection.collection_list import CollectionList
from adcp.types.generated_poc.collection.collection_list_changed_webhook import (
    ChangeSummary,
    CollectionListChangedWebhook,
)
from adcp.types.generated_poc.collection.collection_list_filters import (
    CollectionListFilters,
    ExcludeDistributionId,
)
from adcp.types.generated_poc.collection.create_collection_list_request import (
    CreateCollectionListRequest,
)
from adcp.types.generated_poc.collection.create_collection_list_response import (
    CreateCollectionListResponse,
)
from adcp.types.generated_poc.collection.delete_collection_list_request import (
    DeleteCollectionListRequest,
)
from adcp.types.generated_poc.collection.delete_collection_list_response import (
    DeleteCollectionListResponse,
)
from adcp.types.generated_poc.collection.get_collection_list_request import (
    GetCollectionListRequest,
    Pagination,
)
from adcp.types.generated_poc.collection.get_collection_list_response import (
    Collection,
    CoverageGap,
    DistributionId,
    GetCollectionListResponse,
)
from adcp.types.generated_poc.collection.list_collection_lists_request import (
    ListCollectionListsRequest,
)
from adcp.types.generated_poc.collection.list_collection_lists_response import (
    ListCollectionListsResponse,
)
from adcp.types.generated_poc.collection.update_collection_list_request import (
    UpdateCollectionListRequest,
)
from adcp.types.generated_poc.collection.update_collection_list_response import (
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
