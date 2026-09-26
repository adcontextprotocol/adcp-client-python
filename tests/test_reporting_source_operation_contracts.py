"""Source-review vectors for private operation records; initially UNEXECUTED.

These vectors assert local value invariants, never SQL authority, real recovery,
grant liveness, cancellation settlement or durable storage. No clock is sampled.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import cast
from uuid import UUID

import pytest

from adcp.reporting.source_work.operation_contracts import (
    MAX_COUNTER,
    MAX_XID8,
    AccountTargetV1,
    CaptureReferenceV1,
    CasDisposition,
    CertificateCompletionV1,
    CompletionEvidenceV1,
    ControlPhase,
    ExactOperationConfirmationV1,
    ExcludedWithoutCompletionV1,
    ExclusionKind,
    FinalOperationIntentV1,
    GrantBoundPhase,
    GrantEvidenceV1,
    GrantExclusionEvidenceV1,
    ImmutableReferenceV1,
    InstallationBindingV1,
    NeutralSealReferenceV1,
    NoCommitReason,
    NoMutationReason,
    NoMutationSubmittedV1,
    ObservedOtherCompletionV1,
    OperationalCompletionV1,
    OperationEvidenceV1,
    OperationFamily,
    OperationIdentityV1,
    OperationIntentWitnessV1,
    OperationPhaseV1,
    OperationRecordError,
    OutcomeUnknownV1,
    PredecessorKind,
    PredecessorV1,
    PreGrantPhase,
    PreparedOperationInputsV1,
    ProvedNoCommitV1,
    ReadOnlyWinnerObservationV1,
    ReadPhase,
    RecipeKind,
    StatusPlanV1,
    TentativeOperationResultV1,
    TentativeStage,
    TimerTargetV1,
    TransactionWitnessV1,
    UnknownReason,
)

T_PUB = datetime(2026, 9, 26, 10, 0, 0, 123456, tzinfo=timezone.utc)
T_STATUS = T_PUB + timedelta(seconds=3)
T_CAPTURE = T_PUB + timedelta(seconds=7)


def _ref(number: int) -> ImmutableReferenceV1:
    return ImmutableReferenceV1(record_id=UUID(int=number), sha256="a" * 64)


def _installation() -> InstallationBindingV1:
    return InstallationBindingV1(
        installation_id=UUID(int=1),
        incarnation_id=UUID(int=2),
        database_binding_sha256="b" * 64,
        schema_binding_sha256="c" * 64,
        graph_sha256="d" * 64,
    )


def _target() -> AccountTargetV1:
    return AccountTargetV1(
        account_id="account-private-sentinel",
        configuration_id="config-private-sentinel",
        configuration_version=1,
        generation_binding_sha256="e" * 64,
        target_id="acquisition-private-sentinel",
        obligation_id="obligation-private-sentinel",
        source_execution_key="source-private-sentinel",
    )


def _slot(kind: PredecessorKind, number: int = 30) -> PredecessorV1:
    return PredecessorV1(
        kind=kind,
        state_sha256="f" * 64,
        reference=_ref(number),
        version=4,
        authorization_epoch=2 if kind is PredecessorKind.AUTHORIZATION else None,
    )


def _publication() -> PreparedOperationInputsV1:
    return PreparedOperationInputsV1(
        identity=OperationIdentityV1(
            operation_id=UUID(int=10),
            installation=_installation(),
            target=_target(),
            family=OperationFamily.CERTIFICATE_EFFECT,
            phase=GrantBoundPhase.PROVISIONAL_PUBLICATION,
            recipe=RecipeKind.PUBLICATION_AND_STATUS,
            recipe_sha256="1" * 64,
        ),
        inputs_sha256="2" * 64,
        predecessors=(
            _slot(PredecessorKind.ATTEMPT, 20),
            _slot(PredecessorKind.AUTHORIZATION, 21),
            _slot(PredecessorKind.COMPANION, 22),
        ),
        grant=GrantEvidenceV1(
            attempt_id=UUID(int=20),
            owner_id=UUID(int=23),
            grant_id=UUID(int=24),
            fence=3,
            renewal_sequence=7,
            expires_at=T_STATUS + timedelta(seconds=20),
            authorization_epoch=2,
            authorization_version=4,
            companion_version=4,
        ),
    )


def _plan() -> StatusPlanV1:
    return StatusPlanV1(
        t_status=T_STATUS,
        input_stamp=8,
        dependency_inputs_sha256="3" * 64,
        plan_sha256="4" * 64,
    )


def _witness(prepared: PreparedOperationInputsV1) -> OperationIntentWitnessV1:
    recipe = prepared.identity.recipe
    intent = FinalOperationIntentV1(
        prepared=prepared,
        finalized_intent_sha256="5" * 64,
        t_pub=(
            T_PUB if recipe in (RecipeKind.PUBLICATION, RecipeKind.PUBLICATION_AND_STATUS) else None
        ),
        status_plan=(
            _plan() if recipe in (RecipeKind.STATUS, RecipeKind.PUBLICATION_AND_STATUS) else None
        ),
    )
    return OperationIntentWitnessV1(
        intent=intent,
        transaction=TransactionWitnessV1(
            installation=prepared.identity.installation, full_xid8=(1 << 32) + 41
        ),
    )


def _certificate(witness: OperationIntentWitnessV1) -> CertificateCompletionV1:
    return CertificateCompletionV1(
        witness=witness,
        certificate=_ref(40),
        result=_ref(41),
        actual_effect_sha256="6" * 64,
        captures=(CaptureReferenceV1(reference=_ref(42), captured_at=T_CAPTURE),),
    )


def _timer() -> PreparedOperationInputsV1:
    return PreparedOperationInputsV1(
        identity=OperationIdentityV1(
            operation_id=UUID(int=50),
            installation=_installation(),
            target=TimerTargetV1(partition_id="source-work"),
            family=OperationFamily.OPERATIONAL_MUTATION,
            phase=ControlPhase.TIMER,
            recipe=RecipeKind.IDENTITY_ONLY,
            recipe_sha256="1" * 64,
        ),
        inputs_sha256="2" * 64,
        predecessors=(_slot(PredecessorKind.CONTROL),),
    )


def _admission(phase: PreGrantPhase = PreGrantPhase.PERIOD_ROUTE) -> PreparedOperationInputsV1:
    target = _target()
    predecessors: tuple[PredecessorV1, ...] = (
        _slot(PredecessorKind.ADMISSION),
        _slot(PredecessorKind.AUTHORIZATION),
    )
    if phase is PreGrantPhase.PERIOD_ROUTE:
        target = replace(target, source_execution_key=None)
    elif phase is PreGrantPhase.HISTORICAL_ENROLLMENT:
        target = replace(target, obligation_id=None, source_execution_key=None)
        predecessors += (_slot(PredecessorKind.RANGE),)
    return PreparedOperationInputsV1(
        identity=OperationIdentityV1(
            operation_id=UUID(int=60),
            installation=_installation(),
            target=target,
            family=OperationFamily.CERTIFICATE_EFFECT,
            phase=phase,
            recipe=RecipeKind.IDENTITY_ONLY,
            recipe_sha256="1" * 64,
        ),
        inputs_sha256="2" * 64,
        predecessors=predecessors,
    )


def _read(phase: ReadPhase = ReadPhase.RESERVATION_WINNER) -> PreparedOperationInputsV1:
    target = _target()
    if phase is ReadPhase.ROUTE_WINNER:
        target = replace(target, source_execution_key=None)
    return PreparedOperationInputsV1(
        identity=OperationIdentityV1(
            operation_id=UUID(int=70),
            installation=_installation(),
            target=target,
            family=OperationFamily.READ_ONLY_WINNER,
            phase=phase,
            recipe=RecipeKind.IDENTITY_ONLY,
            recipe_sha256="1" * 64,
        ),
        inputs_sha256="2" * 64,
        predecessors=(),
    )


def _seal() -> NeutralSealReferenceV1:
    return NeutralSealReferenceV1(
        account_id="account-private-sentinel",
        source_execution_key="source-private-sentinel",
        staged_commit_ref="manifest-private-sentinel",
        manifest_sha256="7" * 64,
        byte_count=100,
    )


def _exclusion(prepared: PreparedOperationInputsV1) -> GrantExclusionEvidenceV1:
    assert prepared.grant is not None
    return GrantExclusionEvidenceV1(
        excluded_identity=prepared.identity,
        excluded_inputs_sha256=prepared.inputs_sha256,
        excluded_grant=prepared.grant,
        kind=ExclusionKind.EXACT_ATTEMPT_FENCE,
        excluding_fence=prepared.grant.fence + 1,
        contract_sha256="8" * 64,
        excluding_transition=_ref(80),
        no_completion_coverage=_ref(81),
    )


def test_prepared_inputs_do_not_claim_final_plan_or_time() -> None:
    prepared = _publication()
    assert {field.name for field in fields(prepared)} == {
        "identity",
        "inputs_sha256",
        "predecessors",
        "grant",
    }
    witness = _witness(prepared)
    assert witness.intent.prepared is prepared
    assert witness.intent.finalized_intent_sha256 != prepared.inputs_sha256
    assert witness.transaction.full_xid8 > (1 << 32)
    with pytest.raises(OperationRecordError):
        replace(witness, intent=cast(FinalOperationIntentV1, prepared))


def test_publication_status_and_capture_clocks_remain_separate() -> None:
    witness = _witness(_publication())
    completion = _certificate(witness)
    assert witness.intent.t_pub == T_PUB
    assert witness.intent.status_plan is not None
    assert witness.intent.status_plan.t_status == T_STATUS
    assert completion.captures[0].captured_at == T_CAPTURE
    confirmation = ExactOperationConfirmationV1(expected=witness, completion=completion)
    replay = replace(confirmation)
    assert replay == confirmation
    assert replay.completion.witness.intent is witness.intent
    offset = timezone(timedelta(hours=5, minutes=30))
    assert replace(witness.intent, t_pub=T_PUB.astimezone(offset)) == witness.intent
    assert witness.intent.t_pub.microsecond == 123456


def test_recipe_times_are_required_only_for_declared_phase_recipe() -> None:
    witness = _witness(_publication())
    with pytest.raises(OperationRecordError):
        replace(witness.intent, t_pub=None)
    with pytest.raises(OperationRecordError):
        replace(witness.intent, status_plan=None)
    no_status = replace(
        witness.intent.prepared,
        identity=replace(witness.intent.prepared.identity, recipe=RecipeKind.PUBLICATION),
    )
    assert _witness(no_status).intent.status_plan is None
    control = _witness(_timer())
    with pytest.raises(OperationRecordError):
        replace(control.intent, t_pub=T_PUB)
    with pytest.raises(OperationRecordError):
        replace(control.intent, status_plan=_plan())
    with pytest.raises(OperationRecordError):
        replace(control.intent.prepared.identity, recipe=RecipeKind.PUBLICATION)


@pytest.mark.parametrize("phase", tuple(PreGrantPhase))
def test_pregrant_effects_use_admission_or_range_predecessors(phase: PreGrantPhase) -> None:
    prepared = _admission(phase)
    assert prepared.grant is None
    assert prepared.identity.family is OperationFamily.CERTIFICATE_EFFECT
    assert (PredecessorKind.RANGE in {slot.kind for slot in prepared.predecessors}) == (
        phase is PreGrantPhase.HISTORICAL_ENROLLMENT
    )
    with pytest.raises(OperationRecordError):
        replace(prepared, grant=_publication().grant)
    with pytest.raises(OperationRecordError):
        replace(prepared, predecessors=(_slot(PredecessorKind.CONTROL),))
    with pytest.raises(OperationRecordError):
        ExcludedWithoutCompletionV1(prepared=prepared, exclusion=_exclusion(_publication()))


@pytest.mark.parametrize("phase", tuple(ControlPhase))
def test_operational_control_phases_have_their_own_predecessors(phase: ControlPhase) -> None:
    if phase is ControlPhase.TIMER:
        prepared = _timer()
    else:
        target = replace(_target(), obligation_id=None, source_execution_key=None)
        slots: tuple[PredecessorV1, ...] = (_slot(PredecessorKind.CONTROL),)
        if phase is ControlPhase.CLAIM:
            target = _target()
            slots = (_slot(PredecessorKind.AUTHORIZATION), _slot(PredecessorKind.COMPANION))
        elif phase is ControlPhase.AUTHORIZATION:
            slots = (_slot(PredecessorKind.AUTHORIZATION), _slot(PredecessorKind.CONTROL))
        elif phase is ControlPhase.ENROLLMENT_CONTROL:
            slots = (_slot(PredecessorKind.CONTROL), _slot(PredecessorKind.RANGE))
        identity = replace(_timer().identity, phase=phase, target=target)
        prepared = replace(_timer(), identity=identity, predecessors=slots)
    assert prepared.grant is None
    with pytest.raises(OperationRecordError):
        replace(prepared, grant=_publication().grant)
    with pytest.raises(OperationRecordError):
        ExcludedWithoutCompletionV1(prepared=prepared, exclusion=_exclusion(_publication()))


def test_family_phase_and_target_cannot_be_reclassified_by_null_grant() -> None:
    publication = _publication()
    with pytest.raises(OperationRecordError):
        replace(publication, grant=None)
    with pytest.raises(OperationRecordError):
        replace(publication.identity, family=OperationFamily.OPERATIONAL_MUTATION)
    with pytest.raises(OperationRecordError):
        replace(publication.identity, phase=cast(OperationPhaseV1, "provisional_publication"))
    with pytest.raises(OperationRecordError):
        replace(publication.identity, target=TimerTargetV1(partition_id="source-work"))
    with pytest.raises(OperationRecordError):
        replace(_timer().identity, target=_target())


@pytest.mark.parametrize(
    ("phase", "family", "recipe"),
    (
        (GrantBoundPhase.SEAL, OperationFamily.CERTIFICATE_EFFECT, RecipeKind.IDENTITY_ONLY),
        (
            GrantBoundPhase.ORDINARY_PUBLICATION,
            OperationFamily.CERTIFICATE_EFFECT,
            RecipeKind.PUBLICATION_AND_STATUS,
        ),
        (
            GrantBoundPhase.PROVISIONAL_PUBLICATION,
            OperationFamily.CERTIFICATE_EFFECT,
            RecipeKind.PUBLICATION_AND_STATUS,
        ),
        (GrantBoundPhase.SEMANTIC_FINISH, OperationFamily.CERTIFICATE_EFFECT, RecipeKind.STATUS),
        (GrantBoundPhase.RENEW, OperationFamily.OPERATIONAL_MUTATION, RecipeKind.IDENTITY_ONLY),
        (GrantBoundPhase.DISPATCH, OperationFamily.OPERATIONAL_MUTATION, RecipeKind.IDENTITY_ONLY),
        (GrantBoundPhase.RETRY, OperationFamily.OPERATIONAL_MUTATION, RecipeKind.IDENTITY_ONLY),
        (GrantBoundPhase.RELEASE, OperationFamily.OPERATIONAL_MUTATION, RecipeKind.IDENTITY_ONLY),
    ),
)
def test_each_grant_bound_phase_retains_exact_evidence(
    phase: GrantBoundPhase, family: OperationFamily, recipe: RecipeKind
) -> None:
    original = _publication()
    prepared = replace(
        original,
        identity=replace(original.identity, phase=phase, family=family, recipe=recipe),
    )
    witness = _witness(prepared)
    assert witness.intent.prepared.grant == original.grant
    with pytest.raises(OperationRecordError):
        replace(prepared, grant=None)


def test_grant_binding_checks_exact_attempt_and_authorization_companion_versions() -> None:
    prepared = _publication()
    assert prepared.grant is not None
    for grant in (
        replace(prepared.grant, attempt_id=UUID(int=99)),
        replace(prepared.grant, authorization_epoch=3),
        replace(prepared.grant, authorization_version=5),
        replace(prepared.grant, companion_version=5),
    ):
        with pytest.raises(OperationRecordError):
            replace(prepared, grant=grant)


def test_supplied_expiry_is_not_a_local_live_lease_check() -> None:
    prepared = _publication()
    assert prepared.grant is not None
    old = replace(prepared.grant, expires_at=T_PUB - timedelta(days=10))
    retained = replace(prepared, grant=old)
    assert retained.grant == old  # Shape acceptance asserts no authority at today's clock.


def test_exact_confirmation_compares_the_entire_final_witness() -> None:
    witness = _witness(_publication())
    completion = _certificate(witness)
    assert ExactOperationConfirmationV1(expected=witness, completion=completion).expected == witness
    assert witness.intent.status_plan is not None
    assert witness.intent.prepared.grant is not None
    changed_intents = (
        replace(witness.intent, finalized_intent_sha256="9" * 64),
        replace(witness.intent, t_pub=T_PUB + timedelta(microseconds=1)),
        replace(witness.intent, status_plan=replace(_plan(), plan_sha256="9" * 64)),
        replace(
            witness.intent,
            prepared=replace(witness.intent.prepared, inputs_sha256="9" * 64),
        ),
    )
    for intent in changed_intents:
        with pytest.raises(OperationRecordError):
            ExactOperationConfirmationV1(
                expected=replace(witness, intent=intent), completion=completion
            )
    for grant in (
        replace(witness.intent.prepared.grant, owner_id=UUID(int=99)),
        replace(witness.intent.prepared.grant, grant_id=UUID(int=99)),
        replace(witness.intent.prepared.grant, fence=4),
        replace(witness.intent.prepared.grant, renewal_sequence=8),
        replace(witness.intent.prepared.grant, expires_at=T_STATUS + timedelta(seconds=30)),
    ):
        expected = replace(
            witness,
            intent=replace(witness.intent, prepared=replace(witness.intent.prepared, grant=grant)),
        )
        with pytest.raises(OperationRecordError):
            ExactOperationConfirmationV1(expected=expected, completion=completion)
    with pytest.raises(OperationRecordError):
        ExactOperationConfirmationV1(
            expected=replace(witness, transaction=replace(witness.transaction, full_xid8=900)),
            completion=completion,
        )


def test_uuid_prepared_only_and_tentative_results_cannot_form_confirmation() -> None:
    witness = _witness(_publication())
    completion = _certificate(witness)
    for incomplete in (witness.intent.prepared.identity.operation_id, witness.intent.prepared):
        with pytest.raises(OperationRecordError):
            ExactOperationConfirmationV1(
                expected=cast(OperationIntentWitnessV1, incomplete), completion=completion
            )
    for stage in TentativeStage:
        tentative = TentativeOperationResultV1(witness=witness, result=_ref(90), stage=stage)
        with pytest.raises(OperationRecordError):
            ExactOperationConfirmationV1(
                expected=witness, completion=cast(CompletionEvidenceV1, tentative)
            )
    with pytest.raises(OperationRecordError):
        ExactOperationConfirmationV1(
            expected=witness, completion=cast(CompletionEvidenceV1, _seal())
        )


def test_operational_confirmation_binds_cas_record_without_certificate() -> None:
    prepared = _timer()
    witness = _witness(prepared)
    completion = OperationalCompletionV1(
        witness=witness,
        operation_record=_ref(91),
        result=_ref(92),
        input_predecessors=prepared.predecessors,
        output_predecessors=(replace(prepared.predecessors[0], version=5),),
        disposition=CasDisposition.APPLIED,
    )
    confirmation = ExactOperationConfirmationV1(expected=witness, completion=completion)
    assert confirmation.completion == completion
    rejected = replace(completion, disposition=CasDisposition.NOT_APPLIED)
    assert rejected.disposition is CasDisposition.NOT_APPLIED
    assert "certificate" not in {field.name for field in fields(completion)}
    with pytest.raises(OperationRecordError):
        replace(completion, input_predecessors=(replace(prepared.predecessors[0], version=6),))
    with pytest.raises(OperationRecordError):
        replace(completion, output_predecessors=(_slot(PredecessorKind.RANGE),))
    with pytest.raises(OperationRecordError):
        replace(completion, witness=_witness(_publication()))
    with pytest.raises(OperationRecordError):
        _certificate(witness)


def test_other_completion_is_observed_without_attributing_our_commit() -> None:
    requested = _publication()
    other = replace(requested, identity=replace(requested.identity, operation_id=UUID(int=100)))
    completion = _certificate(_witness(other))
    observed = ObservedOtherCompletionV1(requested=requested, completion=completion)
    other_identity = completion.witness.intent.prepared.identity
    assert observed.requested.identity.operation_id != other_identity.operation_id
    with pytest.raises(OperationRecordError):
        ExactOperationConfirmationV1(expected=_witness(requested), completion=completion)
    with pytest.raises(OperationRecordError):
        ObservedOtherCompletionV1(requested=other, completion=completion)
    assert isinstance(requested.identity.target, AccountTargetV1)
    foreign = replace(requested.identity.target, account_id="another-account")
    with pytest.raises(OperationRecordError):
        ObservedOtherCompletionV1(
            requested=replace(requested, identity=replace(requested.identity, target=foreign)),
            completion=completion,
        )


@pytest.mark.parametrize("phase", tuple(ReadPhase))
def test_read_only_winner_has_no_certificate_or_creation_claim(phase: ReadPhase) -> None:
    prepared = _read(phase)
    observed = ReadOnlyWinnerObservationV1(
        requested=prepared,
        winner=_ref(101),
        seal=_seal() if phase is ReadPhase.SEAL_WINNER else None,
    )
    assert observed.requested.identity.family is OperationFamily.READ_ONLY_WINNER
    assert "certificate" not in {field.name for field in fields(observed)}
    with pytest.raises(OperationRecordError):
        _certificate(_witness(prepared))
    with pytest.raises(OperationRecordError):
        ExactOperationConfirmationV1(
            expected=_witness(prepared), completion=cast(CompletionEvidenceV1, observed)
        )


def test_neutral_seal_is_detached_and_account_execution_bound() -> None:
    read = _read(ReadPhase.SEAL_WINNER)
    seal = _seal()
    assert seal.manifest_sha256 == "7" * 64
    with pytest.raises(OperationRecordError):
        ReadOnlyWinnerObservationV1(requested=read, winner=_ref(101), seal=None)
    for foreign in (
        replace(seal, account_id="foreign-account"),
        replace(seal, source_execution_key="different-execution"),
    ):
        with pytest.raises(OperationRecordError):
            ReadOnlyWinnerObservationV1(requested=read, winner=_ref(101), seal=foreign)
    with pytest.raises(OperationRecordError):
        replace(seal, byte_count=1_048_577)
    with pytest.raises(OperationRecordError):
        replace(seal, staged_commit_ref="../payload")


def test_unknown_preserves_each_available_evidence_stage() -> None:
    prepared = _publication()
    witness = _witness(prepared)
    stages: tuple[OperationEvidenceV1, ...] = (prepared.identity, prepared, witness.intent, witness)
    for stage in stages:
        outcome = OutcomeUnknownV1(evidence=stage, reason=UnknownReason.TRANSPORT_OUTCOME)
        assert outcome.evidence is stage
    overwritten = OutcomeUnknownV1(
        evidence=prepared, reason=UnknownReason.LATEST_OPERATION_SLOT_OVERWRITTEN
    )
    assert overwritten.evidence is prepared
    with pytest.raises(OperationRecordError):
        OutcomeUnknownV1(
            evidence=cast(OperationEvidenceV1, RuntimeError("private-provider-message")),
            reason=UnknownReason.OPERATION_RECORD_UNAVAILABLE,
        )


def test_unknown_retains_pre_lock_xid_without_inventing_final_intent() -> None:
    prepared = _admission(PreGrantPhase.RESERVE_ENVELOPE)
    transaction = TransactionWitnessV1(installation=_installation(), full_xid8=901)
    unknown = OutcomeUnknownV1(
        evidence=prepared,
        reason=UnknownReason.ORIGINAL_TRANSACTION_IN_PROGRESS,
        transaction=transaction,
    )
    assert unknown.evidence is prepared
    assert unknown.transaction is transaction
    assert not isinstance(unknown.evidence, OperationIntentWitnessV1)
    with pytest.raises(OperationRecordError):
        replace(
            unknown,
            transaction=replace(
                transaction,
                installation=replace(_installation(), incarnation_id=UUID(int=202)),
            ),
        )
    witness = _witness(prepared)
    with pytest.raises(OperationRecordError):
        replace(unknown, evidence=witness)  # Its already known xid must match.


def test_no_submission_no_commit_and_exclusion_are_separate() -> None:
    prepared = _admission()
    early = NoMutationSubmittedV1(
        evidence=prepared.identity, reason=NoMutationReason.INPUT_PREPARATION_FAILED
    )
    witness = _witness(prepared)
    no_commit = ProvedNoCommitV1(
        evidence=prepared,
        transaction=witness.transaction,
        resolution=_ref(110),
        reason=NoCommitReason.AUTHORITATIVE_XID_ABORT,
    )
    assert early.evidence is prepared.identity
    assert no_commit.evidence is prepared  # No finalized-intent witness was invented.
    plan_failed = NoMutationSubmittedV1(
        evidence=prepared,
        reason=NoMutationReason.FINAL_PLAN_PREPARATION_FAILED,
        transaction=witness.transaction,
    )
    assert plan_failed.evidence is prepared
    with pytest.raises(OperationRecordError):
        NoMutationSubmittedV1(evidence=witness, reason=NoMutationReason.WITNESS_NOT_OBTAINED)
    with pytest.raises(OperationRecordError):
        replace(no_commit, reason=cast(NoCommitReason, "latest_slot_overwritten"))
    with pytest.raises(OperationRecordError):
        replace(
            no_commit,
            evidence=witness,
            transaction=replace(witness.transaction, full_xid8=999),
        )


def test_grant_exclusion_binds_phase_target_input_and_coverage() -> None:
    prepared = _publication()
    exclusion = _exclusion(prepared)
    outcome = ExcludedWithoutCompletionV1(prepared=prepared, exclusion=exclusion)
    assert outcome.exclusion.no_completion_coverage == _ref(81)
    with pytest.raises(OperationRecordError):
        replace(exclusion, no_completion_coverage=cast(ImmutableReferenceV1, None))
    with pytest.raises(OperationRecordError):
        replace(exclusion, excluding_fence=exclusion.excluded_grant.fence)
    with pytest.raises(OperationRecordError):
        replace(outcome, prepared=replace(prepared, inputs_sha256="9" * 64))
    seal = replace(
        prepared,
        identity=replace(
            prepared.identity, phase=GrantBoundPhase.SEAL, recipe=RecipeKind.IDENTITY_ONLY
        ),
    )
    with pytest.raises(OperationRecordError):
        replace(outcome, prepared=seal)
    with pytest.raises(OperationRecordError):
        replace(exclusion, excluded_identity=_admission().identity)
    released = replace(
        exclusion,
        kind=ExclusionKind.EXACT_ATTEMPT_RELEASE,
        excluding_fence=exclusion.excluded_grant.fence,
    )
    release_outcome = replace(outcome, exclusion=released)
    assert release_outcome.exclusion.kind is ExclusionKind.EXACT_ATTEMPT_RELEASE


def test_witness_installation_and_incarnation_are_exact() -> None:
    witness = _witness(_publication())
    for installation in (
        replace(_installation(), incarnation_id=UUID(int=200)),
        replace(_installation(), database_binding_sha256="9" * 64),
        replace(_installation(), schema_binding_sha256="9" * 64),
    ):
        with pytest.raises(OperationRecordError):
            replace(witness, transaction=replace(witness.transaction, installation=installation))


def test_nested_values_and_bounded_tuples_are_deeply_immutable() -> None:
    prepared = _publication()
    with pytest.raises(FrozenInstanceError):
        setattr(prepared.identity.installation, "incarnation_id", UUID(int=200))
    with pytest.raises(FrozenInstanceError):
        setattr(prepared.predecessors[0], "version", 99)
    with pytest.raises(OperationRecordError):
        replace(prepared, predecessors=cast(tuple[PredecessorV1, ...], list(prepared.predecessors)))
    with pytest.raises(OperationRecordError):
        replace(prepared, predecessors=tuple(reversed(prepared.predecessors)))
    with pytest.raises(OperationRecordError):
        replace(prepared, predecessors=prepared.predecessors + prepared.predecessors[:1])
    with pytest.raises(OperationRecordError):
        replace(prepared, grant=cast(GrantEvidenceV1, {"fence": 3}))
    completion = _certificate(_witness(prepared))
    with pytest.raises(OperationRecordError):
        replace(
            completion, captures=cast(tuple[CaptureReferenceV1, ...], list(completion.captures))
        )
    with pytest.raises(OperationRecordError):
        replace(completion, captures=completion.captures * 2)
    with pytest.raises(OperationRecordError):
        replace(
            completion,
            captures=tuple(
                CaptureReferenceV1(reference=_ref(number), captured_at=T_CAPTURE)
                for number in (201, 202, 203)
            ),
        )


def test_expected_absence_is_a_cas_input_and_not_a_no_commit_proof() -> None:
    absent = PredecessorV1(
        kind=PredecessorKind.CONTROL, state_sha256="0" * 64, reference=None, version=None
    )
    prepared = replace(_timer(), predecessors=(absent,))
    assert prepared.predecessors[0].reference is None
    with pytest.raises(OperationRecordError):
        replace(absent, version=0)
    with pytest.raises(OperationRecordError):
        replace(absent, reference=_ref(204))
    with pytest.raises(OperationRecordError):
        replace(absent, authorization_epoch=1)


@pytest.mark.parametrize("value", (True, False, 1.0, -1, MAX_COUNTER + 1))
def test_counters_reject_bool_float_and_out_of_range_values(value: object) -> None:
    with pytest.raises(OperationRecordError):
        replace(_plan(), input_stamp=cast(int, value))
    assert replace(_plan(), input_stamp=0).input_stamp == 0
    assert replace(_plan(), input_stamp=MAX_COUNTER).input_stamp == MAX_COUNTER


@pytest.mark.parametrize("value", (True, 3.0, -1, 0, 1, 2, MAX_XID8 + 1))
def test_xid8_is_full_unsigned_integer_and_not_an_unknown_sentinel(value: object) -> None:
    with pytest.raises(OperationRecordError):
        TransactionWitnessV1(installation=_installation(), full_xid8=cast(int, value))
    maximum = TransactionWitnessV1(installation=_installation(), full_xid8=MAX_XID8)
    assert maximum.full_xid8 == MAX_XID8


@pytest.mark.parametrize("value", ("", "x" * 256, "a\x00b", "\ud800"))
def test_identifiers_are_bounded_unicode_scalars(value: str) -> None:
    with pytest.raises(OperationRecordError):
        replace(_target(), account_id=value)
    assert replace(_target(), account_id="\U0001f642" * 255).account_id == "\U0001f642" * 255


@pytest.mark.parametrize("value", ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "sha256:" + "a" * 64))
def test_digest_references_have_exact_spelling_without_a_codec(value: str) -> None:
    with pytest.raises(OperationRecordError):
        replace(_ref(205), sha256=value)


def test_uuid_and_integer_fields_do_not_coerce_strings_or_bool() -> None:
    with pytest.raises(OperationRecordError):
        replace(_ref(205), record_id=cast(UUID, str(UUID(int=205))))
    with pytest.raises(OperationRecordError):
        replace(_ref(205), record_id=UUID(int=0))
    with pytest.raises(OperationRecordError):
        replace(_target(), configuration_version=cast(int, True))
    with pytest.raises(OperationRecordError):
        replace(_seal(), byte_count=cast(int, True))


def test_time_values_are_aware_detached_and_never_truncated() -> None:
    class UntrustedZone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta:
            raise AssertionError("custom timezone callback must not run")

    for value in (
        T_PUB.replace(tzinfo=None),
        T_PUB.replace(tzinfo=UntrustedZone()),
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    ):
        with pytest.raises(OperationRecordError):
            replace(_plan(), t_status=value)
    offset = timezone(timedelta(hours=-7))
    plan = replace(_plan(), t_status=T_STATUS.astimezone(offset))
    assert plan.t_status == T_STATUS
    assert plan.t_status.tzinfo is timezone.utc
    assert plan.t_status.microsecond == 123456


def test_reprs_and_validation_diagnostics_do_not_expose_supplied_evidence() -> None:
    prepared = _publication()
    witness = _witness(prepared)
    values = (
        prepared,
        prepared.identity,
        prepared.identity.target,
        prepared.identity.installation,
        prepared.grant,
        witness,
        witness.intent,
        _certificate(witness),
        _seal(),
    )
    for value in values:
        rendered = repr(value)
        assert "private-sentinel" not in rendered
        assert "a" * 64 not in rendered
        assert "<redacted>" in rendered
    with pytest.raises(OperationRecordError) as raised:
        replace(prepared, inputs_sha256="private-invalid-evidence")
    assert str(raised.value) == "INVALID_SOURCE_OPERATION_RECORD"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
