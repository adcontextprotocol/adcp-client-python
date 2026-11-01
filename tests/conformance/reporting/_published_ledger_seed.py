"""Run under an installed adcp + adcp-reporting-storage pair, outside the checkout.

Reads one JSON settings object on stdin and prints one JSON result. ``seed``
creates the ledger with the installed pair's own ``create_schema()`` and writes
two account-level generations with obligations, revisions and rows, the way a
seller on that release did; ``read`` reads them back; ``upgrade`` runs the
installed storage's ``create_schema()`` first. Every action returns an ``image``
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


async def main(settings):
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=4, open=False
    ) as pool:
        await pool.open()
        store = PgReportingLedgerStore(pool=pool)
        result = {"action": settings["action"]}
        try:
            if settings["action"] in {"seed", "upgrade"}:
                await store.create_schema()
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
