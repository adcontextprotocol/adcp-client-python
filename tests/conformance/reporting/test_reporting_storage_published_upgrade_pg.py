"""Ledgers created by the published adcp-reporting-storage wheels upgrade in place.

adcp 10.0.0b4 added ``offering_id`` to the ledger column lists; the only
published storage release, 0.2.0b0, did not create the column, and the pair
installed cleanly and failed every reporting turn (#1763). A seller's
production ledger created by 0.1.0b4 or 0.2.0b0 cannot be recreated, so these
tests seed ledgers with the *actual published wheels* (``adcp==9.3.1`` with
``adcp-reporting-storage==0.1.0b4``, ``adcp==10.0.0b2`` with ``0.2.0b0``),
with generations, obligations, revisions and rows, then upgrade them with this
checkout through both entry points and prove:

* no retained evidence row changes beyond the added ``offering_id = ''``;
* every record reads back and a restart rebinds the stored generations;
* the upgraded catalog is identical to a freshly migrated one;
* the applied schema versions are recorded (``create_schema()`` records the
  ledger's own ``0001``–``0003``; ``migrate`` the rest), re-running is a
  no-op, and concurrent boots serialize;
* ``adcp==10.0.0b4`` with *this* storage, the pair a seller upgrades to,
  upgrades a 0.2.0b0 ledger, where the published 0.2.0b0 fails.

Requires ``ADCP_PG_TEST_URL`` and PyPI; skips otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.ledger import ReportingConfigurationGenerationKey

from ._generation_support import isolated_reporting_pool

pytestmark = pytest.mark.skipif(
    not os.environ.get("ADCP_PG_TEST_URL"), reason="ADCP_PG_TEST_URL not set"
)

ROOT = Path(__file__).resolve().parents[3]
STORAGE_ROOT = ROOT / "packages/adcp-reporting-storage"
SEED = Path(__file__).with_name("_published_ledger_seed.py")

#: Published pairs that created ledgers in the field: the SDK 9 line and the
#: last SDK 10 beta before the contract changed.
PUBLISHED_PAIRS = {"0.1.0b4": "9.3.1", "0.2.0b0": "10.0.0b2"}
#: The first adcp whose column lists carry ``offering_id`` (storage contract 2).
FIRST_CONTRACT_2_ADCP = "10.0.0b4"
#: Tables the upgrade widens; every existing row gains ``offering_id = ''``.
WIDENED = {
    "reporting_configurations",
    "reporting_obligations",
    "reporting_consumer_statuses",
    "reporting_reconciliation_records",
    "reporting_materializer_discovery",
    "reporting_status_scope_checkpoints",
    "reporting_production_generations",
}
ACCOUNTS = ("account-redacted", "account-b")
SEEDED_KEYS = {
    ReportingConfigurationGenerationKey(account, "buyer-1", "gam-delivery", 1)
    for account in ACCOUNTS
}
RESTART_NOW = datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)

CATALOG = """
SELECT 'column ' || table_name || '.' || column_name || ' ' || data_type
       || ' null=' || is_nullable || ' default=' || coalesce(column_default, '')
       || ' collation=' || coalesce(collation_name, '')
FROM information_schema.columns WHERE table_schema = current_schema()
UNION ALL
SELECT 'constraint ' || conrelid::regclass || ' ' || conname || ' ' || pg_get_constraintdef(oid)
FROM pg_constraint WHERE connamespace = current_schema()::regnamespace
UNION ALL
SELECT 'index ' || tablename || ' ' || indexname || ' '
       || regexp_replace(indexdef, ' ON [^ ]+[.]', ' ON ')
FROM pg_indexes WHERE schemaname = current_schema()
UNION ALL
SELECT 'trigger ' || tgrelid::regclass || ' ' || tgname FROM pg_trigger
WHERE NOT tgisinternal
  AND tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace = current_schema()::regnamespace)
"""


# -- installed pairs -------------------------------------------------------


def _install(python: Path, *requirements: str) -> None:
    if shutil.which("uv"):
        command = ["uv", "pip", "install", "--quiet", "--python", str(python), *requirements]
    else:
        command = [str(python), "-m", "pip", "install", "--quiet", *requirements]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if completed.returncode != 0:
        pytest.skip(f"cannot install {' '.join(requirements)} (PyPI unreachable?)")


def _environment(root: Path, name: str, *requirements: str) -> Path:
    """A private interpreter with ``requirements`` installed from PyPI."""
    target = root / name
    if shutil.which("uv"):
        command = ["uv", "venv", "--quiet", "--python", sys.executable, str(target)]
    else:
        command = [sys.executable, "-m", "venv", str(target)]
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    python = target / "bin" / "python"
    _install(python, *requirements)
    return python


def _run(python: Path, settings: dict[str, Any], *, cwd: Path) -> dict[str, Any]:
    """Run the seed script under ``python`` outside the checkout, isolated from it."""
    script = cwd / "published_ledger_seed.py"
    shutil.copy2(SEED, script)
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    completed = subprocess.run(
        [str(python), "-I", str(script)],
        input=json.dumps(settings),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=environment,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("published-pairs")


@pytest.fixture(scope="module", params=sorted(PUBLISHED_PAIRS))
def published(request: pytest.FixtureRequest, workspace: Path) -> tuple[str, Path]:
    """``(storage version, python)`` for a published pair, as a seller installed it."""
    storage = request.param
    python = _environment(
        workspace,
        f"pair-{storage}",
        f"adcp=={PUBLISHED_PAIRS[storage]}",
        f"adcp-reporting-storage[pg]=={storage}",
    )
    return storage, python


@pytest.fixture(scope="module")
def b4_with_published_storage(workspace: Path) -> Path:
    """The failing pair of #1763: adcp 10.0.0b4 with the published 0.2.0b0."""
    return _environment(
        workspace,
        "b4-published",
        f"adcp=={FIRST_CONTRACT_2_ADCP}",
        "adcp-reporting-storage[pg]==0.2.0b0",
    )


@pytest.fixture(scope="module")
def b4_with_this_storage(workspace: Path) -> Path:
    """The pair a seller upgrades to: adcp 10.0.0b4 with this checkout's storage."""
    python = _environment(
        workspace,
        "b4-this",
        f"adcp=={FIRST_CONTRACT_2_ADCP}",
        "adcp-reporting-storage[pg]==0.2.0b0",
    )
    _install(python, "--no-deps", str(STORAGE_ROOT))
    return python


def _settings(pool: Any, action: str) -> dict[str, Any]:
    return {"conninfo": pool.conninfo, "kwargs": pool.kwargs, "action": action}


async def _seed(python: Path, pool: Any, cwd: Path) -> dict[str, Any]:
    result = await asyncio.to_thread(_run, python, _settings(pool, "seed"), cwd=cwd)
    assert "error" not in result, result["error"]
    assert sorted(result["readback"]["revisions"]) == sorted(
        f"rpr_{account}_{ordinal}" for account in ACCOUNTS for ordinal in range(2)
    )
    return result


# -- assertions ------------------------------------------------------------


def _normalized(image: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    return {
        table: sorted(json.dumps(row, sort_keys=True) for row in rows)
        for table, rows in image.items()
    }


def assert_evidence_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Every pre-upgrade row survives byte-for-byte, plus ``offering_id = ''`` where widened."""
    expected = {
        table: [{**row, "offering_id": ""} if table in WIDENED else row for row in rows]
        for table, rows in before.items()
    }
    for table, rows in _normalized(expected).items():
        assert _normalized({table: after[table]})[table] == rows, table
    for table in set(after) - set(before):
        assert after[table] == [], f"the upgrade wrote rows into new table {table}"


async def _versions(pool: Any) -> list[str]:
    from adcp_reporting_storage.migrations import migration_status, migrations

    statuses = await migration_status(pool)
    assert [item.migration.version for item in statuses] == [m.version for m in migrations()]
    return [item.migration.version for item in statuses if not item.pending]


async def _readback_here(pool: Any) -> dict[str, Any]:
    """Read the ledger with this checkout, through the same calls the seed used."""
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    from tests.conformance.reporting import _published_ledger_seed as seed

    return await seed.readback(PgReportingLedgerStore(pool=pool))


async def _image_here(pool: Any) -> dict[str, Any]:
    from tests.conformance.reporting import _published_ledger_seed as seed

    async with pool.connection() as connection:
        return await seed.image(connection)


async def _catalog(pool: Any) -> list[str]:
    async with pool.connection() as connection:
        rows = await (await connection.execute(CATALOG)).fetchall()
    return sorted(row[0] for row in rows)


# -- tests -----------------------------------------------------------------


async def test_create_schema_upgrades_a_published_ledger_in_place(
    published: tuple[str, Path], workspace: Path
) -> None:
    """The ``store=`` startup path (``ReliableReportingService`` calls ``create_schema``)."""
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import migrate

    from adcp.storage.postgres import postgres_storage

    storage, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace)
        assert await _versions(pool) == [], f"{storage} recorded no schema version"
        assert "offering_id" not in seeded["image"]["reporting_obligations"][0]

        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()

        # The ledger's own migrations are applied and recorded; the ones that
        # only postgres_storage needs stay pending for python -m adcp migrate.
        assert await _versions(pool) == ["0001", "0002", "0003"]
        after = await _image_here(pool)
        assert_evidence_unchanged(seeded["image"], after)
        assert await _readback_here(pool) == seeded["readback"]
        upgraded = await _catalog(pool)

        await store.create_schema()  # re-runnable: nothing changes
        assert await _image_here(pool) == after
        assert await _versions(pool) == ["0001", "0002", "0003"]

        applied = await migrate(pool)
        assert [item.version for item in applied] == ["0004", "0005"]
        await postgres_storage(pool=pool).check_schema()  # the migrations-managed path agrees
        assert_evidence_unchanged(seeded["image"], await _image_here(pool))
    async with isolated_reporting_pool(autocommit=True) as fresh:
        await PgReportingLedgerStore(pool=fresh).create_schema()
        assert upgraded == await _catalog(fresh)


async def test_migrate_upgrades_a_published_ledger_to_the_fresh_catalog(
    published: tuple[str, Path], workspace: Path
) -> None:
    """``python -m adcp migrate`` on the seller's ledger yields a freshly migrated catalog."""
    from adcp_reporting_storage.migrations import migrate

    _, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace)
        applied = await migrate(pool)
        assert [item.version for item in applied] == ["0001", "0002", "0003", "0004", "0005"]
        assert await migrate(pool) == []
        assert_evidence_unchanged(seeded["image"], await _image_here(pool))
        assert await _readback_here(pool) == seeded["readback"]
        upgraded = await _catalog(pool)
    async with isolated_reporting_pool(autocommit=True) as fresh:
        await migrate(fresh)
        assert upgraded == await _catalog(fresh)


async def test_restart_rebinds_stored_generations_and_closes_the_next_period(
    published: tuple[str, Path], workspace: Path
) -> None:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    from adcp.reporting.fixtures import redacted_capabilities
    from adcp.reporting.service import ReliableReportingService
    from adcp.reporting.testing import ScriptedReportingAdapter
    from tests.conformance.reporting import _published_ledger_seed as seed
    from tests.test_reliable_reporting_service import _account_context, _rows

    _, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace)
        service = ReliableReportingService(
            store=PgReportingLedgerStore(pool=pool, clock=lambda: RESTART_NOW),
            clock=lambda: RESTART_NOW,
            account_context=_account_context,
        )
        service.sources.register(
            "gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(10)] * 8)
        )
        try:
            await service.start()  # create_schema(): the in-place upgrade
            assert service.initialization_errors == {}
            turn = await service.run_worker(now=RESTART_NOW)
        finally:
            await service.close()
        assert turn.configuration_errors == {}
        assert set(turn.configurations) == SEEDED_KEYS
        store = PgReportingLedgerStore(pool=pool)
        for account in ACCOUNTS:
            configuration = seed._configuration(account)
            # Periods 01:00 and 02:00 were seeded; the restart closes 03:00-04:00.
            next_period = seed._obligation(configuration, 2).period
            closed = await store.find_obligation(
                account_id=account,
                consumer_id="buyer-1",
                delivery_config_id="gam-delivery",
                delivery_config_version=1,
                period_start=next_period.start,
                period_end=next_period.end,
            )
            assert closed is not None, f"{account}: no obligation for the next period"
            revisions = await store.list_revisions(
                account_id=account, reporting_obligation_id=closed.reporting_obligation_id
            )
            assert [item.finality for item in revisions] == ["snapshot"]
        # The seeded evidence is untouched by the turn.
        after = await _image_here(pool)
        for table in ("reporting_revisions", "reporting_revision_rows"):
            seeded_rows = _normalized(seeded["image"])[table]
            assert set(seeded_rows) <= set(_normalized(after)[table]), table
        # The turn may re-observe the seeded snapshot periods (new revisions)
        # but every seeded revision still reads.
        assert set(seeded["readback"]["revisions"]) <= set(
            (await _readback_here(pool))["revisions"]
        )


async def test_concurrent_boots_and_migrate_serialize_on_a_published_ledger(
    published: tuple[str, Path], workspace: Path
) -> None:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import migrate
    from psycopg_pool import AsyncConnectionPool

    _, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace)
        pools = [
            AsyncConnectionPool(
                pool.conninfo, kwargs=pool.kwargs, min_size=1, max_size=2, open=False
            )
            for _ in range(4)
        ]
        for item in pools:
            await item.open(wait=True)
        try:
            await asyncio.gather(
                PgReportingLedgerStore(pool=pools[0]).create_schema(),
                migrate(pools[1]),
                PgReportingLedgerStore(pool=pools[2]).create_schema(),
                migrate(pools[3]),
            )
        finally:
            for item in pools:
                await item.close()
        assert await _versions(pool) == ["0001", "0002", "0003", "0004", "0005"]
        async with pool.connection() as connection:
            row = await (
                await connection.execute("SELECT count(*) FROM adcp_schema_migrations")
            ).fetchone()
        assert row[0] == 5  # each version recorded once across the four runners
        assert_evidence_unchanged(seeded["image"], await _image_here(pool))


async def test_adcp_b4_fails_on_the_published_storage_and_upgrades_with_this_one(
    published: tuple[str, Path],
    b4_with_published_storage: Path,
    b4_with_this_storage: Path,
    workspace: Path,
) -> None:
    """The reported failure, then the pair the fix ships: adcp 10.0.0b4 + this storage."""
    storage, python = published
    if storage != "0.2.0b0":
        pytest.skip("the 10.0.0b4 pairing concerns the 0.2.0b0 ledger")
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace)
        failed = await asyncio.to_thread(
            _run, b4_with_published_storage, _settings(pool, "read"), cwd=workspace
        )
        assert "offering_id" in failed.get("error", ""), failed  # #1763, reproduced
        upgraded = await asyncio.to_thread(
            _run, b4_with_this_storage, _settings(pool, "upgrade"), cwd=workspace
        )
        assert "error" not in upgraded, upgraded["error"]
        assert upgraded["readback"] == seeded["readback"]
        assert_evidence_unchanged(seeded["image"], upgraded["image"])
        assert await _versions(pool) == ["0001", "0002", "0003"]
