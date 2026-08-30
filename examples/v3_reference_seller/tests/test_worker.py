"""Lifecycle tests for the separately supervised durable worker."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))


@pytest.mark.asyncio
async def test_stop_event_cancels_worker_before_resource_shutdown(monkeypatch) -> None:
    import src.worker as worker_module

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run_outbox() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    wiring = SimpleNamespace(
        startup=AsyncMock(),
        shutdown=AsyncMock(),
        outbox=SimpleNamespace(run_worker=run_outbox),
        workflow_queue=SimpleNamespace(),
    )
    monkeypatch.setattr(
        worker_module.DurableTaskWiring,
        "from_env",
        lambda **_kwargs: wiring,
    )
    stop_event = asyncio.Event()

    async def request_stop() -> None:
        await started.wait()
        stop_event.set()

    stopper = asyncio.create_task(request_stop())
    await worker_module.run(stop_event=stop_event)
    await stopper

    assert cancelled.is_set()
    wiring.startup.assert_awaited_once()
    wiring.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_signal_runner_installs_and_removes_sigterm_handler(monkeypatch) -> None:
    import src.worker as worker_module

    loop = asyncio.get_running_loop()
    callbacks = {}
    removed = []

    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        lambda signum, callback: callbacks.__setitem__(signum, callback),
    )
    monkeypatch.setattr(
        loop,
        "remove_signal_handler",
        lambda signum: removed.append(signum) or True,
    )

    async def fake_run(*, stop_event, workflow_handler=None) -> None:
        assert workflow_handler is None
        callbacks[signal.SIGTERM]()
        assert stop_event.is_set()

    monkeypatch.setattr(worker_module, "run", fake_run)

    await worker_module.run_with_signals()

    assert set(callbacks) == {signal.SIGTERM, signal.SIGINT}
    assert removed == [signal.SIGTERM, signal.SIGINT]
