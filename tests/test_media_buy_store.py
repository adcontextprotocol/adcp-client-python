"""Tests for :func:`create_media_buy_store` — opt-in wrapper that gates
``targeting_overlay`` echo on the seller's declared specialisms.

Mirrors the JS ``createMediaBuyStore`` test surface (commit ``dda2a77e``)
adapted to Python's adopter-supplied :class:`MediaBuyStore`. The wrapper
delegates persist / merge / backfill to the adopter's store when the
seller claims ``property-lists`` or ``collection-lists``; for sellers
not claiming those specialisms every method is a no-op pass-through.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    create_media_buy_store,
)

PROPERTY_LIST = {
    "list_id": "acme_outdoor_allowlist_v1",
    "agent_url": "https://lists.example.com",
}
COLLECTION_LIST = {
    "list_id": "sports_collections_v3",
    "agent_url": "https://lists.example.com",
}


class InMemoryMediaBuyStore:
    """Adopter-side reference :class:`MediaBuyStore` impl.

    Implements the persist / merge / backfill contract directly on a
    nested ``{account_id: {media_buy_id: {package_id: overlay}}}`` dict.
    Faithfully ports the JS reference behavior so wrapper tests focus on
    the gate, not the underlying merge semantics.
    """

    def __init__(self) -> None:
        self.records: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}

    async def persist_from_create(
        self,
        account_id: str,
        request: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        request_packages = request.get("packages") or []
        response_packages = result.get("packages") or []
        media_buy_id = result.get("media_buy_id")
        if not request_packages or not media_buy_id:
            return
        persisted: dict[str, dict[str, Any]] = {}
        for i, req_pkg in enumerate(request_packages):
            overlay = req_pkg.get("targeting_overlay")
            if not overlay:
                continue
            buyer_ref = req_pkg.get("buyer_ref")
            matched = None
            if buyer_ref:
                matched = next(
                    (p for p in response_packages if p.get("buyer_ref") == buyer_ref),
                    None,
                )
            if matched is None and i < len(response_packages):
                matched = response_packages[i]
            package_id = matched.get("package_id") if matched else None
            if not package_id:
                continue
            persisted[package_id] = (matched or {}).get("targeting_overlay") or overlay
        if persisted:
            self.records.setdefault(account_id, {})[media_buy_id] = persisted

    async def merge_from_update(
        self,
        account_id: str,
        media_buy_id: str,
        patch: dict[str, Any],
    ) -> None:
        prior = self.records.setdefault(account_id, {}).setdefault(media_buy_id, {})
        for pkg in patch.get("packages") or []:
            package_id = pkg.get("package_id")
            if not package_id:
                continue
            if "targeting_overlay" not in pkg:
                continue
            incoming = pkg["targeting_overlay"]
            if incoming is None:
                prior.pop(package_id, None)
                continue
            existing = prior.get(package_id, {})
            merged = dict(existing)
            for key, value in incoming.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            prior[package_id] = merged
        for pkg in patch.get("new_packages") or []:
            package_id = pkg.get("package_id")
            overlay = pkg.get("targeting_overlay")
            if not package_id or not overlay:
                continue
            prior[package_id] = overlay
        if not prior:
            self.records[account_id].pop(media_buy_id, None)

    async def backfill(self, account_id: str, result: dict[str, Any]) -> dict[str, Any]:
        buys = result.get("media_buys")
        if not buys:
            return result
        account_records = self.records.get(account_id, {})
        for buy in buys:
            media_buy_id = buy.get("media_buy_id")
            if not media_buy_id:
                continue
            record = account_records.get(media_buy_id)
            if not record:
                continue
            for pkg in buy.get("packages") or []:
                package_id = pkg.get("package_id")
                if not package_id or pkg.get("targeting_overlay") is not None:
                    continue
                persisted = record.get(package_id)
                if persisted:
                    pkg["targeting_overlay"] = persisted
        return result


def _capabilities(*specialisms: str) -> DecisioningCapabilities:
    return DecisioningCapabilities(specialisms=list(specialisms))


# -----------------------------
# Active-wrapper path: seller claims property-lists / collection-lists
# -----------------------------


@pytest.mark.asyncio
async def test_persists_and_echoes_targeting_overlay_when_property_lists_claimed() -> None:
    backing = InMemoryMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_capabilities("property-lists"))

    await store.persist_from_create(
        "acct_a",
        {
            "packages": [
                {
                    "buyer_ref": "pkg_a",
                    "targeting_overlay": {
                        "property_list": PROPERTY_LIST,
                        "collection_list": COLLECTION_LIST,
                    },
                }
            ]
        },
        {
            "media_buy_id": "mb_1",
            "packages": [{"package_id": "seller_pkg_001", "buyer_ref": "pkg_a"}],
        },
    )

    result = await store.backfill(
        "acct_a",
        {"media_buys": [{"media_buy_id": "mb_1", "packages": [{"package_id": "seller_pkg_001"}]}]},
    )

    assert result["media_buys"][0]["packages"][0]["targeting_overlay"] == {
        "property_list": PROPERTY_LIST,
        "collection_list": COLLECTION_LIST,
    }


@pytest.mark.asyncio
async def test_active_wrapper_echoes_for_collection_lists_specialism() -> None:
    backing = InMemoryMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_capabilities("collection-lists"))

    overlay = {"collection_list": COLLECTION_LIST}
    await store.persist_from_create(
        "acct_a",
        {"packages": [{"buyer_ref": "pkg_a", "targeting_overlay": overlay}]},
        {"media_buy_id": "mb_1", "packages": [{"package_id": "p1", "buyer_ref": "pkg_a"}]},
    )

    result = await store.backfill(
        "acct_a", {"media_buys": [{"media_buy_id": "mb_1", "packages": [{"package_id": "p1"}]}]}
    )

    assert result["media_buys"][0]["packages"][0]["targeting_overlay"] == {
        "collection_list": COLLECTION_LIST
    }


@pytest.mark.asyncio
async def test_active_wrapper_works_when_both_specialisms_claimed() -> None:
    backing = InMemoryMediaBuyStore()
    store = create_media_buy_store(
        backing, capabilities=_capabilities("property-lists", "collection-lists")
    )

    overlay = {"property_list": PROPERTY_LIST}
    await store.persist_from_create(
        "acct_a",
        {"packages": [{"buyer_ref": "pkg_a", "targeting_overlay": overlay}]},
        {"media_buy_id": "mb_1", "packages": [{"package_id": "p1", "buyer_ref": "pkg_a"}]},
    )
    assert "mb_1" in backing.records["acct_a"]


@pytest.mark.asyncio
async def test_merge_from_update_preserves_prior_fields_when_patch_omits_them() -> None:
    backing = InMemoryMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_capabilities("property-lists"))

    initial_overlay = {"property_list": PROPERTY_LIST}
    patch_overlay = {"collection_list": COLLECTION_LIST}
    await store.persist_from_create(
        "acct_a",
        {"packages": [{"buyer_ref": "pkg_a", "targeting_overlay": initial_overlay}]},
        {"media_buy_id": "mb_1", "packages": [{"package_id": "p1", "buyer_ref": "pkg_a"}]},
    )
    await store.merge_from_update(
        "acct_a",
        "mb_1",
        {"packages": [{"package_id": "p1", "targeting_overlay": patch_overlay}]},
    )
    result = await store.backfill(
        "acct_a",
        {"media_buys": [{"media_buy_id": "mb_1", "packages": [{"package_id": "p1"}]}]},
    )
    assert result["media_buys"][0]["packages"][0]["targeting_overlay"] == {
        "property_list": PROPERTY_LIST,
        "collection_list": COLLECTION_LIST,
    }


@pytest.mark.asyncio
async def test_does_not_overwrite_targeting_overlay_seller_already_echoed() -> None:
    backing = InMemoryMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_capabilities("property-lists"))

    overlay = {"property_list": PROPERTY_LIST}
    await store.persist_from_create(
        "acct_a",
        {"packages": [{"buyer_ref": "pkg_a", "targeting_overlay": overlay}]},
        {"media_buy_id": "mb_1", "packages": [{"package_id": "p1", "buyer_ref": "pkg_a"}]},
    )

    seller_echoed = {"geo_countries": ["US"]}
    result = await store.backfill(
        "acct_a",
        {
            "media_buys": [
                {
                    "media_buy_id": "mb_1",
                    "packages": [{"package_id": "p1", "targeting_overlay": seller_echoed}],
                }
            ]
        },
    )
    assert result["media_buys"][0]["packages"][0]["targeting_overlay"] is seller_echoed


@pytest.mark.asyncio
async def test_account_scopes_records_across_accounts() -> None:
    backing = InMemoryMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_capabilities("property-lists"))

    overlay_a = {"property_list": PROPERTY_LIST}
    overlay_b = {"collection_list": COLLECTION_LIST}

    await store.persist_from_create(
        "acct_a",
        {"packages": [{"buyer_ref": "pkg", "targeting_overlay": overlay_a}]},
        {"media_buy_id": "mb_1", "packages": [{"package_id": "p1", "buyer_ref": "pkg"}]},
    )
    await store.persist_from_create(
        "acct_b",
        {"packages": [{"buyer_ref": "pkg", "targeting_overlay": overlay_b}]},
        {"media_buy_id": "mb_1", "packages": [{"package_id": "p1", "buyer_ref": "pkg"}]},
    )

    a = await store.backfill(
        "acct_a", {"media_buys": [{"media_buy_id": "mb_1", "packages": [{"package_id": "p1"}]}]}
    )
    b = await store.backfill(
        "acct_b", {"media_buys": [{"media_buy_id": "mb_1", "packages": [{"package_id": "p1"}]}]}
    )

    assert a["media_buys"][0]["packages"][0]["targeting_overlay"] == overlay_a
    assert b["media_buys"][0]["packages"][0]["targeting_overlay"] == overlay_b


# -----------------------------
# No-op pass-through path: seller claims neither specialism
# -----------------------------


@pytest.mark.asyncio
async def test_noop_pass_through_when_neither_specialism_claimed() -> None:
    backing = InMemoryMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_capabilities("sales-non-guaranteed"))

    overlay = {"property_list": PROPERTY_LIST}
    await store.persist_from_create(
        "acct_a",
        {"packages": [{"buyer_ref": "pkg_a", "targeting_overlay": overlay}]},
        {"media_buy_id": "mb_1", "packages": [{"package_id": "p1", "buyer_ref": "pkg_a"}]},
    )
    # Backing store was never touched — wrapper short-circuited.
    assert backing.records == {}

    # Backfill is a pure pass-through: response is returned unchanged.
    response: dict[str, Any] = {
        "media_buys": [{"media_buy_id": "mb_1", "packages": [{"package_id": "p1"}]}]
    }
    result = await store.backfill("acct_a", response)
    assert result is response
    assert "targeting_overlay" not in result["media_buys"][0]["packages"][0]


@pytest.mark.asyncio
async def test_noop_pass_through_with_empty_specialisms() -> None:
    backing = InMemoryMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_capabilities())

    await store.merge_from_update(
        "acct_a",
        "mb_1",
        {"packages": [{"package_id": "p1", "targeting_overlay": {"property_list": PROPERTY_LIST}}]},
    )
    assert backing.records == {}


# -----------------------------
# Sync adopter callbacks
# -----------------------------


class SyncMediaBuyStore:
    """Adopter store with sync (non-async) methods.

    Mirrors the ``MaybeAsync`` shape used elsewhere — the wrapper must
    accept either flavour without forcing adopters into ``async def``.
    """

    def __init__(self) -> None:
        self.persisted: list[tuple[str, str]] = []
        self.merged: list[tuple[str, str]] = []
        self.backfilled: list[str] = []

    def persist_from_create(
        self, account_id: str, request: dict[str, Any], result: dict[str, Any]
    ) -> None:
        self.persisted.append((account_id, result["media_buy_id"]))

    def merge_from_update(self, account_id: str, media_buy_id: str, patch: dict[str, Any]) -> None:
        self.merged.append((account_id, media_buy_id))

    def backfill(self, account_id: str, result: dict[str, Any]) -> dict[str, Any]:
        self.backfilled.append(account_id)
        return result


@pytest.mark.asyncio
async def test_wrapper_accepts_sync_adopter_callbacks() -> None:
    backing = SyncMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_capabilities("property-lists"))

    await store.persist_from_create(
        "acct_a",
        {"packages": [{"buyer_ref": "pkg", "targeting_overlay": {"property_list": PROPERTY_LIST}}]},
        {"media_buy_id": "mb_1", "packages": [{"package_id": "p1", "buyer_ref": "pkg"}]},
    )
    await store.merge_from_update("acct_a", "mb_1", {"packages": []})
    await store.backfill("acct_a", {"media_buys": []})

    assert backing.persisted == [("acct_a", "mb_1")]
    assert backing.merged == [("acct_a", "mb_1")]
    assert backing.backfilled == ["acct_a"]


# -----------------------------
# Specialism gating semantics
# -----------------------------


def test_active_wrapper_returns_distinct_object_from_adopter_store() -> None:
    """Wrapper is always a fresh object — never returns the adopter
    store directly even on the no-op path. Lets adopters reason about
    identity (e.g. assigning to ``platform.media_buy_store``) without
    surprise aliasing of their persistence layer."""
    backing = InMemoryMediaBuyStore()
    active = create_media_buy_store(backing, capabilities=_capabilities("property-lists"))
    noop = create_media_buy_store(backing, capabilities=_capabilities("sales-non-guaranteed"))
    assert active is not backing
    assert noop is not backing
    assert active is not noop
