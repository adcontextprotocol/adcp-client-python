"""Shared deterministic vectors; PostgreSQL work always uses real database time."""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from adcp.reporting.ledger import (
    ReportingDeliveryScope,
    ReportingDestinationBinding,
    ReportingMaterializationRecord,
    ReportingRevisionRecord,
    revision_content_sha256,
)
from adcp.reporting.materializer import (
    ReferenceReportingDestinationWriter,
    ReferenceReportingResolver,
    ReportingDestinationIO,
    ReportingRevisionVerifierRegistry,
    reference_digest,
    reference_verifier,
)
from adcp.reporting.materializer.memory import InMemoryReportingMaterializerStore
from adcp.reporting.materializer.service import ReportingMaterializerService
from adcp.reporting.materializer.work import ReportingMaterializerLease

from ._generation_support import END, START, configuration, isolated_reporting_pool, obligation_for
from ._materializer_support import io_context, reference_rows
from ._reliable_support import ManualClock


@dataclass
class DurableHarness:
    store: object
    clock: object
    pool: object = None

    async def image(self):
        """Every table/collection changed by an SDK transaction, including heads."""
        if self.pool is None:
            return deepcopy(
                {k: v for k, v in vars(self.store).items() if k not in {"_clock", "_lock"}}
            )
        from psycopg import sql

        result = {}
        async with self.pool.connection() as c:
            tables = await (
                await c.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
                    " AND starts_with(tablename,'reporting_') ORDER BY tablename"
                )
            ).fetchall()
            for (table,) in tables:
                result[table] = await (
                    await c.execute(
                        sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY to_jsonb(t)::text").format(
                            sql.Identifier(table)
                        )
                    )
                ).fetchall()
        return result

    async def expire(self):
        """Persist expiry, then let the production fence observe it; no timing sleeps."""
        if self.pool is None:
            self.clock.advance(timedelta(seconds=1))
            for work in self.store._materializer_work.values():
                if not work.acked:
                    work.lease_until = self.clock() if work.token is not None else None
                    work.due_at = self.clock()
            for candidate in self.store._materializer_candidates.values():
                if candidate.due_at is not None:
                    candidate.due_at = self.clock()
            return
        async with self.pool.connection() as c, c.transaction():
            await self.store._lock_account(c, "acct_a")
            await c.execute(
                "UPDATE reporting_materializer_work SET lease_until=CASE"
                " WHEN lease_token IS NOT NULL THEN clock_timestamp() END,"
                " due_at=clock_timestamp() WHERE state='pending'"
            )
            await c.execute(
                "UPDATE reporting_materializer_candidates SET due_at=clock_timestamp()"
                " WHERE due_at IS NOT NULL"
            )
            await c.execute("UPDATE reporting_materializer_accounts SET due_at=clock_timestamp()")

    async def queue(self):
        if self.pool is None:
            state = self.store._materializer_outbox
            if state is None:
                return (), ()
            return tuple(state.events.values()), tuple(w.state for w in state.expansions.values())
        async with self.pool.connection() as c:
            events = await (
                await c.execute("SELECT snapshot FROM reporting_materializer_notification_events")
            ).fetchall()
            expansions = await (
                await c.execute("SELECT state FROM reporting_materializer_notification_expansions")
            ).fetchall()
        return tuple(r[0] for r in events), tuple(r[0] for r in expansions)

    async def works(self):
        if self.pool is None:
            return tuple(
                (w.request.external_id, "acked" if w.acked else "pending", w.generation)
                for w in self.store._materializer_work.values()
            )
        async with self.pool.connection() as c:
            return tuple(
                await (
                    await c.execute(
                        "SELECT external_id,state,generation FROM reporting_materializer_work"
                        " ORDER BY created_at,reporting_materialization_id"
                    )
                ).fetchall()
            )


@asynccontextmanager
async def durable_harness(backend, *, notifications=False):
    clock = ManualClock(datetime.now(timezone.utc))
    if backend == "memory":
        yield DurableHarness(
            InMemoryReportingMaterializerStore(clock=clock, notifications=notifications), clock
        )
    else:
        from adcp.reporting.materializer.pg import PgReportingMaterializerStore

        async with isolated_reporting_pool(autocommit=True) as pool:
            store = PgReportingMaterializerStore(pool=pool, notifications=notifications)
            await store.create_schema()
            yield DurableHarness(store, clock, pool)


@dataclass
class DurableCase:
    store: object
    config: object
    obligation: object
    binding: object
    revision: object
    rows: object
    verifier: object
    registry: object
    writer: object
    resolver: object
    io: object

    @property
    def scope(self):
        return ReportingDeliveryScope(
            self.config.generation_key,
            self.binding.consumer_id,
            self.obligation.reporting_obligation_id,
        )

    @property
    def keys(self):
        return (self.verifier.key,)

    def service(self, **kwargs):
        return ReportingMaterializerService(self.store, self.io, self.writer, **kwargs)

    async def claim(self, **kwargs):
        for _ in range(4):
            result = await self.store.claim_materialization(keys=self.keys, **kwargs)
            if getattr(result, "state", None) != "discovered":
                return result
        raise AssertionError("discovery did not converge")

    async def verified(self, lease):
        from adcp.reporting.materializer.service import _LeaseHeartbeat

        assert isinstance(lease, ReportingMaterializerLease)
        target = lease.context
        context = io_context()
        # The same SDK-owned I/O component used by the service; the test holds
        # its explicit lease while injecting individual finish boundaries.
        context = replace(context, heartbeat=_LeaseHeartbeat(self.store, lease, 30, context.cancel))
        prepared = await self.registry.prepare(
            key=self.verifier.key,
            binding=target.binding,
            delivery=target.delivery,
            obligation=target.obligation,
            revisions=target.revisions,
            attempt=lease.attempt,
            reader=self.store,
            context=context,
        )
        locator = await self.io.write(prepared, context=context)
        verified = await self.io.verify(prepared, locator, context=context)
        return prepared, verified

    async def outcomes(self):
        snapshot = await self.store.read_reconciliation_snapshot(caller=self.scope.principal)
        return tuple(r for r in snapshot.records if isinstance(r, ReportingMaterializationRecord))

    async def publish(
        self, revision_id="revision-official", *, finality="official", supersedes=None
    ):
        pairs = self.revision.control_totals
        revision = replace(
            self.revision,
            reporting_revision_id=revision_id,
            finality=finality,
            revision_content_sha256=revision_content_sha256(
                reporting_revision_id=revision_id,
                row_count=len(self.rows),
                control_totals=pairs,
                reporting_rows=self.rows,
                control_total_evidence=self.revision.managed_control_totals,
            ),
            finality_basis="source_final" if finality == "official" else None,
            finality_policy_id="reference-final" if finality == "official" else None,
            finalized_at=END if finality == "official" else None,
            supersedes_reporting_revision_id=supersedes,
        )
        return await self.store.commit_revision(revision, self.rows)


async def durable_case(
    store,
    *,
    count=1,
    account="acct_a",
    consumer="https://buyer.example.test/agent",
    finality="snapshot",
    required="snapshot",
    active=True,
    binding=True,
    legacy_definition=False,
    reconciliation_mode="delivery_only",
):
    verifier = reference_verifier()
    if legacy_definition:
        verifier = replace(
            verifier,
            key=replace(
                verifier.key,
                definition=replace(
                    verifier.key.definition,
                    monetary_metric_units=(),
                    monetary_control_total_units=(),
                ),
            ),
        )
    registry = ReportingRevisionVerifierRegistry((verifier,))
    config = replace(
        configuration(account),
        deactivated_at=None if active else END,
        definition=verifier.key.definition,
        report_definition_id=verifier.key.report_definition_id,
        required_finality=required,
    )
    await store.put_configuration(config)
    obligation = await store.commit_obligation(obligation_for(config))
    cap = verifier.key.capability
    destination = ReportingDestinationBinding(
        config.generation_key,
        consumer,
        "destination",
        "trusted-reference-binding",
        cap.method,
        cap.transport,
        cap.verification_profile,
        reconciliation_mode,
        "analytics",
        400,
        START,
        cap.format,
        ("reference-v1",),
        "available",
    )
    if binding:
        await store.put_destination_binding(destination)
    rows = reference_rows(count)
    _, totals = verifier.canonicalize(rows)
    pairs = tuple((t.name, t.value) for t in totals)
    revision = ReportingRevisionRecord(
        f"revision-{account}",
        account,
        obligation.reporting_obligation_id,
        finality,
        revision_content_sha256(
            reporting_revision_id=f"revision-{account}",
            row_count=count,
            control_totals=pairs,
            reporting_rows=rows,
            control_total_evidence=totals,
        ),
        count,
        pairs,
        END,
        END,
        END,
        finality_basis="source_final" if finality == "official" else None,
        finality_policy_id="reference-final" if finality == "official" else None,
        finalized_at=END if finality == "official" else None,
        canonical_content_digest=reference_digest(verifier, rows),
        managed_control_totals=totals,
    )
    await store.commit_revision(revision, rows)
    writer = ReferenceReportingDestinationWriter((cap,))
    resolver = ReferenceReportingResolver(writer, registry, (destination,))
    return DurableCase(
        store,
        config,
        obligation,
        destination,
        revision,
        rows,
        verifier,
        registry,
        writer,
        resolver,
        ReportingDestinationIO(registry, resolver),
    )
