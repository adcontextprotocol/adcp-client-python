"""Copied outside the checkout: an installed SDK seeds or upgrades a private schema.

Only the seed action imports fixture builders. Upgrade ownership is an explicit
operator mapping from the test's authoritative registry, never a buyer request.
"""

import asyncio
import hashlib
import importlib
import json
import sys
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path


async def image(connection, schema):
    from psycopg import sql

    tables = await (
        await connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s"
            " AND (starts_with(tablename,'reporting_')"
            " OR starts_with(tablename,'adcp_reporting_')) ORDER BY tablename",
            (schema,),
        )
    ).fetchall()
    result = {}
    for (table,) in tables:
        result[table] = await (
            await connection.execute(
                sql.SQL("SELECT to_jsonb(t) FROM {}.{} t ORDER BY to_jsonb(t)::text").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
            )
        ).fetchall()
    return result


async def main(settings):
    from psycopg_pool import AsyncConnectionPool

    workspace = Path(settings["workspace"]).resolve()
    origins = {}
    for name, digest in settings["modules"].items():
        path = Path(importlib.import_module(name).__file__).resolve()
        assert path.is_relative_to(Path(sys.prefix)) and "site-packages" in str(path)
        assert not path.is_relative_to(workspace)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        origins[name] = str(path)
    source_runtime = settings.get("source_runtime", False)
    if not source_runtime:
        assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
    if settings.get("python"):
        assert list(sys.version_info[:2]) == settings["python"]
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=4, open=False
    ) as pool:
        if settings["action"] == "seed":
            sys.path.insert(0, settings["fixtures"])
            from adcp._version import resolve_adcp_version
            from adcp.reporting.feed import PgReportingFeedStore
            from adcp.reporting.ledger import (
                PgReportingReconciliationStore,
                ReportingAdjustmentRecord,
                ReportingControlTotalRecord,
                ReportingRevisionReceiptRecord,
            )
            from adcp.reporting.ledger.delivery import adjustment_to_wire, receipt_to_wire
            from adcp.reporting.outbox import PgStatusNotificationStore
            from tests.conformance.reporting._durable_materializer_support import durable_case
            from tests.conformance.reporting._generation_support import END

            store = PgReportingFeedStore(pool=pool, notifications=settings["notifications"])
            await store.create_schema()
            await PgStatusNotificationStore(
                PgReportingReconciliationStore(pool=pool, notifications=True)
            ).create_schema()
            receipt_mode = settings["kind"] != "materializer"
            case = await durable_case(
                store,
                count=3,
                consumer=settings["consumer"],
                legacy_definition=settings["legacy_definition"],
                finality="official" if receipt_mode else "snapshot",
                required="official" if receipt_mode else "snapshot",
                reconciliation_mode="consumer_receipt" if receipt_mode else "delivery_only",
            )
            assert (await case.service().run_once()).state == "verified"
            outcome = (await case.outcomes())[0]
            if settings["kind"] == "materializer":
                from adcp.reporting.materializer import (
                    ReferenceReportingResolver,
                    ReportingDestinationIO,
                )

                # The authoritative fixture registry owns this generation for
                # its creator. A second legacy recipient has a destination, not
                # a second configuration write or a future ownership grant.
                sibling_binding = replace(
                    case.binding, consumer_id="https://buyer.example.test/isolated"
                )
                await store.put_destination_binding(sibling_binding)
                resolver = ReferenceReportingResolver(
                    case.writer, case.registry, (case.binding, sibling_binding)
                )
                sibling = replace(
                    case,
                    binding=sibling_binding,
                    resolver=resolver,
                    io=ReportingDestinationIO(case.registry, resolver),
                )
                assert (await sibling.service().run_once()).state == "verified"
            request, response = None, None
            if receipt_mode:
                evidence = outcome.verification
                receipt = ReportingRevisionReceiptRecord(
                    case.scope,
                    "historical-revision-0001",
                    case.revision.reporting_revision_id,
                    outcome.reporting_materialization_id,
                    "accepted",
                    evidence.verification_profile,
                    evidence.row_count,
                    evidence.control_totals,
                    outcome.completed_at,
                    observed_canonical_content_digest=evidence.canonical_content_digest,
                )
                adjustment = ReportingAdjustmentRecord(
                    "historical-adjustment",
                    case.config.account_id,
                    case.revision.reporting_revision_id,
                    "source_correction",
                    END,
                    END + timedelta(days=30),
                    (("spend", "-1.50"),),
                    END + timedelta(seconds=5),
                    END + timedelta(seconds=6),
                    managed_control_total_deltas=(
                        ReportingControlTotalRecord("spend", "-1.50", "decimal", "USD"),
                    ),
                )
                await store.commit_adjustment(adjustment)
                request = {
                    "adcp_version": (
                        "3.2-rc.6" if settings["kind"] == "feed" else resolve_adcp_version(None)
                    ),
                    "account": {"account_id": case.config.account_id},
                    "idempotency_key": "historical-mixed-receipts",
                    "receipts": [receipt_to_wire(receipt)],
                    "adjustment_receipts": [
                        {
                            "reporting_receipt_id": "historical-adjustment-0001",
                            "reporting_adjustment_id": adjustment.reporting_adjustment_id,
                            "adjusts_reporting_revision_id": case.revision.reporting_revision_id,
                            "observed_at": outcome.completed_at.isoformat(),
                            "status": "accepted",
                            "observed_adjustment_sha256": adjustment_to_wire(adjustment)[
                                "canonical_adjustment_sha256"
                            ],
                        }
                    ],
                }
                response = await store.ingest_receipt_batch(request, caller=case.scope.principal)
                assert [r["result"] for r in response["results"]] == ["recorded", "recorded"]
                assert (
                    await store.ingest_receipt_batch(request, caller=case.scope.principal)
                    == response
                )
            query = {
                "account": {"account_id": case.config.account_id},
                "view": "periods",
                "pagination": {"max_results": 1},
            }
            first = await store.read_reporting_feed(query, caller=case.scope.principal)
            assert first["pagination"]["has_more"]
            pending = None
            if settings.get("pending"):
                from pydantic import TypeAdapter

                from adcp.reporting.ledger import derive_period, revision_content_sha256
                from adcp.reporting.materializer.work import ReportingMaterializerLease

                period = derive_period(
                    case.config.schedule, account_timezone=case.config.account_timezone, ordinal=1
                )
                obligation = replace(
                    case.obligation,
                    reporting_obligation_id="historical-pending-obligation",
                    period=period,
                    scope_resolved_at=period.end,
                    automated_recovery_deadline_at=period.expected_at
                    + case.config.automated_recovery_window,
                )
                await store.commit_obligation(obligation)
                revision_id = "historical-pending-revision"
                revision = replace(
                    case.revision,
                    reporting_revision_id=revision_id,
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    data_through=period.end,
                    observed_at=period.end,
                    created_at=period.end,
                    finalized_at=period.end,
                    revision_content_sha256=revision_content_sha256(
                        reporting_revision_id=revision_id,
                        row_count=case.revision.row_count,
                        control_totals=case.revision.control_totals,
                        reporting_rows=case.rows,
                        control_total_evidence=case.revision.managed_control_totals,
                    ),
                )
                await store.commit_revision(revision, case.rows)
                for _ in range(16):
                    lease = await store.claim_materialization(
                        keys=(case.verifier.key,), lease_seconds=300
                    )
                    if isinstance(lease, ReportingMaterializerLease):
                        assert lease.attempt.reporting_revision_id == revision_id
                        pending = {
                            "attempt": TypeAdapter(type(lease.attempt)).dump_python(
                                lease.attempt, mode="json"
                            ),
                            "external_id": lease.request.external_id,
                            "generation": lease.generation,
                            "verification_key": asdict(case.verifier.key),
                        }
                        break
                assert pending is not None, "historical SDK must reserve real pending work"
            result = {
                "account": case.config.account_id,
                "consumer": settings["consumer"],
                "delivery_config_id": case.config.delivery_config_id,
                "delivery_config_version": case.config.delivery_config_version,
                "obligation": case.obligation.reporting_obligation_id,
                "revision": case.revision.reporting_revision_id,
                "request": request,
                "response": response,
                "first": first,
                "pending": pending,
            }
        else:
            from psycopg import sql

            from adcp.reporting.feed import PgReportingFeedStore, ReportingFeedError
            from adcp.reporting.ledger import PgReportingLedgerStore, ReportingStatusCaller
            from adcp.reporting.migration import (
                ReportingOwnershipBackfill,
                ReportingOwnershipMigrationError,
                backfill_legacy_reporting,
                legacy_generation_digest,
                migrate_legacy_reporting,
            )

            store = PgReportingFeedStore(pool=pool, notifications=settings["notifications"])
            legacy = settings["legacy"]
            async with pool.connection() as c:
                schema = (await (await c.execute("SELECT current_schema()")).fetchone())[0]
                before = await image(c, schema)
                try:
                    await PgReportingLedgerStore(pool=pool).create_schema()
                except ReportingOwnershipMigrationError:
                    pass
                else:
                    raise AssertionError("legacy schema must require maintenance")
                archive = settings["archive"]
                try:
                    await migrate_legacy_reporting(c, archive_schema=archive, workers_stopped=False)
                except ReportingOwnershipMigrationError:
                    pass
                else:
                    raise AssertionError("workers must stop before migration")
                await migrate_legacy_reporting(c, archive_schema=archive, workers_stopped=True)
                try:
                    await store.create_schema()
                    for consumer in (
                        legacy["consumer"],
                        "another-buyer",
                        "__legacy__",
                        "unassigned",
                    ):
                        assert (
                            await store.list_configurations(
                                caller=ReportingStatusCaller(legacy["account"], consumer)
                            )
                            == ()
                        )
                    assert await image(c, archive) == before
                    cursor = legacy["first"]["pagination"]["cursor"]
                    try:
                        await store.read_reporting_feed(
                            {
                                "account": {"account_id": legacy["account"]},
                                "view": "periods",
                                "pagination": {"cursor": cursor, "max_results": 1},
                            },
                            caller=ReportingStatusCaller(legacy["account"], legacy["consumer"]),
                        )
                    except ReportingFeedError as error:
                        assert (
                            error.code == "INVALID_CHECKPOINT" and "restart" in str(error).lower()
                        )
                    else:
                        raise AssertionError("legacy pagination must restart")
                    digest = await legacy_generation_digest(
                        c,
                        archive_schema=archive,
                        account_id=legacy["account"],
                        delivery_config_id=legacy["delivery_config_id"],
                        delivery_config_version=legacy["delivery_config_version"],
                    )
                    mapping = ReportingOwnershipBackfill(
                        legacy["account"],
                        legacy["delivery_config_id"],
                        legacy["delivery_config_version"],
                        legacy["consumer"],
                        "authoritative-fixture-registry-2026",
                        digest,
                        "retain_without_replay",
                    )
                    await backfill_legacy_reporting(
                        c, archive_schema=archive, mapping=mapping, workers_stopped=True
                    )
                    owner = ReportingStatusCaller(legacy["account"], legacy["consumer"])
                    (retained,) = await store.list_configurations(caller=owner)
                    assert retained.quarantined
                    assert (
                        await store.list_configurations(
                            caller=ReportingStatusCaller(legacy["account"], "another-buyer")
                        )
                        == ()
                    )
                    assert (
                        await store.list_configurations(
                            caller=ReportingStatusCaller("other-account", legacy["consumer"])
                        )
                        == ()
                    )
                    assert await image(c, archive) == before
                    active = await image(c, schema)
                    inherited = {
                        k: len(v)
                        for k, v in before.items()
                        if v
                        and any(
                            word in k
                            for word in ("work", "snapshot", "notification", "destination")
                        )
                    }
                    assert inherited
                    for name in inherited:
                        assert not active.get(
                            name
                        ), "old work/bindings/cursors must remain archived"
                    result = {
                        "archive_unchanged": True,
                        "legacy_cursor_restarted": True,
                        "retained_quarantined": True,
                        "inherited_rows": inherited,
                        "evidence_sha256": digest,
                        "archive": archive,
                        "archive_image_sha256": hashlib.sha256(
                            json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest(),
                    }
                finally:
                    if not settings.get("keep_archive", False):
                        await c.execute(
                            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(archive))
                        )
        for name, module in tuple(sys.modules.items()):
            if (name == "adcp" or name.startswith("adcp.")) and getattr(module, "__file__", None):
                path = Path(module.__file__).resolve()
                if source_runtime:
                    assert path.is_relative_to(workspace / "src")
                else:
                    assert "site-packages" in str(path) and not path.is_relative_to(workspace)
        return {**result, "origins": origins, "python": list(sys.version_info[:2])}


if __name__ == "__main__":
    settings = json.loads(sys.stdin.readline())
    result = asyncio.run(main(settings))
    if settings.get("pause"):
        print(json.dumps({"point": "pending", "legacy": result}), flush=True)
        sys.stdin.readline()
        raise AssertionError("the historical reserved worker must be killed")
    print(json.dumps(result), flush=True)
