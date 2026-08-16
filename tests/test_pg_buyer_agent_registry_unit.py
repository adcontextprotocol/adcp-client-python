"""Database-independent security tests for PgBuyerAgentRegistry SQL paths."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.decisioning.pg import buyer_agent_registry as registry_module
from adcp.decisioning.pg.buyer_agent_registry import PgBuyerAgentRegistry


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[str] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        del params
        self.queries.append(query)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


class _Pool:
    def __init__(self, cursor: _Cursor) -> None:
        self._connection = _Connection(cursor)

    def connection(self) -> _Connection:
        return self._connection


@pytest.fixture(autouse=True)
def _enable_optional_pg_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "PG_AVAILABLE", True)


def test_ambiguous_legacy_credential_mapping_fails_closed() -> None:
    cursor = _Cursor(rows=[("first",), ("second",)])
    registry = PgBuyerAgentRegistry(pool=_Pool(cursor))  # type: ignore[arg-type]

    assert registry._sync_lookup_by_api_key_id("shared") is None
    assert "LIMIT 2" in cursor.queries[0]


def test_schema_bootstrap_creates_partial_unique_credential_index() -> None:
    cursor = _Cursor()
    registry = PgBuyerAgentRegistry(pool=_Pool(cursor))  # type: ignore[arg-type]

    registry.create_schema()

    ddl = "\n".join(cursor.queries)
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in ddl
    assert "api_key_id_uidx" in ddl
    assert "WHERE api_key_id IS NOT NULL" in ddl
    assert "HAVING COUNT(*) > 1" in ddl
    assert "DROP INDEX IF EXISTS adcp_buyer_agents_api_key_id_idx" in ddl


def test_schema_bootstrap_rejects_legacy_duplicate_credentials() -> None:
    cursor = _Cursor(rows=[("shared-credential", 2)])
    registry = PgBuyerAgentRegistry(pool=_Pool(cursor))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="Rotate or remove duplicate bearer credentials"):
        registry.create_schema()

    ddl = "\n".join(cursor.queries)
    assert "CREATE UNIQUE INDEX" not in ddl
    assert "DROP INDEX" not in ddl
