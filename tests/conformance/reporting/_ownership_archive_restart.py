"""Installed floor process proves an archived lease cannot become runnable."""

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


async def main(settings):
    from psycopg import sql
    from psycopg_pool import AsyncConnectionPool
    from pydantic import TypeAdapter

    import adcp
    from adcp.reporting.feed import PgReportingFeedStore
    from adcp.reporting.materializer import ReportingVerificationKey
    from adcp.reporting.materializer.work import ReportingMaterializerLease

    assert list(sys.version_info[:2]) == [3, 10]
    assert Path(adcp.__file__).resolve().is_relative_to(Path(sys.prefix))
    spec = importlib.util.spec_from_file_location(
        "ownership_upgrade", Path(__file__).with_name("ownership_upgrade.py")
    )
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=3, open=False
    ) as pool:
        store = PgReportingFeedStore(pool=pool, notifications=settings["notifications"])
        await store.create_schema()
        key = TypeAdapter(ReportingVerificationKey).validate_python(
            settings["legacy"]["pending"]["verification_key"]
        )
        for _ in range(2):
            assert not isinstance(
                await store.claim_materialization(keys=(key,), lease_seconds=30),
                ReportingMaterializerLease,
            )
        async with pool.connection() as connection:
            retained = await helper.image(connection, settings["archive"])
            digest = hashlib.sha256(
                json.dumps(retained, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            assert digest == settings["archive_image_sha256"]
            row = await (
                await connection.execute(
                    sql.SQL(
                        "SELECT reporting_materialization_id,external_id FROM {}."
                        "reporting_materializer_work WHERE state='pending'"
                    ).format(sql.Identifier(settings["archive"]))
                )
            ).fetchone()
            expected = settings["legacy"]["pending"]
            assert row == (
                expected["attempt"]["reporting_materialization_id"],
                expected["external_id"],
            )
            active = (
                await (
                    await connection.execute(
                        "SELECT count(*) FROM reporting_materializer_work WHERE state='pending'"
                    )
                ).fetchone()
            )[0]
            assert active == 0
        return {
            "pending_materialization_id": row[0],
            "pending_external_id": row[1],
            "active_pending": active,
            "archive_unchanged": True,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(json.load(sys.stdin)))), flush=True)
