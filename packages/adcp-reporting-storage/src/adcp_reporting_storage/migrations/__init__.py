"""Versioned migrations for the SDK's reporting storage (AdCP 3.3, experimental).

Each migration is a numbered file in this package, ``NNNN_name.sql``, applied
in order and recorded in ``adcp_schema_migrations``. A file's header carries
its metadata:

* ``-- adcp-migration: NNNN name`` names it;
* ``-- adcp-upgrade: stopped-worker`` marks an upgrade that workers of earlier
  releases cannot run beside: stop every reporting worker first;
* ``-- include: <path>`` applies one of the package's idempotent DDL files, in
  order, so each statement lives in exactly one place. Any other SQL in the
  file runs after its includes.

Run them with ``python -m adcp migrate`` (``--status`` lists what is applied
and pending, ``--dry-run`` prints the plan). Storage built by
``postgres_storage`` refuses to start on a schema that is behind, naming the
command and the pending versions.

Every migration runs in its own transaction under the reporting schema lock,
so concurrent runners serialize and a failure commits nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from typing import Any

__all__ = [
    "MIGRATE_COMMAND",
    "Migration",
    "MigrationStatus",
    "applied_migrations",
    "migrate",
    "migration_status",
    "migrations",
    "pending_migrations",
]

#: The command that applies pending migrations.
MIGRATE_COMMAND = "python -m adcp migrate"

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


@dataclass(frozen=True)
class Migration:
    """One numbered migration file."""

    version: str
    name: str
    stopped_worker: bool
    includes: tuple[str, ...]
    body: str
    description: str

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
    stopped_worker = False
    declared: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("-- adcp-migration:"):
            declared = stripped.removeprefix("-- adcp-migration:").strip()
        elif stripped.startswith("-- adcp-upgrade:"):
            stopped_worker = stripped.removeprefix("-- adcp-upgrade:").strip() == "stopped-worker"
        elif stripped.startswith("-- include:"):
            includes.append(stripped.removeprefix("-- include:").strip())
        elif stripped.startswith("--"):
            description.append(stripped.removeprefix("--").strip())
        else:
            body.append(line)
    if declared != f"{version} {name}":
        raise ValueError(f"migration file {filename!r} must declare '{version} {name}'")
    return Migration(
        version=version,
        name=name,
        stopped_worker=stopped_worker,
        includes=tuple(includes),
        body="\n".join(body),
        description=" ".join(item for item in description if item),
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
    """``version`` -> ``applied_at`` for every recorded migration (empty when none)."""
    cursor = await connection.execute("SELECT to_regclass('adcp_schema_migrations') IS NOT NULL")
    row = await cursor.fetchone()
    if not row or not row[0]:
        return {}
    cursor = await connection.execute("SELECT version, applied_at FROM adcp_schema_migrations")
    return {str(version): applied_at for version, applied_at in await cursor.fetchall()}


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


async def migrate(pool: Any, *, dry_run: bool = False) -> list[Migration]:
    """Apply every pending migration in order; return what was (or would be) applied.

    ``pool`` is a psycopg ``AsyncConnectionPool`` or ``AsyncConnection`` whose
    ``search_path`` selects the schema. Each migration commits on its own, so a
    failure leaves the earlier ones applied and the failed one pending.
    """
    if hasattr(pool, "getconn"):  # a pool
        async with pool.connection() as connection:
            return await _migrate(connection, dry_run=dry_run)
    return await _migrate(pool, dry_run=dry_run)


async def _migrate(connection: Any, *, dry_run: bool) -> list[Migration]:
    from adcp_reporting_storage.migration import require_owned_schema_or_empty

    pending = await pending_migrations(connection)
    if dry_run or not pending:
        return pending
    applied: list[Migration] = []
    for migration in pending:
        async with connection.transaction():
            await connection.execute(_LOCK)
            await connection.execute(_TABLE)
            if migration.version in await applied_migrations(connection):
                continue  # another runner applied it while this one waited
            await require_owned_schema_or_empty(connection)
            for statement in migration.statements():
                await connection.execute(statement)
            await connection.execute(
                "INSERT INTO adcp_schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
        applied.append(migration)
    return applied


def describe(statuses: Sequence[MigrationStatus]) -> list[str]:
    """``--status`` lines: one per migration, flagging stopped-worker upgrades."""
    lines: list[str] = []
    for status in statuses:
        item = status.migration
        state = "pending" if status.pending else f"applied {status.applied_at:%Y-%m-%dT%H:%M:%SZ}"
        note = (
            "  [stopped-worker upgrade: stop every reporting worker before migrating]"
            if item.stopped_worker and status.pending
            else "  [stopped-worker upgrade]"
            if item.stopped_worker
            else ""
        )
        lines.append(f"{item.version} {item.name}: {state}{note}")
    return lines
