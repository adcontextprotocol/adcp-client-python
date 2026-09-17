"""Actual built/installed frozen artifacts, not renamed or path-selected modules."""

import asyncio
import hashlib
import json
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

from adcp.reporting.ledger import LedgerConflictError, PgReportingReconciliationStore
from adcp.reporting.materializer import PgReportingMaterializerStore
from adcp.reporting.outbox._schema import schema_objects

from ._durable_materializer_support import DurableHarness, durable_case
from ._generation_support import assert_c_collated_rolling_database, isolated_reporting_pool
from .test_reporting_notification_packaging import ROOT, run_step

ARTIFACTS = {
    "beta15": "3e76aa54623529a3dda01cd690b8a5c287c75641",
    "records": "3c405a21f978ed9d3208611bb4a7a8434a056933",
    "integration": "037de4ac822ecefb2f95d32c15c297fb4c45d683",
    "a": "21bf443e7d850d1800ec8a6f2e4abec1c8f85541",
    "b": "198d50e61c74fb82aedbf2c77e06a0e200b91db6",
    "c": "ea150fabd5ad90e3abf93f89729d2919f1c61798",
    "b1": "1c91311ec28d25506d5db43f59d0c34936ecb8f7",
}


def build_frozen(artifact, tmp_path_factory, request, *, sha=None):
    assert_c_collated_rolling_database()
    sha = sha or ARTIFACTS[artifact]
    root = tmp_path_factory.mktemp(f"materializer-{artifact}")
    request.addfinalizer(lambda: shutil.rmtree(root))
    archive, source, dist, environment = (
        root / n for n in ("source.tar.gz", "source", "dist", "installed")
    )
    source.mkdir()
    run_step(
        ["git", "archive", "--format=tar.gz", "-1", f"--output={archive}", sha],
        label=f"{artifact}-exact-git-archive",
        cwd=ROOT,
    )
    with tarfile.open(archive) as tar:
        tar.extractall(source, filter="data")
    archive.unlink()
    run_step(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(source)],
        label=f"{artifact}-build-frozen-wheel",
        cwd=root,
        timeout=180,
    )
    wheel = next(dist.glob("*.whl"))
    run_step(
        [sys.executable, "-m", "venv", str(environment)],
        label=f"{artifact}-isolated-environment",
        cwd=root,
    )
    python = environment / "bin/python"
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, f"{wheel}[pg]"],
        label=f"{artifact}-install-frozen-wheel",
        cwd=root,
        timeout=180,
    )
    script = root / "frozen.py"
    shutil.copy2(Path(__file__).with_name("_materializer_frozen.py"), script)
    modules = {}
    for relative in (
        "ledger/pg.py",
        "ledger/delivery_pg.py",
        "outbox/worker.py",
        "outbox/status_pg.py",
        "materializer/contracts.py",
        "materializer/verification.py",
        "materializer/pg.py",
        "materializer/capture.py",
    ):
        path = source / "src/adcp/reporting" / relative
        if path.exists():
            modules["adcp.reporting." + relative.removesuffix(".py").replace("/", ".")] = (
                hashlib.sha256(path.read_bytes()).hexdigest()
            )
    settings = {
        "artifact": artifact,
        "sha": sha,
        "modules": modules,
        "workspace": str(ROOT),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    # The exact source hashes and wheel survive in the evidence log; the build
    # tree contains 2.2 GiB of cached schemas and is not an execution dependency.
    print(json.dumps({"frozen_build": settings}), flush=True)
    shutil.rmtree(source)
    return (
        root,
        python,
        script,
        settings,
    )


@pytest.fixture(scope="module")
def installed_parent(tmp_path_factory, request):
    return build_frozen("b1", tmp_path_factory, request)


@pytest.fixture(scope="module", params=tuple(ARTIFACTS))
def installed_frozen(request, tmp_path_factory, installed_parent):
    if request.param == "b1":
        return installed_parent
    return build_frozen(request.param, tmp_path_factory, request)


async def frozen_call(artifact, pool, action, **kwargs):
    root, python, script, settings = artifact
    settings = {
        **settings,
        "conninfo": pool.conninfo,
        "kwargs": pool.kwargs,
        "action": action,
        **kwargs,
    }
    raw = await asyncio.to_thread(
        run_step,
        [str(python), "-I", str(script)],
        label=f"{settings['artifact']}-{action}",
        cwd=root,
        value=settings,
        timeout=60,
    )
    result = json.loads(raw)
    assert "failure" not in result, result
    return result


async def test_installed_old_reader_writer_and_workers_on_populated_materializer_schema(
    installed_frozen,
    installed_parent,
):
    async with isolated_reporting_pool(autocommit=True) as pool:
        evidence = await frozen_call(installed_frozen, pool, "install")
        assert evidence["installed"]
        # The approved B1 wheel installs the comparison baseline, including
        # reviewed C's additive capture and B1's unactivated selector fence.
        parent = await frozen_call(installed_parent, pool, "install")
        assert parent["sha"] == ARTIFACTS["b1"]
        baseline_store = PgReportingReconciliationStore(pool=pool, notifications=True)
        # beta.15 can read its original definition shape. Unit declarations are
        # an A prerequisite, not a B2 schema or decoder change. New Managed facts
        # stay in A's separate feed and never enter beta.15's closed Core feed.
        before_url_principals = evidence["artifact"] in {
            "beta15",
            "records",
            "integration",
            "a",
            "b",
        }
        case = await durable_case(
            baseline_store,
            count=3,
            consumer=(
                "frozen-buyer" if before_url_principals else "https://buyer.example.test/agent"
            ),
            legacy_definition=evidence["artifact"] == "beta15",
        )
        # URL principals became readable in C. Earlier binaries still read their
        # opaque caller's records while a separate URL consumer has populated
        # materializations/captures/events in the same account and schema.
        sibling = await durable_case(
            baseline_store,
            count=3,
            consumer="https://buyer.example.test/isolated",
            legacy_definition=evidence["artifact"] == "beta15",
        )
        baseline = await frozen_call(
            installed_frozen, pool, "baseline", consumer=case.binding.consumer_id
        )
        assert baseline["ordinary_writes"] and baseline["core_records"] == 2
        async with pool.connection() as connection:
            old_objects = await schema_objects(connection)
        store = PgReportingMaterializerStore(pool=pool, notifications=True)
        with pytest.raises(LedgerConflictError):
            await store.materializer_ready()
        await store.create_schema()
        async with pool.connection() as connection:
            objects = await schema_objects(connection)
        assert {key: objects[key] for key in old_objects} == old_objects
        assert all("reporting_materializer_" in key for key in objects.keys() - old_objects.keys())
        case.store = sibling.store = store
        assert (await case.service().run_once()).state == "verified"
        assert (await sibling.service().run_once()).state == "verified"
        h = DurableHarness(store, None, pool)
        before = await h.queue()
        async with pool.connection() as connection:
            immutable_before = [
                await (await connection.execute(f"SELECT * FROM {table} ORDER BY 1,2,3")).fetchall()
                for table in (
                    "reporting_materializer_work",
                    "reporting_materializer_status_boundaries",
                    "reporting_materializer_status_heads",
                    "reporting_materializer_notification_events",
                    "reporting_materializer_notification_expansions",
                )
            ]
            captured_heads = await (
                await connection.execute(
                    "SELECT account_id,captured_sequence FROM reporting_materializer_accounts"
                    " ORDER BY account_id"
                )
            ).fetchall()
        result = await frozen_call(
            installed_frozen, pool, "exercise", consumer=case.binding.consumer_id
        )
        assert result["core_records"] == 2 and result["ordinary_writes"]
        assert result["managed_records"] == (0 if result["artifact"] == "beta15" else 4)
        assert result["notifications_ready"] == baseline["notifications_ready"]
        assert await h.queue() == before
        async with pool.connection() as connection:
            immutable_after = [
                await (await connection.execute(f"SELECT * FROM {table} ORDER BY 1,2,3")).fetchall()
                for table in (
                    "reporting_materializer_work",
                    "reporting_materializer_status_boundaries",
                    "reporting_materializer_status_heads",
                    "reporting_materializer_notification_events",
                    "reporting_materializer_notification_expansions",
                )
            ]
            assert (
                await (
                    await connection.execute(
                        "SELECT account_id,captured_sequence FROM reporting_materializer_accounts"
                        " ORDER BY account_id"
                    )
                ).fetchall()
            ) == captured_heads
        assert immutable_after == immutable_before
        assert before[1] == ("quarantined", "quarantined")
        assert await store.materializer_ready()
        print(
            json.dumps(
                {
                    **result,
                    "native": evidence,
                    "baseline": baseline,
                    "baseline_installer": parent,
                    "additive_objects": len(objects.keys() - old_objects.keys()),
                    "wheel_sha256": installed_frozen[3]["wheel_sha256"],
                }
            ),
            flush=True,
        )
