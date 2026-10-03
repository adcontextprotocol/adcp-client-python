"""Nine historical binaries before the stopped-worker caller-ownership upgrade."""

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ._generation_support import isolated_reporting_pool
from ._ownership_upgrade_support import ownership_parent
from .test_reporting_feed_packaging import ROOT, run_step
from .test_reporting_materializer_rolling import ARTIFACTS, build_frozen, frozen_call
from .test_reporting_receipt_rolling import B21, receipt_probe

B22 = "09fd87f79a746665d828dea66b3a1dd9d1fc189e"
B22_TREE = "76c64bb94d8b92faed644158317bb5372b64fcb9"
__all__ = ["ownership_parent"]


@pytest.fixture(scope="module")
def approved_feed_b21(tmp_path_factory, request):
    return receipt_probe(build_frozen("b21", tmp_path_factory, request, sha=B21))


@pytest.fixture(scope="module")
def approved_feed_b22(tmp_path_factory, request):
    root, python, ordinary, original = receipt_probe(
        build_frozen("b22", tmp_path_factory, request, sha=B22)
    )
    modules = dict(original["modules"])
    for relative in (
        "reporting/receipts/pg.py",
        "reporting/receipts/handler.py",
        "reporting/receipts/wire.py",
        "reporting/receipts/transport.py",
        "server/mcp_tools.py",
        "server/a2a_server.py",
        "server/serve.py",
    ):
        content = run_step(
            ["git", "show", f"{B22}:src/adcp/{relative}"], label="b22-exact-module", cwd=ROOT
        )
        modules["adcp." + relative.removesuffix(".py").replace("/", ".")] = hashlib.sha256(
            content.encode()
        ).hexdigest()
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step([*installer, "asgi-lifespan==2.1.0"], label="b22-mounted-probe-dependency", cwd=root)
    shutil.copy2(Path(__file__).with_name("_feed_process.py"), root / "feed_process.py")
    shutil.copy2(Path(__file__).with_name("_receipt_transport.py"), root / "receipt_transport.py")
    return root, python, ordinary, {**original, "modules": modules, "tree": B22_TREE}


@pytest.fixture(scope="module", params=(*ARTIFACTS, "b21", "b22"))
def installed_feed_history(request, tmp_path_factory, approved_feed_b21, approved_feed_b22):
    if request.param == "b21":
        return approved_feed_b21
    if request.param == "b22":
        return approved_feed_b22
    return receipt_probe(build_frozen(request.param, tmp_path_factory, request))


async def b22_mounted(artifact, pool, action, **kwargs):
    root, python, _, settings = artifact
    value = {
        "installed": settings,
        "receipt_only": True,
        "conninfo": pool.conninfo,
        "kwargs": pool.kwargs,
        "action": action,
        "helper": str(root / "receipt_transport.py"),
        **kwargs,
    }
    result = json.loads(
        await asyncio.to_thread(
            run_step,
            [str(python), "-I", str(root / "feed_process.py")],
            label=f"frozen-b22-mounted-{action}",
            cwd=root,
            value=value,
            timeout=90,
        )
    )
    assert result["point"] == "done", result
    return result


async def test_nine_actual_artifacts_preserve_ordinary_writes_and_frozen_b22_mounted_replay(
    installed_feed_history, ownership_parent, approved_feed_b22
):
    """Old workers run before maintenance; all retained bytes then fail closed."""
    from ._ownership_upgrade_support import maintenance_upgrade, seed_legacy

    artifact = installed_feed_history[3]["artifact"]
    modes = [False, True] if artifact in {"b", "c", "b1", "b21", "b22"} else [False]
    for notifications in modes:
        async with isolated_reporting_pool(autocommit=True) as pool:
            legacy = await seed_legacy(
                ownership_parent,
                pool,
                kind="feed",
                notifications=notifications,
                consumer=(
                    "frozen-buyer"
                    if artifact in {"beta15", "records", "integration", "a", "b"}
                    else "https://buyer.example.test/frozen-feed"
                ),
                legacy_definition=artifact == "beta15",
            )
            before = await frozen_call(
                installed_feed_history,
                pool,
                "exercise",
                phase="before",
                account=legacy["account"],
                consumer=legacy["consumer"],
                obligation=legacy["obligation"],
                notifications=notifications,
                receipt_count=2,
            )
            assert before["ordinary_core"]
            assert before["ordinary_materializer"] == (artifact != "beta15")
            replayed = await b22_mounted(
                approved_feed_b22,
                pool,
                "receipt",
                notifications=notifications,
                caller={"account_id": legacy["account"], "consumer_id": legacy["consumer"]},
                request=legacy["request"],
            )
            assert replayed["result"] == legacy["response"]
            upgrade = await maintenance_upgrade(pool, legacy, notifications=notifications)
            print(
                json.dumps(
                    {
                        "feed_maintenance": artifact,
                        "notifications": notifications,
                        "before": before,
                        "upgrade": upgrade,
                    }
                ),
                flush=True,
            )
