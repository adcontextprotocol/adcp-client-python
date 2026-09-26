"""Real-Postgres contract checks for the reporting interop harness database owner."""

from __future__ import annotations

import importlib.util
import os
import secrets
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
sql = pytest.importorskip("psycopg.sql")
conninfo = pytest.importorskip("psycopg.conninfo")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping interop harness Postgres tests",
        allow_module_level=True,
    )

ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = ROOT / "scripts/ci/reporting_interop/run_foundation_matrix.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "reporting_interop_foundation_database_test", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    return runner


def test_create_database_supports_createdb_non_superuser() -> None:
    """Cluster identity must not require pg_read_all_settings or superuser."""

    runner = _load_runner()
    suffix = secrets.token_hex(6)
    role = f"adcp_interop_role_{suffix}"
    database = f"adcp_interop_db_{suffix}"
    password = secrets.token_urlsafe(24)
    fields = conninfo.conninfo_to_dict(TEST_URL)
    fields.update(user=role, password=password)
    role_dsn = conninfo.make_conninfo(**fields)

    with psycopg.connect(TEST_URL, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN CREATEDB PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    try:
        identity = runner._create_database(role_dsn, database)

        assert identity["name"] == database
        assert identity["encoding"] == "UTF8"
        assert identity["collation"] == "C"
        assert set(identity["cluster_identity"]) == {
            "system_identifier",
            "postmaster_started_at",
        }
        assert identity["cluster_identity"]["system_identifier"]
        assert identity["cluster_identity"]["postmaster_started_at"]
        with psycopg.connect(TEST_URL, autocommit=True) as admin:
            role_row = admin.execute(
                "SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname=%s",
                (role,),
            ).fetchone()
        assert role_row == (False, True)
    finally:
        with psycopg.connect(TEST_URL, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
