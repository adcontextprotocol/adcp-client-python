"""Versioned migrations for the SDK's reporting storage (AdCP 3.3, experimental).

Each migration is a numbered file in this package, ``NNNN_name.sql``, applied
in order and recorded in ``adcp_schema_migrations``. A file's header carries
its metadata:

* ``-- adcp-migration: NNNN name`` names it;
* ``-- adcp-upgrade: stopped-worker`` marks an upgrade that workers of earlier
  releases cannot run beside: stop every reporting worker first;
* ``-- adcp-upgrade: contract`` marks the contract half of an expand/contract
  upgrade (below);
* ``-- adcp-upgrade: additive`` marks a migration that only creates new
  objects and depends on nothing a contract step changes, so it is applied
  even while a contract step waits (below);
* ``-- include: <path>`` applies one of the package's idempotent DDL files, in
  order, so each statement lives in exactly one place. Any other SQL in the
  file runs after its includes.

Run them with ``python -m adcp migrate`` (``--status`` lists what is applied
and pending, ``--dry-run`` prints the plan). Storage built by
``postgres_storage`` refuses to start on a schema that is behind, naming the
command and the pending versions.

Every migration runs in its own transaction under the reporting schema lock,
so concurrent runners serialize and a failure commits nothing.
``PgReportingLedgerStore.create_schema()`` applies the migrations made of the
DDL it owns (``0001``–``0003``), recorded the same way, inside its own
bootstrap transaction (:func:`apply_pending`), so a ledger created or upgraded
by either entry point carries its schema version; ``python -m adcp migrate``
then adds the later ones (``0004`` onward), which only ``postgres_storage``
and the notification stores need.

**Expand/contract.** A ledger created by adcp-reporting-storage 0.1.x or
0.2.0b0 (storage contract 1) is upgraded in two halves, so a rolling deploy
works. ``0002`` and ``0006`` *expand* it: processes of the earlier release keep
booting, reading and writing account-level generations beside this one, and a
rollback keeps working. ``0007`` *contracts* it to exactly a new ledger's
catalog, which reporting subscriptions need. The contract step runs only on
request, :data:`CONTRACT_COMMAND` (``migrate(contract=True)``), once every
earlier-release reporting process has stopped: plain ``python -m adcp migrate``
(typically a pre-deploy job, while earlier pods still run), ``create_schema()``
and :func:`apply_pending` stop before it. On a ledger that was never expanded
(a new one, or one that 0.2.0b1/b2 widened in place) the contract step changes
nothing and is applied and recorded without the request. ``postgres_storage``
starts on a ledger whose only pending migration is a contract step.

A migration after a waiting contract step is applied only when it is
``additive`` (``0008``, the issue-event outbox); the walk stops at the first
one that is not, since it may depend on the contract. An additive migration
is therefore applied, and recorded, before the contract step it follows.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from typing import Any

from adcp.reporting.ledger.store import LedgerConflictError

__all__ = [
    "CONTRACT_COMMAND",
    "CONTRACT_WHEN",
    "MIGRATE_COMMAND",
    "Migration",
    "MigrationStatus",
    "ReportingUpgradeNotContractedError",
    "applied_migrations",
    "apply_pending",
    "describe",
    "migrate",
    "migration_status",
    "migrations",
    "needs_contract",
    "pending_migrations",
]

#: The command that applies pending migrations.
MIGRATE_COMMAND = "python -m adcp migrate"
#: The command that also applies the contract step of an expanded ledger.
CONTRACT_COMMAND = "python -m adcp migrate --contract"
#: When to run :data:`CONTRACT_COMMAND`.
CONTRACT_WHEN = "once every earlier-release reporting process has stopped"

_TABLE = """
CREATE TABLE IF NOT EXISTS adcp_schema_migrations (
    version TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_LOCK = (
    "SELECT pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()))"
)
_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
# An expanded ledger keeps the four-column configuration key, with the widened
# one beside it under this name; an earlier release's ledger (to be expanded)
# has the four-column key and no offering_id. Either needs the contract step.
_NEEDS_CONTRACT = """
SELECT EXISTS (
    SELECT 1 FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = current_schema() AND t.relname = 'reporting_configurations'
      AND (c.conname = 'reporting_configurations_generation_key'
           OR (c.contype = 'p' AND NOT EXISTS (
               SELECT 1 FROM pg_attribute a
               WHERE a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                 AND a.attname = 'offering_id'))))
"""
# PostgreSQL: a CHECK constraint was violated.
_CHECK_VIOLATION = "23514"
_UNCONTRACTED_SUFFIX = "_uncontracted"


class ReportingUpgradeNotContractedError(LedgerConflictError):
    """A subscription generation was written before the contract step.

    An expanded ledger (upgraded from adcp-reporting-storage 0.1.x/0.2.0b0
    while processes of that release may still run) holds account-level
    generations only, under ``CHECK (offering_id = '')``. Code
    ``REPORTING_UPGRADE_NOT_CONTRACTED``; the message names the contract step.
    """

    CODE = "REPORTING_UPGRADE_NOT_CONTRACTED"

    def __init__(self) -> None:
        super().__init__(
            self.CODE,
            "this reporting ledger was upgraded from adcp-reporting-storage 0.1.x/0.2.0b0 and is"
            " not contracted yet, so it records account-level generations only: a reporting"
            " subscription's generation (offering_id) needs the contract step. Run"
            f' "{CONTRACT_COMMAND}" {CONTRACT_WHEN}.',
            field="offering_id",
        )


def upgrade_not_contracted_error(error: BaseException) -> ReportingUpgradeNotContractedError | None:
    """The typed error for a write an expanded ledger's ``CHECK (offering_id = '')`` refused."""
    if getattr(error, "sqlstate", None) != _CHECK_VIOLATION:
        return None
    diagnostic = getattr(error, "diag", None)
    constraint = getattr(diagnostic, "constraint_name", None) or ""
    if not constraint.endswith(_UNCONTRACTED_SUFFIX):
        return None
    return ReportingUpgradeNotContractedError()


@dataclass(frozen=True)
class Migration:
    """One numbered migration file."""

    version: str
    name: str
    stopped_worker: bool
    includes: tuple[str, ...]
    body: str
    description: str
    #: The contract half of an expand/contract upgrade: applied to an expanded
    #: ledger only on request (:data:`CONTRACT_COMMAND`).
    contract: bool = False
    #: Creates only new objects and depends on no contract step: applied even
    #: while an earlier contract step waits for :data:`CONTRACT_COMMAND`.
    additive: bool = False

    def statements(self) -> list[str]:
        """The SQL this migration runs, in order."""
        root = files("adcp_reporting_storage")
        result = [root.joinpath(path).read_text() for path in self.includes]
        if self.body.strip():
            result.append(self.body)
        return result


@dataclass(frozen=True)
class MigrationStatus:
    """A migration and whether, and when, it was applied."""

    migration: Migration
    applied_at: datetime | None

    @property
    def pending(self) -> bool:
        return self.applied_at is None


def _parse(filename: str, text: str) -> Migration:
    match = _FILE.match(filename)
    if match is None:
        raise ValueError(f"migration file {filename!r} is not named NNNN_name.sql")
    version, name = match.groups()
    includes: list[str] = []
    body: list[str] = []
    description: list[str] = []
    upgrade = ""
    declared: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("-- adcp-migration:"):
            declared = stripped.removeprefix("-- adcp-migration:").strip()
        elif stripped.startswith("-- adcp-upgrade:"):
            upgrade = stripped.removeprefix("-- adcp-upgrade:").strip()
        elif stripped.startswith("-- include:"):
            includes.append(stripped.removeprefix("-- include:").strip())
        elif stripped.startswith("--"):
            description.append(stripped.removeprefix("--").strip())
        else:
            body.append(line)
    if declared != f"{version} {name}":
        raise ValueError(f"migration file {filename!r} must declare '{version} {name}'")
    if upgrade not in ("", "stopped-worker", "contract", "additive"):
        raise ValueError(f"migration file {filename!r} declares an unknown upgrade {upgrade!r}")
    return Migration(
        version=version,
        name=name,
        stopped_worker=upgrade == "stopped-worker",
        includes=tuple(includes),
        body="\n".join(body),
        description=" ".join(item for item in description if item),
        contract=upgrade == "contract",
        additive=upgrade == "additive",
    )


def migrations() -> tuple[Migration, ...]:
    """Every migration this release ships, in version order."""
    root = files(__name__)
    found = [
        _parse(entry.name, entry.read_text())
        for entry in root.iterdir()
        if entry.name.endswith(".sql")
    ]
    found.sort(key=lambda item: item.version)
    versions = [item.version for item in found]
    if len(set(versions)) != len(versions):
        raise ValueError("two migration files share a version")
    return tuple(found)


async def applied_migrations(connection: Any) -> dict[str, datetime]:
    """``version`` -> ``applied_at`` for every recorded migration (empty when none).

    Reads the ``current_schema()`` table only: an unqualified name resolves
    through the whole ``search_path``, so another schema's record would
    otherwise report this one as migrated.
    """
    from psycopg import sql

    cursor = await connection.execute(
        "SELECT current_schema(),"
        " to_regclass(quote_ident(current_schema()) || '.adcp_schema_migrations') IS NOT NULL"
    )
    row = await cursor.fetchone()
    if not row or not row[1]:
        return {}
    cursor = await connection.execute(
        sql.SQL("SELECT version, applied_at FROM {}").format(
            sql.Identifier(row[0], "adcp_schema_migrations")
        )
    )
    return {str(version): applied_at for version, applied_at in await cursor.fetchall()}


async def needs_contract(connection: Any) -> bool:
    """Whether this schema's ledger is expanded, or is an earlier release's to be expanded.

    Such a ledger needs the contract step (:data:`CONTRACT_COMMAND`); a new
    ledger, or one already contracted or widened in place, does not.
    ``connection`` is a psycopg ``AsyncConnection`` or ``AsyncConnectionPool``.
    """
    if hasattr(connection, "getconn"):  # a pool
        async with connection.connection() as borrowed:
            return await needs_contract(borrowed)
    row = await (await connection.execute(_NEEDS_CONTRACT)).fetchone()
    return bool(row and row[0])


async def migration_status(connection: Any) -> list[MigrationStatus]:
    """Every shipped migration with its ``applied_at``, or ``None`` while pending.

    ``connection`` is a psycopg ``AsyncConnection`` or ``AsyncConnectionPool``.
    """
    if hasattr(connection, "getconn"):  # a pool
        async with connection.connection() as borrowed:
            return await migration_status(borrowed)
    async with connection.transaction():
        applied = await applied_migrations(connection)
    return [MigrationStatus(item, applied.get(item.version)) for item in migrations()]


async def pending_migrations(connection: Any) -> list[Migration]:
    """The shipped migrations not yet applied, in order."""
    return [item.migration for item in await migration_status(connection) if item.pending]


async def migrate(pool: Any, *, dry_run: bool = False, contract: bool = False) -> list[Migration]:
    """Apply every pending migration in order; return what was (or would be) applied.

    ``pool`` is a psycopg ``AsyncConnectionPool`` or ``AsyncConnection`` whose
    ``search_path`` selects the schema. Each migration commits on its own, so a
    failure leaves the earlier ones applied and the failed one pending.

    The walk holds a contract step whose ledger is expanded (or is an earlier
    release's, which this run expands) unless ``contract`` is set: pass it only
    once every earlier-release reporting process has stopped. Past a held
    contract step it applies only ``additive`` migrations, and stops at the
    first one that is not.
    """
    if hasattr(pool, "getconn"):  # a pool
        async with pool.connection() as connection:
            return await _migrate(connection, dry_run=dry_run, contract=contract)
    return await _migrate(pool, dry_run=dry_run, contract=contract)


async def _migrate(connection: Any, *, dry_run: bool, contract: bool) -> list[Migration]:
    pending = await pending_migrations(connection)
    if dry_run:
        if not contract and any(item.contract for item in pending):
            async with connection.transaction():
                expanded = await needs_contract(connection)
            if expanded:
                return _past_held_contract(pending)
        return pending
    applied: list[Migration] = []
    held = False
    for migration in pending:
        async with connection.transaction():
            await connection.execute(_LOCK)
            if held and not migration.additive:
                break  # it may depend on the contract step still waiting
            if migration.contract and not contract and await needs_contract(connection):
                # The operator asks for the contract step explicitly; what
                # follows it is applied only when it is additive.
                held = True
                continue
            if await _apply(connection, migration):
                applied.append(migration)
    return applied


def _past_held_contract(pending: Sequence[Migration]) -> list[Migration]:
    """The plan when every pending contract step is held: up to the first, then additive only.

    Mirrors :func:`_migrate` step for step: past the held contract step the
    walk stops at the first migration that is not additive, another contract
    step included.
    """
    plan: list[Migration] = []
    held = False
    for migration in pending:
        if held and not migration.additive:
            break
        if migration.contract:
            held = True
            continue
        plan.append(migration)
    return plan


async def apply_pending(
    connection: Any, *, within: frozenset[str] | None = None
) -> list[Migration]:
    """Apply pending migrations on ``connection`` inside the caller's transaction.

    The bootstrap path of ``create_schema()``: the caller holds one transaction
    for the whole upgrade, so every migration commits with it or none does.
    Takes the reporting schema lock first, like :func:`migrate`, so a booting
    worker and ``python -m adcp migrate`` serialize instead of racing.

    ``within`` bounds what the caller owns: migrations are applied in order
    and the walk stops at the first pending one that includes a file outside
    it (a later migration may depend on an earlier one, so none is skipped).
    The rest stay pending for ``python -m adcp migrate``. The walk also stops
    at a contract step that the ledger needs: a booting process never
    contracts, since processes of the earlier release may still be running.
    """
    await connection.execute(_LOCK)
    await connection.execute(_TABLE)
    applied = await applied_migrations(connection)
    result: list[Migration] = []
    for migration in migrations():
        if migration.version in applied:
            continue
        if within is not None and not set(migration.includes) <= within:
            break
        if migration.contract and await needs_contract(connection):
            break
        if await _apply(connection, migration):
            result.append(migration)
    return result


async def _apply(connection: Any, migration: Migration) -> bool:
    """Run one migration and record it; ``False`` when a concurrent runner already had."""
    from adcp_reporting_storage.migration import require_owned_schema_or_empty

    await connection.execute(_LOCK)
    await connection.execute(_TABLE)
    if migration.version in await applied_migrations(connection):
        return False  # another runner applied it while this one waited
    await require_owned_schema_or_empty(connection)
    for statement in migration.statements():
        await connection.execute(statement)
    await connection.execute(
        "INSERT INTO adcp_schema_migrations (version, name) VALUES (%s, %s)",
        (migration.version, migration.name),
    )
    return True


def describe(statuses: Sequence[MigrationStatus]) -> list[str]:
    """``--status`` lines: one per migration, flagging stopped-worker and contract steps."""
    lines: list[str] = []
    for status in statuses:
        item = status.migration
        state = "pending" if status.pending else f"applied {status.applied_at:%Y-%m-%dT%H:%M:%SZ}"
        if item.contract:
            note = (
                f"  [contract: run {CONTRACT_WHEN}: {CONTRACT_COMMAND}]"
                if status.pending
                else "  [contract]"
            )
        elif item.stopped_worker:
            note = (
                "  [stopped-worker upgrade: stop every reporting worker before migrating]"
                if status.pending
                else "  [stopped-worker upgrade]"
            )
        else:
            note = ""
        lines.append(f"{item.version} {item.name}: {state}{note}")
    return lines
