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
import time
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
    started = time.monotonic()
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
        # Give only this task-owned process group a bounded graceful shutdown,
        # then escalate. A timeout is a failing gate, never an implicit retry.
        cleanup = "terminated"
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            cleanup = "killed"
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    f"notification distribution {label}: cleanup_deadline pid={process.pid}"
                ) from None
        # Capture actionable process diagnostics without ever including child
        # prose, URLs, fixture values, provider output, or credentials.
        raise AssertionError(
            f"notification distribution {label}: deadline"
            f" pid={process.pid} exit={process.returncode} cleanup={cleanup}"
            f" elapsed_ms={int((time.monotonic() - started) * 1000)}"
            f" stdout_chars={len(stdout)} stderr_chars={len(stderr)}"
        ) from None
    # Do not turn package-manager/provider output into test diagnostics.
    assert process.returncode == 0, f"notification distribution {label}: exit {process.returncode}"
    print(f"notification_distribution stage={label} passed", flush=True)
    return stdout


def test_distribution_subprocess_deadline_is_bounded_and_sanitized(tmp_path):
    with pytest.raises(AssertionError, match=r"deadline_probe: deadline pid=\d+ exit=-(15|9)"):
        run_step(
            [sys.executable, "-c", "import threading; threading.Event().wait()"],
            label="deadline_probe",
            cwd=tmp_path,
            timeout=0,
        )


@pytest.fixture(scope="module")
def built_distribution(tmp_path_factory):
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
    return path, wheel, source


@pytest.fixture(scope="module", params=["wheel", "sdist"])
def installed_distribution(built_distribution, request):
    path, wheel, source = built_distribution
    distribution = wheel if request.param == "wheel" else source
    environment = path / f"installed-{request.param}"
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
    run_step([*installer, str(distribution)], label=f"{request.param}-base-install", cwd=path)
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
    ReportingActivityProjector,
    ReportingActivitySupport,
    resolve_reporting_consumer,
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
assert files("adcp.reporting.ledger").joinpath("reporting_webhook_activity.sql").is_file()
assert files("adcp.reporting.outbox").joinpath("required_schema.json").is_file()
assert get_named_validator("core/webhook-activity-record.json", version="3.2.0-rc.3") is not None
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
    return path, python, installer, distribution


def test_wheel_and_sdist_contain_exact_complete_sql_chain(built_distribution):
    _, wheel, source = built_distribution
    with zipfile.ZipFile(wheel) as archive, tarfile.open(source) as tar:
        prefix = tar.getnames()[0].split("/")[0]
        for name in (*CHAIN, "reporting_webhook_activity.sql"):
            expected = (ROOT / "src" / "adcp" / "reporting" / "ledger" / name).read_bytes()
            assert archive.read(f"adcp/reporting/ledger/{name}") == expected
            member = tar.extractfile(f"{prefix}/src/adcp/reporting/ledger/{name}")
            assert member is not None and member.read() == expected
        assert (
            archive.read("adcp/reporting/outbox/_schema.py")
            == (ROOT / "src" / "adcp" / "reporting" / "outbox" / "_schema.py").read_bytes()
        )
        expected = (ROOT / "src/adcp/reporting/outbox/required_schema.json").read_bytes()
        assert archive.read("adcp/reporting/outbox/required_schema.json") == expected
        member = tar.extractfile(f"{prefix}/src/adcp/reporting/outbox/required_schema.json")
        assert member is not None and member.read() == expected


def test_installed_base_import_needs_no_pg_extra(installed_distribution):
    # The fixture runs the import with PG genuinely absent, before any extra
    # installation, so collection/test order cannot accidentally fake this gate.
    _, python, _, _ = installed_distribution
    assert python.is_file()


async def test_installed_pg_extra_migrates_commits_and_restarts(installed_distribution):
    path, python, installer, distribution = installed_distribution
    async with isolated_reporting_pool(autocommit=True) as pool:
        await asyncio.to_thread(
            run_step, [*installer, str(distribution) + "[pg]"], label="pg-install", cwd=path
        )
        config = configuration()
        obligation = obligation_for(config)
        revision, rows = revision_for(obligation)
        values = {
            "conninfo": pool.conninfo,
            "kwargs": pool.kwargs,
            "now": NOW.isoformat(),
            "rows": rows,
            "required_objects": json.loads(
                (ROOT / "src/adcp/reporting/outbox/required_schema.json").read_text()
            ),
        }
        for name, record in (
            ("config", config),
            ("obligation", obligation),
            ("revision", revision),
        ):
            values[name] = TypeAdapter(type(record)).dump_python(record, mode="json")
        script = r"""
import asyncio, json, sys
from datetime import datetime, timedelta
from importlib.resources import files
from pydantic import TypeAdapter
from psycopg_pool import AsyncConnectionPool
from adcp.reporting.ledger import (
    PgReportingReconciliationStore,
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.outbox import (
    ActivityOutcome, ActivityRequest, PgReportingOutbox, ReportingEnvelopeCipher,
    ReportingLegacyAuthentication, ReportingNotificationSubscription,
    ReportingNotificationWorker, ReportingActivityProjector, ReportingActivitySupport,
)
from adcp.reporting.outbox._schema import schema_objects

values = json.load(sys.stdin)


async def main():
    clock = lambda: datetime.fromisoformat(values["now"])
    subscription = ReportingNotificationSubscription(
        account_id="acct_a", subscriber_id="buyer", principal_id="https://buyer.example/agent",
        url="https://example.test/api/PRIVATE_SECRET?token=QUERY_SECRET",
        event_types=("reporting.ledger_changed",), configuration_revision="revision-1",
        authorization_ref="grant-1", proof_of_control_ref="proof-1",
        authentication=ReportingLegacyAuthentication("Bearer", "AUTH_SECRET"),
        active=True, authorized=True, proof_valid=True,
    )
    class Configurations:
        async def list_active(self, *, account_id, notification_type):
            return (subscription,) if account_id == subscription.account_id else ()
        async def get_active(self, *, account_id, subscriber_id, notification_type):
            return subscription if account_id == subscription.account_id else None
    cipher = ReportingEnvelopeCipher(b"e" * 32)
    async with AsyncConnectionPool(
        values["conninfo"], kwargs=values["kwargs"], open=False
    ) as pool:
        await pool.wait(timeout=10)
        async with pool.connection() as connection:
            version = (await (await connection.execute("SHOW server_version")).fetchone())[0]
            assert version.startswith("16."), "package gate requires PostgreSQL 16.x"
        store = PgReportingReconciliationStore(pool=pool, clock=clock, notifications=True)
        await store.create_schema()
        async with pool.connection() as connection:
            objects = await schema_objects(connection)
            assert objects == values["required_objects"]
            assert json.dumps(objects, indent=2, sort_keys=True) + "\n" == files(
                "adcp.reporting.outbox"
            ).joinpath("required_schema.json").read_text()
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
        outbox = PgReportingOutbox(pool=pool, clock=clock)
        worker = ReportingNotificationWorker(
            outbox=outbox, activity=outbox, subscriptions=Configurations(), cipher=cipher,
            clock=clock,
        )
        projector = ReportingActivityProjector(outbox)
        assert await ReportingActivitySupport(worker, store, projector).durable()
        events = await outbox.list_events(account_id="acct_a")
        assert await worker.expand_one(account_id="acct_a")
        lease = await outbox.claim_delivery(account_id="acct_a", now=clock(), lease_seconds=60)
        original = cipher.open(lease.delivery).prepared
        attempt = await outbox.reserve_attempt(
            lease, request=ActivityRequest(subscription.url, len(original.body)), now=clock(),
        )
        assert attempt.attempt == 1 and "SECRET" not in str(attempt.to_wire())
        assert await outbox.complete_attempt(
            attempt, outcome=ActivityOutcome("failed", 503, 1), now=clock(),
        )
        assert await outbox.finish_delivery(lease, now=clock(), state="pending", retry_at=clock())
    async with AsyncConnectionPool(
        values["conninfo"], kwargs=values["kwargs"], open=False
    ) as fresh:
        await fresh.wait(timeout=10)
        await PgReportingReconciliationStore(pool=fresh).create_schema()
        async with fresh.connection() as connection:
            assert await schema_objects(connection) == values["required_objects"]
        outbox = PgReportingOutbox(pool=fresh, clock=clock)
        assert len(events) == 1 and await outbox.list_events(account_id="acct_a") == events
        assert await outbox.claim_expansion(
            account_id="acct_a", now=clock(), lease_seconds=60,
        ) is None
        lease = await outbox.claim_delivery(account_id="acct_a", now=clock(), lease_seconds=60)
        retried = cipher.open(lease.delivery).prepared
        assert retried.body == original.body and retried.idempotency_key == original.idempotency_key
        attempt = await outbox.reserve_attempt(
            lease, request=ActivityRequest(subscription.url, len(retried.body)), now=clock(),
        )
        assert attempt.attempt == 2
        assert await outbox.complete_attempt(
            attempt, outcome=ActivityOutcome("success", 200, 2), now=clock(),
        )
        assert await outbox.finish_delivery(lease, now=clock(), state="complete")
        rows = await outbox.list_activity(
            account_id="acct_a", consumer_id=subscription.principal_id,
        )
        assert [row.to_wire()["status"] for row in rows] == ["success", "failed"]
        assert await outbox.list_activity(account_id="acct_a", consumer_id="other") == ()
        assert await outbox.list_activity(
            account_id="other", consumer_id=subscription.principal_id,
        ) == ()
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
