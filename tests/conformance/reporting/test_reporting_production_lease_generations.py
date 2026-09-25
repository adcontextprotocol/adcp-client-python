"""Lease bookkeeping preserves held materializations without hiding real changes."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from adcp.reporting.ledger import LedgerConflictError, PgReportingLedgerStore
from adcp.reporting.materializer import PgReportingMaterializerStore, ReportingWriterError
from adcp.reporting.materializer.schema import validate_materializer_schema

from ._durable_materializer_support import durable_case, durable_harness
from ._generation_support import NOW, configuration, isolated_reporting_pool


async def candidate_generation(pool, case):
    key = case.config.generation_key
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT generation FROM reporting_materializer_candidates"
                " WHERE account_id=%s AND consumer_id=%s AND delivery_config_id=%s"
                " AND delivery_config_version=%s AND reporting_obligation_id=%s",
                (
                    key.account_id,
                    case.binding.consumer_id,
                    key.delivery_config_id,
                    key.delivery_config_version,
                    case.obligation.reporting_obligation_id,
                ),
            )
        ).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize("store_type", [PgReportingLedgerStore, PgReportingMaterializerStore])
@pytest.mark.parametrize("source_change", [False, True])
async def test_shared_producer_preserves_held_materialization_and_real_source_fences(
    store_type, source_change
):
    async with durable_harness("postgres") as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        prepared, verified = await case.verified(lease)
        other = await durable_case(h.store, account="acct_b")
        other_lease = await other.claim()
        assert other_lease.scope.principal.account_id == "acct_b"
        await other.publish(revision_id="other-official")
        other_generation = await candidate_generation(h.pool, other)
        if source_change:
            await case.publish(revision_id="new-official")
        expected = await candidate_generation(h.pool, case)
        producer = store_type(pool=h.pool)
        token = await producer.lease_period_close(
            worker_id="producer", now=datetime.now(timezone.utc), lease_seconds=60
        )
        assert token is not None and token.generation_key == case.config.generation_key
        await producer.release_period_close(token, worker_id="producer")
        adoption_operation_1 = await candidate_generation(h.pool, case) == expected
        assert adoption_operation_1
        adoption_operation_2 = await candidate_generation(h.pool, other) == other_generation
        assert adoption_operation_2
        if source_change:
            assert expected > lease.generation
            with pytest.raises(ReportingWriterError, match="CURRENT_REVISION_CHANGED"):
                await h.store.authorize_materialization(lease)
        else:
            assert expected == lease.generation
            await h.store.authorize_materialization(lease)
        result = await h.store.finish_materialization(lease, prepared=prepared, verified=verified)
        assert (result.state, result.reason) == (
            ("failed", "target_changed") if source_change else ("verified", "verified")
        )


@pytest.mark.parametrize("trigger_mode", ["disabled", "replica", "drift"])
async def test_shared_lease_never_blindly_decrements_suppressed_or_changed_triggers(trigger_mode):
    async with durable_harness("postgres") as h:
        case = await durable_case(h.store)
        await case.claim()
        original = await candidate_generation(h.pool, case)
        async with h.store.transaction(), h.store._connection() as connection:
            if trigger_mode == "disabled":
                await connection.execute(
                    "ALTER TABLE reporting_configurations"
                    " DISABLE TRIGGER reporting_materializer_configuration"
                )
            elif trigger_mode == "replica":
                await connection.execute("SET LOCAL session_replication_role='replica'")
                adoption_operation_3 = await (
                    await connection.execute(
                        "SELECT tgenabled FROM pg_trigger"
                        " WHERE tgrelid='reporting_configurations'::regclass"
                        " AND tgname='reporting_materializer_configuration'"
                    )
                ).fetchone() == ("O",)
                assert adoption_operation_3
            else:
                definition = await (
                    await connection.execute(
                        "SELECT pg_get_functiondef('reporting_materializer_source_dirty()'"
                        "::regprocedure)"
                    )
                ).fetchone()
                assert "generation = generation + 1" in definition[0]
                await connection.execute(
                    definition[0].replace(
                        "generation = generation + 1", "generation = generation + 2"
                    )
                )
            token = await h.store.lease_period_close(
                worker_id="producer", now=datetime.now(timezone.utc), lease_seconds=60
            )
            assert token is not None
            await h.store.release_period_close(token, worker_id="producer")
        # Replica mode did not fire the enabled trigger. Drift must remain
        # visible instead of restoring an unexpected increment to a valid lease.
        adoption_operation_4 = await candidate_generation(h.pool, case) == original + (
            4 if trigger_mode == "drift" else 0
        )
        assert adoption_operation_4
        async with h.pool.connection() as connection:
            if trigger_mode == "replica":
                await validate_materializer_schema(connection)
            else:
                with pytest.raises(LedgerConflictError) as rejected:
                    await validate_materializer_schema(connection)
                assert rejected.value.code == "MATERIALIZER_SCHEMA_UNREADY"


@pytest.mark.parametrize("wrong_fence", ["worker", "expiry"])
async def test_stale_shared_release_preserves_current_lease_and_materializer_generation(
    wrong_fence,
):
    async with durable_harness("postgres") as h:
        case = await durable_case(h.store)
        await case.claim()
        producer = PgReportingLedgerStore(pool=h.pool)
        token = await producer.lease_period_close(worker_id="producer", now=NOW, lease_seconds=60)
        assert token is not None
        generation = await candidate_generation(h.pool, case)
        pending = await h.works()
        stale = (
            replace(token, lease_expires_at=token.lease_expires_at + timedelta(seconds=1))
            if wrong_fence == "expiry"
            else token
        )
        await producer.release_period_close(
            stale, worker_id="wrong" if wrong_fence == "worker" else "producer"
        )
        async with h.pool.connection() as connection:
            adoption_operation_5 = await (
                await connection.execute(
                    "SELECT lease_worker_id,lease_expires_at FROM reporting_configurations"
                )
            ).fetchone() == ("producer", token.lease_expires_at)
            assert adoption_operation_5
        adoption_operation_6 = await candidate_generation(h.pool, case) == generation
        assert adoption_operation_6
        adoption_operation_7 = await h.works() == pending
        assert adoption_operation_7
        await producer.release_period_close(token, worker_id="producer")
        adoption_operation_8 = await candidate_generation(h.pool, case) == generation
        assert adoption_operation_8


async def test_base_lease_does_not_require_or_create_materializer_tables():
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        await store.put_configuration(configuration())
        token = await store.lease_period_close(worker_id="producer", now=NOW, lease_seconds=60)
        assert token is not None
        await store.release_period_close(token, worker_id="producer")
        async with pool.connection() as connection:
            adoption_operation_9 = await (
                await connection.execute("SELECT to_regclass('reporting_materializer_candidates')")
            ).fetchone() == (None,)
            assert adoption_operation_9
