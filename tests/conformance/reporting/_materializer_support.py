"""Frozen content shared by the B1 pure, memory and installed-artifact gates."""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from adcp.reporting.ledger import (
    InMemoryReportingReconciliationStore,
    ReportingDeliveryScope,
    ReportingRevisionRecord,
    revision_content_sha256,
)
from adcp.reporting.materializer import (
    ReferenceReportingDestinationWriter,
    ReferenceReportingResolver,
    ReportingDestinationBinding,
    ReportingDestinationIO,
    ReportingIOContext,
    ReportingMaterializationAttempt,
    ReportingObligationDeliveryRecord,
    ReportingRevisionVerifierRegistry,
    reference_digest,
    reference_verifier,
)

from ._generation_support import END, START, configuration, obligation_for


def io_context(seconds=30):
    return ReportingIOContext(
        datetime.now(timezone.utc) + timedelta(seconds=seconds), asyncio.Event()
    )


def reference_rows(count):
    return [
        {
            "row_id": f"{i:06d}",
            "impressions": i % 3,
            "spend": "1.25",
            "currency": "USD",
            "details": {"active": True, "values": [1, None, "é", "e\u0301"]},
        }
        for i in range(count)
    ]


@dataclass
class Case:
    store: object
    verifier: object
    registry: object
    binding: object
    delivery: object
    obligation: object
    revision: object
    attempt: object
    rows: object
    prepared: object
    writer: object
    resolver: object
    io: object

    async def prepare(self, **changes):
        args = dict(
            key=self.verifier.key,
            binding=self.binding,
            delivery=self.delivery,
            obligation=self.obligation,
            revisions=(self.revision,),
            attempt=self.attempt,
            reader=self.store,
            context=io_context(),
        )
        args.update(changes)
        return await self.registry.prepare(**args)


async def materializer_case(
    count=1,
    *,
    capability=None,
    consumer="https://buyer.example.test/agents/reporting",
    account="acct_a",
    store=None,
    finality="snapshot",
):
    verifier = reference_verifier(capability)
    registry = ReportingRevisionVerifierRegistry((verifier,))
    config = replace(
        configuration(account),
        definition=verifier.key.definition,
        report_definition_id=verifier.key.report_definition_id,
        required_finality=finality,
    )
    store = store or InMemoryReportingReconciliationStore(notifications=True)
    await store.put_configuration(config)
    obligation = await store.commit_obligation(obligation_for(config))
    cap = verifier.key.capability
    binding = ReportingDestinationBinding(
        config.generation_key,
        consumer,
        "destination",
        "trusted-reference-binding",
        cap.method,
        cap.transport,
        cap.verification_profile,
        "delivery_only",
        "analytics",
        400,
        START,
        cap.format,
        ("reference-v1",),
        "delivered" if cap.method == "warehouse_materialization" else "available",
    )
    await store.put_destination_binding(binding)
    delivery = ReportingObligationDeliveryRecord(
        ReportingDeliveryScope(config.generation_key, consumer, obligation.reporting_obligation_id),
        "USD",
        END + timedelta(days=400),
        END,
    )
    await store.bind_obligation_delivery(delivery)
    rows = reference_rows(count)
    _, totals = verifier.canonicalize(rows)
    pairs = tuple((t.name, t.value) for t in totals)
    revision = ReportingRevisionRecord(
        "revision-first",
        account,
        obligation.reporting_obligation_id,
        finality,
        revision_content_sha256(
            reporting_revision_id="revision-first",
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
    attempt = ReportingMaterializationAttempt(
        delivery.scope,
        revision.reporting_revision_id,
        "materialization-first",
        1,
        END + timedelta(seconds=1),
    )
    await store.commit_materialization_attempt(attempt)
    writer = ReferenceReportingDestinationWriter((cap,))
    resolver = ReferenceReportingResolver(writer, registry, (binding,))
    io = ReportingDestinationIO(registry, resolver)
    case = Case(
        store,
        verifier,
        registry,
        binding,
        delivery,
        obligation,
        revision,
        attempt,
        rows,
        None,
        writer,
        resolver,
        io,
    )
    case.prepared = await case.prepare()
    return case
