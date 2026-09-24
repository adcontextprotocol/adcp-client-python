"""Run outside the checkout on Python 3.10 without the optional PG drivers."""

import asyncio
import hashlib
import importlib
import importlib.util
import json
import sys
from importlib.resources import files
from pathlib import Path


async def main(settings):
    assert sys.version_info[:2] == (3, 10)
    assert importlib.util.find_spec("psycopg") is None
    assert importlib.util.find_spec("psycopg_pool") is None
    import adcp.reporting.feed as feed
    from adcp.reporting.ledger import ReportingDeliveryPrincipal
    from adcp.reporting.receipts import InMemoryReportingReceiptStore, ReportingReceiptHandler
    from adcp.server import ADCPHandler
    from adcp.server.mcp_tools import get_tools_for_handler
    from adcp.types import GetReportingStatusRequest, GetReportingStatusResponse

    assert "adcp.reporting.feed.pg" not in sys.modules
    for name in feed.__all__:
        assert getattr(feed, name) is not None
    assert "psycopg" not in sys.modules and "psycopg_pool" not in sys.modules
    for relative, digest in settings["assets"].items():
        raw = files("adcp.reporting").joinpath(relative).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
    prefix = "https://buyer.example.test/"
    caller = ReportingDeliveryPrincipal("installed-account", prefix + "a" * (2048 - len(prefix)))
    store = feed.InMemoryReportingFeedStore()
    request = GetReportingStatusRequest.model_validate(
        {"account": {"account_id": caller.account_id}, "view": "periods"}
    ).model_dump(mode="json", exclude_unset=True)
    page = GetReportingStatusResponse.model_validate(
        await store.read_reporting_feed(request, caller=caller)
    )
    assert page.pagination.total_count == 0 and page.pagination.has_more is False
    assert 0 < len(page.changes_checkpoint) <= 2048

    async def resolve(reference, context, consumer):
        assert consumer == caller.consumer_id
        return caller.account_id

    old = ReportingReceiptHandler(InMemoryReportingReceiptStore(), resolve_account=resolve)
    new = ReportingReceiptHandler(store, resolve_account=resolve)
    feed_condition_1 = await old.get_reporting_status(
        request
    ) == await ADCPHandler().get_reporting_status(request)
    assert feed_condition_1
    assert {t["name"] for t in get_tools_for_handler(old)} == {
        "get_adcp_capabilities",
        "sync_reporting_receipts",
    }
    assert "get_reporting_status" in {t["name"] for t in get_tools_for_handler(new)}
    workspace = Path(settings["workspace"]).resolve()
    assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
    origins = {}
    for name in settings["modules"]:
        path = Path(importlib.import_module(name).__file__).resolve()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == settings["modules"][name]
        assert "site-packages" in str(path) and not path.is_relative_to(workspace)
        origins[name] = str(path)
    for name, module in tuple(sys.modules.items()):
        if (name == "adcp" or name.startswith("adcp.")) and getattr(module, "__file__", None):
            assert "site-packages" in module.__file__
            assert not Path(module.__file__).resolve().is_relative_to(workspace)
    return {
        "python": "3.10",
        "driver_absent": True,
        "origins": origins,
        "token_length": len(page.changes_checkpoint),
        "assets": settings["assets"],
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(json.load(sys.stdin)))))
