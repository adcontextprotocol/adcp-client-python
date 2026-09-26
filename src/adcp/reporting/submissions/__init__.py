"""Buyer-only durable receipt submission, separate from reconciliation planning.

Public imports are additive and work without the optional PostgreSQL driver.
The application supplies a trusted authorizer, a receipt client, and an explicit
intent store. Use PgReportingSubmissionIntentStore for restart durability; the
memory implementation is a volatile reference for tests. No checkpoint, seller
service, consumer-status, or client.reporting facade behavior is changed.

Rollout: use a dedicated buyer schema/pool; explicitly call create_schema before
enabling submissions. The migration adds only reporting_buyer_submission_*.
Retain pending intents indefinitely and resume them after any uncertain result.
Do not drop the tables on rollback or replace pending plans with new receipt IDs.
Disable new planning while recovering, then resume the same stored scope with
current authorization. Exact final-head review precedes facade integration.
"""

from adcp.reporting.submissions.models import (
    ReportingReceiptFailureCode,
    ReportingReceiptOutcome,
    ReportingReceiptSubmission,
    ReportingSubmissionCode,
    ReportingSubmissionError,
    ReportingSubmissionReceipt,
    ReportingSubmissionResult,
    ReportingSubmissionScope,
    prepare_reporting_receipt_submission,
)
from adcp.reporting.submissions.pg import PgReportingSubmissionIntentStore
from adcp.reporting.submissions.store import (
    InMemoryReportingSubmissionIntentStore,
    ReportingSubmissionIntentStore,
)
from adcp.reporting.submissions.submit import (
    ReportingReceiptSubmissionClient,
    ReportingSubmissionAuthorizer,
    submit_reporting_receipts,
)

__all__ = [
    "InMemoryReportingSubmissionIntentStore",
    "PgReportingSubmissionIntentStore",
    "ReportingReceiptFailureCode",
    "ReportingReceiptOutcome",
    "ReportingReceiptSubmission",
    "ReportingReceiptSubmissionClient",
    "ReportingSubmissionAuthorizer",
    "ReportingSubmissionCode",
    "ReportingSubmissionError",
    "ReportingSubmissionIntentStore",
    "ReportingSubmissionReceipt",
    "ReportingSubmissionResult",
    "ReportingSubmissionScope",
    "prepare_reporting_receipt_submission",
    "submit_reporting_receipts",
]
