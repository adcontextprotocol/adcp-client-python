"""Immutable buyer-side evidence captured from established proposal discovery."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol, TypeAlias, runtime_checkable

import rfc8785

JsonObject: TypeAlias = dict[str, Any]
ESTABLISHED_PROPOSAL_COMPLETION_TOMBSTONE_RETENTION = timedelta(days=7)
ESTABLISHED_PROPOSAL_MAX_SNAPSHOT_BYTES = 256 * 1024
ESTABLISHED_PROPOSAL_MAX_TERMINAL_RESULT_BYTES = 256 * 1024
ESTABLISHED_PROPOSAL_MAX_PRINCIPAL_BYTES = 256


class ProposalEvidenceChangedError(RuntimeError):
    """The evidence changed or disappeared before atomic reservation."""


class ProposalMutationConflictError(RuntimeError):
    """A proposal is already fenced by a different mutation."""


class ProposalStoreCapacityError(RuntimeError):
    """The configured proposal-store record or byte bound would be exceeded."""


class ProposalEvidencePolicyError(ValueError):
    """Proposal evidence is readable but unsafe for executable retention."""


class ProposalAcceptanceState(str, Enum):
    """Durable state of one established proposal-acceptance mutation."""

    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    AMBIGUOUS = "ambiguous"


class ProposalMutationKind(str, Enum):
    """Established proposal operation protected by an atomic reservation."""

    REFINE = "refine"
    DECLINE = "decline"


@dataclass(frozen=True)
class ProposalAcceptanceRecord:
    """Atomic reservation for accepting one proposal exactly once."""

    proposal_id: str
    principal_id: str
    target_binding: str
    account_identity: str
    source_adcp_version: str
    idempotency_key: str
    request_fingerprint: str
    state: ProposalAcceptanceState
    created_at: datetime
    retry_expires_at: datetime | None = None
    seller_task_id: str | None = None
    completed_at: datetime | None = None
    retain_until: datetime | None = None
    reserved_completion_bytes: int = 0
    _request_json: bytes | None = field(default=None, repr=False)
    _source_proposal_json: bytes | None = field(default=None, repr=False)
    _result_json: bytes | None = field(default=None, repr=False)

    @property
    def request(self) -> JsonObject | None:
        """Return the detached reduced request retained for restart recovery."""

        if self._request_json is None:
            return None
        value = json.loads(self._request_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise RuntimeError("stored proposal acceptance request is not an object")
        return value

    @property
    def result(self) -> JsonObject | None:
        """Return an independent copy of a completed serialized TaskResult."""

        if self._result_json is None:
            return None
        value = json.loads(self._result_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise RuntimeError("stored proposal acceptance result is not an object")
        return value


@dataclass(frozen=True)
class ProposalAcceptanceReservation:
    """Result of an atomic acceptance reservation attempt."""

    record: ProposalAcceptanceRecord
    created: bool


@dataclass(frozen=True)
class ProposalMutationRecord:
    """One all-or-none reservation spanning one or more proposal snapshots."""

    operation: ProposalMutationKind
    proposal_ids: tuple[str, ...]
    principal_id: str
    target_binding: str
    account_identity: str
    source_adcp_version: str
    idempotency_key: str
    request_fingerprint: str
    state: ProposalAcceptanceState
    created_at: datetime
    retry_expires_at: datetime | None = None
    seller_task_id: str | None = None
    completed_at: datetime | None = None
    retain_until: datetime | None = None
    reserved_completion_bytes: int = 0
    reserved_completion_records: int = 0
    _request_json: bytes | None = field(default=None, repr=False)
    _source_proposals_json: bytes | None = field(default=None, repr=False)
    _result_json: bytes | None = field(default=None, repr=False)

    @property
    def request(self) -> JsonObject | None:
        """Return the detached reduced request retained for restart recovery."""

        if self._request_json is None:
            return None
        value = json.loads(self._request_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise RuntimeError("stored proposal mutation request is not an object")
        return value

    @property
    def source_proposals(self) -> tuple[JsonObject, ...]:
        """Return store-captured source generations used by this reservation."""

        if self._source_proposals_json is None:
            return ()
        value = json.loads(self._source_proposals_json)
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise RuntimeError("stored proposal mutation sources are not an object array")
        return tuple(value)

    @property
    def result(self) -> JsonObject | None:
        if self._result_json is None:
            return None
        value = json.loads(self._result_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise RuntimeError("stored proposal mutation result is not an object")
        return value


@dataclass(frozen=True)
class ProposalMutationReservation:
    """Result of an atomic multi-proposal mutation reservation."""

    record: ProposalMutationRecord
    created: bool


@dataclass(frozen=True)
class EstablishedProposalEvidence:
    """An immutable proposal observation bound to its authenticated discovery scope."""

    proposal_id: str
    principal_id: str
    target_binding: str
    account_identity: str
    source_adcp_version: str
    request_fingerprint: str
    observed_at: datetime
    _proposal_json: bytes = field(repr=False)

    @classmethod
    def capture(
        cls,
        proposal: Mapping[str, Any],
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
        observed_request: Mapping[str, Any],
        observed_at: datetime,
    ) -> EstablishedProposalEvidence:
        """Copy and canonicalize seller evidence before it crosses a store boundary."""

        proposal_copy = copy.deepcopy(dict(proposal))
        proposal_id = proposal_copy.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ValueError("every retained proposal must carry a non-empty proposal_id")
        try:
            proposal_json = rfc8785.dumps(proposal_copy)
            request_json = rfc8785.dumps(copy.deepcopy(dict(observed_request)))
        except (TypeError, ValueError) as exc:
            raise ValueError("proposal evidence must be JSON-canonicalizable") from exc
        if len(proposal_json) > ESTABLISHED_PROPOSAL_MAX_SNAPSHOT_BYTES:
            raise ProposalEvidencePolicyError(
                "proposal evidence exceeds the 256 KiB executable snapshot limit"
            )
        if len(principal_id.encode("utf-8")) > ESTABLISHED_PROPOSAL_MAX_PRINCIPAL_BYTES:
            raise ProposalEvidencePolicyError("proposal principal scope exceeds the 256-byte limit")
        return cls(
            proposal_id=proposal_id,
            principal_id=principal_id,
            target_binding=target_binding,
            account_identity=account_identity,
            source_adcp_version=source_adcp_version,
            request_fingerprint=hashlib.sha256(request_json).hexdigest(),
            observed_at=observed_at,
            _proposal_json=proposal_json,
        )

    @property
    def proposal(self) -> JsonObject:
        """Return an independent copy of the retained seller proposal."""

        value = json.loads(self._proposal_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise RuntimeError("stored proposal evidence is not an object")
        return value


@runtime_checkable
class EstablishedProposalEvidenceStore(Protocol):
    """Persistence seam for proposal evidence."""

    is_durable: bool

    async def put(self, evidence: EstablishedProposalEvidence) -> None:
        """Store or exactly replace one proposal observation in its bound scope."""

    async def get(
        self,
        proposal_id: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> EstablishedProposalEvidence | None:
        """Read evidence only from the caller's complete authenticated scope."""

    async def delete(
        self,
        proposal_id: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> None:
        """Invalidate older evidence when a same-ID observation is unsafe."""


@runtime_checkable
class EstablishedProposalAcceptanceStore(Protocol):
    """Atomic persistence required before established proposal acceptance."""

    is_durable: bool

    async def reserve_acceptance(
        self,
        evidence: EstablishedProposalEvidence,
        *,
        idempotency_key: str,
        request: Mapping[str, Any],
        created_at: datetime,
        retry_ttl: timedelta | None = None,
    ) -> ProposalAcceptanceReservation:
        """Reserve proposal, replay scope, expiry, and terminal capacity atomically."""

    async def complete_acceptance(
        self,
        record: ProposalAcceptanceRecord,
        result: Mapping[str, Any],
    ) -> ProposalAcceptanceRecord:
        """Persist the terminal result for an in-flight reservation."""

    async def mark_acceptance_ambiguous(
        self,
        record: ProposalAcceptanceRecord,
    ) -> ProposalAcceptanceRecord:
        """Fence a mutation whose remote outcome could not be established."""


@runtime_checkable
class EstablishedProposalAcceptanceRecoveryStore(EstablishedProposalAcceptanceStore, Protocol):
    """Optional durable submitted-task recovery for established acceptance."""

    async def record_acceptance_task(
        self,
        record: ProposalAcceptanceRecord,
        seller_task_id: str,
    ) -> ProposalAcceptanceRecord:
        """Bind one seller task ID to an in-flight acceptance atomically."""

    async def find_acceptance_by_task(
        self,
        seller_task_id: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> ProposalAcceptanceRecord | None:
        """Find an acceptance by seller task in its authenticated scope."""


@runtime_checkable
class EstablishedProposalMutationStore(Protocol):
    """Atomic persistence for established refinement and decline batches."""

    is_durable: bool

    async def find(
        self,
        proposal_ids: Sequence[str],
        *,
        principal_id: str,
        target_binding: str,
        source_adcp_version: str,
    ) -> Sequence[EstablishedProposalEvidence]:
        """Find detached evidence when compact mutation requests omit account."""

    async def reserve_mutation(
        self,
        evidence: Sequence[EstablishedProposalEvidence],
        *,
        operation: ProposalMutationKind,
        idempotency_key: str,
        request: Mapping[str, Any],
        created_at: datetime,
        retry_ttl: timedelta | None = None,
    ) -> ProposalMutationReservation:
        """Atomically compare evidence and reserve proposals plus completion capacity."""

    async def complete_mutation(
        self,
        record: ProposalMutationRecord,
        result: Mapping[str, Any],
        *,
        replacements: Sequence[EstablishedProposalEvidence] = (),
    ) -> ProposalMutationRecord:
        """Persist a terminal result and successor snapshots atomically."""

    async def mark_mutation_ambiguous(
        self,
        record: ProposalMutationRecord,
    ) -> ProposalMutationRecord:
        """Fence every proposal after an uncertain remote outcome."""

    async def record_mutation_task(
        self,
        record: ProposalMutationRecord,
        seller_task_id: str,
    ) -> ProposalMutationRecord:
        """Bind one seller task ID to an in-flight mutation atomically."""

    async def find_mutation_by_task(
        self,
        seller_task_id: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> ProposalMutationRecord | None:
        """Recover an in-flight mutation by authenticated task scope."""


@runtime_checkable
class EstablishedProposalMutationReplayStore(Protocol):
    """Optional scoped lookup for replay before mutable evidence is re-read."""

    is_durable: bool

    async def find_completed_mutation_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> ProposalMutationRecord | None:
        """Find only a completed mutation in its full authenticated scope."""


@runtime_checkable
class EstablishedProposalTombstoneStore(Protocol):
    """Optional SDK-driven sweeper for completed proposal-operation proofs."""

    is_durable: bool

    async def prune_completion_tombstones(self, limit: int = 1000) -> int:
        """Delete completion proofs only after the seven-day retention floor."""


class InMemoryEstablishedProposalEvidenceStore:
    """Process-local evidence store for tests and single-worker development."""

    is_durable = False

    def __init__(
        self,
        *,
        max_records: int = 256,
        max_bytes: int = 4 * 1024 * 1024,
        completion_tombstone_retention: timedelta = (
            ESTABLISHED_PROPOSAL_COMPLETION_TOMBSTONE_RETENTION
        ),
        max_terminal_result_bytes: int = ESTABLISHED_PROPOSAL_MAX_TERMINAL_RESULT_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
            raise ValueError("max_records must be a positive integer")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if (
            isinstance(max_terminal_result_bytes, bool)
            or not isinstance(max_terminal_result_bytes, int)
            or max_terminal_result_bytes <= 0
        ):
            raise ValueError("max_terminal_result_bytes must be a positive integer")
        if completion_tombstone_retention < ESTABLISHED_PROPOSAL_COMPLETION_TOMBSTONE_RETENTION:
            raise ValueError("completion_tombstone_retention must be at least seven days")
        self._lock = asyncio.Lock()
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._max_terminal_result_bytes = max_terminal_result_bytes
        self._completion_tombstone_retention = completion_tombstone_retention
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: dict[tuple[str, str, str, str, str], EstablishedProposalEvidence] = {}
        self._acceptances: dict[tuple[str, str, str, str, str], ProposalAcceptanceRecord] = {}
        self._acceptance_keys: dict[tuple[str, str, str, str, str], str] = {}
        self._acceptance_task_keys: dict[
            tuple[str, str, str, str, str], tuple[str, str, str, str, str]
        ] = {}
        self._mutations: dict[tuple[str, str, str, str, str], ProposalMutationRecord] = {}
        self._mutation_keys: dict[
            tuple[str, str, str, str, str], tuple[str, str, str, str, str]
        ] = {}
        self._mutation_task_keys: dict[
            tuple[str, str, str, str, str], tuple[str, str, str, str, str]
        ] = {}
        self._proposal_fences: dict[tuple[str, str, str, str], tuple[str, ...]] = {}
        self._acceptance_tombstones: dict[tuple[str, str, str, str], str] = {}

    @staticmethod
    def _key(
        proposal_id: str,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> tuple[str, str, str, str, str]:
        return (
            principal_id,
            target_binding,
            account_identity,
            source_adcp_version,
            proposal_id,
        )

    @staticmethod
    def _fence_key(
        proposal_id: str,
        principal_id: str,
        target_binding: str,
        source_adcp_version: str,
    ) -> tuple[str, str, str, str]:
        return principal_id, target_binding, source_adcp_version, proposal_id

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("proposal store clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _evidence_size(value: EstablishedProposalEvidence) -> int:
        return len(value._proposal_json) + sum(
            len(part.encode("utf-8"))
            for part in (
                value.proposal_id,
                value.principal_id,
                value.target_binding,
                value.account_identity,
                value.source_adcp_version,
                value.request_fingerprint,
            )
        )

    @staticmethod
    def _acceptance_size(value: ProposalAcceptanceRecord) -> int:
        return (
            len(value._request_json or b"")
            + len(value._source_proposal_json or b"")
            + len(value._result_json or b"")
            + value.reserved_completion_bytes
            + 256
            + sum(
                len(part.encode("utf-8"))
                for part in (
                    value.proposal_id,
                    value.principal_id,
                    value.target_binding,
                    value.account_identity,
                    value.source_adcp_version,
                    value.idempotency_key,
                    value.request_fingerprint,
                    value.seller_task_id or "",
                )
            )
        )

    @staticmethod
    def _mutation_size(value: ProposalMutationRecord) -> int:
        return (
            len(value._request_json or b"")
            + len(value._source_proposals_json or b"")
            + len(value._result_json or b"")
            + value.reserved_completion_bytes
            + 256
            + sum(
                len(part.encode("utf-8"))
                for part in (
                    *value.proposal_ids,
                    value.principal_id,
                    value.target_binding,
                    value.account_identity,
                    value.source_adcp_version,
                    value.idempotency_key,
                    value.request_fingerprint,
                    value.seller_task_id or "",
                )
            )
        )

    @staticmethod
    def _acceptance_tombstone_size(key: tuple[str, str, str, str], idempotency_key: str) -> int:
        return (
            128
            + len(idempotency_key.encode("utf-8"))
            + sum(len(part.encode("utf-8")) for part in key)
        )

    @staticmethod
    def _reserved_replacement_bytes(evidence: EstablishedProposalEvidence) -> int:
        # The serialized proposal contains its ID, but the evidence index also
        # retains that ID separately. Reserve both maxima plus known scope data.
        return (2 * ESTABLISHED_PROPOSAL_MAX_SNAPSHOT_BYTES) + sum(
            len(part.encode("utf-8"))
            for part in (
                evidence.principal_id,
                evidence.target_binding,
                evidence.account_identity,
                evidence.source_adcp_version,
                evidence.request_fingerprint,
            )
        )

    def _ensure_capacity(
        self,
        *,
        records: Mapping[tuple[str, str, str, str, str], EstablishedProposalEvidence] | None = None,
        acceptances: (
            Mapping[tuple[str, str, str, str, str], ProposalAcceptanceRecord] | None
        ) = None,
        mutations: Mapping[tuple[str, str, str, str, str], ProposalMutationRecord] | None = None,
    ) -> None:
        candidate_records = records if records is not None else self._records
        candidate_acceptances = acceptances if acceptances is not None else self._acceptances
        candidate_mutations = mutations if mutations is not None else self._mutations
        count = len(candidate_records) + len(candidate_acceptances) + len(candidate_mutations)
        count += sum(value.reserved_completion_records for value in candidate_mutations.values())
        count += len(self._acceptance_tombstones)
        size = (
            sum(self._evidence_size(value) for value in candidate_records.values())
            + sum(self._acceptance_size(value) for value in candidate_acceptances.values())
            + sum(self._mutation_size(value) for value in candidate_mutations.values())
            + sum(
                self._acceptance_tombstone_size(key, idempotency_key)
                for key, idempotency_key in self._acceptance_tombstones.items()
            )
        )
        if count > self._max_records or size > self._max_bytes:
            raise ProposalStoreCapacityError(
                "proposal store capacity would be exceeded; prune or increase its bounds"
            )

    def _assert_evidence_unexpired(self, evidence: EstablishedProposalEvidence) -> None:
        """Validate temporal authorization using the store's transaction-time clock."""

        expires_at = evidence.proposal.get("expires_at")
        if expires_at is None:
            return
        if not isinstance(expires_at, str) or not expires_at:
            raise ProposalEvidenceChangedError("proposal evidence has an invalid expires_at")
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProposalEvidenceChangedError(
                "proposal evidence has an invalid expires_at"
            ) from exc
        if parsed.tzinfo is None:
            raise ProposalEvidenceChangedError("proposal evidence has a naive expires_at")
        if parsed.astimezone(timezone.utc) <= self._now():
            raise ProposalEvidenceChangedError("proposal evidence expired before reservation")

    async def put(self, evidence: EstablishedProposalEvidence) -> None:
        key = self._key(
            evidence.proposal_id,
            evidence.principal_id,
            evidence.target_binding,
            evidence.account_identity,
            evidence.source_adcp_version,
        )
        async with self._lock:
            fence_key = self._fence_key(
                evidence.proposal_id,
                evidence.principal_id,
                evidence.target_binding,
                evidence.source_adcp_version,
            )
            if fence_key in self._proposal_fences and self._records.get(key) != evidence:
                raise ProposalMutationConflictError("reserved proposal evidence cannot be replaced")
            records = dict(self._records)
            records[key] = evidence
            self._ensure_capacity(records=records)
            self._records = records

    async def get(
        self,
        proposal_id: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> EstablishedProposalEvidence | None:
        key = self._key(
            proposal_id,
            principal_id,
            target_binding,
            account_identity,
            source_adcp_version,
        )
        async with self._lock:
            return self._records.get(key)

    async def delete(
        self,
        proposal_id: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> None:
        key = self._key(
            proposal_id,
            principal_id,
            target_binding,
            account_identity,
            source_adcp_version,
        )
        async with self._lock:
            fence_key = self._fence_key(
                proposal_id,
                principal_id,
                target_binding,
                source_adcp_version,
            )
            if fence_key in self._proposal_fences:
                raise ProposalMutationConflictError("reserved proposal evidence cannot be deleted")
            self._records.pop(key, None)

    async def find(
        self,
        proposal_ids: Sequence[str],
        *,
        principal_id: str,
        target_binding: str,
        source_adcp_version: str,
    ) -> Sequence[EstablishedProposalEvidence]:
        wanted = set(proposal_ids)
        async with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if record.proposal_id in wanted
                and record.principal_id == principal_id
                and record.target_binding == target_binding
                and record.source_adcp_version == source_adcp_version
            )

    async def reserve_acceptance(
        self,
        evidence: EstablishedProposalEvidence,
        *,
        idempotency_key: str,
        request: Mapping[str, Any],
        created_at: datetime,
        retry_ttl: timedelta | None = None,
    ) -> ProposalAcceptanceReservation:
        if retry_ttl is not None and retry_ttl <= timedelta(0):
            raise ValueError("proposal acceptance retry_ttl must be positive")
        try:
            request_json = rfc8785.dumps(copy.deepcopy(dict(request)))
        except (TypeError, ValueError) as exc:
            raise ValueError("proposal acceptance request must be JSON-canonicalizable") from exc
        fingerprint = hashlib.sha256(request_json).hexdigest()
        proposal_key = self._key(
            evidence.proposal_id,
            evidence.principal_id,
            evidence.target_binding,
            evidence.account_identity,
            evidence.source_adcp_version,
        )
        idempotency_scope = (
            evidence.principal_id,
            evidence.target_binding,
            evidence.account_identity,
            evidence.source_adcp_version,
            idempotency_key,
        )
        async with self._lock:
            if self._records.get(proposal_key) != evidence:
                raise ProposalEvidenceChangedError(
                    "proposal evidence changed before acceptance reservation"
                )
            existing = self._acceptances.get(proposal_key)
            keyed_proposal = self._acceptance_keys.get(idempotency_scope)
            if existing is not None:
                if (
                    existing.state is ProposalAcceptanceState.AMBIGUOUS
                    and existing.seller_task_id is None
                    and existing.idempotency_key == idempotency_key
                    and existing.request_fingerprint == fingerprint
                    and existing.retry_expires_at is not None
                    and existing.retry_expires_at > self._now()
                ):
                    self._assert_evidence_unexpired(evidence)
                    retry = ProposalAcceptanceRecord(
                        proposal_id=existing.proposal_id,
                        principal_id=existing.principal_id,
                        target_binding=existing.target_binding,
                        account_identity=existing.account_identity,
                        source_adcp_version=existing.source_adcp_version,
                        idempotency_key=existing.idempotency_key,
                        request_fingerprint=existing.request_fingerprint,
                        state=ProposalAcceptanceState.IN_FLIGHT,
                        created_at=existing.created_at,
                        retry_expires_at=existing.retry_expires_at,
                        reserved_completion_bytes=existing.reserved_completion_bytes,
                        _request_json=existing._request_json,
                        _source_proposal_json=existing._source_proposal_json,
                    )
                    self._acceptances[proposal_key] = retry
                    return ProposalAcceptanceReservation(record=retry, created=True)
                return ProposalAcceptanceReservation(record=existing, created=False)
            if keyed_proposal is not None:
                keyed_record = self._acceptances[
                    self._key(
                        keyed_proposal,
                        evidence.principal_id,
                        evidence.target_binding,
                        evidence.account_identity,
                        evidence.source_adcp_version,
                    )
                ]
                return ProposalAcceptanceReservation(record=keyed_record, created=False)
            fence_key = self._fence_key(
                evidence.proposal_id,
                evidence.principal_id,
                evidence.target_binding,
                evidence.source_adcp_version,
            )
            if fence_key in self._proposal_fences:
                raise ProposalMutationConflictError(
                    "proposal is already reserved by another mutation"
                )
            self._assert_evidence_unexpired(evidence)
            record = ProposalAcceptanceRecord(
                proposal_id=evidence.proposal_id,
                principal_id=evidence.principal_id,
                target_binding=evidence.target_binding,
                account_identity=evidence.account_identity,
                source_adcp_version=evidence.source_adcp_version,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                state=ProposalAcceptanceState.IN_FLIGHT,
                created_at=created_at,
                retry_expires_at=(self._now() + retry_ttl if retry_ttl is not None else None),
                reserved_completion_bytes=self._max_terminal_result_bytes,
                _request_json=request_json,
                _source_proposal_json=evidence._proposal_json,
            )
            acceptances = dict(self._acceptances)
            acceptances[proposal_key] = record
            self._ensure_capacity(acceptances=acceptances)
            self._acceptances[proposal_key] = record
            self._acceptance_keys[idempotency_scope] = evidence.proposal_id
            self._proposal_fences[fence_key] = ("accept", idempotency_key)
            return ProposalAcceptanceReservation(record=record, created=True)

    async def complete_acceptance(
        self,
        record: ProposalAcceptanceRecord,
        result: Mapping[str, Any],
    ) -> ProposalAcceptanceRecord:
        try:
            result_json = rfc8785.dumps(copy.deepcopy(dict(result)))
        except (TypeError, ValueError) as exc:
            raise ValueError("proposal acceptance result must be JSON-canonicalizable") from exc
        key = self._key(
            record.proposal_id,
            record.principal_id,
            record.target_binding,
            record.account_identity,
            record.source_adcp_version,
        )
        async with self._lock:
            current = self._acceptances.get(key)
            if current != record or current.state is not ProposalAcceptanceState.IN_FLIGHT:
                raise RuntimeError("proposal acceptance reservation changed before completion")
            if len(result_json) > current.reserved_completion_bytes:
                raise ProposalStoreCapacityError(
                    "proposal acceptance result exceeds its reserved completion capacity"
                )
            completed_at = self._now()
            completed = ProposalAcceptanceRecord(
                proposal_id=current.proposal_id,
                principal_id=current.principal_id,
                target_binding=current.target_binding,
                account_identity=current.account_identity,
                source_adcp_version=current.source_adcp_version,
                idempotency_key=current.idempotency_key,
                request_fingerprint=current.request_fingerprint,
                state=ProposalAcceptanceState.COMPLETED,
                created_at=current.created_at,
                retry_expires_at=current.retry_expires_at,
                seller_task_id=current.seller_task_id,
                completed_at=completed_at,
                retain_until=completed_at + self._completion_tombstone_retention,
                _request_json=current._request_json,
                _source_proposal_json=current._source_proposal_json,
                _result_json=result_json,
            )
            acceptances = dict(self._acceptances)
            acceptances[key] = completed
            self._ensure_capacity(acceptances=acceptances)
            self._acceptances[key] = completed
            return completed

    async def mark_acceptance_ambiguous(
        self,
        record: ProposalAcceptanceRecord,
    ) -> ProposalAcceptanceRecord:
        key = self._key(
            record.proposal_id,
            record.principal_id,
            record.target_binding,
            record.account_identity,
            record.source_adcp_version,
        )
        async with self._lock:
            current = self._acceptances.get(key)
            if current != record or current.state is not ProposalAcceptanceState.IN_FLIGHT:
                raise RuntimeError("proposal acceptance reservation changed before fencing")
            ambiguous = ProposalAcceptanceRecord(
                proposal_id=current.proposal_id,
                principal_id=current.principal_id,
                target_binding=current.target_binding,
                account_identity=current.account_identity,
                source_adcp_version=current.source_adcp_version,
                idempotency_key=current.idempotency_key,
                request_fingerprint=current.request_fingerprint,
                state=ProposalAcceptanceState.AMBIGUOUS,
                created_at=current.created_at,
                retry_expires_at=current.retry_expires_at,
                seller_task_id=current.seller_task_id,
                reserved_completion_bytes=current.reserved_completion_bytes,
                _request_json=current._request_json,
                _source_proposal_json=current._source_proposal_json,
            )
            self._acceptances[key] = ambiguous
            return ambiguous

    async def record_acceptance_task(
        self,
        record: ProposalAcceptanceRecord,
        seller_task_id: str,
    ) -> ProposalAcceptanceRecord:
        if not isinstance(seller_task_id, str) or not seller_task_id:
            raise ValueError("seller_task_id must be non-empty")
        key = self._key(
            record.proposal_id,
            record.principal_id,
            record.target_binding,
            record.account_identity,
            record.source_adcp_version,
        )
        task_scope = (
            record.principal_id,
            record.target_binding,
            record.account_identity,
            record.source_adcp_version,
            seller_task_id,
        )
        async with self._lock:
            current = self._acceptances.get(key)
            if current != record or current.state is not ProposalAcceptanceState.IN_FLIGHT:
                raise RuntimeError("proposal acceptance reservation changed before task binding")
            existing_key = self._acceptance_task_keys.get(task_scope)
            if existing_key is not None and existing_key != key:
                raise ProposalMutationConflictError(
                    "seller task ID is already bound to another proposal acceptance"
                )
            if current.seller_task_id not in {None, seller_task_id}:
                raise ProposalMutationConflictError(
                    "proposal acceptance is already bound to another seller task ID"
                )
            if current.seller_task_id == seller_task_id:
                return current
            bound = ProposalAcceptanceRecord(
                proposal_id=current.proposal_id,
                principal_id=current.principal_id,
                target_binding=current.target_binding,
                account_identity=current.account_identity,
                source_adcp_version=current.source_adcp_version,
                idempotency_key=current.idempotency_key,
                request_fingerprint=current.request_fingerprint,
                state=current.state,
                created_at=current.created_at,
                retry_expires_at=current.retry_expires_at,
                seller_task_id=seller_task_id,
                reserved_completion_bytes=current.reserved_completion_bytes,
                _request_json=current._request_json,
                _source_proposal_json=current._source_proposal_json,
            )
            acceptances = dict(self._acceptances)
            acceptances[key] = bound
            self._ensure_capacity(acceptances=acceptances)
            self._acceptances[key] = bound
            self._acceptance_task_keys[task_scope] = key
            return bound

    async def find_acceptance_by_task(
        self,
        seller_task_id: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> ProposalAcceptanceRecord | None:
        task_scope = (
            principal_id,
            target_binding,
            account_identity,
            source_adcp_version,
            seller_task_id,
        )
        async with self._lock:
            key = self._acceptance_task_keys.get(task_scope)
            return self._acceptances.get(key) if key is not None else None

    async def reserve_mutation(
        self,
        evidence: Sequence[EstablishedProposalEvidence],
        *,
        operation: ProposalMutationKind,
        idempotency_key: str,
        request: Mapping[str, Any],
        created_at: datetime,
        retry_ttl: timedelta | None = None,
    ) -> ProposalMutationReservation:
        if retry_ttl is not None and retry_ttl <= timedelta(0):
            raise ValueError("proposal mutation retry_ttl must be positive")
        rows = tuple(evidence)
        if not rows:
            raise ValueError("proposal mutation evidence must not be empty")
        if len({row.proposal_id for row in rows}) != len(rows):
            raise ValueError("proposal mutation evidence contains duplicate proposal IDs")
        first = rows[0]
        scope = (
            first.principal_id,
            first.target_binding,
            first.account_identity,
            first.source_adcp_version,
        )
        if any(
            (
                row.principal_id,
                row.target_binding,
                row.account_identity,
                row.source_adcp_version,
            )
            != scope
            for row in rows
        ):
            raise ValueError("proposal mutation evidence must share one authenticated scope")
        request_json = rfc8785.dumps(copy.deepcopy(dict(request)))
        source_proposals_json = rfc8785.dumps([row.proposal for row in rows])
        fingerprint = hashlib.sha256(request_json).hexdigest()
        mutation_key = (*scope, idempotency_key)
        async with self._lock:
            for row in rows:
                evidence_key = self._key(
                    row.proposal_id,
                    row.principal_id,
                    row.target_binding,
                    row.account_identity,
                    row.source_adcp_version,
                )
                if self._records.get(evidence_key) != row:
                    raise ProposalEvidenceChangedError(
                        "proposal evidence changed before mutation reservation"
                    )
            existing_key = self._mutation_keys.get(mutation_key)
            if existing_key is not None:
                existing = self._mutations[existing_key]
                if (
                    existing.state is ProposalAcceptanceState.AMBIGUOUS
                    and existing.seller_task_id is None
                    and existing.operation is operation
                    and existing.request_fingerprint == fingerprint
                    and existing.retry_expires_at is not None
                    and existing.retry_expires_at > self._now()
                ):
                    for row in rows:
                        self._assert_evidence_unexpired(row)
                    retry = ProposalMutationRecord(
                        operation=existing.operation,
                        proposal_ids=existing.proposal_ids,
                        principal_id=existing.principal_id,
                        target_binding=existing.target_binding,
                        account_identity=existing.account_identity,
                        source_adcp_version=existing.source_adcp_version,
                        idempotency_key=existing.idempotency_key,
                        request_fingerprint=existing.request_fingerprint,
                        state=ProposalAcceptanceState.IN_FLIGHT,
                        created_at=existing.created_at,
                        retry_expires_at=existing.retry_expires_at,
                        reserved_completion_bytes=existing.reserved_completion_bytes,
                        reserved_completion_records=existing.reserved_completion_records,
                        _request_json=existing._request_json,
                        _source_proposals_json=existing._source_proposals_json,
                    )
                    self._mutations[existing_key] = retry
                    return ProposalMutationReservation(record=retry, created=True)
                return ProposalMutationReservation(record=existing, created=False)
            proposal_ids = tuple(row.proposal_id for row in rows)
            record_key = mutation_key
            unindexed = self._mutations.get(record_key)
            if unindexed is not None:
                return ProposalMutationReservation(record=unindexed, created=False)
            for row in rows:
                fence_key = self._fence_key(
                    row.proposal_id,
                    row.principal_id,
                    row.target_binding,
                    row.source_adcp_version,
                )
                if fence_key in self._proposal_fences:
                    raise ProposalMutationConflictError(
                        f"proposal {row.proposal_id} is already reserved by another mutation"
                    )
                self._assert_evidence_unexpired(row)
            reserved_completion_bytes = self._max_terminal_result_bytes + sum(
                self._reserved_replacement_bytes(row) for row in rows
            )
            record = ProposalMutationRecord(
                operation=operation,
                proposal_ids=proposal_ids,
                principal_id=first.principal_id,
                target_binding=first.target_binding,
                account_identity=first.account_identity,
                source_adcp_version=first.source_adcp_version,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                state=ProposalAcceptanceState.IN_FLIGHT,
                created_at=created_at,
                retry_expires_at=(self._now() + retry_ttl if retry_ttl is not None else None),
                reserved_completion_bytes=reserved_completion_bytes,
                reserved_completion_records=len(rows),
                _request_json=request_json,
                _source_proposals_json=source_proposals_json,
            )
            mutations = dict(self._mutations)
            mutations[record_key] = record
            self._ensure_capacity(mutations=mutations)
            self._mutations[record_key] = record
            self._mutation_keys[mutation_key] = record_key
            for proposal_id in proposal_ids:
                self._proposal_fences[
                    self._fence_key(
                        proposal_id,
                        first.principal_id,
                        first.target_binding,
                        first.source_adcp_version,
                    )
                ] = (operation.value, idempotency_key)
            return ProposalMutationReservation(record=record, created=True)

    async def complete_mutation(
        self,
        record: ProposalMutationRecord,
        result: Mapping[str, Any],
        *,
        replacements: Sequence[EstablishedProposalEvidence] = (),
    ) -> ProposalMutationRecord:
        result_json = rfc8785.dumps(copy.deepcopy(dict(result)))
        key = (
            record.principal_id,
            record.target_binding,
            record.account_identity,
            record.source_adcp_version,
            record.idempotency_key,
        )
        async with self._lock:
            current = self._mutations.get(key)
            if current != record or current.state is not ProposalAcceptanceState.IN_FLIGHT:
                raise RuntimeError("proposal mutation reservation changed before completion")
            for replacement in replacements:
                if (
                    replacement.principal_id,
                    replacement.target_binding,
                    replacement.account_identity,
                    replacement.source_adcp_version,
                ) != (
                    record.principal_id,
                    record.target_binding,
                    record.account_identity,
                    record.source_adcp_version,
                ):
                    raise ValueError("replacement proposal escaped the mutation scope")
            records = dict(self._records)
            for replacement in replacements:
                records[
                    self._key(
                        replacement.proposal_id,
                        replacement.principal_id,
                        replacement.target_binding,
                        replacement.account_identity,
                        replacement.source_adcp_version,
                    )
                ] = replacement
            added_records = max(0, len(records) - len(self._records))
            if added_records > current.reserved_completion_records:
                raise ProposalStoreCapacityError(
                    "proposal mutation replacements exceed reserved record capacity"
                )
            current_record_bytes = sum(
                self._evidence_size(value) for value in self._records.values()
            )
            replacement_record_bytes = sum(self._evidence_size(value) for value in records.values())
            completion_bytes = len(result_json) + max(
                0, replacement_record_bytes - current_record_bytes
            )
            if completion_bytes > current.reserved_completion_bytes:
                raise ProposalStoreCapacityError(
                    "proposal mutation result and replacements exceed reserved completion capacity"
                )
            completed_at = self._now()
            completed = ProposalMutationRecord(
                operation=current.operation,
                proposal_ids=current.proposal_ids,
                principal_id=current.principal_id,
                target_binding=current.target_binding,
                account_identity=current.account_identity,
                source_adcp_version=current.source_adcp_version,
                idempotency_key=current.idempotency_key,
                request_fingerprint=current.request_fingerprint,
                state=ProposalAcceptanceState.COMPLETED,
                created_at=current.created_at,
                retry_expires_at=current.retry_expires_at,
                seller_task_id=current.seller_task_id,
                completed_at=completed_at,
                retain_until=completed_at + self._completion_tombstone_retention,
                _request_json=current._request_json,
                _source_proposals_json=current._source_proposals_json,
                _result_json=result_json,
            )
            mutations = dict(self._mutations)
            mutations[key] = completed
            self._ensure_capacity(records=records, mutations=mutations)
            self._mutations[key] = completed
            self._records = records
            return completed

    async def mark_mutation_ambiguous(
        self,
        record: ProposalMutationRecord,
    ) -> ProposalMutationRecord:
        key = (
            record.principal_id,
            record.target_binding,
            record.account_identity,
            record.source_adcp_version,
            record.idempotency_key,
        )
        async with self._lock:
            current = self._mutations.get(key)
            if current != record or current.state is not ProposalAcceptanceState.IN_FLIGHT:
                raise RuntimeError("proposal mutation reservation changed before fencing")
            ambiguous = ProposalMutationRecord(
                operation=current.operation,
                proposal_ids=current.proposal_ids,
                principal_id=current.principal_id,
                target_binding=current.target_binding,
                account_identity=current.account_identity,
                source_adcp_version=current.source_adcp_version,
                idempotency_key=current.idempotency_key,
                request_fingerprint=current.request_fingerprint,
                state=ProposalAcceptanceState.AMBIGUOUS,
                created_at=current.created_at,
                retry_expires_at=current.retry_expires_at,
                seller_task_id=current.seller_task_id,
                reserved_completion_bytes=current.reserved_completion_bytes,
                reserved_completion_records=current.reserved_completion_records,
                _request_json=current._request_json,
                _source_proposals_json=current._source_proposals_json,
            )
            self._mutations[key] = ambiguous
            return ambiguous

    async def record_mutation_task(
        self,
        record: ProposalMutationRecord,
        seller_task_id: str,
    ) -> ProposalMutationRecord:
        if not isinstance(seller_task_id, str) or not seller_task_id:
            raise ValueError("seller_task_id must be non-empty")
        key = (
            record.principal_id,
            record.target_binding,
            record.account_identity,
            record.source_adcp_version,
            record.idempotency_key,
        )
        task_scope = (
            record.principal_id,
            record.target_binding,
            record.account_identity,
            record.source_adcp_version,
            seller_task_id,
        )
        async with self._lock:
            current = self._mutations.get(key)
            if current != record or current.state is not ProposalAcceptanceState.IN_FLIGHT:
                raise RuntimeError("proposal mutation reservation changed before task binding")
            existing_key = self._mutation_task_keys.get(task_scope)
            if existing_key is not None and existing_key != key:
                raise ProposalMutationConflictError(
                    "seller task ID is already bound to another proposal mutation"
                )
            if current.seller_task_id is not None and current.seller_task_id != seller_task_id:
                raise ProposalMutationConflictError(
                    "proposal mutation is already bound to another seller task ID"
                )
            if current.seller_task_id == seller_task_id:
                return current
            bound = ProposalMutationRecord(
                operation=current.operation,
                proposal_ids=current.proposal_ids,
                principal_id=current.principal_id,
                target_binding=current.target_binding,
                account_identity=current.account_identity,
                source_adcp_version=current.source_adcp_version,
                idempotency_key=current.idempotency_key,
                request_fingerprint=current.request_fingerprint,
                state=current.state,
                created_at=current.created_at,
                retry_expires_at=current.retry_expires_at,
                seller_task_id=seller_task_id,
                reserved_completion_bytes=current.reserved_completion_bytes,
                reserved_completion_records=current.reserved_completion_records,
                _request_json=current._request_json,
                _source_proposals_json=current._source_proposals_json,
            )
            mutations = dict(self._mutations)
            mutations[key] = bound
            self._ensure_capacity(mutations=mutations)
            self._mutations[key] = bound
            self._mutation_task_keys[task_scope] = key
            return bound

    async def find_mutation_by_task(
        self,
        seller_task_id: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> ProposalMutationRecord | None:
        task_scope = (
            principal_id,
            target_binding,
            account_identity,
            source_adcp_version,
            seller_task_id,
        )
        async with self._lock:
            key = self._mutation_task_keys.get(task_scope)
            return self._mutations.get(key) if key is not None else None

    async def find_completed_mutation_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        principal_id: str,
        target_binding: str,
        account_identity: str,
        source_adcp_version: str,
    ) -> ProposalMutationRecord | None:
        key = (
            principal_id,
            target_binding,
            account_identity,
            source_adcp_version,
            idempotency_key,
        )
        async with self._lock:
            record_key = self._mutation_keys.get(key)
            if record_key is None:
                return None
            record = self._mutations.get(record_key)
            if record is None or record.state is not ProposalAcceptanceState.COMPLETED:
                return None
            return record

    async def prune_completion_tombstones(self, limit: int = 1000) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("prune limit must be a positive integer")
        async with self._lock:
            now = self._now()
            pruned = 0
            for key, record in tuple(self._acceptances.items()):
                if pruned >= limit:
                    break
                if (
                    record.state is not ProposalAcceptanceState.COMPLETED
                    or record.retain_until is None
                    or record.retain_until > now
                ):
                    continue
                result = record.result or {}
                fence_key = self._fence_key(
                    record.proposal_id,
                    record.principal_id,
                    record.target_binding,
                    record.source_adcp_version,
                )
                fence = ("accept", record.idempotency_key)
                if result.get("success") is True:
                    evidence = self._records.get(key)
                    if (
                        evidence is not None
                        and evidence._proposal_json == record._source_proposal_json
                    ):
                        self._records.pop(key, None)
                    # A successful acceptance consumes the proposal forever. Keep
                    # only this compact fence after the replay result ages out.
                    self._proposal_fences[fence_key] = fence
                    self._acceptance_tombstones[fence_key] = record.idempotency_key
                elif self._proposal_fences.get(fence_key) == fence:
                    self._proposal_fences.pop(fence_key, None)
                self._acceptance_keys.pop(
                    (
                        record.principal_id,
                        record.target_binding,
                        record.account_identity,
                        record.source_adcp_version,
                        record.idempotency_key,
                    ),
                    None,
                )
                if record.seller_task_id is not None:
                    self._acceptance_task_keys.pop(
                        (
                            record.principal_id,
                            record.target_binding,
                            record.account_identity,
                            record.source_adcp_version,
                            record.seller_task_id,
                        ),
                        None,
                    )
                self._acceptances.pop(key, None)
                pruned += 1
            for key, mutation_record in tuple(self._mutations.items()):
                if pruned >= limit:
                    break
                if (
                    mutation_record.state is not ProposalAcceptanceState.COMPLETED
                    or mutation_record.retain_until is None
                    or mutation_record.retain_until > now
                ):
                    continue
                result = mutation_record.result or {}
                source_rows = mutation_record.source_proposals
                if result.get("success") is True:
                    for proposal_id, source in zip(
                        mutation_record.proposal_ids, source_rows, strict=False
                    ):
                        evidence_key = self._key(
                            proposal_id,
                            mutation_record.principal_id,
                            mutation_record.target_binding,
                            mutation_record.account_identity,
                            mutation_record.source_adcp_version,
                        )
                        current = self._records.get(evidence_key)
                        if current is not None and current.proposal == source:
                            self._records.pop(evidence_key, None)
                for proposal_id in mutation_record.proposal_ids:
                    fence_key = self._fence_key(
                        proposal_id,
                        mutation_record.principal_id,
                        mutation_record.target_binding,
                        mutation_record.source_adcp_version,
                    )
                    if self._proposal_fences.get(fence_key) == (
                        mutation_record.operation.value,
                        mutation_record.idempotency_key,
                    ):
                        self._proposal_fences.pop(fence_key, None)
                if mutation_record.seller_task_id is not None:
                    self._mutation_task_keys.pop(
                        (
                            mutation_record.principal_id,
                            mutation_record.target_binding,
                            mutation_record.account_identity,
                            mutation_record.source_adcp_version,
                            mutation_record.seller_task_id,
                        ),
                        None,
                    )
                self._mutation_keys.pop(key, None)
                self._mutations.pop(key, None)
                pruned += 1
            return pruned


__all__ = [
    "ESTABLISHED_PROPOSAL_COMPLETION_TOMBSTONE_RETENTION",
    "ESTABLISHED_PROPOSAL_MAX_PRINCIPAL_BYTES",
    "ESTABLISHED_PROPOSAL_MAX_SNAPSHOT_BYTES",
    "ESTABLISHED_PROPOSAL_MAX_TERMINAL_RESULT_BYTES",
    "EstablishedProposalAcceptanceStore",
    "EstablishedProposalAcceptanceRecoveryStore",
    "EstablishedProposalEvidence",
    "EstablishedProposalEvidenceStore",
    "EstablishedProposalMutationStore",
    "EstablishedProposalMutationReplayStore",
    "EstablishedProposalTombstoneStore",
    "InMemoryEstablishedProposalEvidenceStore",
    "ProposalAcceptanceRecord",
    "ProposalAcceptanceReservation",
    "ProposalAcceptanceState",
    "ProposalEvidenceChangedError",
    "ProposalEvidencePolicyError",
    "ProposalMutationConflictError",
    "ProposalMutationKind",
    "ProposalMutationRecord",
    "ProposalMutationReservation",
    "ProposalStoreCapacityError",
]
