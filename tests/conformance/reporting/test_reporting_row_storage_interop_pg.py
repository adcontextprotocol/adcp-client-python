"""JS-written chunks verify in Python and Python-written chunks verify in JS.

The acceptance gate for persistence Stage 2: both SDKs against **one PostgreSQL schema
and one bucket**, in both directions.

* The live test runs the real ``@adcp/sdk`` row storage through
  ``tests/interop/reporting_row_storage_js_driver.cjs``. It needs Node and a built
  adcp-client checkout::

      ADCP_JS_SDK_DIR=/path/to/adcp-client ADCP_PG_TEST_URL=postgresql://... \\
          pytest tests/conformance/reporting/test_reporting_row_storage_interop_pg.py

  Set ``ADCP_UPDATE_INTEROP_FIXTURES=1`` to refresh the committed fixtures from a live run.
* The replay tests always run. They load the rows each SDK wrote, as recorded in
  ``tests/fixtures/reporting-row-storage-interop-v1/``, into a fresh database and bucket
  and verify them through this SDK, so Python CI needs no Node.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore
from adcp.reporting.ledger.row_storage import ReportingRowStorageV1

from . import _row_storage_interop as interop
from ._generation_support import isolated_reporting_pool

JS_SDK = os.environ.get("ADCP_JS_SDK_DIR")
#: Set by the CI interop lane: a missing build or environment is a failure, never a skip.
REQUIRED = os.environ.get("ADCP_REQUIRE_JS_INTEROP") == "1"
DRIVER = Path(__file__).resolve().parents[2] / "interop" / "reporting_row_storage_js_driver.cjs"


async def run_js(mode: str, *flags: str, environment: dict[str, str]) -> str:
    process = await asyncio.create_subprocess_exec(
        shutil.which("node") or "node",
        str(DRIVER),
        mode,
        *flags,
        env={**os.environ, **environment},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=240)
    assert process.returncode == 0, stderr.decode()[-2000:]
    return stdout.decode()


async def setup(pool: Any, root: Path) -> tuple[PgReportingLedgerStore, ReportingRowStorageV1]:
    store = PgReportingLedgerStore(pool=pool, row_storage=interop.storage_options(root))
    await store.create_schema()
    assert store._row_storage is not None
    return store, store._row_storage


@pytest.mark.skipif(
    not REQUIRED and (not JS_SDK or not shutil.which("node")),
    reason="set ADCP_JS_SDK_DIR to a built adcp-client checkout and install Node",
)
async def test_one_database_and_one_bucket_in_both_directions(tmp_path: Path) -> None:
    if REQUIRED:
        assert JS_SDK, "ADCP_REQUIRE_JS_INTEROP is set but ADCP_JS_SDK_DIR is not"
        assert shutil.which("node"), "ADCP_REQUIRE_JS_INTEROP is set but node is not installed"
        assert (
            Path(JS_SDK) / "dist/lib/reporting/ledger/index.js"
        ).is_file(), "ADCP_REQUIRE_JS_INTEROP is set but the JavaScript SDK is not built"
    root = tmp_path / "bucket"
    async with isolated_reporting_pool() as pool:
        _, storage = await setup(pool, root)
        async with pool.connection() as connection:
            schema = (await (await connection.execute("SELECT current_schema()")).fetchone())[0]
        environment = {
            "ADCP_JS_SDK_DIR": str(JS_SDK),
            "ADCP_PG_URL": os.environ["ADCP_PG_TEST_URL"],
            "ADCP_PG_SCHEMA": schema,
            "ADCP_ROWS_ROOT": str(root),
            "ADCP_ROWS_NAMESPACE": interop.NAMESPACE,
        }
        js_out = tmp_path / "js-written.json"
        await run_js("write", "--out", str(js_out), environment=environment)
        js_written = json.loads(js_out.read_text())
        assert len(js_written) == len(interop.golden_vectors()) * len(interop.SET_KINDS)

        # JS-written chunks, in the shared tables and the shared bucket, verify in Python.
        assert await interop.python_verify(storage, pool, js_written) == len(js_written)

        # Python-written chunks verify in JS, against the same database and bucket.
        python_written = await interop.python_write(storage, pool)
        py_out = tmp_path / "python-written.json"
        py_out.write_text(json.dumps(python_written))
        await run_js("read", "--in", str(py_out), environment=environment)

        # Both SDKs resolved the same binding rows: one identity digest, one namespace.
        async with pool.connection() as connection:
            bindings = await (
                await connection.execute(
                    "SELECT row_binding_id, kind, provider, identity_sha256, namespace_key"
                    " FROM adcp_reporting_row_bindings ORDER BY 1"
                )
            ).fetchall()
        assert [row[:3] for row in bindings] == [
            ("inline", "postgres", "postgres"),
            ("reports", "object", "filesystem"),
        ]

        if os.environ.get("ADCP_UPDATE_INTEROP_FIXTURES") == "1":
            interop.INTEROP_DIR.mkdir(parents=True, exist_ok=True)
            for name, written in (("js-written", js_written), ("python-written", python_written)):
                state = await interop.dump_state(
                    pool, root, [item["row_set_id"] for item in written]
                )
                (interop.INTEROP_DIR / f"{name}.json").write_text(
                    json.dumps({"expected": written, "state": state}, indent=1, sort_keys=True)
                    + "\n"
                )


@pytest.mark.parametrize("writer", ["js-written", "python-written"])
async def test_rows_written_by_the_other_sdk_verify_here(writer: str, tmp_path: Path) -> None:
    path = interop.INTEROP_DIR / f"{writer}.json"
    if not path.exists():
        pytest.skip("interop fixtures have not been generated")
    recorded = json.loads(path.read_text())
    root = tmp_path / "bucket"
    async with isolated_reporting_pool() as pool:
        _, storage = await setup(pool, root)
        await interop.replay_state(pool, root, recorded["state"])
        verified = await interop.python_verify(storage, pool, recorded["expected"])
        assert verified == len(recorded["expected"]) == 12


async def test_the_recorded_object_bytes_decompress_to_the_golden_chunks(tmp_path: Path) -> None:
    """The JS SDK's gzip objects hold exactly the canonical chunks the golden fixture pins."""
    import base64
    import gzip

    path = interop.INTEROP_DIR / "js-written.json"
    if not path.exists():
        pytest.skip("interop fixtures have not been generated")
    recorded = json.loads(path.read_text())
    golden = {
        vector["id"]: [base64.b64decode(chunk) for chunk in vector["expected"]["chunks_base64"]]
        for vector in interop.golden_vectors()
    }
    by_key = {
        chunk["object_key"]: chunk
        for chunk in recorded["state"]["row_chunks"]
        if chunk["object_key"]
    }
    assert by_key
    for key, encoded in recorded["state"]["objects"].items():
        chunk = by_key[key]
        vector_id = chunk["row_set_id"].split("_")[2]
        # Revision and rows-only sets hold the same canonical rows, hence the same chunks.
        assert gzip.decompress(base64.b64decode(encoded)) == golden[vector_id][chunk["chunk_index"]]
