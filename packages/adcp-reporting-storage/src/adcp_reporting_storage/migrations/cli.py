"""``python -m adcp migrate``: apply the reporting storage migrations.

Registered as the ``migrate`` entry point of the ``adcp.commands`` group, which
``python -m adcp`` dispatches to, so ``adcp`` never imports this distribution.
Also runnable as ``python -m adcp_reporting_storage.migrations.cli``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

USAGE = "python -m adcp migrate [--dsn URL] [--status | --dry-run] [--contract]"


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adcp migrate",
        usage=USAGE,
        description=(
            "Apply the reporting storage migrations (adcp-reporting-storage) to a PostgreSQL "
            "database, in order. Each migration commits on its own under the reporting schema "
            "lock. A ledger created by adcp-reporting-storage 0.1.x/0.2.0b0 is expanded, so "
            "processes of that release keep running through a rolling deploy; the contract "
            "step that completes the upgrade runs only with --contract."
        ),
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("ADCP_DATABASE_URL") or os.environ.get("DATABASE_URL"),
        help="PostgreSQL URL; defaults to $ADCP_DATABASE_URL, then $DATABASE_URL. Select the "
        "schema with options=-csearch_path=<schema>.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="List applied and pending migrations")
    mode.add_argument("--dry-run", action="store_true", help="Print what would be applied")
    parser.add_argument(
        "--contract",
        action="store_true",
        help="Also apply the contract step of an expanded ledger. Run it only once every "
        "earlier-release reporting process has stopped: they cannot write a contracted ledger.",
    )
    args = parser.parse_args(list(argv))
    if not args.dsn:
        parser.error("pass --dsn or set DATABASE_URL")
    if args.status and args.contract:
        parser.error("--contract applies migrations; it does not combine with --status")
    try:
        return asyncio.run(
            _run(args.dsn, status=args.status, dry_run=args.dry_run, contract=args.contract)
        )
    except ImportError as error:
        print(
            f"{error}. Install the PostgreSQL storage: pip install 'adcp-reporting-storage[pg]'",
            file=sys.stderr,
        )
        return 2


async def _run(dsn: str, *, status: bool, dry_run: bool, contract: bool = False) -> int:
    from psycopg import AsyncConnection

    from adcp_reporting_storage.migrations import (
        CONTRACT_COMMAND,
        CONTRACT_WHEN,
        describe,
        migrate,
        migration_status,
        needs_contract,
    )

    async with await AsyncConnection.connect(dsn, autocommit=True) as connection:
        if status:
            statuses = await migration_status(connection)
            for line in describe(statuses):
                print(line)
            pending = [item for item in statuses if item.pending]
            if not pending:
                print("up to date")
            elif all(item.migration.contract for item in pending):
                if await needs_contract(connection):
                    print(
                        "up to date except the contract step: run "
                        f'"{CONTRACT_COMMAND}" {CONTRACT_WHEN}'
                    )
                else:
                    print(
                        "up to date except recording the contract step, which changes nothing "
                        'on this ledger: run "python -m adcp migrate"'
                    )
            else:
                print(f"{len(pending)} pending")
            return 0
        planned = await migrate(connection, dry_run=dry_run, contract=contract)
        verb = "would apply" if dry_run else "applied"
        for item in planned:
            note = (
                " (contract)"
                if item.contract
                else " (stopped-worker upgrade)" if item.stopped_worker else ""
            )
            print(f"{verb} {item.version} {item.name}{note}")
        held = [
            item
            for item in (await migration_status(connection))
            if item.pending and item.migration not in planned
        ]
        if held and held[0].migration.contract:
            item = held[0].migration
            print(
                f"not applied {item.version} {item.name} (contract): run "
                f'"{CONTRACT_COMMAND}" {CONTRACT_WHEN}'
            )
        elif not planned:
            print("up to date")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
