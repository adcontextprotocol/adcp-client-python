"""ProposalStore — per-tenant proposal lifecycle persistence.

The single ledger for proposal recipes across the entire lifecycle:
draft (in-flight refine iterations) → committed (post-finalize, with
``expires_at`` hold window) → consumed (post-``create_media_buy``).

See ``docs/proposals/proposal-manager-v15-design.md`` § D1, D3.

* :class:`ProposalState` — three-state enum.
* :class:`ProposalRecord` — the per-proposal storage row.
* :class:`ProposalStore` — Protocol adopters implement; mirrors
  :class:`MediaBuyStore` (sync OR async per method).
* :class:`InMemoryProposalStore` — non-durable reference impl. Suitable
  for local dev and CI; production wires a durable backing.
* :func:`create_dev_proposal_store` — factory that warns on construction.

The framework drives every state transition. Adopter callbacks return
``MaybeAsync[...]`` — the framework awaits at the call site via
:func:`_await_maybe`, mirroring the
:mod:`adcp.decisioning.media_buy_store` precedent.

Identity dimensions
-------------------

Three distinct identity axes flow through the proposal lifecycle — do not
conflate them:

* **account_id** — the buyer's account (who is buying). Populated from
  ``ctx.account.id`` at dispatch time. The primary ownership key for all
  cross-tenant isolation checks.

* **publisher_id** — the seller tenant that owns this proposal record
  (which publisher's agent is being called). Populated from
  ``ctx.account.metadata["tenant_id"]`` via the dispatch helper
  ``_tenant_id(ctx)``. Only relevant for multi-tenant seller deployments
  where a single process serves several publisher clients. Single-tenant
  deployments leave this ``None``; Protocol semantics are unchanged.

* **metadata["tenant_id"] / router dispatch key** (internal) — the key
  ``PlatformRouter`` uses to select the right per-tenant platform
  instance (store, manager). Same string as ``publisher_id`` in most
  deployments, but kept separate because the router key is a routing
  concern while ``publisher_id`` is a storage scoping concern.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Protocol,
    runtime_checkable,
)

from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from adcp.decisioning.recipe import Recipe
    from adcp.decisioning.types import MaybeAsync


logger = logging.getLogger(__name__)


# Python 3.10 floor — StrEnum landed at 3.11. Use Enum + str mixin
# so ``ProposalState.DRAFT == "draft"`` holds across supported versions.
class ProposalState(str, Enum):
    """Lifecycle states for a stored proposal.

    No ``EXPIRED`` member: the framework computes expiry from
    :attr:`ProposalRecord.expires_at` + the current clock + the
    adopter's grace window (see proposal_lifecycle.py D7). Storing
    expiry as a state would create a clock-driven write the framework
    doesn't actually need.
    """

    DRAFT = "draft"  # mutable; refine iterations overwrite
    COMMITTED = "committed"  # immutable + expires_at enforcement
    CONSUMING = "consuming"  # adapter dispatch in flight; reservation held
    CONSUMED = "consumed"  # post-create_media_buy terminal


@dataclass(frozen=True)
class ProposalRecord:
    """The framework's per-proposal storage row.

    :param proposal_id: Stable identifier the buyer receives in the
        ``proposals[]`` wire array.
    :param account_id: Account that owns the proposal. Drives the
        cross-tenant check in :meth:`ProposalStore.get`.
    :param state: Current lifecycle state.
    :param recipes: ``product_id -> Recipe`` mapping. The
        :class:`ProposalManager` returned these alongside products
        on get_products / refine_products; the framework persists
        them so :meth:`DecisioningPlatform.create_media_buy` can
        hydrate ``ctx.recipes`` from this same record.
    :param proposal_payload: The wire ``Proposal`` shape. Stored so
        the framework can re-emit it on refine iterations or replay
        it post-finalize without round-tripping through the manager
        again.
    :param expires_at: Set on :meth:`commit`. The inventory hold
        window; framework rejects ``create_media_buy`` calls past
        this deadline (plus the adopter's grace window).
    :param media_buy_id: Set on :meth:`mark_consumed`. The accepted
        proposal's terminal binding to a media buy; reverse-index
        lookups via :meth:`get_by_media_buy_id` use this.
    :param recipe_schema_version: Captured at :meth:`put_draft` time.
        Adopters whose Recipe subclasses add required fields later
        bump the schema and write a migration (or evict pre-bump
        records). Framework reads but does not enforce.
    """

    proposal_id: str
    account_id: str
    state: ProposalState
    recipes: Mapping[str, Recipe]
    proposal_payload: Mapping[str, Any]
    # Seller-tenant scope for multi-tenant deployments. None for
    # single-tenant adopters; see module docstring for the three-way
    # identity distinction.
    publisher_id: str | None = None
    expires_at: datetime | None = None
    media_buy_id: str | None = None
    recipe_schema_version: int = 1


@runtime_checkable
class ProposalStore(Protocol):
    """Per-tenant proposal lifecycle persistence.

    Methods may be sync or async — the framework awaits at call time
    via :func:`_await_maybe` (mirrors
    :class:`adcp.decisioning.MediaBuyStore`).

    State machine the framework drives:

    .. code-block:: text

                                  ┌──── release_consumption ────┐
                                  ▼                             │
        put_draft ─► DRAFT ─► commit ─► COMMITTED ─► try_reserve_consumption ─► CONSUMING
                       ▲                                                          │
                       │                                                          │
                    (refine                                              finalize_consumption
                     iteration)                                                   │
                       │                                                          ▼
                       └─ put_draft (overwrite while DRAFT) ─┘                CONSUMED
                                                                              (terminal)

    The ``COMMITTED → CONSUMING → CONSUMED`` two-phase transition
    prevents the inventory double-spend race that a check-then-act
    sequence on ``COMMITTED`` would expose. Two parallel
    ``create_media_buy(proposal_id=X)`` calls cannot both reserve
    the proposal — the second :meth:`try_reserve_consumption` raises
    ``PROPOSAL_NOT_COMMITTED`` once the first transitioned the record.
    Adapter dispatch runs against the reservation; on success the
    framework calls :meth:`finalize_consumption` (records the
    ``media_buy_id``); on failure the framework calls
    :meth:`release_consumption` (rolls back to ``COMMITTED`` so the
    buyer can retry).

    Transitions outside this graph (commit-from-COMMITTED,
    finalize_consumption-from-DRAFT, etc.) raise :class:`AdcpError`
    with ``code='INTERNAL_ERROR'`` — those are framework / adopter
    bugs, not buyer-facing rejections.
    """

    is_durable: ClassVar[bool]
    """Drives the production-mode gate. ``False`` for
    :class:`InMemoryProposalStore`; ``True`` for adopter-supplied
    durable backings (Postgres / Redis / SQLAlchemy)."""

    def put_draft(
        self,
        *,
        proposal_id: str,
        account_id: str,
        publisher_id: str | None = None,
        recipes: Mapping[str, Recipe],
        proposal_payload: Mapping[str, Any],
    ) -> MaybeAsync[None]:
        """Store / replace a draft proposal.

        Refine iterations call this with the same ``proposal_id`` to
        overwrite. Calling :meth:`put_draft` on a record currently in
        :attr:`ProposalState.COMMITTED` or :attr:`ProposalState.CONSUMED`
        is rejected.

        :param publisher_id: Optional seller-tenant scope. Pass
            ``_tenant_id(ctx)`` at dispatch time. Single-tenant adopters
            leave this ``None``; the store treats it as "no publisher
            scope" and all Protocol semantics are unchanged.
        """
        ...

    def get(
        self,
        proposal_id: str,
        *,
        expected_account_id: str | None = None,
        expected_publisher_id: str | None = None,
    ) -> MaybeAsync[ProposalRecord | None]:
        """Look up a proposal record. Cross-tenant probes return ``None``.

        Mirrors :meth:`adcp.decisioning.TaskRegistry.get`'s posture:
        when ``expected_account_id`` is supplied, a mismatch returns
        ``None`` rather than the raw record. The dispatch path always
        passes the authenticated principal's account_id; adopter
        impls MUST honor this — returning a cross-tenant record
        enables principal-enumeration via proposal_id probing.

        :param expected_publisher_id: When supplied, the lookup is
            additionally scoped to the given publisher tenant. A
            mismatch returns ``None`` (same principal-enumeration
            defence as ``expected_account_id``). ``None`` means "no
            publisher filter" — all publishers are in scope.
        """
        ...

    def commit(
        self,
        proposal_id: str,
        *,
        expires_at: datetime,
        proposal_payload: Mapping[str, Any],
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> MaybeAsync[None]:
        """Promote ``DRAFT`` → ``COMMITTED``.

        Idempotent on re-call with equal ``expires_at`` +
        ``proposal_payload``. A second commit with different values
        raises ``INTERNAL_ERROR`` — adopter bug.

        ``expected_account_id`` scopes the write to the calling
        principal's tenant. Durable backings whose primary key is
        ``(account_id, proposal_id)`` MUST use this in the SQL
        predicate — a write keyed only on ``proposal_id`` either
        misses (silently no-ops) or hits the wrong tenant's row.
        Required as of v1.5.1 (#727); the previous unscoped signature
        was a cross-tenant write surface.
        """
        ...

    def try_reserve_consumption(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> MaybeAsync[ProposalRecord]:
        """Atomic CAS: ``COMMITTED`` → ``CONSUMING``.

        The framework calls this **before** dispatching
        :meth:`DecisioningPlatform.create_media_buy`. Holds the
        reservation until :meth:`finalize_consumption` (success) or
        :meth:`release_consumption` (rollback). Two parallel callers
        cannot both reserve — the loser raises ``PROPOSAL_NOT_COMMITTED``.

        :raises AdcpError: ``PROPOSAL_NOT_FOUND`` when no record exists,
            ``PROPOSAL_NOT_COMMITTED`` when state is not ``COMMITTED``
            (already CONSUMING / CONSUMED / DRAFT). Adopters backed by
            SQL implement this with ``SELECT … FOR UPDATE`` or an
            equivalent atomic CAS — the contract is that two
            concurrent calls produce exactly one success.

        :returns: The record on success, with ``state == CONSUMING``.
        """
        ...

    def finalize_consumption(
        self,
        proposal_id: str,
        *,
        media_buy_id: str,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> MaybeAsync[None]:
        """Promote ``CONSUMING`` → ``CONSUMED`` and record the
        ``media_buy_id`` back-reference for
        :meth:`get_by_media_buy_id` reverse-index lookups.

        :raises AdcpError: ``INTERNAL_ERROR`` if the record is not in
            ``CONSUMING`` (framework called this without a prior
            successful :meth:`try_reserve_consumption`).
        """
        ...

    def release_consumption(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> MaybeAsync[None]:
        """Rollback path: ``CONSUMING`` → ``COMMITTED``.

        Called by the framework when the adapter's
        :meth:`create_media_buy` raises (transient upstream error,
        validation, etc.) so the buyer can retry without
        ``PROPOSAL_NOT_COMMITTED``. Idempotent on a record already in
        ``COMMITTED`` (in case the adapter raised after a successful
        :meth:`finalize_consumption` — rare but harmless).
        """
        ...

    def mark_consumed(
        self,
        proposal_id: str,
        *,
        media_buy_id: str,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> MaybeAsync[None]:
        """Direct ``COMMITTED`` → ``CONSUMED`` transition.

        New code uses :meth:`try_reserve_consumption` +
        :meth:`finalize_consumption` for the race-safe two-phase
        commit. This method is equivalent to a reserve-and-finalize
        against a single thread of writes; adopters MUST NOT call it
        from concurrent dispatch paths.

        ``expected_account_id`` scopes the transition to the calling
        principal's tenant — same rationale as :meth:`commit`.
        """
        ...

    def discard(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> MaybeAsync[None]:
        """Rollback path. Idempotent — discarding an unknown id (or
        a cross-tenant probe) is a no-op (no raise). Symmetric with
        :meth:`adcp.decisioning.TaskRegistry.discard`.

        ``expected_account_id`` scopes the delete to the calling
        principal's tenant; a cross-tenant probe must not delete the
        other tenant's row.
        """
        ...

    def get_by_media_buy_id(
        self,
        media_buy_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> MaybeAsync[ProposalRecord | None]:
        """Reverse-index lookup. Hydrate the (consumed) proposal that
        produced this ``media_buy_id`` for the given tenant.

        ``expected_account_id`` is required (no default) because
        ``media_buy_id`` is adopter-controlled and can collide across
        tenants — sequential IDs, deterministic test fixtures, etc.
        Indexing on the tenant-scoped tuple is the only safe shape.
        Adopters backed by SQL add a uniqueness constraint on
        ``(account_id, media_buy_id)`` where ``media_buy_id IS NOT NULL``.

        Returns ``None`` for legacy buys / non-proposal flows that
        never went through the proposal lifecycle.
        """
        ...


async def _await_maybe(value: Any) -> Any:
    """Resolve a value that may be a coroutine OR a plain return.

    Mirrors :func:`adcp.decisioning.media_buy_store._await_maybe` —
    don't roll a new bridge for the same pattern.
    """
    if inspect.isawaitable(value):
        return await value
    return value


# ---------------------------------------------------------------------------
# In-memory reference implementation
# ---------------------------------------------------------------------------


# Default eviction windows for the in-memory ref. Production backings
# scope retention to their compliance / audit posture; the in-memory
# ref's defaults match the design § D1 storyboard guidance.
_DEFAULT_DRAFT_TTL = timedelta(hours=24)
_DEFAULT_COMMITTED_GRACE = timedelta(days=7)


class InMemoryProposalStore:
    """Process-local :class:`ProposalStore` reference implementation.

    Storage is a plain ``dict[str, ProposalRecord]`` guarded by an
    :class:`asyncio.Lock`. Adequate for local dev, CI, and tests;
    production deployments wire a durable backing implementing the
    same Protocol.

    **Eviction:**

    * Drafts older than ``draft_ttl`` (default 24h) are evicted on
      every read / write.
    * Committed proposals more than ``committed_grace`` past
      ``expires_at`` (default 7 days) are evicted.

    Eviction runs lazily — no background timer thread. The first
    operation after the eviction window passes triggers cleanup.

    **Cross-tenant safety:** :meth:`get` and
    :meth:`get_by_media_buy_id` honor ``expected_account_id`` —
    cross-tenant probes return ``None``, not the raw record.
    """

    is_durable: ClassVar[bool] = False

    def __init__(
        self,
        *,
        draft_ttl: timedelta = _DEFAULT_DRAFT_TTL,
        committed_grace: timedelta = _DEFAULT_COMMITTED_GRACE,
        clock: Any = None,
    ) -> None:
        """
        :param draft_ttl: How long a draft proposal lives without a
            commit before being evicted. Default 24h.
        :param committed_grace: How long a committed (or consumed)
            proposal lives past its ``expires_at`` before eviction.
            Default 7 days.
        :param clock: Test injectable; defaults to
            ``lambda: datetime.now(timezone.utc)``. Tests pin a
            deterministic clock to validate eviction.
        """
        self._records: dict[str, ProposalRecord] = {}
        # Reverse index keyed by (publisher_id or '', account_id, media_buy_id).
        # The publisher_id dimension prevents collisions for multi-tenant
        # deployments; '' stands in for None (single-tenant/no publisher scope).
        self._media_buy_index: dict[tuple[str, str, str], str] = {}
        self._lock = asyncio.Lock()
        self._draft_ttl = draft_ttl
        self._committed_grace = committed_grace
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._creation_times: dict[str, datetime] = {}

    def _evict_expired_locked(self) -> None:
        """Remove records past their TTL. Must be called under the lock."""
        now = self._clock()
        to_remove: list[str] = []
        for proposal_id, record in self._records.items():
            created = self._creation_times.get(proposal_id, now)
            if record.state == ProposalState.DRAFT:
                if now - created > self._draft_ttl:
                    to_remove.append(proposal_id)
            elif record.expires_at is not None:
                deadline = record.expires_at + self._committed_grace
                if now > deadline:
                    to_remove.append(proposal_id)
        for proposal_id in to_remove:
            removed = self._records.pop(proposal_id, None)
            self._creation_times.pop(proposal_id, None)
            if removed is not None and removed.media_buy_id is not None:
                self._media_buy_index.pop(
                    (removed.publisher_id or "", removed.account_id, removed.media_buy_id), None
                )

    async def put_draft(
        self,
        *,
        proposal_id: str,
        account_id: str,
        publisher_id: str | None = None,
        recipes: Mapping[str, Recipe],
        proposal_payload: Mapping[str, Any],
    ) -> None:
        async with self._lock:
            self._evict_expired_locked()
            existing = self._records.get(proposal_id)
            if existing is not None and existing.state != ProposalState.DRAFT:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Cannot put_draft on proposal {proposal_id!r} in "
                        f"state {existing.state.value!r}; refine iterations "
                        "are only valid on draft proposals. Once committed "
                        "or consumed, a proposal_id is immutable."
                    ),
                    recovery="terminal",
                )
            record = ProposalRecord(
                proposal_id=proposal_id,
                account_id=account_id,
                publisher_id=publisher_id,
                state=ProposalState.DRAFT,
                recipes=dict(recipes),
                proposal_payload=dict(proposal_payload),
            )
            self._records[proposal_id] = record
            # Track creation time only for fresh records — refine
            # iterations preserve the original creation time so the
            # 24h draft TTL is anchored to the start of the buyer's
            # session, not the most recent iteration.
            if proposal_id not in self._creation_times:
                self._creation_times[proposal_id] = self._clock()

    async def get(
        self,
        proposal_id: str,
        *,
        expected_account_id: str | None = None,
        expected_publisher_id: str | None = None,
    ) -> ProposalRecord | None:
        async with self._lock:
            self._evict_expired_locked()
            record = self._records.get(proposal_id)
            if record is None:
                return None
            if expected_account_id is not None and record.account_id != expected_account_id:
                # Cross-tenant probe — return None, not raw record.
                return None
            if expected_publisher_id is not None and record.publisher_id != expected_publisher_id:
                return None
            return record

    async def commit(
        self,
        proposal_id: str,
        *,
        expires_at: datetime,
        proposal_payload: Mapping[str, Any],
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        async with self._lock:
            self._evict_expired_locked()
            record = self._records.get(proposal_id)
            if record is None or record.account_id != expected_account_id or (
                expected_publisher_id is not None
                and record.publisher_id != expected_publisher_id
            ):
                # Cross-tenant probe collapses to "not in store" — same
                # principal-enumeration defence as :meth:`get`.
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Cannot commit proposal {proposal_id!r}: not in "
                        "store for the expected tenant. The framework's "
                        "finalize dispatch must put_draft before commit."
                    ),
                    recovery="terminal",
                )
            payload_dict = dict(proposal_payload)
            if record.state == ProposalState.COMMITTED:
                # Idempotent only when the second commit matches the first.
                same_deadline = record.expires_at == expires_at
                same_payload = dict(record.proposal_payload) == payload_dict
                if same_deadline and same_payload:
                    return
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Proposal {proposal_id!r} already committed with a "
                        "different expires_at or payload — re-commit with "
                        "different values is a developer bug."
                    ),
                    recovery="terminal",
                )
            if record.state != ProposalState.DRAFT:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Cannot commit proposal {proposal_id!r} from state "
                        f"{record.state.value!r}; commit requires DRAFT."
                    ),
                    recovery="terminal",
                )
            self._records[proposal_id] = replace(
                record,
                state=ProposalState.COMMITTED,
                expires_at=expires_at,
                proposal_payload=payload_dict,
            )

    async def try_reserve_consumption(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> ProposalRecord:
        async with self._lock:
            self._evict_expired_locked()
            record = self._records.get(proposal_id)
            # Cross-tenant probe collapses to PROPOSAL_NOT_FOUND — same
            # principal-enumeration defense as :meth:`get`.
            if record is None or record.account_id != expected_account_id or (
                expected_publisher_id is not None
                and record.publisher_id != expected_publisher_id
            ):
                raise AdcpError(
                    "PROPOSAL_NOT_FOUND",
                    message=(f"Proposal {proposal_id!r} not found."),
                    recovery="terminal",
                    field="proposal_id",
                )
            if record.state != ProposalState.COMMITTED:
                raise AdcpError(
                    "PROPOSAL_NOT_COMMITTED",
                    message=(
                        f"Proposal {proposal_id!r} is in state "
                        f"{record.state.value!r}; create_media_buy "
                        "requires a committed proposal that hasn't "
                        "been accepted or reserved by another request."
                    ),
                    recovery="correctable",
                    field="proposal_id",
                )
            reserved = replace(record, state=ProposalState.CONSUMING)
            self._records[proposal_id] = reserved
            return reserved

    async def finalize_consumption(
        self,
        proposal_id: str,
        *,
        media_buy_id: str,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        async with self._lock:
            record = self._records.get(proposal_id)
            if record is None or record.account_id != expected_account_id or (
                expected_publisher_id is not None
                and record.publisher_id != expected_publisher_id
            ):
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"finalize_consumption: proposal {proposal_id!r} "
                        "not found for the expected tenant."
                    ),
                    recovery="terminal",
                )
            # Idempotent on already-CONSUMED with the same media_buy_id.
            if record.state == ProposalState.CONSUMED:
                if record.media_buy_id == media_buy_id:
                    return
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Proposal {proposal_id!r} already consumed by "
                        f"media_buy_id={record.media_buy_id!r}; cannot "
                        f"re-consume as {media_buy_id!r}."
                    ),
                    recovery="terminal",
                )
            if record.state != ProposalState.CONSUMING:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"finalize_consumption requires CONSUMING; "
                        f"proposal {proposal_id!r} is in "
                        f"{record.state.value!r}. Framework must call "
                        "try_reserve_consumption first."
                    ),
                    recovery="terminal",
                )
            self._records[proposal_id] = replace(
                record,
                state=ProposalState.CONSUMED,
                media_buy_id=media_buy_id,
            )
            self._media_buy_index[
                (record.publisher_id or "", record.account_id, media_buy_id)
            ] = proposal_id

    async def release_consumption(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        async with self._lock:
            record = self._records.get(proposal_id)
            if record is None or record.account_id != expected_account_id or (
                expected_publisher_id is not None
                and record.publisher_id != expected_publisher_id
            ):
                # Idempotent — releasing an unknown id is a no-op so the
                # adapter-failure rollback path can be unconditional.
                return
            if record.state == ProposalState.COMMITTED:
                # Already rolled back (e.g., another rollback path ran).
                return
            if record.state != ProposalState.CONSUMING:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"release_consumption requires CONSUMING; "
                        f"proposal {proposal_id!r} is in "
                        f"{record.state.value!r}."
                    ),
                    recovery="terminal",
                )
            self._records[proposal_id] = replace(
                record,
                state=ProposalState.COMMITTED,
            )

    async def mark_consumed(
        self,
        proposal_id: str,
        *,
        media_buy_id: str,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        # Equivalent to try_reserve_consumption + finalize_consumption
        # against a single-threaded write. New dispatch code uses the
        # two-phase methods directly.
        async with self._lock:
            self._evict_expired_locked()
            record = self._records.get(proposal_id)
            if record is None or record.account_id != expected_account_id or (
                expected_publisher_id is not None
                and record.publisher_id != expected_publisher_id
            ):
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Cannot mark_consumed proposal {proposal_id!r}: "
                        "not in store for the expected tenant."
                    ),
                    recovery="terminal",
                )
            if record.state == ProposalState.CONSUMED:
                # Idempotent only when re-marking with the same media_buy_id.
                if record.media_buy_id == media_buy_id:
                    return
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Proposal {proposal_id!r} already consumed by "
                        f"media_buy_id={record.media_buy_id!r}; cannot "
                        f"re-consume as {media_buy_id!r}."
                    ),
                    recovery="terminal",
                )
            if record.state != ProposalState.COMMITTED:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Cannot mark_consumed proposal {proposal_id!r} "
                        f"from state {record.state.value!r}; mark_consumed "
                        "requires COMMITTED."
                    ),
                    recovery="terminal",
                )
            self._records[proposal_id] = replace(
                record,
                state=ProposalState.CONSUMED,
                media_buy_id=media_buy_id,
            )
            self._media_buy_index[
                (record.publisher_id or "", record.account_id, media_buy_id)
            ] = proposal_id

    async def discard(
        self,
        proposal_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> None:
        async with self._lock:
            record = self._records.get(proposal_id)
            if record is None or record.account_id != expected_account_id or (
                expected_publisher_id is not None
                and record.publisher_id != expected_publisher_id
            ):
                # Idempotent — unknown id or cross-tenant probe is a no-op.
                return
            self._records.pop(proposal_id, None)
            self._creation_times.pop(proposal_id, None)
            if record.media_buy_id is not None:
                self._media_buy_index.pop(
                    (record.publisher_id or "", record.account_id, record.media_buy_id), None
                )

    async def get_by_media_buy_id(
        self,
        media_buy_id: str,
        *,
        expected_account_id: str,
        expected_publisher_id: str | None = None,
    ) -> ProposalRecord | None:
        async with self._lock:
            self._evict_expired_locked()
            if expected_publisher_id is not None:
                key = (expected_publisher_id, expected_account_id, media_buy_id)
                proposal_id = self._media_buy_index.get(key)
                if proposal_id is None:
                    return None
                record = self._records.get(proposal_id)
                if record is None:
                    self._media_buy_index.pop(key, None)
                    return None
                return record
            # No publisher filter — scan for any matching (*, expected_account_id, media_buy_id).
            for (pub, acct, mbid), pid in list(self._media_buy_index.items()):
                if acct == expected_account_id and mbid == media_buy_id:
                    record = self._records.get(pid)
                    if record is None:
                        self._media_buy_index.pop((pub, acct, mbid), None)
                        continue
                    return record
            return None


def create_dev_proposal_store(
    *,
    draft_ttl: timedelta = _DEFAULT_DRAFT_TTL,
    committed_grace: timedelta = _DEFAULT_COMMITTED_GRACE,
) -> InMemoryProposalStore:
    """Build an :class:`InMemoryProposalStore` with a dev-mode warning.

    Adopters bringing up a storyboard locally use this factory so the
    wiring reads as a deliberate dev-mode choice. Production
    deployments wire a durable backing — the warning surfaces at every
    construction site so silent-prod-on-in-memory is one log search
    away from being caught.

    See ``docs/proposals/proposal-manager-v15-design.md`` § D1.
    """
    warnings.warn(
        "create_dev_proposal_store() returns an in-memory store; "
        "do NOT use in production deployments. Multi-worker (gunicorn / "
        "uvicorn workers / k8s replicas) deployments lose every "
        "in-flight proposal at the first worker that didn't see "
        "put_draft. Wire a durable ProposalStore (Postgres / Redis / "
        "SQLAlchemy) for production.",
        UserWarning,
        stacklevel=2,
    )
    return InMemoryProposalStore(
        draft_ttl=draft_ttl,
        committed_grace=committed_grace,
    )


__all__ = [
    "InMemoryProposalStore",
    "ProposalRecord",
    "ProposalState",
    "ProposalStore",
    "create_dev_proposal_store",
]
