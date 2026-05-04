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

        put_draft  ──►  DRAFT  ──►  commit  ──►  COMMITTED  ──►  mark_consumed  ──►  CONSUMED
                          ▲                                                              │
                          │                                                              │
                       (refine                                                       (terminal)
                        iteration)
                          │
                          └───────  put_draft (overwrite while DRAFT) ─┘

    Transitions outside this graph (commit-from-COMMITTED,
    mark_consumed-from-DRAFT, etc.) raise :class:`AdcpError` with
    ``code='INTERNAL_ERROR'`` — those are framework / adopter bugs,
    not buyer-facing rejections.
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
        recipes: Mapping[str, Recipe],
        proposal_payload: Mapping[str, Any],
    ) -> MaybeAsync[None]:
        """Store / replace a draft proposal.

        Refine iterations call this with the same ``proposal_id`` to
        overwrite. Calling :meth:`put_draft` on a record currently in
        :attr:`ProposalState.COMMITTED` or :attr:`ProposalState.CONSUMED`
        is rejected.
        """
        ...

    def get(
        self,
        proposal_id: str,
        *,
        expected_account_id: str | None = None,
    ) -> MaybeAsync[ProposalRecord | None]:
        """Look up a proposal record. Cross-tenant probes return ``None``.

        Mirrors :meth:`adcp.decisioning.TaskRegistry.get`'s posture:
        when ``expected_account_id`` is supplied, a mismatch returns
        ``None`` rather than the raw record. The dispatch path always
        passes the authenticated principal's account_id; adopter
        impls MUST honor this — returning a cross-tenant record
        enables principal-enumeration via proposal_id probing.
        """
        ...

    def commit(
        self,
        proposal_id: str,
        *,
        expires_at: datetime,
        proposal_payload: Mapping[str, Any],
    ) -> MaybeAsync[None]:
        """Promote ``DRAFT`` → ``COMMITTED``.

        Idempotent on re-call with equal ``expires_at`` +
        ``proposal_payload``. A second commit with different values
        raises ``INTERNAL_ERROR`` — adopter bug.
        """
        ...

    def mark_consumed(
        self,
        proposal_id: str,
        *,
        media_buy_id: str,
    ) -> MaybeAsync[None]:
        """Promote ``COMMITTED`` → ``CONSUMED`` and record the
        ``media_buy_id`` back-reference for
        :meth:`get_by_media_buy_id` reverse-index lookups."""
        ...

    def discard(self, proposal_id: str) -> MaybeAsync[None]:
        """Rollback path. Idempotent — discarding an unknown id is a
        no-op (no raise). Symmetric with
        :meth:`adcp.decisioning.TaskRegistry.discard`."""
        ...

    def get_by_media_buy_id(
        self,
        media_buy_id: str,
        *,
        expected_account_id: str | None = None,
    ) -> MaybeAsync[ProposalRecord | None]:
        """Reverse-index lookup. Hydrate the (consumed) proposal that
        produced this ``media_buy_id``.

        Returns ``None`` for legacy buys / non-proposal flows that
        never went through the proposal lifecycle. Adopters backed by
        SQL add a uniqueness constraint on
        ``(account_id, media_buy_id)`` where ``media_buy_id IS NOT NULL``.
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
        """Create an in-memory ProposalStore.

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
        self._media_buy_index: dict[str, str] = {}  # media_buy_id -> proposal_id
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
                self._media_buy_index.pop(removed.media_buy_id, None)

    async def put_draft(
        self,
        *,
        proposal_id: str,
        account_id: str,
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
    ) -> ProposalRecord | None:
        async with self._lock:
            self._evict_expired_locked()
            record = self._records.get(proposal_id)
            if record is None:
                return None
            if expected_account_id is not None and record.account_id != expected_account_id:
                # Cross-tenant probe — return None, not raw record.
                return None
            return record

    async def commit(
        self,
        proposal_id: str,
        *,
        expires_at: datetime,
        proposal_payload: Mapping[str, Any],
    ) -> None:
        async with self._lock:
            self._evict_expired_locked()
            record = self._records.get(proposal_id)
            if record is None:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"Cannot commit proposal {proposal_id!r}: not in "
                        "store. The framework's finalize dispatch must "
                        "put_draft before commit."
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

    async def mark_consumed(
        self,
        proposal_id: str,
        *,
        media_buy_id: str,
    ) -> None:
        async with self._lock:
            self._evict_expired_locked()
            record = self._records.get(proposal_id)
            if record is None:
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(f"Cannot mark_consumed proposal {proposal_id!r}: " "not in store."),
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
            self._media_buy_index[media_buy_id] = proposal_id

    async def discard(self, proposal_id: str) -> None:
        async with self._lock:
            record = self._records.pop(proposal_id, None)
            self._creation_times.pop(proposal_id, None)
            if record is not None and record.media_buy_id is not None:
                self._media_buy_index.pop(record.media_buy_id, None)

    async def get_by_media_buy_id(
        self,
        media_buy_id: str,
        *,
        expected_account_id: str | None = None,
    ) -> ProposalRecord | None:
        async with self._lock:
            self._evict_expired_locked()
            proposal_id = self._media_buy_index.get(media_buy_id)
            if proposal_id is None:
                return None
            record = self._records.get(proposal_id)
            if record is None:
                # Index drift — clean up.
                self._media_buy_index.pop(media_buy_id, None)
                return None
            if expected_account_id is not None and record.account_id != expected_account_id:
                return None
            return record


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
