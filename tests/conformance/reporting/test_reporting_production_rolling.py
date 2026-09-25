"""Actual approved B2.3 and hardening binaries across installed B2.4 activation."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

import pytest

from adcp.reporting.ledger import revision_content_sha256
from adcp.reporting.ledger.models import derive_period

from ._feed_support import feed_harness, feed_request, mixed_case, walk
from ._production_packaging import copied_fixtures, inspect_distribution, source_basis
from ._production_support import production_harness
from .test_reporting_feed_hardening_installed import approved_b23
from .test_reporting_feed_installed_pg import (
    b1_wheels,
    built_distribution,
    feed_wheels,
    installed_feed,
)
from .test_reporting_feed_process import feed_process
from .test_reporting_materializer_process import Child
from .test_reporting_materializer_rolling import build_frozen
from .test_reporting_notification_packaging import ROOT, run_step

__all__ = ["approved_b23", "b1_wheels", "built_distribution", "feed_wheels", "installed_feed"]
HARDENING = "e16eb8cf3074cabd45aab42840950f05ad6d2b43"


@pytest.fixture(scope="module", params=["b23", "hardening"])
def production_parent(request, tmp_path_factory):
    if request.param == "b23":
        return request.getfixturevalue("approved_b23")
    root, _, _, identity = build_frozen("hardening-b24", tmp_path_factory, request, sha=HARDENING)
    interpreter = os.environ.get("ADCP_PYTHON310")
    if interpreter is None:
        pytest.skip("ADCP_PYTHON310 supplies the installed floor artifact")
    environment = root / "python310"
    run_step(
        [interpreter, "-m", "venv", str(environment)], label="hardening-floor-environment", cwd=root
    )
    python = environment / "bin/python"
    wheel = next((root / "dist").glob("*.whl"))
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, f"{wheel}[pg]", "asgi-lifespan==2.1.0"],
        label="hardening-floor-install",
        cwd=root,
        timeout=180,
    )
    from .test_reporting_feed_packaging import feed_modules

    with zipfile.ZipFile(wheel) as archive:
        modules = {}
        for name in {*feed_modules(), "adcp.reporting.outbox.status_pg"}:
            member = name.replace(".", "/") + ".py"
            if member not in archive.namelist():
                member = name.replace(".", "/") + "/__init__.py"
            raw = archive.read(member)
            assert raw == subprocess.check_output(
                ["git", "show", f"{HARDENING}:src/{member}"], cwd=ROOT
            )
            modules[name] = hashlib.sha256(raw).hexdigest()
    script, helper = root / "feed_process.py", root / "receipt_transport.py"
    shutil.copy2(Path(__file__).with_name("_feed_process.py"), script)
    shutil.copy2(Path(__file__).with_name("_receipt_transport.py"), helper)
    return (
        root,
        python,
        script,
        helper,
        {
            **identity,
            "modules": modules,
            "python": [3, 10],
            "tree": "c043d1e14c5071859f566e07cc9980058fa6ee07",
        },
        wheel,
    )


@pytest.fixture(scope="module")
def production_install(installed_feed, feed_wheels, built_distribution, production_parent):
    root, python, _, _, current = installed_feed
    parent_label = production_parent[4]["sha"][:12]
    label = parent_label + "-" + current["distribution"]
    _, wheels, _ = feed_wheels
    _, _, source = built_distribution
    modules, assets = inspect_distribution(wheels[current["distribution"]], source)
    fixture_root = copied_fixtures(root, label + "-restart")
    script = fixture_root / "production_process.py"
    shutil.copy2(Path(__file__).with_name("_production_installed_process.py"), script)
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, "pytest==9.1.1", "pytest-asyncio==1.4.0", "respx==0.23.1"],
        label="production-process-fixtures",
        cwd=root,
        timeout=180,
    )
    return (
        python,
        script,
        {
            **current,
            "fixtures": str(fixture_root),
            "modules": modules,
            "assets": assets,
            "source_basis": source_basis(
                wheels[current["distribution"]],
                source,
                evidence=Path(os.environ.get("ADCP_PRODUCTION_EVIDENCE", root / "evidence")),
                label=label + "-rolling",
            ),
        },
    )


@asynccontextmanager
async def installed_child(python, script, settings, path):
    log = path / settings.get(
        "diagnostic_name", "activation.log" if settings["pause"] else "continuation.log"
    )
    try:
        with log.open("xb") as diagnostic:
            process = await asyncio.create_subprocess_exec(
                str(python),
                "-I",
                str(script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=diagnostic,
            )

            class ActivationChild(Child):
                async def event(self, point):
                    line = await asyncio.wait_for(self.process.stdout.readline(), 120)
                    assert (
                        line
                    ), f"installed process exited before {point}; diagnostic retained at {log}"
                    result = json.loads(line)
                    assert result["point"] == point
                    return result

            child = ActivationChild(process)
            try:
                await child.send(settings)
                yield child
            finally:
                await child.kill()
    finally:
        raw = log.read_bytes()
        retained = None
        if os.environ.get("ADCP_PRODUCTION_EVIDENCE"):
            evidence = Path(os.environ["ADCP_PRODUCTION_EVIDENCE"])
            evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
            retained = evidence / (settings["evidence_key"] + "-" + log.name)
            with retained.open("xb") as stream:
                stream.write(raw)
        print(
            json.dumps(
                {
                    "installed_activation_process_log": str(log),
                    "retained_log": str(retained) if retained else None,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            ),
            flush=True,
        )


@pytest.mark.parametrize("notifications", [False, True])
async def test_actual_parent_page_one_to_installed_activation_sigkill_and_complete_walk(
    production_parent, production_install, notifications, tmp_path
):
    old_root, old_python, old_script, old_helper, old, old_wheel = production_parent
    python, script, current = production_install
    evidence_key = f"{old['sha'][:12]}-{current['distribution']}-{int(notifications)}"
    async with feed_harness("postgres", notifications=notifications) as h:
        case, receipt_request, receipt_response = await mixed_case(h)
        # Seed public ordinary records on the parent schema. The memory fixture
        # supplies only deterministic input values and a provider grant; the
        # installed parent, below, creates the actual attempt and durable work.
        async with production_harness(
            "memory",
            tmp_path / "destination.sqlite",
            notifications=notifications,
            count=0,
            reconciled=True,
            identity_prefix="b24-",
        ) as seed:
            item = seed.item
            await h.store.put_configuration(item.config)
            await h.store.commit_obligation(item.obligation)
            await h.store.put_destination_binding(item.binding)
            await h.store.commit_revision(item.revision, item.rows)
            key = asdict(item.verifier.key)
        legacy_script = old_root / "production_legacy_process.py"
        shutil.copy2(Path(__file__).with_name("_production_legacy_process.py"), legacy_script)
        legacy_module = subprocess.check_output(
            ["git", "show", f"{old['sha']}:src/adcp/reporting/outbox/status_pg.py"], cwd=ROOT
        )
        materializer_module = subprocess.check_output(
            ["git", "show", f"{old['sha']}:src/adcp/reporting/materializer/pg.py"], cwd=ROOT
        )
        legacy_settings = {
            "conninfo": h.pool.conninfo,
            "kwargs": h.pool.kwargs,
            "notifications": notifications,
            "module_sha256": hashlib.sha256(legacy_module).hexdigest(),
            "materializer_module_sha256": hashlib.sha256(materializer_module).hexdigest(),
            "verification_key": key,
            "evidence_key": evidence_key,
        }
        async with installed_child(
            old_python,
            legacy_script,
            {
                **legacy_settings,
                "pending": True,
                "pause": True,
                "diagnostic_name": "historical-pending.log",
                "revision_id": item.revision.reporting_revision_id,
            },
            tmp_path,
        ) as child:
            pending = await child.event("pending")
            await child.kill()
            assert child.process.returncode == -9
        async with h.pool.connection() as connection:
            assert (
                await (
                    await connection.execute("SELECT to_regclass('reporting_production_accounts')")
                ).fetchone()
            )[0] is None
        pending_before_migration = (await h.image())["reporting_materializer_work"]
        options = {
            "python": old_python,
            "script": old_script,
            "helper": old_helper,
            "installed": old,
        }
        async with feed_process(h, case, feed_request(case), pause="committed", **options) as child:
            first = (await child.event("committed"))["result"]
            await child.kill()
            assert child.process.returncode == -9
        snapshot = await h.store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=case.binding.principal
        )
        expected = await walk(h.store, feed_request(case), case.binding.principal, first=first)
        # Change actual live evidence while the original binary's frozen pages
        # stay open. No reconstruction of its original representation is used.
        await h.store.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=case.revision.reporting_revision_id,
            readable=False,
        )
        settings = {
            **current,
            "conninfo": h.pool.conninfo,
            "kwargs": h.pool.kwargs,
            "destination": str(tmp_path / "destination.sqlite"),
            "notifications": notifications,
            "caller": {"account_id": "acct_a", "consumer_id": case.binding.consumer_id},
            "receipt_request": receipt_request,
            "receipt_response": receipt_response,
            "historical_pending": pending,
            "evidence_key": evidence_key,
            "pause": True,
        }
        async with installed_child(python, script, settings, tmp_path) as child:
            activated = await child.event("activated")
            await child.kill()
            assert child.process.returncode == -9
        assert activated["pending_continuation"]["state"] == "verified"
        assert activated["pending_continuation"]["external_id"] == pending["external_id"]
        assert (
            await h.store.read_reporting_feed_snapshot(
                snapshot.snapshot_id, caller=case.binding.principal
            )
            == snapshot
        )
        # Run the actual inherited binary's projector primitive and sweeper on
        # this newly activated schema, with no child SDK imported into it.
        # An actually eligible revision for the next period makes this an old
        # reservation exclusion test, rather than an idle-worker observation.
        selected = await h.store.get_revision(
            account_id="acct_a", reporting_revision_id="b24-production-revision"
        )
        assert selected is not None and selected.row_count == 0
        original_obligation = await h.store.get_obligation(
            account_id="acct_a", reporting_obligation_id=selected.reporting_obligation_id
        )
        period = derive_period(
            item.config.schedule, account_timezone=item.config.account_timezone, ordinal=1
        )
        new_obligation = replace(
            original_obligation,
            reporting_obligation_id="b24-fence-next-period",
            period=period,
            scope_resolved_at=period.end,
            automated_recovery_deadline_at=period.expected_at
            + item.config.automated_recovery_window,
        )
        await h.store.commit_obligation(new_obligation)
        revision_id = "b24-fence-next-revision"
        await h.store.commit_revision(
            replace(
                selected,
                reporting_revision_id=revision_id,
                reporting_obligation_id=new_obligation.reporting_obligation_id,
                data_through=period.end,
                observed_at=period.end,
                finalized_at=period.end,
                created_at=period.end + timedelta(seconds=1),
                revision_content_sha256=revision_content_sha256(
                    reporting_revision_id=revision_id,
                    row_count=0,
                    control_totals=selected.control_totals,
                    reporting_rows=[],
                    control_total_evidence=selected.managed_control_totals,
                ),
            ),
            [],
        )
        before_old = await h.image()
        fenced = json.loads(
            await asyncio.to_thread(
                run_step,
                [str(old_python), "-I", str(legacy_script)],
                label="actual-parent-projector-fence",
                cwd=old_root,
                value=legacy_settings,
                timeout=90,
            )
        )
        assert fenced["historical_projection"] == "trigger_fenced"
        assert fenced["historical_materializer"] == "reservation_trigger_fenced"
        assert await h.image() == before_old
        settings.update(
            pause=False,
            new_revision_after_snapshot=revision_id,
            continuation=feed_request(
                case, pagination={"cursor": first["pagination"]["cursor"], "max_results": 1}
            ),
            new_continuation=feed_request(
                case,
                pagination={"cursor": activated["first"]["pagination"]["cursor"], "max_results": 1},
            ),
        )
        async with installed_child(python, script, settings, tmp_path) as child:
            result = await child.event("done")
            production_operation_1 = await asyncio.wait_for(child.process.wait(), 10)
            assert production_operation_1 == 0
        assert result["legacy"] == {
            "pages": expected[0][1:],
            "binding": snapshot.binding,
            "version": snapshot.representation_version,
            "ownership_mode": snapshot.ownership_mode,
        }
        assert result["new"] == activated["new_remaining"]
        assert result["fresh_external_writes"] == 1
        assert result["new"]["version"] == 2 and result["new"]["ownership_mode"] == "bindings"
        assert (
            await h.store.read_reporting_feed_snapshot(
                snapshot.snapshot_id, caller=case.binding.principal
            )
            == snapshot
        )
        print(
            json.dumps(
                {
                    "b24_actual_parent_activation_restart": old["sha"],
                    "parent_tree": old["tree"],
                    "parent_wheel_sha256": hashlib.sha256(old_wheel.read_bytes()).hexdigest(),
                    "current": current,
                    "notifications": notifications,
                    "historical_pending": pending,
                    "pending_continuation": activated["pending_continuation"],
                    "pending_before_migration_sha256": hashlib.sha256(
                        json.dumps(pending_before_migration, sort_keys=True).encode()
                    ).hexdigest(),
                    "parent_fence": fenced,
                    "legacy_page_count": len(expected[0]),
                    "new_page_count": len(result["new"]["pages"]) + 1,
                    "legacy_snapshot_sha256": hashlib.sha256(
                        json.dumps(expected[0], sort_keys=True).encode()
                    ).hexdigest(),
                    "new_origins": result["origins"],
                }
            ),
            flush=True,
        )
