"""History, late binding, private captured ordering and selector corruption."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    ReportingMaterializationAttempt,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.materializer import ReportingDestinationRequest, ReportingWriterError

from ._durable_materializer_support import durable_case, durable_harness

pytestmark = pytest.mark.parametrize("backend", ["memory", "postgres"])


async def test_exact_source_replay_does_not_dirty_an_inflight_generation(backend):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        await h.store.put_configuration(case.config)
        await h.store.commit_revision(case.revision, case.rows)
        await h.store.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=case.revision.reporting_revision_id,
            readable=True,
        )
        prepared, evidence = await case.verified(lease)
        assert (
            await h.store.finish_materialization(lease, prepared=prepared, verified=evidence)
        ).state == "verified"


async def test_selected_official_never_uses_snapshot_artifact_and_preserves_both_histories(backend):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        first = await case.claim()
        prepared, evidence = await case.verified(first)
        await h.store.finish_materialization(first, prepared=prepared, verified=evidence)
        original = await case.outcomes()
        official = await case.publish()
        await h.store.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=official.reporting_revision_id,
            readable=False,
        )
        assert (await case.claim()).reason == "revision_unreadable"
        assert await case.outcomes() == original
        await h.store.set_revision_readable(
            account_id="acct_a", reporting_revision_id=official.reporting_revision_id, readable=True
        )
        second = await case.claim()
        assert first.attempt.attempt == second.attempt.attempt == 1
        assert first.attempt.reporting_revision_id != second.attempt.reporting_revision_id
        assert first.request.external_id != second.request.external_id
        prepared, evidence = await case.verified(second)
        await h.store.finish_materialization(second, prepared=prepared, verified=evidence)
        assert len(await case.outcomes()) == 2
        assert (await case.outcomes())[0] == original[0]
        assert case.writer.write_effects == 2


@pytest.mark.parametrize("corruption", ["fork", "cycle", "multiple_official"])
async def test_corrupt_topology_parks_without_attempt_or_hot_loop(backend, corruption, monkeypatch):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)

        def corrupt(context):
            first = case.revision
            second = replace(
                first,
                reporting_revision_id="second",
                supersedes_reporting_revision_id=first.reporting_revision_id,
            )
            if corruption == "fork":
                revisions = (first, second, replace(second, reporting_revision_id="third"))
            elif corruption == "cycle":
                revisions = (replace(first, supersedes_reporting_revision_id="second"), second)
            else:
                official = replace(
                    first,
                    finality="official",
                    finality_basis="source_final",
                    finality_policy_id="policy",
                    finalized_at=first.created_at,
                )
                revisions = (official, replace(official, reporting_revision_id="second"))
            return replace(context, revisions=revisions)

        cls = type(h.store)
        if backend == "memory":
            original = cls._context

            def read(self, scope):
                return corrupt(original(self, scope))

            method = "_context"
        else:
            original = cls._materializer_context_on

            async def read(self, connection, scope):
                return corrupt(await original(self, connection, scope))

            method = "_materializer_context_on"
        with monkeypatch.context() as patch:
            patch.setattr(cls, method, read)
            assert (await case.claim()).reason == "history_corrupt"
            assert (await case.claim()).state == "idle"
        assert not await h.works() and await h.queue() == ((), ())


async def test_inactive_late_binding_waits_for_activation_without_allocating_history(backend):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store, active=False, binding=False)
        await h.store.put_destination_binding(case.binding)
        assert (await case.claim()).reason == "inactive"
        assert not await h.works()
        await h.store.put_configuration(replace(case.config, deactivated_at=None))
        assert (await case.claim()).attempt.attempt == 1


async def test_private_consumer_boundaries_keep_a_durable_account_order(backend):
    from adcp.reporting.ledger.notification_models import ReportingNotificationError

    async with durable_harness(backend, notifications=True) as h:
        first = await durable_case(h.store)
        second = await durable_case(h.store, consumer="https://buyer.example.test/second")
        for case in (first, second):
            lease = await case.claim()
            # Account-fair discovery chooses the canonical consumer order.
            target = first if lease.scope == first.scope else second
            prepared, evidence = await target.verified(lease)
            await h.store.finish_materialization(lease, prepared=prepared, verified=evidence)
        boundaries = [
            (await h.store.read_materializer_boundaries(caller=case.scope.principal))[0]
            for case in (first, second)
        ]
        assert {b.sequence for b in boundaries} == {1}
        assert {b.account_sequence for b in boundaries} == {1, 2}
        for boundary in boundaries:
            assert boundary.core.consumer_ids == (boundary.caller.consumer_id,)
            assert all(
                getattr(r, "consumer_id", getattr(getattr(r, "scope", None), "consumer_id", None))
                == boundary.caller.consumer_id
                for r in boundary.reconciliation
            )
            with pytest.raises(ReportingNotificationError):
                replace(boundary, core=replace(boundary.core, consumer_ids=("another-consumer",)))
            with pytest.raises(ReportingNotificationError):
                replace(boundary, reporting_materialization_id="missing-outcome")
            with pytest.raises(ReportingNotificationError):
                replace(boundary, reconciliation=())
        assert len((await h.queue())[0]) == 2


async def test_rejected_consumer_receipt_does_not_dirty_materializer_or_allocate_retry(backend):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store, reconciliation_mode="consumer_receipt")
        assert (await case.service().run_once()).state == "verified"
        outcome = (await case.outcomes())[0]
        before = await h.works()
        await h.store.record_revision_receipt(
            ReportingRevisionReceiptRecord(
                case.scope,
                "receipt-rejected-0001",
                case.revision.reporting_revision_id,
                outcome.reporting_materialization_id,
                "rejected",
                case.binding.verification_profile,
                0,
                (),
                outcome.completed_at,
                rejection_codes=("ROW_COUNT_MISMATCH",),
            )
        )
        assert (await case.claim()).state in {"idle", "discovered"}
        assert await h.works() == before


async def test_tampered_lease_cannot_cross_principal_or_generation(backend):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        for changes in (
            {"scope": replace(lease.scope, consumer_id="another-consumer")},
            {"attempt": replace(lease.attempt, attempt=2)},
            {"token": "invalid"},
        ):
            with pytest.raises(ReportingWriterError):
                replace(lease, **changes)
        assert not await case.outcomes()


async def test_publication_does_not_bypass_legacy_pending_on_an_older_revision(backend):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        await h.store.bind_obligation_delivery(
            ReportingObligationDeliveryRecord(
                case.scope,
                "USD",
                case.revision.created_at + timedelta(days=400),
                case.revision.created_at,
            )
        )
        legacy = ReportingMaterializationAttempt(
            case.scope, case.revision.reporting_revision_id, "legacy", 1, case.revision.created_at
        )
        await h.store.commit_materialization_attempt(legacy)
        official = await case.publish()
        assert (await case.claim()).reason == "legacy_pending"
        assert not await h.works()
        identity = ReportingDestinationRequest.from_binding(
            case.binding, legacy, case.verifier.key
        ).external_id
        await h.store.import_pending_materialization(
            scope=case.scope,
            reporting_materialization_id="legacy",
            original_external_id=identity,
            keys=case.keys,
        )
        # Exact recovery concludes the obsolete attempt safely before the
        # current official gets its own first attempt; no old artifact is reused.
        assert (await case.service().run_once()).state == "failed"
        assert case.writer.write_effects == 0
        current = await case.claim()
        assert current.attempt.reporting_revision_id == official.reporting_revision_id
        assert current.attempt.attempt == 1
        assert len(await h.works()) == 2 and await h.queue() == ((), ())


async def test_external_terminal_write_parks_owned_pending_history_without_a_hot_loop(backend):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        await h.store.commit_materialization(
            ReportingMaterializationRecord(
                case.scope,
                lease.attempt.reporting_revision_id,
                lease.attempt.reporting_materialization_id,
                "failed",
                lease.attempt.created_at,
                failure_code="WRITE_FAILED",
            )
        )
        await h.expire()
        assert (await case.claim()).reason == "history_corrupt"
        assert (await case.claim()).state == "idle"
        assert len(await h.works()) == 1
        assert not await h.store.read_materializer_boundaries(caller=case.scope.principal)
        assert await h.queue() == ((), ())


async def test_public_terminal_outcome_keeps_projection_dirty_work_without_readiness(backend):
    """Suppressing the fenced readiness event must not drop ordinary projection work.

    Adopters keep persisting materialization outcomes directly while the durable
    service runs. Before this regression the materializer store silently stopped
    marking the status scope dirty for those writes, so the projector never saw
    the outcome and `get_reporting_status` stayed stale indefinitely.
    """
    from adcp.reporting.ledger._delivery_state import change_id
    from adcp.reporting.ledger.notification_models import ReportingStatusEvidence

    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        outcome = ReportingMaterializationRecord(
            case.scope,
            lease.attempt.reporting_revision_id,
            lease.attempt.reporting_materialization_id,
            "failed",
            lease.attempt.created_at,
            failure_code="WRITE_FAILED",
        )
        before = await h.dirty()
        stored, created = await h.store.commit_materialization(outcome)
        assert created
        added = (await h.dirty())[len(before) :]
        assert [reason for reason, _ in added] == ["materialization"]
        assert added[0][1] == ReportingStatusEvidence(stored.kind, change_id(stored))
        # The readiness intent still belongs only to a fenced verified finish.
        assert await h.queue() == ((), ())
        assert "reporting.delivery_ready" not in await h.ordinary_events()


async def test_reserved_attempt_keeps_the_finish_transaction_as_the_only_dirty_work(backend):
    """Reservation stays short: the terminal finish owns the projection work."""
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        before = await h.dirty()
        lease = await case.claim()
        assert (await h.dirty())[len(before) :] == ()
        prepared, verified = await case.verified(lease)
        assert (
            await h.store.finish_materialization(lease, prepared=prepared, verified=verified)
        ).state == "verified"
        assert [reason for reason, _ in (await h.dirty())[len(before) :]] == ["materialization"]


async def test_restored_exact_component_can_explicitly_resume_parked_identity(backend):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        await h.expire()
        assert (await h.store.claim_materialization(keys=())).reason == "component_unavailable"
        assert (await case.claim()).state == "idle"
        await h.store.import_pending_materialization(
            scope=case.scope,
            reporting_materialization_id=lease.attempt.reporting_materialization_id,
            original_external_id=lease.request.external_id,
            keys=case.keys,
        )
        resumed = await case.claim()
        assert resumed.attempt == lease.attempt and resumed.request == lease.request
        assert resumed.token != lease.token
