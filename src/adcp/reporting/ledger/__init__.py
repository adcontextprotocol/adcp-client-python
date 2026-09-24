"""Reliable Reporting ``reporting.core``, seller side.

Core makes one question machine-answerable: *do I have definitive reporting for
this period -- and if not, whose problem is it?*  Four ideas do the work.

1. **Obligations exist before reports.**  At each period close the seller
   freezes the scope and commits an obligation whether or not source data
   exists, so a missing first report is detectable rather than silent.
2. **A zero-row report differs from no report.**  An empty period commits a
   revision like any other; absence means something is wrong.
3. **Revisions are immutable.**  A provisional restatement is a new snapshot
   superseding the old one.  An official revision is terminal; later
   corrections are explicit accounting adjustments.
4. **``get_reporting_status`` answers "where am I?"** -- one authoritative read
   over the ledger, summarized by five health states.

What you assemble
-----------------

::

    from psycopg_pool import AsyncConnectionPool
    from adcp.reporting.ledger import (
        PgReportingLedgerStore, ProducerOfferings, ReportingProducer,
        ReportingStatusCaller, ReportingStatusHandler,
    )

    store = PgReportingLedgerStore(pool=pool)
    await store.create_schema()

    producer = ReportingProducer(
        source=my_executor,                    # adcp.reporting.source
        offerings=ProducerOfferings(official_offering_id="DAILY_OFFICIAL_V1"),
        store=store,
    )
    await producer.run_worker()                # from cron, a loop, a supervisor

    status = ReportingStatusHandler(store)
    payload = await status.handle(
        {"view": "summary"},
        caller=ReportingStatusCaller(account_id=..., consumer_id=...),
    )

No Temporal, no Celery, no Redis lock.  Durability lives in the store; the
worker is a stateless leased turn, and the status handler is a pure projection
you can mount from :mod:`adcp.server` or any framework.

Health is derived, never stored -- a pure function of obligations, revisions,
and the snapshot clock, so it cannot go stale and two readers of one snapshot
cannot disagree.

Opt-in surface
--------------

:mod:`adcp.reporting.ledger.consumer_status` implements ``sync_reporting_status``,
an additive opt-in extension in AdCP 3.2.0-rc.2 that is **off by default**.
See that module for what turning it on commits you to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adcp.reporting.currency import (
    ReportingCurrencyError,
    require_single_currency,
    validate_currency,
)
from adcp.reporting.evidence import ReportingCanonicalDigest
from adcp.reporting.ledger.consumer_status import (
    ConsumerMismatch,
    ConsumerStatusDisabledError,
    ConsumerStatusIngest,
    consumer_mismatch_issue_key,
    consumer_statement_conflicts,
    current_consumer_statement,
    project_consumer_mismatch,
    stale_received_grace_deadline,
)
from adcp.reporting.ledger.delivery import (
    InMemoryReportingReconciliationStore,
    ReportingDestinationStore,
    ReportingMaterializationStore,
    ReportingMaterializationView,
    ReportingReceiptStore,
    ReportingReconciliationSnapshot,
    ReportingReconciliationStore,
    adjustment_to_wire,
    materialization_to_wire,
    receipt_to_wire,
    revision_to_wire,
)
from adcp.reporting.ledger.delivery_changes import (
    ReportingReconciliationChange,
    ReportingReconciliationChangeStore,
    ReportingReconciliationCheckpoint,
    ReportingReconciliationCursor,
    ReportingReconciliationFeedStore,
    ReportingReconciliationFilter,
    ReportingReconciliationPage,
    ReportingReconciliationSnapshotToken,
)
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingControlTotalRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingDeliveryScope,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationCheck,
    ReportingMaterializationKey,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
    ReportingPhysicalChecksum,
    ReportingReceiptKey,
    ReportingReceiptRecord,
    ReportingReconciliationRecordKind,
    ReportingResourceRecord,
    ReportingRevisionReceiptRecord,
    ReportingVerificationRecord,
)
from adcp.reporting.ledger.health import (
    ObligationProjection,
    aggregate_reporting_health,
    current_required_revision,
    issue_id_for,
    issue_id_for_occurrence,
    project_obligation_health,
)
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    ConsumerStatusValue,
    LedgerChange,
    LedgerSnapshot,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingDefinitionBinding,
    ReportingDeliveryEscalation,
    ReportingFinality,
    ReportingHealth,
    ReportingIssue,
    ReportingIssueLifecycle,
    ReportingIssueStateValue,
    ReportingMismatchCode,
    ReportingObligationRecord,
    ReportingPeriodBoundary,
    ReportingProductionStatus,
    ReportingRevisionRecord,
    ReportingScheduleSpec,
    derive_period,
    iso_duration_to_timedelta,
)
from adcp.reporting.ledger.producer import (
    CurrencyResolver,
    FixedCurrencyResolver,
    ProducerOfferings,
    ReportingProducer,
    WorkerTurn,
    revision_content_sha256,
)
from adcp.reporting.ledger.status import (
    ReportingStatusCaller,
    ReportingStatusHandler,
    ReportingStatusView,
)
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusLifecycleIntent,
    StatusProjectionInput,
    StatusProjectionResult,
    project_status_scope,
)
from adcp.reporting.ledger.status_snapshot import ReportingStatusParticipant
from adcp.reporting.ledger.store import (
    InMemoryReportingLedgerStore,
    LeasedConfiguration,
    LedgerConflictError,
    LedgerPage,
    ReportingLedgerStore,
    ReportingRowPage,
    RestatementCheckpoint,
    RestatementCheckpointStore,
    check_issue_state_transition,
    issue_is_retirable,
    reject_reserved_authoritative_party,
)
from adcp.reporting.revision_selection import (
    REPORTING_SELECTOR_VERSION,
    ReportingRevisionCorrupt,
    ReportingRevisionNotReady,
    ReportingRevisionSelected,
    ReportingRevisionSelection,
    select_reporting_revision,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.delivery_pg import PgReportingReconciliationStore
    from adcp.reporting.ledger.pg import PgReportingLedgerStore
    from adcp.reporting.ledger.status_server import (
        ReportingStatusCallerResolver,
        ReportingStatusNotificationHandler,
    )

__all__ = [
    "REPORTING_SELECTOR_VERSION",
    "ReportingRevisionCorrupt",
    "ReportingRevisionNotReady",
    "ReportingRevisionSelected",
    "ReportingRevisionSelection",
    "select_reporting_revision",
    "ReportingStatusCallerResolver",
    "ReportingStatusNotificationHandler",
    "ReportingStatusSnapshot",
    "StatusLifecycleIntent",
    "StatusProjectionInput",
    "StatusProjectionResult",
    "project_status_scope",
    "ReportingStatusParticipant",
    "ConsumerMismatch",
    "ConsumerStatusDisabledError",
    "ConsumerStatusIngest",
    "ConsumerStatusRecord",
    "ConsumerStatusValue",
    "CurrencyResolver",
    "FixedCurrencyResolver",
    "InMemoryReportingLedgerStore",
    "InMemoryReportingReconciliationStore",
    "LeasedConfiguration",
    "LedgerChange",
    "LedgerConflictError",
    "LedgerPage",
    "LedgerSnapshot",
    "ObligationProjection",
    "PgReportingLedgerStore",
    "PgReportingReconciliationStore",
    "ProducerOfferings",
    "ReportingAdjustmentReceiptRecord",
    "ReportingAdjustmentRecord",
    "ReportingCanonicalDigest",
    "ReportingConfiguration",
    "ReportingConfigurationGenerationKey",
    "ReportingControlTotalRecord",
    "ReportingCurrencyError",
    "ReportingDefinitionBinding",
    "ReportingDeliveryEscalation",
    "ReportingDeliveryPrincipal",
    "ReportingDeliveryRecord",
    "ReportingDeliveryScope",
    "ReportingDestinationBinding",
    "ReportingDestinationStore",
    "ReportingFinality",
    "ReportingHealth",
    "ReportingIssue",
    "ReportingIssueLifecycle",
    "ReportingIssueStateValue",
    "ReportingLedgerStore",
    "ReportingMaterializationAttempt",
    "ReportingMaterializationCheck",
    "ReportingMaterializationKey",
    "ReportingMaterializationRecord",
    "ReportingMaterializationStore",
    "ReportingMaterializationView",
    "ReportingMismatchCode",
    "ReportingObligationDeliveryRecord",
    "ReportingObligationRecord",
    "ReportingPeriodBoundary",
    "ReportingPhysicalChecksum",
    "ReportingProducer",
    "ReportingProductionStatus",
    "ReportingReceiptKey",
    "ReportingReceiptRecord",
    "ReportingReceiptStore",
    "ReportingReconciliationSnapshot",
    "ReportingReconciliationChange",
    "ReportingReconciliationChangeStore",
    "ReportingReconciliationCheckpoint",
    "ReportingReconciliationCursor",
    "ReportingReconciliationFeedStore",
    "ReportingReconciliationFilter",
    "ReportingReconciliationPage",
    "ReportingReconciliationSnapshotToken",
    "ReportingReconciliationRecordKind",
    "ReportingReconciliationStore",
    "ReportingResourceRecord",
    "ReportingRevisionReceiptRecord",
    "ReportingRevisionRecord",
    "ReportingRowPage",
    "RestatementCheckpoint",
    "RestatementCheckpointStore",
    "ReportingScheduleSpec",
    "ReportingStatusCaller",
    "ReportingStatusHandler",
    "ReportingStatusView",
    "ReportingVerificationRecord",
    "WorkerTurn",
    "adjustment_to_wire",
    "aggregate_reporting_health",
    "derive_period",
    "check_issue_state_transition",
    "consumer_mismatch_issue_key",
    "consumer_statement_conflicts",
    "current_consumer_statement",
    "current_required_revision",
    "iso_duration_to_timedelta",
    "issue_id_for",
    "issue_is_retirable",
    "issue_id_for_occurrence",
    "materialization_to_wire",
    "project_consumer_mismatch",
    "project_obligation_health",
    "receipt_to_wire",
    "reject_reserved_authoritative_party",
    "require_single_currency",
    "revision_content_sha256",
    "revision_to_wire",
    "stale_received_grace_deadline",
    "validate_currency",
]


def __getattr__(name: str) -> object:
    """Keep core evidence imports independent of database and server adapters."""
    if name == "PgReportingLedgerStore":
        from adcp.reporting.ledger.pg import PgReportingLedgerStore

        return PgReportingLedgerStore
    if name == "PgReportingReconciliationStore":
        from adcp.reporting.ledger.delivery_pg import PgReportingReconciliationStore

        return PgReportingReconciliationStore
    if name in {"ReportingStatusCallerResolver", "ReportingStatusNotificationHandler"}:
        from adcp.reporting.ledger import status_server

        return getattr(status_server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
