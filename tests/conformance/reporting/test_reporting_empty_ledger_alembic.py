"""The documented Alembic recipe: replacements commit with the version bump."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ._empty_ledger_support import (
    connect,
    create_owned_schema,
    database_url,
    install_legacy,
    isolated_schema,
    require_driver,
    surface,
)

_ENV = """
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

attributes = context.config.attributes
engine = create_engine(
    attributes["url"], poolclass=NullPool, connect_args={"options": attributes["options"]}
)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
"""

_DOC = Path(__file__).parents[3] / "docs/reporting-caller-ownership-migration.md"


def _recipe() -> str:
    """The Alembic revision exactly as the migration guide prints it."""
    section = _DOC.read_text().split("### From an Alembic revision", 1)[1]
    return section.split("```python\n", 1)[1].split("```", 1)[0]


# The guide's revision, with an injected failure after the replacement.
_REVISION = (
    _recipe().replace(
        "        replace_empty_legacy_reporting_sync(op.get_bind())\n",
        "        replace_empty_legacy_reporting_sync(op.get_bind())\n"
        "        if context.config.attributes.get('fail_after_replacement'):\n"
        "            raise RuntimeError('a later step of this revision failed')\n",
    )
    + """
from alembic import context

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None
"""
)


def _config(tmp_path, schema: str, driver: str, **attributes):
    from alembic.config import Config
    from sqlalchemy import make_url

    (tmp_path / "versions").mkdir(exist_ok=True)
    (tmp_path / "env.py").write_text(textwrap.dedent(_ENV))
    (tmp_path / "script.py.mako").write_text("")
    (tmp_path / "versions" / "0001_reporting.py").write_text(_REVISION)
    config = Config()
    config.set_main_option("script_location", str(tmp_path))
    config.attributes.update(
        url=make_url(database_url()).set(drivername=f"postgresql+{driver}"),
        options=f"-csearch_path={schema}",
        **attributes,
    )
    return config


def _version(schema: str) -> list[tuple[str]] | None:
    with connect(schema) as connection:
        if connection.execute("SELECT to_regclass('alembic_version')").fetchone() == (None,):
            return None
        return connection.execute("SELECT version_num FROM alembic_version").fetchall()


@pytest.mark.parametrize("driver", ["psycopg", "psycopg2"])
def test_alembic_upgrade_and_downgrade_commit_with_the_version(tmp_path, driver):
    require_driver(f"sqlalchemy+{driver}")
    pytest.importorskip("alembic")
    from alembic import command

    with isolated_schema() as schema:
        with connect(schema, autocommit=True) as setup:
            install_legacy(setup)
            legacy = surface(setup)

        failing = _config(tmp_path, schema, driver, fail_after_replacement=True)
        with pytest.raises(RuntimeError, match="later step"):
            command.upgrade(failing, "head")
        with connect(schema) as check:
            assert surface(check) == legacy
        assert _version(schema) is None

        command.upgrade(_config(tmp_path, schema, driver), "head")
        with connect(schema) as check:
            assert surface(check) == 0
        assert _version(schema) == [("0001",)]

        # SDK 9 builds its owned ledger when the service starts.
        create_owned_schema(schema)
        command.downgrade(_config(tmp_path, schema, driver), "base")
        with connect(schema, autocommit=True) as check:
            assert surface(check) == 0
            # An 8.x image can now create its own ledger again.
            install_legacy(check)
        assert _version(schema) == []


def test_alembic_recipe_is_a_no_op_without_a_ledger(tmp_path):
    require_driver("sqlalchemy+psycopg")
    pytest.importorskip("alembic")
    from alembic import command

    with isolated_schema() as schema:
        command.upgrade(_config(tmp_path, schema, "psycopg"), "head")
        assert _version(schema) == [("0001",)]
        command.downgrade(_config(tmp_path, schema, "psycopg"), "base")
        assert _version(schema) == []
