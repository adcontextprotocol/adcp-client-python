"""Fresh process: rebuild the graph, then let the actual indexed worker recover."""

import asyncio
import json
import os
import sys
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from ._production_context_source import DurableBindingSource
from ._production_support import production_harness
from .test_reliable_reporting_production_admission import registry_factory, service_factory
from .test_reporting_production_bindings import source_documents


async def main():
    selected = {"context_change": {"adapter": "resolver-must-not-run"}}
    async with AsyncConnectionPool(
        os.environ["ADCP_PG_TEST_URL"],
        open=False,
        min_size=1,
        max_size=1,
        kwargs={
            "autocommit": True,
            "options": "-csearch_path=" + os.environ["ADCP_CONTEXT_TEST_SCHEMA"],
        },
    ) as pool:
        await pool.wait()
        async with production_harness(
            "postgres",
            Path(sys.argv[1]),
            count=0,
            source_publication=True,
            existing_pool=pool,
            source_factory=DurableBindingSource,
            source_registry_factory=registry_factory(selected),
            service_factory=service_factory,
        ) as h:
            requests = h.production.offerings[0].producer._source.requests
            records = await source_documents(h)
            print(
                json.dumps(
                    {
                        "resolved": len(selected.get("resolved", ())),
                        "local_bindings": len(h.service._bindings),
                        "requests": [r.identity.account_id for r in requests],
                        "contexts": [r[3]["service_context_sha256"] for r in records],
                        "ready": h.service.ready,
                    }
                )
            )


asyncio.run(main())
