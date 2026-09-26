"""Independent pool-of-one processes used by the inline storage contract tests."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import InlineReportingSource
from adcp.reporting.inline_storage import PgReportingSealStore, PgReportingStagingStore


async def run(mode: str, schema: str, output: Path, counter: Path, value: int) -> None:
    async with AsyncConnectionPool(
        os.environ["ADCP_INLINE_TEST_DSN"],
        kwargs={"options": f"-c search_path={schema}"},
        min_size=1,
        max_size=1,
        open=False,
    ) as pool:
        staging = PgReportingStagingStore(pool=pool)
        seals = PgReportingSealStore(pool=pool)
        if mode == "create":
            await staging.create_schema()
            output.write_text("{}")
            return
        if mode == "stage":
            reference = await staging.stage(
                account_id="shared-account",
                source_execution_key=f"execution-{value}",
                ordinal=value,
                payload=b"same-content",
            )
            output.write_text(json.dumps(reference))
            return

        async def fetch(_request: object) -> list[dict[str, object]]:
            if mode == "replay":
                raise AssertionError("a committed seal must replay without dispatch")
            with counter.open("a") as stream:
                stream.write("dispatch\n")
            # Borrowing the sole connection inside the provider proves no
            # storage transaction is held across the external source call.
            async with pool.connection() as connection:
                await connection.execute("SELECT 1")
            # Both processes must reach dispatch before either may publish;
            # they offer different valid manifests for the same execution key.
            for _ in range(3000):
                if counter.read_text().count("dispatch\n") == 2:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("the other source process never reached dispatch")
            return [
                {
                    "media_buy_id": "media-buy-redacted",
                    "campaign_id": "campaign-redacted-1",
                    "impressions": value,
                    "spend": "1.25",
                }
            ]

        source = InlineReportingSource(
            capabilities=redacted_capabilities(),
            fetch=fetch,
            staging=staging,
            seals=seals,
            clock=lambda: datetime(2026, 11, 6, 12, tzinfo=timezone.utc),
        )
        request = redacted_snapshot_request()
        result = await source.execute(request, cancel=asyncio.Event())
        assert result.manifest_bytes is not None
        from adcp.reporting.conformance import validate_reporting_source_execution

        manifest = await validate_reporting_source_execution(
            capabilities=source.capabilities,
            request=request,
            result=result,
            object_reader=staging,
        )
        obj = manifest.objects[0]
        payload = await staging.read(
            object_ref=obj.object_ref,
            object_generation=obj.object_generation,
            account_id=request.identity.account_id,
            source_scope={},
            cancel=asyncio.Event(),
        )
        output.write_text(
            json.dumps(
                {
                    "manifest": base64.b64encode(result.manifest_bytes).decode(),
                    "payload": base64.b64encode(payload).decode(),
                    "reference": result.response.manifest.model_dump(),
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["create", "stage", "publish", "replay"])
    parser.add_argument("schema")
    parser.add_argument("output", type=Path)
    parser.add_argument("counter", type=Path)
    parser.add_argument("--value", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(run(args.mode, args.schema, args.output, args.counter, args.value))
