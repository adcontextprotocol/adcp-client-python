"""Real database/process restart of first-attempt windows in all three queues."""

import asyncio
import hashlib
import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import adcp.reporting.production.delivery_window as window_module

from .test_reporting_materializer_process import Child
from .test_reporting_production_notifications import (
    pin_current_clock,
    queued_production,
    retained_windows,
)


@asynccontextmanager
async def delivery_child(h, path, *, queue, pause, at):
    origin = Path(window_module.__file__).resolve()
    log = path / ("accepted-crash.log" if pause else "cold-retry.log")
    with log.open("wb") as diagnostic:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.conformance.reporting._production_delivery_process",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=diagnostic,
        )
        child = Child(process)
        try:
            await child.send(
                {
                    "conninfo": h.pool.conninfo,
                    "kwargs": h.pool.kwargs,
                    "queue": queue,
                    "pause": pause,
                    "at": at.isoformat(),
                    "consumer": h.item.binding.consumer_id,
                    "installed": "site-packages" in str(origin),
                    "module_sha256": hashlib.sha256(origin.read_bytes()).hexdigest(),
                }
            )
            yield child
        finally:
            await child.kill()
    print(
        json.dumps(
            {
                "production_delivery_process_log": str(log),
                "bytes": log.stat().st_size,
                "sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            }
        ),
        flush=True,
    )


@pytest.mark.parametrize("queue", [0, 1, 2], ids=["core", "status-v2", "ready-v2"])
@pytest.mark.parametrize("offset", [-1, 0], ids=["before", "exact"])
async def test_pg_sigkill_after_accepted_http_keeps_first_retry_window(
    queue, offset, tmp_path, monkeypatch
):
    async with queued_production("postgres", tmp_path, monkeypatch) as h:
        await pin_current_clock(h)
        # This process receives exactly this registration. Multi-recipient
        # expansion/rollback is exercised separately; retaining registrations
        # unknown to this child would suppress a different first delivery.
        for key in tuple(h.subscriptions.values):
            if key[0] == "acct_a" and key[1] != "buyer":
                del h.subscriptions.values[key]
        production_operation_1 = await h.workers[queue].expand_one(account_id="acct_a")
        assert production_operation_1
        async with delivery_child(h, tmp_path, queue=queue, pause=True, at=h.clock()) as child:
            first = await child.event("accepted_before_ack")
            assert len(await retained_windows(h)) == 1
            await child.kill()
            assert child.process.returncode == -9
        saved = await retained_windows(h)
        at = datetime.fromisoformat(first["expires_at"]) + timedelta(microseconds=offset)
        async with delivery_child(h, tmp_path, queue=queue, pause=False, at=at) as child:
            restored = await child.event("done")
            production_operation_2 = await asyncio.wait_for(child.process.wait(), 10)
            assert production_operation_2 == 0
        assert restored["http_calls"] == int(offset < 0)
        assert restored["activity_count"] == (2 if offset < 0 else 1)
        assert (restored["state"], restored["error_code"]) == (
            ("complete", None) if offset < 0 else ("suppressed", "lease_expired")
        )
        for key in ("key_sha256", "body_sha256", "started_at", "expires_at", "origin"):
            assert restored[key] == first[key]
        assert await retained_windows(h) == saved
        print(
            json.dumps(
                {
                    "production_delivery_cold_restart": {
                        "queue": queue,
                        "offset_microseconds": offset,
                        **restored,
                    }
                }
            ),
            flush=True,
        )
