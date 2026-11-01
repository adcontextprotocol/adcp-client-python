#!/usr/bin/env python3
"""Prove an installed ``adcp`` + ``adcp-reporting-storage`` pair works, before publishing.

Run inside an environment where both distributions are installed (the release
gate builds one from the distribution under release and the published
companion). Checks, in order:

1. **Storage contract.** Both sides name the reporting ledger layout they
   implement with one integer (``adcp.reporting.ledger.storage_contract``,
   ``adcp_reporting_storage.contract``). Releases that predate the
   declaration are classified: an adcp whose obligation column list carries
   ``offering_id`` is contract 2, else 1; a storage without the module is
   contract 1. A mismatch is the failure of adcp-client-python#1763, refused
   here with both versions named.
2. **The ledger, on PostgreSQL** (when ``ADCP_PG_TEST_URL`` is set): in a
   private schema, ``create_schema()``, then write and read back a
   configuration generation, an obligation and a revision through the
   installed stores, the calls that failed on the incompatible pair.

Exit status 0 when the pair works; 1 with a one-line reason otherwise. Uses
only APIs shared by adcp 9.x and 10.x, so the gate runs on both release lines.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version


def installed(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "not installed"


def adcp_contract() -> int:
    try:
        return int(
            import_module("adcp.reporting.ledger.storage_contract").REPORTING_STORAGE_CONTRACT
        )
    except ImportError:
        columns = import_module("adcp.reporting.ledger._record_rows")._OBLIGATION_COLUMNS
        return 2 if "offering_id" in [item.strip() for item in columns.split(",")] else 1


def storage_contract() -> int:
    try:
        return int(import_module("adcp_reporting_storage.contract").STORAGE_CONTRACT)
    except ImportError:
        return 1  # 0.1.0b4 and 0.2.0b0 predate the declaration


def check_contract() -> None:
    expected, implemented = adcp_contract(), storage_contract()
    pair = (
        f"adcp {installed('adcp')} + adcp-reporting-storage {installed('adcp-reporting-storage')}"
    )
    if expected != implemented:
        raise SystemExit(
            f"{pair}: adcp reads the ledger with storage contract {expected} but the storage "
            f"implements contract {implemented}; the pair would fail every reporting turn"
        )
    print(f"{pair}: storage contract {expected} on both sides")


async def check_ledger(url: str) -> None:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from psycopg import AsyncConnection, sql
    from psycopg_pool import AsyncConnectionPool

    from adcp.reporting.ledger import (
        ReportingConfiguration,
        ReportingObligationRecord,
        ReportingRevisionRecord,
        ReportingScheduleSpec,
        ReportingStatusCaller,
        derive_period,
        revision_content_sha256,
    )
    from adcp.reporting.ledger.models import first_ordinal_after

    schedule = ReportingScheduleSpec(period_duration="PT1H", delivery_sla="PT1H", alignment="utc")
    activated = datetime(2026, 9, 1, 0, 20, tzinfo=timezone.utc)
    configuration = ReportingConfiguration(
        delivery_config_id="smoke",
        delivery_config_version=1,
        account_id="smoke-account",
        consumer_id="smoke-buyer",
        report_definition_id="smoke_v1",
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=schedule,
        required_finality="official",
        activated_at=activated,
        media_buy_ids=("mb_1",),
    )
    ordinal = first_ordinal_after(schedule, account_timezone="UTC", activated_at=activated)
    period = derive_period(schedule, account_timezone="UTC", ordinal=ordinal)
    obligation = ReportingObligationRecord(
        reporting_obligation_id="rpo_smoke",
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
        created_at=period.end,
        currency="USD",
    )
    rows = [{"media_buy_id": "mb_1", "impressions": 5}]
    totals = (("impressions", "5"),)
    revision = ReportingRevisionRecord(
        reporting_revision_id="rpr_smoke",
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        finality="official",
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id="rpr_smoke",
            row_count=1,
            control_totals=totals,
            reporting_rows=rows,
        ),
        row_count=1,
        control_totals=totals,
        observed_at=period.end,
        data_through=period.end,
        created_at=period.expected_at,
        finality_basis="source_final",
        finality_policy_id="policy_1",
        finalized_at=period.end,
    )

    schema = f"adcp_companion_smoke_{secrets.token_hex(6)}"
    async with await AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with AsyncConnectionPool(
                url,
                kwargs={"options": f"-csearch_path={schema}", "autocommit": True},
                min_size=1,
                max_size=4,
                open=False,
            ) as pool:
                await pool.open(wait=True)
                store = PgReportingLedgerStore(pool=pool)
                await store.create_schema()
                await store.put_configuration(configuration)
                committed = await store.commit_obligation(obligation)
                await store.commit_revision(revision, rows)
                found = await store.find_obligation(
                    account_id=configuration.account_id,
                    consumer_id=configuration.consumer_id,
                    delivery_config_id=configuration.delivery_config_id,
                    delivery_config_version=configuration.delivery_config_version,
                    period_start=period.start,
                    period_end=period.end,
                )
                listed = await store.list_configurations(
                    caller=ReportingStatusCaller(
                        account_id=configuration.account_id,
                        consumer_id=configuration.consumer_id,
                    )
                )
                revisions = await store.list_revisions(
                    account_id=configuration.account_id,
                    reporting_obligation_id=committed.reporting_obligation_id,
                )
                recovered = await store.list_all_configurations()
                await store.create_schema()  # a restart's bootstrap is a no-op
            if found is None or found.reporting_obligation_id != "rpo_smoke":
                raise SystemExit("the installed pair cannot find the obligation it committed")
            if len(listed) != 1 or len(recovered) != 1:
                raise SystemExit("the installed pair cannot read the configuration it stored")
            if [item.reporting_revision_id for item in revisions] != ["rpr_smoke"]:
                raise SystemExit("the installed pair cannot read the revision it committed")
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    print("the installed pair creates the ledger schema and round-trips its records")


def main() -> int:
    try:
        check_contract()
        url = os.environ.get("ADCP_PG_TEST_URL")
        if url:
            asyncio.run(check_ledger(url))
        else:
            print("ADCP_PG_TEST_URL not set: the PostgreSQL round trip was skipped")
    except SystemExit as error:
        if error.code not in (None, 0):
            print(f"companion smoke failed: {error.code}", file=sys.stderr)
            return 1
        raise
    except Exception as error:  # a driver or DDL failure is a failing gate too
        print(f"companion smoke failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
