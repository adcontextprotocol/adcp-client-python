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
* the upgrade is rolling-deploy safe (#1856): this checkout EXPANDS the
  ledger, so processes of the published pair keep restarting, reading,
  writing and running worker turns beside it, a rollback keeps working, and a
  subscription generation is refused with a typed error until the contract
  step, which leaves a catalog identical to a freshly migrated one;
* the applied schema versions are recorded (``create_schema()`` records the
  ledger's own ``0001``–``0003``; ``migrate`` the rest, except the contract
  step, which needs ``contract=True``), re-running is a no-op, and concurrent
  boots serialize, old and new alike;
* a ledger the published 0.2.0b1 already widened in place counts as contracted;
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
    # Migration 0006 (status checkpoint offerings) re-keys these the same way.
    "reporting_status_checkpoints",
    "reporting_status_events",
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


def _settings(pool: Any, action: str, *, notifications: bool = False) -> dict[str, Any]:
    return {
        "conninfo": pool.conninfo,
        "kwargs": pool.kwargs,
        "action": action,
        "notifications": notifications,
    }


async def _seed(
    python: Path, pool: Any, cwd: Path, *, notifications: bool = False
) -> dict[str, Any]:
    result = await asyncio.to_thread(
        _run, python, _settings(pool, "seed", notifications=notifications), cwd=cwd
    )
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


#: The migrations create_schema() owns and records; the rest are migrate's.
LEDGER_VERSIONS = ["0001", "0002", "0003"]


def _shipped(*, contract: bool = True) -> list[str]:
    """Every shipped version; ``contract=False`` leaves out the contract steps."""
    from adcp_reporting_storage.migrations import migrations

    return [item.version for item in migrations() if contract or not item.contract]


def _rest(*, contract: bool = True) -> list[str]:
    return [version for version in _shipped(contract=contract) if version not in LEDGER_VERSIONS]


#: The contract steps: applied to an expanded ledger only with ``contract=True``.
CONTRACT_VERSIONS = ["0007"]
#: The objects that exist only while a ledger is expanded (catalog lines).
EXPANDED_LEDGER = {
    "constraint reporting_configurations reporting_configurations_generation_key",
    "constraint reporting_configurations reporting_configurations_uncontracted",
    "constraint reporting_consumer_statuses reporting_consumer_statuses_uncontracted",
    "constraint reporting_obligations reporting_obligations_uncontracted",
    "index reporting_configurations reporting_configurations_generation_key",
    "index reporting_obligations reporting_obligations_offering_period_key",
}
EXPANDED_STATUS = {
    "constraint reporting_status_scope_checkpoints reporting_status_scope_checkpoints_offering_key",
    "index reporting_status_scope_checkpoints reporting_status_scope_checkpoints_offering_key",
    "constraint reporting_status_notification_events"
    " reporting_status_notification_events_offering_fkey",
    "constraint reporting_status_notification_events"
    " reporting_status_notification_events_offering_key",
    "index reporting_status_notification_events reporting_status_notification_events_offering_key",
}


def _expand_only(catalog: list[str]) -> set[str]:
    """The expand-only objects in a catalog, as ``kind table name``."""
    from tests.conformance.reporting._expand_manifest import EXPAND_SUFFIXES

    found = set()
    for line in catalog:
        kind, table, name, *_ = line.split(" ")
        if kind in ("constraint", "index") and name.endswith(EXPAND_SUFFIXES):
            found.add(f"{kind} {table} {name}")
    return found


async def _versions(pool: Any) -> list[str]:
    from adcp_reporting_storage.migrations import migration_status

    statuses = await migration_status(pool)
    assert [item.migration.version for item in statuses] == _shipped()
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


async def _full_catalog(pool: Any) -> list[str]:
    """``_catalog`` plus every function's full definition and trigger definitions."""
    async with pool.connection() as connection:
        functions = await (
            await connection.execute(
                "SELECT 'function ' || replace(pg_get_functiondef(p.oid),"
                " quote_ident(current_schema()) || '.', '') FROM pg_proc p"
                " WHERE p.pronamespace = current_schema()::regnamespace"
            )
        ).fetchall()
        triggers = await (
            await connection.execute(
                "SELECT 'triggerdef ' || replace(pg_get_triggerdef(t.oid),"
                " quote_ident(current_schema()) || '.', '') FROM pg_trigger t"
                " JOIN pg_class c ON c.oid = t.tgrelid"
                " WHERE NOT t.tgisinternal AND c.relnamespace = current_schema()::regnamespace"
            )
        ).fetchall()
    return sorted([*await _catalog(pool), *(row[0] for row in [*functions, *triggers])])


# -- tests -----------------------------------------------------------------


async def test_create_schema_upgrades_a_published_ledger_in_place(
    published: tuple[str, Path], workspace: Path
) -> None:
    """The ``store=`` startup path (``ReliableReportingService`` calls ``create_schema``)."""
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import migrate, needs_contract

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
        assert await _versions(pool) == LEDGER_VERSIONS
        after = await _image_here(pool)
        assert_evidence_unchanged(seeded["image"], after)
        assert await _readback_here(pool) == seeded["readback"]
        upgraded = await _catalog(pool)

        await store.create_schema()  # re-runnable: nothing changes
        assert await _image_here(pool) == after
        assert await _versions(pool) == LEDGER_VERSIONS

        # The boot expanded the ledger: what the published release needs keeps
        # its name beside the widened keys, until the contract step.
        assert await needs_contract(pool)
        assert _expand_only(upgraded) == EXPANDED_LEDGER

        applied = await migrate(pool)  # a pre-deploy job: never contracts
        assert [item.version for item in applied] == _rest(contract=False)
        await postgres_storage(pool=pool).check_schema()  # the migrations-managed path agrees
        assert [item.version for item in await migrate(pool, contract=True)] == CONTRACT_VERSIONS
        assert_evidence_unchanged(seeded["image"], await _image_here(pool))
        contracted = await _full_catalog(pool)
    async with isolated_reporting_pool(autocommit=True) as fresh:
        await PgReportingLedgerStore(pool=fresh).create_schema()
        await migrate(fresh)
        assert contracted == await _full_catalog(fresh)


async def test_migrate_upgrades_a_published_ledger_to_the_fresh_catalog(
    published: tuple[str, Path], workspace: Path
) -> None:
    """``python -m adcp migrate`` on the seller's ledger yields a freshly migrated catalog."""
    from adcp_reporting_storage.migrations import migrate

    _, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace)
        applied = await migrate(pool)
        assert [item.version for item in applied] == _shipped(contract=False)
        assert await migrate(pool) == []  # the contract step waits for --contract
        assert [item.version for item in await migrate(pool, contract=True)] == CONTRACT_VERSIONS
        assert await migrate(pool, contract=True) == []
        assert_evidence_unchanged(seeded["image"], await _image_here(pool))
        assert await _readback_here(pool) == seeded["readback"]
        upgraded = await _full_catalog(pool)
    async with isolated_reporting_pool(autocommit=True) as fresh:
        await migrate(fresh)
        assert upgraded == await _full_catalog(fresh)


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
        assert await _versions(pool) == _shipped(contract=False)
        async with pool.connection() as connection:
            row = await (
                await connection.execute("SELECT count(*) FROM adcp_schema_migrations")
            ).fetchone()
        # Each version recorded once across the four runners; none contracted.
        assert row[0] == len(_shipped(contract=False))
        assert_evidence_unchanged(seeded["image"], await _image_here(pool))


async def test_a_notifications_enabled_published_ledger_upgrades_through_every_migration(
    published: tuple[str, Path], workspace: Path
) -> None:
    """Status notifications add the checkpoint tables that migration 0006 re-keys."""
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import migrate

    from tests.conformance.reporting import _published_ledger_seed as seed

    _, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace, notifications=True)
        assert "reporting_status_checkpoints" in seeded["image"]
        store = PgReportingLedgerStore(pool=pool, notifications=True)
        await seed.create_schema(pool, store, notifications=True)  # a seller's boot
        assert await _versions(pool) == LEDGER_VERSIONS
        applied = await migrate(pool)
        assert [item.version for item in applied] == _rest(contract=False)
        assert await _versions(pool) == _shipped(contract=False)
        assert await migrate(pool) == []
        assert _expand_only(await _catalog(pool)) == EXPANDED_LEDGER | EXPANDED_STATUS
        assert [item.version for item in await migrate(pool, contract=True)] == CONTRACT_VERSIONS
        assert _expand_only(await _catalog(pool)) == set()
        assert_evidence_unchanged(seeded["image"], await _image_here(pool))
        assert await _readback_here(pool) == seeded["readback"]


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
        assert await _versions(pool) == LEDGER_VERSIONS


# -- rolling deploy: expand, old and new side by side, contract (#1856) ------

#: Worker-turn instants, one per pod round, so each round closes a new period.
TURNS = [datetime(2026, 11, 1, hour, 30, tzinfo=timezone.utc) for hour in range(4, 12)]
#: The published 0.2.0b1 pair (#1831), which widened ledgers in place.
WIDENED_IN_PLACE_PAIR = ("0.2.0b1", "10.0.0b8")


async def _old_pod(
    python: Path,
    pool: Any,
    cwd: Path,
    *,
    notifications: bool,
    ordinal: int,
    now: datetime | None = None,
    account: str = "account-old-pod",
) -> dict[str, Any]:
    """A pod of the published pair restarts and works (``old_pod`` in the seed script)."""
    settings = {
        **_settings(pool, "old_pod", notifications=notifications),
        "ordinal": ordinal,
        "account": account,
        **({"now": now.isoformat()} if now is not None else {}),
    }
    result = await asyncio.to_thread(_run, python, settings, cwd=cwd)
    assert "error" not in result, result["error"]
    return result


def _all_ok(result: dict[str, Any]) -> None:
    failed = {step: outcome for step, outcome in result["steps"].items() if outcome != "ok"}
    assert result["steps"] and not failed, failed


async def _new_pod(
    pool: Any,
    *,
    notifications: bool,
    ordinal: int,
    now: datetime,
    account: str,
    reconciliation_account: str,
) -> None:
    """A pod of this checkout boots and does the same work, plus reconciliation writes."""
    from adcp_reporting_storage import PgReportingReconciliationStore
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    from tests.conformance.reporting import _published_ledger_seed as seed
    from tests.conformance.reporting._reconciliation_support import scenario

    store = PgReportingLedgerStore(pool=pool, notifications=notifications)
    await seed.create_schema(pool, store, notifications=notifications)
    configuration = seed._configuration(ACCOUNTS[0])
    obligation = await store.commit_obligation(seed._obligation(configuration, ordinal))
    await store.commit_revision(*seed._revision(obligation, f"rpr_{ACCOUNTS[0]}_{ordinal}"))
    await store.put_configuration(seed._configuration(account))
    turned = await seed.worker_turn(store, now)
    assert [ACCOUNTS[0], "buyer-1", "gam-delivery", 1] in turned
    # Destination binding, delivery, attempt, materialization and receipt: every
    # reconciliation function an earlier release's boot re-creates.
    recorder = PgReportingReconciliationStore(pool=pool, notifications=notifications)
    await scenario(recorder, account_id=reconciliation_account)
    if notifications:
        from adcp_reporting_storage.outbox.status_pg import PgStatusNotificationStore

        status = PgStatusNotificationStore(store)
        for account_id in (ACCOUNTS[0], reconciliation_account):
            await status.baseline(account_id=account_id)
            await status.project_one(account_id=account_id)
            assert await status.checkpoints(account_id=account_id)


async def _read_here(pool: Any, *accounts: str) -> dict[str, Any]:
    """This checkout reads the seeded records, and every account's generations."""
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    from adcp.reporting.ledger import ReportingStatusCaller

    store = PgReportingLedgerStore(pool=pool)
    configurations = []
    for account in accounts:
        caller = ReportingStatusCaller(account_id=account, consumer_id="buyer-1")
        configurations += [
            item.account_id for item in await store.list_configurations(caller=caller)
        ]
    return {"configurations": configurations, "readback": await _readback_here(pool)}


def _subscription_configuration(account: str) -> Any:
    from dataclasses import replace

    from adcp.reporting.ledger.models import ReportingSubscriptionGeneration
    from tests.conformance.reporting import _published_ledger_seed as seed

    return replace(
        seed._configuration(account),
        delivery_config_id="analytics",
        subscription=ReportingSubscriptionGeneration("analytics", "PAID_MEDIA_SNAPSHOT"),
    )


async def _fresh_catalog(*, notifications: bool) -> list[str]:
    """A new ledger created by this checkout the way the rolled-out sellers' was."""
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import migrate

    from tests.conformance.reporting import _published_ledger_seed as seed

    async with isolated_reporting_pool(autocommit=True) as fresh:
        store = PgReportingLedgerStore(pool=fresh, notifications=notifications)
        await seed.create_schema(fresh, store, notifications=notifications)
        await migrate(fresh)
        return await _full_catalog(fresh)


@pytest.mark.parametrize("notifications", [False, True], ids=["ledger", "notifications"])
async def test_a_rolling_deploy_runs_published_and_new_pods_side_by_side_until_contract(
    published: tuple[str, Path], workspace: Path, notifications: bool
) -> None:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import (
        ReportingUpgradeNotContractedError,
        migrate,
        needs_contract,
    )

    from adcp.storage.postgres import postgres_storage
    from tests.conformance.reporting import _published_ledger_seed as seed

    _, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        # 1. The published pair's ledger.
        seeded = await _seed(python, pool, workspace, notifications=notifications)
        # 2. The first new pod boots (and the pre-deploy job migrates): expanded.
        await seed.create_schema(
            pool,
            PgReportingLedgerStore(pool=pool, notifications=notifications),
            notifications=notifications,
        )
        assert [item.version for item in await migrate(pool)] == _rest(contract=False)
        await postgres_storage(pool=pool).check_schema()  # only the contract step pends
        assert await needs_contract(pool)
        expanded = EXPANDED_LEDGER | (EXPANDED_STATUS if notifications else set())
        assert _expand_only(await _catalog(pool)) == expanded
        assert_evidence_unchanged(seeded["image"], await _image_here(pool))
        # 3. A published pod restarts, reads, records a generation, closes a
        #    period and runs a full worker turn.
        old = await _old_pod(
            python, pool, workspace, notifications=notifications, ordinal=2, now=TURNS[0]
        )
        _all_ok(old)
        assert old["readback"] == seeded["readback"]
        assert [ACCOUNTS[0], "buyer-1", "gam-delivery", 1] in old["turn"]
        # 4. A new pod reads everything, the published pod's rows too, and writes.
        read = await _read_here(pool, *ACCOUNTS, "account-old-pod")
        assert "account-old-pod" in read["configurations"]
        assert set(seeded["readback"]["revisions"]) <= set(read["readback"]["revisions"])
        await _new_pod(
            pool,
            notifications=notifications,
            ordinal=3,
            now=TURNS[1],
            account="account-new-pod",
            reconciliation_account="acct_new_pod",
        )
        # 5. A published pod restarts again (its boot re-creates its function
        #    bodies), then new pods still write reconciliation and status records.
        again = await _old_pod(
            python,
            pool,
            workspace,
            notifications=notifications,
            ordinal=4,
            now=TURNS[2],
            account="account-old-pod-2",
        )
        _all_ok(again)
        assert "account-new-pod" in [row[0] for row in again["turn"]]  # the new pod's generation
        await _new_pod(
            pool,
            notifications=notifications,
            ordinal=5,
            now=TURNS[3],
            account="account-new-pod-2",
            reconciliation_account="acct_after_old_restart",
        )
        assert _expand_only(await _catalog(pool)) == expanded  # no boot moved the ledger
        async with pool.connection() as connection:
            recorded = await (
                await connection.execute(
                    "SELECT count(DISTINCT account_id) FROM reporting_reconciliation_records"
                    " WHERE account_id IN ('acct_new_pod', 'acct_after_old_restart')"
                )
            ).fetchone()
            assert recorded[0] == 2
            if notifications:
                events = await (
                    await connection.execute(
                        "SELECT count(*) FROM reporting_status_notification_events"
                    )
                ).fetchone()
                assert events[0] > 0
        # 6. A subscription generation before contract: a typed error naming the step.
        store = PgReportingLedgerStore(pool=pool, notifications=notifications)
        with pytest.raises(ReportingUpgradeNotContractedError) as refused:
            await store.put_configuration(_subscription_configuration(ACCOUNTS[0]))
        assert refused.value.code == "REPORTING_UPGRADE_NOT_CONTRACTED"
        assert "python -m adcp migrate --contract" in str(refused.value)
        # 7. Every published process has stopped: contract.
        assert [item.version for item in await migrate(pool, contract=True)] == CONTRACT_VERSIONS
        assert not await needs_contract(pool)
        # 8. Exactly a new ledger's catalog; subscriptions now record.
        assert await _full_catalog(pool) == await _fresh_catalog(notifications=notifications)
        await seed.create_schema(pool, store, notifications=notifications)  # a new pod boots
        await store.put_configuration(_subscription_configuration(ACCOUNTS[0]))
        await _new_pod(
            pool,
            notifications=notifications,
            ordinal=6,
            now=TURNS[4],
            account="account-after-contract",
            reconciliation_account="acct_after_contract",
        )
        # 9. The published pair cannot run on the contracted ledger: its boot
        #    refuses (with the caller-ownership message of #1831) and its writes fail.
        late = await _old_pod(python, pool, workspace, notifications=notifications, ordinal=7)
        assert "caller ownership" in late["steps"]["create_schema"], late["steps"]
        assert "no unique or exclusion constraint" in late["steps"]["put_configuration"]
        assert late["steps"]["readback"] == "ok"  # reads still work


async def test_a_rollback_before_contract_keeps_the_published_pair_working(
    published: tuple[str, Path], workspace: Path
) -> None:
    """Old pods alone, after the new release expanded the ledger and was rolled back."""
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import migrate

    from tests.conformance.reporting import _published_ledger_seed as seed

    _, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace, notifications=True)
        store = PgReportingLedgerStore(pool=pool, notifications=True)
        await seed.create_schema(pool, store, notifications=True)
        await migrate(pool)
        expanded = await _full_catalog(pool)
        for round_, now in enumerate(TURNS[:2]):
            old = await _old_pod(
                python,
                pool,
                workspace,
                notifications=True,
                ordinal=2 + round_,
                now=now,
                account=f"account-rollback-{round_}",
            )
            _all_ok(old)
            # Every seeded record reads back; a worker turn may add revisions.
            assert old["readback"]["obligations"] == seeded["readback"]["obligations"]
            assert set(seeded["readback"]["revisions"]) <= set(old["readback"]["revisions"])
            assert old["checkpoints"] > 0
        assert await _full_catalog(pool) == expanded  # the published boots changed nothing
        # Rolling forward again later still works.
        await _new_pod(
            pool,
            notifications=True,
            ordinal=4,
            now=TURNS[2],
            account="account-forward",
            reconciliation_account="acct_forward",
        )


async def test_concurrent_new_and_published_boots_serialize_on_a_published_ledger(
    published: tuple[str, Path], workspace: Path
) -> None:
    """Two new boots and a published boot at once: no deadlock, each step applied once."""
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from psycopg_pool import AsyncConnectionPool

    from tests.conformance.reporting import _published_ledger_seed as seed

    _, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace, notifications=True)
        pools = [
            AsyncConnectionPool(
                pool.conninfo, kwargs=pool.kwargs, min_size=1, max_size=2, open=False
            )
            for _ in range(2)
        ]
        for item in pools:
            await item.open(wait=True)
        settings = _settings(pool, "upgrade", notifications=True)
        try:
            old, *_ = await asyncio.gather(
                asyncio.to_thread(_run, python, settings, cwd=workspace),
                *(
                    seed.create_schema(
                        item,
                        PgReportingLedgerStore(pool=item, notifications=True),
                        notifications=True,
                    )
                    for item in pools
                ),
            )
        finally:
            for item in pools:
                await item.close()
        assert "error" not in old, old["error"]  # the published boot, before or after
        assert await _versions(pool) == LEDGER_VERSIONS
        async with pool.connection() as connection:
            row = await (
                await connection.execute("SELECT count(*) FROM adcp_schema_migrations")
            ).fetchone()
        assert row[0] == len(LEDGER_VERSIONS)
        assert _expand_only(await _catalog(pool)) == EXPANDED_LEDGER | EXPANDED_STATUS
        assert_evidence_unchanged(seeded["image"], await _image_here(pool))
        _all_ok(await _old_pod(python, pool, workspace, notifications=True, ordinal=2))


async def test_the_expand_manifest_is_the_expanded_published_catalog(
    published: tuple[str, Path], workspace: Path
) -> None:
    """``outbox/required_expand_schema.json`` is derived from a real expanded ledger.

    Both releases' validators accept the expanded catalog: the published
    pair's by object name (it boots, above), this checkout's through the
    expand alternates.
    """
    from importlib.resources import files

    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.outbox import _schema, status_schema

    from tests.conformance.reporting import _published_ledger_seed as seed
    from tests.conformance.reporting._expand_manifest import derive, render

    _, python = published
    async with isolated_reporting_pool(autocommit=True) as pool:
        await _seed(python, pool, workspace, notifications=True)
        # Expand without validating, as the published boot leaves it.
        _all_ok(await _old_pod(python, pool, workspace, notifications=True, ordinal=2))
        store = PgReportingLedgerStore(pool=pool, notifications=True)
        await seed.create_schema(pool, store, notifications=True)
        _all_ok(await _old_pod(python, pool, workspace, notifications=True, ordinal=3))
        async with pool.connection() as connection:
            installed = await _schema.schema_objects(connection)
            await status_schema.validate_status_schema(connection)  # this checkout accepts it
    manifests = {**_schema.REQUIRED_OBJECTS, **status_schema.REQUIRED_STATUS_OBJECTS}
    derived = render(derive(installed, manifests))
    packaged = files("adcp_reporting_storage.outbox").joinpath("required_expand_schema.json")
    if os.environ.get("ADCP_WRITE_EXPAND_MANIFEST"):
        Path(str(packaged)).write_text(derived)
    assert packaged.read_text() == derived


@pytest.fixture(scope="module")
def widened_in_place(workspace: Path) -> Path:
    """The published 0.2.0b1 pair, which widened a 0.1.x ledger in place (#1831)."""
    storage, adcp = WIDENED_IN_PLACE_PAIR
    return _environment(
        workspace, f"pair-{storage}", f"adcp=={adcp}", f"adcp-reporting-storage[pg]=={storage}"
    )


async def test_a_ledger_the_published_0_2_0b1_widened_counts_as_contracted(
    published: tuple[str, Path], widened_in_place: Path, workspace: Path
) -> None:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import migrate, needs_contract

    from adcp.storage.postgres import postgres_storage
    from tests.conformance.reporting import _published_ledger_seed as seed

    storage, python = published
    if storage != "0.1.0b4":
        pytest.skip("one seed is enough: 0.2.0b1 widens 0.1.0b4 and 0.2.0b0 ledgers alike")
    async with isolated_reporting_pool(autocommit=True) as pool:
        seeded = await _seed(python, pool, workspace, notifications=True)
        upgraded = await asyncio.to_thread(
            _run, widened_in_place, _settings(pool, "upgrade", notifications=True), cwd=workspace
        )
        assert "error" not in upgraded, upgraded["error"]
        assert not await needs_contract(pool)
        before = await _catalog(pool)
        assert _expand_only(before) == set()
        store = PgReportingLedgerStore(pool=pool, notifications=True)
        await seed.create_schema(pool, store, notifications=True)  # boots, does not re-expand
        assert await _catalog(pool) == before
        # The contract step is recorded without --contract: it changes nothing here.
        applied = [item.version for item in await migrate(pool)]
        assert applied[-1:] == CONTRACT_VERSIONS
        await postgres_storage(pool=pool).check_schema()
        after = await _catalog(pool)  # 0004 and 0005 add their own tables
        assert set(before) <= set(after) and _expand_only(after) == set()
        await store.put_configuration(_subscription_configuration(ACCOUNTS[0]))
        assert_evidence_unchanged(seeded["image"], upgraded["image"])


async def test_migrate_holds_the_contract_step_and_status_says_when_to_run_it(
    published: tuple[str, Path], workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m adcp migrate`` as a pre-deploy job, beside running published pods."""
    from adcp_reporting_storage.migrations import describe, migration_status
    from adcp_reporting_storage.migrations.cli import _run

    from adcp.storage.postgres import postgres_storage

    storage, python = published
    if storage != "0.1.0b4":
        pytest.skip("the CLI's output does not depend on which published pair seeded")
    async with isolated_reporting_pool(autocommit=True) as pool:
        await _seed(python, pool, workspace)
        async with pool.connection() as connection:
            schema = (await (await connection.execute("SELECT current_schema()")).fetchone())[0]
        base = os.environ["ADCP_PG_TEST_URL"]
        dsn = f"{base}{'&' if '?' in base else '?'}options=-csearch_path%3D{schema}"
        when = "once every earlier-release reporting process has stopped"

        assert await _run(dsn, status=False, dry_run=True) == 0
        planned = capsys.readouterr().out
        assert "would apply 0006 reporting_status_checkpoint_offerings\n" in planned
        assert "would apply 0007" not in planned  # held
        assert (
            "not applied 0007 reporting_contract_generation_keys (contract): run"
            f' "python -m adcp migrate --contract" {when}'
        ) in planned

        assert await _run(dsn, status=False, dry_run=False) == 0
        applied = capsys.readouterr().out
        assert "applied 0002 reporting_subscription_generations\n" in applied
        assert "\napplied 0007" not in applied
        assert "not applied 0007 reporting_contract_generation_keys (contract)" in applied
        await postgres_storage(pool=pool).check_schema()  # starts: only the contract pends

        assert await _run(dsn, status=True, dry_run=False) == 0
        status = capsys.readouterr().out.strip().splitlines()
        assert status[-2] == (
            "0007 reporting_contract_generation_keys: pending  [contract: run"
            f" {when}: python -m adcp migrate --contract]"
        )
        assert status[-1] == (
            f'up to date except the contract step: run "python -m adcp migrate --contract" {when}'
        )
        assert describe(await migration_status(pool))[-1] == status[-2]
        _all_ok(await _old_pod(python, pool, workspace, notifications=False, ordinal=2))

        assert await _run(dsn, status=False, dry_run=False, contract=True) == 0
        assert capsys.readouterr().out.strip() == (
            "applied 0007 reporting_contract_generation_keys (contract)"
        )
        assert await _run(dsn, status=True, dry_run=False) == 0
        assert capsys.readouterr().out.strip().endswith("up to date")
