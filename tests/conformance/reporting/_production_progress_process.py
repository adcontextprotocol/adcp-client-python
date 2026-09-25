"""Cold producer restart: committed bounded window, retained lease, then SIGKILL."""

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path


async def main():
    from psycopg_pool import AsyncConnectionPool

    from ._production_support import production_harness
    from .test_reporting_production_lock_order import source_turn
    from .test_reporting_production_progress import turn_document

    settings = json.loads(await asyncio.to_thread(sys.stdin.readline))
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=2, max_size=4, open=False
    ) as pool:
        async with production_harness(
            "postgres", Path(settings["path"]), count=0, periods=131, existing_pool=pool
        ) as h:
            support = h.production
            producer = support.offerings[0].producer
            source = producer._source
            h.source_clock.advance(timedelta(hours=131, seconds=0 if settings["pause"] else 61))
            if settings["pause"]:
                original = producer._acquire_pending

                async def pause(configuration, turn, *, now):
                    await original(configuration, turn, now=now)
                    print(
                        json.dumps(
                            {"point": "committed_before_release", **turn_document(turn, source)}
                        ),
                        flush=True,
                    )
                    # The parent observes the real committed cursor and live
                    # configuration lease, then kills this process at this edge.
                    await asyncio.to_thread(sys.stdin.readline)
                    raise AssertionError("paused child must be killed")

                producer._acquire_pending = pause
            turn = await source_turn(support)
            result = turn_document(turn, source)
            repeated = await source_turn(support)
            assert not repeated.obligations_committed and not repeated.revisions_committed
            assert result["executions"] == [
                r.identity.source_execution_key for r in source.requests
            ]
            await support.aclose()
            print(json.dumps({"point": "done", **result}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
