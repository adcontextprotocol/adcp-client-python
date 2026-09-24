"""Installed Python 3.10 SQL and authenticated HTTP replay, run outside the checkout."""

import asyncio
import hashlib
import importlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace


async def main(settings):
    from psycopg_pool import AsyncConnectionPool

    from adcp.reporting.receipts import PgReportingReceiptStore, ReportingReceiptError

    installed = settings["installed"]
    workspace = Path(installed["workspace"]).resolve()
    assert list(sys.version_info[:2]) == installed["python"]
    origins = {}
    for name, expected in installed["modules"].items():
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        origins[name] = str(path)
    assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=1, open=False
    ) as pool:
        store = PgReportingReceiptStore(pool=pool, notifications=settings["notifications"])
        if settings["action"] == "install":
            try:
                await store.receipt_ingestion_ready()
            except ReportingReceiptError as error:
                assert error.code == "RECEIPT_SCHEMA_UNREADY"
            else:
                raise AssertionError("empty schema must fail closed")
            await store.create_schema()
            await store.create_schema()
            assert await store.receipt_ingestion_ready()
            result = {"installed": True}
        else:
            spec = importlib.util.spec_from_file_location("installed_transport", settings["helper"])
            transport = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(transport)
            mount = transport.MountedReceipts(SimpleNamespace(store=store), hydrated=True)
            caller = settings["caller"]
            scenario = SimpleNamespace(
                obligation=SimpleNamespace(account_id=caller["account_id"]),
                binding=SimpleNamespace(consumer_id=caller["consumer_id"]),
            )
            mount.authorize(scenario)
            async with mount.client() as client:
                _, result = await mount.mcp(client, settings["request"])
                assert [r["result"] for r in result["results"]] == ["recorded", "recorded"]
                receipt_operation_1 = await mount.a2a(client, settings["request"])
                assert (receipt_operation_1)[1] == result
                mount.grants.clear()
                for call in (mount.mcp, mount.a2a):
                    _, denied = await call(client, settings["request"])
                    assert transport.error_code(denied) == "UNAUTHORIZED"
                assert len(mount.auth_calls) == 4
    for name, module in tuple(sys.modules.items()):
        if (name == "adcp" or name.startswith("adcp.")) and getattr(module, "__file__", None):
            assert "site-packages" in module.__file__
            assert not Path(module.__file__).resolve().is_relative_to(workspace)
    return {"result": result, "origins": origins, "python": installed["python"]}


if __name__ == "__main__":
    try:
        result = asyncio.run(main(json.load(sys.stdin)))
    except Exception as error:
        result = {
            "failure": type(error).__name__,
            "frames": [
                [Path(frame.filename).name, frame.lineno]
                for frame in traceback.extract_tb(error.__traceback__)
            ],
        }
    print(json.dumps(result))
