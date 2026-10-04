"""Resolve-once wrappers for caller-owned PostgreSQL decisioning stores.

Factories open infrastructure on the serving loop, then construct the original
concrete stores so constructor validation remains authoritative. A successful
resolution is cached; failed initialization can be retried. These wrappers never
open, close, replace or reopen pools themselves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, ClassVar, Generic, TypeAlias, TypeVar

from adcp.decisioning.context import RequestContext
from adcp.decisioning.pg.proposal_store import DEFAULT_TABLE_NAME, PgProposalStore, _migration_sql
from adcp.decisioning.pg.task_registry import PgTaskRegistry
from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox
from adcp.decisioning.proposal_store import ProposalRecord
from adcp.decisioning.recipe import Recipe
from adcp.decisioning.task_registry import (
    TaskTransition,
    TaskWebhookAuthentication,
    _TaskLifecycleObservers,
)

_Store = TypeVar("_Store")
LazyProposalStoreFactory: TypeAlias = Callable[[], PgProposalStore | Awaitable[PgProposalStore]]
TaskRegistryStores: TypeAlias = PgTaskRegistry | tuple[PgTaskRegistry, PgTaskWebhookOutbox]
LazyTaskRegistryFactory: TypeAlias = Callable[
    [], TaskRegistryStores | Awaitable[TaskRegistryStores]
]
LazyTaskWebhookOutboxFactory: TypeAlias = Callable[
    [], PgTaskWebhookOutbox | Awaitable[PgTaskWebhookOutbox]
]


class _LazyStore(Generic[_Store]):
    def __init__(
        self, factory: Callable[[], _Store | Awaitable[_Store]], store_type: type[_Store]
    ) -> None:
        self._factory = factory
        self._store_type = store_type
        self._store: _Store | None = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def resolved(self) -> _Store | None:
        """The cached concrete store, or None; inspection never invokes the factory."""
        return self._store

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            # No successful resource was cached. A failed attempt on a now-dead
            # loop may retry, with a fresh lock. The factory owns failed cleanup.
            if self._store is None and self._loop.is_closed() and not self._lock.locked():
                self._loop = loop
                self._lock = asyncio.Lock()
            else:
                raise RuntimeError(
                    "Lazy PostgreSQL stores cannot be reused on another event loop; "
                    "construct a new wrapper and pool for that loop"
                )

    async def resolve(self) -> _Store:
        """Resolve once on the serving loop. Concurrent callers share success.

        Factory failures/cancellation are not cached. After successful resolution,
        reusing this wrapper on another loop raises, even if the old loop has closed.
        """
        self._bind_loop()
        cached = self._store
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._store
            if cached is not None:
                return cached
            result = self._factory()
            store = await result if isinstance(result, Awaitable) else result
            if not isinstance(store, self._store_type):
                raise TypeError(f"Factory must return {self._store_type.__name__}")
            self._validate(store)
            self._store = store
            return store

    def _validate(self, store: _Store) -> None:
        pass

    def _require_resolved(self) -> _Store:
        store = self._store
        if store is None:
            raise RuntimeError("Await resolve() before using synchronous store helpers")
        # Pure configuration/crypto helpers may run without a loop. When called
        # on an active loop, reject accidental reuse just like async operations.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return store
        self._bind_loop()
        return store


class LazyProposalStore(_LazyStore[PgProposalStore]):
    """Deferred PgProposalStore; durable before and after resolution."""

    is_durable: ClassVar[bool] = True

    def __init__(self, factory: LazyProposalStoreFactory) -> None:
        super().__init__(factory, PgProposalStore)

    def _validate(self, store: PgProposalStore) -> None:
        if store.is_durable is not True:
            raise ValueError("LazyProposalStore requires a durable PostgreSQL store")

    @classmethod
    def migration_sql(cls, table_name: str = DEFAULT_TABLE_NAME) -> dict[str, str]:
        """Return the concrete store's migration SQL without opening infrastructure."""
        return _migration_sql(table_name)

    async def create_schema(self) -> None:
        await (await self.resolve()).create_schema()

    async def put_draft(
        self,
        *,
        proposal_id: str,
        account_id: str,
        recipes: Mapping[str, Recipe],
        proposal_payload: Mapping[str, Any],
    ) -> None:
        await (await self.resolve()).put_draft(
            proposal_id=proposal_id,
            account_id=account_id,
            recipes=recipes,
            proposal_payload=proposal_payload,
        )

    async def get(self, proposal_id: str, *, expected_account_id: str) -> ProposalRecord | None:
        return await (await self.resolve()).get(
            proposal_id, expected_account_id=expected_account_id
        )

    async def commit(
        self,
        proposal_id: str,
        *,
        expires_at: datetime,
        proposal_payload: Mapping[str, Any],
        expected_account_id: str,
    ) -> None:
        await (await self.resolve()).commit(
            proposal_id,
            expires_at=expires_at,
            proposal_payload=proposal_payload,
            expected_account_id=expected_account_id,
        )

    async def try_reserve_consumption(
        self, proposal_id: str, *, expected_account_id: str
    ) -> ProposalRecord:
        return await (await self.resolve()).try_reserve_consumption(
            proposal_id, expected_account_id=expected_account_id
        )

    async def finalize_consumption(
        self, proposal_id: str, *, media_buy_id: str, expected_account_id: str
    ) -> None:
        await (await self.resolve()).finalize_consumption(
            proposal_id, media_buy_id=media_buy_id, expected_account_id=expected_account_id
        )

    async def release_consumption(self, proposal_id: str, *, expected_account_id: str) -> None:
        await (await self.resolve()).release_consumption(
            proposal_id, expected_account_id=expected_account_id
        )

    async def mark_consumed(
        self, proposal_id: str, *, media_buy_id: str, expected_account_id: str
    ) -> None:
        await (await self.resolve()).mark_consumed(
            proposal_id, media_buy_id=media_buy_id, expected_account_id=expected_account_id
        )

    async def discard(self, proposal_id: str, *, expected_account_id: str) -> None:
        await (await self.resolve()).discard(proposal_id, expected_account_id=expected_account_id)

    async def get_by_media_buy_id(
        self, media_buy_id: str, *, expected_account_id: str
    ) -> ProposalRecord | None:
        return await (await self.resolve()).get_by_media_buy_id(
            media_buy_id, expected_account_id=expected_account_id
        )


class LazyTaskRegistry(_LazyStore[PgTaskRegistry], _TaskLifecycleObservers):
    """Deferred PgTaskRegistry, including its listing and metrics contracts.

    A factory can return a registry, or an original (registry, outbox) pair. Build
    them together using one caller-owned pool. Pair and signing-scope validation
    are retained. Register observers before first use; they receive the first
    submitted transition too. No arbitrary delegate attributes are forwarded.

    For a server advertising SDK task-webhook signing, await resolve() before
    constructing the server: its synchronous boot validator needs the actual
    outbox/sender/horizon configuration. Polling-only servers can resolve on the
    first task operation.
    """

    is_durable: ClassVar[bool] = True

    def __init__(self, factory: LazyTaskRegistryFactory) -> None:
        self._init_lifecycle_observers()

        async def resolve_registry() -> PgTaskRegistry:
            result = factory()
            stores = await result if isinstance(result, Awaitable) else result
            if isinstance(stores, tuple):
                registry, outbox = stores
                if not isinstance(registry, PgTaskRegistry) or not isinstance(
                    outbox, PgTaskWebhookOutbox
                ):
                    raise TypeError("Task factory must return a concrete registry/outbox pair")
                if registry.task_webhook_outbox is not outbox or registry._pool is not outbox._pool:
                    raise ValueError(
                        "Task registry/outbox pair must share one pool and registration"
                    )
                return registry
            return stores

        super().__init__(resolve_registry, PgTaskRegistry)

    def _validate(self, store: PgTaskRegistry) -> None:
        if store.is_durable is not True:
            raise ValueError("LazyTaskRegistry requires a durable PostgreSQL registry")
        if not callable(store.list):
            raise TypeError("LazyTaskRegistry requires PostgreSQL listing support")
        outbox = store.task_webhook_outbox
        if outbox is not None and outbox._pool is not store._pool:
            raise ValueError("Task registry and outbox must share one pool")
        store.add_lifecycle_observer(self._relay_transition)

    def _relay_transition(
        self,
        event: TaskTransition,
        *,
        task_id: str,
        account_id: str,
        task_type: str,
        created_at: float,
        updated_at: float,
    ) -> None:
        self._notify_lifecycle_observers(
            event,
            {
                "task_id": task_id,
                "account_id": account_id,
                "task_type": task_type,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        )

    @property
    def task_webhook_outbox(self) -> PgTaskWebhookOutbox | None:
        """Original coupled outbox, available after resolution; no eager opening."""
        store = self.resolved
        return store.task_webhook_outbox if store is not None else None

    @property
    def atomic_task_webhook_outbox(self) -> bool:
        store = self.resolved
        return store.atomic_task_webhook_outbox if store is not None else False

    async def create_schema(self) -> None:
        await (await self.resolve()).create_schema()

    async def issue(
        self,
        *,
        account_id: str,
        task_type: str,
        request_context: dict[str, Any] | None = None,
        webhook_url: str | None = None,
        webhook_operation_id: str | None = None,
        webhook_token: str | None = None,
        webhook_authentication: TaskWebhookAuthentication | None = None,
        webhook_signing_scope_id: str | None = None,
        **_extra: Any,
    ) -> str:
        return await (await self.resolve()).issue(
            account_id=account_id,
            task_type=task_type,
            request_context=request_context,
            webhook_url=webhook_url,
            webhook_operation_id=webhook_operation_id,
            webhook_token=webhook_token,
            webhook_authentication=webhook_authentication,
            webhook_signing_scope_id=webhook_signing_scope_id,
            **_extra,
        )

    async def resolve_webhook_signing_scope(self, context: RequestContext[Any]) -> str | None:
        return await (await self.resolve()).resolve_webhook_signing_scope(context)

    async def update_progress(self, task_id: str, progress: dict[str, Any]) -> None:
        await (await self.resolve()).update_progress(task_id, progress)

    async def complete(self, task_id: str, result: dict[str, Any]) -> None:
        await (await self.resolve()).complete(task_id, result)

    async def fail(self, task_id: str, error: dict[str, Any]) -> None:
        await (await self.resolve()).fail(task_id, error)

    async def get(
        self, task_id: str, *, expected_account_id: str | None = None
    ) -> dict[str, Any] | None:
        return await (await self.resolve()).get(task_id, expected_account_id=expected_account_id)

    async def list(
        self,
        *,
        account_id: str,
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
        pagination: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await (await self.resolve()).list(
            account_id=account_id, filters=filters, sort=sort, pagination=pagination
        )

    async def discard(self, task_id: str) -> None:
        await (await self.resolve()).discard(task_id)


class LazyTaskWebhookOutbox(_LazyStore[PgTaskWebhookOutbox]):
    """Deferred concrete outbox. Synchronous crypto helpers require resolve().

    Use from_registry() to share a registry factory's original outbox. Pass the
    original objects, constructed inside that factory, to PgTaskRegistry; do not
    substitute this worker facade into the concrete constructor's pool checks.
    """

    delivery_state_is_durable: ClassVar[bool] = True
    supports_atomic_task_outbox: ClassVar[bool] = True

    def __init__(self, factory: LazyTaskWebhookOutboxFactory) -> None:
        super().__init__(factory, PgTaskWebhookOutbox)

    def _validate(self, store: PgTaskWebhookOutbox) -> None:
        if (
            store.delivery_state_is_durable is not True
            or store.supports_atomic_task_outbox is not True
        ):
            raise ValueError("LazyTaskWebhookOutbox requires a durable atomic PostgreSQL outbox")

    @classmethod
    def from_registry(cls, registry: LazyTaskRegistry) -> LazyTaskWebhookOutbox:
        """Create a worker facade resolving the same original registry/outbox pair."""

        async def resolve_outbox() -> PgTaskWebhookOutbox:
            store = await registry.resolve()
            if store.task_webhook_outbox is None:
                raise ValueError("The registry factory did not configure a task webhook outbox")
            return store.task_webhook_outbox

        return cls(resolve_outbox)

    @property
    def delivery_retry_horizon_seconds(self) -> int:
        return self._require_resolved().delivery_retry_horizon_seconds

    @property
    def legacy_hmac_fallback(self) -> bool:
        return self._require_resolved().legacy_hmac_fallback

    async def create_schema(self) -> None:
        await (await self.resolve()).create_schema()

    async def enqueue_terminal(
        self,
        conn: Any,
        *,
        task_id: str,
        account_id: str,
        task_type: str,
        status: str,
        result: dict[str, Any],
        url: str,
        operation_id: str,
        token: str | None,
        authentication: TaskWebhookAuthentication | None = None,
        signing_scope_id: str | None = None,
    ) -> int:
        return await (await self.resolve()).enqueue_terminal(
            conn,
            task_id=task_id,
            account_id=account_id,
            task_type=task_type,
            status=status,
            result=result,
            url=url,
            operation_id=operation_id,
            token=token,
            authentication=authentication,
            signing_scope_id=signing_scope_id,
        )

    def validate_registration(
        self, url: str, authentication: TaskWebhookAuthentication | None = None
    ) -> None:
        (self._require_resolved()).validate_registration(url, authentication)

    def protect_registration(
        self,
        *,
        account_id: str,
        task_id: str,
        task_type: str,
        url: str,
        operation_id: str,
        token: str | None,
        authentication: TaskWebhookAuthentication | None = None,
        signing_scope_id: str | None = None,
    ) -> tuple[bytes, bytes]:
        return (self._require_resolved()).protect_registration(
            account_id=account_id,
            task_id=task_id,
            task_type=task_type,
            url=url,
            operation_id=operation_id,
            token=token,
            authentication=authentication,
            signing_scope_id=signing_scope_id,
        )

    def open_registration(
        self,
        *,
        account_id: str,
        task_id: str,
        task_type: str,
        encrypted_registration: bytes,
        nonce: bytes,
    ) -> tuple[str, str, str | None]:
        return (self._require_resolved()).open_registration(
            account_id=account_id,
            task_id=task_id,
            task_type=task_type,
            encrypted_registration=encrypted_registration,
            nonce=nonce,
        )

    async def run_worker(
        self, *, poll_interval: float = 1.0, purge_interval: float = 300.0
    ) -> None:
        await (await self.resolve()).run_worker(
            poll_interval=poll_interval, purge_interval=purge_interval
        )

    async def process_one(self) -> bool:
        return await (await self.resolve()).process_one()

    async def purge_expired(self) -> None:
        await (await self.resolve()).purge_expired()
