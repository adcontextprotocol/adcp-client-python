"""Separately supervised durable task-webhook worker.

Run beside the web process with the same ``ADCP_TASK_*`` and
``ADCP_WEBHOOK_SIGNING_*`` environment variables::

    python -m src.worker
"""

from __future__ import annotations

import asyncio
import logging

from .durable_tasks import DurableTaskWiring

logger = logging.getLogger(__name__)


async def run() -> None:
    wiring = DurableTaskWiring.from_env(required=True, include_idempotency=False)
    assert wiring is not None  # required=True makes None impossible
    await wiring.startup()
    logger.info("Task-webhook worker started")
    try:
        await wiring.outbox.run_worker()
    finally:
        await wiring.shutdown()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()


__all__ = ["main", "run"]
