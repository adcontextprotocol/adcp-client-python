"""Integration tests for the :class:`MediaBuyStore` wired through
:class:`PlatformHandler`.

The unit tests in ``test_media_buy_store.py`` exercise the standalone
wrapper (specialism gating + sync/async dispatch + reference impl
behavior). These tests verify the framework integration: a store wired
via ``media_buy_store=`` on :class:`PlatformHandler` is invoked from the
``create_media_buy`` / ``update_media_buy`` / ``get_media_buys`` shims
with the correct call shape so adopters never persist + echo by hand.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
    create_media_buy_store,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.server.base import ToolContext

PROPERTY_LIST = {
    "list_id": "acme_outdoor_allowlist_v1",
    "agent_url": "https://lists.example.com",
}


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-store-")
    yield pool
    pool.shutdown(wait=True)


class _RecordingMediaBuyStore:
    """Adopter-side reference store that records every call shape.

    Records dict-shaped payloads so tests can assert the framework
    normalized Pydantic → dict before crossing the adopter boundary.
    """

    def __init__(self) -> None:
        self.records: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        self.persist_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.merge_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.backfill_calls: list[tuple[str, dict[str, Any]]] = []

    async def persist_from_create(
        self,
        account_id: str,
        request: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        self.persist_calls.append((account_id, request, result))
        media_buy_id = result.get("media_buy_id")
        if not media_buy_id:
            return
        request_packages = request.get("packages") or []
        response_packages = result.get("packages") or []
        persisted: dict[str, dict[str, Any]] = {}
        for i, req_pkg in enumerate(request_packages):
            overlay = req_pkg.get("targeting_overlay")
            if not overlay:
                continue
            if i < len(response_packages):
                package_id = response_packages[i].get("package_id")
                if package_id:
                    persisted[package_id] = overlay
        if persisted:
            self.records.setdefault(account_id, {})[media_buy_id] = persisted

    async def merge_from_update(
        self,
        account_id: str,
        media_buy_id: str,
        patch: dict[str, Any],
    ) -> None:
        self.merge_calls.append((account_id, media_buy_id, patch))
        prior = self.records.setdefault(account_id, {}).setdefault(media_buy_id, {})
        for pkg in patch.get("packages") or []:
            package_id = pkg.get("package_id")
            overlay = pkg.get("targeting_overlay")
            if not package_id or not overlay:
                continue
            existing = dict(prior.get(package_id, {}))
            existing.update(overlay)
            prior[package_id] = existing

    async def backfill(self, account_id: str, result: dict[str, Any]) -> dict[str, Any]:
        self.backfill_calls.append((account_id, result))
        for buy in result.get("media_buys") or []:
            media_buy_id = buy.get("media_buy_id")
            record = self.records.get(account_id, {}).get(media_buy_id or "", {})
            for pkg in buy.get("packages") or []:
                package_id = pkg.get("package_id")
                if not package_id or pkg.get("targeting_overlay") is not None:
                    continue
                persisted = record.get(package_id)
                if persisted:
                    pkg["targeting_overlay"] = persisted
        return result


def _make_handler(
    platform: DecisioningPlatform,
    executor: ThreadPoolExecutor,
    *,
    media_buy_store: Any = None,
) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        media_buy_store=media_buy_store,
    )


# -----------------------------
# create_media_buy → persist_from_create
# -----------------------------


@pytest.mark.asyncio
async def test_create_media_buy_persists_overlay_through_handler(executor) -> None:
    """A successful ``create_media_buy`` invokes
    ``store.persist_from_create`` with the request and response
    normalized to dict shape, scoped to the resolved ``account.id``.
    """
    from adcp.types import (
        CreateMediaBuyRequest,
        CreateMediaBuySuccessResponse,
        Package,
    )

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["property-lists"])
        accounts = SingletonAccounts(account_id="acme")

        async def create_media_buy(self, req, ctx):
            return CreateMediaBuySuccessResponse(
                media_buy_id="mb_1",
                confirmed_at="2026-05-01T00:00:00Z",
                revision=1,
                packages=[Package(package_id="seller_pkg_001")],
                status="active",
            )

    backing = _RecordingMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_Platform.capabilities)
    handler = _make_handler(_Platform(), executor, media_buy_store=store)

    req = CreateMediaBuyRequest(
        account={"account_id": "acct_a"},
        brand={"domain": "example.com"},
        idempotency_key="idem_aaaa1234567890",
        start_time="2026-05-01T00:00:00Z",
        end_time="2026-05-31T23:59:59Z",
        packages=[
            {
                "product_id": "prod_a",
                "buyer_ref": "pkg_a",
                "budget": 1000.0,
                "pricing_option_id": "po_a",
                "targeting_overlay": {"property_list": PROPERTY_LIST},
            }
        ],
    )
    await handler.create_media_buy(req, ToolContext())

    assert len(backing.persist_calls) == 1
    account_id, request_dict, result_dict = backing.persist_calls[0]
    assert account_id == "acme:anonymous"
    # Framework normalized Pydantic → dict before crossing the adopter
    # boundary so the store contract is shape-stable. Pydantic's
    # ``mode='json'`` dump preserves explicit nulls and normalizes
    # ``AnyUrl`` to its string form — adopters see the same shape that
    # would go on the wire.
    assert isinstance(request_dict, dict)
    assert isinstance(result_dict, dict)
    persisted_property_list = request_dict["packages"][0]["targeting_overlay"]["property_list"]
    assert persisted_property_list["list_id"] == PROPERTY_LIST["list_id"]
    assert result_dict["media_buy_id"] == "mb_1"
    # Reference store actually persisted under the seller package_id.
    assert (
        backing.records["acme:anonymous"]["mb_1"]["seller_pkg_001"]["property_list"]["list_id"]
        == PROPERTY_LIST["list_id"]
    )


@pytest.mark.asyncio
async def test_create_media_buy_with_no_store_skips_persist(executor) -> None:
    """When no store is wired, the handler doesn't pay the
    ``on_complete`` hook cost — the create path is unchanged."""
    from adcp.types import (
        CreateMediaBuyRequest,
        CreateMediaBuySuccessResponse,
    )

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acme")

        async def create_media_buy(self, req, ctx):
            return CreateMediaBuySuccessResponse(
                media_buy_id="mb_1",
                confirmed_at="2026-05-01T00:00:00Z",
                revision=1,
                packages=[],
                status="active",
            )

    handler = _make_handler(_Platform(), executor, media_buy_store=None)

    resp = await handler.create_media_buy(
        CreateMediaBuyRequest(
            account={"account_id": "acct_a"},
            brand={"domain": "example.com"},
            idempotency_key="idem_aaaa1234567890",
            start_time="2026-05-01T00:00:00Z",
            end_time="2026-05-31T23:59:59Z",
        ),
        ToolContext(),
    )
    assert resp.media_buy_id == "mb_1"


@pytest.mark.asyncio
async def test_create_media_buy_noop_path_when_seller_lacks_specialism(executor) -> None:
    """Adopter wires a store but seller doesn't claim
    ``property-lists`` / ``collection-lists`` — the wrapper's no-op
    path short-circuits and the backing store is never touched."""
    from adcp.types import (
        CreateMediaBuyRequest,
        CreateMediaBuySuccessResponse,
        Package,
    )

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="acme")

        async def create_media_buy(self, req, ctx):
            return CreateMediaBuySuccessResponse(
                media_buy_id="mb_1",
                confirmed_at="2026-05-01T00:00:00Z",
                revision=1,
                packages=[Package(package_id="p1")],
                status="active",
            )

    backing = _RecordingMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_Platform.capabilities)
    handler = _make_handler(_Platform(), executor, media_buy_store=store)

    await handler.create_media_buy(
        CreateMediaBuyRequest(
            account={"account_id": "acct_a"},
            brand={"domain": "example.com"},
            idempotency_key="idem_aaaa1234567890",
            start_time="2026-05-01T00:00:00Z",
            end_time="2026-05-31T23:59:59Z",
            packages=[
                {
                    "product_id": "p",
                    "budget": 100.0,
                    "pricing_option_id": "po",
                    "targeting_overlay": {"property_list": PROPERTY_LIST},
                }
            ],
        ),
        ToolContext(),
    )
    assert backing.persist_calls == []
    assert backing.records == {}


# -----------------------------
# update_media_buy → merge_from_update
# -----------------------------


@pytest.mark.asyncio
async def test_update_media_buy_merges_overlay_through_handler(executor) -> None:
    """A successful ``update_media_buy`` invokes
    ``store.merge_from_update`` with the dict-normalized patch and the
    media_buy_id pulled off the request."""
    from adcp.types import UpdateMediaBuyRequest, UpdateMediaBuySuccessResponse

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["property-lists"])
        accounts = SingletonAccounts(account_id="acme")

        async def update_media_buy(self, media_buy_id, patch, ctx):
            return UpdateMediaBuySuccessResponse(
                media_buy_id=media_buy_id, revision=2, status="active", packages=[]
            )

    backing = _RecordingMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_Platform.capabilities)
    handler = _make_handler(_Platform(), executor, media_buy_store=store)

    req = UpdateMediaBuyRequest(
        account={"account_id": "acct_a"},
        media_buy_id="mb_1",
        idempotency_key="idem_bbbb1234567890",
        packages=[
            {
                "package_id": "seller_pkg_001",
                "targeting_overlay": {"property_list": PROPERTY_LIST},
            }
        ],
    )
    await handler.update_media_buy(req, ToolContext())

    assert len(backing.merge_calls) == 1
    account_id, media_buy_id, patch_dict = backing.merge_calls[0]
    assert account_id == "acme:anonymous"
    assert media_buy_id == "mb_1"
    assert isinstance(patch_dict, dict)
    merged_property_list = patch_dict["packages"][0]["targeting_overlay"]["property_list"]
    assert merged_property_list["list_id"] == PROPERTY_LIST["list_id"]
    assert (
        backing.records["acme:anonymous"]["mb_1"]["seller_pkg_001"]["property_list"]["list_id"]
        == PROPERTY_LIST["list_id"]
    )


# -----------------------------
# get_media_buys → backfill
# -----------------------------


@pytest.mark.asyncio
async def test_get_media_buys_backfills_overlay_through_handler(executor) -> None:
    """``get_media_buys`` calls ``store.backfill`` before returning; the
    persisted overlay is injected into packages the platform didn't
    echo itself."""
    from adcp.types import GetMediaBuysRequest

    backing = _RecordingMediaBuyStore()
    # Pre-populate the store as if a prior create_media_buy persisted.
    backing.records["acme:anonymous"] = {
        "mb_1": {"seller_pkg_001": {"property_list": PROPERTY_LIST}}
    }

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["property-lists"])
        accounts = SingletonAccounts(account_id="acme")

        async def get_media_buys(self, req, ctx):
            # Adopter returns dict-shaped response (valid per the wire
            # contract — handler.py allows dict OR Pydantic). Packages
            # carry no ``targeting_overlay`` so the store has work to
            # do.
            return {
                "media_buys": [
                    {
                        "media_buy_id": "mb_1",
                        "confirmed_at": "2026-05-01T00:00:00Z",
                        "revision": 1,
                        "packages": [{"package_id": "seller_pkg_001"}],
                    }
                ]
            }

    store = create_media_buy_store(backing, capabilities=_Platform.capabilities)
    handler = _make_handler(_Platform(), executor, media_buy_store=store)

    resp = await handler.get_media_buys(
        GetMediaBuysRequest(account={"account_id": "acct_a"}), ToolContext()
    )

    assert len(backing.backfill_calls) == 1
    backfill_account, _ = backing.backfill_calls[0]
    assert backfill_account == "acme:anonymous"
    # Response now carries the echoed overlay.
    assert resp["media_buys"][0]["packages"][0]["targeting_overlay"] == {
        "property_list": PROPERTY_LIST,
    }


@pytest.mark.asyncio
async def test_get_media_buys_with_no_store_returns_response_untouched(executor) -> None:
    """No store wired → response passes through verbatim."""
    from adcp.types import GetMediaBuysRequest

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acme")

        async def get_media_buys(self, req, ctx):
            return {
                "media_buys": [
                    {
                        "media_buy_id": "mb_1",
                        "confirmed_at": "2026-05-01T00:00:00Z",
                        "revision": 1,
                        "packages": [{"package_id": "p1"}],
                    }
                ]
            }

    handler = _make_handler(_Platform(), executor, media_buy_store=None)
    resp = await handler.get_media_buys(
        GetMediaBuysRequest(account={"account_id": "acct_a"}), ToolContext()
    )
    assert "targeting_overlay" not in resp["media_buys"][0]["packages"][0]


# -----------------------------
# End-to-end persist → backfill round-trip
# -----------------------------


@pytest.mark.asyncio
async def test_persist_then_backfill_round_trip(executor) -> None:
    """The full spec flow: ``create_media_buy`` persists the overlay,
    a subsequent ``get_media_buys`` echoes it. Exercises both shims via
    the same handler with one wired store, like a real adopter."""
    from adcp.types import (
        CreateMediaBuyRequest,
        CreateMediaBuySuccessResponse,
        GetMediaBuysRequest,
        Package,
    )

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["property-lists"])
        accounts = SingletonAccounts(account_id="acme")

        async def create_media_buy(self, req, ctx):
            return CreateMediaBuySuccessResponse(
                media_buy_id="mb_42",
                confirmed_at="2026-05-01T00:00:00Z",
                revision=1,
                packages=[Package(package_id="seller_pkg_42")],
                status="active",
            )

        async def get_media_buys(self, req, ctx):
            return {
                "media_buys": [
                    {
                        "media_buy_id": "mb_42",
                        "confirmed_at": "2026-05-01T00:00:00Z",
                        "revision": 1,
                        "packages": [{"package_id": "seller_pkg_42"}],
                    }
                ]
            }

    backing = _RecordingMediaBuyStore()
    store = create_media_buy_store(backing, capabilities=_Platform.capabilities)
    handler = _make_handler(_Platform(), executor, media_buy_store=store)

    await handler.create_media_buy(
        CreateMediaBuyRequest(
            account={"account_id": "acct_a"},
            brand={"domain": "example.com"},
            idempotency_key="idem_cccc1234567890",
            start_time="2026-05-01T00:00:00Z",
            end_time="2026-05-31T23:59:59Z",
            packages=[
                {
                    "product_id": "p",
                    "buyer_ref": "pkg",
                    "budget": 1000.0,
                    "pricing_option_id": "po",
                    "targeting_overlay": {"property_list": PROPERTY_LIST},
                }
            ],
        ),
        ToolContext(),
    )

    resp = await handler.get_media_buys(
        GetMediaBuysRequest(account={"account_id": "acct_a"}), ToolContext()
    )
    echoed = resp["media_buys"][0]["packages"][0]["targeting_overlay"]
    assert echoed["property_list"]["list_id"] == PROPERTY_LIST["list_id"]
