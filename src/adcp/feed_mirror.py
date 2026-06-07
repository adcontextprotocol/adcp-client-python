"""In-memory mirror of an AdCP agent's wholesale product and signal feeds.

A :class:`FeedMirror` maintains a local replica of a seller's wholesale
product feed and/or wholesale signal feed so a buyer can read priced
inventory without a round trip on every lookup. It implements the AdCP 3.1
wholesale-feed sync pattern:

- **bootstrap** — initial full load via ``get_products`` with
  ``buying_mode="wholesale"`` and ``get_signals`` with
  ``discovery_mode="wholesale"``, walking pagination cursors and storing the
  returned ``wholesale_feed_version`` / ``pricing_version`` / ``cache_scope``
  per feed.
- **conditional refresh** — re-reads each feed presenting the cached
  ``if_wholesale_feed_version`` (and ``if_pricing_version`` when known). When
  the seller short-circuits with ``unchanged: true`` the local replica is
  left untouched.
- **incremental webhook application** — applies a single
  :class:`WholesaleFeedWebhook` (``product.*`` / ``signal.*`` events) to the
  in-memory index without a follow-up read, since wholesale-feed events are
  denormalized.
- **bulk-change re-bootstrap** — a ``wholesale_feed.bulk_change`` webhook
  re-bootstraps only the feed named by ``affected_entity_type``.

The mirror builds into local maps and swaps atomically on a successful fetch,
so an ``unchanged`` short-circuit never wipes the replica.

This is a client-side helper: it consumes seller-emitted wholesale-feed
state. It is distinct from buyer-provided catalogs managed by
``sync_catalogs``.

Example::

    from adcp import ADCPClient, FeedMirror
    from adcp.types import WholesaleFeedWebhook

    mirror = FeedMirror(client)
    await mirror.bootstrap()
    print(f"mirroring {len(mirror.products)} products")

    # In your webhook receiver, after signature/auth validation:
    await mirror.apply_webhook(WholesaleFeedWebhook.model_validate(body))
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from adcp.types import (
    AccountReference,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
    PaginationRequest,
    Product,
    Signal,
    WholesaleFeedWebhook,
)

if TYPE_CHECKING:
    from adcp.types import WholesaleFeedEvent
    from adcp.types.core import TaskResult

logger = logging.getLogger(__name__)

# Default page size for wholesale enumeration. The spec caps max_results at
# 100; the mirror walks cursors until has_more is exhausted.
_DEFAULT_PAGE_LIMIT = 100

FeedEntity = Literal["product", "signal"]
CacheScopeLiteral = Literal["public", "account"]


def _scope_str(scope: Any) -> CacheScopeLiteral:
    """Normalize a CacheScope enum or string to the wire literal.

    Defaults to ``"public"`` for absent/unknown scopes — the spec requires a
    seller to return ``cache_scope: "public"`` for unscoped reads.
    """
    value = scope.value if hasattr(scope, "value") else scope
    return "account" if value == "account" else "public"


# Callback invoked after a webhook event mutates the mirror. Receives the
# applied event so adopters can fan out to audit logs or downstream queues.
EventHandler = Callable[["WholesaleFeedEvent"], None]


@runtime_checkable
class FeedMirrorClient(Protocol):
    """The subset of :class:`adcp.ADCPClient` a :class:`FeedMirror` calls.

    Declared as a Protocol so tests can inject a minimal stub and so the
    mirror does not import the concrete client (avoiding a cycle).
    """

    async def get_products(
        self, request: GetProductsRequest
    ) -> TaskResult[GetProductsResponse]: ...

    async def get_signals(self, request: GetSignalsRequest) -> TaskResult[GetSignalsResponse]: ...


@runtime_checkable
class FeedStateStore(Protocol):
    """Optional persistence hook for cached feed-version state.

    Lets adopters survive a process restart without re-bootstrapping from
    scratch: persist :class:`FeedState` per feed on save, restore on load.
    All methods are async to allow database-backed implementations.
    """

    async def load(self, entity: FeedEntity) -> FeedState | None:
        """Return the persisted state for a feed, or ``None`` if absent."""
        ...

    async def save(self, entity: FeedEntity, state: FeedState) -> None:
        """Persist the state for a feed."""
        ...


@dataclass
class FeedState:
    """Cached version tokens for one wholesale feed.

    The tokens are opaque per the spec — the mirror never inspects or orders
    them, it only echoes the cached ``wholesale_feed_version`` (and
    ``pricing_version`` when present) back on the next conditional read.
    """

    wholesale_feed_version: str | None = None
    pricing_version: str | None = None
    cache_scope: Literal["public", "account"] = "public"


@dataclass
class RefreshResult:
    """Outcome of a :meth:`FeedMirror.refresh` / :meth:`bootstrap` call.

    ``unchanged`` is ``True`` when the seller short-circuited every requested
    feed with ``unchanged: true`` (the replica was not mutated).
    """

    products_unchanged: bool = False
    signals_unchanged: bool = False
    product_count: int = 0
    signal_count: int = 0

    @property
    def unchanged(self) -> bool:
        """True when no requested feed reported a change."""
        return self.products_unchanged and self.signals_unchanged


@dataclass
class _FeedMetadata:
    """Mutable working copy of feed metadata accumulated across pages."""

    wholesale_feed_version: str | None = None
    pricing_version: str | None = None
    cache_scope: Literal["public", "account"] = "public"
    items: dict[str, Any] = field(default_factory=dict)
    unchanged: bool = False


class FeedMirror:
    """In-memory mirror of an agent's wholesale product and signal feeds.

    Args:
        client: An :class:`adcp.ADCPClient` (or any object satisfying
            :class:`FeedMirrorClient`) bound to the target agent.
        account: Optional account scope for wholesale reads. Wholesale-feed
            webhooks are account-anchored, so pass the same account used to
            register ``notification_configs[]`` so repair reads reconcile the
            correct public or account overlay.
        on_event: Optional callback invoked after each webhook event mutates
            the mirror. Receives the applied :class:`WholesaleFeedEvent`.
        state_store: Optional :class:`FeedStateStore` for persisting cached
            feed-version tokens across process restarts.
        page_limit: Page size for wholesale enumeration (max 100 per spec).
    """

    def __init__(
        self,
        client: FeedMirrorClient,
        *,
        account: AccountReference | None = None,
        on_event: EventHandler | None = None,
        state_store: FeedStateStore | None = None,
        page_limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> None:
        self._client = client
        self._account = account
        self._on_event = on_event
        self._state_store = state_store
        self._page_limit = page_limit

        self._products: dict[str, Product] = {}
        self._signals: dict[str, Signal] = {}
        self._product_state = FeedState()
        self._signal_state = FeedState()

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

    @property
    def products(self) -> dict[str, Product]:
        """Live view of the product index keyed by ``product_id``."""
        return self._products

    @property
    def signals(self) -> dict[str, Signal]:
        """Live view of the signal index keyed by ``signal_agent_segment_id``."""
        return self._signals

    @property
    def product_state(self) -> FeedState:
        """Cached version tokens for the wholesale product feed."""
        return self._product_state

    @property
    def signal_state(self) -> FeedState:
        """Cached version tokens for the wholesale signal feed."""
        return self._signal_state

    def get_product(self, product_id: str) -> Product | None:
        """Return a mirrored product by id, or ``None``."""
        return self._products.get(product_id)

    def get_signal(self, signal_agent_segment_id: str) -> Signal | None:
        """Return a mirrored signal by id, or ``None``."""
        return self._signals.get(signal_agent_segment_id)

    # ------------------------------------------------------------------
    # Bootstrap / refresh
    # ------------------------------------------------------------------

    async def bootstrap(self, entities: FeedEntity | Literal["all"] = "all") -> RefreshResult:
        """Initial full load of the wholesale feed(s).

        Restores any persisted :class:`FeedState` from the configured
        ``state_store`` first, so a bootstrap after a restart presents the
        cached version on the conditional read and short-circuits when the
        seller has nothing new.

        Args:
            entities: ``"all"`` (default), ``"product"``, or ``"signal"`` to
                bootstrap a single feed.
        """
        if self._state_store is not None:
            await self._restore_state(entities)
        return await self._fetch(entities)

    async def refresh(self, entities: FeedEntity | Literal["all"] = "all") -> RefreshResult:
        """Conditional re-read of the wholesale feed(s).

        Presents the cached ``if_wholesale_feed_version`` (and
        ``if_pricing_version`` when known). When the seller returns
        ``unchanged: true`` the replica is left untouched and the result's
        ``*_unchanged`` flag is set.
        """
        return await self._fetch(entities)

    async def _fetch(self, entities: FeedEntity | Literal["all"]) -> RefreshResult:
        result = RefreshResult()
        do_products = entities in ("all", "product")
        do_signals = entities in ("all", "signal")

        if do_products:
            meta = await self._fetch_products()
            self._commit("product", meta)
            result.products_unchanged = meta.unchanged
        else:
            # Treat a feed we didn't fetch as unchanged so RefreshResult.unchanged
            # reflects only the feeds the caller asked about.
            result.products_unchanged = True

        if do_signals:
            meta = await self._fetch_signals()
            self._commit("signal", meta)
            result.signals_unchanged = meta.unchanged
        else:
            result.signals_unchanged = True

        result.product_count = len(self._products)
        result.signal_count = len(self._signals)
        return result

    async def _fetch_products(self) -> _FeedMetadata:
        state = self._product_state
        meta = _FeedMetadata(
            wholesale_feed_version=state.wholesale_feed_version,
            pricing_version=state.pricing_version,
            cache_scope=state.cache_scope,
        )
        cursor: str | None = None
        first_page = True
        while True:
            request = self._build_products_request(cursor, first_page, state)
            task = await self._client.get_products(request)
            body = self._require_body(task, "get_products")
            if body.unchanged:
                self._merge_metadata(meta, body)
                meta.unchanged = True
                return meta
            for product in body.products or []:
                meta.items[product.product_id] = product
            self._merge_metadata(meta, body)
            cursor = self._next_cursor(body)
            first_page = False
            if cursor is None:
                return meta

    async def _fetch_signals(self) -> _FeedMetadata:
        state = self._signal_state
        meta = _FeedMetadata(
            wholesale_feed_version=state.wholesale_feed_version,
            pricing_version=state.pricing_version,
            cache_scope=state.cache_scope,
        )
        cursor: str | None = None
        first_page = True
        while True:
            request = self._build_signals_request(cursor, first_page, state)
            task = await self._client.get_signals(request)
            body = self._require_body(task, "get_signals")
            if body.unchanged:
                self._merge_metadata(meta, body)
                meta.unchanged = True
                return meta
            for signal in body.signals or []:
                meta.items[signal.signal_agent_segment_id] = signal
            self._merge_metadata(meta, body)
            cursor = self._next_cursor(body)
            first_page = False
            if cursor is None:
                return meta

    def _build_products_request(
        self, cursor: str | None, first_page: bool, state: FeedState
    ) -> GetProductsRequest:
        kwargs: dict[str, Any] = {
            "buying_mode": "wholesale",
            "pagination": PaginationRequest(max_results=self._page_limit, cursor=cursor),
        }
        if self._account is not None:
            kwargs["account"] = self._account
        # Conditional fetch on the first page only — pagination.cursor is not
        # part of the version scoping tuple, so a mid-walk page must not carry
        # the if_* tokens.
        if first_page and state.wholesale_feed_version is not None:
            kwargs["if_wholesale_feed_version"] = state.wholesale_feed_version
            if state.pricing_version is not None:
                kwargs["if_pricing_version"] = state.pricing_version
        return GetProductsRequest(**kwargs)

    def _build_signals_request(
        self, cursor: str | None, first_page: bool, state: FeedState
    ) -> GetSignalsRequest:
        kwargs: dict[str, Any] = {
            "discovery_mode": "wholesale",
            "pagination": PaginationRequest(max_results=self._page_limit, cursor=cursor),
        }
        if self._account is not None:
            kwargs["account"] = self._account
        if first_page and state.wholesale_feed_version is not None:
            kwargs["if_wholesale_feed_version"] = state.wholesale_feed_version
            if state.pricing_version is not None:
                kwargs["if_pricing_version"] = state.pricing_version
        return GetSignalsRequest(**kwargs)

    @staticmethod
    def _require_body(task: TaskResult[Any], operation: str) -> Any:
        if not task.success or task.data is None:
            raise FeedMirrorError(
                f"{operation} did not return a successful wholesale response: "
                f"{task.error or task.message or task.status}"
            )
        return task.data

    @staticmethod
    def _merge_metadata(meta: _FeedMetadata, body: Any) -> None:
        if body.wholesale_feed_version is not None:
            meta.wholesale_feed_version = body.wholesale_feed_version
        if body.pricing_version is not None:
            meta.pricing_version = body.pricing_version
        if body.cache_scope is not None:
            meta.cache_scope = _scope_str(body.cache_scope)

    @staticmethod
    def _next_cursor(body: Any) -> str | None:
        pagination = body.pagination
        if pagination is not None and pagination.has_more:
            return cast("str | None", pagination.cursor)
        return None

    def _commit(self, entity: FeedEntity, meta: _FeedMetadata) -> None:
        state = self._product_state if entity == "product" else self._signal_state
        state.wholesale_feed_version = meta.wholesale_feed_version
        state.pricing_version = meta.pricing_version
        state.cache_scope = meta.cache_scope
        if not meta.unchanged:
            # Atomic swap — only replace the live index on a fresh fetch so an
            # unchanged short-circuit never wipes the replica.
            if entity == "product":
                self._products = meta.items
            else:
                self._signals = meta.items

    async def _restore_state(self, entities: FeedEntity | Literal["all"]) -> None:
        assert self._state_store is not None
        if entities in ("all", "product"):
            restored = await self._state_store.load("product")
            if restored is not None:
                self._product_state = restored
        if entities in ("all", "signal"):
            restored = await self._state_store.load("signal")
            if restored is not None:
                self._signal_state = restored

    async def _persist_state(self, entity: FeedEntity) -> None:
        if self._state_store is None:
            return
        state = self._product_state if entity == "product" else self._signal_state
        await self._state_store.save(entity, state)

    # ------------------------------------------------------------------
    # Incremental webhook application
    # ------------------------------------------------------------------

    async def apply_webhook(self, webhook: WholesaleFeedWebhook) -> RefreshResult | None:
        """Apply one wholesale-feed webhook to the local mirror.

        Call this from your HTTP webhook receiver after signature/auth
        validation. ``product.*`` / ``signal.*`` events mutate the index in
        place (events are denormalized — no follow-up read needed) and update
        the cached feed version for the affected feed. A
        ``wholesale_feed.bulk_change`` event re-bootstraps only the feed named
        by ``affected_entity_type`` and returns the :class:`RefreshResult`.

        Raises:
            FeedMirrorError: When the webhook envelope is internally
                inconsistent (``notification_type`` / ``notification_id`` do
                not match the embedded event).
        """
        event = webhook.event
        if webhook.notification_type != event.event_type:
            raise FeedMirrorError("webhook notification_type does not match event.event_type")
        if str(webhook.notification_id) != str(event.event_id):
            raise FeedMirrorError("webhook notification_id does not match event.event_id")

        if event.event_type == "wholesale_feed.bulk_change":
            affected = self._bulk_change_entity(event)
            result = await self.refresh(affected)
            self._dispatch(event)
            return result

        self._apply_event(event)
        self._remember_webhook_version(webhook)
        self._dispatch(event)
        await self._persist_state(self._event_entity(event))
        return None

    @staticmethod
    def _bulk_change_entity(event: WholesaleFeedEvent) -> FeedEntity:
        affected: Any = getattr(event.payload, "affected_entity_type", None)
        value = affected.value if hasattr(affected, "value") else affected
        if value == "product":
            return "product"
        if value == "signal":
            return "signal"
        raise FeedMirrorError(
            "wholesale_feed.bulk_change payload missing required affected_entity_type"
        )

    @staticmethod
    def _event_entity(event: WholesaleFeedEvent) -> FeedEntity:
        return "product" if str(event.event_type).startswith("product.") else "signal"

    def _apply_event(self, event: WholesaleFeedEvent) -> None:
        event_type = str(event.event_type)
        payload = event.payload
        if event_type in ("product.created", "product.updated"):
            product = getattr(payload, "product", None)
            if product is not None:
                self._products[product.product_id] = product
        elif event_type == "product.priced":
            existing = self._products.get(payload.product_id)
            if existing is not None:
                self._products[payload.product_id] = existing.model_copy(
                    update={"pricing_options": payload.pricing_options}
                )
        elif event_type == "product.removed":
            self._products.pop(payload.product_id, None)
        elif event_type in ("signal.created", "signal.updated"):
            signal = getattr(payload, "signal", None)
            if signal is not None:
                self._signals[signal.signal_agent_segment_id] = signal
        elif event_type == "signal.priced":
            existing_signal = self._signals.get(payload.signal_agent_segment_id)
            if existing_signal is not None:
                self._signals[payload.signal_agent_segment_id] = existing_signal.model_copy(
                    update={"pricing_options": payload.pricing_options}
                )
        elif event_type == "signal.removed":
            self._signals.pop(payload.signal_agent_segment_id, None)

    def _remember_webhook_version(self, webhook: WholesaleFeedWebhook) -> None:
        entity = self._event_entity(webhook.event)
        state = self._product_state if entity == "product" else self._signal_state
        state.wholesale_feed_version = webhook.wholesale_feed_version
        state.cache_scope = _scope_str(webhook.cache_scope)

    def _dispatch(self, event: WholesaleFeedEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            logger.exception("FeedMirror on_event handler raised")


class FeedMirrorError(Exception):
    """Raised when a wholesale-feed read fails or a webhook is inconsistent."""
