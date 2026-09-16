"""Real wheel/sdist installation, lazy base imports, and installed [pg] restart."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from ._generation_support import (
    NOW,
    configuration,
    isolated_reporting_pool,
    obligation_for,
    revision_for,
)
from .test_reporting_notification_migration import CHAIN

ROOT = Path(__file__).resolve().parents[3]


def run_step(command, *, label, cwd, value=None, timeout=120):
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={key: item for key, item in os.environ.items() if key != "PYTHONPATH"},
    )
    print(f"notification_distribution stage={label} pid={process.pid} started", flush=True)
    try:
        stdout, _ = process.communicate(
            json.dumps(value) if value is not None else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # The new session contains only this test's build/install descendants.
        # Kill the owned group too, so a package-manager child cannot survive a
        # timed-out build or keep an inherited pipe open indefinitely.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"notification distribution {label}: cleanup_deadline pid={process.pid}"
            ) from None
        raise AssertionError(
            f"notification distribution {label}: deadline"
            f" pid={process.pid} exit={process.returncode}"
        ) from None
    # Do not turn package-manager/provider output into test diagnostics.
    assert process.returncode == 0, f"notification distribution {label}: exit {process.returncode}"
    print(f"notification_distribution stage={label} passed", flush=True)
    return stdout


def test_distribution_subprocess_deadline_is_bounded_and_sanitized(tmp_path):
    with pytest.raises(AssertionError, match=r"deadline_probe: deadline pid=\d+ exit=-9"):
        run_step(
            [sys.executable, "-c", "import threading; threading.Event().wait()"],
            label="deadline_probe",
            cwd=tmp_path,
            timeout=0,
        )


@pytest.fixture(scope="module")
def installed_distribution(tmp_path_factory):
    path = tmp_path_factory.mktemp("reporting-outbox-distribution")
    project = path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, project / name)
    for source in ROOT.glob("MIGRATION*.md"):
        shutil.copy2(source, project / source.name)
    shutil.copytree(
        ROOT / "src",
        project / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "_schemas"),
    )
    for version in ("2.5", "3.0", "3.1", "3.2.0-rc.3"):
        shutil.copytree(
            ROOT / "schemas" / "cache" / version, project / "schemas" / "cache" / version
        )
    dist = path / "dist"
    # build's default path makes an sdist, then builds the wheel FROM that sdist.
    run_step(
        [sys.executable, "-m", "build", "--outdir", str(dist), str(project)],
        label="build-sdist-and-wheel",
        cwd=path,
    )
    wheel, source = next(dist.glob("*.whl")), next(dist.glob("*.tar.gz"))
    environment = path / "installed"
    run_step(
        [sys.executable, "-m", "venv", str(environment)],
        label="isolated-environment",
        cwd=path,
        timeout=30,
    )
    python = environment / "bin" / "python"
    # UV is an acceleration only; normal pip-based CI exercises the same wheel.
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step([*installer, str(wheel)], label="base-install", cwd=path)
    base_check = r"""
import importlib.util
from importlib.resources import files
from pathlib import Path
import adcp
from adcp.reporting.outbox import (
    InMemoryReportingOutbox,
    PgReportingOutbox,
    ReportingEnvelopeCipher,
    ReportingNotificationWorker,
)
from adcp.reporting.ledger import InMemoryReportingLedgerStore, ReportingProducer
from adcp.validation.schema_loader import get_named_validator
import inspect

assert importlib.util.find_spec("psycopg") is None
assert importlib.util.find_spec("psycopg_pool") is None
assert "installed" in str(Path(adcp.__file__))
assert "notifications" not in inspect.signature(ReportingProducer).parameters
assert not hasattr(InMemoryReportingLedgerStore(), "commit_materialization")
outbox = InMemoryReportingOutbox(InMemoryReportingLedgerStore(notifications=True))
# The PG outbox must refuse construction with the same actionable extra hint
# the sibling PG stores raise, not a driver error from inside a later query.
try:
    PgReportingOutbox(pool=None)
except ImportError as error:
    assert "adcp[pg]" in str(error), str(error)
else:
    raise AssertionError("PgReportingOutbox must raise the [pg] install hint")
assert files("adcp.reporting.ledger").joinpath("reporting_notification_outbox.sql").is_file()
assert (
    get_named_validator("core/reporting-ledger-changed-webhook.json", version="3.2.0-rc.3")
    is not None
)
print("base-import-without-pg-ok")
"""
    assert (
        run_step([str(python), "-c", base_check], label="base-import-without-pg", cwd=path).strip()
        == "base-import-without-pg-ok"
    )
    return path, python, installer, wheel, source


def test_wheel_and_sdist_contain_exact_complete_sql_chain(installed_distribution):
    _, _, _, wheel, source = installed_distribution
    with zipfile.ZipFile(wheel) as archive, tarfile.open(source) as tar:
        prefix = tar.getnames()[0].split("/")[0]
        for name in CHAIN:
            expected = (ROOT / "src" / "adcp" / "reporting" / "ledger" / name).read_bytes()
            assert archive.read(f"adcp/reporting/ledger/{name}") == expected
            member = tar.extractfile(f"{prefix}/src/adcp/reporting/ledger/{name}")
            assert member is not None and member.read() == expected
        assert (
            archive.read("adcp/reporting/outbox/_schema.py")
            == (ROOT / "src" / "adcp" / "reporting" / "outbox" / "_schema.py").read_bytes()
        )


def test_installed_base_import_needs_no_pg_extra(installed_distribution):
    # The fixture runs the import with PG genuinely absent, before any extra
    # installation, so collection/test order cannot accidentally fake this gate.
    _, python, _, _, _ = installed_distribution
    assert python.is_file()


async def test_installed_pg_extra_migrates_commits_and_restarts(installed_distribution):
    path, python, installer, wheel, _ = installed_distribution
    async with isolated_reporting_pool(autocommit=True) as pool:
        await asyncio.to_thread(
            run_step, [*installer, str(wheel) + "[pg]"], label="pg-install", cwd=path
        )
        config = configuration()
        obligation = obligation_for(config)
        revision, rows = revision_for(obligation)
        values = {
            "conninfo": pool.conninfo,
            "kwargs": pool.kwargs,
            "now": NOW.isoformat(),
            "rows": rows,
        }
        for name, record in (
            ("config", config),
            ("obligation", obligation),
            ("revision", revision),
        ):
            values[name] = TypeAdapter(type(record)).dump_python(record, mode="json")
        script = r"""
import asyncio, json, sys
from datetime import datetime
from pydantic import TypeAdapter
from psycopg_pool import AsyncConnectionPool
from adcp.reporting.ledger import (
    PgReportingReconciliationStore,
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.outbox import PgReportingOutbox

values = json.load(sys.stdin)


async def main():
    clock = lambda: datetime.fromisoformat(values["now"])
    async with AsyncConnectionPool(
        values["conninfo"], kwargs=values["kwargs"], open=False
    ) as pool:
        await pool.wait(timeout=10)
        store = PgReportingReconciliationStore(pool=pool, clock=clock, notifications=True)
        await store.create_schema()
        await store.put_configuration(
            TypeAdapter(ReportingConfiguration).validate_python(values["config"])
        )
        await store.commit_obligation(
            TypeAdapter(ReportingObligationRecord).validate_python(values["obligation"])
        )
        await store.commit_revision(
            TypeAdapter(ReportingRevisionRecord).validate_python(values["revision"]),
            values["rows"],
        )
        events = await PgReportingOutbox(pool=pool, clock=clock).list_events(
            account_id="acct_a"
        )
    async with AsyncConnectionPool(
        values["conninfo"], kwargs=values["kwargs"], open=False
    ) as fresh:
        await fresh.wait(timeout=10)
        await PgReportingReconciliationStore(pool=fresh).create_schema()
        outbox = PgReportingOutbox(pool=fresh, clock=clock)
        assert len(events) == 1 and await outbox.list_events(account_id="acct_a") == events
        assert (
            await outbox.claim_expansion(account_id="acct_a", now=clock(), lease_seconds=60)
            is not None
        )
        assert await outbox.list_events(account_id="other") == ()
    print("installed-pg-restart-ok")


asyncio.run(asyncio.wait_for(main(), 35))
"""
        result = await asyncio.to_thread(
            run_step,
            [str(python), "-c", script],
            label="pg-migration-restart",
            cwd=path,
            value=values,
            timeout=45,
        )
        assert result.strip() == "installed-pg-restart-ok"
