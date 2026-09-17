"""Real receipt worker. Pause while holding an ordinal transaction, then SIGKILL."""

import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path


async def main():
    settings = json.loads(await asyncio.to_thread(sys.stdin.readline))
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    from adcp.reporting.ledger import ReportingDeliveryPrincipal
    from adcp.reporting.receipts import PgReportingReceiptStore

    origins = {}
    if settings.get("installed"):
        installed = settings["installed"]
        workspace = Path(installed["workspace"]).resolve()
        assert list(sys.version_info[:2]) == installed["python"]
        for name, expected in installed["modules"].items():
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
            origins[name] = str(path)
        assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
        for name, module in tuple(sys.modules.items()):
            if (name == "adcp" or name.startswith("adcp.")) and getattr(module, "__file__", None):
                assert "site-packages" in module.__file__
                assert not Path(module.__file__).resolve().is_relative_to(workspace)

    async def pause(point):
        if settings.get("pause") == point:
            print(json.dumps({"point": point}), flush=True)
            assert json.loads(await asyncio.to_thread(sys.stdin.readline))["continue"]

    class Connection(AsyncConnection):
        async def execute(self, query, params=None, **kwargs):
            result = await super().execute(query, params, **kwargs)
            if isinstance(query, str):
                for prefix, point in (
                    ("INSERT INTO reporting_receipt_ingestion_batches", "header"),
                    ("INSERT INTO reporting_reconciliation_records", "receipt"),
                    ("INSERT INTO reporting_reconciliation_changes", "feed"),
                    ("INSERT INTO reporting_receipt_ingestion_boundaries", "capture"),
                    ("INSERT INTO reporting_receipt_ingestion_results", "ordinal"),
                    ("UPDATE reporting_receipt_ingestion_batches", "final"),
                ):
                    if query.startswith(prefix):
                        await pause(point)
            return result

    class Store(PgReportingReceiptStore):
        async def _receipt_results_on(self, connection, caller, batch):
            result = await super()._receipt_results_on(connection, caller, batch)
            if len(result) == 1:
                await pause("between_ordinals")
            return result

    async with AsyncConnectionPool(
        settings["conninfo"],
        kwargs=settings["kwargs"],
        min_size=1,
        max_size=1,
        connection_class=Connection,
        open=False,
    ) as pool:
        store = Store(pool=pool, notifications=settings["notifications"])
        result = await store.ingest_receipt_batch(
            settings["request"], caller=ReportingDeliveryPrincipal(**settings["caller"])
        )
        await pause("committed")
        print(json.dumps({"point": "done", "result": result, "origins": origins}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
