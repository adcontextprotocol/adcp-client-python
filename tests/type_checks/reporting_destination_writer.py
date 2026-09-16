"""Public B1 adoption, without private model imports, Any, or type suppressions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from adcp.reporting.ledger import (
    ReportingObligationRecord,
    ReportingRevisionRecord,
    select_reporting_revision,
)
from adcp.reporting.materializer import (
    ReferenceReportingDestinationWriter,
    ReferenceReportingResolver,
    ReportingDestinationBinding,
    ReportingDestinationIO,
    ReportingDestinationLocator,
    ReportingDestinationPage,
    ReportingDestinationRequest,
    ReportingDestinationResolver,
    ReportingDestinationSession,
    ReportingDestinationWriter,
    ReportingIOContext,
    ReportingIOPhase,
    ReportingMaterializationAttempt,
    ReportingNativeObservation,
    ReportingObligationDeliveryRecord,
    ReportingPreparedRevision,
    ReportingRevisionRowReader,
    ReportingRevisionVerifierRegistry,
    ReportingVerifiedDestination,
    reference_verifier,
)


@dataclass(frozen=True)
class TrustedResolver:
    """Real adopters acquire credentials in session._open and release in _close."""

    delegate: ReferenceReportingResolver

    def resolve(
        self,
        request: ReportingDestinationRequest,
        *,
        phase: ReportingIOPhase,
        context: ReportingIOContext,
    ) -> ReportingDestinationSession:
        return self.delegate.resolve(request, phase=phase, context=context)


class AdopterSession(ReportingDestinationSession):
    """Typed provider hooks; the SDK owns redaction and the async context lifecycle."""

    async def _open(self) -> None:
        pass

    async def _close(self) -> None:
        pass

    async def write(self, content: ReportingPreparedRevision) -> ReportingDestinationLocator:
        raise NotImplementedError

    async def read_rows(
        self, locator: ReportingDestinationLocator, *, cursor: str | None, limit: int
    ) -> ReportingDestinationPage:
        raise NotImplementedError

    async def read_manifest(self, locator: ReportingDestinationLocator) -> bytes:
        raise NotImplementedError

    async def list_objects(self, locator: ReportingDestinationLocator) -> tuple[str, ...]:
        raise NotImplementedError

    async def read_object(
        self, locator: ReportingDestinationLocator, *, object_ref: str
    ) -> AsyncIterator[bytes]:
        yield b""

    async def observe_native_version(
        self, locator: ReportingDestinationLocator
    ) -> ReportingNativeObservation:
        raise NotImplementedError


async def adopter(
    reader: ReportingRevisionRowReader,
    binding: ReportingDestinationBinding,
    delivery: ReportingObligationDeliveryRecord,
    obligation: ReportingObligationRecord,
    history: Sequence[ReportingRevisionRecord],
    attempt: ReportingMaterializationAttempt,
    deadline: datetime,
) -> ReportingVerifiedDestination | None:
    selection = select_reporting_revision(
        history,
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        required_finality=obligation.required_finality,
    )
    if selection.kind == "corrupt" or selection.kind == "not_ready":
        reason: str = selection.reason
        assert reason
        return None
    chosen: ReportingRevisionRecord = selection.revision
    assert chosen.reporting_revision_id == attempt.reporting_revision_id
    verifier = reference_verifier()
    registry = ReportingRevisionVerifierRegistry((verifier,))
    reference = ReferenceReportingDestinationWriter((verifier.key.capability,))
    production: Literal[False] = reference.production_eligible
    assert production is False
    writer: ReportingDestinationWriter = reference
    assert writer.capabilities
    resolver: ReportingDestinationResolver = TrustedResolver(
        ReferenceReportingResolver(reference, registry, (binding,))
    )
    cancel = asyncio.Event()
    prepared = await registry.prepare(
        key=verifier.key,
        binding=binding,
        delivery=delivery,
        obligation=obligation,
        revisions=history,
        attempt=attempt,
        reader=reader,
        context=ReportingIOContext(deadline, cancel),
    )
    io = ReportingDestinationIO(registry, resolver)
    locator = await io.write(prepared, context=ReportingIOContext(deadline, cancel))
    return await io.verify(prepared, locator, context=ReportingIOContext(deadline, cancel))
