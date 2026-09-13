from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from adcp.compat import (
    ESTABLISHED_PROPOSAL_MAX_PRINCIPAL_BYTES,
    ESTABLISHED_PROPOSAL_MAX_SNAPSHOT_BYTES,
    CompatibilityContinuationError,
    CompatibleTaskResult,
    EstablishedProposalEvidence,
    InMemoryCompatibilityContinuationStore,
    InMemoryEstablishedProposalEvidenceStore,
    LegacyPurchaseCoordinator,
    MediaBuyCompatibility,
    MediaBuyLifecycle,
    MediaBuyLifecycleCompatibilityError,
    MediaBuyLifecycleCoordinator,
    ProposalMutationKind,
    ProposalStoreCapacityError,
    ReconciliationResult,
)
from adcp.compat.negotiated_catalog_purchase import _negotiate_version
from adcp.negotiation import compute_terms_digest
from adcp.server.mcp_tools import _normalize_response_envelope
from adcp.types import (
    ControlMediaBuyRequest,
    DeclineProposalsRequest,
    GetMediaBuyDeliveryRequest,
    GetMediaBuysRequest,
    ListProductsRequest,
    RefineProposalsRequest,
    RequestProposalsRequest,
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
        self.proposal_requests: list[Any] = []
        self.accept_requests: list[Any] = []
        self.create_requests: list[Any] = []
        self.refine_requests: list[Any] = []
        self.decline_requests: list[Any] = []
        self.control_requests: list[Any] = []
        self.update_requests: list[Any] = []
        self.media_buy_read_requests: list[Any] = []
        self.legacy_media_buy_read_requests: list[Any] = []
        self.delivery_read_requests: list[Any] = []
        self.legacy_delivery_read_requests: list[Any] = []
        self.task_status_results: list[TaskResult[Any]] = []
        self.proposal_result: TaskResult[Any] | None = None

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

    async def request_proposals(self, request: Any) -> TaskResult[Any]:
        self.proposal_requests.append(request)
        return self.proposal_result or _completed(self.response)

    async def accept_proposal(self, request: Any) -> TaskResult[Any]:
        self.accept_requests.append(request)
        return _completed({"media_buy_id": "mb-compact", "packages": []})

    async def create_media_buy_legacy(self, request: Any) -> TaskResult[Any]:
        self.create_requests.append(request)
        return _completed({"media_buy_id": "mb-established", "packages": []})

    async def refine_proposals(self, request: Any) -> TaskResult[Any]:
        self.refine_requests.append(request)
        return _completed(self.response)

    async def decline_proposals(self, request: Any) -> TaskResult[Any]:
        self.decline_requests.append(request)
        return _completed(self.response)

    async def control_media_buy(self, request: Any) -> TaskResult[Any]:
        self.control_requests.append(request)
        return _completed(self.response)

    async def update_media_buy_legacy(self, request: Any) -> TaskResult[Any]:
        self.update_requests.append(request)
        return _completed(self.response)

    async def get_media_buys(self, request: Any) -> TaskResult[Any]:
        self.media_buy_read_requests.append(request)
        return _completed(self.response)

    async def get_media_buys_legacy(self, request: Any) -> TaskResult[Any]:
        self.legacy_media_buy_read_requests.append(request)
        return _completed(self.response)

    async def get_media_buy_delivery(self, request: Any) -> TaskResult[Any]:
        self.delivery_read_requests.append(request)
        return _completed(self.response)

    async def get_media_buy_delivery_legacy(self, request: Any) -> TaskResult[Any]:
        self.legacy_delivery_read_requests.append(request)
        return _completed(self.response)

    async def get_task_status(self, _request: Any) -> TaskResult[Any]:
        return self.task_status_results.pop(0)


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


def _proposal_request() -> RequestProposalsRequest:
    return RequestProposalsRequest.model_validate(
        {
            "idempotency_key": "proposal-request-0001",
            "account": {"account_id": "account-acme"},
            "brand": {"domain": "acme.example"},
            "brief": "A premium display campaign for Acme.",
            "criteria": {"offer_filters": {"countries": ["US"]}},
        }
    )


def _compact_proposal_response() -> dict[str, Any]:
    product = copy.deepcopy(_CASES[1]["legacy_response"]["products"][0])
    product.pop("format_ids")
    terms = {
        "brand": {"domain": "acme.example"},
        "purchases": [
            {
                "product_id": product["product_id"],
                "pricing_option_id": "fixed-cpm",
                "pricing": product["pricing_options"][0],
                "budget": 1000,
                "start_time": "2099-01-01T00:00:00Z",
                "end_time": "2099-02-01T00:00:00Z",
            }
        ],
        "start_time": "2099-01-01T00:00:00Z",
        "end_time": "2099-02-01T00:00:00Z",
        "total_budget": {"amount": 1000, "currency": "USD"},
    }
    return {
        "status": "completed",
        "outcome": "proposed",
        "proposals": [
            {
                "proposal_id": "compact-proposal-1",
                "proposal_kind": "new_media_buy",
                "proposal_status": "draft",
                "expires_at": "2098-02-01T00:00:00Z",
                "name": "Compact plan",
                "commercial_terms": terms,
                "terms_digest": compute_terms_digest(terms),
            }
        ],
        "products": [product],
        "replayed": True,
    }


def _legacy_proposal_response(case_index: int = 1) -> dict[str, Any]:
    response = copy.deepcopy(_CASES[case_index]["legacy_response"])
    product_id = response["products"][0]["product_id"]
    response["proposals"] = [
        {
            "proposal_id": "legacy-proposal-1",
            "name": "Legacy plan",
            "allocations": [{"product_id": product_id, "allocation_percentage": 100}],
        }
    ]
    return response


def _accept_request(*, digest: str | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {
        "idempotency_key": "proposal-acceptance-key-0001",
        "account": {"account_id": "account-acme"},
        "proposal_id": "legacy-proposal-1",
        "total_budget": {"amount": 1000, "currency": "USD"},
    }
    if digest is not None:
        request["proposal_terms_digest"] = digest
    return request


def _established_fallback() -> dict[str, Any]:
    return {
        "brand": {"domain": "acme.example"},
        "start_time": "2099-01-01T00:00:00Z",
        "end_time": "2099-02-01T00:00:00Z",
    }


def _refine_request(proposal_id: str = "legacy-proposal-1") -> RefineProposalsRequest:
    return RefineProposalsRequest.model_validate(
        {
            "idempotency_key": "proposal-refinement-key-0001",
            "refinements": [{"proposal_id": proposal_id, "action": "revise", "ask": "More video"}],
        }
    )


def _decline_request(proposal_id: str = "legacy-proposal-1") -> DeclineProposalsRequest:
    return DeclineProposalsRequest.model_validate(
        {
            "idempotency_key": "proposal-decline-key-0001",
            "declines": [
                {"proposal_id": proposal_id, "reason": "price", "detail": "Too expensive"}
            ],
        }
    )


def _control_request(**updates: Any) -> ControlMediaBuyRequest:
    payload: dict[str, Any] = {
        "idempotency_key": "media-buy-control-key-0001",
        "account": {"account_id": "account-acme"},
        "media_buy_id": "mb-1",
        "revision": 7,
        "paused": True,
    }
    payload.update(updates)
    return ControlMediaBuyRequest.model_validate(payload)


def _delivery_response() -> dict[str, Any]:
    return {
        "reporting_period": {
            "start": "2098-01-01T00:00:00Z",
            "end": "2098-01-02T00:00:00Z",
        },
        "currency": "USD",
        "media_buy_deliveries": [],
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
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as exc:
        MediaBuyLifecycleCoordinator(client, _caps(["3.2-rc.1"]), [])
    assert exc.value.feature == "lifecycle_tool_not_advertised"


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
    purchase = await coordinator.buy_products(listing, purchase_request)

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


@pytest.mark.asyncio
async def test_compact_proposal_request_preserves_native_outcome_and_digest() -> None:
    client = _Client("3.2-rc.1", _compact_proposal_response())
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(
            ["3.2-rc.1"],
            served="3.2-rc.1",
            tools=["list_products", "buy_products", "request_proposals"],
        ),
        ["list_products", "buy_products", "request_proposals"],
    )

    result = await coordinator.request_proposals(_proposal_request())

    assert isinstance(result, CompatibleTaskResult)
    assert result.success
    assert result.data is not None
    assert result.data.outcome == "proposed"
    assert result.data.proposals[0]["proposal_id"] == "compact-proposal-1"
    assert result.compatibility.tools_used == ("request_proposals",)
    assert len(client.proposal_requests) == 1
    assert client.legacy_requests == []


@pytest.mark.asyncio
async def test_compact_proposal_digest_mismatch_fails_closed() -> None:
    response = _compact_proposal_response()
    response["proposals"][0]["terms_digest"] = "sha256:invalid"
    client = _Client("3.2-rc.1", response)
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(
            ["3.2-rc.1"],
            served="3.2-rc.1",
            tools=["list_products", "buy_products", "request_proposals"],
        ),
        ["list_products", "buy_products", "request_proposals"],
    )

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as exc:
        await coordinator.request_proposals(_proposal_request())

    assert exc.value.feature == "proposal_terms_digest"
    assert exc.value.code == "PROPOSAL_DIGEST_MISMATCH"


@pytest.mark.asyncio
async def test_compact_accept_proposal_preserves_native_signature() -> None:
    response = _compact_proposal_response()
    proposal = response["proposals"][0]
    client = _Client("3.2-rc.1", response)
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(
            ["3.2-rc.1"],
            served="3.2-rc.1",
            tools=["list_products", "request_proposals", "accept_proposal"],
        ),
        ["list_products", "request_proposals", "accept_proposal"],
    )
    request = {
        "idempotency_key": "proposal-acceptance-key-0001",
        "account": {"account_id": "account-acme"},
        "proposal_id": proposal["proposal_id"],
        "proposal_terms_digest": proposal["terms_digest"],
    }

    result = await coordinator.accept_proposal(request)

    assert result.success
    assert result.compatibility.tools_used == ("accept_proposal",)
    assert len(client.accept_requests) == 1
    assert client.create_requests == []


@pytest.mark.asyncio
async def test_established_acceptance_requires_durable_store_by_default() -> None:
    client = _Client("3.2-rc.1", _legacy_proposal_response())
    store = InMemoryEstablishedProposalEvidenceStore()
    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=lambda _execution: {},
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=[
            "proposal_terms_digest_not_enforced",
            "proposal_terms_digest_unavailable",
            "proposal_snapshot_not_immutable",
            "proposal_hold_not_verifiable",
        ],
        clock=lambda: _NOW,
    )
    await coordinator.request_proposals(_proposal_request())

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as exc:
        await coordinator.accept_proposal(
            _accept_request(), established_fallback=_established_fallback()
        )

    assert exc.value.feature == "durable_acceptance_store"
    assert client.create_requests == []


@pytest.mark.asyncio
async def test_established_acceptance_reserves_once_and_replays_result() -> None:
    client = _Client("3.2-rc.1", _legacy_proposal_response())
    store = InMemoryEstablishedProposalEvidenceStore()
    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=lambda _execution: {},
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    losses = [
        "proposal_terms_digest_not_enforced",
        "proposal_terms_digest_unavailable",
        "proposal_snapshot_not_immutable",
        "proposal_hold_not_verifiable",
    ]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=losses,
        allow_non_durable_proposal_acceptance=True,
        clock=lambda: _NOW,
    )
    await coordinator.request_proposals(_proposal_request())

    first = await coordinator.accept_proposal(
        _accept_request(),
        established_fallback=_established_fallback(),
        accepted_losses=losses,
    )
    replay = await coordinator.accept_proposal(
        _accept_request(),
        established_fallback=_established_fallback(),
        accepted_losses=losses,
    )

    assert first.success and replay.success
    assert len(client.create_requests) == 1
    wire = client.create_requests[0].model_dump(mode="json", exclude_none=True)
    assert wire["proposal_id"] == "legacy-proposal-1"
    assert wire["brand"] == {"domain": "acme.example"}
    assert replay.compatibility.losses == tuple(losses)
    assert replay.compatibility.warnings == ("Replayed the durable buyer-side acceptance result.",)


@pytest.mark.asyncio
async def test_established_acceptance_recovers_submitted_task_after_restart() -> None:
    class SubmittedAcceptanceClient(_Client):
        async def create_media_buy_legacy(self, request: Any) -> TaskResult[Any]:
            self.create_requests.append(request)
            return TaskResult(
                status=TaskStatus.SUBMITTED,
                data=_Payload.model_validate(
                    {"status": "submitted", "task_id": "legacy-accept-task-restart"}
                ),
                metadata={"task_id": "legacy-accept-task-restart"},
            )

    losses = [
        "proposal_terms_digest_not_enforced",
        "proposal_terms_digest_unavailable",
        "proposal_snapshot_not_immutable",
        "proposal_hold_not_verifiable",
    ]
    client = SubmittedAcceptanceClient("3.2-rc.1", _legacy_proposal_response())
    store = InMemoryEstablishedProposalEvidenceStore()

    def coordinator() -> MediaBuyLifecycleCoordinator:
        return MediaBuyLifecycleCoordinator(
            client,
            _caps(["3.0"]),
            ["get_products", "create_media_buy"],
            proposal_evidence_store=store,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
            allowed_losses=losses,
            allow_non_durable_proposal_acceptance=True,
            clock=lambda: _NOW,
        )

    first = coordinator()
    await first.request_proposals(_proposal_request())
    initial = await first.accept_proposal(
        _accept_request(),
        established_fallback=_established_fallback(),
        accepted_losses=losses,
    )
    retry = await coordinator().accept_proposal(
        _accept_request(),
        established_fallback=_established_fallback(),
        accepted_losses=losses,
    )
    client.task_status_results.append(
        _completed(
            {
                "task_id": "legacy-accept-task-restart",
                "task_type": "create_media_buy",
                "protocol": "media_buy",
                "status": "completed",
                "created_at": "2098-01-01T00:00:00Z",
                "updated_at": "2098-01-01T00:00:01Z",
                "completed_at": "2098-01-01T00:00:01Z",
                "result": {"media_buy_id": "mb-restarted", "packages": []},
            }
        )
    )

    recovered = await coordinator().recover_acceptance(
        "legacy-accept-task-restart",
        account={"account_id": "account-acme"},
        poll_interval=0.01,
    )

    assert initial.status is TaskStatus.SUBMITTED
    assert retry.status is TaskStatus.SUBMITTED
    assert recovered.success and recovered.data is not None
    assert recovered.data["media_buy_id"] == "mb-restarted"
    assert recovered.compatibility.warnings == (
        "Recovered a durable submitted proposal acceptance.",
    )
    assert len(client.create_requests) == 1
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as wrong_scope:
        await coordinator().recover_acceptance(
            "legacy-accept-task-restart",
            account={"account_id": "other-account"},
            poll_interval=0.01,
        )
    assert wrong_scope.value.feature == "submitted_task_not_found"


@pytest.mark.asyncio
async def test_established_acceptance_enforces_digest_bound_terms_and_conditional_losses() -> None:
    response = _legacy_proposal_response()
    terms = {
        "brand": {"domain": "acme.example"},
        "start_time": "2099-01-01T00:00:00Z",
        "end_time": "2099-02-01T00:00:00Z",
        "total_budget": {"amount": 1000, "currency": "USD"},
    }
    response["proposals"][0].update(
        {
            "proposal_kind": "new_media_buy",
            "proposal_status": "committed",
            "expires_at": "2099-01-01T00:00:00Z",
            "commercial_terms": terms,
            "terms_digest": compute_terms_digest(terms),
        }
    )
    client = _Client("3.2-rc.1", response)
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        proposal_evidence_store=InMemoryEstablishedProposalEvidenceStore(),
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=["proposal_terms_digest_not_enforced"],
        allow_non_durable_proposal_acceptance=True,
        clock=lambda: _NOW,
    )
    discovered = await coordinator.request_proposals(_proposal_request())
    assert discovered.data is not None
    request = _accept_request(digest=response["proposals"][0]["terms_digest"])
    request["total_budget"] = {"amount": 2000, "currency": "USD"}

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as mismatch:
        await coordinator.accept_proposal(request)
    assert mismatch.value.code == "PROPOSAL_DIGEST_MISMATCH"
    assert client.create_requests == []

    request["total_budget"] = terms["total_budget"]
    accepted = await coordinator.accept_proposal(request)
    assert accepted.success
    assert accepted.compatibility.losses == ("proposal_terms_digest_not_enforced",)
    wire = client.create_requests[0].model_dump(mode="json", exclude_none=True)
    assert wire["brand"] == terms["brand"]
    assert wire["start_time"] == terms["start_time"]


@pytest.mark.asyncio
async def test_established_acceptance_rejects_loss_or_request_conflicts_preflight() -> None:
    client = _Client("3.2-rc.1", _legacy_proposal_response())
    store = InMemoryEstablishedProposalEvidenceStore()
    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=lambda _execution: {},
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    losses = [
        "proposal_terms_digest_not_enforced",
        "proposal_terms_digest_unavailable",
        "proposal_snapshot_not_immutable",
        "proposal_hold_not_verifiable",
    ]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=losses,
        allow_non_durable_proposal_acceptance=True,
        clock=lambda: _NOW,
    )
    await coordinator.request_proposals(_proposal_request())

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as loss_error:
        await coordinator.accept_proposal(
            _accept_request(),
            established_fallback=_established_fallback(),
            accepted_losses=losses[:-1],
        )
    assert loss_error.value.feature == "accepted_losses"

    await coordinator.accept_proposal(
        _accept_request(),
        established_fallback=_established_fallback(),
        accepted_losses=losses,
    )
    conflicting = _accept_request()
    conflicting["total_budget"] = {"amount": 2000, "currency": "USD"}
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as conflict_error:
        await coordinator.accept_proposal(
            conflicting,
            established_fallback=_established_fallback(),
            accepted_losses=losses,
        )
    assert conflict_error.value.feature == "acceptance_conflict"
    assert len(client.create_requests) == 1


@pytest.mark.asyncio
async def test_established_acceptance_fences_ambiguous_transport_failure() -> None:
    class FailingClient(_Client):
        async def create_media_buy_legacy(self, request: Any) -> TaskResult[Any]:
            self.create_requests.append(request)
            raise TimeoutError("seller outcome was not observed")

    client = FailingClient("3.2-rc.1", _legacy_proposal_response())
    store = InMemoryEstablishedProposalEvidenceStore()
    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=lambda _execution: {},
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    losses = [
        "proposal_terms_digest_not_enforced",
        "proposal_terms_digest_unavailable",
        "proposal_snapshot_not_immutable",
        "proposal_hold_not_verifiable",
        "mutation_idempotency_not_guaranteed",
    ]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"], idempotent=False),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=losses,
        allow_non_durable_proposal_acceptance=True,
        clock=lambda: _NOW,
    )
    await coordinator.request_proposals(_proposal_request())

    with pytest.raises(TimeoutError):
        await coordinator.accept_proposal(
            _accept_request(),
            established_fallback=_established_fallback(),
            accepted_losses=losses,
        )
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as fenced:
        await coordinator.accept_proposal(
            _accept_request(),
            established_fallback=_established_fallback(),
            accepted_losses=losses,
        )

    assert fenced.value.feature == "acceptance_outcome_ambiguous"
    assert len(client.create_requests) == 1


@pytest.mark.asyncio
async def test_established_acceptance_exact_retry_uses_advertised_replay_window() -> None:
    class RetryClient(_Client):
        async def create_media_buy_legacy(self, request: Any) -> TaskResult[Any]:
            self.create_requests.append(request)
            if len(self.create_requests) == 1:
                raise TimeoutError("seller outcome was not observed")
            return _completed({"media_buy_id": "mb-safe-retry", "packages": []})

    client = RetryClient("3.2-rc.1", _legacy_proposal_response())
    store = InMemoryEstablishedProposalEvidenceStore(clock=lambda: _NOW)
    losses = [
        "proposal_terms_digest_not_enforced",
        "proposal_terms_digest_unavailable",
        "proposal_snapshot_not_immutable",
        "proposal_hold_not_verifiable",
    ]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=losses,
        allow_non_durable_proposal_acceptance=True,
        clock=lambda: _NOW,
    )
    await coordinator.request_proposals(_proposal_request())

    with pytest.raises(TimeoutError):
        await coordinator.accept_proposal(
            _accept_request(),
            established_fallback=_established_fallback(),
            accepted_losses=losses,
        )
    retried = await coordinator.accept_proposal(
        _accept_request(),
        established_fallback=_established_fallback(),
        accepted_losses=losses,
    )

    assert retried.success
    assert retried.data.media_buy_id == "mb-safe-retry"
    assert len(client.create_requests) == 2


@pytest.mark.asyncio
async def test_compact_refine_and_decline_keep_native_tasks() -> None:
    client = _Client(
        "3.2-rc.1",
        {
            "status": "completed",
            "results": [
                {
                    "source_proposal_id": "compact-proposal-1",
                    "outcome": "unable",
                    "reason_code": "source_unavailable",
                    "reason": "Proposal expired",
                }
            ],
            "products": [],
        },
    )
    tools = ["list_products", "refine_proposals", "decline_proposals"]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.2-rc.1"], served="3.2-rc.1", tools=tools),
        tools,
    )

    refined = await coordinator.refine_proposals(_refine_request("compact-proposal-1"))
    client.response = {"results": [{"proposal_id": "compact-proposal-1", "outcome": "declined"}]}
    declined = await coordinator.decline_proposals(_decline_request("compact-proposal-1"))

    assert refined.success and refined.data is not None
    assert refined.data.outcome == "native_results"
    assert refined.compatibility.tools_used == ("refine_proposals",)
    assert declined.success and declined.data is not None
    assert declined.data.results[0]["outcome"] == "declined"
    assert declined.compatibility.tools_used == ("decline_proposals",)
    assert len(client.refine_requests) == len(client.decline_requests) == 1


@pytest.mark.asyncio
async def test_established_refinement_projects_supported_subset_and_replays() -> None:
    client = _Client("3.2-rc.1", _legacy_proposal_response())
    store = InMemoryEstablishedProposalEvidenceStore()
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allow_non_durable_proposal_mutations=True,
        clock=lambda: _NOW,
    )
    await coordinator.request_proposals(_proposal_request())
    response = _legacy_proposal_response()
    response["proposals"][0]["proposal_id"] = "legacy-proposal-2"
    response["refinement_applied"] = [
        {"scope": "proposal", "proposal_id": "legacy-proposal-1", "status": "applied"}
    ]
    client.response = response

    first = await coordinator.refine_proposals(_refine_request())
    replay = await coordinator.refine_proposals(_refine_request())

    assert first.success and first.data is not None
    assert first.data.outcome == "legacy_projected"
    assert first.data.proposals[0]["proposal_id"] == "legacy-proposal-2"
    assert replay.success
    assert len(client.legacy_requests) == 2  # discovery plus one refinement
    wire = client.legacy_requests[1].model_dump(mode="json", exclude_none=True)
    assert wire["buying_mode"] == "refine"
    assert wire["refine"] == [
        {
            "scope": "proposal",
            "proposal_id": "legacy-proposal-1",
            "action": "include",
            "ask": "More video",
        }
    ]


@pytest.mark.asyncio
async def test_established_refinement_rejects_unprojectable_constraints_preflight() -> None:
    client = _Client("3.2-rc.1", _legacy_proposal_response())
    store = InMemoryEstablishedProposalEvidenceStore()
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products"],
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allow_non_durable_proposal_mutations=True,
        clock=lambda: _NOW,
    )
    await coordinator.request_proposals(_proposal_request())
    constrained = RefineProposalsRequest.model_validate(
        {
            "idempotency_key": "proposal-refinement-key-0002",
            "refinements": [
                {
                    "proposal_id": "legacy-proposal-1",
                    "action": "revise",
                    "constraints": {"total_budget": {"max": 1000, "currency": "USD"}},
                }
            ],
        }
    )

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as exc:
        await coordinator.refine_proposals(constrained)

    assert exc.value.feature == "legacy_request_projection"
    assert len(client.legacy_requests) == 1


@pytest.mark.asyncio
async def test_established_decline_reports_loss_and_never_forwards_reason() -> None:
    client = _Client("3.2-rc.1", _legacy_proposal_response())
    store = InMemoryEstablishedProposalEvidenceStore()
    losses = ["proposal_decline_not_terminal", "proposal_decline_reason_not_forwarded"]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products"],
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allowed_losses=losses,
        allow_non_durable_proposal_mutations=True,
        clock=lambda: _NOW,
    )
    await coordinator.request_proposals(_proposal_request())
    response = copy.deepcopy(_CASES[1]["legacy_response"])
    response["refinement_applied"] = [
        {"scope": "proposal", "proposal_id": "legacy-proposal-1", "status": "applied"}
    ]
    client.response = response

    result = await coordinator.decline_proposals(_decline_request(), accepted_losses=losses)

    assert result.success and result.data is not None
    assert result.data.outcome == "legacy_unconfirmed"
    assert result.data.results == ({"proposal_id": "legacy-proposal-1", "outcome": "unconfirmed"},)
    assert result.compatibility.losses == tuple(losses)
    wire = client.legacy_requests[1].model_dump(mode="json", exclude_none=True)
    assert wire["refine"] == [
        {"scope": "proposal", "proposal_id": "legacy-proposal-1", "action": "omit"}
    ]
    assert "Too expensive" not in json.dumps(wire)


@pytest.mark.asyncio
async def test_established_refinement_keeps_durable_fence_through_task_polling() -> None:
    client = _Client("3.2-rc.1", _legacy_proposal_response(case_index=2))
    store = InMemoryEstablishedProposalEvidenceStore()
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.1"]),
        ["get_products"],
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allow_non_durable_proposal_mutations=True,
        clock=lambda: _NOW,
    )
    await coordinator.request_proposals(_proposal_request())
    client.response = {"status": "submitted", "task_id": "legacy-refine-task-1"}
    terminal = _legacy_proposal_response(case_index=2)
    terminal["proposals"][0]["proposal_id"] = "legacy-proposal-2"
    terminal["refinement_applied"] = [
        {"scope": "proposal", "proposal_id": "legacy-proposal-1", "status": "applied"}
    ]
    client.task_status_results.append(
        _completed(
            {
                "task_id": "legacy-refine-task-1",
                "task_type": "get_products",
                "protocol": "media_buy",
                "status": "completed",
                "created_at": "2098-01-01T00:00:00Z",
                "updated_at": "2098-01-01T00:00:01Z",
                "completed_at": "2098-01-01T00:00:01Z",
                "result": terminal,
            }
        )
    )

    initial = await coordinator.refine_proposals(_refine_request())
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as fenced:
        await coordinator.refine_proposals(_refine_request())
    completed = await coordinator.wait_for_proposal_mutation(initial, poll_interval=0.01)

    assert initial.status is TaskStatus.SUBMITTED
    assert fenced.value.feature == "proposal_mutation_in_flight"
    assert completed.success and completed.data is not None
    assert completed.data.proposals[0]["proposal_id"] == "legacy-proposal-2"
    assert completed.data.evidence[0].proposal_id == "legacy-proposal-2"
    assert len(client.legacy_requests) == 2


@pytest.mark.asyncio
async def test_established_refinement_recovers_submitted_task_after_coordinator_restart() -> None:
    client = _Client("3.2-rc.1", _legacy_proposal_response(case_index=2))
    store = InMemoryEstablishedProposalEvidenceStore()

    def coordinator() -> MediaBuyLifecycleCoordinator:
        return MediaBuyLifecycleCoordinator(
            client,
            _caps(["3.1"]),
            ["get_products"],
            proposal_evidence_store=store,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
            allow_non_durable_proposal_mutations=True,
            clock=lambda: _NOW,
        )

    first_process = coordinator()
    await first_process.request_proposals(_proposal_request())
    client.response = {"status": "submitted", "task_id": "legacy-refine-task-restart"}
    initial = await first_process.refine_proposals(_refine_request())
    terminal = _legacy_proposal_response(case_index=2)
    terminal["proposals"][0]["proposal_id"] = "legacy-proposal-restarted"
    terminal["refinement_applied"] = [
        {"scope": "proposal", "proposal_id": "legacy-proposal-1", "status": "applied"}
    ]
    client.task_status_results.append(
        _completed(
            {
                "task_id": "legacy-refine-task-restart",
                "task_type": "get_products",
                "protocol": "media_buy",
                "status": "completed",
                "created_at": "2098-01-01T00:00:00Z",
                "updated_at": "2098-01-01T00:00:01Z",
                "completed_at": "2098-01-01T00:00:01Z",
                "result": terminal,
            }
        )
    )

    recovered = await coordinator().recover_proposal_mutation(
        "legacy-refine-task-restart",
        account={"account_id": "account-acme"},
        poll_interval=0.01,
    )

    assert initial.status is TaskStatus.SUBMITTED
    assert recovered.success and recovered.data is not None
    assert recovered.data.proposals[0]["proposal_id"] == "legacy-proposal-restarted"
    assert recovered.compatibility.warnings == ("Recovered a durable submitted proposal mutation.",)
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as wrong_scope:
        await coordinator().recover_proposal_mutation(
            "legacy-refine-task-restart",
            account={"account_id": "other-account"},
            poll_interval=0.01,
        )
    assert wrong_scope.value.feature == "submitted_task_not_found"


@pytest.mark.asyncio
async def test_completed_refinement_tombstone_prunes_without_reopening_source() -> None:
    store_now = [_NOW]
    store = InMemoryEstablishedProposalEvidenceStore(clock=lambda: store_now[0])
    client = _Client("3.2-rc.1", _legacy_proposal_response())
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products"],
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        allow_non_durable_proposal_mutations=True,
        clock=lambda: store_now[0],
    )
    await coordinator.request_proposals(_proposal_request())
    response = _legacy_proposal_response()
    response["proposals"][0]["proposal_id"] = "legacy-proposal-successor"
    response["refinement_applied"] = [
        {"scope": "proposal", "proposal_id": "legacy-proposal-1", "status": "applied"}
    ]
    client.response = response

    completed = await coordinator.refine_proposals(_refine_request())
    replay = await coordinator.refine_proposals(_refine_request())
    store_now[0] += timedelta(days=6)
    assert await store.prune_completion_tombstones() == 0
    store_now[0] += timedelta(days=1)
    assert await store.prune_completion_tombstones() == 1

    assert completed.success and replay.success
    assert len(client.legacy_requests) == 2
    assert not await store.find(
        ["legacy-proposal-1"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        source_adcp_version="3.0.18",
    )
    assert await store.find(
        ["legacy-proposal-successor"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        source_adcp_version="3.0.18",
    )
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as consumed:
        await coordinator.refine_proposals(_refine_request())
    assert consumed.value.feature == "proposal_evidence"


@pytest.mark.asyncio
async def test_tombstone_pruning_uses_store_captured_source_generation() -> None:
    store_now = [_NOW]
    store = InMemoryEstablishedProposalEvidenceStore(clock=lambda: store_now[0])
    evidence = EstablishedProposalEvidence.capture(
        {"proposal_id": "proposal-1", "name": "Original"},
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        account_identity='{"account_id":"account-acme"}',
        source_adcp_version="3.0.18",
        observed_request={"brief": "test"},
        observed_at=_NOW,
    )
    await store.put(evidence)
    reservation = await store.reserve_mutation(
        [evidence],
        operation=ProposalMutationKind.DECLINE,
        idempotency_key="decline-proposal-1",
        request={"wire_request": {"proposal_id": "proposal-1"}},
        created_at=_NOW,
    )
    await store.complete_mutation(reservation.record, {"success": True})

    store_now[0] += timedelta(days=7)

    assert await store.prune_completion_tombstones() == 1
    assert not await store.find(
        ["proposal-1"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        source_adcp_version="3.0.18",
    )


@pytest.mark.asyncio
async def test_proposal_store_enforces_configured_record_and_byte_capacity() -> None:
    evidence = EstablishedProposalEvidence.capture(
        {"proposal_id": "proposal-1", "name": "A"},
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        account_identity='{"account_id":"account-acme"}',
        source_adcp_version="3.0.18",
        observed_request={"brief": "test"},
        observed_at=_NOW,
    )
    record_limited = InMemoryEstablishedProposalEvidenceStore(max_records=1)
    await record_limited.put(evidence)
    second = EstablishedProposalEvidence.capture(
        {"proposal_id": "proposal-2", "name": "B"},
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        account_identity='{"account_id":"account-acme"}',
        source_adcp_version="3.0.18",
        observed_request={"brief": "test"},
        observed_at=_NOW,
    )
    with pytest.raises(ProposalStoreCapacityError):
        await record_limited.put(second)
    with pytest.raises(ProposalStoreCapacityError):
        await InMemoryEstablishedProposalEvidenceStore(max_bytes=1).put(evidence)


@pytest.mark.asyncio
async def test_proposal_mutation_exact_retry_expires_on_store_clock() -> None:
    store_now = [_NOW]
    store = InMemoryEstablishedProposalEvidenceStore(clock=lambda: store_now[0])
    evidence = EstablishedProposalEvidence.capture(
        {"proposal_id": "proposal-retry", "name": "Retry"},
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        account_identity='{"account_id":"account-acme"}',
        source_adcp_version="3.0.18",
        observed_request={"brief": "test"},
        observed_at=_NOW,
    )
    await store.put(evidence)
    reservation_input = {"wire_request": {"proposal_id": "proposal-retry"}}
    first = await store.reserve_mutation(
        [evidence],
        operation=ProposalMutationKind.REFINE,
        idempotency_key="retry-proposal-1",
        request=reservation_input,
        created_at=_NOW,
        retry_ttl=timedelta(hours=1),
    )
    await store.mark_mutation_ambiguous(first.record)
    retry = await store.reserve_mutation(
        [evidence],
        operation=ProposalMutationKind.REFINE,
        idempotency_key="retry-proposal-1",
        request=reservation_input,
        created_at=_NOW,
        retry_ttl=timedelta(hours=1),
    )
    assert retry.created

    await store.mark_mutation_ambiguous(retry.record)
    store_now[0] += timedelta(hours=1)
    expired = await store.reserve_mutation(
        [evidence],
        operation=ProposalMutationKind.REFINE,
        idempotency_key="retry-proposal-1",
        request=reservation_input,
        created_at=_NOW,
        retry_ttl=timedelta(hours=1),
    )

    assert not expired.created
    assert expired.record.state.value == "ambiguous"


@pytest.mark.asyncio
async def test_compact_control_media_buy_keeps_native_task() -> None:
    client = _Client(
        "3.2-rc.1",
        {
            "status": "completed",
            "media_buy_id": "mb-1",
            "revision": 8,
            "media_buy_status": "paused",
        },
    )
    tools = ["list_products", "control_media_buy"]
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.2-rc.1"], served="3.2-rc.1", tools=tools),
        tools,
    )

    result = await coordinator.control_media_buy(_control_request())

    assert result.success and result.data is not None
    assert result.data.media_buy_id == "mb-1"
    assert result.data.revision == 8
    assert result.compatibility.compatibility is MediaBuyCompatibility.NATIVE
    assert result.compatibility.tools_used == ("control_media_buy",)
    assert len(client.control_requests) == 1
    assert not client.update_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["3.0", "3.1"])
async def test_established_control_projects_revision_checked_update(version: str) -> None:
    client = _Client(
        "3.2-rc.1",
        {
            "status": "completed",
            "media_buy_id": "mb-1",
            "revision": 8,
            "media_buy_status": "paused",
        },
    )
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps([version]),
        ["get_products", "update_media_buy"],
    )
    request = _control_request(packages=[{"package_id": "pkg-1", "budget": 500, "paused": True}])

    result = await coordinator.control_media_buy(request)

    assert result.success and result.data is not None
    assert result.compatibility.compatibility is MediaBuyCompatibility.LOSSLESS_PROJECTION
    assert result.compatibility.tools_used == ("update_media_buy",)
    wire = client.update_requests[0].model_dump(mode="json", exclude_none=True)
    assert wire["revision"] == 7
    assert wire["paused"] is True
    assert wire["packages"] == [{"package_id": "pkg-1", "budget": 500.0, "paused": True}]
    assert not client.control_requests


@pytest.mark.asyncio
async def test_established_control_rejects_compact_only_fields_before_dispatch() -> None:
    client = _Client("3.2-rc.1", {})
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.1"]),
        ["update_media_buy"],
    )

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as root_error:
        await coordinator.control_media_buy(
            _control_request(total_budget={"amount": 1000, "currency": "USD"})
        )
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as package_error:
        await coordinator.control_media_buy(
            _control_request(packages=[{"package_id": "pkg-1", "catalog_ids": ["cat-1"]}])
        )

    assert root_error.value.feature == "legacy_request_projection"
    assert package_error.value.feature == "legacy_request_projection"
    assert not client.update_requests


@pytest.mark.asyncio
async def test_established_control_polls_submitted_update_with_original_route() -> None:
    client = _Client("3.2-rc.1", {"status": "submitted", "task_id": "control-task-1"})
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.1"]),
        ["update_media_buy"],
    )
    client.task_status_results.append(
        _completed(
            {
                "task_id": "control-task-1",
                "task_type": "update_media_buy",
                "protocol": "media_buy",
                "status": "completed",
                "created_at": "2098-01-01T00:00:00Z",
                "updated_at": "2098-01-01T00:00:01Z",
                "completed_at": "2098-01-01T00:00:01Z",
                "result": {
                    "status": "completed",
                    "media_buy_id": "mb-1",
                    "revision": 8,
                    "media_buy_status": "paused",
                },
            }
        )
    )

    initial = await coordinator.control_media_buy(_control_request())
    completed = await coordinator.wait_for_control_media_buy(initial, poll_interval=0.01)

    assert initial.status is TaskStatus.SUBMITTED
    assert completed.success and completed.data is not None
    assert completed.data.revision == 8
    assert completed.compatibility.tools_used == ("update_media_buy",)


@pytest.mark.asyncio
async def test_compact_media_buy_readbacks_keep_shared_native_tools() -> None:
    tools = ["list_products", "get_media_buys", "get_media_buy_delivery"]
    client = _Client("3.2-rc.1", {"media_buys": []})
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.2-rc.1"], served="3.2-rc.1", tools=tools),
        tools,
    )

    buys = await coordinator.get_media_buys(
        GetMediaBuysRequest(account={"account_id": "account-acme"})
    )
    client.response = _delivery_response()
    delivery = await coordinator.get_media_buy_delivery(
        GetMediaBuyDeliveryRequest(account={"account_id": "account-acme"})
    )

    assert buys.success and buys.data is not None
    assert buys.data.operation == "get_media_buys"
    assert buys.compatibility.compatibility is MediaBuyCompatibility.NATIVE
    assert delivery.success and delivery.data is not None
    assert delivery.data.operation == "get_media_buy_delivery"
    assert len(client.media_buy_read_requests) == len(client.delivery_read_requests) == 1
    assert not client.legacy_media_buy_read_requests
    assert not client.legacy_delivery_read_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["3.0", "3.1"])
async def test_established_media_buy_readbacks_validate_and_suppress_new_defaults(
    version: str,
) -> None:
    tools = ["get_media_buys", "get_media_buy_delivery"]
    client = _Client("3.2-rc.1", {"media_buys": []})
    coordinator = MediaBuyLifecycleCoordinator(client, _caps([version]), tools)

    buys = await coordinator.get_media_buys(
        GetMediaBuysRequest(account={"account_id": "account-acme"})
    )
    client.response = _delivery_response()
    delivery = await coordinator.get_media_buy_delivery(
        GetMediaBuyDeliveryRequest(account={"account_id": "account-acme"})
    )

    assert buys.success and delivery.success
    assert buys.compatibility.compatibility is MediaBuyCompatibility.LOSSLESS_PROJECTION
    assert delivery.compatibility.compatibility is MediaBuyCompatibility.LOSSLESS_PROJECTION
    buys_wire = client.legacy_media_buy_read_requests[0].model_dump(mode="json", exclude_none=True)
    delivery_wire = client.legacy_delivery_read_requests[0].model_dump(
        mode="json", exclude_none=True
    )
    assert "indicator_types" not in buys_wire
    assert "requested_metrics" not in delivery_wire
    if version == "3.0":
        assert "include_webhook_activity" not in buys_wire
        assert "webhook_activity_limit" not in buys_wire
        assert "include_window_breakdown" not in delivery_wire


@pytest.mark.asyncio
async def test_established_readback_rejects_newer_fields_and_missing_tools_preflight() -> None:
    client = _Client("3.2-rc.1", {"media_buys": []})
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_media_buys"],
    )

    with pytest.raises(MediaBuyLifecycleCompatibilityError) as field_error:
        await coordinator.get_media_buys(
            GetMediaBuysRequest(
                account={"account_id": "account-acme"},
                include_webhook_activity=True,
            )
        )
    with pytest.raises(MediaBuyLifecycleCompatibilityError) as tool_error:
        await coordinator.get_media_buy_delivery(
            GetMediaBuyDeliveryRequest(account={"account_id": "account-acme"})
        )

    assert field_error.value.feature == "request_projection"
    assert tool_error.value.feature == "lifecycle_tool_not_advertised"
    assert not client.legacy_media_buy_read_requests
    assert not client.legacy_delivery_read_requests


@pytest.mark.asyncio
async def test_media_buy_readback_polls_with_exact_task_type() -> None:
    client = _Client("3.2-rc.1", {"status": "submitted", "task_id": "read-task-1"})
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.1"]),
        ["get_media_buys"],
    )
    client.task_status_results.append(
        _completed(
            {
                "task_id": "read-task-1",
                "task_type": "get_media_buys",
                "protocol": "media_buy",
                "status": "completed",
                "created_at": "2098-01-01T00:00:00Z",
                "updated_at": "2098-01-01T00:00:01Z",
                "completed_at": "2098-01-01T00:00:01Z",
                "result": {"media_buys": []},
            }
        )
    )

    initial = await coordinator.get_media_buys(
        GetMediaBuysRequest(account={"account_id": "account-acme"})
    )
    completed = await coordinator.wait_for_media_buy_readback(initial, poll_interval=0.01)

    assert initial.status is TaskStatus.SUBMITTED
    assert completed.success and completed.data is not None
    assert completed.data.raw == {"media_buys": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "case_index", "source_version"),
    [("3.0", 1, "3.0.18"), ("3.1", 2, "3.1.15")],
)
async def test_established_proposal_request_retains_bound_immutable_evidence(
    version: str, case_index: int, source_version: str
) -> None:
    response = copy.deepcopy(_CASES[case_index]["legacy_response"])
    product_id = response["products"][0]["product_id"]
    response["proposals"] = [
        {
            "proposal_id": "legacy-proposal-1",
            "name": "Legacy plan",
            "allocations": [{"product_id": product_id, "allocation_percentage": 100}],
        }
    ]
    store = InMemoryEstablishedProposalEvidenceStore()
    client = _Client("3.2-rc.1", response)
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps([version], idempotent=False),
        ["get_products", "create_media_buy"],
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        clock=lambda: _NOW,
    )

    result = await coordinator.request_proposals(_proposal_request())

    assert result.success
    assert result.data is not None
    assert result.data.outcome == "proposed"
    assert result.data.purchase_continuation is None
    assert len(result.data.evidence) == 1
    evidence = await store.get(
        "legacy-proposal-1",
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        account_identity='{"account_id":"account-acme"}',
        source_adcp_version=source_version,
    )
    assert isinstance(evidence, EstablishedProposalEvidence)
    assert (
        await store.get(
            "legacy-proposal-1",
            principal_id="other-principal",
            target_binding="seller-session-acme",
            account_identity='{"account_id":"account-acme"}',
            source_adcp_version=source_version,
        )
        is None
    )
    response["proposals"][0]["name"] = "mutated after dispatch"
    assert evidence.proposal["name"] == "Legacy plan"
    sent = client.legacy_requests[0].model_dump(mode="json", exclude_none=True)
    assert sent["buying_mode"] == "brief"
    assert sent["filters"]["countries"] == ["US"]

    client.response["proposals"][0]["accessToken"] = "seller-secret"
    unsafe = await coordinator.request_proposals(_proposal_request())
    assert unsafe.data is not None
    assert unsafe.data.evidence == ()
    assert "not retained" in unsafe.compatibility.warnings[0]
    assert (
        await store.get(
            "legacy-proposal-1",
            principal_id="principal-acme",
            target_binding="seller-session-acme",
            account_identity='{"account_id":"account-acme"}',
            source_adcp_version=source_version,
        )
        is None
    )


@pytest.mark.asyncio
async def test_oversized_proposal_revokes_stale_executable_evidence() -> None:
    response = _legacy_proposal_response()
    store = InMemoryEstablishedProposalEvidenceStore()
    client = _Client("3.2-rc.1", response)
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(["3.0"]),
        ["get_products", "create_media_buy"],
        proposal_evidence_store=store,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        clock=lambda: _NOW,
    )
    retained = await coordinator.request_proposals(_proposal_request())
    assert retained.data is not None and retained.data.evidence

    client.response["proposals"][0]["seller_extension"] = {
        "padding": "x" * ESTABLISHED_PROPOSAL_MAX_SNAPSHOT_BYTES
    }
    oversized = await coordinator.request_proposals(_proposal_request())

    assert oversized.success and oversized.data is not None
    assert oversized.data.proposals[0]["proposal_id"] == "legacy-proposal-1"
    assert oversized.data.evidence == ()
    assert "not retained" in oversized.compatibility.warnings[0]
    assert (
        await store.get(
            "legacy-proposal-1",
            principal_id="principal-acme",
            target_binding="seller-session-acme",
            account_identity='{"account_id":"account-acme"}',
            source_adcp_version="3.0.18",
        )
        is None
    )


def test_lifecycle_coordinator_bounds_authenticated_principal_scope() -> None:
    with pytest.raises(ValueError, match="principal_id must be at most"):
        MediaBuyLifecycleCoordinator(
            _Client("3.2-rc.1", {}),
            _caps(["3.2-rc.1"], served="3.2-rc.1", tools=["list_products"]),
            ["list_products"],
            principal_id="x" * (ESTABLISHED_PROPOSAL_MAX_PRINCIPAL_BYTES + 1),
        )

    invalid_ttl = _caps(["3.2-rc.1"], served="3.2-rc.1", tools=["list_products"])
    invalid_ttl.adcp.idempotency.replay_ttl_seconds = 60
    with pytest.raises(ValueError, match="replay_ttl_seconds"):
        MediaBuyLifecycleCoordinator(
            _Client("3.2-rc.1", {}),
            invalid_ttl,
            ["list_products"],
        )


@pytest.mark.asyncio
async def test_missing_replay_ttl_reports_mutation_guarantee_loss() -> None:
    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=lambda _execution: {},
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    capabilities = _caps(["3.0"])
    capabilities.adcp.idempotency.replay_ttl_seconds = None
    coordinator = MediaBuyLifecycleCoordinator(
        _Client("3.2-rc.1", _CASES[1]["legacy_response"]),
        capabilities,
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

    assert listing.purchase_continuation is not None
    assert "mutation_idempotency_not_guaranteed" in listing.purchase_continuation.losses


@pytest.mark.asyncio
async def test_established_products_only_proposal_reuses_purchase_continuation() -> None:
    response = copy.deepcopy(_CASES[1]["legacy_response"])
    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=lambda _execution: {},
        token_derivation_key=b"test-only-continuation-token-key-32-bytes-minimum",
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    coordinator = MediaBuyLifecycleCoordinator(
        _Client("3.2-rc.1", response),
        _caps(["3.0"], idempotent=False),
        ["get_products", "create_media_buy"],
        legacy_purchase_coordinator=legacy,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
        clock=lambda: _NOW,
    )

    result = await coordinator.request_proposals(_proposal_request())

    assert result.data is not None
    assert result.data.outcome == "products_available"
    assert result.data.purchase_continuation is not None
    assert result.data.purchase_continuation.losses == (
        "feed_version_not_atomic",
        "pricing_version_not_atomic",
        "mutation_idempotency_not_guaranteed",
    )


@pytest.mark.asyncio
async def test_submitted_proposal_result_is_projected_after_polling() -> None:
    client = _Client("3.2-rc.1", {"status": "submitted", "task_id": "proposal-task-1"})
    client.task_status_results.append(
        _completed(
            {
                "task_id": "proposal-task-1",
                "task_type": "request_proposals",
                "protocol": "media_buy",
                "status": "completed",
                "created_at": "2098-01-01T00:00:00Z",
                "updated_at": "2098-01-01T00:00:01Z",
                "completed_at": "2098-01-01T00:00:01Z",
                "result": _compact_proposal_response(),
            }
        )
    )
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(
            ["3.2-rc.1"],
            served="3.2-rc.1",
            tools=["list_products", "buy_products", "request_proposals"],
        ),
        ["list_products", "buy_products", "request_proposals"],
    )

    initial = await coordinator.request_proposals(_proposal_request())
    completed = await coordinator.wait_for_proposals(initial, poll_interval=0.01)

    assert initial.status is TaskStatus.SUBMITTED
    assert initial.data is None
    assert initial.task_id == "proposal-task-1"
    assert completed.success
    assert completed.data is not None
    assert completed.data.outcome == "proposed"
    assert completed.compatibility.tools_used == ("request_proposals",)


@pytest.mark.asyncio
async def test_a2a_working_proposal_state_keeps_task_identity_and_provenance() -> None:
    client = _Client("3.2-rc.1", {})
    client.proposal_result = TaskResult[Any](
        status=TaskStatus.SUBMITTED,
        data=None,
        success=True,
        metadata={
            "task_id": "proposal-task-working",
            "context_id": "proposal-context",
            "status": "working",
        },
    )
    coordinator = MediaBuyLifecycleCoordinator(
        client,
        _caps(
            ["3.2-rc.1"],
            served="3.2-rc.1",
            tools=["list_products", "buy_products", "request_proposals"],
        ),
        ["list_products", "buy_products", "request_proposals"],
    )

    result = await coordinator.request_proposals(_proposal_request())

    assert result.status is TaskStatus.WORKING
    assert result.task_id == "proposal-task-working"
    assert result.data is None
    assert result.compatibility.tools_used == ("request_proposals",)


def test_exact_legacy_prerelease_source_bundle_is_supported() -> None:
    # Regression for exact prerelease pins: the continuation validator accepts
    # an exact bundled 3.1 source, while still rejecting invented releases.
    from adcp.compat.purchase_continuation import _validate_source_version

    _validate_source_version("3.1.0-rc.13")
    with pytest.raises(CompatibilityContinuationError, match="bundled source schema release"):
        _validate_source_version("3.1.0-rc.12")
