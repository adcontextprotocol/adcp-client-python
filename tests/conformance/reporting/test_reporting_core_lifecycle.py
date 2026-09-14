"""The whole Reliable Reporting Core loop, end to end, over real Postgres.

Every other test in this suite checks one piece. This one checks that the
pieces compose: a simulated reporting source drives
:class:`~adcp.reporting.ledger.ReportingProducer`, which writes a
:class:`~adcp.reporting.ledger.pg.PgReportingLedgerStore`, which
:class:`~adcp.reporting.ledger.ReportingStatusHandler` projects onto the wire,
which the SDK's **own buyer-side** ``reconcile_reporting_core`` then reconciles.

That last hop is the point. The buyer half of this SDK was written against the
spec, not against this producer; if the seller's projection is wrong, the
buyer's reconciler says so, in the same terms a real buyer would. The
projection is additionally validated against the generated
``GetReportingStatusResponse`` -- so "the seller emits valid AdCP" is asserted,
not assumed.

The narrative, in order:

1. install a reporting configuration
2. close a period -- the obligation is committed before any source data exists
3. the obligation is visible to the buyer, and reconciliation is *not* definitive
4. the source answers; an immutable revision is committed
5. the buyer does an exact read and independently recomputes the binding
6. a genuinely empty period commits a zero-row revision and satisfies its obligation
7. a period whose source never answers has no revision
8. that period reads ``delayed`` while automated recovery runs
9. and ``action_required`` once the recovery window elapses
10. a restatement supersedes the prior snapshot rather than editing it

Requires ``ADCP_PG_TEST_URL``; skips without it.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

psycopg = pytest.importorskip("psycopg")
psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping the reporting lifecycle test",
        allow_module_level=True,
    )

from adcp.reporting import (  # noqa: E402
    ExpectedReportingPeriod,
    ReportingReconciliationError,
    reconcile_reporting_core,
)
from adcp.reporting.fixtures import redacted_capabilities  # noqa: E402
from adcp.reporting.inline_source import (  # noqa: E402
    InlineFetchResult,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.ledger import (  # noqa: E402
    LedgerConflictError,
    ProducerOfferings,
    ReportingConfiguration,
    ReportingDefinitionBinding,
    ReportingProducer,
    ReportingScheduleSpec,
    ReportingStatusCaller,
    ReportingStatusHandler,
    derive_period,
    revision_content_sha256,
)
from adcp.reporting.ledger.models import first_ordinal_after  # noqa: E402
from adcp.reporting.ledger.pg import PgReportingLedgerStore  # noqa: E402
from adcp.types import (  # noqa: E402
    GetReportingStatusRequest,
    GetReportingStatusResponse,
    SyncReportingStatusRequest,
    SyncReportingStatusResponse,
)
from adcp.types.core import TaskResult  # noqa: E402

ACCOUNT = "acct_lifecycle"
CALLER = ReportingStatusCaller(account_id=ACCOUNT, consumer_id="buyer_lifecycle")
CONFIG_ID = "hourly_delivery"
DEFINITION_ID = "hourly_delivery_v1"

#: Hourly periods with a one-hour SLA and a two-hour recovery window, so the
#: whole waiting -> delayed -> action_required walk fits in a few simulated
#: hours rather than needing a day of test clock.
SCHEDULE = ReportingScheduleSpec(period_duration="PT1H", delivery_sla="PT1H", alignment="utc")
ACTIVATED_AT = datetime(2026, 9, 1, 0, 20, tzinfo=timezone.utc)
RECOVERY_WINDOW = timedelta(hours=2)

DEFINITION = ReportingDefinitionBinding(
    report_definition_uri="https://contracts.example.test/reporting/hourly-delivery-v1",
    report_definition_sha256="a" * 64,
    schema_version="1.0.0",
    schema_uri="https://contracts.example.test/reporting/hourly-delivery-v1/schema",
    schema_sha256="b" * 64,
)

_TABLES = (
    "reporting_ledger_changes",
    "reporting_consumer_statuses",
    "reporting_adjustments",
    "reporting_revision_rows",
    "reporting_revisions",
    "reporting_obligations",
    "reporting_configurations",
)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class SimulatedSource:
    """A reporting source whose answers the test scripts, period by period.

    Answers are keyed by period start so a test can say "this hour delivered,
    that hour was genuinely empty, and that other hour the source never
    answered" -- the three cases Core must keep distinct.
    """

    def __init__(self) -> None:
        self.answers: dict[datetime, Any] = {}
        self.calls: list[datetime] = []

    def set(self, period_start: datetime, answer: Any) -> None:
        self.answers[period_start] = answer

    def __call__(self, request: Any) -> Any:
        start = request.period.start.astimezone(timezone.utc)
        self.calls.append(start)
        if start not in self.answers:
            # Unscripted period: the source has nothing to say yet, which is
            # "not ready", not "zero".
            return None
        return self.answers[start]


class LedgerStatusClient:
    """Adapts the seller's status handler to the buyer's client Protocol.

    This is the seam a real deployment crosses over MCP or A2A. Doing it
    in-process keeps the test about the *contract* rather than about transport,
    while still forcing the seller's payload through the generated response
    model -- so a projection that would not validate on the wire fails here.
    """

    def __init__(self, handler: ReportingStatusHandler, caller: ReportingStatusCaller) -> None:
        self._handler = handler
        self._caller = caller
        self.validated: list[GetReportingStatusResponse] = []

    async def get_reporting_status(
        self, request: GetReportingStatusRequest
    ) -> TaskResult[GetReportingStatusResponse]:
        payload = request.model_dump(mode="json", exclude_none=True)
        result = await self._handler.handle(payload, caller=self._caller)
        response = GetReportingStatusResponse.model_validate(result)
        self.validated.append(response)
        return TaskResult(success=True, data=response, status="completed")


@pytest.fixture()
async def ledger() -> AsyncIterator[PgReportingLedgerStore]:
    schema = f"adcp_life_{secrets.token_hex(6)}"
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=4, open=False
    ) as bootstrap:
        await bootstrap.open()
        async with bootstrap.connection() as connection:
            await connection.execute(f"CREATE SCHEMA {schema}")

    async with psycopg_pool.AsyncConnectionPool(
        f"{TEST_URL}?options=-csearch_path%3D{schema}", min_size=2, max_size=8, open=False
    ) as pool:
        await pool.open()
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        try:
            yield store
        finally:
            async with pool.connection() as connection:
                for table in _TABLES:
                    await connection.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=2, open=False
    ) as cleanup:
        await cleanup.open()
        async with cleanup.connection() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _period(offset: int):
    base = first_ordinal_after(SCHEDULE, account_timezone="UTC", activated_at=ACTIVATED_AT)
    return derive_period(SCHEDULE, account_timezone="UTC", ordinal=base + offset)


def _configuration(required_finality: str = "snapshot") -> ReportingConfiguration:
    return ReportingConfiguration(
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        account_id=ACCOUNT,
        report_definition_id=DEFINITION_ID,
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=SCHEDULE,
        required_finality=required_finality,  # type: ignore[arg-type]
        activated_at=ACTIVATED_AT,
        media_buy_ids=("mb_lifecycle",),
        automated_recovery_window=RECOVERY_WINDOW,
        definition=DEFINITION,
    )


def _expected_periods(count: int) -> list[ExpectedReportingPeriod]:
    """What the *buyer* independently derives from the retained configuration.

    Deliberately computed from the schedule rather than read back from the
    seller: the seller's obligation list cannot prove it did not omit one.
    """
    return [
        ExpectedReportingPeriod(
            delivery_config_id=CONFIG_ID,
            delivery_config_version=1,
            report_definition_id=DEFINITION_ID,
            feed_purpose="analytics",
            reporting_profile="paid_media_delivery",
            media_buy_ids=("mb_lifecycle",),
            period_start=_period(index).start.isoformat(),
            period_end=_period(index).end.isoformat(),
        )
        for index in range(count)
    ]


def _producer(
    store: PgReportingLedgerStore, source: SimulatedSource, *, now: datetime
) -> tuple[ReportingProducer, InlineReportingSource]:
    staging = InMemoryStagingStore()
    executor = InlineReportingSource(
        capabilities=redacted_capabilities(),
        fetch=source,
        staging=staging,
        seals=InMemorySealStore(),
        clock=lambda: now,
    )
    producer = ReportingProducer(
        source=executor,
        offerings=ProducerOfferings(
            snapshot_offering_id="FIXTURE_PULSE_V1",
            official_offering_id="FIXTURE_DAILY_OFFICIAL_V1",
            publication_namespace="reporting-source:fixture",
            requested_metrics=("impressions", "spend"),
            requested_dimensions=("campaign_id",),
            source_scope=dict(redacted_capabilities().source_scope),
        ),
        store=store,
        object_reader=staging,
        clock=lambda: now,
    )
    return producer, executor


async def _run_worker_at(
    store: PgReportingLedgerStore, source: SimulatedSource, *, now: datetime
) -> Any:
    producer, _ = _producer(store, source, now=now)
    return await producer.run_worker()


async def _reconcile(
    store: PgReportingLedgerStore, *, expected: list[ExpectedReportingPeriod]
) -> Any:
    client = LedgerStatusClient(ReportingStatusHandler(store), CALLER)
    return await reconcile_reporting_core(
        client,
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": ACCOUNT}, "view": "periods"}
        ),
        expected_periods=expected,
    )


# --------------------------------------------------------------------------
# The lifecycle
# --------------------------------------------------------------------------


async def test_reporting_core_lifecycle(ledger: PgReportingLedgerStore) -> None:
    source = SimulatedSource()
    configuration = _configuration()

    # 1. Install the configuration. The clock starts here, not at media-buy
    #    acceptance.
    await ledger.put_configuration(configuration)
    installed = await ledger.list_configurations(account_id=ACCOUNT)
    assert len(installed) == 1
    assert installed[0].definition == DEFINITION

    first, second, third = _period(0), _period(1), _period(2)
    assert first.start == datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
    assert first.expected_at == datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)

    # 2 & 3. Close the first period with the source deliberately silent. The
    #        obligation is committed anyway -- that is what makes a missing
    #        first report detectable instead of invisible.
    turn = await _run_worker_at(ledger, source, now=first.end + timedelta(minutes=1))
    assert len(turn.obligations_committed) == 1
    assert turn.revisions_committed == []

    obligation = await ledger.find_obligation(
        account_id=ACCOUNT,
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        period_start=first.start,
        period_end=first.end,
    )
    assert obligation is not None
    assert obligation.scope_resolved_at == first.end

    client = LedgerStatusClient(ReportingStatusHandler(ledger), CALLER)
    visible = await client.get_reporting_status(
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": ACCOUNT}, "view": "periods"}
        )
    )
    assert visible.data is not None
    assert [item.reporting_obligation_id for item in (visible.data.periods or [])] == [
        obligation.reporting_obligation_id
    ]
    # The buyer's own reconciler: an obligation with no revision is not
    # definitive reporting, no matter how healthy the seller feels.
    outcome = await _reconcile(ledger, expected=_expected_periods(1))
    assert outcome.definitive is False
    assert outcome.missing_expected_periods == []

    # 4. The source answers. An immutable revision is committed.
    source.set(
        first.start,
        InlineFetchResult(
            rows=[
                {
                    "media_buy_id": "mb_lifecycle",
                    "campaign_id": "cmp_1",
                    "impressions": 1200,
                    "spend": "4.80",
                }
            ],
            data_through=first.end,
        ),
    )
    # Deliberately before the second period closes: this step is about the
    # first period's revision, not about how many periods have elapsed.
    turn = await _run_worker_at(ledger, source, now=first.end + timedelta(minutes=30))
    assert turn.obligations_committed == []
    assert len(turn.revisions_committed) == 1
    revision_id = turn.revisions_committed[0]

    revision = await ledger.get_revision(account_id=ACCOUNT, reporting_revision_id=revision_id)
    assert revision is not None
    assert revision.row_count == 1
    assert revision.finality == "snapshot"

    reconciled = await _reconcile(ledger, expected=_expected_periods(1))
    assert reconciled.definitive is True, reconciled.obligations
    assert reconciled.obligations[0].reporting_revision_id == revision_id

    # 5. The exact read, verified independently. A fresh date-range pull is
    #    explicitly *not* a substitute: it may have changed since publication.
    walked: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = await ledger.read_revision_rows(
            account_id=ACCOUNT, reporting_revision_id=revision_id, cursor=cursor, limit=1
        )
        walked.extend(page.rows)
        if not page.has_more:
            break
        cursor = page.cursor
    assert walked == [
        {
            "media_buy_id": "mb_lifecycle",
            "campaign_id": "cmp_1",
            "impressions": 1200,
            "spend": "4.80",
        }
    ]
    assert (
        revision_content_sha256(
            reporting_revision_id=revision_id,
            row_count=len(walked),
            control_totals=revision.control_totals,
            reporting_rows=walked,
        )
        == revision.revision_content_sha256
    )

    # 6. A genuinely empty period. A zero-row revision is a revision: it
    #    satisfies its obligation exactly like any other.
    source.set(second.start, InlineFetchResult(rows=[], data_through=second.end))
    turn = await _run_worker_at(ledger, source, now=second.end + timedelta(minutes=30))
    assert len(turn.obligations_committed) == 1
    assert len(turn.revisions_committed) == 1

    zero_obligation = await ledger.find_obligation(
        account_id=ACCOUNT,
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        period_start=second.start,
        period_end=second.end,
    )
    assert zero_obligation is not None
    zero_revisions = await ledger.list_revisions(
        account_id=ACCOUNT, reporting_obligation_id=zero_obligation.reporting_obligation_id
    )
    assert [item.row_count for item in zero_revisions] == [0]

    settled = await _reconcile(ledger, expected=_expected_periods(2))
    assert settled.definitive is True, settled.obligations

    # 7. A period the source never answers for. The obligation exists; no
    #    revision does. Absence is represented by absence, never by a zero.
    turn = await _run_worker_at(ledger, source, now=third.end + timedelta(minutes=1))
    silent_obligation = await ledger.find_obligation(
        account_id=ACCOUNT,
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        period_start=third.start,
        period_end=third.end,
    )
    assert silent_obligation is not None
    assert (
        await ledger.list_revisions(
            account_id=ACCOUNT, reporting_obligation_id=silent_obligation.reporting_obligation_id
        )
        == ()
    )
    assert turn.slices_failed == [silent_obligation.reporting_obligation_id]

    # The zero-row period and the silent period are distinguishable, which is
    # the entire operational point of the tier.
    assert zero_revisions[0].row_count == 0
    assert (
        await ledger.list_revisions(
            account_id=ACCOUNT, reporting_obligation_id=silent_obligation.reporting_obligation_id
        )
        != zero_revisions
    )

    # 8 & 9. That period's health walks the clock: waiting, then delayed while
    #        automated recovery runs, then action_required. A dead feed is
    #        never parked in delayed.
    handler = ReportingStatusHandler(ledger)
    states = {}
    for label, moment in (
        ("before_due", third.expected_at - timedelta(minutes=1)),
        ("delayed", third.expected_at + timedelta(minutes=1)),
        ("escalated", third.expected_at + RECOVERY_WINDOW + timedelta(minutes=1)),
    ):
        states[label] = await _health_of(handler, silent_obligation, at=moment)
    assert states["before_due"] == ("waiting", None)
    assert states["delayed"] == ("delayed", "REPORT_OVERDUE")
    assert states["escalated"] == ("action_required", "REPORT_OVERDUE")

    # A buyer reconciling a scope containing that period is not definitive.
    assert (await _reconcile(ledger, expected=_expected_periods(3))).definitive is False

    # 10. A restatement supersedes the first period's snapshot rather than
    #     editing it. Both revisions remain; only the new one is current.
    source.set(
        first.start,
        InlineFetchResult(
            rows=[
                {
                    "media_buy_id": "mb_lifecycle",
                    "campaign_id": "cmp_1",
                    "impressions": 1500,
                    "spend": "6.00",
                }
            ],
            data_through=first.end,
        ),
    )
    restated = await _restate(ledger, source, obligation, now=first.expected_at + RECOVERY_WINDOW)
    chain = await ledger.list_revisions(
        account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert len(chain) == 2
    assert restated.supersedes_reporting_revision_id == revision_id
    # The superseded revision is still retained and still readable: a consumer
    # that already cited it must be able to fetch exactly what it cited.
    superseded = await ledger.read_revision_rows(
        account_id=ACCOUNT, reporting_revision_id=revision_id
    )
    assert superseded.rows[0]["impressions"] == 1200

    current = await _current_revision_id(handler, obligation)
    assert current == restated.reporting_revision_id


async def _health_of(
    handler: ReportingStatusHandler, obligation: Any, *, at: datetime
) -> tuple[str, str | None]:
    """Read one obligation's health at a chosen ledger boundary.

    ``ledger_as_of`` is the store's clock, so the boundary is moved by swapping
    the store's clock rather than by passing an instant into the projection --
    the same seam a real deployment has, where the database decides.
    """
    from adcp.reporting.ledger.health import project_obligation_health

    revisions = await handler._store.list_revisions(  # noqa: SLF001 - deliberate white box
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )
    projection = project_obligation_health(
        obligation, revisions, ledger_as_of=at, scope_closed=True
    )
    code = projection.issues[0].code if projection.issues else None
    return projection.health, code


async def _current_revision_id(handler: ReportingStatusHandler, obligation: Any) -> str | None:
    from adcp.reporting.ledger.health import project_obligation_health

    revisions = await handler._store.list_revisions(  # noqa: SLF001 - deliberate white box
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
    )
    projection = project_obligation_health(
        obligation,
        revisions,
        ledger_as_of=obligation.period.expected_at + timedelta(days=1),
        scope_closed=True,
    )
    return (
        projection.current_revision.reporting_revision_id
        if projection.current_revision is not None
        else None
    )


async def _restate(
    store: PgReportingLedgerStore, source: SimulatedSource, obligation: Any, *, now: datetime
) -> Any:
    """Ask for a new observation of an already-satisfied snapshot obligation.

    A settled period is left alone by an ordinary worker turn, so a
    restatement is explicit. The observation ordinal advances with the
    committed revision count, which is what gives this acquisition a distinct
    source execution key rather than replaying the sealed original.
    """
    producer, _ = _producer(store, source, now=now)
    restated = await producer.acquire_obligation(
        _configuration(), obligation, restate=True, now=now
    )
    assert restated is not None
    return restated


# --------------------------------------------------------------------------
# The buyer's denominator
# --------------------------------------------------------------------------


async def test_a_buyer_detects_a_period_the_seller_never_obligated(
    ledger: PgReportingLedgerStore,
) -> None:
    """The reason the buyer derives its own expectations.

    A seller that simply omits a period returns a complete-looking, internally
    consistent ledger. Only the buyer's independently derived denominator
    catches it.
    """
    source = SimulatedSource()
    await ledger.put_configuration(_configuration())
    first = _period(0)
    source.set(first.start, InlineFetchResult(rows=[], data_through=first.end))
    # Stop before the second period closes, so the seller's ledger is complete
    # and internally consistent -- exactly the shape that hides an omission.
    await _run_worker_at(ledger, source, now=first.end + timedelta(minutes=30))

    # The seller obligated one period; the buyer expects two.
    settled = await _reconcile(ledger, expected=_expected_periods(1))
    assert settled.definitive is True

    gap = await _reconcile(ledger, expected=_expected_periods(2))
    assert gap.definitive is False
    assert len(gap.missing_expected_periods) == 1
    assert gap.missing_expected_periods[0].period_start == _period(1).start.isoformat()


async def test_core_reconciliation_refuses_a_managed_delivery_ledger(
    ledger: PgReportingLedgerStore,
) -> None:
    """Core must not accidentally activate a higher tier.

    ``reconcile_reporting_core`` rejects destination, materialization, and
    receipt records rather than quietly ignoring them -- a buyer that thinks it
    is doing Core must not end up trusting managed-delivery evidence it never
    verified.
    """
    source = SimulatedSource()
    await ledger.put_configuration(_configuration())
    first = _period(0)
    source.set(first.start, InlineFetchResult(rows=[], data_through=first.end))
    await _run_worker_at(ledger, source, now=first.expected_at)

    class ManagedHandler(ReportingStatusHandler):
        async def handle(self, request: dict[str, Any], *, caller: Any) -> dict[str, Any]:
            payload = await super().handle(request, caller=caller)
            for period in payload.get("periods", []):
                period["destination_ref"] = "dest_1"
            return payload

    client = LedgerStatusClient(ManagedHandler(ledger), CALLER)
    with pytest.raises(ReportingReconciliationError) as error:
        await reconcile_reporting_core(
            client,
            GetReportingStatusRequest.model_validate(
                {"account": {"account_id": ACCOUNT}, "view": "periods"}
            ),
            expected_periods=_expected_periods(1),
        )
    assert error.value.code == "MANAGED_DELIVERY_NOT_ENABLED"


async def test_the_status_projection_validates_as_adcp_wire(
    ledger: PgReportingLedgerStore,
) -> None:
    """Every view the seller emits parses as the generated response model.

    ``GetReportingStatusResponse`` discriminates the summary, periods, and
    revision shapes, so this also asserts the projection does not leak fields
    across views.
    """
    source = SimulatedSource()
    await ledger.put_configuration(_configuration())
    first = _period(0)
    source.set(
        first.start,
        InlineFetchResult(
            rows=[{"media_buy_id": "mb_lifecycle", "impressions": 7, "spend": "0.03"}],
            data_through=first.end,
        ),
    )
    await _run_worker_at(ledger, source, now=first.expected_at)
    handler = ReportingStatusHandler(ledger)

    summary = GetReportingStatusResponse.model_validate(
        await handler.handle({"view": "summary"}, caller=CALLER)
    )
    assert summary.view is not None
    assert summary.coverage is not None

    periods_payload = await handler.handle({"view": "periods"}, caller=CALLER)
    periods = GetReportingStatusResponse.model_validate(periods_payload)
    assert periods.periods and periods.revisions
    published = periods.revisions[0]
    assert str(published.report_definition_uri) == DEFINITION.report_definition_uri
    assert published.schema_sha256 == DEFINITION.schema_sha256
    assert published.data_through_precision is not None

    exact = GetReportingStatusResponse.model_validate(
        await handler.handle(
            {
                "view": "revision",
                "reporting_revision_id": published.reporting_revision_id,
            },
            caller=CALLER,
        )
    )
    assert exact.revision is not None
    assert exact.revision.revision_content_sha256 == published.revision_content_sha256


# --------------------------------------------------------------------------
# sync_reporting_status: gated on rc.2
# --------------------------------------------------------------------------


async def test_the_consumer_status_loop_closes_over_the_lifecycle(
    ledger: PgReportingLedgerStore,
) -> None:
    """The buyer tells the seller what it could actually consume.

    Driven through the generated ``SyncReportingStatusRequest`` and asserted to
    come back as a valid ``SyncReportingStatusResponse``, the same
    generated-type round trip
    :func:`test_the_status_projection_validates_as_adcp_wire` does for
    ``get_reporting_status`` -- so "the seller speaks valid AdCP here too" is
    asserted, not assumed.
    """
    from adcp.reporting.ledger import ConsumerStatusIngest

    source = SimulatedSource()
    await ledger.put_configuration(_configuration())
    first = _period(0)
    source.set(
        first.start,
        InlineFetchResult(
            rows=[{"media_buy_id": "mb_lifecycle", "impressions": 3, "spend": "0.01"}],
            data_through=first.end,
        ),
    )
    await _run_worker_at(ledger, source, now=first.expected_at)
    obligation = await ledger.find_obligation(
        account_id=ACCOUNT,
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        period_start=first.start,
        period_end=first.end,
    )
    assert obligation is not None
    revisions = await ledger.list_revisions(
        account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
    )
    revision = revisions[0]

    ingest = ConsumerStatusIngest(ledger, enabled=True)
    statement = {
        "reporting_status_id": "status_lifecycle_0000001",
        "delivery_config_id": CONFIG_ID,
        "delivery_config_version": 1,
        "report_definition_id": DEFINITION_ID,
        "period": {
            "start": first.start.isoformat().replace("+00:00", "Z"),
            "end": first.end.isoformat().replace("+00:00", "Z"),
            "source_timezone": "UTC",
        },
        "consumer_status": "received",
        "status_as_of": first.expected_at.isoformat().replace("+00:00", "Z"),
        "reporting_obligation_id": obligation.reporting_obligation_id,
        "reporting_revision_id": revision.reporting_revision_id,
        "observed_revision_content_sha256": revision.revision_content_sha256,
    }
    # Through the generated request model, so a batch this SDK would not
    # accept on the wire cannot pass here either.
    batch = SyncReportingStatusRequest.model_validate(
        {
            "account": {"account_id": ACCOUNT},
            "idempotency_key": "idem_lifecycle_00000001",
            "statuses": [statement],
        }
    )
    recorded = await ingest.handle(
        batch.model_dump(mode="json", exclude_none=True),
        account_id=ACCOUNT,
        consumer_id=CALLER.consumer_id,
    )
    assert recorded["results"][0]["result"] == "recorded"
    assert SyncReportingStatusResponse.model_validate(recorded).status == "completed"

    # A matching `received` is not a mismatch: the seller and the buyer agree.
    handler = ReportingStatusHandler(ledger, consumer_status_enabled=True)
    payload = await handler.handle({"view": "periods"}, caller=CALLER)
    assert payload["periods"][0]["issues"] == []
    assert payload["periods"][0]["current_consumer_status_id"] == "status_lifecycle_0000001"

    # The buyer then loses access. The seller still projects healthy, so only
    # this caller's view degrades -- with the diagnosed party being the buyer.
    unreadable = await ingest.handle(
        {
            "statuses": [
                {
                    # `unreadable` forbids the observed digest; the key must be
                    # absent, because an explicit null still satisfies
                    # JSON Schema's `required`.
                    **{
                        key: value
                        for key, value in statement.items()
                        if key != "observed_revision_content_sha256"
                    },
                    "reporting_status_id": "status_lifecycle_0000002",
                    "supersedes_reporting_status_id": "status_lifecycle_0000001",
                    "consumer_status": "unreadable",
                    "failure_code": "access_denied",
                }
            ]
        },
        account_id=ACCOUNT,
        consumer_id=CALLER.consumer_id,
    )
    assert unreadable["results"][0]["result"] == "recorded"
    assert SyncReportingStatusResponse.model_validate(unreadable).status == "completed"

    degraded = await handler.handle({"view": "periods"}, caller=CALLER)
    issue = degraded["periods"][0]["issues"][0]
    assert issue["code"] == "CONSUMER_STATUS_MISMATCH"
    assert issue["responsible_party"] == "buyer"
    assert degraded["periods"][0]["consumer_status_count"] == 2


# --------------------------------------------------------------------------
# AdCP 3.2.0-rc.3 hardening, over real Postgres
# --------------------------------------------------------------------------
#
# Mirrors the comply controller's `restate_after_received` semantics: restate
# only against the caller's current received revision, return the grace
# deadline, and let the caller position the clock inside or past it. Repeating
# the operation is convergent -- it reuses the committed restatement and only
# moves the boundary.


def _handler_at(ledger: PgReportingLedgerStore, boundary: datetime) -> ReportingStatusHandler:
    """A handler whose snapshot boundary stands at ``boundary``.

    The handler deliberately has no clock of its own -- ``ledger_as_of`` is the
    *store's* observation boundary, which is what makes every page of one
    cursor describe the same instant. So the boundary is moved by giving the
    store a pinned clock over the same pool, not by teaching the handler to
    take a timestamp. The seeded evidence sits in simulated time, and real
    ``now()`` is past every deadline, so without this the within-grace case
    would be untestable.
    """
    return ReportingStatusHandler(
        PgReportingLedgerStore(pool=ledger._pool, clock=lambda: boundary),  # noqa: SLF001
        consumer_status_enabled=True,
    )


async def _post_status(
    ledger: PgReportingLedgerStore, period: Any, **fields: Any
) -> dict[str, Any]:
    from adcp.reporting.ledger import ConsumerStatusIngest

    statement: dict[str, Any] = {
        "delivery_config_id": CONFIG_ID,
        "delivery_config_version": 1,
        "report_definition_id": DEFINITION_ID,
        "period": {
            "start": period.start.isoformat().replace("+00:00", "Z"),
            "end": period.end.isoformat().replace("+00:00", "Z"),
            "source_timezone": "UTC",
        },
        "status_as_of": period.expected_at.isoformat().replace("+00:00", "Z"),
        **fields,
    }
    # Through the generated request model so a batch this SDK would refuse on
    # the wire cannot pass here either.
    batch = SyncReportingStatusRequest.model_validate(
        {
            "account": {"account_id": ACCOUNT},
            "idempotency_key": f"idem_rc3_{statement['reporting_status_id'][-8:]}",
            "statuses": [statement],
        }
    )
    result = await ConsumerStatusIngest(ledger, enabled=True).handle(
        batch.model_dump(mode="json", exclude_none=True),
        account_id=ACCOUNT,
        consumer_id=CALLER.consumer_id,
    )
    assert SyncReportingStatusResponse.model_validate(result).status == "completed"
    return result


async def _seed_received_then_restate(
    ledger: PgReportingLedgerStore,
) -> tuple[Any, Any, Any, datetime]:
    """Publish, have the buyer receive it, then restate that exact revision.

    Returns ``(period, obligation, restated, grace_deadline)``. The deadline is
    the restatement's ``created_at`` plus the generation's PT1H SLA, which is
    what the controller's ``stale_received_grace_deadline`` reports.
    """
    source = SimulatedSource()
    await ledger.put_configuration(_configuration())
    period = _period(0)
    source.set(
        period.start,
        InlineFetchResult(
            rows=[{"media_buy_id": "mb_lifecycle", "impressions": 3, "spend": "0.01"}],
            data_through=period.end,
        ),
    )
    await _run_worker_at(ledger, source, now=period.expected_at)
    obligation = await ledger.find_obligation(
        account_id=ACCOUNT,
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        period_start=period.start,
        period_end=period.end,
    )
    assert obligation is not None
    revisions = await ledger.list_revisions(
        account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
    )
    received = revisions[0]

    posted = await _post_status(
        ledger,
        period,
        reporting_status_id="status_rc3_lifecycle_001",
        consumer_status="received",
        reporting_obligation_id=obligation.reporting_obligation_id,
        reporting_revision_id=received.reporting_revision_id,
        observed_revision_content_sha256=received.revision_content_sha256,
    )
    assert posted["results"][0]["result"] == "recorded", posted

    # The seller restates the revision the caller just received. Binding the
    # restatement to a real prior read is what the controller requires, rather
    # than restating into a vacuum.
    restate_at = period.expected_at + timedelta(minutes=30)
    source.set(
        period.start,
        InlineFetchResult(
            rows=[{"media_buy_id": "mb_lifecycle", "impressions": 9, "spend": "0.03"}],
            data_through=period.end,
        ),
    )
    restated = await _restate(ledger, source, obligation, now=restate_at)
    assert restated.supersedes_reporting_revision_id == received.reporting_revision_id
    grace_deadline = restated.created_at + timedelta(hours=1)
    return period, obligation, restated, grace_deadline


async def test_restate_after_received_is_delayed_within_grace(
    ledger: PgReportingLedgerStore,
) -> None:
    # within_grace. The buyer read exactly what the seller then required and
    # has not yet had a bounded chance to re-read, so this is not a page-out.
    period, _obligation, _restated, deadline = await _seed_received_then_restate(ledger)
    payload = await _handler_at(ledger, deadline - timedelta(minutes=5)).handle(
        {"view": "periods"}, caller=CALLER
    )
    record = payload["periods"][0]
    assert record["health"] == "delayed"
    issue = next(i for i in record["issues"] if i["code"] == "CONSUMER_STATUS_MISMATCH")
    assert issue["severity"] == "delayed"
    assert issue["recommended_action"] == "wait_for_retry"
    assert issue["opened_at"]
    assert issue["reporting_status_id"] == "status_rc3_lifecycle_001"
    del period


async def test_restate_after_received_escalates_past_grace(
    ledger: PgReportingLedgerStore,
) -> None:
    # past_grace. Same issue_id and opened_at as the delayed emission, so the
    # buyer ages one work item instead of two.
    _period, _obligation, _restated, deadline = await _seed_received_then_restate(ledger)
    within = await _handler_at(ledger, deadline - timedelta(minutes=5)).handle(
        {"view": "periods"}, caller=CALLER
    )
    delayed_issue = next(
        i for i in within["periods"][0]["issues"] if i["code"] == "CONSUMER_STATUS_MISMATCH"
    )
    past = await _handler_at(ledger, deadline + timedelta(minutes=1)).handle(
        {"view": "periods"}, caller=CALLER
    )
    record = past["periods"][0]
    assert record["health"] == "action_required"
    issue = next(i for i in record["issues"] if i["code"] == "CONSUMER_STATUS_MISMATCH")
    assert issue["severity"] == "action_required"
    assert issue["recommended_action"] != "wait_for_retry"
    assert issue["issue_id"] == delayed_issue["issue_id"]
    assert issue["opened_at"] == delayed_issue["opened_at"]


async def test_superseding_with_received_on_the_new_revision_returns_to_health(
    ledger: PgReportingLedgerStore,
) -> None:
    # The resolution path. resolved is reachable only because the consumer
    # superseded with a statement that agrees -- never by seller fiat.
    period, obligation, restated, deadline = await _seed_received_then_restate(ledger)
    past = deadline + timedelta(minutes=1)
    handler = _handler_at(ledger, past)
    assert (await handler.handle({"view": "periods"}, caller=CALLER))["periods"][0][
        "health"
    ] == "action_required"

    reread = await _post_status(
        ledger,
        period,
        reporting_status_id="status_rc3_lifecycle_002",
        supersedes_reporting_status_id="status_rc3_lifecycle_001",
        consumer_status="received",
        reporting_obligation_id=obligation.reporting_obligation_id,
        reporting_revision_id=restated.reporting_revision_id,
        observed_revision_content_sha256=restated.revision_content_sha256,
    )
    assert reread["results"][0]["result"] == "recorded", reread

    healthy = await handler.handle({"view": "periods"}, caller=CALLER)
    record = healthy["periods"][0]
    assert [i for i in record["issues"] if i["code"] == "CONSUMER_STATUS_MISMATCH"] == []
    assert record["health"] in {"healthy", "complete"}


async def test_a_content_mismatch_round_trips_over_postgres(
    ledger: PgReportingLedgerStore,
) -> None:
    # The code survives the durable round trip. Reading it back out of
    # Postgres is the part a schema-only upgrade cannot give you.
    source = SimulatedSource()
    await ledger.put_configuration(_configuration())
    period = _period(0)
    source.set(
        period.start,
        InlineFetchResult(
            rows=[{"media_buy_id": "mb_lifecycle", "impressions": 3, "spend": "0.01"}],
            data_through=period.end,
        ),
    )
    await _run_worker_at(ledger, source, now=period.expected_at)
    obligation = await ledger.find_obligation(
        account_id=ACCOUNT,
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        period_start=period.start,
        period_end=period.end,
    )
    assert obligation is not None
    revision = (
        await ledger.list_revisions(
            account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
        )
    )[0]

    result = await _post_status(
        ledger,
        period,
        reporting_status_id="status_rc3_mismatch_001",
        consumer_status="content_mismatch",
        reporting_obligation_id=obligation.reporting_obligation_id,
        reporting_revision_id=revision.reporting_revision_id,
        observed_revision_content_sha256=revision.revision_content_sha256,
        mismatch_code="scope_media_buy_missing",
    )
    assert result["results"][0]["result"] == "recorded", result
    assert result["results"][0]["consumer_status"]["mismatch_code"] == "scope_media_buy_missing"

    stored = await ledger.list_consumer_statuses(account_id=ACCOUNT, consumer_id=CALLER.consumer_id)
    assert [item.mismatch_code for item in stored] == ["scope_media_buy_missing"]

    handler = ReportingStatusHandler(ledger, consumer_status_enabled=True)
    payload = await handler.handle({"view": "periods"}, caller=CALLER)
    record = payload["periods"][0]
    assert record["health"] == "action_required"
    issue = next(i for i in record["issues"] if i["code"] == "CONSUMER_STATUS_MISMATCH")
    # No grace window: content that arrived and is wrong is immediate.
    assert issue["severity"] == "action_required"
    # And it survives onto the wire projection the buyer reads back.
    assert payload["consumer_statuses"][0]["mismatch_code"] == "scope_media_buy_missing"


async def test_consumer_status_pending_appears_after_the_deadline_and_clears(
    ledger: PgReportingLedgerStore,
) -> None:
    # Silence is counted, never escalated. The deadline is expected_at plus
    # the advertised automated_recovery_window_seconds, not scope close.
    source = SimulatedSource()
    await ledger.put_configuration(_configuration())
    period = _period(0)
    source.set(
        period.start,
        InlineFetchResult(
            rows=[{"media_buy_id": "mb_lifecycle", "impressions": 3, "spend": "0.01"}],
            data_through=period.end,
        ),
    )
    await _run_worker_at(ledger, source, now=period.expected_at)
    obligation = await ledger.find_obligation(
        account_id=ACCOUNT,
        delivery_config_id=CONFIG_ID,
        delivery_config_version=1,
        period_start=period.start,
        period_end=period.end,
    )
    assert obligation is not None
    revision = (
        await ledger.list_revisions(
            account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
        )
    )[0]
    before = await _handler_at(ledger, period.expected_at + timedelta(minutes=1)).handle(
        {"view": "summary"}, caller=CALLER
    )
    assert before["obligation_counts"]["consumer_status_pending"] == 0
    assert before["issues"] == []

    deadline = period.expected_at + RECOVERY_WINDOW
    after = await _handler_at(ledger, deadline).handle({"view": "summary"}, caller=CALLER)
    assert after["obligation_counts"]["consumer_status_pending"] == 1
    # Counted, not escalated: no issue is raised over the caller's silence...
    assert [i for i in after["issues"] if i["code"] == "CONSUMER_STATUS_MISMATCH"] == []
    # ...and health is byte-identical to the same read with the loop off, which
    # is the invariant that matters: a buyer that has not integrated the loop is
    # not evidence about the seller. (Later periods are genuinely overdue at
    # this boundary, so the value itself is not the point -- its *equality* is.)
    without_loop = ReportingStatusHandler(
        PgReportingLedgerStore(pool=ledger._pool, clock=lambda: deadline)  # noqa: SLF001
    )
    baseline = await without_loop.handle({"view": "summary"}, caller=CALLER)
    assert after["health"] == baseline["health"]
    assert {
        k: v for k, v in after["obligation_counts"].items() if k != "consumer_status_pending"
    } == baseline["obligation_counts"]

    await _post_status(
        ledger,
        period,
        reporting_status_id="status_rc3_pending_001",
        consumer_status="received",
        reporting_obligation_id=obligation.reporting_obligation_id,
        reporting_revision_id=revision.reporting_revision_id,
        observed_revision_content_sha256=revision.revision_content_sha256,
    )
    cleared = await _handler_at(ledger, period.expected_at + RECOVERY_WINDOW).handle(
        {"view": "summary"}, caller=CALLER
    )
    assert cleared["obligation_counts"]["consumer_status_pending"] == 0


async def test_reserved_authoritative_party_is_refused_before_install(
    ledger: PgReportingLedgerStore,
) -> None:
    # Rejected before the generation is stored and before any obligation
    # exists, and never coerced to seller.
    from dataclasses import replace

    with pytest.raises(LedgerConflictError) as caught:
        await ledger.put_configuration(replace(_configuration(), authoritative_party="consumer"))
    assert caught.value.code == "UNSUPPORTED_FEATURE"
    assert await ledger.list_configurations(account_id=ACCOUNT) == ()


async def test_create_schema_upgrades_an_rc2_database_in_place(
    ledger: PgReportingLedgerStore,
) -> None:
    """An adopter on the rc.2 schema gets the rc.3 columns on the next boot.

    The ledger fixture already ran ``create_schema()``, so this drops back to
    the rc.2 shape and re-runs it -- proving the DDL is an upgrade path and not
    only a bootstrap. A fresh-install-only test would pass while every existing
    deployment failed on the first ``content_mismatch``.
    """
    async with ledger._pool.connection() as connection:  # noqa: SLF001
        await connection.execute(
            "ALTER TABLE reporting_consumer_statuses DROP COLUMN mismatch_code"
        )
        await connection.execute(
            "ALTER TABLE reporting_configurations DROP COLUMN authoritative_party"
        )
        await connection.execute("DROP TABLE reporting_issue_lifecycle")

    await ledger.create_schema()

    async with ledger._pool.connection() as connection:  # noqa: SLF001
        columns = {
            row[0]
            for row in await (
                await connection.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name IN"
                    "   ('reporting_consumer_statuses', 'reporting_configurations')"
                )
            ).fetchall()
        }
        assert {"mismatch_code", "authoritative_party"} <= columns
        tables = {
            row[0]
            for row in await (
                await connection.execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_name = 'reporting_issue_lifecycle'"
                )
            ).fetchall()
        }
        assert tables == {"reporting_issue_lifecycle"}

    # And the upgraded database actually works, not just has the columns.
    await ledger.put_configuration(_configuration())
    assert (await ledger.list_configurations(account_id=ACCOUNT))[0].authoritative_party == "seller"
