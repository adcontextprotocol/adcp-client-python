"""Strict source-only vectors; initially UNEXECUTED, with no public re-export."""

from __future__ import annotations

from datetime import datetime

from typing_extensions import assert_never, assert_type

from adcp.reporting.source_work.operation_contracts import (
    CertificateCompletionV1,
    CompletionEvidenceV1,
    ExactOperationConfirmationV1,
    ExcludedWithoutCompletionV1,
    FinalOperationIntentV1,
    GrantEvidenceV1,
    NoMutationSubmittedV1,
    ObservedOtherCompletionV1,
    OperationalCompletionV1,
    OperationEvidenceV1,
    OperationIdentityV1,
    OperationIntentWitnessV1,
    OutcomeUnknownV1,
    PredecessorV1,
    PreparedOperationInputsV1,
    ProvedNoCommitV1,
    ReadOnlyWinnerObservationV1,
    SourceOperationOutcomeV1,
    StatusPlanV1,
    TentativeOperationResultV1,
    TransactionWitnessV1,
)


def inspect_prepared(value: PreparedOperationInputsV1) -> None:
    assert_type(value.identity, OperationIdentityV1)
    assert_type(value.inputs_sha256, str)
    assert_type(value.predecessors, tuple[PredecessorV1, ...])
    assert_type(value.grant, GrantEvidenceV1 | None)


def inspect_finalized(value: OperationIntentWitnessV1) -> None:
    assert_type(value.intent, FinalOperationIntentV1)
    assert_type(value.intent.prepared, PreparedOperationInputsV1)
    assert_type(value.intent.finalized_intent_sha256, str)
    assert_type(value.intent.t_pub, datetime | None)
    assert_type(value.intent.status_plan, StatusPlanV1 | None)
    assert_type(value.transaction.full_xid8, int)
    if value.intent.status_plan is not None:
        assert_type(value.intent.status_plan.t_status, datetime)


def inspect_available_stage(value: OperationEvidenceV1) -> str:
    if isinstance(value, OperationIntentWitnessV1):
        assert_type(value.intent, FinalOperationIntentV1)
        return "final intent and transaction witness supplied"
    if isinstance(value, FinalOperationIntentV1):
        assert_type(value.prepared, PreparedOperationInputsV1)
        return "final intent supplied"
    if isinstance(value, PreparedOperationInputsV1):
        assert_type(value.identity, OperationIdentityV1)
        return "prepared inputs supplied"
    if isinstance(value, OperationIdentityV1):
        return "identity supplied"
    assert_never(value)


def inspect_completion(value: CompletionEvidenceV1) -> str:
    if isinstance(value, CertificateCompletionV1):
        assert_type(value.certificate.sha256, str)
        return "certificate-backed evidence supplied"
    if isinstance(value, OperationalCompletionV1):
        assert_type(value.input_predecessors, tuple[PredecessorV1, ...])
        assert_type(value.output_predecessors, tuple[PredecessorV1, ...])
        return "operation-specific CAS evidence supplied"
    assert_never(value)


def inspect_outcome(value: SourceOperationOutcomeV1) -> str:
    if isinstance(value, TentativeOperationResultV1):
        assert_type(value.witness, OperationIntentWitnessV1)
        return "tentative even after SQL validation"
    if isinstance(value, ExactOperationConfirmationV1):
        assert_type(value.expected, OperationIntentWitnessV1)
        assert_type(value.completion, CompletionEvidenceV1)
        return "matching supplied final witness and completion"
    if isinstance(value, ObservedOtherCompletionV1):
        assert_type(value.requested, PreparedOperationInputsV1)
        return "another operation observed"
    if isinstance(value, ReadOnlyWinnerObservationV1):
        assert_type(value.requested, PreparedOperationInputsV1)
        return "retained winner observed without creation attribution"
    if isinstance(value, NoMutationSubmittedV1):
        assert_type(value.evidence, OperationEvidenceV1)
        assert_type(value.transaction, TransactionWitnessV1 | None)
        return "no business mutation submitted"
    if isinstance(value, ProvedNoCommitV1):
        assert_type(value.evidence, OperationEvidenceV1)
        return "qualified resolution supplied"
    if isinstance(value, ExcludedWithoutCompletionV1):
        assert_type(value.prepared, PreparedOperationInputsV1)
        return "phase-specific exclusion and coverage supplied"
    if isinstance(value, OutcomeUnknownV1):
        assert_type(value.evidence, OperationEvidenceV1)
        assert_type(value.transaction, TransactionWitnessV1 | None)
        return "unknown at the supplied evidence stage"
    assert_never(value)
