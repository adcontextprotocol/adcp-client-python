"""PropertyListsPlatform + CollectionListsPlatform — list-publishing
specialisms.

Two distinct specialisms with parallel CRUD shapes, both publishing
authorized lists with token-issuance semantics:

* **``property-lists``** — agent publishes/maintains authorized
  property lists (which sellers can sell what for which advertisers;
  buyer-side authorization graphs). Sellers FETCH and validate
  against these.
* **``collection-lists``** — agent publishes/maintains authorized
  collection lists (program/show-level brand safety via
  IMDb / Gracenote / EIDR ids). Sellers FETCH and apply for
  inventory filtering.

Both have CRUD on the same shape — create, update, get, list, delete
— just on different list types. They could fold into one interface
if your adopter implements both; today they're separated to match
the spec specialism-per-list-type shape.

Token-issuance semantics: ``create_*`` returns a one-time
``fetch_token`` sellers store in their secret manager; ``delete_*``
revokes the token. Tokens are scoped per-seller for revocation;
compromise-driven revocation MUST also trigger the delete path.

Mirrors the JS-side ``PropertyListsPlatform`` and
``CollectionListsPlatform`` interfaces at
``src/lib/server/decisioning/specialisms/lists.ts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import MaybeAsync
    from adcp.types import (
        CreateCollectionListRequest,
        CreateCollectionListResponse,
        CreatePropertyListRequest,
        CreatePropertyListResponse,
        DeleteCollectionListRequest,
        DeleteCollectionListResponse,
        DeletePropertyListRequest,
        DeletePropertyListResponse,
        GetCollectionListRequest,
        GetCollectionListResponse,
        GetPropertyListRequest,
        GetPropertyListResponse,
        ListCollectionListsRequest,
        ListCollectionListsResponse,
        ListPropertyListsRequest,
        ListPropertyListsResponse,
        UpdateCollectionListRequest,
        UpdateCollectionListResponse,
        UpdatePropertyListRequest,
        UpdatePropertyListResponse,
    )


#: Per-platform metadata generic.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class PropertyListsPlatform(Protocol, Generic[TMeta]):
    """Property-list CRUD with fetch-token issuance semantics.

    Methods may be sync (return ``T`` directly) or async (return
    ``Awaitable[T]``); the dispatch adapter detects via
    :func:`asyncio.iscoroutinefunction` and runs sync methods on a
    thread pool.

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    rejection (``REFERENCE_NOT_FOUND``, ``POLICY_VIOLATION``).
    """

    def create_property_list(
        self,
        req: CreatePropertyListRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[CreatePropertyListResponse]:
        """Create a property list.

        Returns a ``fetch_token`` the buyer stores in their secret
        manager. Token is scoped to this ``list_id``; MUST NOT be
        reused across lists.
        """
        ...

    def update_property_list(
        self,
        req: UpdatePropertyListRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[UpdatePropertyListResponse]:
        """Patch an existing property list."""
        ...

    def get_property_list(
        self,
        req: GetPropertyListRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetPropertyListResponse]:
        """Read a property list by id.

        Sellers call this with the ``fetch_token`` from
        :meth:`create_property_list`.
        """
        ...

    def list_property_lists(
        self,
        req: ListPropertyListsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListPropertyListsResponse]:
        """Discover property lists the caller is authorized to read."""
        ...

    def delete_property_list(
        self,
        req: DeletePropertyListRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[DeletePropertyListResponse]:
        """Delete a property list.

        MUST revoke the ``fetch_token`` immediately and signal cache
        invalidation to sellers (reduced ``cache_valid_until`` or a
        list-changed webhook). Compromise-driven revocation MUST
        also trigger this path.
        """
        ...


@runtime_checkable
class CollectionListsPlatform(Protocol, Generic[TMeta]):
    """Collection-list CRUD with fetch-token issuance semantics.

    Parallel shape to :class:`PropertyListsPlatform`; covers
    program-level brand-safety lists (shows, series, podcasts) keyed
    by IMDb / Gracenote / EIDR ids.

    Same security model: ``create_*`` issues a per-seller
    ``fetch_token``, ``delete_*`` revokes it; compromise-driven
    revocation MUST trigger delete.
    """

    def create_collection_list(
        self,
        req: CreateCollectionListRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[CreateCollectionListResponse]:
        """Create a collection list. Returns a per-seller-scoped
        ``fetch_token``."""
        ...

    def update_collection_list(
        self,
        req: UpdateCollectionListRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[UpdateCollectionListResponse]:
        """Patch an existing collection list."""
        ...

    def get_collection_list(
        self,
        req: GetCollectionListRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetCollectionListResponse]:
        """Read a collection list by id."""
        ...

    def list_collection_lists(
        self,
        req: ListCollectionListsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListCollectionListsResponse]:
        """Discover collection lists the caller is authorized to read."""
        ...

    def delete_collection_list(
        self,
        req: DeleteCollectionListRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[DeleteCollectionListResponse]:
        """Delete a collection list. Revokes the ``fetch_token`` and
        signals cache invalidation."""
        ...


__all__ = ["CollectionListsPlatform", "PropertyListsPlatform"]
