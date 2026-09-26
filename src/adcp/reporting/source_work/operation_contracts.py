"""Private, descriptive values for supplied source-operation evidence.

These records validate shape and internal binding only. They create no permit,
verify no database, grant, hash, absence, transaction status or retained history,
and perform no I/O, clock sampling, serialization or durable-success promotion.
In particular, a frozen witness does not establish that its evidence was retained
before mutation or will survive restart. That is the future transaction owner's
responsibility. SQL validation and participant results remain tentative.

Prepared inputs precede checkout. A recipe's final status plan/time is determined
inside the owned transaction and bound into a distinct final intent before the
first certificate/business mutation. Publication time, status time and generated
capture times have separate fields and meanings. No physical-COMMIT clock or
exactly-once provider guarantee follows from any record.

Identifiers are bounded Unicode scalar strings (255 characters, at most 1,020
UTF-8 bytes); SHA256 references are exactly 64 lowercase hex characters. Counters
are nonnegative signed-64-bit integers unless stated otherwise. Full xid8 values
are unsigned-64-bit integers, excluding reserved values 0..2. No counter accepts
bool or float. Times retain microsecond precision, are supplied as aware datetime
values with a stdlib fixed offset or ZoneInfo, and are detached to UTC. No custom
tzinfo object is retained. Tuples are exact tuples of exact record types; lists
and subclasses are rejected instead of implicitly consuming mutable input.

The two-capture ceiling is the selected first reference's descriptive bound,
not a new limit on independent low-level APIs. There are at most six predecessor
slots, one per closed kind. No deployment authorization/retention default exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TypeAlias
from uuid import UUID
from zoneinfo import ZoneInfo

MAX_COUNTER = (1 << 63) - 1
MAX_XID8 = (1 << 64) - 1
MAX_IDENTIFIER_CHARACTERS = 255
MAX_CAPTURE_REFERENCES = 2
MAX_SEAL_MANIFEST_BYTES = 1_048_576


class OperationRecordError(ValueError):
    """A closed diagnostic; no supplied field or evidence is formatted."""

    def __init__(self) -> None:
        super().__init__("INVALID_SOURCE_OPERATION_RECORD")


class _Record:
    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


def _require(condition: bool) -> None:
    if not condition:
        raise OperationRecordError()


def _exact(value: object, expected: type[object]) -> None:
    _require(type(value) is expected)


def _identifier(value: str) -> None:
    _exact(value, str)
    _require(1 <= len(value) <= MAX_IDENTIFIER_CHARACTERS)
    _require(all(char != "\x00" and not 0xD800 <= ord(char) <= 0xDFFF for char in value))


def _digest(value: str) -> None:
    _exact(value, str)
    _require(len(value) == 64 and all(char in "0123456789abcdef" for char in value))


def _execution_key(value: str) -> None:
    _identifier(value)
    _require(8 <= len(value) <= 255)
    _require(
        all(
            char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"
            for char in value
        )
    )


def _uuid(value: UUID) -> None:
    _exact(value, UUID)
    _require(value.int != 0)


def _counter(value: int, *, minimum: int = 0, maximum: int = MAX_COUNTER) -> None:
    _exact(value, int)
    _require(minimum <= value <= maximum)


def _utc(value: datetime) -> datetime:
    _exact(value, datetime)
    _require(type(value.tzinfo) in (timezone, ZoneInfo))
    result: datetime | None
    try:
        result = value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        result = None
    if result is None:
        raise OperationRecordError()
    return result


class OperationFamily(str, Enum):
    CERTIFICATE_EFFECT = "certificate_effect"
    OPERATIONAL_MUTATION = "operational_mutation"
    READ_ONLY_WINNER = "read_only_winner"


class PreGrantPhase(str, Enum):
    PERIOD_ROUTE = "period_route"
    RESERVE_ENVELOPE = "reserve_envelope"
    HISTORICAL_ENROLLMENT = "historical_enrollment"


class GrantBoundPhase(str, Enum):
    SEAL = "seal"
    ORDINARY_PUBLICATION = "ordinary_publication"
    PROVISIONAL_PUBLICATION = "provisional_publication"
    SEMANTIC_FINISH = "semantic_finish"
    RENEW = "renew"
    DISPATCH = "dispatch"
    RETRY = "retry"
    RELEASE = "release"


class ControlPhase(str, Enum):
    CLAIM = "claim"
    AUTHORIZATION = "authorization"
    TIMER = "timer"
    RANK = "rank"
    ENROLLMENT_CONTROL = "enrollment_control"
    AUTHORIZATION_WAKE = "authorization_wake"


class ReadPhase(str, Enum):
    ROUTE_WINNER = "route_winner"
    RESERVATION_WINNER = "reservation_winner"
    SEAL_WINNER = "seal_winner"
    PUBLICATION_WINNER = "publication_winner"


OperationPhaseV1: TypeAlias = PreGrantPhase | GrantBoundPhase | ControlPhase | ReadPhase


class RecipeKind(str, Enum):
    IDENTITY_ONLY = "identity_only"
    STATUS = "status"
    PUBLICATION = "publication"
    PUBLICATION_AND_STATUS = "publication_and_status"


class PredecessorKind(str, Enum):
    ADMISSION = "admission"
    AUTHORIZATION = "authorization"
    COMPANION = "companion"
    RANGE = "range"
    CONTROL = "control"
    ATTEMPT = "attempt"


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class InstallationBindingV1(_Record):
    """Opaque supplied identities; no database/restore identity is derived here."""

    installation_id: UUID
    incarnation_id: UUID
    database_binding_sha256: str
    schema_binding_sha256: str
    graph_sha256: str

    def __post_init__(self) -> None:
        _uuid(self.installation_id)
        _uuid(self.incarnation_id)
        _digest(self.database_binding_sha256)
        _digest(self.schema_binding_sha256)
        _digest(self.graph_sha256)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class AccountTargetV1(_Record):
    account_id: str
    configuration_id: str
    configuration_version: int
    generation_binding_sha256: str
    target_id: str
    obligation_id: str | None = None
    source_execution_key: str | None = None

    def __post_init__(self) -> None:
        for value in (self.account_id, self.configuration_id, self.target_id):
            _identifier(value)
        _counter(self.configuration_version, minimum=1)
        _digest(self.generation_binding_sha256)
        if self.obligation_id is not None:
            _identifier(self.obligation_id)
        if self.source_execution_key is not None:
            _execution_key(self.source_execution_key)
            _require(self.obligation_id is not None)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class TimerTargetV1(_Record):
    """Installation-qualified control partition; no invented account lock owner."""

    partition_id: str

    def __post_init__(self) -> None:
        _identifier(self.partition_id)


OperationTargetV1: TypeAlias = AccountTargetV1 | TimerTargetV1


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class ImmutableReferenceV1(_Record):
    record_id: UUID
    sha256: str

    def __post_init__(self) -> None:
        _uuid(self.record_id)
        _digest(self.sha256)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class PredecessorV1(_Record):
    """An expected versioned slot, including explicit expected absence.

    For absence, reference/version/authorization_epoch are all None. state_sha256
    still binds the supplied expected state. This describes a CAS input; it is
    not independent evidence that an earlier operation never committed.
    """

    kind: PredecessorKind
    state_sha256: str
    reference: ImmutableReferenceV1 | None
    version: int | None
    authorization_epoch: int | None = None

    def __post_init__(self) -> None:
        _exact(self.kind, PredecessorKind)
        _digest(self.state_sha256)
        _require((self.reference is None) == (self.version is None))
        if self.reference is not None:
            _exact(self.reference, ImmutableReferenceV1)
        if self.version is not None:
            _counter(self.version)
        if self.kind is PredecessorKind.AUTHORIZATION and self.reference is not None:
            _require(self.authorization_epoch is not None)
            if self.authorization_epoch is not None:
                _counter(self.authorization_epoch, minimum=1)
        else:
            _require(self.authorization_epoch is None)


def _predecessors(values: tuple[PredecessorV1, ...]) -> None:
    _exact(values, tuple)
    _require(len(values) <= len(PredecessorKind))
    for value in values:
        _exact(value, PredecessorV1)
    _require(len({value.kind for value in values}) == len(values))
    _require(tuple(sorted(values, key=lambda value: value.kind.value)) == values)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class GrantEvidenceV1(_Record):
    """An exact supplied reference to the one grant family, never a live lease."""

    attempt_id: UUID
    owner_id: UUID
    grant_id: UUID
    fence: int
    renewal_sequence: int
    expires_at: datetime
    authorization_epoch: int
    authorization_version: int
    companion_version: int

    def __post_init__(self) -> None:
        for value in (self.attempt_id, self.owner_id, self.grant_id):
            _uuid(value)
        _counter(self.fence, minimum=1)
        _counter(self.renewal_sequence)
        _counter(self.authorization_epoch, minimum=1)
        _counter(self.authorization_version)
        _counter(self.companion_version)
        object.__setattr__(self, "expires_at", _utc(self.expires_at))


_PUBLICATIONS = frozenset(
    (GrantBoundPhase.ORDINARY_PUBLICATION, GrantBoundPhase.PROVISIONAL_PUBLICATION)
)
_CERTIFICATE_GRANT_PHASES = _PUBLICATIONS | frozenset(
    (GrantBoundPhase.SEAL, GrantBoundPhase.SEMANTIC_FINISH)
)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class OperationIdentityV1(_Record):
    operation_id: UUID
    installation: InstallationBindingV1
    target: OperationTargetV1
    family: OperationFamily
    phase: OperationPhaseV1
    recipe: RecipeKind
    recipe_sha256: str

    def __post_init__(self) -> None:
        _uuid(self.operation_id)
        _exact(self.installation, InstallationBindingV1)
        _exact(self.family, OperationFamily)
        _exact(self.recipe, RecipeKind)
        _digest(self.recipe_sha256)
        _require(type(self.phase) in (PreGrantPhase, GrantBoundPhase, ControlPhase, ReadPhase))
        if isinstance(self.phase, PreGrantPhase) or self.phase in _CERTIFICATE_GRANT_PHASES:
            expected_family = OperationFamily.CERTIFICATE_EFFECT
        elif isinstance(self.phase, ReadPhase):
            expected_family = OperationFamily.READ_ONLY_WINNER
        else:
            expected_family = OperationFamily.OPERATIONAL_MUTATION
        _require(self.family is expected_family)
        if self.phase is ControlPhase.TIMER:
            _exact(self.target, TimerTargetV1)
        else:
            _exact(self.target, AccountTargetV1)
            if isinstance(self.target, AccountTargetV1):
                acquisition = (
                    isinstance(self.phase, GrantBoundPhase)
                    or self.phase in (PreGrantPhase.RESERVE_ENVELOPE, ControlPhase.CLAIM)
                    or self.phase
                    in (
                        ReadPhase.RESERVATION_WINNER,
                        ReadPhase.SEAL_WINNER,
                        ReadPhase.PUBLICATION_WINNER,
                    )
                )
                obligation = acquisition or self.phase in (
                    PreGrantPhase.PERIOD_ROUTE,
                    ReadPhase.ROUTE_WINNER,
                )
                _require((self.target.obligation_id is not None) == obligation)
                _require((self.target.source_execution_key is not None) == acquisition)
        publication = self.phase in _PUBLICATIONS
        _require(
            (self.recipe in (RecipeKind.PUBLICATION, RecipeKind.PUBLICATION_AND_STATUS))
            == publication
        )
        if self.recipe is RecipeKind.STATUS:
            _require(
                self.phase
                in (
                    PreGrantPhase.PERIOD_ROUTE,
                    PreGrantPhase.HISTORICAL_ENROLLMENT,
                    GrantBoundPhase.SEMANTIC_FINISH,
                )
            )


def _required_predecessors(phase: OperationPhaseV1) -> frozenset[PredecessorKind]:
    if isinstance(phase, ReadPhase):
        return frozenset()
    if isinstance(phase, GrantBoundPhase):
        return frozenset(
            (PredecessorKind.ATTEMPT, PredecessorKind.AUTHORIZATION, PredecessorKind.COMPANION)
        )
    if phase is PreGrantPhase.HISTORICAL_ENROLLMENT:
        return frozenset(
            (PredecessorKind.ADMISSION, PredecessorKind.AUTHORIZATION, PredecessorKind.RANGE)
        )
    if isinstance(phase, PreGrantPhase):
        return frozenset((PredecessorKind.ADMISSION, PredecessorKind.AUTHORIZATION))
    if phase is ControlPhase.CLAIM:
        return frozenset((PredecessorKind.AUTHORIZATION, PredecessorKind.COMPANION))
    if phase is ControlPhase.AUTHORIZATION:
        return frozenset((PredecessorKind.AUTHORIZATION, PredecessorKind.CONTROL))
    if phase is ControlPhase.ENROLLMENT_CONTROL:
        return frozenset((PredecessorKind.CONTROL, PredecessorKind.RANGE))
    return frozenset((PredecessorKind.CONTROL,))


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class PreparedOperationInputsV1(_Record):
    """Detached input identity; contains no finalized status time or plan."""

    identity: OperationIdentityV1
    inputs_sha256: str
    predecessors: tuple[PredecessorV1, ...]
    grant: GrantEvidenceV1 | None = None

    def __post_init__(self) -> None:
        _exact(self.identity, OperationIdentityV1)
        _digest(self.inputs_sha256)
        _predecessors(self.predecessors)
        _require(
            frozenset(value.kind for value in self.predecessors)
            == _required_predecessors(self.identity.phase)
        )
        bound = isinstance(self.identity.phase, GrantBoundPhase)
        _require((self.grant is not None) == bound)
        if self.grant is not None:
            _exact(self.grant, GrantEvidenceV1)
            slots = {value.kind: value for value in self.predecessors}
            attempt = slots[PredecessorKind.ATTEMPT]
            authorization = slots[PredecessorKind.AUTHORIZATION]
            companion = slots[PredecessorKind.COMPANION]
            _require(attempt.reference is not None)
            if attempt.reference is not None:
                _require(attempt.reference.record_id == self.grant.attempt_id)
            _require(authorization.authorization_epoch == self.grant.authorization_epoch)
            _require(authorization.version == self.grant.authorization_version)
            _require(companion.version == self.grant.companion_version)
        if (
            isinstance(self.identity.phase, PreGrantPhase)
            or self.identity.phase is ControlPhase.CLAIM
        ):
            _require(all(value.reference is not None for value in self.predecessors))


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class StatusPlanV1(_Record):
    """Owner-finalized plan identity; evaluates no lifecycle algorithm."""

    t_status: datetime
    input_stamp: int
    dependency_inputs_sha256: str
    plan_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "t_status", _utc(self.t_status))
        _counter(self.input_stamp)
        _digest(self.dependency_inputs_sha256)
        _digest(self.plan_sha256)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class FinalOperationIntentV1(_Record):
    prepared: PreparedOperationInputsV1
    finalized_intent_sha256: str
    t_pub: datetime | None = None
    status_plan: StatusPlanV1 | None = None

    def __post_init__(self) -> None:
        _exact(self.prepared, PreparedOperationInputsV1)
        _digest(self.finalized_intent_sha256)
        recipe = self.prepared.identity.recipe
        _require(
            (self.t_pub is not None)
            == (recipe in (RecipeKind.PUBLICATION, RecipeKind.PUBLICATION_AND_STATUS))
        )
        _require(
            (self.status_plan is not None)
            == (recipe in (RecipeKind.STATUS, RecipeKind.PUBLICATION_AND_STATUS))
        )
        if self.t_pub is not None:
            object.__setattr__(self, "t_pub", _utc(self.t_pub))
        if self.status_plan is not None:
            _exact(self.status_plan, StatusPlanV1)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class TransactionWitnessV1(_Record):
    """Supplied pre-mutation top-level xid8, not a transaction-status proof."""

    installation: InstallationBindingV1
    full_xid8: int

    def __post_init__(self) -> None:
        _exact(self.installation, InstallationBindingV1)
        _counter(self.full_xid8, minimum=3, maximum=MAX_XID8)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class OperationIntentWitnessV1(_Record):
    intent: FinalOperationIntentV1
    transaction: TransactionWitnessV1

    def __post_init__(self) -> None:
        _exact(self.intent, FinalOperationIntentV1)
        _exact(self.transaction, TransactionWitnessV1)
        _require(self.intent.prepared.identity.installation == self.transaction.installation)


OperationEvidenceV1: TypeAlias = (
    OperationIdentityV1
    | PreparedOperationInputsV1
    | FinalOperationIntentV1
    | OperationIntentWitnessV1
)


def _identity(evidence: OperationEvidenceV1) -> OperationIdentityV1:
    _require(
        type(evidence)
        in (
            OperationIdentityV1,
            PreparedOperationInputsV1,
            FinalOperationIntentV1,
            OperationIntentWitnessV1,
        )
    )
    if isinstance(evidence, OperationIntentWitnessV1):
        return evidence.intent.prepared.identity
    if isinstance(evidence, FinalOperationIntentV1):
        return evidence.prepared.identity
    if isinstance(evidence, PreparedOperationInputsV1):
        return evidence.identity
    return evidence


def _available_transaction(
    evidence: OperationEvidenceV1, transaction: TransactionWitnessV1 | None
) -> None:
    identity = _identity(evidence)
    if transaction is not None:
        _exact(transaction, TransactionWitnessV1)
        _require(transaction.installation == identity.installation)
        if isinstance(evidence, OperationIntentWitnessV1):
            _require(transaction == evidence.transaction)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class CaptureReferenceV1(_Record):
    reference: ImmutableReferenceV1
    captured_at: datetime

    def __post_init__(self) -> None:
        _exact(self.reference, ImmutableReferenceV1)
        object.__setattr__(self, "captured_at", _utc(self.captured_at))


def _captures(values: tuple[CaptureReferenceV1, ...]) -> None:
    _exact(values, tuple)
    _require(len(values) <= MAX_CAPTURE_REFERENCES)
    for value in values:
        _exact(value, CaptureReferenceV1)
    _require(len({value.reference.record_id for value in values}) == len(values))


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class NeutralSealReferenceV1(_Record):
    """Detached reference data only; no object existence or A2 provenance claim.

    manifest_sha256 names exact manifest bytes, not the content-fingerprint
    component stored in the existing revision source_manifest_sha256 column.
    No mutable SealedSlice/Pydantic model or manifest payload is retained here.
    """

    account_id: str
    source_execution_key: str
    staged_commit_ref: str
    manifest_sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _identifier(self.account_id)
        _execution_key(self.source_execution_key)
        _identifier(self.staged_commit_ref)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        _require(self.staged_commit_ref[0] in alphabet)
        _require(all(char in alphabet + "_.-" for char in self.staged_commit_ref))
        _digest(self.manifest_sha256)
        _counter(self.byte_count, minimum=1, maximum=MAX_SEAL_MANIFEST_BYTES)


def _seal_target(seal: NeutralSealReferenceV1, identity: OperationIdentityV1) -> None:
    _exact(seal, NeutralSealReferenceV1)
    _require(isinstance(identity.target, AccountTargetV1))
    if isinstance(identity.target, AccountTargetV1):
        _require(seal.account_id == identity.target.account_id)
        _require(seal.source_execution_key == identity.target.source_execution_key)


class TentativeStage(str, Enum):
    PARTICIPANT_RETURNED = "participant_returned"
    SQL_VALIDATED = "sql_validated"


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class TentativeOperationResultV1(_Record):
    witness: OperationIntentWitnessV1
    result: ImmutableReferenceV1
    stage: TentativeStage
    seal: NeutralSealReferenceV1 | None = None

    def __post_init__(self) -> None:
        _exact(self.witness, OperationIntentWitnessV1)
        _exact(self.result, ImmutableReferenceV1)
        _exact(self.stage, TentativeStage)
        if self.seal is not None:
            identity = self.witness.intent.prepared.identity
            _require(identity.phase is GrantBoundPhase.SEAL)
            _seal_target(self.seal, identity)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class CertificateCompletionV1(_Record):
    """Supplied settled completion references, distinct from a tentative result."""

    witness: OperationIntentWitnessV1
    certificate: ImmutableReferenceV1
    result: ImmutableReferenceV1
    actual_effect_sha256: str
    captures: tuple[CaptureReferenceV1, ...] = ()

    def __post_init__(self) -> None:
        _exact(self.witness, OperationIntentWitnessV1)
        _require(self.witness.intent.prepared.identity.family is OperationFamily.CERTIFICATE_EFFECT)
        _exact(self.certificate, ImmutableReferenceV1)
        _exact(self.result, ImmutableReferenceV1)
        _digest(self.actual_effect_sha256)
        _captures(self.captures)


class CasDisposition(str, Enum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class OperationalCompletionV1(_Record):
    """An exact operation record, including recorded CAS rejection if applicable.

    Output versions do not by themselves prove the requested CAS succeeded.
    Their actual transition rules and result retention belong to the owner.
    """

    witness: OperationIntentWitnessV1
    operation_record: ImmutableReferenceV1
    result: ImmutableReferenceV1
    input_predecessors: tuple[PredecessorV1, ...]
    output_predecessors: tuple[PredecessorV1, ...]
    disposition: CasDisposition

    def __post_init__(self) -> None:
        _exact(self.witness, OperationIntentWitnessV1)
        prepared = self.witness.intent.prepared
        _require(prepared.identity.family is OperationFamily.OPERATIONAL_MUTATION)
        _exact(self.operation_record, ImmutableReferenceV1)
        _exact(self.result, ImmutableReferenceV1)
        _predecessors(self.input_predecessors)
        _predecessors(self.output_predecessors)
        _require(self.input_predecessors == prepared.predecessors)
        _require(
            tuple(value.kind for value in self.output_predecessors)
            == tuple(value.kind for value in self.input_predecessors)
        )
        _exact(self.disposition, CasDisposition)


CompletionEvidenceV1: TypeAlias = CertificateCompletionV1 | OperationalCompletionV1


def _completion(value: CompletionEvidenceV1) -> None:
    _require(type(value) in (CertificateCompletionV1, OperationalCompletionV1))


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class ExactOperationConfirmationV1(_Record):
    """Describes matching supplied evidence; constructing it verifies no commit."""

    expected: OperationIntentWitnessV1
    completion: CompletionEvidenceV1

    def __post_init__(self) -> None:
        _exact(self.expected, OperationIntentWitnessV1)
        _completion(self.completion)
        _require(self.expected == self.completion.witness)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class ObservedOtherCompletionV1(_Record):
    requested: PreparedOperationInputsV1
    completion: CompletionEvidenceV1

    def __post_init__(self) -> None:
        _exact(self.requested, PreparedOperationInputsV1)
        _completion(self.completion)
        requested = self.requested.identity
        observed = self.completion.witness.intent.prepared.identity
        _require(requested.operation_id != observed.operation_id)
        _require(requested.installation == observed.installation)
        _require(requested.target == observed.target)
        _require(requested.family is observed.family and requested.phase is observed.phase)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class ReadOnlyWinnerObservationV1(_Record):
    """Observes retained data; attributes neither creation nor dispatch authority."""

    requested: PreparedOperationInputsV1
    winner: ImmutableReferenceV1
    seal: NeutralSealReferenceV1 | None = None

    def __post_init__(self) -> None:
        _exact(self.requested, PreparedOperationInputsV1)
        identity = self.requested.identity
        _require(identity.family is OperationFamily.READ_ONLY_WINNER)
        _exact(self.winner, ImmutableReferenceV1)
        _require((self.seal is not None) == (identity.phase is ReadPhase.SEAL_WINNER))
        if self.seal is not None:
            _seal_target(self.seal, identity)


class NoMutationReason(str, Enum):
    INPUT_PREPARATION_FAILED = "input_preparation_failed"
    FINAL_PLAN_PREPARATION_FAILED = "final_plan_preparation_failed"
    WITNESS_NOT_OBTAINED = "witness_not_obtained"
    STOPPED_BEFORE_SUBMISSION = "stopped_before_submission"


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class NoMutationSubmittedV1(_Record):
    evidence: OperationEvidenceV1
    reason: NoMutationReason
    transaction: TransactionWitnessV1 | None = None

    def __post_init__(self) -> None:
        _available_transaction(self.evidence, self.transaction)
        _exact(self.reason, NoMutationReason)
        if self.reason is NoMutationReason.INPUT_PREPARATION_FAILED:
            _require(type(self.evidence) is OperationIdentityV1)
        if self.reason is NoMutationReason.FINAL_PLAN_PREPARATION_FAILED:
            _require(type(self.evidence) is PreparedOperationInputsV1)
        if self.reason is NoMutationReason.WITNESS_NOT_OBTAINED:
            _require(type(self.evidence) is not OperationIntentWitnessV1)
            _require(self.transaction is None)


class NoCommitReason(str, Enum):
    AUTHORITATIVE_XID_ABORT = "authoritative_xid_abort"
    TOP_LEVEL_ROLLBACK_ACKNOWLEDGED = "top_level_rollback_acknowledged"


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class ProvedNoCommitV1(_Record):
    """Describes supplied qualified resolution; does not check status or restore.

    A task join, an absent result, and an overwritten latest slot are not reasons.
    No final intent is fabricated if resolution occurred before it was frozen.
    """

    evidence: OperationEvidenceV1
    transaction: TransactionWitnessV1
    resolution: ImmutableReferenceV1
    reason: NoCommitReason

    def __post_init__(self) -> None:
        identity = _identity(self.evidence)
        _exact(self.transaction, TransactionWitnessV1)
        _require(identity.installation == self.transaction.installation)
        if isinstance(self.evidence, OperationIntentWitnessV1):
            _require(self.evidence.transaction == self.transaction)
        _exact(self.resolution, ImmutableReferenceV1)
        _exact(self.reason, NoCommitReason)


class ExclusionKind(str, Enum):
    EXACT_ATTEMPT_FENCE = "exact_attempt_fence"
    EXACT_ATTEMPT_RELEASE = "exact_attempt_release"


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class GrantExclusionEvidenceV1(_Record):
    """Requires separate exclusion and no-earlier-completion coverage references.

    A higher number alone is insufficient. The supplied contract must cover this
    exact phase/target at all later entries; this class does not qualify it.
    No admission/range/control exclusion contract is selected by this module.
    """

    excluded_identity: OperationIdentityV1
    excluded_inputs_sha256: str
    excluded_grant: GrantEvidenceV1
    kind: ExclusionKind
    excluding_fence: int
    contract_sha256: str
    excluding_transition: ImmutableReferenceV1
    no_completion_coverage: ImmutableReferenceV1

    def __post_init__(self) -> None:
        _exact(self.excluded_identity, OperationIdentityV1)
        _require(isinstance(self.excluded_identity.phase, GrantBoundPhase))
        _digest(self.excluded_inputs_sha256)
        _exact(self.excluded_grant, GrantEvidenceV1)
        _exact(self.kind, ExclusionKind)
        _counter(self.excluding_fence, minimum=1)
        _digest(self.contract_sha256)
        _exact(self.excluding_transition, ImmutableReferenceV1)
        _exact(self.no_completion_coverage, ImmutableReferenceV1)
        if self.kind is ExclusionKind.EXACT_ATTEMPT_FENCE:
            _require(self.excluding_fence > self.excluded_grant.fence)
        else:
            _require(self.excluding_fence == self.excluded_grant.fence)


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class ExcludedWithoutCompletionV1(_Record):
    prepared: PreparedOperationInputsV1
    exclusion: GrantExclusionEvidenceV1

    def __post_init__(self) -> None:
        _exact(self.prepared, PreparedOperationInputsV1)
        _require(isinstance(self.prepared.identity.phase, GrantBoundPhase))
        _exact(self.exclusion, GrantExclusionEvidenceV1)
        _require(self.prepared.identity == self.exclusion.excluded_identity)
        _require(self.prepared.inputs_sha256 == self.exclusion.excluded_inputs_sha256)
        _require(self.prepared.grant == self.exclusion.excluded_grant)


class UnknownReason(str, Enum):
    TRANSPORT_OUTCOME = "transport_outcome"
    ORIGINAL_TRANSACTION_IN_PROGRESS = "original_transaction_in_progress"
    TRANSACTION_STATUS_UNAVAILABLE = "transaction_status_unavailable"
    FINAL_INTENT_UNAVAILABLE = "final_intent_unavailable"
    OPERATION_RECORD_UNAVAILABLE = "operation_record_unavailable"
    LATEST_OPERATION_SLOT_OVERWRITTEN = "latest_operation_slot_overwritten"
    EXCLUSION_CONTRACT_UNAVAILABLE = "exclusion_contract_unavailable"
    INSTALLATION_UNQUALIFIED = "installation_unqualified"


@dataclass(frozen=True, slots=True, repr=False, kw_only=True)
class OutcomeUnknownV1(_Record):
    """May retain a pre-lock xid before a final intent/witness exists.

    An in-progress original transaction must not be awaited while holding its
    needed account lock. This record performs neither waiting nor readback.
    """

    evidence: OperationEvidenceV1
    reason: UnknownReason
    transaction: TransactionWitnessV1 | None = None

    def __post_init__(self) -> None:
        _available_transaction(self.evidence, self.transaction)
        _exact(self.reason, UnknownReason)


SourceOperationOutcomeV1: TypeAlias = (
    TentativeOperationResultV1
    | ExactOperationConfirmationV1
    | ObservedOtherCompletionV1
    | ReadOnlyWinnerObservationV1
    | NoMutationSubmittedV1
    | ProvedNoCommitV1
    | ExcludedWithoutCompletionV1
    | OutcomeUnknownV1
)
