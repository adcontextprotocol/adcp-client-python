"""Shared durable state machine: real PostgreSQL and deterministic memory."""

from dataclasses import fields, replace

import pytest
from pydantic import TypeAdapter

from adcp.reporting.ledger import ReportingMaterializationAttempt
from adcp.reporting.materializer import (
    ReportingVerifiedDestination,
    ReportingWriterError,
    ReportingWriterFailure,
)
from adcp.reporting.materializer.work import ReportingMaterializerLease

from ._durable_materializer_support import durable_case, durable_harness
from ._generation_support import configuration, obligation_for, revision_for

pytestmark = pytest.mark.parametrize("backend", ["memory", "postgres"])


async def test_core_records_do_not_create_managed_work_capture_or_readiness(backend):
    async with durable_harness(backend, notifications=True) as h:
        config = configuration()
        obligation = obligation_for(config)
        revision, rows = revision_for(obligation)
        await h.store.put_configuration(config)
        await h.store.commit_obligation(obligation)
        await h.store.commit_revision(revision, rows)
        materializer_operation_1 = await h.store.claim_materialization(keys=())
        assert (materializer_operation_1).state == "idle"
        assert not await h.works() and await h.queue() == ((), ())


async def test_durable_verified_success_still_cannot_advertise_unactivated_readiness(backend):
    from adcp.reporting.ledger.notification_models import ReportingNotificationError
    from adcp.reporting.outbox import (
        InMemoryReportingOutbox,
        PgReportingOutbox,
        ReportingEnvelopeCipher,
        ReportingNotificationWorker,
    )

    class NoSubscriptions:
        async def list_active(self, **kwargs):
            raise AssertionError("capability inspection must not enumerate subscriptions")

        async def get_active(self, **kwargs):
            raise AssertionError("capability inspection must not authorize an HTTP delivery")

    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        materializer_operation_2 = await case.service().run_once()
        assert (materializer_operation_2).state == "verified"
        outbox = (
            PgReportingOutbox(pool=h.pool)
            if h.pool is not None
            else InMemoryReportingOutbox(h.store)
        )
        worker = ReportingNotificationWorker(
            outbox=outbox,
            subscriptions=NoSubscriptions(),
            cipher=ReportingEnvelopeCipher(b"e" * 32),
        )
        # The new store is deliberately not in the production composition's
        # approved identity set until B2.4 proves projection/mount readiness.
        with pytest.raises(ReportingNotificationError, match="notification_chain_unready"):
            await worker.advertised_notifications(
                h.store, account_id="acct_a", ready_scope=case.scope
            )
        assert len((await h.queue())[0]) == 1 and (await h.queue())[1] == ("quarantined",)


async def test_materializer_store_closes_core_advertisement_until_b24_admits_it(backend):
    """Pin the full extent of the closed advertisement so B2.4 must act deliberately.

    `advertised_notifications` matches the ledger by exact `type(...)`, so both
    materializer stores lose Core `reporting.ledger_changed` too, not only the
    Managed/Reconciled claims. Fail closed is correct here, but it is broader
    than a tier veto and an adopter switching store classes must be told.
    """
    from adcp.reporting.ledger.notification_models import ReportingNotificationError
    from adcp.reporting.outbox import (
        InMemoryReportingOutbox,
        PgReportingOutbox,
        ReportingEnvelopeCipher,
        ReportingNotificationWorker,
    )

    class NoSubscriptions:
        async def list_active(self, **kwargs):
            return ()

        async def get_active(self, **kwargs):
            return None

    async def advertise(store, outbox):
        return await ReportingNotificationWorker(
            outbox=outbox,
            subscriptions=NoSubscriptions(),
            cipher=ReportingEnvelopeCipher(b"e" * 32),
        ).advertised_notifications(store, account_id="acct_a", ready_scope=None)

    async with durable_harness(backend, notifications=True) as h:
        if h.pool is None:
            from adcp.reporting.ledger.delivery import InMemoryReportingReconciliationStore

            reviewed = InMemoryReportingReconciliationStore(notifications=True)
            reviewed_outbox = InMemoryReportingOutbox(reviewed)
            outbox = InMemoryReportingOutbox(h.store)
        else:
            from adcp.reporting.ledger.delivery_pg import PgReportingReconciliationStore

            reviewed = PgReportingReconciliationStore(pool=h.pool, notifications=True)
            reviewed_outbox = PgReportingOutbox(pool=h.pool)
            outbox = PgReportingOutbox(pool=h.pool)
        # The reviewed store still advertises the Core notification.
        materializer_operation_3 = await advertise(reviewed, reviewed_outbox)
        assert (materializer_operation_3)["ledger_notification"] == ("reporting.ledger_changed")
        with pytest.raises(ReportingNotificationError, match="notification_chain_unready"):
            await advertise(h.store, outbox)


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("count", [0, 501])
async def test_verified_finish_captures_private_inputs_acks_and_quarantines_exact_event(
    backend, notifications, count
):
    async with durable_harness(backend, notifications=notifications) as h:
        case = await durable_case(h.store, count=count)
        lease = await case.claim()
        assert lease.attempt.attempt == 1
        assert lease.context.delivery.created_at == lease.attempt.created_at
        prepared, evidence = await case.verified(lease)
        result = await h.store.finish_materialization(lease, prepared=prepared, verified=evidence)
        assert result.state == "verified"
        assert case.writer.write_effects == 1
        assert case.writer.open_count == case.writer.close_count == 2
        outcomes = await case.outcomes()
        assert len(outcomes) == 1 and outcomes[0].verification.row_count == count
        boundaries = await h.store.read_materializer_boundaries(caller=case.scope.principal)
        assert len(boundaries) == 1
        assert boundaries[0].as_of == outcomes[0].completed_at
        assert boundaries[0].core.consumer_ids == (case.binding.consumer_id,)
        assert outcomes[0] in boundaries[0].reconciliation
        assert (
            await h.store.read_materializer_boundaries(
                caller=replace(case.scope.principal, consumer_id="another-buyer")
            )
            == ()
        )
        events, expansions = await h.queue()
        assert len(events) == int(notifications)
        assert expansions == (("quarantined",) if notifications else ())
        assert (await h.works())[0][1] == "acked"
        materializer_operation_4 = await h.store.finish_materialization(
            lease, prepared=prepared, verified=evidence
        )
        assert materializer_operation_4 == result
        assert await h.queue() == (events, expansions)
        assert await h.store.read_materializer_boundaries(caller=case.scope.principal) == boundaries


async def test_service_owns_reservation_verification_and_finish(backend):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        materializer_operation_5 = await case.service().run_once()
        assert (materializer_operation_5).state == "verified"
        materializer_operation_6 = await case.service().run_once()
        assert (materializer_operation_6).state in {"idle", "discovered"}
        assert case.writer.write_effects == 1


async def test_invalid_caller_arguments_stay_actionable_and_claim_no_external_effect(backend):
    """A caller-argument rejection must not masquerade as an unknown external effect.

    The closed-error guard previously rewrote these deterministic ``ValueError``s
    into ``RESOURCE_UNAVAILABLE``/``same_identity``/``unknown``, which tells an
    adopter a destination write may have started and hides what to correct.
    """
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        before = await h.image()
        calls = (
            (
                "materializer leases",
                lambda: h.store.claim_materialization(keys=case.keys, lease_seconds=1),
            ),
            (
                "materializer leases",
                lambda: h.store.claim_materialization(keys=case.keys, lease_seconds=301),
            ),
            (
                "materializer boundary reads",
                lambda: h.store.read_materializer_boundaries(caller=case.scope.principal, after=-1),
            ),
            (
                "materializer boundary reads",
                lambda: h.store.read_materializer_boundaries(caller=case.scope.principal, limit=0),
            ),
        )
        for expected, call in calls:
            with pytest.raises(ValueError) as caught:
                await call()
            assert not isinstance(caught.value, ReportingWriterError)
            assert str(caught.value).startswith(expected)
            assert not hasattr(caught.value, "failure")
        assert await h.image() == before
        assert case.writer.write_effects == 0


async def test_unknown_effect_resumes_same_attempt_external_identity_and_rejects_old_fence(backend):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        first = await case.claim()
        prepared, evidence = await case.verified(first)
        result = await h.store.finish_materialization(
            first, error=ReportingWriterFailure("WRITE_FAILED", "same_identity", "unknown")
        )
        assert result.state == "pending"
        assert not await case.outcomes()
        await h.expire()
        second = await case.claim()
        assert second.attempt == first.attempt
        assert second.request.external_id == first.request.external_id
        assert second.token != first.token
        materializer_operation_7 = await h.store.finish_materialization(
            first, prepared=prepared, verified=evidence
        )
        assert (materializer_operation_7).state == "pending"
        prepared, evidence = await case.verified(second)
        assert case.writer.write_effects == 1
        materializer_operation_8 = await h.store.finish_materialization(
            second, prepared=prepared, verified=evidence
        )
        assert (materializer_operation_8).state == "verified"


@pytest.mark.parametrize("change", ["official", "unreadable", "deactivated"])
async def test_changed_authority_finishes_safe_failure_without_reusing_artifact(backend, change):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        first = await case.claim()
        prepared, evidence = await case.verified(first)
        if change == "official":
            await case.publish()
        elif change == "unreadable":
            await h.store.set_revision_readable(
                account_id=case.config.account_id,
                reporting_revision_id=case.revision.reporting_revision_id,
                readable=False,
            )
        else:
            await h.store.put_configuration(
                replace(case.config, deactivated_at=case.revision.created_at)
            )
        result = await h.store.finish_materialization(first, prepared=prepared, verified=evidence)
        assert result.state == "failed"
        assert (await case.outcomes())[0].failure_code == "RESOURCE_UNAVAILABLE"
        assert await h.queue() == ((), ())
        next_work = await case.claim()
        if change == "official":
            assert next_work.attempt.reporting_revision_id == "revision-official"
            assert next_work.attempt.attempt == 1
            assert next_work.request.external_id != first.request.external_id
        else:
            assert not isinstance(next_work, ReportingMaterializerLease)
            if change == "unreadable":
                await h.store.set_revision_readable(
                    account_id=case.config.account_id,
                    reporting_revision_id=case.revision.reporting_revision_id,
                    readable=True,
                )
                next_work = await case.claim()
                assert (
                    next_work.attempt.reporting_revision_id == first.attempt.reporting_revision_id
                )
                assert next_work.attempt.attempt == 2


async def test_known_service_failure_allows_next_attempt_but_pending_does_not(backend):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        first = await case.claim()
        materializer_operation_9 = await case.claim()
        assert not isinstance(materializer_operation_9, ReportingMaterializerLease)
        await h.store.finish_materialization(
            first, error=ReportingWriterFailure("WRITE_FAILED", "new_attempt", "not_started")
        )
        await h.expire()
        second = await case.claim()
        assert second.attempt.attempt == 2
        assert second.request.external_id != first.request.external_id


async def test_sdk_seal_rejects_fabricated_or_modified_readback(backend):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        prepared, evidence = await case.verified(lease)
        forged = ReportingVerifiedDestination(
            evidence.request, evidence.resource, evidence.verification
        )
        with pytest.raises(ReportingWriterError):
            await h.store.finish_materialization(lease, prepared=prepared, verified=forged)
        codec = TypeAdapter(ReportingVerifiedDestination)
        assert {f.name for f in fields(evidence)} == {"request", "resource", "verification"}
        assert set(codec.dump_python(evidence, mode="json")) == {
            "request",
            "resource",
            "verification",
        }
        restored = codec.validate_json(codec.dump_json(evidence))
        assert restored == evidence  # B1's serializable observation contract survives.
        with pytest.raises(ReportingWriterError):
            await h.store.finish_materialization(lease, prepared=prepared, verified=restored)
        object.__setattr__(evidence, "verification", replace(evidence.verification, row_count=7))
        with pytest.raises(ReportingWriterError):
            await h.store.finish_materialization(lease, prepared=prepared, verified=evidence)
        assert not await case.outcomes()
        assert (await h.works())[0][1] == "pending"


async def test_prior_lease_readback_cannot_finish_a_new_fence_in_the_same_process(backend):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        old = await case.claim()
        prepared, proof = await case.verified(old)
        await h.expire()
        resumed = await case.claim()
        assert resumed.attempt == old.attempt and resumed.token != old.token
        with pytest.raises(ReportingWriterError):
            await h.store.finish_materialization(resumed, prepared=prepared, verified=proof)
        assert not await case.outcomes() and await h.queue() == ((), ())
        prepared, proof = await case.verified(resumed)
        materializer_operation_10 = await h.store.finish_materialization(
            resumed, prepared=prepared, verified=proof
        )
        assert (materializer_operation_10).state == "verified"
        assert case.writer.write_effects == 1


async def test_finish_rechecks_sdk_evidence_after_acquiring_transaction_locks(backend, monkeypatch):
    async with durable_harness(backend, notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        prepared, proof = await case.verified(lease)
        before = await h.image()
        cls = type(h.store)
        if h.pool is None:
            original = cls._held

            def held(self, reserved):
                result = original(self, reserved)
                object.__setattr__(proof, "verification", replace(proof.verification, row_count=7))
                return result

            monkeypatch.setattr(cls, "_held", held)
        else:
            original = cls._held_on

            async def held(self, connection, reserved):
                result = await original(self, connection, reserved)
                object.__setattr__(proof, "verification", replace(proof.verification, row_count=7))
                return result

            monkeypatch.setattr(cls, "_held_on", held)
        with pytest.raises(ReportingWriterError):
            await h.store.finish_materialization(lease, prepared=prepared, verified=proof)
        assert await h.image() == before
        assert not await case.outcomes() and await h.queue() == ((), ())


async def test_legacy_pending_requires_explicit_exact_external_history_import(backend):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store)
        # The public API remains independent; this predates any service reservation.
        from datetime import timedelta

        from adcp.reporting.ledger import ReportingObligationDeliveryRecord
        from adcp.reporting.materializer import ReportingDestinationRequest

        await h.store.bind_obligation_delivery(
            ReportingObligationDeliveryRecord(
                case.scope,
                "USD",
                case.revision.created_at + timedelta(days=400),
                case.revision.created_at,
            )
        )
        attempt = ReportingMaterializationAttempt(
            case.scope, case.revision.reporting_revision_id, "legacy", 1, case.revision.created_at
        )
        await h.store.commit_materialization_attempt(attempt)
        result = await case.claim()
        assert result.reason == "legacy_pending"
        assert not await h.works()
        with pytest.raises(ReportingWriterError):
            await h.store.import_pending_materialization(
                scope=case.scope,
                reporting_materialization_id="legacy",
                original_external_id="unknown",
                keys=case.keys,
            )
        identity = ReportingDestinationRequest.from_binding(
            case.binding, attempt, case.verifier.key
        ).external_id
        await h.store.import_pending_materialization(
            scope=case.scope,
            reporting_materialization_id="legacy",
            original_external_id=identity,
            keys=case.keys,
        )
        lease = await case.claim()
        assert lease.attempt == attempt and lease.request.external_id == identity


@pytest.mark.parametrize(
    "required,finality,expected",
    [
        ("snapshot", "snapshot", "verified"),
        ("official", "snapshot", "parked"),
        ("official", "official", "verified"),
    ],
)
async def test_finality_and_late_delivery_binding(backend, required, finality, expected):
    async with durable_harness(backend) as h:
        case = await durable_case(h.store, required=required, finality=finality, binding=False)
        materializer_operation_11 = await case.service().run_once()
        assert (materializer_operation_11).state == "idle"
        await h.store.put_destination_binding(case.binding)
        materializer_operation_12 = await case.service().run_once()
        assert (materializer_operation_12).state == expected
