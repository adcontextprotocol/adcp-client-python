"""Actual installed/source transport process; durable snapshot crash boundaries."""

import asyncio
import hashlib
import importlib
import importlib.util
import json
import sys
import traceback
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace


async def main(settings):
    from psycopg_pool import AsyncConnectionPool

    from adcp.reporting.ledger import ReportingDeliveryPrincipal
    from adcp.reporting.receipts import PgReportingReceiptStore, ReportingReceiptHandler

    installed = settings.get("installed")
    origins = {}
    if installed is not None:
        workspace = Path(installed["workspace"]).resolve()
        if "python" in installed:
            assert list(sys.version_info[:2]) == installed["python"]
        assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
        for name, expected in installed["modules"].items():
            path = Path(importlib.import_module(name).__file__).resolve()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
            assert "site-packages" in str(path) and not path.is_relative_to(workspace)
            origins[name] = str(path)

    async def pause(point, **evidence):
        if settings.get("pause") == point:
            print(json.dumps({"point": point, **evidence}), flush=True)
            assert json.loads(await asyncio.to_thread(sys.stdin.readline))["continue"]

    receipt_only = settings.get("receipt_only", False)
    if receipt_only:
        store_type = PgReportingReceiptStore
    else:
        from adcp.reporting.feed import PgReportingFeedStore

        class Store(PgReportingFeedStore):
            async def _capture_feed_on(self, connection, *args, **kwargs):
                captured = await super()._capture_feed_on(connection, *args, **kwargs)
                await pause("captured")
                return captured

            async def _save_feed_snapshot_on(self, connection, stored):
                await super()._save_feed_snapshot_on(connection, stored)
                await pause("inserted", snapshot_id=stored.snapshot.snapshot_id)

        store_type = Store

    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=1, open=False
    ) as pool:
        store = store_type(pool=pool, notifications=settings["notifications"])
        if settings["action"] == "install":
            await store.create_schema()
            await store.create_schema()
            assert await store.receipt_ingestion_ready()
            if not receipt_only:
                assert await store.reporting_feed_ready()
            if settings.get("legacy_status_schema"):
                from adcp.reporting.ledger import PgReportingReconciliationStore
                from adcp.reporting.outbox import PgStatusNotificationStore

                await PgStatusNotificationStore(
                    PgReportingReconciliationStore(pool=pool, notifications=True)
                ).create_schema()
            result = {
                "installed": True,
                "materializer_objects": len(
                    json.loads(
                        files("adcp.reporting.materializer")
                        .joinpath("required_schema.json")
                        .read_text()
                    )
                ),
                "receipt_objects": len(
                    json.loads(
                        files("adcp.reporting.receipts")
                        .joinpath("required_schema.json")
                        .read_text()
                    )
                ),
            }
            if not receipt_only:
                result["feed_objects"] = len(
                    json.loads(
                        files("adcp.reporting.feed").joinpath("required_schema.json").read_text()
                    )
                )
        else:
            spec = importlib.util.spec_from_file_location("frozen_transport", settings["helper"])
            transport = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(transport)
            mount = transport.MountedReceipts(SimpleNamespace(store=store), hydrated=True)
            caller = ReportingDeliveryPrincipal(**settings["caller"])
            subject = SimpleNamespace(
                obligation=SimpleNamespace(account_id=caller.account_id),
                binding=SimpleNamespace(consumer_id=caller.consumer_id),
            )
            if not receipt_only:
                mount.handler = ReportingReceiptHandler(
                    store,
                    resolve_account=mount.resolve_account,
                    consumer_status_enabled=settings.get("feedback", False),
                )
                # Preserve the fixture's public pin after replacing its handler.
                # This also works with the actual historical SDK constructors.
                mount.handler.get_adcp_version = lambda: mount.version
            mount.authorize(subject)
            async with mount.client() as client:
                _, inventory = await mount.mcp(client, inventory=True)
                expected = {"get_adcp_capabilities", "sync_reporting_receipts"}
                if not receipt_only:
                    expected.add("get_reporting_status")
                assert {tool["name"] for tool in inventory["tools"]} == expected
                for path in ("/.well-known/agent.json", "/.well-known/agent-card.json"):
                    assert {s["id"] for s in (await client.get(path)).json()["skills"]} == expected
                if settings["action"] == "receipt":
                    _, response = await mount.mcp(client, settings["request"])
                    assert response["results"]
                    for v1 in (False, True):
                        assert (await mount.a2a(client, settings["request"], v1=v1))[1] == response
                    mount.grants.clear()
                    for call in (mount.mcp, mount.a2a):
                        assert (
                            transport.error_code((await call(client, settings["request"]))[1])
                            == "UNAUTHORIZED"
                        )
                    result = response
                else:

                    def reporting(wire):
                        return wire.replace(
                            '"name": "sync_reporting_receipts"', '"name": "get_reporting_status"'
                        ).replace(
                            '"skill": "sync_reporting_receipts"', '"skill": "get_reporting_status"'
                        )

                    request = dict(settings["request"])
                    pages = []
                    while True:
                        if settings.get("transport", "mcp") == "mcp":
                            _, page = await mount.mcp(client, request, mutate_wire=reporting)
                        else:
                            _, page = await mount.a2a(
                                client, request, mutate_wire=reporting, v1=settings.get("v1", False)
                            )
                        assert "pagination" in page, page
                        pages.append(page)
                        await pause("committed", result=page)
                        if settings["action"] != "walk" or not page["pagination"]["has_more"]:
                            break
                        assert len(pages) < 1000
                        request["pagination"] = {
                            **request.get("pagination", {}),
                            "cursor": page["pagination"]["cursor"],
                        }
                    snapshot = await store.read_reporting_feed_snapshot(
                        pages[0]["ledger_snapshot_id"], caller=caller
                    )
                    mount.grants.clear()
                    assert (
                        transport.error_code(
                            (await mount.a2a(client, request, mutate_wire=reporting))[1]
                        )
                        == "UNAUTHORIZED"
                    )
                    result = {
                        "pages": pages,
                        "binding": snapshot.binding,
                        "version": snapshot.representation_version,
                        "ownership_mode": snapshot.ownership_mode,
                    }
    if installed is not None:
        for name, module in tuple(sys.modules.items()):
            if (name == "adcp" or name.startswith("adcp.")) and getattr(module, "__file__", None):
                assert "site-packages" in module.__file__
                assert not Path(module.__file__).resolve().is_relative_to(workspace)
    return {"point": "done", "result": result, "origins": origins}


if __name__ == "__main__":
    try:
        result = asyncio.run(main(json.loads(sys.stdin.readline())))
    except Exception as error:
        result = {
            "point": "failed",
            "failure": type(error).__name__,
            "frames": [
                [Path(frame.filename).name, frame.lineno]
                for frame in traceback.extract_tb(error.__traceback__)
            ],
        }
    print(json.dumps(result), flush=True)
