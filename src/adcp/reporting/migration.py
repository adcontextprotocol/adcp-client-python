"""Explicit, stopped-worker upgrade of reporting state lacking caller ownership.

These operator APIs must never be mounted as buyer tools. They preserve the
legacy schema as a read-only archive and import only verified Core evidence.
Queues, leases, destination bindings and signing material are never imported.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Generator
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
    "replace_empty_legacy_reporting",
    "replace_empty_legacy_reporting_sync",
    "replace_empty_owned_reporting",
    "replace_empty_owned_reporting_sync",
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
            "If all legacy tables are empty, call replace_empty_legacy_reporting. "
            "Otherwise: Stop reporting workers and run migrate_legacy_reporting "
            "before create_schema"
        )


@dataclass(frozen=True)
class _Statement:
    """One statement of an empty-ledger replacement, as either driver runs it."""

    query: Any
    params: tuple[Any, ...] | None = None
    returns_rows: bool = False


_Rows = list[tuple[Any, ...]]
# A replacement is written once, as a generator that yields each statement and
# is sent back its rows. Every guard and every statement lives there; the sync
# and async drivers below only execute, so both enforce the same checks in the
# same order. ``sql`` is the driver's composition module: psycopg 3 and psycopg2
# expose the same ``sql.SQL``/``sql.Identifier`` API.
_Steps = Generator[_Statement, _Rows, None]

_SCHEMA_LOCK = (
    "SELECT pg_advisory_xact_lock(hashtext('adcp.reporting.schema'),hashtext(current_schema()))"
)
_ARCHIVE_REGISTRY = "adcp_reporting_ownership_archives"
# Rows that schema installation or service startup writes, by the predicate they
# match before any reporting happens: the change-feed horizon singleton until the
# first prune, and the row-storage bindings each boot registers (row bytes are
# tracked in the row-set, chunk and write-intent tables, which must be empty).
# They are configuration, not reporting state, so alone they leave a ledger empty.
_STARTUP_ROWS = {
    "adcp_reporting_change_horizon": "pruned_through_seq = 0 AND pruned_through_xid = '0'",
    "adcp_reporting_row_bindings": "TRUE",
}
_DEPENDENT_OBJECTS_STILL_EXIST = "2BP01"
_SAVEPOINT = "adcp_reporting_replacement"


def _query(query: Any, params: tuple[Any, ...] | None = None) -> _Statement:
    return _Statement(query, params, returns_rows=True)


def _drop_functions(sql: Any, names: list[str]) -> _Statement:
    # regprocedure text is emitted by PostgreSQL, never caller-supplied SQL.
    return _Statement(
        sql.SQL("DROP FUNCTION {} RESTRICT").format(sql.SQL(", ").join(map(sql.SQL, names)))
    )


def _replacement_steps(sql: Any, *, owned: bool) -> _Steps:
    """Lock, verify and drop an empty reporting surface of the expected shape.

    ``owned=False`` removes a pre-ownership (8.x) ledger before ``create_schema``;
    ``owned=True`` removes the caller-owned ledger before an 8.x image starts.
    """
    surface = "owned" if owned else "legacy"
    isolation = (yield _query("SHOW transaction_isolation"))[0][0]
    if isolation != "read committed":
        raise ReportingOwnershipMigrationError(
            f"empty {surface} replacement requires READ COMMITTED isolation"
        )
    yield _Statement(_SCHEMA_LOCK)
    current = (yield _query("SELECT current_schema()"))[0][0]
    columns = yield _query(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s"
        " AND table_name='reporting_configurations'",
        (current,),
    )
    if owned:
        if ("consumer_id",) not in columns:
            raise ReportingOwnershipMigrationError("expected a caller-owned reporting schema")
    elif not columns or ("consumer_id",) in columns:
        raise ReportingOwnershipMigrationError("expected an unmigrated legacy reporting schema")
    tables = yield _query(
        "SELECT c.oid, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n"
        " ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN ('r','p','f')"
        " AND (c.relname LIKE 'reporting\\_%%' ESCAPE '\\'"
        " OR c.relname LIKE 'adcp\\_reporting\\_%%' ESCAPE '\\') ORDER BY c.relname",
        (current,),
    )
    if any(kind == "f" for _, _, kind in tables):
        raise ReportingOwnershipMigrationError("foreign reporting tables cannot be verified")
    oids = [oid for oid, _, _ in tables]
    for _, table, _ in tables:
        yield _Statement(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(current, table))
        )
    hidden_rows = yield _query(
        "SELECT 1 FROM pg_class WHERE oid=ANY(%s::oid[]) AND row_security_active(oid) LIMIT 1",
        (oids,),
    )
    if hidden_rows:
        raise ReportingOwnershipMigrationError(
            f"row security prevents verifying all {surface} reporting rows"
        )
    # Dropping a partitioned table can implicitly remove children. Only
    # children in the same verified reporting surface may be removed.
    outside_children = yield _query(
        "SELECT 1 FROM pg_inherits WHERE inhparent=ANY(%s::oid[])"
        " AND NOT inhrelid=ANY(%s::oid[]) LIMIT 1",
        (oids, oids),
    )
    if outside_children:
        raise ReportingOwnershipMigrationError("reporting tables have external children")
    if owned and any(table == _ARCHIVE_REGISTRY for _, table, _ in tables):
        # A registered archive means migrate_legacy_reporting moved a populated
        # legacy ledger aside. Dropping the registry would orphan that archive,
        # and the legacy rows are not restored by removing the owned ledger.
        registered = yield _query(
            sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(current, _ARCHIVE_REGISTRY))
        )
        if registered:
            raise ReportingOwnershipMigrationError(
                "owned reporting registers a legacy quarantine archive; restore the"
                " pre-upgrade backup instead"
            )
    for _, table, _ in tables:
        rows = sql.SQL("SELECT 1 FROM {}").format(sql.Identifier(current, table))
        if table in _STARTUP_ROWS:
            rows += sql.SQL(" WHERE NOT ({})").format(sql.SQL(_STARTUP_ROWS[table]))
        populated = yield _query(rows + sql.SQL(" LIMIT 1"))
        if populated:
            raise ReportingOwnershipMigrationError(
                "owned reporting contains rows; nothing was removed"
                if owned
                else "legacy reporting contains rows; use migrate_legacy_reporting"
            )
    triggers = yield _query(
        "SELECT t.tgname, c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid"
        " WHERE t.tgrelid=ANY(%s::oid[]) AND NOT t.tgisinternal",
        (oids,),
    )
    # Remove only triggers on the locked, verified empty tables.
    for trigger, table in triggers:
        yield _Statement(
            sql.SQL("DROP TRIGGER {} ON {} RESTRICT").format(
                sql.Identifier(trigger), sql.Identifier(current, table)
            )
        )
    # A function that takes a table's row type (or an array of it), or whose
    # SQL-standard body references a table, depends on it and goes before the
    # tables. A CHECK constraint depends on the function it calls, so every
    # other function goes after them.
    functions = yield _query(
        "SELECT p.oid::regprocedure::text, EXISTS (SELECT 1 FROM pg_depend d"
        " LEFT JOIN pg_type t ON d.refclassid='pg_type'::regclass AND t.oid=d.refobjid"
        " LEFT JOIN pg_type e ON e.oid=t.typelem"
        " WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid"
        " AND (d.refclassid='pg_class'::regclass AND d.refobjid=ANY(%s::oid[])"
        " OR t.typrelid=ANY(%s::oid[]) OR e.typrelid=ANY(%s::oid[])))"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=%s"
        " AND (p.proname LIKE 'reporting\\_%%' ESCAPE '\\'"
        " OR p.proname LIKE 'adcp\\_reporting\\_%%' ESCAPE '\\') ORDER BY 1",
        (oids, oids, oids, current),
    )
    row_typed = [name for name, typed in functions if typed]
    if row_typed:
        yield _drop_functions(sql, row_typed)
    if tables:
        yield _Statement(
            sql.SQL("DROP TABLE {} RESTRICT").format(
                sql.SQL(", ").join(sql.Identifier(current, table) for _, table, _ in tables)
            )
        )
    untyped = [name for name, typed in functions if not typed]
    if untyped:
        yield _drop_functions(sql, untyped)
    sequences = yield _query(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
        " WHERE n.nspname=%s AND c.relkind='S'"
        " AND (c.relname LIKE 'reporting\\_%%' ESCAPE '\\'"
        " OR c.relname LIKE 'adcp\\_reporting\\_%%' ESCAPE '\\')",
        (current,),
    )
    if sequences:
        yield _Statement(
            sql.SQL("DROP SEQUENCE {} RESTRICT").format(
                sql.SQL(", ").join(sql.Identifier(current, name) for (name,) in sequences)
            )
        )


def _dependency_error(*, owned: bool) -> ReportingOwnershipMigrationError:
    surface, outcome = ("owned", "removed") if owned else ("legacy", "replaced")
    return ReportingOwnershipMigrationError(
        f"{surface} reporting has external dependencies; nothing was {outcome}"
    )


def _is_dependency_failure(exc: BaseException) -> bool:
    # psycopg 3 exposes the SQLSTATE as ``sqlstate``, psycopg2 as ``pgcode``.
    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    return code == _DEPENDENT_OBJECTS_STILL_EXIST


async def _replace_async(connection: Any, *, owned: bool) -> None:
    from psycopg import sql

    steps = _replacement_steps(sql, owned=owned)
    try:
        async with connection.transaction():
            statement = next(steps)
            while True:
                cursor = await connection.execute(statement.query, statement.params)
                rows = await cursor.fetchall() if statement.returns_rows else []
                try:
                    statement = steps.send(rows)
                except StopIteration:
                    break
    except Exception as exc:
        if _is_dependency_failure(exc):
            raise _dependency_error(owned=owned) from exc
        raise


def _sync_driver(connection: Any) -> tuple[Any, Any]:
    """Return the DB-API connection to run on and its ``sql`` composition module.

    A SQLAlchemy ``Connection`` (Alembic's ``op.get_bind()``) is unwrapped to its
    driver connection, which carries the same transaction. As any statement on
    it would, this begins the SQLAlchemy transaction if none is open.
    """
    driver = connection
    if hasattr(getattr(connection, "connection", None), "driver_connection"):
        if not connection.in_transaction():
            connection.begin()
        driver = connection.connection.driver_connection
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg2-only installation
        pass
    else:
        if isinstance(driver, psycopg.Connection):
            return driver, psycopg.sql
    try:
        import psycopg2.extensions  # type: ignore[import-untyped,import-not-found,unused-ignore]
        import psycopg2.sql  # type: ignore[import-untyped,import-not-found,unused-ignore]
    except ImportError:
        pass
    else:
        if isinstance(driver, psycopg2.extensions.connection):
            return driver, psycopg2.sql
    raise TypeError(
        "the sync replacement needs a sync psycopg 3 or psycopg2 connection, or a"
        " SQLAlchemy Connection on either driver; await the async variant otherwise"
    )


def _replace_sync(connection: Any, *, owned: bool) -> None:
    driver, sql = _sync_driver(connection)
    steps = _replacement_steps(sql, owned=owned)
    # Outside any transaction on an autocommit connection, the replacement is its
    # own transaction. Otherwise it joins the caller's (the driver opens one on a
    # non-autocommit connection), never commits it, and runs in a savepoint so a
    # refusal leaves the caller's transaction usable. Both drivers report libpq's
    # transaction status, where 0 is idle.
    if driver.autocommit and driver.info.transaction_status == 0:
        undo: tuple[str, ...] = ("ROLLBACK",)
        begin, release = "BEGIN", "COMMIT"
    else:
        begin = f"SAVEPOINT {_SAVEPOINT}"
        release = f"RELEASE SAVEPOINT {_SAVEPOINT}"
        undo = (f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}", release)
    try:
        with driver.cursor() as cursor:
            cursor.execute(begin)
            try:
                statement = next(steps)
                while True:
                    cursor.execute(statement.query, statement.params)
                    rows = cursor.fetchall() if statement.returns_rows else []
                    try:
                        statement = steps.send(rows)
                    except StopIteration:
                        break
            except BaseException:
                # A broken connection cannot undo; report the original failure.
                with contextlib.suppress(Exception):
                    for command in undo:
                        cursor.execute(command)
                raise
            cursor.execute(release)
    except Exception as exc:
        if _is_dependency_failure(exc):
            raise _dependency_error(owned=owned) from exc
        raise


async def replace_empty_legacy_reporting(connection: Any) -> None:
    """Remove an entirely empty pre-ownership ledger without creating an archive.

    Call this explicitly on an operator connection, then call ``create_schema``.
    The transaction locks every retained reporting table before checking rows.
    Populated ledgers and dependencies outside the reporting surface are refused;
    failure rolls back every drop. Reporting admission must remain disabled
    until all processes use the caller-owned API.

    With no transaction open on the connection, this commits its own
    transaction, on an autocommit connection or not. Inside a transaction
    already open on the connection, it runs as a savepoint of it.
    From a synchronous migration such as an Alembic revision, use
    :func:`replace_empty_legacy_reporting_sync`.
    """
    await _replace_async(connection, owned=False)


def replace_empty_legacy_reporting_sync(connection: Any) -> None:
    """Synchronous :func:`replace_empty_legacy_reporting`, inside the caller's transaction.

    Accepts a sync psycopg 3 or psycopg2 connection, or a SQLAlchemy
    ``Connection`` on either driver, such as Alembic's ``op.get_bind()``. Unless
    the connection is in autocommit mode with no transaction open, the drop joins
    the caller's transaction and commits with it, so it is atomic with a
    migration's version bump; the locks are held until then. A refusal rolls back only this
    function's savepoint. It never starts an event loop.
    """
    _replace_sync(connection, owned=False)


async def replace_empty_owned_reporting(connection: Any) -> None:
    """Remove an entirely empty caller-owned ledger, so an 8.x image can start.

    The reverse of :func:`replace_empty_legacy_reporting`, with the same
    guarantees: READ COMMITTED isolation, the schema advisory lock, then
    ``ACCESS EXCLUSIVE`` on every ``reporting_*`` and ``adcp_reporting_*`` table
    before any row is checked. A populated table, a registered legacy
    quarantine archive, active row security or a dependency outside the
    reporting surface is refused, and nothing is removed. Every reporting
    process must be stopped and stay stopped until the 8.x image is deployed.
    """
    await _replace_async(connection, owned=True)


def replace_empty_owned_reporting_sync(connection: Any) -> None:
    """Synchronous :func:`replace_empty_owned_reporting`, inside the caller's transaction.

    Accepts the same connections, with the same transaction behaviour, as
    :func:`replace_empty_legacy_reporting_sync` — for example from an Alembic
    ``downgrade()``.
    """
    _replace_sync(connection, owned=True)


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
