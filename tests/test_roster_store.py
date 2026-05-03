"""Tests for :func:`adcp.decisioning.create_roster_account_store`.

Shape C ``AccountStore`` factory for publisher-curated rosters where the
adopter has a fixed allowlist of accounts. Pairs with
:class:`SingletonAccounts` (Shape derived) and :class:`ExplicitAccounts`
(Shape explicit, loader-driven).

The roster IS the allowlist — auth-based filtering happens upstream of
this layer. Write paths (``upsert`` / ``sync_governance``) fail closed
with ``PERMISSION_DENIED`` per-entry; the roster is read-only by design.
"""

from __future__ import annotations

import asyncio

import pytest

from adcp.decisioning import (
    Account,
    AccountStore,
    AuthInfo,
    ResolveContext,
    SyncAccountsResultRow,
    SyncGovernanceEntry,
    create_roster_account_store,
)
from adcp.types import AccountReference


def _by_id(account_id: str) -> AccountReference:
    return AccountReference(root={"account_id": account_id})


def _by_natural_key(domain: str, operator: str) -> AccountReference:
    return AccountReference(
        root={"brand": {"domain": domain}, "operator": operator},
    )


def _make_roster() -> dict[str, Account]:
    return {
        "acct_alpha": Account(id="acct_alpha", name="Alpha", status="active"),
        "acct_beta": Account(id="acct_beta", name="Beta", status="active"),
    }


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_hit_returns_account() -> None:
    """ref carrying a known ``account_id`` returns the roster entry."""
    store = create_roster_account_store(roster=_make_roster())
    result = asyncio.run(store.resolve(_by_id("acct_alpha")))
    assert result is not None
    assert result.id == "acct_alpha"
    assert result.name == "Alpha"


def test_resolve_miss_returns_none() -> None:
    """ref carrying an unknown ``account_id`` returns ``None`` —
    fall-through path the framework projects to ``ACCOUNT_NOT_FOUND``."""
    store = create_roster_account_store(roster=_make_roster())
    result = asyncio.run(store.resolve(_by_id("acct_unknown")))
    assert result is None


def test_resolve_natural_key_returns_none() -> None:
    """``{brand, operator}``-shaped refs return ``None`` — publisher-
    curated rosters are queried by explicit id only. Adopters wanting
    natural-key resolution wrap ``resolve``."""
    store = create_roster_account_store(roster=_make_roster())
    result = asyncio.run(store.resolve(_by_natural_key("alpha.example.com", "alpha.example.com")))
    assert result is None


def test_resolve_none_ref_returns_none() -> None:
    """Ref-less calls (``provide_performance_feedback``,
    ``list_creative_formats``, ``preview_creative``) pass ``ref=None``;
    the helper returns ``None`` and adopters wrap to synthesize a
    publisher singleton when needed."""
    store = create_roster_account_store(roster=_make_roster())
    result = asyncio.run(store.resolve(None))
    assert result is None


def test_resolve_accepts_auth_info_kwarg() -> None:
    """The framework dispatcher calls ``accounts.resolve(ref_dict,
    auth_info=auth_info)`` — i.e. ``auth_info`` is a keyword argument
    on every dispatch path. Verify the roster store accepts that exact
    call shape (and ignores ``auth_info`` because the roster IS the
    allowlist)."""
    store = create_roster_account_store(roster=_make_roster())
    auth = AuthInfo(kind="signed_request", principal="agent_foo", scopes=["read"])
    result = asyncio.run(store.resolve(_by_id("acct_alpha"), auth_info=auth))
    assert result is not None
    assert result.id == "acct_alpha"


def test_resolve_positional_no_auth_info() -> None:
    """Positional single-arg calls (no ``auth_info``) keep working —
    matches the Protocol's ``auth_info=None`` default."""
    store = create_roster_account_store(roster=_make_roster())
    result = asyncio.run(store.resolve(_by_id("acct_beta")))
    assert result is not None
    assert result.id == "acct_beta"


def test_store_conforms_to_account_store_protocol() -> None:
    """``AccountStore`` is ``runtime_checkable``; the framework's
    boot-time platform validator calls ``isinstance(store,
    AccountStore)``. Any structural drift between the roster store's
    ``resolve`` signature and the Protocol breaks that check."""
    store = create_roster_account_store(roster=_make_roster())
    assert isinstance(store, AccountStore)


def test_resolution_literal_is_explicit() -> None:
    """Boot-time platform validation reads ``store.resolution`` to
    fail fast on misconfigured deployments. Roster stores are
    ``'explicit'`` — wire ref drives lookup."""
    store = create_roster_account_store(roster=_make_roster())
    assert store.resolution == "explicit"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_returns_full_roster() -> None:
    """``list_accounts`` returns every roster entry. Auth-based
    filtering is upstream — the roster IS the allowlist."""
    roster = _make_roster()
    store = create_roster_account_store(roster=roster)
    result = asyncio.run(store.list(ctx=ResolveContext()))
    assert len(result) == 2
    ids = {a.id for a in result}
    assert ids == {"acct_alpha", "acct_beta"}


def test_list_empty_roster() -> None:
    """Empty roster lists empty — not an error."""
    store = create_roster_account_store(roster={})
    result = asyncio.run(store.list(ctx=ResolveContext()))
    assert result == []


# ---------------------------------------------------------------------------
# upsert — fail-closed PERMISSION_DENIED per entry
# ---------------------------------------------------------------------------


def test_upsert_denies_every_entry() -> None:
    """``sync_accounts`` is not supported on a roster-backed store —
    the adopter curates accounts out-of-band. Every entry returns a
    ``failed`` row with ``PERMISSION_DENIED`` so the wire response
    surfaces the rejection per-entry instead of operation-level
    raising (which would fail the whole batch)."""
    store = create_roster_account_store(roster=_make_roster())
    refs = [
        _by_natural_key("acme.com", "acme.com"),
        _by_natural_key("globex.com", "globex.com"),
    ]
    rows = asyncio.run(store.upsert(refs, ctx=ResolveContext()))
    assert len(rows) == 2
    for row in rows:
        assert row.action == "failed"
        assert row.errors is not None
        assert row.errors[0]["code"] == "PERMISSION_DENIED"
        assert "roster" in row.errors[0]["message"].lower()


def test_upsert_empty_refs_returns_empty() -> None:
    """An empty refs list returns an empty result list — not an
    error."""
    store = create_roster_account_store(roster=_make_roster())
    rows = asyncio.run(store.upsert([], ctx=ResolveContext()))
    assert rows == []


def test_upsert_denies_by_id_refs_with_conformant_row_shape() -> None:
    """The typical ``sync_accounts`` shape carries ``account_id``-arm
    refs (buyer pre-selected an account id, calling sync to bind
    governance / verify exists). Roster stores reject those entries
    too — accounts are publisher-curated. Verify the failed row's
    shape conforms to :class:`SyncAccountsResultRow` (instance type +
    required fields populated, so the framework's wire projector
    won't crash on a missing field)."""
    store = create_roster_account_store(roster=_make_roster())
    rows = asyncio.run(
        store.upsert([_by_id("acct_alpha"), _by_id("acct_unknown")], ctx=ResolveContext())
    )
    assert len(rows) == 2
    for row in rows:
        assert isinstance(row, SyncAccountsResultRow)
        assert row.action == "failed"
        assert row.status == "failed"
        assert row.errors is not None
        assert row.errors[0]["code"] == "PERMISSION_DENIED"
        # id-arm refs don't carry brand/operator; failed rows surface
        # empty defaults (the buyer correlates by request order).
        assert row.brand == {}
        assert row.operator == ""


def test_upsert_echoes_brand_operator_for_natural_key_refs() -> None:
    """``SyncAccountsResultRow.brand`` and ``operator`` are required
    on the wire. For natural-key refs we echo them back so the buyer
    can correlate the rejection to their request entry."""
    store = create_roster_account_store(roster=_make_roster())
    rows = asyncio.run(
        store.upsert([_by_natural_key("acme.com", "acme.com")], ctx=ResolveContext())
    )
    assert rows[0].brand == {"domain": "acme.com"}
    assert rows[0].operator == "acme.com"


# ---------------------------------------------------------------------------
# sync_governance — fail-closed PERMISSION_DENIED per entry
# ---------------------------------------------------------------------------


def test_sync_governance_denies_every_entry() -> None:
    """Buyer-supplied governance agents are not persisted on a
    roster-backed store — the adopter doesn't model buyer-supplied
    governance bindings. Per-entry rejection (not operation-level)
    so a multi-account batch sees explicit rejection per row."""
    store = create_roster_account_store(roster=_make_roster())
    entries = [
        SyncGovernanceEntry(
            account=_by_id("acct_alpha"),
            governance_agents=[{"url": "https://gov.example.com/"}],
        ),
        SyncGovernanceEntry(
            account=_by_id("acct_beta"),
            governance_agents=[],
        ),
    ]
    rows = asyncio.run(store.sync_governance(entries, ctx=ResolveContext()))
    assert len(rows) == 2
    for row in rows:
        assert row.status == "failed"
        assert row.errors is not None
        assert row.errors[0]["code"] == "PERMISSION_DENIED"
        assert "roster" in row.errors[0]["message"].lower()


def test_sync_governance_echoes_account_ref() -> None:
    """``SyncGovernanceResultRow.account`` echoes the request ref so
    the buyer can correlate the rejection."""
    store = create_roster_account_store(roster=_make_roster())
    ref = _by_id("acct_alpha")
    rows = asyncio.run(
        store.sync_governance(
            [SyncGovernanceEntry(account=ref, governance_agents=[])],
            ctx=ResolveContext(),
        )
    )
    assert rows[0].account is ref


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_construction_rejects_key_id_mismatch() -> None:
    """Roster keys MUST match their value's ``id``. A mismatch is an
    adopter bug — fail at construction with a clear message naming
    the bad key, not silently at first lookup."""
    bad_roster = {
        "acct_alpha": Account(id="acct_alpha", name="Alpha"),
        "acct_beta": Account(id="acct_WRONG", name="Beta"),
    }
    with pytest.raises(ValueError) as exc_info:
        create_roster_account_store(roster=bad_roster)
    msg = str(exc_info.value)
    assert "acct_beta" in msg
    assert "acct_WRONG" in msg


def test_construction_accepts_empty_roster() -> None:
    """An empty roster is legal — adopter can ship an empty
    allowlist (every resolve misses, every list returns empty)."""
    store = create_roster_account_store(roster={})
    assert store.resolution == "explicit"


# ---------------------------------------------------------------------------
# Immutability — external mutation does not leak through
# ---------------------------------------------------------------------------


def test_external_mutation_does_not_leak_into_store() -> None:
    """The roster passed at construction is copied into an internal
    structure. Mutating the input dict afterward MUST NOT affect the
    store's view — adopters who reuse the input dict for other
    purposes don't accidentally widen the allowlist."""
    roster = _make_roster()
    store = create_roster_account_store(roster=roster)

    # Buyer-side mutation: adopter clears their map after handing it
    # to the store.
    roster.clear()
    roster["acct_attacker"] = Account(id="acct_attacker", name="Attacker")

    # Store still sees the original two entries; the injected attacker
    # entry is invisible.
    listed = asyncio.run(store.list(ctx=ResolveContext()))
    ids = {a.id for a in listed}
    assert ids == {"acct_alpha", "acct_beta"}
    assert "acct_attacker" not in ids

    attacker = asyncio.run(store.resolve(_by_id("acct_attacker")))
    assert attacker is None
