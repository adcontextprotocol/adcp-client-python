"""The durable seam under the reporting producer and the status handler.

:class:`ReportingLedgerStore` is deliberately small.  Everything interesting --
health, coverage roll-up, issue identity, period derivation -- is computed from
what the store returns, not stored by it.  A store's whole job is: keep
immutable records, keep them ordered, hand back a consistent snapshot, and
refuse to break the invariants that make the records evidence.

The invariants a conforming store MUST enforce, because they cannot be checked
after the fact:

* **Obligation identity is unique per (config generation, period).**  Two
  obligations for one logical period would let a seller quietly publish twice
  and pick a winner.
* **Revisions are immutable.**  Re-committing an existing
  ``reporting_revision_id`` with different content is an idempotency conflict,
  not an update.
* **At most one official revision per obligation.**  It is terminal; a later
  restatement is an adjustment.
* **Supersession is atomic and exact.**  A snapshot restatement names the
  current leaf; naming a stale leaf must fail rather than fork the chain.
* **The change feed is gapless per account.**  Every immutable write appends in
  the same transaction as the record it describes.

:class:`InMemoryReportingLedgerStore` is the reference implementation and the
conformance target.  It is genuinely usable in tests and single-process pilots,
and it is not durable -- Reliable Reporting's whole premise is retained
evidence, so production wants
:class:`~adcp.reporting.ledger.pg.PgReportingLedgerStore` or your own.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.currency import (
    monetary_decimal,
    require_frozen_currency,
    validate_currency_units,
    validate_monetary_content,
)
from adcp.reporting.evidence import ReportingControlTotalRecord
from adcp.reporting.ledger.health import issue_id_for_occurrence
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    LedgerRecordKind,
    LedgerSnapshot,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingIssueLifecycle,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.notification_models import (
    DirtyReason,
    ReportingDomainEvent,
    ReportingNotificationError,
    ReportingStatusEvidence,
    ReportingStatusScope,
    configuration_evidence,
    issue_evidence,
    validate_scope_refinement,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.status_projection import ReportingStatusSnapshot
    from adcp.reporting.outbox.memory import NotificationState

_MEMORY_TRANSACTION: ContextVar[tuple[int, object] | None] = ContextVar(
    "reporting_memory_transaction", default=None
)

__all__ = [
    "InMemoryReportingLedgerStore",
    "LeasedConfiguration",
    "LedgerConflictError",
    "LedgerPage",
    "ReportingLedgerStore",
    "RestatementCheckpoint",
    "RestatementCheckpointStore",
    "ReportingRowPage",
    "decode_cursor",
    "check_issue_state_transition",
    "encode_cursor",
    "issue_is_retirable",
    "reject_reserved_authoritative_party",
]


class LedgerConflictError(RuntimeError):
    """A write would violate an invariant that makes the ledger evidence.

    Carries a stable ``code`` so a handler can map it to a wire error without
    matching on prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LeasedConfiguration:
    """One configuration generation leased for period-close work.

    Carries its account so the producer never has to invert the lease back into
    a tenant -- an inversion that is easy to get subtly wrong when two accounts
    reuse a ``delivery_config_id``.
    """

    account_id: str
    delivery_config_id: str
    delivery_config_version: int
    lease_expires_at: datetime

    @property
    def generation_key(self) -> ReportingConfigurationGenerationKey:
        return ReportingConfigurationGenerationKey(
            account_id=self.account_id,
            delivery_config_id=self.delivery_config_id,
            delivery_config_version=self.delivery_config_version,
        )


@dataclass(frozen=True)
class RestatementCheckpoint:
    """Durable scheduling state for successful source observations.

    ``next_observation`` advances even when the source content is unchanged.
    Without that durable ordinal the next scheduled refresh would replay the
    same sealed source execution forever instead of making a new observation.
    """

    account_id: str
    reporting_obligation_id: str
    checked_at: datetime
    next_observation: int
    provisional_until: datetime | None = None

    def __post_init__(self) -> None:
        if self.next_observation < 1:
            raise ValueError("next_observation must be at least one")
        _utc(self.checked_at)
        if self.provisional_until is not None:
            _utc(self.provisional_until)


@runtime_checkable
class RestatementCheckpointStore(Protocol):
    """Optional store extension used only by source settling policies."""

    async def get_restatement_checkpoint(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> RestatementCheckpoint | None: ...

    async def record_restatement_checkpoint(
        self, checkpoint: RestatementCheckpoint
    ) -> RestatementCheckpoint:
        """Persist the next source observation ordinal after a successful read."""
        ...


@dataclass(frozen=True)
class LedgerPage:
    """One page of the flat union of ledger records.

    Pagination is over the *flat union* of obligations, revisions, adjustments
    and consumer statuses rather than nesting history under each obligation.
    Nesting looks friendlier and is unbounded: one obligation with ten thousand
    snapshot restatements would make a single "page" unpageable.
    """

    obligations: tuple[ReportingObligationRecord, ...]
    revisions: tuple[ReportingRevisionRecord, ...]
    adjustments: tuple[ReportingAdjustmentRecord, ...]
    consumer_statuses: tuple[ConsumerStatusRecord, ...]
    total_count: int
    has_more: bool
    cursor: str | None


@dataclass(frozen=True)
class ReportingRowPage:
    """One page of an exact revision read.

    Every page repeats identical revision metadata and binding; a consumer
    hashes the concatenated rows in cursor order only after exhausting the
    frozen walk.  Do not substitute a fresh date-range pull for this read -- it
    may have changed since the immutable revision was published.
    """

    rows: tuple[dict[str, Any], ...]
    total_count: int
    has_more: bool
    cursor: str | None


def encode_cursor(payload: dict[str, Any]) -> str:
    """Opaque, self-describing cursor.

    Base64url over canonical JSON: opaque to the consumer, cheap for the seller
    to validate, and stable enough that the same logical position encodes
    identically on every page.  It is *not* signed -- the store re-validates
    caller, account, and snapshot on use, which is the check that actually
    matters.
    """
    return base64.urlsafe_b64encode(canonical_json_utf8_v1(payload)).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as error:
        raise LedgerConflictError(
            "INVALID_CURSOR", "the pagination cursor is not readable"
        ) from error
    if not isinstance(payload, dict):
        raise LedgerConflictError("INVALID_CURSOR", "the pagination cursor is not readable")
    return payload


@runtime_checkable
class ReportingLedgerStore(Protocol):
    """Durable home for obligations, revisions, adjustments, and statuses."""

    async def create_schema(self) -> None:
        """Idempotently create or upgrade this store's schema. Safe on every boot."""
        ...

    # -- configurations --------------------------------------------------

    async def put_configuration(self, configuration: ReportingConfiguration) -> None:
        """Record an accepted configuration generation.

        A generation is immutable: re-putting one with changed content is a
        conflict, because a buyer that retained it derives expectations from it.
        """
        ...

    async def list_configurations(
        self, *, account_id: str, delivery_config_ids: Sequence[str] | None = None
    ) -> tuple[ReportingConfiguration, ...]: ...

    # -- obligations -----------------------------------------------------

    async def commit_obligation(
        self, obligation: ReportingObligationRecord
    ) -> ReportingObligationRecord:
        """Commit an obligation, or return the existing one for its period.

        Idempotent by ``(account, config generation, period)``, not by id: two
        workers racing a period close must converge on one obligation.
        New records require an explicit, validated currency. A legacy record
        with unknown currency can be read/replayed but cannot be filled here.
        """
        ...

    async def get_obligation(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> ReportingObligationRecord | None: ...

    async def find_obligation(
        self,
        *,
        account_id: str,
        delivery_config_id: str,
        delivery_config_version: int,
        period_start: datetime,
        period_end: datetime,
    ) -> ReportingObligationRecord | None: ...

    # -- revisions -------------------------------------------------------

    async def commit_revision(
        self,
        revision: ReportingRevisionRecord,
        rows: Sequence[dict[str, Any]],
    ) -> ReportingRevisionRecord:
        """Commit an immutable revision and its frozen rows.

        Enforces terminal officials, exact supersession, and idempotent replay
        of an identical ``reporting_revision_id``.
        """
        ...

    async def list_revisions(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> tuple[ReportingRevisionRecord, ...]: ...

    async def get_revision(
        self, *, account_id: str, reporting_revision_id: str
    ) -> ReportingRevisionRecord | None: ...

    async def read_revision_rows(
        self,
        *,
        account_id: str,
        reporting_revision_id: str,
        cursor: str | None = None,
        limit: int = 500,
    ) -> ReportingRowPage:
        """Walk one revision's frozen rows in stable order."""
        ...

    async def set_revision_readable(
        self, *, account_id: str, reporting_revision_id: str, readable: bool
    ) -> None:
        """Record that a revision's content is (no longer) readable.

        Retention expiry and storage loss are real; representing them is how an
        obligation becomes honestly ``action_required`` instead of staying
        ``complete`` over evidence nobody can read.
        """
        ...

    # -- adjustments -----------------------------------------------------

    async def commit_adjustment(
        self, adjustment: ReportingAdjustmentRecord
    ) -> ReportingAdjustmentRecord: ...

    async def list_adjustments(
        self, *, account_id: str, reporting_revision_ids: Sequence[str]
    ) -> tuple[ReportingAdjustmentRecord, ...]: ...

    # -- consumer status (preview) ---------------------------------------

    async def record_consumer_status(
        self, status: ConsumerStatusRecord
    ) -> tuple[ConsumerStatusRecord, bool]:
        """Append a consumer status statement, superseding the chain's leaf.

        Returns ``(record, recorded)`` where ``recorded`` is ``False`` for an
        exact idempotent replay.  Supersession is atomic: naming a stale or
        missing leaf must raise rather than fork the chain, so a successful
        retry cannot erase a recorded outage.
        """
        ...

    async def resolve_consumer_status_replay(
        self, status: ConsumerStatusRecord
    ) -> ConsumerStatusRecord | None:
        """Return the stored row when this exact statement was already recorded.

        Exists so the ingest can answer "is this an exact retry?" *before* it
        validates anything time-dependent. ``record_consumer_status`` already
        makes that check, but it runs last, and some validation the ingest does
        first -- notably "``content_mismatch`` must name the revision the seller
        currently requires" -- has an answer that changes as the seller
        publishes. An exact retry sent after a restatement would then be
        rejected for a reason that did not apply when it was first accepted,
        and the spec's ``result_mapping`` requires ``unchanged``.

        Returns ``None`` when the id is unknown, so the caller proceeds to
        validate and record. Raises :class:`LedgerConflictError` with
        ``STATUS_IDENTITY_CONFLICT`` when the id exists with different content:
        reuse with changed content is still a conflict, and answering
        ``unchanged`` there would let a buyer rewrite a recorded statement.
        """
        ...

    async def list_consumer_statuses(
        self,
        *,
        account_id: str,
        consumer_id: str,
        reporting_obligation_ids: Sequence[str] | None = None,
    ) -> tuple[ConsumerStatusRecord, ...]: ...

    # -- issue lifecycle -------------------------------------------------

    async def ensure_issue_opened(
        self,
        *,
        issue_key: str,
        account_id: str,
        consumer_id: str | None,
        observed_at: datetime,
    ) -> ReportingIssueLifecycle:
        """Return this condition's live occurrence, opening one if needed.

        Idempotent: the second caller gets the first caller's ``opened_at``.
        That is the whole point -- AdCP 3.2.0-rc.3 anchors the escalation clock
        to ``opened_at``, so a re-emission that advanced it would let an
        unattended mismatch stay below ``action_required`` indefinitely.

        This is the one write the read path performs. ``get_reporting_status``
        is where a consumer mismatch is *first observed*, and a stable
        first-observation timestamp cannot be derived from immutable evidence
        alone. Implementations must make it safe under concurrent reads; two
        readers of one condition must converge on one row rather than open two
        occurrences.

        A condition retired as ``resolved`` or ``waived`` opens a *new*
        occurrence with the next ``generation``, a new ``issue_id``, and a new
        ``opened_at``, which is exactly the spec's "a recurrence after
        retirement receives a new issue_id".
        """
        ...

    async def set_issue_state(
        self,
        *,
        issue_key: str,
        account_id: str,
        state: Literal["acknowledged", "waived"],
        at: datetime,
        external_ref: str | None = None,
    ) -> ReportingIssueLifecycle:
        """Move a live occurrence forward, or attach an ``external_ref``.

        Deliberately cannot set ``resolved``. The spec forbids retiring a
        ``CONSUMER_STATUS_MISMATCH`` out of a degraded projection while the
        statement that caused it is still the consumer's current leaf, so
        resolution is not an operator action -- it is what the projection does
        when the condition actually clears (see :meth:`retire_issue`).
        Otherwise a seller could unilaterally erase a buyer-attributed
        disagreement, which is the one outcome this separately attributed loop
        exists to prevent.

        ``waived`` *is* an operator action: it records an off-protocol
        agreement to stop acting. It removes the issue from ``issues[]`` and
        still leaves the caller's view degraded.
        """
        ...

    async def retire_issue(
        self, *, issue_key: str, account_id: str, at: datetime
    ) -> ReportingIssueLifecycle | None:
        """Mark this condition's live occurrence ``resolved``.

        Called by the projection when the condition no longer holds. Returns
        ``None`` when there was nothing live to retire, so a repeated
        projection is convergent rather than an error.
        """
        ...

    async def get_issue(self, *, issue_key: str, account_id: str) -> ReportingIssueLifecycle | None:
        """The live occurrence of this condition, or ``None``."""
        ...

    # -- snapshots and pagination ----------------------------------------

    async def open_snapshot(self, *, account_id: str, filters_fingerprint: str) -> LedgerSnapshot:
        """Take a consistent read boundary for one account and filter set."""
        ...

    async def read_page(
        self,
        *,
        snapshot: LedgerSnapshot,
        consumer_id: str | None,
        delivery_config_ids: Sequence[str] | None,
        media_buy_ids: Sequence[str] | None,
        offset: int,
        limit: int,
        changes_after_sequence: int | None,
    ) -> LedgerPage: ...

    # -- worker leasing --------------------------------------------------

    async def lease_period_close(
        self, *, worker_id: str, now: datetime, lease_seconds: float
    ) -> LeasedConfiguration | None:
        """Lease one configuration generation for period-close work.

        Leasing rather than locking so a worker that dies mid-close releases its
        work by expiry instead of wedging the period forever, and so two workers
        cannot both close the same period.  Implementations should prefer the
        generation whose work is most overdue.
        """
        ...

    async def release_period_close(self, lease: LeasedConfiguration, *, worker_id: str) -> None: ...


# --------------------------------------------------------------------------
# Reference implementation
# --------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(value)).hexdigest()


def _revision_identity(revision: ReportingRevisionRecord) -> str:
    """What makes two commits of one revision id "the same revision".

    Content plus binding, excluding readability -- which is mutable state about
    storage, not about what was published.
    """
    payload = {
        "finality": revision.finality,
        "revision_content_sha256": revision.revision_content_sha256,
        "row_count": revision.row_count,
        "control_totals": [list(item) for item in revision.control_totals],
        "obligation": revision.reporting_obligation_id,
        "supersedes": revision.supersedes_reporting_revision_id,
    }
    if revision.canonical_content_digest is not None:
        payload["canonical_content_digest"] = revision.canonical_content_digest.to_wire()
    if revision.managed_control_totals is not None:
        payload["managed_control_totals"] = [
            item.to_wire() for item in revision.managed_control_totals
        ]
    if revision.canonical_content_digest is not None or revision.managed_control_totals is not None:
        payload["managed_metadata"] = managed_revision_metadata(revision)
    return _fingerprint(payload)


def managed_revision_metadata(revision: ReportingRevisionRecord) -> dict[str, Any]:
    """New evidence is exact; omit this block entirely for legacy replay hashes."""
    return {
        "account_id": revision.account_id,
        "created_at": _utc(revision.created_at).isoformat(),
        "observed_at": _utc(revision.observed_at).isoformat(),
        "data_through": _utc(revision.data_through).isoformat() if revision.data_through else None,
        "finality_basis": revision.finality_basis,
        "finality_policy_id": revision.finality_policy_id,
        "finalized_at": _utc(revision.finalized_at).isoformat() if revision.finalized_at else None,
        "readable_at_commit": revision.readable_at_commit,
        "source_publication_id": revision.source_publication_id,
        "source_manifest_sha256": revision.source_manifest_sha256,
    }


def validate_managed_revision_rows(
    revision: ReportingRevisionRecord, rows: Sequence[dict[str, Any]]
) -> None:
    """Managed expectations must start from an internally consistent Core revision.

    This checks Core's existing binding only. It does not derive or substitute the
    separate managed canonical digest or fetch its pinned contract.
    """
    if revision.canonical_content_digest is None and revision.managed_control_totals is None:
        return
    from adcp.reporting.ledger.producer import revision_content_sha256

    actual = revision_content_sha256(
        reporting_revision_id=revision.reporting_revision_id,
        row_count=revision.row_count,
        control_totals=revision.control_totals,
        reporting_rows=rows,
        control_total_evidence=revision.managed_control_totals,
    )
    if actual != revision.revision_content_sha256:
        raise LedgerConflictError(
            "REVISION_CONTENT_MISMATCH", "revision row content does not match its immutable binding"
        )


def _consumer_status_identity(status: ConsumerStatusRecord) -> str:
    payload: dict[str, Any] = {
        "chain": list(status.chain_key),
        "consumer_status": status.consumer_status,
        "status_as_of": _utc(status.status_as_of).isoformat(),
        "supersedes": status.supersedes_reporting_status_id,
        "obligation": status.reporting_obligation_id,
        "revision": status.reporting_revision_id,
        "digest": status.observed_revision_content_sha256,
        "failure_code": status.failure_code,
    }
    # Conditional on purpose. ``canonical_json_utf8_v1`` encodes ``None`` as
    # ``null``, so including the key unconditionally would change the digest of
    # every statement that has no mismatch_code -- i.e. every row an rc.2 SDK
    # wrote. After an in-place upgrade a buyer's exact retry would then fail
    # with STATUS_IDENTITY_CONFLICT instead of replaying as ``unchanged``,
    # which is the one thing an idempotent append-only surface must never do.
    # Omitting the key when absent keeps rc.2 digests byte-identical while a
    # code-only change is still a conflict: "the metric is missing" and "the
    # currency is wrong" are different claims and must not share one immutable
    # identity.
    if status.mismatch_code is not None:
        payload["mismatch_code"] = status.mismatch_code
    return _fingerprint(payload)


#: Forward-only issue lifecycle, per ``reporting-status-issue.json``
#: ``issue_lifecycle``: state moves only through ``open``, then
#: ``acknowledged``, then ``resolved`` or ``waived``.
#:
#: The reverse edges are what matter. Without ``waived -> acknowledged`` being
#: refused, an operator could un-waive an issue and republish the same
#: ``issue_id`` and ``opened_at`` -- resurrecting a work item both parties had
#: agreed to stop acting on, under an identity a consumer has already filed
#: away. A recurrence is supposed to get a *new* occurrence, not a revived one.
_ISSUE_STATE_SUCCESSORS: dict[str, frozenset[str]] = {
    "open": frozenset({"acknowledged", "resolved", "waived"}),
    "acknowledged": frozenset({"resolved", "waived"}),
    "resolved": frozenset(),
    "waived": frozenset(),
}


def issue_is_retirable(current: str) -> bool:
    """Whether the projection may move this state to ``resolved``.

    ``waived`` is not retirable: it is already out of the projection by
    agreement, and overwriting that readable act with ``resolved`` is an edge
    the forward-only lifecycle forbids. Both stores consult this rather than
    each remembering the rule.
    """
    return "resolved" in _ISSUE_STATE_SUCCESSORS.get(current, frozenset())


def check_issue_state_transition(current: str, requested: str) -> None:
    """Refuse any transition the spec's forward-only lifecycle forbids.

    Raises :class:`LedgerConflictError` rather than returning a bool so neither
    store can forget to act on the answer.
    """
    if requested == current:
        # Idempotent: re-acknowledging an acknowledged issue is a no-op, which
        # is what makes an operator retry safe.
        return
    if requested not in _ISSUE_STATE_SUCCESSORS.get(current, frozenset()):
        raise LedgerConflictError(
            "ISSUE_STATE_TRANSITION_INVALID",
            "issue_state moves only forward through open, acknowledged, then resolved or "
            f"waived; {current!r} -> {requested!r} is not permitted. A recurrence gets a new "
            "occurrence rather than reviving a retired one",
        )


class InMemoryReportingLedgerStore:
    """Process-local reference store. Correct, ordered, and not durable.

    ``clock`` supplies change timestamps and the snapshot observation boundary,
    standing in for the database clock a durable store reads. Override it to
    place a test's ledger boundary at a deliberate instant.
    """

    def __init__(
        self, *, clock: Callable[[], datetime] | None = None, notifications: bool = False
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._configurations: dict[ReportingConfigurationGenerationKey, ReportingConfiguration] = {}
        self._obligations: dict[str, ReportingObligationRecord] = {}
        self._obligation_by_period: dict[
            tuple[ReportingConfigurationGenerationKey, str, str], str
        ] = {}
        self._revisions: dict[str, ReportingRevisionRecord] = {}
        self._revision_identity: dict[str, str] = {}
        self._rows: dict[str, tuple[dict[str, Any], ...]] = {}
        self._restatement_checkpoints: dict[str, RestatementCheckpoint] = {}
        self._adjustments: dict[str, ReportingAdjustmentRecord] = {}
        self._statuses: dict[tuple[str, str, str], ConsumerStatusRecord] = {}
        self._status_identity: dict[tuple[str, str, str], str] = {}
        self._changes: list[tuple[int, str, LedgerRecordKind, str, datetime]] = []
        self._sequence = 0
        self._leases: dict[ReportingConfigurationGenerationKey, tuple[str, datetime]] = {}
        # When each generation was last handed to a worker, so releasing a
        # lease sends that generation to the back of the queue instead of
        # letting it win every turn.
        self._lease_turns: dict[ReportingConfigurationGenerationKey, int] = {}
        self._lease_turn = 0
        # Live occurrence per (account, issue_key), plus the retired generation
        # high-water mark so a recurrence never reuses an id.
        self._issues: dict[tuple[str, str], ReportingIssueLifecycle] = {}
        self._issue_generations: dict[tuple[str, str], int] = {}
        self._issue_status_scopes: dict[tuple[str, str], ReportingStatusScope] = {}
        self._notification_state: NotificationState | None = None
        self._status_notification_state: Any = None
        if notifications:
            from adcp.reporting.outbox.memory import NotificationState

            self._notification_state = NotificationState()
            self._notification_state.issue_scopes = self._issue_status_scopes

    @asynccontextmanager
    async def _mutation(self) -> AsyncIterator[None]:
        """Publish domain changes and notifications under one rollback boundary.

        Default-off stores retain their original lock cost. The reference
        in-memory transaction copies retained values only when opted in.
        """
        owner = (id(self), asyncio.current_task())
        if _MEMORY_TRANSACTION.get() == owner:
            yield
            return
        async with self._lock:
            token = _MEMORY_TRANSACTION.set(owner)
            before = (
                deepcopy(
                    {
                        key: value
                        for key, value in vars(self).items()
                        if key not in {"_lock", "_clock"}
                    }
                )
                if self._notification_state is not None
                else None
            )
            try:
                dirty_start = len(self._notification_state.dirty) if self._notification_state else 0
                yield
                if self._notification_state is not None:
                    from adcp.reporting.ledger.status_snapshot import settle_memory_snapshot
                    from adcp.reporting.outbox.status import StatusBoundary

                    dirty = tuple(self._notification_state.dirty[dirty_start:])
                    transaction_id = str(uuid4())
                    for account_id in sorted({d.scope.account_id for d in dirty}):
                        snapshot = settle_memory_snapshot(self, account_id)
                        account_dirty = tuple(d for d in dirty if d.scope.account_id == account_id)
                        self._notification_state.boundaries.append(
                            StatusBoundary(
                                transaction_id,
                                max(d.sequence for d in account_dirty),
                                account_dirty,
                                snapshot,
                            )
                        )
            except BaseException:
                if before is not None:
                    vars(self).update(before)
                raise
            finally:
                _MEMORY_TRANSACTION.reset(token)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[InMemoryReportingLedgerStore]:
        """Group several source mutations into one atomic, replayable boundary."""
        async with self._mutation():
            yield self

    def _record_notification(self, event: ReportingDomainEvent) -> None:
        if self._notification_state is not None:
            self._notification_state.enqueue(event)

    def _dirty_status(
        self,
        scope: ReportingStatusScope,
        reason: DirtyReason,
        before: ReportingStatusEvidence | None = None,
        after: ReportingStatusEvidence | None = None,
    ) -> None:
        if self._notification_state is not None:
            self._notification_state.mark_dirty(scope, reason, self._clock(), before, after)

    def _resolve_issue_scope(
        self,
        *,
        account_id: str,
        consumer_id: str | None,
        issue_id: str,
        status_scope: ReportingStatusScope | None,
    ) -> ReportingStatusScope:
        """Validate a requested scope refinement without touching retained state.

        Default-off stores deliberately keep no rollback copy, so every caller
        that moves an issue record must clear this check *before* mutating:
        PostgreSQL rolls the statement back, the reference store cannot.
        """
        existing = self._issue_status_scopes.get((account_id, issue_id))
        scope = (
            status_scope or existing or ReportingStatusScope(account_id, consumer_id=consumer_id)
        )
        if scope.account_id != account_id or scope.consumer_id != consumer_id:
            raise ReportingNotificationError("invalid_status_scope")
        validate_scope_refinement(existing, scope)
        if scope.generation_key is not None:
            configuration = self._configurations.get(scope.generation_key)
            if configuration is None or (
                scope.feed_purpose is not None and configuration.feed_purpose != scope.feed_purpose
            ):
                raise ReportingNotificationError("invalid_status_scope")
        if scope.reporting_obligation_id is not None:
            obligation = self._obligations.get(scope.reporting_obligation_id)
            if (
                obligation is None
                or obligation.account_id != scope.account_id
                or (
                    scope.generation_key is not None
                    and obligation.generation_key != scope.generation_key
                )
                or (
                    scope.feed_purpose is not None and obligation.feed_purpose != scope.feed_purpose
                )
            ):
                raise ReportingNotificationError("invalid_status_scope")
        return scope

    def _dirty_issue(
        self,
        issue: ReportingIssueLifecycle,
        status_scope: ReportingStatusScope | None,
        before: ReportingIssueLifecycle | None = None,
        *,
        enqueue: bool = True,
    ) -> None:
        key = (issue.account_id, issue.issue_id)
        existing = self._issue_status_scopes.get(key)
        scope = self._resolve_issue_scope(
            account_id=issue.account_id,
            consumer_id=issue.consumer_id,
            issue_id=issue.issue_id,
            status_scope=status_scope,
        )
        self._issue_status_scopes[key] = scope
        if enqueue and (before != issue or existing != scope):
            self._dirty_status(
                scope, "issue", issue_evidence(before) if before else None, issue_evidence(issue)
            )

    async def create_schema(self) -> None:
        return None

    def _append(self, account_id: str, kind: LedgerRecordKind, record_id: str) -> None:
        self._sequence += 1
        self._changes.append((self._sequence, account_id, kind, record_id, self._clock()))

    # -- configurations --------------------------------------------------

    async def put_configuration(self, configuration: ReportingConfiguration) -> None:
        reject_reserved_authoritative_party(configuration)
        async with self._mutation():
            key = configuration.generation_key
            existing = self._configurations.get(key)
            if existing is not None and _fingerprint(_config_payload(existing)) != _fingerprint(
                _config_payload(configuration)
            ):
                raise LedgerConflictError(
                    "CONFIGURATION_GENERATION_IMMUTABLE",
                    f"configuration {key.delivery_config_id}@{key.delivery_config_version} "
                    "already exists with different content for this account; "
                    "publish a new version instead of editing a retained generation",
                )
            self._configurations[key] = configuration
            changed = existing is None or configuration_lifecycle(
                existing
            ) != configuration_lifecycle(configuration)
            if changed and self._notification_state is not None:
                self._dirty_status(
                    ReportingStatusScope(configuration.account_id, configuration.generation_key),
                    "configuration",
                    before=configuration_evidence(existing) if existing is not None else None,
                    after=configuration_evidence(configuration),
                )

    async def list_configurations(
        self, *, account_id: str, delivery_config_ids: Sequence[str] | None = None
    ) -> tuple[ReportingConfiguration, ...]:
        wanted = set(delivery_config_ids) if delivery_config_ids else None
        return tuple(
            configuration
            for configuration in self._configurations.values()
            if configuration.account_id == account_id
            and (wanted is None or configuration.delivery_config_id in wanted)
        )

    # -- obligations -----------------------------------------------------

    async def commit_obligation(
        self, obligation: ReportingObligationRecord
    ) -> ReportingObligationRecord:
        async with self._mutation():
            key = (
                obligation.generation_key,
                _utc(obligation.period.start).isoformat(),
                _utc(obligation.period.end).isoformat(),
            )
            existing_id = self._obligation_by_period.get(key)
            if existing_id is not None:
                return self._obligations[existing_id]
            if obligation.reporting_obligation_id in self._obligations:
                raise LedgerConflictError(
                    "OBLIGATION_IDENTITY_CONFLICT",
                    "the obligation identifier already belongs to a different logical period",
                )
            require_frozen_currency(obligation.currency)
            self._obligations[obligation.reporting_obligation_id] = obligation
            self._obligation_by_period[key] = obligation.reporting_obligation_id
            self._append(obligation.account_id, "obligation", obligation.reporting_obligation_id)
            if self._notification_state is not None:
                self._dirty_status(
                    ReportingStatusScope.for_obligation(obligation),
                    "obligation",
                    after=ReportingStatusEvidence("obligation", obligation.reporting_obligation_id),
                )
            return obligation

    async def get_obligation(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> ReportingObligationRecord | None:
        found = self._obligations.get(reporting_obligation_id)
        return found if found and found.account_id == account_id else None

    async def find_obligation(
        self,
        *,
        account_id: str,
        delivery_config_id: str,
        delivery_config_version: int,
        period_start: datetime,
        period_end: datetime,
    ) -> ReportingObligationRecord | None:
        key = (
            ReportingConfigurationGenerationKey(
                account_id=account_id,
                delivery_config_id=delivery_config_id,
                delivery_config_version=delivery_config_version,
            ),
            _utc(period_start).isoformat(),
            _utc(period_end).isoformat(),
        )
        found = self._obligation_by_period.get(key)
        return (
            await self.get_obligation(account_id=account_id, reporting_obligation_id=found)
            if found
            else None
        )

    # -- revisions -------------------------------------------------------

    async def commit_revision(
        self, revision: ReportingRevisionRecord, rows: Sequence[dict[str, Any]]
    ) -> ReportingRevisionRecord:
        rows = tuple(deepcopy(row) for row in rows)
        # Checked first, exactly as PgReportingLedgerStore does: a caller whose
        # rows do not match its own declared count must get the same code from
        # both stores, not whichever invariant that store happens to reach.
        if revision.row_count != len(rows):
            raise LedgerConflictError(
                "ROW_COUNT_MISMATCH",
                f"revision declares {revision.row_count} rows but {len(rows)} were supplied",
            )
        validate_managed_revision_rows(revision, rows)
        async with self._mutation():
            identity = _revision_identity(revision)
            existing = self._revisions.get(revision.reporting_revision_id)
            if existing is not None:
                if existing.account_id != revision.account_id:
                    raise LedgerConflictError(
                        "REVISION_NOT_FOUND", "no such revision for this account"
                    )
                if self._revision_identity[revision.reporting_revision_id] != identity:
                    raise LedgerConflictError(
                        "REVISION_IMMUTABLE",
                        f"revision {revision.reporting_revision_id} already exists with "
                        "different content; a restatement is a new revision",
                    )
                return existing
            obligation = self._obligations.get(revision.reporting_obligation_id)
            if obligation is None or obligation.account_id != revision.account_id:
                raise LedgerConflictError(
                    "OBLIGATION_NOT_FOUND",
                    "a revision must attach to an obligation committed at the period close",
                )
            validate_revision_currency(obligation, revision, rows)
            siblings = [
                item
                for item in self._revisions.values()
                if item.reporting_obligation_id == revision.reporting_obligation_id
            ]
            if revision.finality == "official" and any(
                item.finality == "official" for item in siblings
            ):
                raise LedgerConflictError(
                    "OFFICIAL_REVISION_TERMINAL",
                    "an official revision already exists for this obligation; publish a later "
                    "source correction as an adjustment",
                )
            if revision.supersedes_reporting_revision_id:
                self._require_current_leaf(revision, siblings)
            self._revisions[revision.reporting_revision_id] = revision
            self._revision_identity[revision.reporting_revision_id] = identity
            self._rows[revision.reporting_revision_id] = tuple(dict(row) for row in rows)
            self._append(revision.account_id, "revision", revision.reporting_revision_id)
            if self._notification_state is not None:
                from adcp.reporting.ledger.notification_events import revision_event

                self._record_notification(revision_event(revision, self._clock()))
                self._dirty_status(
                    ReportingStatusScope.for_obligation(obligation),
                    "revision",
                    after=ReportingStatusEvidence(
                        "revision",
                        revision.reporting_revision_id,
                        readable=revision.readable,
                        supersedes_id=revision.supersedes_reporting_revision_id,
                    ),
                )
            return revision

    def _require_current_leaf(
        self, revision: ReportingRevisionRecord, siblings: Sequence[ReportingRevisionRecord]
    ) -> None:
        target = revision.supersedes_reporting_revision_id
        known = {item.reporting_revision_id for item in siblings}
        if target not in known:
            raise LedgerConflictError(
                "SUPERSEDES_UNKNOWN",
                f"revision {target} is not part of this obligation's chain",
            )
        already = {
            item.supersedes_reporting_revision_id
            for item in siblings
            if item.supersedes_reporting_revision_id
        }
        if target in already:
            raise LedgerConflictError(
                "SUPERSEDES_STALE",
                f"revision {target} has already been superseded; a stale pointer would fork "
                "the chain and let a successful retry erase a recorded restatement",
            )

    async def list_revisions(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> tuple[ReportingRevisionRecord, ...]:
        return tuple(
            item
            for item in self._revisions.values()
            if item.account_id == account_id
            and item.reporting_obligation_id == reporting_obligation_id
        )

    async def get_restatement_checkpoint(
        self, *, account_id: str, reporting_obligation_id: str
    ) -> RestatementCheckpoint | None:
        checkpoint = self._restatement_checkpoints.get(reporting_obligation_id)
        return checkpoint if checkpoint and checkpoint.account_id == account_id else None

    async def record_restatement_checkpoint(
        self, checkpoint: RestatementCheckpoint
    ) -> RestatementCheckpoint:
        async with self._lock:
            obligation = self._obligations.get(checkpoint.reporting_obligation_id)
            if obligation is None or obligation.account_id != checkpoint.account_id:
                raise LedgerConflictError(
                    "OBLIGATION_NOT_FOUND",
                    "a restatement checkpoint must attach to an obligation for this account",
                )
            existing = self._restatement_checkpoints.get(checkpoint.reporting_obligation_id)
            if existing is not None:
                if checkpoint.next_observation < existing.next_observation:
                    return existing
                if checkpoint.next_observation == existing.next_observation and _utc(
                    checkpoint.checked_at
                ) <= _utc(existing.checked_at):
                    return existing
            self._restatement_checkpoints[checkpoint.reporting_obligation_id] = checkpoint
            return checkpoint

    async def get_revision(
        self, *, account_id: str, reporting_revision_id: str
    ) -> ReportingRevisionRecord | None:
        found = self._revisions.get(reporting_revision_id)
        return found if found and found.account_id == account_id else None

    async def read_revision_rows(
        self,
        *,
        account_id: str,
        reporting_revision_id: str,
        cursor: str | None = None,
        limit: int = 500,
    ) -> ReportingRowPage:
        revision = await self.get_revision(
            account_id=account_id, reporting_revision_id=reporting_revision_id
        )
        if revision is None:
            raise LedgerConflictError("REVISION_NOT_FOUND", "no such revision for this account")
        rows = self._rows.get(reporting_revision_id, ())
        offset = int(decode_cursor(cursor).get("offset", 0)) if cursor else 0
        window = rows[offset : offset + limit]
        has_more = offset + limit < len(rows)
        return ReportingRowPage(
            rows=tuple(deepcopy(row) for row in window),
            total_count=len(rows),
            has_more=has_more,
            cursor=(
                encode_cursor({"revision": reporting_revision_id, "offset": offset + limit})
                if has_more
                else None
            ),
        )

    async def set_revision_readable(
        self, *, account_id: str, reporting_revision_id: str, readable: bool
    ) -> None:
        async with self._mutation():
            existing = self._revisions.get(reporting_revision_id)
            if existing is None or existing.account_id != account_id:
                raise LedgerConflictError("REVISION_NOT_FOUND", "no such revision for this account")
            if existing.readable == readable:
                return
            self._revisions[reporting_revision_id] = replace(existing, readable=readable)
            if self._notification_state is not None:
                obligation = self._obligations[existing.reporting_obligation_id]
                self._dirty_status(
                    ReportingStatusScope.for_obligation(obligation),
                    "readability",
                    ReportingStatusEvidence(
                        "revision", reporting_revision_id, readable=existing.readable
                    ),
                    ReportingStatusEvidence("revision", reporting_revision_id, readable=readable),
                )

    # -- adjustments -----------------------------------------------------

    async def commit_adjustment(
        self, adjustment: ReportingAdjustmentRecord
    ) -> ReportingAdjustmentRecord:
        async with self._mutation():
            existing = self._adjustments.get(adjustment.reporting_adjustment_id)
            if existing is not None:
                if existing.account_id != adjustment.account_id:
                    raise LedgerConflictError("ADJUSTMENT_UNAVAILABLE", "adjustment is unavailable")
                if existing != adjustment:
                    raise LedgerConflictError(
                        "ADJUSTMENT_IMMUTABLE", "adjustment content is immutable"
                    )
                return existing
            revision = self._revisions.get(adjustment.adjusts_reporting_revision_id)
            if revision is None or revision.account_id != adjustment.account_id:
                raise LedgerConflictError(
                    "REVISION_NOT_FOUND", "an adjustment must name a committed revision"
                )
            if revision.finality != "official":
                raise LedgerConflictError(
                    "ADJUSTMENT_REQUIRES_OFFICIAL",
                    "adjustments correct an official revision; restate a snapshot with a "
                    "superseding snapshot revision instead",
                )
            validate_adjustment_currency(
                self._obligations[revision.reporting_obligation_id], adjustment
            )
            self._adjustments[adjustment.reporting_adjustment_id] = adjustment
            self._append(adjustment.account_id, "adjustment", adjustment.reporting_adjustment_id)
            if self._notification_state is not None:
                from adcp.reporting.ledger.notification_events import adjustment_event

                self._record_notification(adjustment_event(adjustment, self._clock()))
                self._dirty_status(
                    ReportingStatusScope.for_obligation(
                        self._obligations[revision.reporting_obligation_id]
                    ),
                    "adjustment",
                    after=ReportingStatusEvidence("adjustment", adjustment.reporting_adjustment_id),
                )
            return adjustment

    async def list_adjustments(
        self, *, account_id: str, reporting_revision_ids: Sequence[str]
    ) -> tuple[ReportingAdjustmentRecord, ...]:
        wanted = set(reporting_revision_ids)
        return tuple(
            item
            for item in self._adjustments.values()
            if item.account_id == account_id and item.adjusts_reporting_revision_id in wanted
        )

    # -- consumer status -------------------------------------------------

    async def resolve_consumer_status_replay(
        self, status: ConsumerStatusRecord
    ) -> ConsumerStatusRecord | None:
        async with self._lock:
            return self._replay(status)

    def _replay(self, status: ConsumerStatusRecord) -> ConsumerStatusRecord | None:
        key = (status.account_id, status.consumer_id, status.reporting_status_id)
        existing = self._statuses.get(key)
        if existing is None:
            return None
        if self._status_identity[key] != _consumer_status_identity(status):
            raise LedgerConflictError(
                "STATUS_IDENTITY_CONFLICT",
                f"reporting_status_id {status.reporting_status_id} was already "
                "recorded with different content",
            )
        return existing

    async def record_consumer_status(
        self, status: ConsumerStatusRecord
    ) -> tuple[ConsumerStatusRecord, bool]:
        from adcp.reporting.evidence import consumer_reference

        consumer_reference(status.consumer_id)
        async with self._mutation():
            identity = _consumer_status_identity(status)
            existing = self._replay(status)
            if existing is not None:
                return existing, False
            from adcp.reporting.ledger.status_snapshot import (
                memory_snapshot,
                validate_status_evidence,
            )

            validate_status_evidence(status, memory_snapshot(self, status.account_id))
            leaf = self._current_status_leaf(status.chain_key)
            if status.supersedes_reporting_status_id:
                if (
                    leaf is None
                    or leaf.reporting_status_id != status.supersedes_reporting_status_id
                ):
                    raise LedgerConflictError(
                        "STATUS_SUPERSEDES_STALE",
                        "supersedes_reporting_status_id must name this chain's current leaf; "
                        "a stale pointer would let a successful retry erase a recorded outage",
                    )
                self._statuses[(leaf.account_id, leaf.consumer_id, leaf.reporting_status_id)] = (
                    replace(leaf, superseded=True)
                )
            elif leaf is not None:
                raise LedgerConflictError(
                    "STATUS_SUPERSEDES_REQUIRED",
                    "this chain already has a current statement; a new statement must "
                    "explicitly supersede it",
                )
            key = (status.account_id, status.consumer_id, status.reporting_status_id)
            self._statuses[key] = status
            self._status_identity[key] = identity
            self._append(status.account_id, "consumer_status", status.reporting_status_id)
            from adcp.reporting.ledger.status_snapshot import settle_memory_snapshot

            settle_memory_snapshot(self, status.account_id)
            if self._notification_state is not None:
                self._dirty_status(
                    ReportingStatusScope(
                        status.account_id,
                        status.generation_key,
                        status.reporting_obligation_id,
                        status.consumer_id,
                    ),
                    "consumer_status",
                    (
                        ReportingStatusEvidence("consumer_status", leaf.reporting_status_id)
                        if leaf is not None
                        else None
                    ),
                    ReportingStatusEvidence(
                        "consumer_status",
                        status.reporting_status_id,
                        supersedes_id=status.supersedes_reporting_status_id,
                    ),
                )
            return status, True

    async def record_consumer_status_with_lifecycle(
        self, status: ConsumerStatusRecord
    ) -> tuple[ConsumerStatusRecord, bool]:
        return await self.record_consumer_status(status)

    async def read_status_snapshot(self, *, account_id: str) -> ReportingStatusSnapshot:
        from adcp.reporting.ledger.status_snapshot import settle_memory_snapshot

        async with self._mutation():
            return settle_memory_snapshot(self, account_id)

    def _current_status_leaf(self, chain_key: tuple[Any, ...]) -> ConsumerStatusRecord | None:
        for item in self._statuses.values():
            if item.chain_key == chain_key and not item.superseded:
                return item
        return None

    async def list_consumer_statuses(
        self,
        *,
        account_id: str,
        consumer_id: str,
        reporting_obligation_ids: Sequence[str] | None = None,
    ) -> tuple[ConsumerStatusRecord, ...]:
        wanted = set(reporting_obligation_ids) if reporting_obligation_ids is not None else None
        result = []
        for item in self._statuses.values():
            if item.account_id != account_id or item.consumer_id != consumer_id:
                continue
            if wanted is not None and not self._matches_obligations(item, wanted):
                continue
            result.append(item)
        return tuple(result)

    def _matches_obligations(self, status: ConsumerStatusRecord, wanted: set[str]) -> bool:
        """Attach a statement to an obligation by id *or* by logical period key.

        The logical-key path is what makes the loop repairable: a statement
        filed as ``obligation_missing`` predates the obligation it is about, and
        when the seller later creates that obligation the existing chain must
        attach to it rather than being lost, forked, or reset.
        """
        for obligation_id in wanted:
            obligation = self._obligations.get(obligation_id)
            if obligation is None or obligation.account_id != status.account_id:
                continue
            if status.reporting_obligation_id == obligation_id:
                return True
            if (
                obligation.generation_key == status.generation_key
                and obligation.report_definition_id == status.report_definition_id
                and _utc(obligation.period.start) == _utc(status.period_start)
                and _utc(obligation.period.end) == _utc(status.period_end)
            ):
                return True
        return False

    # -- issue lifecycle -------------------------------------------------

    async def ensure_issue_opened(
        self,
        *,
        issue_key: str,
        account_id: str,
        consumer_id: str | None,
        observed_at: datetime,
        status_scope: ReportingStatusScope | None = None,
    ) -> ReportingIssueLifecycle:
        async with self._mutation():
            key = (account_id, issue_key)
            live = self._issues.get(key)
            if live is not None:
                if live.consumer_id != consumer_id:
                    raise ReportingNotificationError("invalid_status_scope")
                previous_scope = self._issue_status_scopes.get((account_id, live.issue_id))
                if status_scope is not None:
                    validate_scope_refinement(previous_scope, status_scope)
                else:
                    status_scope = previous_scope
            if live is not None and live.live:
                self._dirty_issue(live, status_scope, live)
                return live
            generation = self._issue_generations.get(key, 0) + 1
            record = ReportingIssueLifecycle(
                issue_key=issue_key,
                issue_id=issue_id_for_occurrence(issue_key, generation),
                account_id=account_id,
                consumer_id=consumer_id,
                opened_at=_utc(observed_at),
                issue_state="open",
                generation=generation,
            )
            self._resolve_issue_scope(
                account_id=account_id,
                consumer_id=consumer_id,
                issue_id=record.issue_id,
                status_scope=status_scope,
            )
            self._issue_generations[key] = generation
            self._issues[key] = record
            self._dirty_issue(record, status_scope)
            return record

    async def set_issue_state(
        self,
        *,
        issue_key: str,
        account_id: str,
        state: Literal["acknowledged", "waived"],
        at: datetime,
        external_ref: str | None = None,
        status_scope: ReportingStatusScope | None = None,
    ) -> ReportingIssueLifecycle:
        async with self._mutation():
            if state not in {"acknowledged", "waived"}:
                raise LedgerConflictError(
                    "ISSUE_STATE_NOT_OPERATOR_SETTABLE",
                    f"issue_state {state!r} is not settable by an operator. 'resolved' is "
                    "reachable only when the condition actually clears -- the projection "
                    "retires it -- because a seller must not retire a mismatch out of a "
                    "degraded projection while the statement that caused it is still the "
                    "consumer's current leaf",
                )
            key = (account_id, issue_key)
            live = self._issues.get(key)
            if live is None or not live.live:
                raise LedgerConflictError(
                    "ISSUE_NOT_OPEN",
                    f"no open issue {issue_key!r} for this account; a retired issue cannot be "
                    "reopened, and a recurrence gets a new occurrence",
                )
            check_issue_state_transition(live.issue_state, state)
            updated = replace(
                live,
                issue_state=state,
                external_ref=external_ref or live.external_ref,
                # Set on the way into a retired state and never cleared.
                retired_at=_utc(at) if state == "waived" else live.retired_at,
            )
            self._resolve_issue_scope(
                account_id=account_id,
                consumer_id=live.consumer_id,
                issue_id=live.issue_id,
                status_scope=status_scope,
            )
            self._issues[key] = updated
            # Derive the no-op from the resulting record rather than predicting
            # it: an idempotent re-acknowledge changes nothing and enqueues
            # nothing, while anything that does move retained evidence stays
            # reconstructable for the projector.
            self._dirty_issue(updated, status_scope, live)
            return updated

    async def retire_issue(
        self,
        *,
        issue_key: str,
        account_id: str,
        at: datetime,
        status_scope: ReportingStatusScope | None = None,
    ) -> ReportingIssueLifecycle | None:
        async with self._mutation():
            key = (account_id, issue_key)
            live = self._issues.get(key)
            if live is None or not live.live or not issue_is_retirable(live.issue_state):
                # Convergent: nothing live, or already waived. A waived issue
                # is retired from the projection by agreement, and overwriting
                # that readable act with `resolved` is an edge the forward-only
                # lifecycle forbids -- enforced here rather than left to each
                # caller to remember.
                return None
            check_issue_state_transition(live.issue_state, "resolved")
            retired = replace(live, issue_state="resolved", retired_at=_utc(at))
            self._resolve_issue_scope(
                account_id=account_id,
                consumer_id=live.consumer_id,
                issue_id=live.issue_id,
                status_scope=status_scope,
            )
            self._issues[key] = retired
            self._dirty_issue(retired, status_scope, live)
            return retired

    async def get_issue(self, *, issue_key: str, account_id: str) -> ReportingIssueLifecycle | None:
        async with self._lock:
            # Live occurrences only, matching the Protocol docstring and the
            # Postgres store. Returning a retired row here would let the
            # projection re-emit a resolved issue's opened_at.
            live = self._issues.get((account_id, issue_key))
            return live if live is not None and live.live else None

    # -- snapshots -------------------------------------------------------

    async def open_snapshot(self, *, account_id: str, filters_fingerprint: str) -> LedgerSnapshot:
        async with self._lock:
            as_of = _utc(self._clock())
            max_sequence = max(
                (item[0] for item in self._changes if item[1] == account_id), default=0
            )
            return LedgerSnapshot(
                snapshot_id="rpls_"
                + _fingerprint([account_id, filters_fingerprint, max_sequence])[:32],
                account_id=account_id,
                ledger_as_of=as_of,
                max_sequence=max_sequence,
            )

    async def read_page(
        self,
        *,
        snapshot: LedgerSnapshot,
        consumer_id: str | None,
        delivery_config_ids: Sequence[str] | None,
        media_buy_ids: Sequence[str] | None,
        offset: int,
        limit: int,
        changes_after_sequence: int | None,
        feed_purposes: Sequence[str] | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> LedgerPage:
        lower = changes_after_sequence or 0
        selected = [
            item
            for item in self._changes
            if item[1] == snapshot.account_id and lower < item[0] <= snapshot.max_sequence
        ]
        config_filter = set(delivery_config_ids) if delivery_config_ids else None
        media_buy_filter = set(media_buy_ids) if media_buy_ids else None

        records: list[tuple[int, LedgerRecordKind, Any]] = []
        for sequence, _account, kind, record_id, _committed in sorted(selected):
            record = self._resolve(kind, record_id, snapshot.account_id, consumer_id)
            if record is None or record.account_id != snapshot.account_id:
                continue
            if not self._in_scope(
                kind,
                record,
                config_filter,
                media_buy_filter,
                consumer_id,
                feed_purposes,
                period_start,
                period_end,
            ):
                continue
            records.append((sequence, kind, record))

        window = records[offset : offset + limit]
        has_more = offset + limit < len(records)
        return LedgerPage(
            obligations=tuple(item[2] for item in window if item[1] == "obligation"),
            revisions=tuple(item[2] for item in window if item[1] == "revision"),
            adjustments=tuple(item[2] for item in window if item[1] == "adjustment"),
            consumer_statuses=tuple(item[2] for item in window if item[1] == "consumer_status"),
            total_count=len(records),
            has_more=has_more,
            cursor=(
                encode_cursor({"snapshot": snapshot.snapshot_id, "offset": offset + limit})
                if has_more
                else None
            ),
        )

    def _resolve(
        self, kind: str, record_id: str, account_id: str = "", consumer_id: str | None = None
    ) -> Any:
        if kind == "obligation":
            return self._obligations.get(record_id)
        if kind == "revision":
            return self._revisions.get(record_id)
        if kind == "adjustment":
            return self._adjustments.get(record_id)
        if kind == "consumer_status":
            return self._statuses.get((account_id, consumer_id or "", record_id))
        # Optional delivery records have their own retained snapshot reader.
        # Core status must not count or expose them, even in a shared ledger.
        return None

    def _in_scope(
        self,
        kind: LedgerRecordKind,
        record: Any,
        config_filter: set[str] | None,
        media_buy_filter: set[str] | None,
        consumer_id: str | None,
        feed_purposes: Sequence[str] | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> bool:
        from adcp.reporting.ledger.status_projection import configuration_selected, period_selected

        if kind == "consumer_status":
            # A caller sees only its own statements; another consumer's
            # operational status is not disclosed.
            if consumer_id is None or record.consumer_id != consumer_id:
                return False
            configuration = self._configurations.get(record.generation_key)
            return (
                configuration is not None
                and configuration_selected(
                    configuration,
                    delivery_config_ids=tuple(config_filter or ()),
                    media_buy_ids=tuple(media_buy_filter or ()),
                    feed_purposes=feed_purposes or (),
                )
                and period_selected(
                    record.period_start, record.period_end, period_start, period_end
                )
            )
        obligation = self._obligation_for(kind, record)
        if obligation is None:
            return False
        configuration = self._configurations.get(obligation.generation_key)
        if (
            configuration is None
            or not configuration_selected(
                configuration,
                delivery_config_ids=tuple(config_filter or ()),
                media_buy_ids=tuple(media_buy_filter or ()),
                feed_purposes=feed_purposes or (),
            )
            or not period_selected(
                obligation.period.start, obligation.period.end, period_start, period_end
            )
        ):
            return False
        if config_filter is not None and obligation.delivery_config_id not in config_filter:
            return False
        if media_buy_filter is not None and not media_buy_filter.intersection(
            obligation.media_buy_ids
        ):
            return False
        return True

    def _obligation_for(
        self, kind: LedgerRecordKind, record: Any
    ) -> ReportingObligationRecord | None:
        if kind == "obligation":
            found: ReportingObligationRecord = record
            return found
        if kind == "revision":
            return self._obligations.get(record.reporting_obligation_id)
        revision = self._revisions.get(record.adjusts_reporting_revision_id)
        return self._obligations.get(revision.reporting_obligation_id) if revision else None

    # -- leasing ---------------------------------------------------------

    async def lease_period_close(
        self, *, worker_id: str, now: datetime, lease_seconds: float
    ) -> LeasedConfiguration | None:
        from datetime import timedelta

        async with self._lock:
            moment = _utc(now)
            # Rank leasable generations the way the SQL store's
            # `ORDER BY lease_expires_at NULLS FIRST` does -- unheld before
            # expired, oldest expiry first -- then break the tie by whichever
            # generation went longest without a turn.  Without that last term a
            # worker that releases at the end of every turn re-leases the same
            # generation forever, and every other account's periods are never
            # closed: starvation that only appears once two accounts can hold
            # the same delivery_config_id.
            ranked: list[tuple[tuple[int, float, int], ReportingConfigurationGenerationKey]] = []
            for key in self._configurations:
                turn = self._lease_turns.get(key, 0)
                held = self._leases.get(key)
                if held is None:
                    ranked.append(((0, 0.0, turn), key))
                elif _utc(held[1]) <= moment:
                    ranked.append(((1, _utc(held[1]).timestamp(), turn), key))
            if not ranked:
                return None
            # `min` keeps the first of equal ranks, so generations that have
            # never been leased are handed out in the order they were accepted.
            key = min(ranked, key=lambda item: item[0])[1]
            configuration = self._configurations[key]
            expires = moment + timedelta(seconds=lease_seconds)
            self._lease_turn += 1
            self._lease_turns[key] = self._lease_turn
            self._leases[key] = (worker_id, expires)
            return LeasedConfiguration(
                account_id=configuration.account_id,
                delivery_config_id=configuration.delivery_config_id,
                delivery_config_version=configuration.delivery_config_version,
                lease_expires_at=expires,
            )

    async def release_period_close(self, lease: LeasedConfiguration, *, worker_id: str) -> None:
        async with self._lock:
            key = lease.generation_key
            held = self._leases.get(key)
            if held == (worker_id, _utc(lease.lease_expires_at)):
                del self._leases[key]


def reject_reserved_authoritative_party(configuration: ReportingConfiguration) -> None:
    """Refuse ``authoritative_party: consumer`` before the generation is stored.

    AdCP 3.2.0-rc.3 reserves the value for a buyer-deposited billing revision
    task scoped to a later minor. No released minor defines that task, so a
    3.2 seller must reject it with ``UNSUPPORTED_FEATURE`` *before* the
    generation becomes ready and before any obligation exists -- and must not
    silently coerce it to ``seller``.

    Coercing would be the dangerous option: the buyer asked to be the
    authoritative counter for a billing feed and would get a seller-authoritative
    one, with every obligation, revision, and receipt in this ledger quietly
    attributed the wrong way round.
    """
    if configuration.authoritative_party == "consumer":
        raise LedgerConflictError(
            "UNSUPPORTED_FEATURE",
            "authoritative_party 'consumer' is reserved for the buyer-deposited billing "
            "revision task, which no released AdCP minor defines. This seller produces "
            "every revision; omit the field or set it to 'seller'. See "
            "https://github.com/adcontextprotocol/adcp/issues/7440",
        )


def configuration_lifecycle(configuration: ReportingConfiguration) -> tuple[Any, ...]:
    """The rc.3 lifecycle state carried over one immutable content generation.

    ``reporting-delivery-config-state.json`` walks a single generation from
    ``ready`` to ``inactive``, requiring ``deactivated_at`` on the way, and the
    recovery/retention windows are operational state too. That is why none of
    these fields feed ``content_sha256``: a re-put changing only them applies
    to the retained generation instead of conflicting with it. Both stores
    compare exactly this tuple so their status-dirty journals agree.
    """
    return (
        _utc(configuration.activated_at) if configuration.activated_at else None,
        _utc(configuration.deactivated_at) if configuration.deactivated_at else None,
        configuration.automated_recovery_window,
        configuration.status_retention_days,
    )


def _config_payload(configuration: ReportingConfiguration) -> dict[str, Any]:
    schedule = configuration.schedule
    return {
        "account_id": configuration.account_id,
        "report_definition_id": configuration.report_definition_id,
        "reporting_profile": configuration.reporting_profile,
        "feed_purpose": configuration.feed_purpose,
        "required_finality": configuration.required_finality,
        "account_timezone": configuration.account_timezone,
        "authoritative_party": configuration.authoritative_party,
        "media_buy_ids": sorted(configuration.media_buy_ids),
        "definition": configuration.definition.to_storage() if configuration.definition else None,
        "schedule": {
            "period_duration": schedule.period_duration,
            "delivery_sla": schedule.delivery_sla,
            "alignment": schedule.alignment,
            "period_timezone": schedule.period_timezone,
            "period_anchor": (
                _utc(schedule.period_anchor).isoformat() if schedule.period_anchor else None
            ),
        },
    }


def validate_revision_currency(
    obligation: ReportingObligationRecord,
    revision: ReportingRevisionRecord,
    rows: Sequence[dict[str, Any]],
) -> None:
    """Shared write gate; low-level stores enforce the same monetary invariant."""
    currency = require_frozen_currency(obligation.currency)
    definition = obligation.definition
    validate_monetary_content(
        currency=currency,
        rows=rows,
        totals=revision.control_totals,
        metric_units=definition.monetary_metric_units if definition else (),
        total_units=definition.monetary_control_total_units if definition else (),
    )
    if revision.managed_control_totals is not None:
        validate_managed_total_units(obligation, revision.managed_control_totals)


def validate_adjustment_currency(
    obligation: ReportingObligationRecord, adjustment: ReportingAdjustmentRecord
) -> None:
    """Deltas inherit units from the target's obligation; they cannot re-resolve."""
    currency = require_frozen_currency(obligation.currency)
    units = {"spend": currency}
    if obligation.definition is not None:
        units.update(obligation.definition.monetary_metric_units)
        units.update(obligation.definition.monetary_control_total_units)
    validate_currency_units(currency, units.items())
    for name, value in adjustment.control_total_deltas:
        if name in units:
            monetary_decimal(value)
    if adjustment.managed_control_total_deltas is not None:
        validate_managed_total_units(obligation, adjustment.managed_control_total_deltas)


def validate_managed_total_units(
    obligation: ReportingObligationRecord, totals: tuple[ReportingControlTotalRecord, ...]
) -> None:
    units = {"spend": require_frozen_currency(obligation.currency)}
    if obligation.definition is not None:
        units.update(obligation.definition.monetary_metric_units)
        units.update(obligation.definition.monetary_control_total_units)
    for total in totals:
        if total.name in units and total.unit is not None and total.unit != units[total.name]:
            raise LedgerConflictError(
                "CURRENCY_MISMATCH", "control total evidence disagrees with frozen currency"
            )
