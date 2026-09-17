"""B1 public destination contracts and verification; no durable Managed service.

Use the immutable registry to prepare all frozen source rows, then invoke write
and verify explicitly with separate authorization sessions. The reference
writer is exclusively for tests/development. B2 owns durable work, fencing,
retry allocation, final target reselection, and readiness transactions.
"""

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
from adcp.reporting.materializer.reference import (
    ReferenceReportingDestinationWriter,
    ReferenceReportingResolver,
    reference_digest,
    reference_verifier,
)
from adcp.reporting.materializer.verification import (
    ReportingDestinationIO,
    ReportingRevisionRowReader,
    ReportingRevisionVerifier,
    ReportingRevisionVerifierRegistry,
    ReportingVerifiedDestination,
    validate_materialization_target,
)

__all__ = [
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
