"""Read-only required-object validation; unrelated adopter DDL is permitted.

The manifest is generated from a clean, packaged migration chain. Each required
column, constraint, index, trigger, and guard function is checked independently.
Diagnostics contain only a static classification and a bundled object name.
Catalog queries are schema-scoped and never inspect tenant rows.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError

REQUIRED_OBJECTS: dict[str, dict[str, Any]] = json.loads(
    files("adcp.reporting.outbox").joinpath("required_schema.json").read_text()
)
SCHEMA_CONTRACT: dict[str, str] = {
    key: value["fingerprint"] for key, value in REQUIRED_OBJECTS.items()
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def schema_objects(connection: Any) -> dict[str, dict[str, Any]]:
    tables = await (
        await connection.execute(
            "SELECT c.oid, c.relname, c.relkind, c.relpersistence FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema()"
            " AND c.relkind IN ('r','p') AND starts_with(c.relname, 'reporting_')"
            " ORDER BY c.relname"
        )
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}

    def remember(key: str, value: object, *, enabled: bool = True) -> None:
        result[key] = {"fingerprint": _digest(value), "enabled": enabled}

    for oid, name, kind, persistence in tables:
        remember(f"table:{name}", (kind, persistence))
        columns = await (
            await connection.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, co.collname,"
                " replace(pg_get_expr(d.adbin, d.adrelid),"
                " quote_ident(current_schema()) || '.', ''),"
                " a.attidentity, a.attgenerated FROM pg_attribute a"
                " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
                " LEFT JOIN pg_collation co ON co.oid = a.attcollation WHERE a.attrelid = %s"
                " AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attname",
                (oid,),
            )
        ).fetchall()
        for row in columns:
            remember(f"column:{name}.{row[0]}", row[1:])
        constraints = await (
            await connection.execute(
                "SELECT conname, contype, replace(pg_get_constraintdef(oid),"
                " quote_ident(current_schema()) || '.', ''),"
                " convalidated, condeferrable, condeferred"
                " FROM pg_constraint WHERE conrelid = %s ORDER BY conname",
                (oid,),
            )
        ).fetchall()
        for row in constraints:
            remember(f"constraint:{name}.{row[0]}", row[1:], enabled=bool(row[3]))
        indexes = await (
            await connection.execute(
                "SELECT c.relname, i.indisunique, i.indisvalid, i.indisready, i.indislive,"
                " replace(pg_get_indexdef(i.indexrelid), quote_ident(current_schema()) || '.', '')"
                " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                " WHERE i.indrelid = %s ORDER BY c.relname",
                (oid,),
            )
        ).fetchall()
        for row in indexes:
            remember(f"index:{name}.{row[0]}", row[1:], enabled=all(row[2:5]))
        triggers = await (
            await connection.execute(
                "SELECT tgname, tgenabled, replace(pg_get_triggerdef(oid),"
                " quote_ident(current_schema()) || '.', '') FROM pg_trigger WHERE tgrelid = %s"
                " AND NOT tgisinternal ORDER BY tgname",
                (oid,),
            )
        ).fetchall()
        for row in triggers:
            remember(f"trigger:{name}.{row[0]}", row[1:], enabled=row[1] != "D")
    functions = await (
        await connection.execute(
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid), p.prosrc,"
            " l.lanname, p.provolatile, p.proisstrict, p.prosecdef, p.proconfig,"
            " pg_get_function_result(p.oid), p.proparallel, p.proleakproof FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang"
            " WHERE n.nspname = current_schema() AND starts_with(p.proname, 'reporting_')"
            " ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)"
        )
    ).fetchall()
    for row in functions:
        remember(f"function:{row[0]}({row[1]})", row[2:])
    return result


async def schema_contract(connection: Any) -> dict[str, str]:
    return {key: value["fingerprint"] for key, value in (await schema_objects(connection)).items()}


async def validate_schema(connection: Any, *, activity: bool = False) -> None:
    try:
        installed = await schema_objects(connection)
    except Exception:
        raise ReportingNotificationError(
            "notification_schema_unready:catalog_unavailable"
        ) from None
    _validate_schema_objects(installed, activity=activity)


def _validate_schema_objects(
    installed: dict[str, dict[str, Any]], *, activity: bool = False
) -> None:
    """Apply the packaged B contract to an already captured catalog."""
    if not REQUIRED_OBJECTS:
        raise ReportingNotificationError("notification_schema_unready:manifest_missing")
    for key, expected in REQUIRED_OBJECTS.items():
        if not activity and "reporting_webhook_" in key:
            continue
        actual = installed.get(key)
        if actual is None:
            classification = "missing"
        elif not actual["enabled"]:
            classification = "disabled"
        elif actual["fingerprint"] != expected["fingerprint"]:
            classification = "changed"
        else:
            continue
        raise ReportingNotificationError(f"notification_schema_unready:{classification}:{key}")
