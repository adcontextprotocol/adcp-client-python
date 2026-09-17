"""Destination contracts, verification and optional durable materialization.

Use the immutable registry to prepare all frozen source rows, then invoke write
and verify explicitly with separate authorization sessions. The reference
writer is exclusively for tests/development. The durable service owns fencing,
retry allocation, target reselection and atomic finish. Production tier and
notification activation additionally require the complete seller projection.
"""

from typing import TYPE_CHECKING

from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryPrincipal,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingObligationDeliveryRecord,
)
from adcp.reporting.materializer._json import (
    ReportingVerificationLimits,
    parse_reporting_json,
    strict_reporting_json,
)
from adcp.reporting.materializer.capture import ReportingMaterializerBoundary
from adcp.reporting.materializer.contracts import (
    ReportingCanonicalization,
    ReportingDestinationLocator,
    ReportingDestinationPage,
    ReportingDestinationRequest,
    ReportingDestinationResolver,
    ReportingDestinationSession,
    ReportingDestinationWriter,
    ReportingExternalEffect,
    ReportingHeartbeat,
    ReportingIOContext,
    ReportingIOPhase,
    ReportingNativeObservation,
    ReportingPreparedRevision,
    ReportingVerificationKey,
    ReportingWriterCapability,
    ReportingWriterError,
    ReportingWriterFailure,
    ReportingWriterFailureCode,
    ReportingWriterRetry,
)
from adcp.reporting.materializer.memory import InMemoryReportingMaterializerStore
from adcp.reporting.materializer.reference import (
    ReferenceReportingDestinationWriter,
    ReferenceReportingResolver,
    reference_digest,
    reference_verifier,
)
from adcp.reporting.materializer.service import ReportingMaterializerService
from adcp.reporting.materializer.verification import (
    ReportingDestinationIO,
    ReportingRevisionRowReader,
    ReportingRevisionVerifier,
    ReportingRevisionVerifierRegistry,
    ReportingVerifiedDestination,
    validate_materialization_target,
)
from adcp.reporting.materializer.work import (
    MaterializerReason,
    ReportingMaterializerLease,
    ReportingMaterializerStore,
    ReportingMaterializerTurn,
)

if TYPE_CHECKING:
    from adcp.reporting.materializer.pg import PgReportingMaterializerStore

__all__ = [
    "InMemoryReportingMaterializerStore",
    "MaterializerReason",
    "PgReportingMaterializerStore",
    "ReferenceReportingDestinationWriter",
    "ReferenceReportingResolver",
    "ReportingCanonicalization",
    "ReportingDeliveryPrincipal",
    "ReportingDestinationBinding",
    "ReportingDestinationIO",
    "ReportingDestinationLocator",
    "ReportingDestinationPage",
    "ReportingDestinationRequest",
    "ReportingDestinationResolver",
    "ReportingDestinationSession",
    "ReportingDestinationWriter",
    "ReportingExternalEffect",
    "ReportingHeartbeat",
    "ReportingIOContext",
    "ReportingIOPhase",
    "ReportingMaterializationAttempt",
    "ReportingMaterializerBoundary",
    "ReportingMaterializerLease",
    "ReportingMaterializerService",
    "ReportingMaterializerStore",
    "ReportingMaterializerTurn",
    "ReportingNativeObservation",
    "ReportingObligationDeliveryRecord",
    "ReportingPreparedRevision",
    "ReportingRevisionRowReader",
    "ReportingRevisionVerifier",
    "ReportingRevisionVerifierRegistry",
    "ReportingVerificationKey",
    "ReportingVerificationLimits",
    "ReportingVerifiedDestination",
    "ReportingWriterCapability",
    "ReportingWriterError",
    "ReportingWriterFailure",
    "ReportingWriterFailureCode",
    "ReportingWriterRetry",
    "parse_reporting_json",
    "reference_digest",
    "reference_verifier",
    "strict_reporting_json",
    "validate_materialization_target",
]


def __getattr__(name: str) -> object:
    if name == "PgReportingMaterializerStore":
        from adcp.reporting.materializer.pg import PgReportingMaterializerStore

        return PgReportingMaterializerStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
