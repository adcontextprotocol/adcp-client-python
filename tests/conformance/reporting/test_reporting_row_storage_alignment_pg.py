"""In-place upgrade of row storage from the pre-AdCP-alignment names and bounds.

``tests/fixtures/reporting-row-storage-pre-alignment.sql`` is the DDL as released before the
alignment (``object_key``, ``native_version``, ``byte_count``, ``physical_byte_count``,
``canonical_byte_count`` and ``encoding``). Applying the current migration renames in place,
keeps every recorded location, and leaves a database indistinguishable from a fresh one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.ledger.row_storage import (
    REPORTING_ROW_STORAGE_DDL_PATH,
    REPORTING_ROW_STORAGE_TABLES,
)
from adcp.reporting.outbox._schema import _catalog_objects

from ._generation_support import isolated_reporting_pool

OLD_DDL = (
    Path(__file__).resolve().parents[2] / "fixtures" / "reporting-row-storage-pre-alignment.sql"
)
SHA = "a" * 64


async def seed_old(connection: Any, object_key: str = "adcp-rows/ns/acct/2026/10/01/k") -> None:
    await connection.execute(OLD_DDL.read_text())
    await connection.execute(
        "INSERT INTO adcp_reporting_row_bindings (row_binding_id, kind, provider, identity_config,"
        " identity_sha256, namespace_key) VALUES ('reports', 'object', 'filesystem', '{}', %s, %s)",
        (SHA, "b" * 64),
    )
    await connection.execute(
        "INSERT INTO adcp_reporting_row_sets (row_set_id, row_set_kind, account_id, obligation_id,"
        " encoding, digest_profile, content_sha256, canonical_byte_count, row_count,"
        " row_manifest_sha256, chunk_count, row_binding_id)"
        " VALUES ('rrev_old', 'revision', 'acct', 'robl', 'adcp_canonical_jsonl_v1',"
        " 'revision_envelope_v1', %s, 99, 1, %s, 1, 'reports')",
        (SHA, SHA),
    )
    await connection.execute(
        "INSERT INTO adcp_reporting_row_chunks (row_set_id, chunk_index, account_id, obligation_id,"
        " first_ordinal, row_count, byte_count, sha256, segments, object_key, native_version,"
        " compression, physical_sha256, physical_byte_count)"
        " VALUES ('rrev_old', 0, 'acct', 'robl', 0, 1, 42, %s, '[]', %s, '1:2:3:4', 'gzip', %s, 17)",
        (SHA, object_key, SHA),
    )
    await connection.execute(
        "INSERT INTO adcp_reporting_row_write_intents (row_binding_id, account_id, row_set_id,"
        " content_sha256, owner_token, created_objects, expires_at)"
        " VALUES ('reports', 'acct', 'rrev_open', %s, 'tok',"
        ' \'[{"objectKey": "k1", "nativeVersion": "v1"}, {"unrelated": true}]\','
        " clock_timestamp() + INTERVAL '1 hour')",
        (SHA,),
    )


async def catalog(connection: Any) -> dict[str, Any]:
    return await _catalog_objects(
        connection,
        table_names=REPORTING_ROW_STORAGE_TABLES,
        function_identity=("adcp_reporting_row_storage_immutable", ""),
        name_prefix="adcp_",
    )


async def test_an_old_database_is_renamed_in_place_and_matches_a_fresh_install() -> None:
    migration = REPORTING_ROW_STORAGE_DDL_PATH.read_text()
    async with isolated_reporting_pool() as old, isolated_reporting_pool() as fresh:
        async with old.connection() as connection, connection.transaction():
            await seed_old(connection)
            await connection.execute(migration)
        async with fresh.connection() as connection, connection.transaction():
            await connection.execute(migration)

        async with old.connection() as connection:
            row_set = await (
                await connection.execute(
                    "SELECT format, algorithm, content_size_bytes FROM adcp_reporting_row_sets"
                )
            ).fetchone()
            chunk = await (
                await connection.execute(
                    "SELECT canonical_size_bytes, object_ref, native_version_ref, size_bytes"
                    " FROM adcp_reporting_row_chunks"
                )
            ).fetchone()
            intent = await (
                await connection.execute(
                    "SELECT created_objects FROM adcp_reporting_row_write_intents"
                )
            ).fetchone()
            columns = {
                row[0]
                for row in await (
                    await connection.execute(
                        "SELECT table_name || '.' || column_name FROM information_schema.columns"
                        " WHERE table_schema = current_schema()"
                    )
                ).fetchall()
            }
            upgraded = await catalog(connection)
        # Every recorded location and size is kept; the old single-valued encoding is backfilled.
        assert row_set == ("jsonl", "adcp_jcs_rows_v1", 99)
        assert chunk == (42, "adcp-rows/ns/acct/2026/10/01/k", "1:2:3:4", 17)
        assert intent[0] == [
            {"object_ref": "k1", "native_version_ref": "v1"},
            {"unrelated": True},
        ]
        assert (
            not {
                "adcp_reporting_row_chunks.object_key",
                "adcp_reporting_row_chunks.native_version",
                "adcp_reporting_row_chunks.byte_count",
                "adcp_reporting_row_chunks.physical_byte_count",
                "adcp_reporting_row_sets.canonical_byte_count",
                "adcp_reporting_row_sets.encoding",
            }
            & columns
        )

        async with fresh.connection() as connection:
            installed = await catalog(connection)
        # Names, constraints, indexes and the guard function all equal a clean install's, which
        # is what the startup catalog check compares.
        assert upgraded.keys() == installed.keys()
        assert {key: value["fingerprint"] for key, value in upgraded.items()} == {
            key: value["fingerprint"] for key, value in installed.items()
        }


async def test_reapplying_the_migration_is_a_no_op() -> None:
    migration = REPORTING_ROW_STORAGE_DDL_PATH.read_text()
    async with isolated_reporting_pool() as pool:
        async with pool.connection() as connection, connection.transaction():
            await seed_old(connection)
            await connection.execute(migration)
        async with pool.connection() as connection:
            before = await catalog(connection)
        async with pool.connection() as connection, connection.transaction():
            await connection.execute(migration)
        async with pool.connection() as connection:
            after = await catalog(connection)
            intent = await (
                await connection.execute(
                    "SELECT created_objects FROM adcp_reporting_row_write_intents"
                )
            ).fetchone()
        assert before == after
        assert intent[0][0] == {"object_ref": "k1", "native_version_ref": "v1"}


async def test_an_out_of_bounds_recorded_key_fails_the_migration_and_rolls_back() -> None:
    psycopg = pytest.importorskip("psycopg")

    migration = REPORTING_ROW_STORAGE_DDL_PATH.read_text()
    async with isolated_reporting_pool() as pool:
        async with pool.connection() as connection, connection.transaction():
            await seed_old(connection, object_key="adcp-rows/" + "k" * 1100)
        with pytest.raises(psycopg.errors.CheckViolation, match="outside AdCP 1 to 1024"):
            async with pool.connection() as connection, connection.transaction():
                await connection.execute(migration)
        async with pool.connection() as connection:
            columns = {
                row[0]
                for row in await (
                    await connection.execute(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = current_schema()"
                        " AND table_name = 'adcp_reporting_row_chunks'"
                    )
                ).fetchall()
            }
        # Nothing was renamed: the failed migration left the old schema exactly as it was.
        assert "object_key" in columns and "object_ref" not in columns


async def test_the_new_bounds_are_enforced_on_a_fresh_install() -> None:
    psycopg = pytest.importorskip("psycopg")

    migration = REPORTING_ROW_STORAGE_DDL_PATH.read_text()
    async with isolated_reporting_pool() as pool:
        async with pool.connection() as connection, connection.transaction():
            await connection.execute(migration)
            await connection.execute(
                "INSERT INTO adcp_reporting_row_bindings (row_binding_id, kind, provider,"
                " identity_config, identity_sha256, namespace_key)"
                " VALUES ('reports', 'object', 'filesystem', '{}', %s, %s)",
                (SHA, "b" * 64),
            )
            await connection.execute(
                "INSERT INTO adcp_reporting_row_sets (row_set_id, row_set_kind, account_id,"
                " obligation_id, format, algorithm, digest_profile, content_sha256,"
                " content_size_bytes, row_count, row_manifest_sha256, chunk_count, row_binding_id)"
                " VALUES ('r', 'revision', 'a', 'o', 'jsonl', 'adcp_jcs_rows_v1',"
                " 'revision_envelope_v1', %s, 9, 1, %s, 1, 'reports')",
                (SHA, SHA),
            )

        async def chunk(ref: str, version: str) -> None:
            async with pool.connection() as connection, connection.transaction():
                await connection.execute(
                    "INSERT INTO adcp_reporting_row_chunks (row_set_id, chunk_index, account_id,"
                    " obligation_id, first_ordinal, row_count, canonical_size_bytes, sha256,"
                    " segments, object_ref, native_version_ref, compression, physical_sha256,"
                    " size_bytes) VALUES ('r', 0, 'a', 'o', 0, 1, 5, %s, '[]', %s, %s, 'gzip',"
                    " %s, 3)",
                    (SHA, ref, version, SHA),
                )

        await chunk("k" * 1024, "v" * 1024)  # the bounds themselves are allowed
        async with pool.connection() as connection, connection.transaction():
            await connection.execute("DELETE FROM adcp_reporting_row_chunks")
        for ref, version in (("k" * 1025, "v"), ("k", "v" * 1025), ("", "v")):
            with pytest.raises(psycopg.errors.CheckViolation):
                await chunk(ref, version)
