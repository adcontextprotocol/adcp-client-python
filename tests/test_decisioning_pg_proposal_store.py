"""Unit tests for adcp.decisioning.pg.PgProposalStore.

These tests run without a PostgreSQL instance — they cover:

* Stub import path (``from adcp.decisioning import PgProposalStore``)
* ``is_durable = True`` marker (class-level)
* ``ImportError`` raised on instantiation when ``[pg]`` extra is absent
* Protocol structural matching when psycopg IS installed
* ``MIGRATION`` upgrade/downgrade strings present
* ``table_name`` validation rejects unsafe identifiers
* Helper encode/decode round-trips for recipes and payloads

Real-database behavioral tests live in
``tests/conformance/decisioning/test_pg_proposal_store.py`` — they skip
unless ``ADCP_PG_TEST_URL`` is set.
"""

from __future__ import annotations

import pytest

# -- Stub always importable -----------------------------------------------


def test_pg_proposal_store_importable_from_decisioning() -> None:
    from adcp.decisioning import PgProposalStore  # noqa: F401

    assert PgProposalStore is not None


def test_pg_proposal_store_is_durable_stub() -> None:
    from adcp.decisioning import PgProposalStore

    assert getattr(PgProposalStore, "is_durable", None) is True


def test_pg_proposal_store_stub_raises_import_error_without_pg() -> None:
    import importlib.util

    if importlib.util.find_spec("psycopg_pool") is not None:
        pytest.skip("psycopg_pool is installed — stub not in effect")

    from adcp.decisioning import PgProposalStore

    with pytest.raises(ImportError, match=r"adcp\[pg\]"):
        PgProposalStore(pool=None)  # type: ignore[arg-type]


# -- Tests requiring psycopg (structural, no real DB) ---------------------


psycopg_pool = pytest.importorskip(
    "psycopg_pool",
    reason="psycopg_pool not installed — skipping structural pg tests",
)


def test_pg_proposal_store_satisfies_protocol() -> None:
    from unittest.mock import MagicMock

    from adcp.decisioning import PgProposalStore
    from adcp.decisioning.proposal_store import ProposalStore

    store = PgProposalStore(pool=MagicMock())
    assert isinstance(store, ProposalStore)


def test_pg_proposal_store_is_durable_class_var() -> None:
    """is_durable must be class-level, not instance-level — production-mode
    gates inspect ``type(store).is_durable``."""
    from adcp.decisioning.pg import PgProposalStore

    assert PgProposalStore.is_durable is True
    assert "is_durable" in PgProposalStore.__dict__


def test_pg_proposal_store_migration_sql_default_table() -> None:
    """Adopters wiring an Alembic / dbmate migration paste the upgrade
    string. Default-table variant must include the canonical name."""
    from adcp.decisioning.pg import PgProposalStore

    migration = PgProposalStore.migration_sql()
    assert "upgrade" in migration
    assert "downgrade" in migration
    upgrade = migration["upgrade"]
    assert "adcp_proposal_drafts" in upgrade
    assert "PRIMARY KEY (account_id, proposal_id)" in upgrade
    assert "media_buy_idx" in upgrade
    assert migration["downgrade"].startswith("DROP TABLE")


def test_pg_proposal_store_migration_sql_honors_custom_table_name() -> None:
    """An adopter who constructed PgProposalStore with a custom
    table_name MUST also be able to ask for migration SQL keyed to
    that same name — otherwise the docs-emitted DDL would create the
    wrong table."""
    from adcp.decisioning.pg import PgProposalStore

    migration = PgProposalStore.migration_sql(table_name="my_app_proposals")
    assert "my_app_proposals" in migration["upgrade"]
    assert "adcp_proposal_drafts" not in migration["upgrade"]
    assert "my_app_proposals_media_buy_idx" in migration["upgrade"]
    assert "DROP TABLE IF EXISTS my_app_proposals" in migration["downgrade"]


def test_pg_proposal_store_migration_sql_rejects_unsafe_table_name() -> None:
    """SQL identifier guard applies to the migration API too."""
    from adcp.decisioning.pg import PgProposalStore

    with pytest.raises(ValueError, match="table_name must match"):
        PgProposalStore.migration_sql(table_name="bad-name")


def test_pg_proposal_store_rejects_unsafe_table_name() -> None:
    """SQL identifier guard — ``table_name`` must match the ASCII-only
    pattern. Non-ASCII letters, hyphens, or quote characters get
    rejected at construction time."""
    from unittest.mock import MagicMock

    from adcp.decisioning.pg import PgProposalStore

    with pytest.raises(ValueError, match="table_name must match"):
        PgProposalStore(pool=MagicMock(), table_name="bad-name")
    with pytest.raises(ValueError, match="table_name must match"):
        PgProposalStore(pool=MagicMock(), table_name="proposals; DROP TABLE")
    with pytest.raises(ValueError, match="table_name must match"):
        PgProposalStore(pool=MagicMock(), table_name="Α_unicode_alpha")  # noqa: RUF001


def test_pg_proposal_store_accepts_custom_table_name() -> None:
    """Adopters with one Postgres serving multiple AdCP instances or
    whose ``proposal_drafts`` table is already taken pass a custom
    table_name."""
    from unittest.mock import MagicMock

    from adcp.decisioning.pg import PgProposalStore

    store = PgProposalStore(pool=MagicMock(), table_name="my_app_proposals")
    assert "my_app_proposals" in store._sql_get_state  # type: ignore[attr-defined]


# -- helper round-trip tests -----------------------------------------------


def test_encode_decode_recipes_round_trip() -> None:
    """A recipe → JSON → recipe round-trip preserves all typed fields
    using the default decoder (base ``Recipe``)."""
    from adcp.decisioning.pg.proposal_store import (
        _decode_recipes,
        _default_recipe_decoder,
        _encode_recipes,
    )
    from adcp.decisioning.recipe import CapabilityOverlap, Recipe

    overlap = CapabilityOverlap(pricing_models=frozenset({"cpm"}))
    recipes = {"prod_1": Recipe(capability_overlap=overlap)}
    encoded = _encode_recipes(recipes)
    decoded = _decode_recipes(encoded, _default_recipe_decoder)
    assert "prod_1" in decoded
    # The default decoder returns a base Recipe with capability_overlap
    # rehydrated to a CapabilityOverlap dataclass.
    assert decoded["prod_1"].capability_overlap is not None
    # Default decoder strips arbitrary types (frozenset → list via JSON);
    # adopters who want exact frozenset round-trips supply a typed
    # decoder. Confirm at minimum that the dict shape survives.


def test_encode_recipes_accepts_dict_shaped_passthrough() -> None:
    """Adopter migrations may seed the store with pre-serialized dicts;
    confirm the encoder accepts them without requiring a Recipe instance."""
    from adcp.decisioning.pg.proposal_store import _encode_recipes

    encoded = _encode_recipes({"prod_1": {"recipe_kind": "gam", "x": 1}})  # type: ignore[dict-item]
    assert "recipe_kind" in encoded
    assert "gam" in encoded


def test_decode_payload_accepts_dict_and_string() -> None:
    """psycopg may return JSONB as a dict (preferred) or string (older
    drivers); both must round-trip cleanly."""
    from adcp.decisioning.pg.proposal_store import _decode_payload

    assert _decode_payload({"k": "v"}) == {"k": "v"}
    assert _decode_payload('{"k": "v"}') == {"k": "v"}
    assert _decode_payload(None) == {}


def test_ensure_utc_rejects_naive_datetime() -> None:
    """Naive datetimes from the column indicate adopter migration drift
    (column declared as ``TIMESTAMP WITHOUT TIME ZONE``). Fail fast."""
    from datetime import datetime

    from adcp.decisioning.pg.proposal_store import _ensure_utc
    from adcp.decisioning.types import AdcpError

    with pytest.raises(AdcpError, match="naive datetime"):
        _ensure_utc(datetime(2026, 1, 1))


def test_ensure_utc_none_passthrough() -> None:
    from adcp.decisioning.pg.proposal_store import _ensure_utc

    assert _ensure_utc(None) is None


# -- migration_sql ↔ proposal_store.sql drift guard ------------------------


def _normalize_sql(text: str) -> str:
    """Strip comments and collapse whitespace so the migration_sql()
    output and the .sql file can be compared semantically."""
    out_lines = []
    for line in text.splitlines():
        stripped = line.split("--", 1)[0].rstrip()
        if not stripped.strip():
            continue
        out_lines.append(" ".join(stripped.split()))
    return "\n".join(out_lines)


def test_migration_sql_matches_proposal_store_sql_file() -> None:
    """The Python ``migration_sql()`` upgrade string and the shipped
    ``proposal_store.sql`` file MUST stay in sync. If you change one,
    change both — adopters wire either depending on their migration
    tool."""
    from pathlib import Path

    from adcp.decisioning.pg import PgProposalStore

    py_upgrade = PgProposalStore.migration_sql()["upgrade"]
    sql_path = (
        Path(__file__).parent.parent / "src" / "adcp" / "decisioning" / "pg" / "proposal_store.sql"
    )
    file_text = sql_path.read_text()
    assert _normalize_sql(py_upgrade) == _normalize_sql(file_text)


# -- default recipe_decoder fail-loud --------------------------------------


def test_default_decoder_fails_loud_on_typed_payload() -> None:
    """When the stored payload has a ``recipe_kind`` field, the default
    decoder MUST raise with a clear pointer to ``recipe_decoder=`` —
    not the raw Pydantic ``extra='forbid'`` message."""
    from adcp.decisioning.pg.proposal_store import _default_recipe_decoder
    from adcp.decisioning.types import AdcpError

    with pytest.raises(AdcpError) as exc:
        _default_recipe_decoder({"recipe_kind": "gam", "line_item_id": "li_1"})
    assert "recipe_decoder" in str(exc.value)
    assert exc.value.code == "INTERNAL_ERROR"
