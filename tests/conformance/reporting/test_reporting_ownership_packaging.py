"""Owned generation resources and read contracts in installed wheel and sdist."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tarfile
import zipfile

import pytest

from ._generation_support import require_rolling_database
from .test_reporting_materializer_packaging import b1_wheels, built_distribution
from .test_reporting_notification_packaging import ROOT, run_step

__all__ = ["b1_wheels", "built_distribution"]

SMOKE = r"""
import asyncio, json, secrets, sys
from dataclasses import replace
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from adcp.reporting.ledger import (
    ReportingConfiguration,
    ReportingScheduleSpec,
    ReportingStatusCaller,
)
from adcp.reporting.migration import ReportingOwnershipBackfill
from adcp.reporting.service import ReliableReportingService
from adcp.server import ADCPHandler
from psycopg import AsyncConnection, sql
from psycopg_pool import AsyncConnectionPool

settings = json.load(sys.stdin)
assert Path(__import__("adcp.reporting.migration", fromlist=["*"]).__file__).is_relative_to(
    Path(sys.prefix)
)
assert (
    "retain_without_replay"
    in files("adcp.reporting.ledger").joinpath("reporting_caller_ownership.sql").read_text()
)


async def verify(service):
    await service.start()
    try:
        config = ReportingConfiguration(
            "daily",
            1,
            "account",
            "buyer-a",
            "definition",
            "paid_media_delivery",
            "analytics",
            ReportingScheduleSpec("PT1H", "PT1H"),
            "snapshot",
        )
        await service.store.put_configuration(config)
        await service.store.put_configuration(
            replace(config, consumer_id="buyer-b", feed_purpose="billing")
        )
        a, b = (ReportingStatusCaller("account", owner) for owner in ("buyer-a", "buyer-b"))
        assert (await service.store.list_configurations(caller=a))[0].feed_purpose == "analytics"
        assert (await service.store.list_configurations(caller=b))[0].feed_purpose == "billing"
        assert (await service.store.read_status_snapshot(caller=a)).configurations == (config,)

        class Seller(ADCPHandler):
            pass

        mounted = service.install(Seller())
        context = type("Context", (), {"caller_identity": "buyer-a"})()
        response = await mounted.get_reporting_status(
            {"account": {"account_id": "account"}, "view": "summary"}, context
        )
        assert response["account_id"] == "account" and response["obligation_counts"]["total"] == 0
    finally:
        await service.close()


async def main():
    resolver = lambda request, context: ReportingStatusCaller(
        request["account"]["account_id"], context.caller_identity
    )
    await verify(
        ReliableReportingService.memory(account_context=lambda _: None, caller_resolver=resolver)
    )
    schema = "adcp_owned_install_" + secrets.token_hex(6)
    async with await AsyncConnection.connect(settings["url"], autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with AsyncConnectionPool(
                settings["url"],
                kwargs={"options": "-csearch_path=" + schema},
                min_size=1,
                max_size=4,
                open=False,
            ) as pool:
                await pool.wait()
                await verify(
                    ReliableReportingService.postgres(
                        pool=pool, account_context=lambda _: None, caller_resolver=resolver
                    )
                )
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    sys.path.insert(0, settings["fixtures"])
    from tests.conformance.reporting.test_reporting_caller_ownership import (
        test_installed_managed_admission_reuses_daily_for_two_callers,
    )
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        for backend in ("memory", "postgres"):
            destination = Path(directory) / backend
            destination.mkdir()
            await test_installed_managed_admission_reuses_daily_for_two_callers(
                backend, destination
            )
    assert Path(__import__("adcp.reporting.production", fromlist=["*"]).__file__).is_relative_to(Path(sys.prefix))
    print("installed owned Core and managed memory and PostgreSQL passed")


asyncio.run(main())

"""


@pytest.mark.parametrize("kind", ["vcs", "sdist"])
async def test_owned_resources_and_private_reads_installed(b1_wheels, built_distribution, kind):
    require_rolling_database()
    url = os.environ["ADCP_PG_TEST_URL"]
    root, wheels, _ = b1_wheels
    _, _, source = built_distribution
    resources = [
        "migration.py",
        "ledger/reporting_caller_ownership.sql",
        "ledger/reporting_ledger.sql",
        "ledger/reporting_ledger_reconciliation.sql",
        "ledger/reporting_production.sql",
        "production/service_context.sql",
        "production/service_context_schema.json",
        "production/required_schema.json",
        "outbox/required_schema.json",
        "materializer/required_schema.json",
    ]
    with zipfile.ZipFile(wheels[kind]) as wheel, tarfile.open(source) as archive:
        prefix = archive.getnames()[0].split("/")[0]
        for relative in resources:
            expected = (ROOT / "src/adcp/reporting" / relative).read_bytes()
            assert wheel.read("adcp/reporting/" + relative) == expected
            assert (
                archive.extractfile(prefix + "/src/adcp/reporting/" + relative).read() == expected
            )
    environment = root / ("owned-installed-" + kind)
    await asyncio.to_thread(
        run_step,
        [os.environ.get("ADCP_PYTHON310", sys.executable), "-m", "venv", str(environment)],
        label="owned-environment",
        cwd=root,
    )
    python = environment / "bin/python"
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    await asyncio.to_thread(
        run_step,
        [*installer, str(wheels[kind]) + "[pg]", "pytest", "pytest-asyncio", "asgi-lifespan"],
        label="owned-install",
        cwd=root,
        timeout=180,
    )
    fixture_root = root / ("owned-fixtures-" + kind)
    shutil.copytree(
        ROOT / "tests", fixture_root / "tests", ignore=shutil.ignore_patterns("__pycache__")
    )
    await asyncio.to_thread(
        run_step,
        [str(python), "-I", "-c", SMOKE],
        label="owned-installed-core",
        cwd=root,
        value={"url": url, "fixtures": str(fixture_root)},
        timeout=180,
    )
