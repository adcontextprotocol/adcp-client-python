from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from adcp.compat import (
    CompatibilityContinuationError,
    CoordinatorBuyProductsInput,
    InMemoryCompatibilityContinuationStore,
    LegacyPurchaseCoordinator,
    MediaBuyCompatibility,
    MediaBuyLifecycle,
    MediaBuyLifecycleCompatibilityError,
    MediaBuyLifecycleCoordinator,
    ReconciliationResult,
)
from adcp.compat.negotiated_catalog_purchase import _negotiate_version
from adcp.server.mcp_tools import _normalize_response_envelope
from adcp.types import (
    ListProductsRequest,
)
from adcp.types.core import TaskResult, TaskStatus
from adcp.types.legacy import LegacyGetProductsResponse

_VECTORS = (
    Path(__file__).parent
    / "conformance"
    / "vectors"
    / "products-only-brief-compatibility"
    / "vectors.json"
)
_CASES = json.loads(_VECTORS.read_text())["cases"]
_NOW = datetime(2098, 1, 1, tzinfo=timezone.utc)
_LEGACY_MUTATION_LOSS = "mutation_idempotency_not_guaranteed"


def test_coordinator_purchase_input_excludes_seller_version_evidence() -> None:
    fields = CoordinatorBuyProductsInput.__annotations__

    assert {"idempotency_key", "account", "purchases", "start_time", "end_time"} <= fields.keys()
    assert {
        "adcp_major_version",
        "adcp_version",
        "feed_version",
        "pricing_version",
    }.isdisjoint(fields)


def test_compact_list_response_does_not_gain_task_status() -> None:
    response: dict[str, Any] = {"products": [], "feed_version": "feed-1"}

    _normalize_response_envelope(
        "list_products",
        response,
        {},
        adcp_version="3.2-rc.1",
    )

    assert response == {
        "outcome": "listed",
        "products": [],
        "feed_version": "feed-1",
    }


class _Payload(BaseModel):
    model_config = ConfigDict(extra="allow")


class _Client:
    def __init__(self, version: str, response: dict[str, Any]) -> None:
        self.version = version
        self.response = response
        self.list_requests: list[Any] = []
        self.buy_requests: list[Any] = []
        self.legacy_requests: list[Any] = []
        self.create_requests: list[Any] = []

    def get_adcp_version(self) -> str:
        return self.version

    async def list_products(self, request: Any) -> TaskResult[Any]:
        self.list_requests.append(request)
        return _completed(self.response)

    async def buy_products(self, request: Any) -> TaskResult[Any]:
        self.buy_requests.append(request)
        return _completed({"media_buy_id": "mb-compact"})

    async def get_products_legacy(self, request: Any) -> TaskResult[Any]:
        self.legacy_requests.append(request)
        return _completed(self.response)

    async def create_media_buy_legacy(self, request: Any) -> TaskResult[Any]:
        self.create_requests.append(request)
        return _completed({"media_buy_id": "mb-established", "packages": []})


def _completed(payload: dict[str, Any]) -> TaskResult[Any]:
    return TaskResult(status=TaskStatus.COMPLETED, data=_Payload.model_validate(payload))


def _caps(
    versions: list[str] | None,
    *,
    served: str | None = None,
    tools: list[str] | None = None,
    buying_modes: list[str] | None = None,
    idempotent: bool = True,
) -> Any:
    return SimpleNamespace(
        adcp_version=served,
        adcp=SimpleNamespace(
            supported_versions=versions,
            idempotency=SimpleNamespace(
                supported=idempotent,
                replay_ttl_seconds=86400 if idempotent else None,
            ),
        ),
        media_buy=SimpleNamespace(
            lifecycle_tools=tools,
            buying_modes=(
                ["brief", "wholesale", "refine"] if buying_modes is None else buying_modes
            ),
        ),
    )


def _list_request() -> ListProductsRequest:
    return ListProductsRequest.model_validate(
        {
            "idempotency_key": "catalog-listing-key-0001",
            "account": {"account_id": "account-acme"},
            "criteria": {"offer_filters": {"countries": ["US"]}},
            "max_results": 25,
        }
    )


def _purchase(product_id: str) -> dict[str, Any]:
    return {
        "idempotency_key": "5f203b58-9ae7-4d02-b05f-eb239a47f065",
        "account": {"account_id": "account-acme"},
        "brand": {"domain": "acme.example"},
        "purchases": [
            {
                "product_id": product_id,
                "pricing_option_id": "fixed-cpm",
                "budget": 1000,
            }
        ],
        "start_time": "2099-01-01T00:00:00Z",
        "end_time": "2099-02-01T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("client_version", "versions", "served", "expected"),
    [
        ("3.2-rc.1", None, None, "3.0"),
        ("3.2-rc.1", ["3.0", "3.1"], None, "3.1"),
        ("3.2-rc.1", ["3.1-rc.9", "3.1-rc.10"], None, "3.1-rc.10"),
        ("3.2-rc.1", ["3.0", "3.2-rc.1"], "3.0", "3.0"),
    ],
)
def test_negotiation_uses_served_contract_then_stable_minor_downshift(
    client_version: str,
    versions: list[str] | None,
    served: str | None,
    expected: str,
) -> None:
    assert _negotiate_version(_caps(versions, served=served), client_version) == expected


def test_negotiation_rejects_served_version_newer_than_client_pin() -> None:
    with pytest.raises(ValueError, match="newer than client pin"):
        _negotiate_version(_caps(["3.2"], served="3.2"), "3.2-rc.1")


@pytest.mark.parametrize(
    ("versions", "served", "message"),
    [
        (["2.5"], "2.5", "cross-major"),
        (["3.0"], "3.1", "absent from its supported_versions"),
        (["3.0", "3.1-rc.12"], "3.1-rc.12", "No bundled schema"),
    ],
)
def test_negotiation_rejects_unusable_served_contracts(
    versions: list[str], served: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _negotiate_version(_caps(versions, served=served), "3.2-rc.1")


def test_negotiation_downshifts_past_an_unbundled_advertised_prerelease() -> None:
    assert _negotiate_version(_caps(["3.0", "3.1-rc.12"]), "3.2-rc.1") == "3.0"


def test_compact_contract_requires_an_advertised_route() -> None:
    client = _Client("3.2-rc.1", {})
    coordinator = MediaBuyLifecycleCoordinator(client, _caps(["3.2-rc.1"]), [])

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as exc:
        coordinator.report("list_products")

    assert exc.value.feature == "catalog_purchase_route_not_advertised"


@pytest.mark.parametrize(
    ("version", "tools", "expected"),
    [
        ("3.0", ["get_products", "create_media_buy"], MediaBuyLifecycle.ESTABLISHED),
        ("3.1", ["get_products", "create_media_buy"], MediaBuyLifecycle.ESTABLISHED),
        ("3.2-rc.1", ["list_products", "buy_products"], MediaBuyLifecycle.COMPACT),
    ],
)
def test_routing_matrix_is_separate_from_purchase_projection(
    version: str, tools: list[str], expected: MediaBuyLifecycle
) -> None:
    coordinator = MediaBuyLifecycleCoordinator(
        _Client("3.2-rc.1", {}),
        _caps([version], tools=tools),
        tools,
    )
    assert coordinator.lifecycle is expected


@pytest.mark.asyncio
async def test_compact_list_buy_preserves_real_versions_and_country_filter() -> None:
    response = {
        "products": [{"product_id": "compact-product", "name": "Compact product"}],
        "feed_version": "seller-feed-1",
        "pricing_version": "seller-price-1",
        "cache_scope": "account",
    }
    client = _Client("3.2-rc.1", response)
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(
            ["3.0", "3.1", "3.2-rc.1"],
            served="3.2-rc.1",
            tools=["list_products", "buy_products"],
        ),
        ["list_products", "buy_products"],
    )

    listing = await coordinator.list_products(_list_request())
    purchase_request = _purchase("compact-product")
    purchase_request.update(
        {"feed_version": "caller-feed-must-not-win", "pricing_version": "caller-price-must-not-win"}
    )
    # Top-level Pydantic inputs are deeply serialized before coordinator preflight.
    purchase = await coordinator.buy_products(listing, _Payload.model_validate(purchase_request))

    assert listing.feed_version == "seller-feed-1"
    assert listing.compatibility.lifecycle is MediaBuyLifecycle.COMPACT
    assert listing.compatibility.compatibility is MediaBuyCompatibility.NATIVE
    assert client.list_requests[0].criteria.offer_filters.countries[0].root == "US"
    sent = client.buy_requests[0]
    assert sent.feed_version == "seller-feed-1"
    assert sent.pricing_version == "seller-price-1"
    assert purchase.compatibility.tools_used == ("buy_products",)


@pytest.mark.asyncio
async def test_compact_buy_drops_caller_pricing_when_listing_has_no_pricing_version() -> None:
    client = _Client(
        "3.2-rc.1",
        {
            "products": [{"product_id": "compact-product", "name": "Compact product"}],
            "feed_version": "seller-feed-1",
            "cache_scope": "account",
        },
    )
    tools = ["list_products", "buy_products"]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.2-rc.1"], served="3.2-rc.1", tools=tools),
        tools,
    )
    listing = await coordinator.list_products(_list_request())
    purchase_request = _purchase("compact-product")
    purchase_request["pricing_version"] = "caller-price-must-be-removed"

    await coordinator.buy_products(listing, purchase_request)

    assert client.buy_requests[0].pricing_version is None


@pytest.mark.asyncio
async def test_catalog_route_rejects_a_partial_compact_pair_before_listing() -> None:
    client = _Client("3.2-rc.1", {})
    tools = ["list_products", "get_products", "create_media_buy"]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.2-rc.1"], served="3.2-rc.1", tools=tools),
        tools,
    )

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as exc:
        await coordinator.list_products(_list_request())

    assert exc.value.feature == "catalog_purchase_route_not_advertised"
    assert not client.list_requests
    assert not client.legacy_requests


@pytest.mark.asyncio
async def test_established_catalog_requires_tools_and_wholesale_mode_before_dispatch() -> None:
    request = _list_request()
    for tools, modes in [([], ["wholesale"]), (["get_products", "create_media_buy"], ["brief"])]:
        client = _Client("3.2-rc.1", {})
        coordinator = MediaBuyLifecycleCoordinator(
            client,
            _caps(["3.1"], tools=tools, buying_modes=modes),
            tools,
        )

        with pytest.raises(MediaBuyLifecycleCompatibilityError) as exc:
            await coordinator.list_products(request)

        assert exc.value.feature == "catalog_purchase_route_not_advertised"
        assert not client.legacy_requests


def test_adcp_30_does_not_require_the_later_buying_modes_capability() -> None:
    tools = ["get_products", "create_media_buy"]
    coordinator = MediaBuyLifecycleCoordinator(
        _Client("3.2-rc.1", {}),
        _caps(["3.0"], tools=tools, buying_modes=[]),
        tools,
    )

    assert coordinator._select_lifecycle("list_products") is MediaBuyLifecycle.ESTABLISHED


@pytest.mark.asyncio
async def test_list_rejects_conditional_cache_reuse_before_dispatch() -> None:
    client = _Client("3.2-rc.1", {})
    tools = ["list_products", "buy_products"]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.2-rc.1"], served="3.2-rc.1", tools=tools),
        tools,
    )
    request = _list_request().model_copy(update={"if_feed_version": "feed-previous"})

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as exc:
        await coordinator.list_products(request)

    assert exc.value.feature == "conditional_catalog_reuse"
    assert not client.list_requests


@pytest.mark.asyncio
async def test_empty_compact_catalog_is_a_valid_completed_listing() -> None:
    client = _Client(
        "3.2-rc.1", {"products": [], "feed_version": "feed-empty", "cache_scope": "account"}
    )
    tools = ["list_products", "buy_products"]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.2-rc.1"], served="3.2-rc.1", tools=tools),
        tools,
    )

    listing = await coordinator.list_products(_list_request())

    assert listing.products == ()
    assert listing.feed_version == "feed-empty"


@pytest.mark.asyncio
async def test_empty_established_catalog_has_no_purchase_continuation() -> None:
    client = _Client("3.2-rc.1", {"products": []})
    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=lambda _execution: {},
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    tools = ["get_products", "create_media_buy"]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"], tools=tools),
        tools,
        legacy_purchase_coordinator=legacy,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=["feed_version_not_atomic", "pricing_version_not_atomic"],
        clock=lambda: _NOW,
    )

    listing = await coordinator.list_products(_list_request())

    assert listing.products == ()
    assert listing.purchase_continuation is None


def test_completed_payload_does_not_invent_newer_response_defaults() -> None:
    client = _Client("3.2-rc.1", {})
    tools = ["get_products", "create_media_buy"]
    coordinator = MediaBuyLifecycleCoordinator(client, _caps(["3.0"], tools=tools), tools)
    wire = copy.deepcopy(_CASES[1]["legacy_response"])
    typed = LegacyGetProductsResponse.model_validate(wire)

    payload = coordinator._completed_payload(
        "list_products",
        TaskResult(status=TaskStatus.COMPLETED, data=typed),
        MediaBuyLifecycle.ESTABLISHED,
    )

    assert "cache_scope" not in payload
    assert "replayed" not in payload
    assert "status" not in payload


@pytest.mark.asyncio
async def test_explicit_task_served_version_must_match_negotiation() -> None:
    client = _Client(
        "3.2-rc.1",
        {
            "adcp_version": "3.1",
            "products": [{"product_id": "wrong-contract", "name": "Wrong contract"}],
            "feed_version": "seller-feed-1",
        },
    )
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(
            ["3.2-rc.1"],
            served="3.2-rc.1",
            tools=["list_products", "buy_products"],
        ),
        ["list_products", "buy_products"],
    )

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as exc:
        await coordinator.list_products(_list_request())

    assert exc.value.feature == "served_version_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "case_index"),
    [("3.0", 1), ("3.1", 2)],
)
async def test_established_tokenless_list_buy_is_bound_and_replays(
    version: str, case_index: int
) -> None:
    case = copy.deepcopy(_CASES[case_index])
    response = case["legacy_response"]
    if version == "3.1":
        response["adcp_version"] = version
    calls: list[Any] = []

    async def execute(execution: Any) -> dict[str, Any]:
        calls.append(execution)
        if execution.source_adcp_version.startswith("3.1."):
            return {
                "media_buy_id": "mb-established",
                "confirmed_at": "2098-01-01T00:00:00Z",
                "revision": 1,
                "packages": [],
            }
        return {"media_buy_id": "mb-established", "packages": []}

    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=execute,
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    client = _Client("3.2-rc.1", response)
    losses = [
        "feed_version_not_atomic",
        "pricing_version_not_atomic",
        "mutation_idempotency_not_guaranteed",
    ]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps([version], idempotent=False),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=losses,
        clock=lambda: _NOW,
    )

    listing = await coordinator.list_products(
        _list_request(), issuance_idempotency_key=f"listing-{version}"
    )
    product_id = f"brief-display-{version.replace('.', '')}"
    purchase_request = _purchase(product_id)
    purchase_request["adcp_version"] = "3.2-rc.1"
    purchase_request["adcp_major_version"] = 3
    first = await coordinator.buy_products(listing, purchase_request)
    replay = await coordinator.buy_products(listing, purchase_request)

    sent_list = client.legacy_requests[0].model_dump(mode="json", exclude_none=True)
    assert sent_list["filters"]["countries"] == ["US"]
    assert listing.feed_version is None
    assert listing.pricing_version is None
    assert listing.purchase_continuation is not None
    assert listing.purchase_continuation.losses == tuple(losses)
    assert first.result == replay.result
    assert first.compatibility.losses == tuple(losses)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_established_purchase_rejects_binding_and_loss_mismatch_before_dispatch() -> None:
    case = copy.deepcopy(_CASES[1])
    calls = 0

    async def execute(_execution: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"media_buy_id": "should-not-run", "packages": []}

    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=execute,
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    coordinator = MediaBuyLifecycleCoordinator(
        _Client("3.2-rc.1", case["legacy_response"]),
        _caps(["3.0"], idempotent=False),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=[
            "feed_version_not_atomic",
            "pricing_version_not_atomic",
            "mutation_idempotency_not_guaranteed",
        ],
        clock=lambda: _NOW,
    )
    listing = await coordinator.list_products(_list_request())
    wrong_account = _purchase("brief-display-30")
    wrong_account["account"] = {"account_id": "other-account"}
    missing_selection = _purchase("brief-display-30")
    missing_selection.pop("purchases")

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as account_error:
        await coordinator.buy_products(listing, wrong_account)
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as selection_error:
        await coordinator.buy_products(listing, missing_selection)
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as loss_error:
        await coordinator.buy_products(listing, _purchase("brief-display-30"), accepted_losses=[])

    assert account_error.value.feature == "account_binding"
    assert selection_error.value.feature == "purchase_selection"
    assert loss_error.value.feature == "accepted_losses"
    assert calls == 0


@pytest.mark.asyncio
async def test_established_catalog_continuation_redeems_after_coordinator_restart() -> None:
    calls: list[Any] = []

    async def execute(execution: Any) -> dict[str, Any]:
        calls.append(execution)
        return {"media_buy_id": "mb-restarted-purchase", "packages": []}

    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=execute,
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    client = _Client("3.2-rc.1", copy.deepcopy(_CASES[1]["legacy_response"]))
    losses = [
        "feed_version_not_atomic",
        "pricing_version_not_atomic",
        "mutation_idempotency_not_guaranteed",
    ]

    def coordinator() -> MediaBuyLifecycleCoordinator:
        return MediaBuyLifecycleCoordinator(
            client,
            _caps(["3.0"], idempotent=False),
            ["get_products", "create_media_buy"],
            legacy_purchase_coordinator=legacy,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
            allowed_losses=losses,
            clock=lambda: _NOW,
        )

    listing = await coordinator().list_products(
        _list_request(), issuance_idempotency_key="restartable-listing-3.0"
    )
    assert listing.purchase_continuation is not None

    first = await coordinator().continue_legacy_purchase(
        listing.purchase_continuation.token,
        _purchase("brief-display-30"),
        accepted_losses=losses,
    )
    replay = await coordinator().continue_legacy_purchase(
        listing.purchase_continuation.token,
        _purchase("brief-display-30"),
        accepted_losses=losses,
    )

    assert first.success and replay.success
    assert first.result == replay.result
    assert first.compatibility.losses == tuple(losses)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_established_route_rejects_undeclared_requirements_before_dispatch() -> None:
    case = copy.deepcopy(_CASES[1])
    client = _Client("3.2-rc.1", case["legacy_response"])
    calls = 0

    async def execute(_execution: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"media_buy_id": "should-not-run", "packages": []}

    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=execute,
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=["feed_version_not_atomic", "pricing_version_not_atomic"],
        clock=lambda: _NOW,
    )
    unsupported_listing = _list_request().model_copy(
        update={
            "criteria": _list_request().criteria.model_copy(
                update={
                    "offer_filters": _list_request().criteria.offer_filters.model_copy(
                        update={"pricing_structures": ["auction"]}
                    )
                }
            )
        }
    )

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as listing_error:
        await coordinator.list_products(unsupported_listing)
    assert listing_error.value.feature == "legacy_request_projection"
    assert client.legacy_requests == []

    listing = await coordinator.list_products(_list_request())
    unsupported_purchase = _purchase("brief-display-30")
    unsupported_purchase["daily_budget_cap"] = 100
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as purchase_error:
        await coordinator.buy_products(listing, unsupported_purchase)
    assert purchase_error.value.feature == "legacy_request_projection"
    assert calls == 0


@pytest.mark.asyncio
async def test_established_listing_preserves_seller_fields_separately_from_routing() -> None:
    case = copy.deepcopy(_CASES[1])
    case["legacy_response"]["products"][0]["seller_extension"] = {"tier": "gold"}
    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=lambda _execution: {},
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    coordinator = MediaBuyLifecycleCoordinator(
        _Client("3.2-rc.1", case["legacy_response"]),
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        clock=lambda: _NOW,
    )

    listing = await coordinator.list_products(_list_request())

    assert listing.products[0]["seller_extension"] == {"tier": "gold"}


@pytest.mark.asyncio
async def test_established_ambiguous_purchase_recovers_through_existing_reconciler() -> None:
    case = copy.deepcopy(_CASES[1])

    async def execute(_execution: Any) -> dict[str, Any]:
        raise TimeoutError("seller completion was not observed")

    async def reconcile(_execution: Any, _operation: Any) -> ReconciliationResult:
        return ReconciliationResult.applied({"media_buy_id": "mb-recovered", "packages": []})

    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=execute,
        reconciler=reconcile,
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    coordinator = MediaBuyLifecycleCoordinator(
        _Client("3.2-rc.1", case["legacy_response"]),
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=["feed_version_not_atomic", "pricing_version_not_atomic"],
        clock=lambda: _NOW,
    )
    listing = await coordinator.list_products(_list_request())

    with pytest.raises(CompatibilityContinuationError):
        await coordinator.buy_products(listing, _purchase("brief-display-30"))
    operation = await legacy.get_legacy_purchase_operation_by_idempotency_key(
        "5f203b58-9ae7-4d02-b05f-eb239a47f065",
        principal_id="principal-acme",
    )
    recovered = await coordinator.recover_legacy_purchase(operation)

    assert recovered.result["media_buy_id"] == "mb-recovered"
    assert recovered.compatibility.losses == (
        "feed_version_not_atomic",
        "pricing_version_not_atomic",
    )
