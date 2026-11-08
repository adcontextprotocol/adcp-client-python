"""Run under an installed adcp + adcp-reporting-storage pair, outside the checkout.

Reads one JSON settings object on stdin and prints one JSON result. ``seed``
creates the ledger with the installed pair's own ``create_schema()`` and writes
two account-level generations with obligations, revisions and rows, the way a
seller on that release did; ``read`` reads them back; ``upgrade`` runs the
installed storage's ``create_schema()`` first; ``old_pod`` does what a still
running, or restarted, pod of that pair does (``old_pod`` below). With ``notifications`` the
outbox and status-notification stores are created too, as a seller with
status notifications boots them. Every action returns an ``image``
of every ``reporting_*``/``adcp_reporting_*`` table so the test can prove no
retained evidence changed. Only APIs common to adcp 9.3.1 and 10.0.0b2+ are used.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

ACTIVATED_AT = datetime(2026, 11, 1, 0, 20, tzinfo=timezone.utc)
ACCOUNTS = ("account-redacted", "account-b")
CONSUMER = "buyer-1"
VOLATILE_TABLES = {"adcp_schema_migrations"}


def _configuration(account_id):
    from adcp.reporting.ledger import (
        ReportingConfiguration,
        ReportingDefinitionBinding,
        ReportingScheduleSpec,
    )

    return ReportingConfiguration(
        delivery_config_id="gam-delivery",
        delivery_config_version=1,
        account_id=account_id,
        consumer_id=CONSUMER,
        report_definition_id="PAID_MEDIA_DAILY_V1",
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=ReportingScheduleSpec(
            period_duration="PT1H", delivery_sla="PT10M", alignment="utc"
        ),
        required_finality="snapshot",
        activated_at=ACTIVATED_AT,
        media_buy_ids=("media-buy-redacted",),
        definition=ReportingDefinitionBinding(
            report_definition_uri="https://contracts.example.test/reporting/paid-media-daily-v1",
            report_definition_sha256="a" * 64,
            schema_version="1.0.0",
            schema_uri="https://contracts.example.test/reporting/paid-media-daily-v1/schema",
            schema_sha256="b" * 64,
        ),
    )


def _obligation(configuration, ordinal):
    from adcp.reporting.ledger import ReportingObligationRecord, derive_period
    from adcp.reporting.ledger.models import first_ordinal_after

    schedule = configuration.schedule
    base = first_ordinal_after(schedule, account_timezone="UTC", activated_at=ACTIVATED_AT)
    period = derive_period(schedule, account_timezone="UTC", ordinal=base + ordinal)
    return ReportingObligationRecord(
        reporting_obligation_id=f"rpo_{configuration.account_id}_{ordinal}",
        account_id=configuration.account_id,
        consumer_id=configuration.consumer_id,
        delivery_config_id=configuration.delivery_config_id,
        delivery_config_version=configuration.delivery_config_version,
        report_definition_id=configuration.report_definition_id,
        reporting_profile=configuration.reporting_profile,
        feed_purpose=configuration.feed_purpose,
        period=period,
        scope_resolved_at=period.end,
        media_buy_ids=configuration.media_buy_ids,
        required_finality=configuration.required_finality,
        automated_recovery_deadline_at=period.expected_at + timedelta(hours=6),
        schedule=schedule,
        definition=configuration.definition,
        created_at=period.end,
        currency="USD",
    )


def _revision(obligation, revision_id):
    from adcp.reporting.ledger import ReportingRevisionRecord, revision_content_sha256

    rows = [
        {
            "media_buy_id": "media-buy-redacted",
            "campaign_id": "campaign-redacted-1",
            "impressions": 10,
            "spend": "1.25",
        }
    ]
    totals = (("impressions", "10"), ("spend", "1.25"))
    record = ReportingRevisionRecord(
        reporting_revision_id=revision_id,
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        finality="snapshot",
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision_id,
            row_count=len(rows),
            control_totals=totals,
            reporting_rows=rows,
        ),
        row_count=len(rows),
        control_totals=totals,
        observed_at=obligation.period.end,
        data_through=obligation.period.end,
        created_at=obligation.period.expected_at,
    )
    return record, rows


async def image(connection):
    """Every reporting table's rows as JSON, in a stable order."""
    tables = await (
        await connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            " AND (starts_with(tablename, 'reporting_') OR starts_with(tablename, 'adcp_'))"
            " ORDER BY tablename"
        )
    ).fetchall()
    result = {}
    for (table,) in tables:
        if table in VOLATILE_TABLES:
            continue
        rows = await (
            await connection.execute(
                f"SELECT to_jsonb(t) FROM {table} t ORDER BY to_jsonb(t)::text"  # noqa: S608
            )
        ).fetchall()
        result[table] = [row[0] for row in rows]
    return result


async def readback(store):
    from adcp.reporting.ledger import ReportingStatusCaller

    result = {"configurations": [], "obligations": [], "revisions": [], "rows": {}}
    for account_id in ACCOUNTS:
        caller = ReportingStatusCaller(account_id=account_id, consumer_id=CONSUMER)
        for configuration in await store.list_configurations(caller=caller):
            result["configurations"].append(
                [
                    configuration.account_id,
                    configuration.consumer_id,
                    configuration.delivery_config_id,
                    configuration.delivery_config_version,
                ]
            )
        for ordinal in range(2):
            seeded = _obligation(_configuration(account_id), ordinal)
            found = await store.find_obligation(
                account_id=account_id,
                consumer_id=CONSUMER,
                delivery_config_id="gam-delivery",
                delivery_config_version=1,
                period_start=seeded.period.start,
                period_end=seeded.period.end,
            )
            result["obligations"].append(found.reporting_obligation_id if found else None)
            if found is None:
                continue
            for revision in await store.list_revisions(
                account_id=account_id, reporting_obligation_id=found.reporting_obligation_id
            ):
                result["revisions"].append(revision.reporting_revision_id)
                page = await store.read_revision_rows(
                    account_id=account_id, reporting_revision_id=revision.reporting_revision_id
                )
                result["rows"][revision.reporting_revision_id] = list(page.rows)
    return result


async def create_schema(pool, store, *, notifications):
    """The stores a notifications-enabled seller boots, in the order it boots them."""
    if notifications:
        from adcp_reporting_storage import PgReportingOutbox

        await PgReportingOutbox(pool=pool).create_schema()
    await store.create_schema()
    if notifications:
        from adcp_reporting_storage.outbox.status_pg import PgStatusNotificationStore

        await PgStatusNotificationStore(store).create_schema()


def _capability_offering(route):
    return {
        "offering_id": f"{route.upper()}_SNAPSHOT_V1",
        "feed_purpose": "analytics",
        "report_definition_id": "PAID_MEDIA_DAILY_V1",
        "report_definition_uri": "https://contracts.example.test/reporting/paid-media-daily-v1",
        "report_definition_sha256": "a" * 64,
        "reporting_profile": {
            "id": "paid_media_delivery",
            "version": "1.0.0",
            "schema_uri": "https://contracts.example.test/reporting/paid-media-daily-v1/schema",
            "schema_sha256": "b" * 64,
            "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
            "schema_ref_policy": "local_fragment_only",
            "grain": "one row per media buy per period",
            "primary_keys": ["media_buy_id"],
        },
        "schedule": {"period_duration": "PT1H", "delivery_sla": "PT10M", "alignment": "utc"},
        "supported_finality": ["snapshot"],
        "reconciliation_mode": "delivery_only",
    }


def _account_context(configuration):
    from adcp.reporting.fixtures import SNAPSHOT_OFFERING_ID, redacted_capabilities
    from adcp.reporting.service import ReportingAccountContext

    route = configuration.delivery_config_id.removesuffix("-delivery")
    return ReportingAccountContext(
        account_id=configuration.account_id,
        adapter=route,
        currency="USD",
        account_timezone=configuration.account_timezone,
        source_scope=redacted_capabilities().source_scope,
        snapshot_offering_id=SNAPSHOT_OFFERING_ID,
        requested_dimensions=("campaign_id",),
        capability_offering=_capability_offering(route),
        publication_namespace="reporting-source:fixture",
    )


async def worker_turn(store, now):
    """One full worker turn of this pair's service over the stored ``gam`` generations.

    Generations other test steps record for reconciliation scaffolding (the
    ``daily`` route) are left out of the startup scan; every other stored
    generation, whichever release recorded it, is recovered and worked.
    """
    from adcp.reporting.fixtures import redacted_capabilities
    from adcp.reporting.service import ReliableReportingService
    from adcp.reporting.testing import ScriptedReportingAdapter

    rows = [
        {
            "media_buy_id": "media-buy-redacted",
            "campaign_id": "campaign-redacted-1",
            "impressions": 10,
            "spend": "1.25",
        }
    ]
    every = store.list_all_configurations

    async def gam_generations():
        return tuple(item for item in await every() if item.delivery_config_id == "gam-delivery")

    store.list_all_configurations = gam_generations
    service = ReliableReportingService(
        store=store, clock=lambda: now, account_context=_account_context
    )
    service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), [rows] * 8))
    try:
        await service.start()  # create_schema(): this pair's boot
        if service.initialization_errors:
            raise RuntimeError(f"initialization errors {service.initialization_errors}")
        turn = await service.run_worker(now=now)
    finally:
        await service.close()
        del store.list_all_configurations
    if turn.configuration_errors:
        raise RuntimeError(f"configuration errors {turn.configuration_errors}")
    return sorted(
        [key.account_id, key.consumer_id, key.delivery_config_id, key.delivery_config_version]
        for key in turn.configurations
    )


async def old_pod(pool, store, settings, result):
    """What a still-running (or restarted) pod of this pair does, one step at a time.

    Each step's outcome is recorded, so the test can assert every one: a
    restart (``create_schema``), reads, recording a new generation, closing a
    period of an existing one, a full worker turn and, with notifications, a
    status baseline and projection.
    """
    notifications = bool(settings.get("notifications"))
    ordinal = int(settings.get("ordinal", 2))
    steps = result["steps"] = {}

    async def step(name, operation):
        try:
            outcome = await operation()
        except Exception as error:
            steps[name] = f"{type(error).__name__}: {error}"
            return None
        steps[name] = "ok"
        return outcome

    status = None
    if notifications:
        from adcp_reporting_storage.outbox.status_pg import PgStatusNotificationStore

        status = PgStatusNotificationStore(store)
    await step("create_schema", lambda: create_schema(pool, store, notifications=notifications))
    result["readback"] = await step("readback", lambda: readback(store))
    if status is not None:
        await step("status_baseline", lambda: status.baseline(account_id=ACCOUNTS[0]))

    async def close_period():
        configuration = _configuration(ACCOUNTS[0])
        obligation = await store.commit_obligation(_obligation(configuration, ordinal))
        revision, rows = _revision(obligation, f"rpr_{ACCOUNTS[0]}_{ordinal}")
        await store.commit_revision(revision, rows)

    await step("commit_obligation_and_revision", close_period)
    await step(
        "put_configuration",
        lambda: store.put_configuration(_configuration(settings.get("account", "account-old-pod"))),
    )
    if settings.get("now"):
        now = datetime.fromisoformat(settings["now"])
        result["turn"] = await step("worker_turn", lambda: worker_turn(store, now))
    if status is not None:
        await step("status_project", lambda: status.project_one(account_id=ACCOUNTS[0]))
        checkpoints = await step(
            "status_checkpoints", lambda: status.checkpoints(account_id=ACCOUNTS[0])
        )
        result["checkpoints"] = len(checkpoints or ())


async def main(settings):
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from psycopg_pool import AsyncConnectionPool

    notifications = bool(settings.get("notifications"))
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=4, open=False
    ) as pool:
        await pool.open()
        store = PgReportingLedgerStore(pool=pool, notifications=notifications)
        result = {"action": settings["action"]}
        try:
            if settings["action"] in {"seed", "upgrade"}:
                await create_schema(pool, store, notifications=notifications)
            if settings["action"] == "seed":
                for account_id in ACCOUNTS:
                    configuration = _configuration(account_id)
                    await store.put_configuration(configuration)
                    for ordinal in range(2):
                        obligation = await store.commit_obligation(
                            _obligation(configuration, ordinal)
                        )
                        revision, rows = await _async_revision(obligation, account_id, ordinal)
                        await store.commit_revision(revision, rows)
            if settings["action"] == "old_pod":
                await old_pod(pool, store, settings, result)
            else:
                result["readback"] = await readback(store)
        except Exception as error:  # reported, so the test can assert on the repro
            result["error"] = f"{type(error).__name__}: {error}"
        async with pool.connection() as connection:
            result["image"] = await image(connection)
    return result


async def _async_revision(obligation, account_id, ordinal):
    return _revision(obligation, f"rpr_{account_id}_{ordinal}")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(json.loads(sys.stdin.read()))), default=str))
