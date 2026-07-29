"""Regression tests for the bearer-credential uniqueness migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0003_unique_buyer_api_key.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("unique_buyer_api_key_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_rejects_legacy_duplicates_before_index_changes() -> None:
    migration = _load_migration()
    result = MagicMock()
    result.scalar_one.return_value = 2
    connection = MagicMock()
    connection.execute.return_value = result

    with (
        patch.object(migration.context, "is_offline_mode", return_value=False),
        patch.object(migration.op, "get_bind", return_value=connection),
        patch.object(migration.op, "drop_index") as drop_index,
    ):
        with pytest.raises(RuntimeError, match="duplicated credential identifier"):
            migration.upgrade()
    drop_index.assert_not_called()


def test_upgrade_replaces_legacy_index_with_unique_index() -> None:
    migration = _load_migration()
    result = MagicMock()
    result.scalar_one.return_value = 0
    connection = MagicMock()
    connection.execute.return_value = result

    with (
        patch.object(migration.context, "is_offline_mode", return_value=False),
        patch.object(migration.op, "get_bind", return_value=connection),
        patch.object(migration.op, "drop_index") as drop_index,
        patch.object(migration.op, "create_index") as create_index,
    ):
        migration.upgrade()

    drop_index.assert_called_once_with("buyer_agents_api_key_idx", table_name="buyer_agents")
    assert create_index.call_args.kwargs["unique"] is True


def test_offline_upgrade_emits_index_changes_without_querying_data() -> None:
    migration = _load_migration()
    with (
        patch.object(migration.context, "is_offline_mode", return_value=True),
        patch.object(migration.op, "get_bind") as get_bind,
        patch.object(migration.op, "drop_index"),
        patch.object(migration.op, "create_index") as create_index,
    ):
        migration.upgrade()

    get_bind.assert_not_called()
    assert create_index.call_args.kwargs["unique"] is True
