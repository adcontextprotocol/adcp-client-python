"""An additional exact B2.4 artifact boundary; older rolling identities stay intact."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

from ._generation_support import isolated_reporting_pool
from ._production_packaging import copied_fixtures, production_modules, source_basis
from .test_reporting_feed_installed_pg import (
    b1_wheels,
    built_distribution,
    feed_wheels,
    installed_feed,
)
from .test_reporting_materializer_rolling import build_frozen
from .test_reporting_notification_packaging import ROOT, run_step

__all__ = ["b1_wheels", "built_distribution", "feed_wheels", "installed_feed"]
B24 = "34c8f6d929aeac3407e2f595104a8e903e572623"
B24_TREE = "2dd33404cb50e6d87ae875ccfa1c983e7faabd44"


def installer(python):
    return (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )


@pytest.fixture(scope="module")
def accepted_b24(request, tmp_path_factory):
    root, _, _, identity = build_frozen("accepted-b24-rc6", tmp_path_factory, request, sha=B24)
    interpreter = os.environ.get("ADCP_PYTHON310")
    assert interpreter, "the accepted B2.4 boundary requires the installed Python 3.10 floor"
    assert (
        subprocess.check_output(["git", "rev-parse", B24 + "^{tree}"], cwd=ROOT, text=True).strip()
        == B24_TREE
    )
    environment = root / "python310"
    run_step([interpreter, "-m", "venv", str(environment)], label="b24-rc6-floor-env", cwd=root)
    python = environment / "bin/python"
    wheel = next((root / "dist").glob("*.whl"))
    run_step(
        [*installer(python), f"{wheel}[pg]", "asgi-lifespan==2.1.0", "pytest==9.1.1"],
        label="b24-rc6-floor-install",
        cwd=root,
        timeout=180,
    )
    historical_paths = set(
        subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", B24, "--", "src/adcp"],
            cwd=ROOT,
            text=True,
        ).splitlines()
    )
    modules = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in production_modules():
            member = name.replace(".", "/") + ".py"
            if "src/" + member not in historical_paths:
                member = name.replace(".", "/") + "/__init__.py"
            # New production modules are absent from the pinned B2.4 source and wheel.
            if "src/" + member not in historical_paths:
                continue
            raw = archive.read(member)
            assert raw == subprocess.check_output(["git", "show", f"{B24}:src/{member}"], cwd=ROOT)
            modules[name] = hashlib.sha256(raw).hexdigest()
    return root, python, wheel, {**identity, "tree": B24_TREE, "modules": modules}


@pytest.fixture(scope="module")
def rc6_installed_boundary(accepted_b24, installed_feed, feed_wheels, built_distribution):
    old_root, old_python, old_wheel, old = accepted_b24
    root, python, _, _, current = installed_feed
    run_step(
        [*installer(python), "pytest==9.1.1"], label="rc6-rolling-fixtures", cwd=root, timeout=180
    )
    fixtures = copied_fixtures(root, "rc6-continuation-" + current["distribution"])
    script = fixtures / "rc6_continuation_process.py"
    shutil.copy2(Path(__file__).with_name("_rc6_continuation_process.py"), script)
    evidence = Path(
        os.environ.get("ADCP_PRODUCTION_EVIDENCE")
        or tempfile.mkdtemp(prefix="adcp-rc6-rolling-evidence-")
    )
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    retained = evidence / "accepted-b24.whl"
    if not retained.exists():
        shutil.copy2(old_wheel, retained)
    assert hashlib.sha256(retained.read_bytes()).hexdigest() == old["wheel_sha256"]
    _, wheels, _ = feed_wheels
    _, _, sdist = built_distribution
    basis = source_basis(
        wheels[current["distribution"]],
        sdist,
        evidence=evidence,
        label="rc6-rolling-" + current["distribution"],
    )
    old.update(
        workspace=str(ROOT),
        fixtures=str(fixtures),
        artifact={"head": B24, "tree": B24_TREE, "wheel_sha256": old["wheel_sha256"]},
    )
    current.update(
        workspace=str(ROOT),
        fixtures=str(fixtures),
        modules=production_modules(),
        artifact={"source_basis": basis, "wheel_sha256": current["wheel_sha256"]},
    )
    return old_python, python, script, old, current, evidence


def execute(python, script, settings, evidence, label):
    """Retain complete child stdout/stderr before asserting its terminal result."""
    command = [str(python), "-I", str(script)]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=script.parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(json.dumps(settings).encode(), timeout=180)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    record = {
        "command": command,
        "exit": process.returncode,
        "seconds": round(time.monotonic() - started, 3),
    }
    for stream, raw in (("stdout", stdout), ("stderr", stderr)):
        path = evidence / (label + "." + stream)
        with path.open("xb") as output:
            output.write(raw)
        record[stream] = {
            "path": str(path),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    (evidence / (label + ".json")).write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"rc6_installed_continuation": record}), flush=True)
    assert (
        process.returncode == 0
    ), f"installed continuation failed; original diagnostics: {evidence / label}"
    return json.loads(stdout)


@pytest.mark.parametrize("notifications", [False, True])
async def test_actual_accepted_b24_snapshot_to_rc6_installed_restart(
    rc6_installed_boundary, notifications
):
    old_python, python, script, old, current, evidence = rc6_installed_boundary
    label = current["distribution"] + "-" + str(int(notifications))
    async with isolated_reporting_pool(autocommit=True) as pool:
        private = {"conninfo": pool.conninfo, "kwargs": pool.kwargs, "notifications": notifications}
        first = await asyncio.to_thread(
            execute,
            old_python,
            script,
            {**old, **private, "phase": "seed"},
            evidence,
            "rc6-b24-" + label,
        )
        for phase in ("replay", "restart"):
            result = await asyncio.to_thread(
                execute,
                python,
                script,
                {**current, **private, "phase": phase, "prior": first},
                evidence,
                "rc6-" + phase + "-" + label,
            )
            assert result["pages"] == first["pages"]
            assert result["snapshot_sha256"] == first["snapshot_sha256"]
            assert result["checkpoint"] == first["checkpoint"]
            assert result["version_boundary"] == "REPORTING_FEED_VERSION_MISMATCH"
