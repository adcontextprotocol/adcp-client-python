"""Eight historical receipt installations before the ownership maintenance window."""

import json
import shutil
from pathlib import Path

import pytest

from ._generation_support import isolated_reporting_pool
from ._ownership_upgrade_support import ownership_parent
from .test_reporting_materializer_rolling import ARTIFACTS, build_frozen, frozen_call

B21 = "3fd62121c96a074e3ea458c30c5224d6a586f169"
__all__ = ["ownership_parent"]


def receipt_probe(artifact):
    root, python, _, settings = artifact
    script = root / "receipt_frozen.py"
    shutil.copy2(Path(__file__).with_name("_receipt_frozen.py"), script)
    return root, python, script, settings


@pytest.fixture(scope="module")
def installed_b21(tmp_path_factory, request):
    return receipt_probe(build_frozen("b21", tmp_path_factory, request, sha=B21))


@pytest.fixture(scope="module", params=(*ARTIFACTS, "b21"))
def installed_receipt_history(request, tmp_path_factory, installed_b21):
    if request.param == "b21":
        return installed_b21
    return receipt_probe(build_frozen(request.param, tmp_path_factory, request))


async def test_actual_old_readers_and_writers_before_and_after_receipt_migration(
    installed_receipt_history, ownership_parent
):
    from ._ownership_upgrade_support import maintenance_upgrade, seed_legacy

    artifact = installed_receipt_history[3]["artifact"]
    for notifications in [False, True] if artifact in {"b", "c", "b1", "b21"} else [False]:
        async with isolated_reporting_pool(autocommit=True) as pool:
            legacy = await seed_legacy(
                ownership_parent,
                pool,
                kind="receipt",
                notifications=notifications,
                consumer=(
                    "frozen-buyer"
                    if artifact in {"beta15", "records", "integration", "a", "b"}
                    else "https://buyer.example.test/historical"
                ),
                legacy_definition=artifact == "beta15",
            )
            before = await frozen_call(
                installed_receipt_history,
                pool,
                "exercise",
                phase="before",
                receipt_count=2,
                account=legacy["account"],
                consumer=legacy["consumer"],
                obligation=legacy["obligation"],
                notifications=notifications,
            )
            assert before["ordinary_core"]
            assert before["ordinary_materializer"] == (artifact != "beta15")
            upgrade = await maintenance_upgrade(pool, legacy, notifications=notifications)
            print(
                json.dumps(
                    {
                        "receipt_maintenance": artifact,
                        "notifications": notifications,
                        "before": before,
                        "upgrade": upgrade,
                    }
                ),
                flush=True,
            )
