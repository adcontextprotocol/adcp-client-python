#!/usr/bin/env python3
"""Refresh ``tests/fixtures/reporting-row-storage-interop-v1/`` from a live run.

Runs the same steps as the live cross-SDK test
(``tests/conformance/reporting/test_reporting_row_storage_interop_pg.py``):
the JavaScript SDK writes the golden row sets into one PostgreSQL schema and
one filesystem bucket, this SDK verifies them and writes its own, and what
each writer left behind is recorded for the replay tests.

Usage::

    ADCP_JS_SDK_DIR=/path/to/adcp-client ADCP_PG_TEST_URL=postgresql://... \\
        python scripts/regenerate_row_storage_interop_fixtures.py

Needs Node and a built adcp-client checkout (``npm ci && npm run build &&
npm run build --workspace packages/reporting-storage``). The fixtures are
written one item per line (#1491); commit the diff with the change that
motivated it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.conformance.reporting import _row_storage_interop as interop  # noqa: E402
from tests.conformance.reporting._generation_support import isolated_reporting_pool  # noqa: E402
from tests.conformance.reporting.test_reporting_row_storage_interop_pg import (  # noqa: E402
    run_js,
    setup,
)


async def regenerate(js_sdk: str, workspace: Path) -> None:
    root = workspace / "bucket"
    async with isolated_reporting_pool() as pool:
        _, storage = await setup(pool, root)
        async with pool.connection() as connection:
            schema = (await (await connection.execute("SELECT current_schema()")).fetchone())[0]
        environment = {
            "ADCP_JS_SDK_DIR": js_sdk,
            "ADCP_PG_URL": os.environ["ADCP_PG_TEST_URL"],
            "ADCP_PG_SCHEMA": schema,
            "ADCP_ROWS_ROOT": str(root),
            "ADCP_ROWS_NAMESPACE": interop.NAMESPACE,
        }
        js_out = workspace / "js-written.json"
        await run_js("write", "--out", str(js_out), environment=environment)
        js_written = json.loads(js_out.read_text())
        assert await interop.python_verify(storage, pool, js_written) == len(js_written)
        python_written = await interop.python_write(storage, pool)
        py_out = workspace / "python-written.json"
        py_out.write_text(json.dumps(python_written))
        await run_js("read", "--in", str(py_out), environment=environment)
        for name, written in (("js-written", js_written), ("python-written", python_written)):
            state = await interop.dump_state(pool, root, [item["row_set_id"] for item in written])
            path = interop.INTEROP_DIR / f"{name}.json"
            interop.write_fixture(path, written, state)
            print(f"wrote {path.relative_to(ROOT)} ({len(written)} row sets)")


def main() -> int:
    js_sdk = os.environ.get("ADCP_JS_SDK_DIR")
    if not js_sdk or not os.environ.get("ADCP_PG_TEST_URL"):
        raise SystemExit("set ADCP_JS_SDK_DIR (a built adcp-client checkout) and ADCP_PG_TEST_URL")
    with tempfile.TemporaryDirectory(prefix="adcp-row-storage-interop-") as workspace:
        asyncio.run(regenerate(js_sdk, Path(workspace)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
