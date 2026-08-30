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

    assert stopper.done()
    assert cancelled.is_set()
    wiring.startup.assert_awaited_once()
    wiring.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_failure_wins_when_shutdown_is_also_ready(monkeypatch) -> None:
    import src.worker as worker_module

    stop_event = asyncio.Event()

    async def fail_outbox() -> None:
        stop_event.set()
        raise RuntimeError("outbox failed")

    wiring = SimpleNamespace(
        startup=AsyncMock(),
        shutdown=AsyncMock(),
        outbox=SimpleNamespace(run_worker=fail_outbox),
        workflow_queue=SimpleNamespace(),
    )
    monkeypatch.setattr(
        worker_module.DurableTaskWiring,
        "from_env",
        lambda **_kwargs: wiring,
    )
    real_wait = asyncio.wait

    async def wait_for_all(awaitables, *, return_when):
        assert return_when is asyncio.FIRST_COMPLETED
        return await real_wait(awaitables, return_when=asyncio.ALL_COMPLETED)

    monkeypatch.setattr(worker_module.asyncio, "wait", wait_for_all)

    with pytest.raises(RuntimeError, match="outbox failed"):
        await worker_module.run(stop_event=stop_event)

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
