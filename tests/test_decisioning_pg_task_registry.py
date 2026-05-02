"""Unit tests for adcp.decisioning.pg.PostgresTaskRegistry.

These tests run without a PostgreSQL instance — they cover:

* Stub import path (``from adcp.decisioning import PostgresTaskRegistry``)
* ``is_durable = True`` marker (class-level, not instance-level)
* ``ImportError`` raised on instantiation when ``[pg]`` extra is absent
* account_id validation in ``issue()`` (same guard as InMemoryTaskRegistry)
* Protocol structural matching when psycopg IS installed

Real-database behavioral tests live in
``tests/conformance/decisioning/test_pg_task_registry.py`` — they skip
unless ``ADCP_PG_TEST_URL`` is set.
"""

from __future__ import annotations

import pytest

# -- Stub always importable -----------------------------------------------


def test_postgres_task_registry_importable_from_decisioning() -> None:
    """PostgresTaskRegistry is always importable from adcp.decisioning,
    even without the [pg] extra (stub class replaces the real one)."""
    from adcp.decisioning import PostgresTaskRegistry  # noqa: F401

    assert PostgresTaskRegistry is not None


def test_postgres_task_registry_is_durable_stub() -> None:
    """The stub class advertises is_durable=True so type-checking passes."""
    from adcp.decisioning import PostgresTaskRegistry

    assert getattr(PostgresTaskRegistry, "is_durable", None) is True


def test_postgres_task_registry_stub_raises_import_error_without_pg() -> None:
    """When psycopg_pool is not installed, instantiation raises ImportError."""
    import importlib.util

    if importlib.util.find_spec("psycopg_pool") is not None:
        pytest.skip("psycopg_pool is installed — stub not in effect")

    from adcp.decisioning import PostgresTaskRegistry

    with pytest.raises(ImportError, match="adcp\\[pg\\]"):
        PostgresTaskRegistry(pool=None)  # type: ignore[arg-type]


# -- Tests requiring psycopg (structural, no real DB) ---------------------


psycopg_pool = pytest.importorskip(
    "psycopg_pool",
    reason="psycopg_pool not installed — skipping structural pg tests",
)


def test_postgres_task_registry_satisfies_protocol() -> None:
    """PostgresTaskRegistry structurally matches the TaskRegistry Protocol
    when the [pg] extra is installed."""
    from unittest.mock import MagicMock

    from adcp.decisioning import PostgresTaskRegistry
    from adcp.decisioning.task_registry import TaskRegistry

    mock_pool = MagicMock()
    registry = PostgresTaskRegistry(pool=mock_pool)
    assert isinstance(registry, TaskRegistry)


def test_postgres_task_registry_is_durable_class_var() -> None:
    """is_durable must be a class-level bool, not an instance attribute.

    serve.py checks ``type(registry).is_durable`` (via hasattr(type(...)))
    so an instance-level attribute would pass the hasattr check but fail
    mypy's ClassVar constraint and Protocol matching.
    """
    from adcp.decisioning.pg import PostgresTaskRegistry

    assert PostgresTaskRegistry.is_durable is True
    # Verify it's on the class, not only on instances.
    assert "is_durable" in PostgresTaskRegistry.__dict__


@pytest.mark.asyncio
async def test_issue_rejects_empty_account_id() -> None:
    """issue() must reject blank / sentinel account_ids (cross-tenant guard)."""
    from unittest.mock import MagicMock

    from adcp.decisioning.pg import PostgresTaskRegistry

    registry = PostgresTaskRegistry(pool=MagicMock())

    with pytest.raises(ValueError, match="account_id must be a non-empty"):
        await registry.issue(account_id="", task_type="create_media_buy")

    with pytest.raises(ValueError, match="account_id must be a non-empty"):
        await registry.issue(account_id="   ", task_type="create_media_buy")

    with pytest.raises(ValueError, match="account_id must be a non-empty"):
        await registry.issue(account_id="<unset>", task_type="create_media_buy")
