"""Read-only required-object validation; unrelated adopter DDL is permitted.

The manifest is generated from a clean, packaged migration chain. Each required
column, constraint, index, trigger, and guard function is checked independently.
Diagnostics contain only a static classification and a bundled object name.
Catalog queries are schema-scoped and never inspect tenant rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from typing import Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError

REQUIRED_OBJECTS: dict[str, dict[str, Any]] = json.loads(
    files("adcp.reporting.outbox").joinpath("required_schema.json").read_text()
)
PROVISIONAL_REQUIRED_OBJECTS: dict[str, dict[str, Any]] = json.loads(
    files("adcp.reporting.ledger").joinpath("required_provisional_schema.json").read_text()
)
SCHEMA_CONTRACT: dict[str, str] = {
    key: value["fingerprint"] for key, value in REQUIRED_OBJECTS.items()
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def schema_objects(connection: Any) -> dict[str, dict[str, Any]]:
    return await _catalog_objects(connection)


_CATALOG_PREFIXES = ("reporting_", "adcp_")


async def _catalog_objects(
    connection: Any,
    *,
    table_names: tuple[str, ...] | None = None,
    function_identity: tuple[str, str] | None = None,
    name_prefix: str = "reporting_",
) -> dict[str, dict[str, Any]]:
    """Read fresh catalog state with the same fingerprints for either scope.

    ``name_prefix`` selects the object namespace. Every notification and
    extension manifest covers ``reporting_*``; the private shared row-storage
    tables are ``adcp_*`` and pass an explicit table list, so unrelated adopter
    objects stay out of scope there too.
    """
    if name_prefix not in _CATALOG_PREFIXES:
        raise ValueError("unsupported catalog prefix")

    def scoped(query: str) -> str:
        return query.replace("'reporting_'", f"'{name_prefix}'")

    table_query = (
        "SELECT c.oid, c.relname, c.relkind, c.relpersistence FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema()"
        " AND c.relkind IN ('r','p') AND starts_with(c.relname, 'reporting_')"
        " ORDER BY c.relname"
    )
    if table_names is not None:
        table_query = (
            "SELECT c.oid, c.relname, c.relkind, c.relpersistence FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema()"
            " AND c.relkind IN ('r','p') AND starts_with(c.relname, 'reporting_')"
            " AND c.relname = ANY(%s::text[]) ORDER BY c.relname"
        )
    tables = await (
        await connection.execute(
            scoped(table_query),
            (list(table_names),) if table_names is not None else None,
        )
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}

    def remember(key: str, value: object, *, enabled: bool = True) -> None:
        result[key] = {"fingerprint": _digest(value), "enabled": enabled}

    names = {oid: name for oid, name, _, _ in tables}
    for _, name, kind, persistence in tables:
        remember(f"table:{name}", (kind, persistence))
    # Capture each object kind for the selected tables in one query. These are
    # still fresh catalog reads on the caller's connection; no cache can hide
    # DDL drift. Round trips no longer grow with the number of reporting tables.
    oids = list(names)
    columns = await (
        await connection.execute(
            "SELECT a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod),"
            " a.attnotnull, co.collname, replace(pg_get_expr(d.adbin, d.adrelid),"
            " quote_ident(current_schema()) || '.', ''),"
            " a.attidentity, a.attgenerated FROM pg_attribute a"
            " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
            " LEFT JOIN pg_collation co ON co.oid = a.attcollation"
            " WHERE a.attrelid = ANY(%s::oid[])"
            " AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attrelid, a.attname",
            (oids,),
        )
    ).fetchall()
    for oid, *row in columns:
        remember(f"column:{names[oid]}.{row[0]}", row[1:])
    constraints = await (
        await connection.execute(
            "SELECT conrelid, conname, contype, replace(pg_get_constraintdef(oid),"
            " quote_ident(current_schema()) || '.', ''),"
            " convalidated, condeferrable, condeferred"
            " FROM pg_constraint WHERE conrelid = ANY(%s::oid[]) ORDER BY conrelid, conname",
            (oids,),
        )
    ).fetchall()
    for oid, *row in constraints:
        remember(f"constraint:{names[oid]}.{row[0]}", row[1:], enabled=bool(row[3]))
    indexes = await (
        await connection.execute(
            "SELECT i.indrelid, c.relname, i.indisunique, i.indisvalid, i.indisready, i.indislive,"
            " replace(pg_get_indexdef(i.indexrelid), quote_ident(current_schema()) || '.', '')"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE i.indrelid = ANY(%s::oid[]) ORDER BY i.indrelid, c.relname",
            (oids,),
        )
    ).fetchall()
    for oid, *row in indexes:
        remember(f"index:{names[oid]}.{row[0]}", row[1:], enabled=all(row[2:5]))
    triggers = await (
        await connection.execute(
            "SELECT tgrelid, tgname, tgenabled, replace(pg_get_triggerdef(oid),"
            " quote_ident(current_schema()) || '.', '') FROM pg_trigger"
            " WHERE tgrelid = ANY(%s::oid[]) AND NOT tgisinternal ORDER BY tgrelid, tgname",
            (oids,),
        )
    ).fetchall()
    for oid, *row in triggers:
        remember(f"trigger:{names[oid]}.{row[0]}", row[1:], enabled=row[1] != "D")
    function_query = (
        "SELECT p.proname, pg_get_function_identity_arguments(p.oid), p.prosrc,"
        " l.lanname, p.provolatile, p.proisstrict, p.prosecdef, p.proconfig,"
        " pg_get_function_result(p.oid), p.proparallel, p.proleakproof FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang"
        " WHERE n.nspname = current_schema() AND starts_with(p.proname, 'reporting_')"
        " ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)"
    )
    if function_identity is not None:
        function_query = (
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid), p.prosrc,"
            " l.lanname, p.provolatile, p.proisstrict, p.prosecdef, p.proconfig,"
            " pg_get_function_result(p.oid), p.proparallel, p.proleakproof FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang"
            " WHERE n.nspname = current_schema() AND starts_with(p.proname, 'reporting_')"
            " AND p.proname=%s AND pg_get_function_identity_arguments(p.oid)=%s"
            " ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)"
        )
    functions = await (
        await connection.execute(scoped(function_query), function_identity)
    ).fetchall()
    for row in functions:
        remember(f"function:{row[0]}({row[1]})", row[2:])
    return result


async def schema_contract(connection: Any) -> dict[str, str]:
    return {key: value["fingerprint"] for key, value in (await schema_objects(connection)).items()}


LEGACY_ROW_ARCHIVE = "adcp_reporting_legacy_revision_rows_v0"
_LEGACY_ROW_OBJECT = re.compile(r"^[a-z]+:reporting_revision_rows(?:\.|$)")


def is_legacy_row_object(key: str) -> bool:
    """Whether a manifest key belongs to the retired ``reporting_revision_rows`` table."""
    return _LEGACY_ROW_OBJECT.match(key) is not None


async def legacy_rows_retired(connection: Any, installed: dict[str, dict[str, Any]]) -> bool:
    """Whether row storage retired the legacy inline-row table by renaming it.

    Chunked rows replace ``reporting_revision_rows``. Once every revision is
    migrated or quarantined the table is renamed (never truncated) so a binary
    that predates chunked rows fails closed. The renamed table satisfies the
    manifest. A table that is merely missing still does not, and the extra
    query runs only when that table is absent.
    """
    if "table:reporting_revision_rows" in installed:
        return False
    row = await (
        await connection.execute(
            "SELECT to_regclass(quote_ident(current_schema()) || '.' || %s)", (LEGACY_ROW_ARCHIVE,)
        )
    ).fetchone()
    return row is not None and row[0] is not None


async def validate_schema(connection: Any, *, activity: bool = False) -> None:
    try:
        installed = await schema_objects(connection)
        retired = await legacy_rows_retired(connection, installed)
    except Exception:
        raise ReportingNotificationError(
            "notification_schema_unready:catalog_unavailable"
        ) from None
    _validate_schema_objects(installed, activity=activity, legacy_rows_retired=retired)


def _validate_schema_objects(
    installed: dict[str, dict[str, Any]],
    *,
    activity: bool = False,
    legacy_rows_retired: bool = False,
) -> None:
    """Apply the packaged B contract to an already captured catalog."""
    if not REQUIRED_OBJECTS:
        raise ReportingNotificationError("notification_schema_unready:manifest_missing")
    # A historical notification-only installation may have no provisional
    # capability. Once any exact known extension object exists, its entire
    # contract is required; unrelated adopter DDL remains unrelated.
    required = REQUIRED_OBJECTS
    if any(key in installed for key in PROVISIONAL_REQUIRED_OBJECTS):
        required = {**required, **PROVISIONAL_REQUIRED_OBJECTS}
    for key, expected in required.items():
        if not activity and "reporting_webhook_" in key:
            continue
        if legacy_rows_retired and is_legacy_row_object(key):
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


async def validate_provisional_schema(connection: Any) -> None:
    """Require the complete observation extension even when none of it exists."""
    from adcp.reporting.ledger.store import LedgerConflictError

    try:
        installed = await _catalog_objects(
            connection,
            table_names=(
                "reporting_provisional_acquisitions",
                "reporting_provisional_observations",
            ),
            function_identity=("reporting_provisional_immutable", ""),
        )
    except Exception:
        raise LedgerConflictError(
            "PROVISIONAL_SCHEMA_UNREADY", "provisional_schema_unready:catalog_unavailable"
        ) from None
    if not PROVISIONAL_REQUIRED_OBJECTS:
        raise LedgerConflictError(
            "PROVISIONAL_SCHEMA_UNREADY", "provisional_schema_unready:manifest_missing"
        )
    for key, expected in PROVISIONAL_REQUIRED_OBJECTS.items():
        actual = installed.get(key)
        if actual is None:
            classification = "missing"
        elif not actual["enabled"]:
            classification = "disabled"
        elif actual["fingerprint"] != expected["fingerprint"]:
            classification = "changed"
        else:
            continue
        raise LedgerConflictError(
            "PROVISIONAL_SCHEMA_UNREADY", f"provisional_schema_unready:{classification}:{key}"
        )
