"""B1 development destination: verify frozen content without advertising Managed.

The trusted application supplies a frozen binding and its existing attempt.
Keep this object across retries to retain the reference writer's in-process
idempotency history. It is intentionally not durable or production eligible.
There is no scheduler, retry allocation, persistence, or readiness publication.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from adcp.reporting.ledger import ReportingObligationRecord, ReportingRevisionRecord
from adcp.reporting.materializer import (
    ReferenceReportingDestinationWriter,
    ReferenceReportingResolver,
    ReportingDestinationBinding,
    ReportingDestinationIO,
    ReportingIOContext,
    ReportingMaterializationAttempt,
    ReportingObligationDeliveryRecord,
    ReportingRevisionRowReader,
    ReportingRevisionVerifier,
    ReportingRevisionVerifierRegistry,
    ReportingVerifiedDestination,
    reference_verifier,
)


@dataclass(frozen=True)
class DevelopmentDestination:
    verifier: ReportingRevisionVerifier
    registry: ReportingRevisionVerifierRegistry
    resolver: ReferenceReportingResolver
    writer: ReferenceReportingDestinationWriter

    async def verify_revision(
        self,
        *,
        reader: ReportingRevisionRowReader,
        binding: ReportingDestinationBinding,
        delivery: ReportingObligationDeliveryRecord,
        obligation: ReportingObligationRecord,
        revisions: Sequence[ReportingRevisionRecord],
        attempt: ReportingMaterializationAttempt,
        deadline_at: datetime,
        cancel: asyncio.Event,
    ) -> ReportingVerifiedDestination:
        # Complete source verification happens before the resolver can open a
        # destination session. The same frozen attempt keeps its external ID.
        prepared = await self.registry.prepare(
            key=self.verifier.key,
            reader=reader,
            binding=binding,
            delivery=delivery,
            obligation=obligation,
            revisions=revisions,
            attempt=attempt,
            context=ReportingIOContext(deadline_at, cancel),
        )
        io = ReportingDestinationIO(self.registry, self.resolver)
        locator = await io.write(prepared, context=ReportingIOContext(deadline_at, cancel))
        # A fresh session reauthorizes readback, including any intervening
        # revocation/credential rotation. Credentials never enter these records.
        return await io.verify(prepared, locator, context=ReportingIOContext(deadline_at, cancel))


def development_destination(binding: ReportingDestinationBinding) -> DevelopmentDestination:
    """Install the exact bundled JSONL example definition/canonicalization."""
    verifier = reference_verifier()
    registry = ReportingRevisionVerifierRegistry((verifier,))
    writer = ReferenceReportingDestinationWriter((verifier.key.capability,))
    resolver = ReferenceReportingResolver(writer, registry, (binding,))
    return DevelopmentDestination(verifier, registry, resolver, writer)
