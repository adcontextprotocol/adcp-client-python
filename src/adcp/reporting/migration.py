"""Explicit, stopped-worker upgrade of reporting state lacking caller ownership.

These operator APIs must never be mounted as buyer tools. They preserve the
legacy schema as a read-only archive and import only verified Core evidence.
Queues, leases, destination bindings and signing material are never imported.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Literal

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import consumer_reference, principal_reference

__all__ = [
    "ReportingOwnershipBackfill",
    "ReportingOwnershipMigrationError",
    "backfill_legacy_reporting",
    "legacy_generation_digest",
    "migrate_legacy_reporting",
]


class ReportingOwnershipMigrationError(RuntimeError):
    """An operator migration precondition failed; no partial changes commit."""


@dataclass(frozen=True)
class ReportingOwnershipBackfill:
    """Authoritative mapping with an explicit retained-only recovery decision.

    ``authority_reference`` identifies the operator's external ownership proof.
    ``evidence_sha256`` pins the exact legacy generation and immutable records.
    This mapping grants read access, never permission to replay old work. Create
    a new generation with fresh destination/signing admission for new delivery.
    """

    account_id: str
    delivery_config_id: str
    delivery_config_version: int
    consumer_id: str
    authority_reference: str
    evidence_sha256: str
    pending_work_disposition: Literal["retain_without_replay"]

    def __post_init__(self) -> None:
        consumer_reference(self.consumer_id)
        principal_reference(self.authority_reference)
        if not self.account_id or not self.delivery_config_id or self.delivery_config_version < 1:
            raise ValueError("an exact legacy generation is required")
        if not re.fullmatch(r"[a-f0-9]{64}", self.evidence_sha256):
            raise ValueError("the legacy evidence digest is required")
        if self.pending_work_disposition != "retain_without_replay":
            raise ValueError("legacy work cannot be replayed through ownership backfill")


def _archive_name(name: str) -> None:
    if not re.fullmatch(r"adcp_reporting_quarantine_[a-z0-9_]{1,40}", name):
        raise ValueError("use a dedicated adcp_reporting_quarantine_* schema")


async def require_owned_schema_or_empty(connection: Any) -> None:
    row = await (
        await connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
            " WHERE n.nspname=current_schema() AND c.relname='reporting_configurations'),"
            " EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()"
            " AND table_name='reporting_configurations' AND column_name='consumer_id')"
        )
    ).fetchone()
    if row[0] and not row[1]:
        raise ReportingOwnershipMigrationError(
            "Stop reporting workers and run migrate_legacy_reporting before create_schema"
        )


async def migrate_legacy_reporting(
    connection: Any, *, archive_schema: str, workers_stopped: bool
) -> None:
    """Atomically archive legacy state and install the complete owned schema.

    Use a maintenance connection owned by the operator. All processes that can
    read, admit or work reporting must be stopped. Existing cursors are invalid.
    This operation has no automatic data backfill and never sends notifications.
    """
    from psycopg import sql

    _archive_name(archive_schema)
    if workers_stopped is not True:
        raise ReportingOwnershipMigrationError("reporting workers and admission must be stopped")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('adcp.reporting.schema'),ha"
            "shtext(current_schema()))"
        )
        current = (await (await connection.execute("SELECT current_schema()")).fetchone())[0]
        columns = await (
            await connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=%s"
                " AND table_name='reporting_configurations'",
                (current,),
            )
        ).fetchall()
        if not columns or ("consumer_id",) in columns:
            raise ReportingOwnershipMigrationError("expected an unmigrated legacy reporting schema")
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(archive_schema)))
        await connection.execute(
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(archive_schema))
        )
        tables = await (
            await connection.execute(
                "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
                " WHERE n.nspname=%s AND c.relkind='r'"
                " AND (relname LIKE 'reporting\\_%%' ESCAPE '\\' OR relname LIKE 'ad"
                "cp\\_reporting\\_%%' ESCAPE '\\')"
                " ORDER BY relname",
                (current,),
            )
        ).fetchall()
        # Lock the entire legacy surface before moving any object. Foreign keys,
        # triggers, identities and artifact bytes survive ALTER ... SET SCHEMA.
        for (table,) in tables:
            await connection.execute(
                sql.SQL("LOCK TABLE {}.{} IN ACCESS EXCLUSIVE MODE").format(
                    sql.Identifier(current), sql.Identifier(table)
                )
            )
        for (table,) in tables:
            await connection.execute(
                sql.SQL("ALTER TABLE {}.{} SET SCHEMA {}").format(
                    sql.Identifier(current), sql.Identifier(table), sql.Identifier(archive_schema)
                )
            )
        sequences = await (
            await connection.execute(
                "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
                " WHERE n.nspname=%s AND c.relkind='S' AND (relname LIKE 'reportin"
                "g%%' OR relname LIKE 'adcp_reporting%%')",
                (current,),
            )
        ).fetchall()
        for (sequence,) in sequences:
            await connection.execute(
                sql.SQL("ALTER SEQUENCE {}.{} SET SCHEMA {}").format(
                    sql.Identifier(current),
                    sql.Identifier(sequence),
                    sql.Identifier(archive_schema),
                )
            )
        functions = await (
            await connection.execute(
                "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace"
                " n ON n.oid=p.pronamespace"
                " WHERE n.nspname=%s AND (p.proname LIKE 'reporting%%' OR p.pronam"
                "e LIKE 'adcp_reporting%%')",
                (current,),
            )
        ).fetchall()
        for (function,) in functions:
            # regprocedure is emitted by PostgreSQL, never caller-supplied SQL.
            await connection.execute(
                sql.SQL("ALTER FUNCTION {} SET SCHEMA {}").format(
                    sql.SQL(function), sql.Identifier(archive_schema)
                )
            )
        await connection.execute(
            sql.SQL(
                "CREATE FUNCTION {}.reject_writes() RETURNS trigger LANGUAGE plpgs"
                "ql AS $$ BEGIN RAISE EXCEPTION 'legacy reporting is quarantined r"
                "ead-only'; END $$"
            ).format(sql.Identifier(archive_schema))
        )
        for (table,) in tables:
            await connection.execute(
                sql.SQL(
                    "CREATE TRIGGER ownership_quarantine BEFORE INSERT OR UPDATE OR DE"
                    "LETE OR TRUNCATE ON {}.{} FOR EACH STATEMENT EXECUTE FUNCTION {}."
                    "reject_writes()"
                ).format(
                    sql.Identifier(archive_schema),
                    sql.Identifier(table),
                    sql.Identifier(archive_schema),
                )
            )
        await connection.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(
                sql.Identifier(archive_schema)
            )
        )
        # The maintenance role owns the archive, even when the legacy tables
        # belonged to the application. Revoke callable functions and sequences
        # as well as tables; triggers remain attached to the archived objects.
        role = (await (await connection.execute("SELECT current_user")).fetchone())[0]
        for (table,) in tables:
            await connection.execute(
                sql.SQL("ALTER TABLE {}.{} OWNER TO {}").format(
                    sql.Identifier(archive_schema), sql.Identifier(table), sql.Identifier(role)
                )
            )
        for kind in ("SEQUENCES", "FUNCTIONS"):
            await connection.execute(
                sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA {} FROM PUBLIC").format(
                    sql.SQL(kind), sql.Identifier(archive_schema)
                )
            )
        root = files("adcp.reporting.ledger")
        for name in (
            "reporting_ledger.sql",
            "reporting_ledger_account_generations.sql",
            "reporting_ledger_obligation_currency.sql",
            "reporting_ledger_reconciliation.sql",
            "reporting_notification_outbox.sql",
            "reporting_webhook_activity.sql",
            "reporting_provisional_observations.sql",
            "reporting_materializer.sql",
            "reporting_receipt_ingestion.sql",
            "reporting_feed.sql",
            "reporting_status_notifications.sql",
            "reporting_status_selector_version.sql",
            "reporting_projection.sql",
            "reporting_projection_notifications.sql",
            "reporting_projection_feed.sql",
            "reporting_production.sql",
            "reporting_caller_ownership.sql",
        ):
            await connection.execute(root.joinpath(name).read_text())
        await connection.execute(
            files("adcp.reporting.production").joinpath("service_context.sql").read_text()
        )
        await connection.execute(
            "INSERT INTO adcp_reporting_ownership_archives(archive_schema) VALUES (%s)",
            (archive_schema,),
        )


async def _legacy_evidence(
    connection: Any, archive_schema: str, account: str, config: str, version: int
) -> dict[str, list[dict[str, Any]]]:
    from psycopg import sql

    _archive_name(archive_schema)
    registered = await (
        await connection.execute(
            "SELECT 1 FROM adcp_reporting_ownership_archives WHERE archive_schema=%s",
            (archive_schema,),
        )
    ).fetchone()
    if not registered:
        raise ReportingOwnershipMigrationError(
            "archive is not registered by the maintenance migration"
        )
    result: dict[str, list[dict[str, Any]]] = {}
    conditions = {
        "reporting_configurations": (
            "account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s"
        ),
        "reporting_obligations": "account_id=%s AND delivery_config_id=%s AND delivery_config_versi"
        "on=%s",
    }
    for table, condition in conditions.items():
        rows = await (
            await connection.execute(
                sql.SQL("SELECT to_jsonb(t) FROM {}.{} t WHERE ").format(
                    sql.Identifier(archive_schema), sql.Identifier(table)
                )
                + sql.SQL(condition),
                (account, config, version),
            )
        ).fetchall()
        result[table] = [row[0] for row in rows]
    ids = [r["reporting_obligation_id"] for r in result["reporting_obligations"]]
    rows = await (
        await connection.execute(
            sql.SQL(
                "SELECT to_jsonb(t) FROM {}.reporting_revisions t WHERE reporting_"
                "obligation_id=ANY(%s)"
            ).format(sql.Identifier(archive_schema)),
            (ids,),
        )
    ).fetchall()
    result["reporting_revisions"] = [r[0] for r in rows]
    revisions = [r["reporting_revision_id"] for r in result["reporting_revisions"]]
    for table, field in (
        ("reporting_revision_rows", "reporting_revision_id"),
        ("reporting_adjustments", "adjusts_reporting_revision_id"),
    ):
        rows = await (
            await connection.execute(
                sql.SQL("SELECT to_jsonb(t) FROM {}.{} t WHERE {}=ANY(%s)").format(
                    sql.Identifier(archive_schema), sql.Identifier(table), sql.Identifier(field)
                ),
                (revisions,),
            )
        ).fetchall()
        result[table] = [r[0] for r in rows]
    record_ids = (
        ids + revisions + [r["reporting_adjustment_id"] for r in result["reporting_adjustments"]]
    )
    rows = await (
        await connection.execute(
            sql.SQL(
                "SELECT to_jsonb(t) FROM {}.reporting_ledger_changes t WHERE accou"
                "nt_id=%s AND record_id=ANY(%s) AND record_kind IN ('obligation','"
                "revision','adjustment')"
            ).format(sql.Identifier(archive_schema)),
            (account, record_ids),
        )
    ).fetchall()
    result["reporting_ledger_changes"] = [r[0] for r in rows]
    return {
        table: sorted(rows, key=lambda r: canonical_json_utf8_v1(r))
        for table, rows in result.items()
    }


async def legacy_generation_digest(
    connection: Any,
    *,
    archive_schema: str,
    account_id: str,
    delivery_config_id: str,
    delivery_config_version: int,
) -> str:
    """Pin the retained Core evidence before approving an authoritative mapping."""
    evidence = await _legacy_evidence(
        connection, archive_schema, account_id, delivery_config_id, delivery_config_version
    )
    return hashlib.sha256(canonical_json_utf8_v1(evidence)).hexdigest()


async def backfill_legacy_reporting(
    connection: Any,
    *,
    archive_schema: str,
    mapping: ReportingOwnershipBackfill,
    workers_stopped: bool,
) -> None:
    """Import pinned Core evidence read-only for its externally verified owner.

    Legacy reconciliation/artifact documents, cursors and all pending work stay
    archived unchanged. Their old signatures/identifiers are never relabelled.
    Recovery requires manual reconciliation followed by new owned admission.
    """
    from psycopg import sql

    if workers_stopped is not True:
        raise ReportingOwnershipMigrationError("backfill requires stopped reporting workers")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('adcp.reporting.schema'),ha"
            "shtext(current_schema()))"
        )
        evidence = await _legacy_evidence(
            connection,
            archive_schema,
            mapping.account_id,
            mapping.delivery_config_id,
            mapping.delivery_config_version,
        )
        digest = hashlib.sha256(canonical_json_utf8_v1(evidence)).hexdigest()
        if digest != mapping.evidence_sha256 or len(evidence["reporting_configurations"]) != 1:
            raise ReportingOwnershipMigrationError(
                "ownership proof does not pin this legacy generation"
            )
        prior = await (
            await connection.execute(
                "SELECT consumer_id,evidence_sha256,authority_reference FROM adcp_"
                "reporting_ownership_backfills WHERE archive_schema=%s AND account"
                "_id=%s AND delivery_config_id=%s AND delivery_config_version=%s",
                (
                    archive_schema,
                    mapping.account_id,
                    mapping.delivery_config_id,
                    mapping.delivery_config_version,
                ),
            )
        ).fetchone()
        expected = (mapping.consumer_id, digest, mapping.authority_reference)
        if prior:
            if prior != expected:
                raise ReportingOwnershipMigrationError("legacy ownership mapping is immutable")
            return
        for table, rows in evidence.items():
            columns = [
                r[0]
                for r in await (
                    await connection.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_sc"
                        "hema=current_schema() AND table_name=%s ORDER BY ordinal_position",
                        (table,),
                    )
                ).fetchall()
            ]
            for row in rows:
                # Copy original SQL values directly, not a JSON round trip. Owner
                # and quarantine are new metadata; immutable fields stay exact.
                selected: list[sql.Composable] = []
                params: list[Any] = []
                for column in columns:
                    if column == "consumer_id":
                        selected.append(sql.SQL("%s"))
                        params.append(mapping.consumer_id)
                    elif column == "quarantined":
                        selected.append(sql.SQL("TRUE"))
                    elif column in {"lease_worker_id", "lease_expires_at"}:
                        selected.append(sql.SQL("NULL"))
                    elif column in row:
                        selected.append(sql.Identifier(column))
                    else:
                        raise ReportingOwnershipMigrationError(
                            "legacy schema requires explicit reconciliation before import"
                        )
                pk = {
                    "reporting_configurations": "delivery_config_id",
                    "reporting_obligations": "reporting_obligation_id",
                    "reporting_revisions": "reporting_revision_id",
                    "reporting_adjustments": "reporting_adjustment_id",
                    "reporting_revision_rows": "reporting_revision_id",
                    "reporting_ledger_changes": "seq",
                }[table]
                where = sql.SQL("{}=%s").format(sql.Identifier(pk))
                params.append(row[pk])
                if table == "reporting_configurations":
                    where += sql.SQL(" AND account_id=%s AND delivery_config_version=%s")
                    params.extend((mapping.account_id, mapping.delivery_config_version))
                if table == "reporting_revision_rows":
                    where += sql.SQL(" AND ordinal=%s")
                    params.append(row["ordinal"])
                await connection.execute(
                    sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}.{} WHERE ").format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(selected),
                        sql.Identifier(archive_schema),
                        sql.Identifier(table),
                    )
                    + where,
                    params,
                )
        await connection.execute(
            "SELECT setval(pg_get_serial_sequence('reporting_ledger_changes','"
            "seq'), greatest(COALESCE((SELECT max(seq) FROM reporting_ledger_c"
            "hanges),0),1), EXISTS (SELECT 1 FROM reporting_ledger_changes))"
        )
        await connection.execute(
            "INSERT INTO adcp_reporting_ownership_backfills VALUES (%s,%s,%s,%"
            "s,%s,%s,%s,'retain_without_replay')",
            (
                archive_schema,
                mapping.account_id,
                mapping.delivery_config_id,
                mapping.delivery_config_version,
                mapping.consumer_id,
                digest,
                mapping.authority_reference,
            ),
        )
