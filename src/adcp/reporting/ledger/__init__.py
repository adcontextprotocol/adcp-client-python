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

from adcp.reporting.currency import (
    ReportingCurrencyError,
    require_single_currency,
    validate_currency,
)
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
from adcp.reporting.ledger.store import (
    InMemoryReportingLedgerStore,
    LeasedConfiguration,
    LedgerConflictError,
    LedgerPage,
    ReportingLedgerStore,
    ReportingRowPage,
    check_issue_state_transition,
    issue_is_retirable,
    reject_reserved_authoritative_party,
)

__all__ = [
    "ConsumerMismatch",
    "ConsumerStatusDisabledError",
    "ConsumerStatusIngest",
    "ConsumerStatusRecord",
    "ConsumerStatusValue",
    "CurrencyResolver",
    "FixedCurrencyResolver",
    "InMemoryReportingLedgerStore",
    "LeasedConfiguration",
    "LedgerChange",
    "LedgerConflictError",
    "LedgerPage",
    "LedgerSnapshot",
    "ObligationProjection",
    "PgReportingLedgerStore",
    "ProducerOfferings",
    "ReportingAdjustmentRecord",
    "ReportingConfiguration",
    "ReportingConfigurationGenerationKey",
    "ReportingCurrencyError",
    "ReportingDefinitionBinding",
    "ReportingDeliveryEscalation",
    "ReportingFinality",
    "ReportingHealth",
    "ReportingIssue",
    "ReportingIssueLifecycle",
    "ReportingIssueStateValue",
    "ReportingLedgerStore",
    "ReportingMismatchCode",
    "ReportingObligationRecord",
    "ReportingPeriodBoundary",
    "ReportingProducer",
    "ReportingProductionStatus",
    "ReportingRevisionRecord",
    "ReportingRowPage",
    "ReportingScheduleSpec",
    "ReportingStatusCaller",
    "ReportingStatusHandler",
    "ReportingStatusView",
    "WorkerTurn",
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
    "project_consumer_mismatch",
    "project_obligation_health",
    "reject_reserved_authoritative_party",
    "require_single_currency",
    "revision_content_sha256",
    "stale_received_grace_deadline",
    "validate_currency",
]


def __getattr__(name: str) -> object:
    """Load the Postgres store lazily so ``psycopg`` stays an optional extra."""
    if name == "PgReportingLedgerStore":
        from adcp.reporting.ledger.pg import PgReportingLedgerStore

        return PgReportingLedgerStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
