"""Tests for adcp.decisioning.refine — buying_mode='refine' scaffold.

Covers:
- assert_buying_mode_consistent rejects refine+brief, wholesale+brief,
  refine without refine[].
- build_refinement_applied position-matches scope + ids correctly for all
  three discriminated variants.
- project_refine_response rejects mismatched outcome counts.
- Handler dispatches to refine_get_products() when present and
  buying_mode='refine'.
- Handler rejects buying_mode='refine' on platforms without
  refine_get_products() with INVALID_REQUEST.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    RefinementOutcome,
    RefineResult,
    SingletonAccounts,
    assert_buying_mode_consistent,
    build_refinement_applied,
    has_refine_support,
    project_refine_response,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.server.base import ToolContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-refine-")
    yield pool
    pool.shutdown(wait=True)


def _refine_request_request_scope(ask: str = "more video") -> Any:
    """Build a refine request with scope='request'."""
    from adcp.types import GetProductsRequest

    return GetProductsRequest(
        buying_mode="refine",
        refine=[{"scope": "request", "ask": ask}],
    )


def _refine_request_product_scope(product_id: str = "p1", action: str = "include") -> Any:
    from adcp.types import GetProductsRequest

    return GetProductsRequest(
        buying_mode="refine",
        refine=[{"scope": "product", "product_id": product_id, "action": action}],
    )


def _refine_request_proposal_scope(proposal_id: str = "prop_1") -> Any:
    from adcp.types import GetProductsRequest

    return GetProductsRequest(
        buying_mode="refine",
        refine=[{"scope": "proposal", "proposal_id": proposal_id, "action": "include"}],
    )


# ---------------------------------------------------------------------------
# assert_buying_mode_consistent
# ---------------------------------------------------------------------------


def test_brief_with_brief_text_is_valid() -> None:
    from adcp.types import GetProductsRequest

    req = GetProductsRequest(buying_mode="brief", brief="display campaign")
    assert_buying_mode_consistent(req)  # no raise


def test_wholesale_without_brief_is_valid() -> None:
    from adcp.types import GetProductsRequest

    req = GetProductsRequest(buying_mode="wholesale")
    assert_buying_mode_consistent(req)  # no raise


def test_refine_with_entries_is_valid() -> None:
    req = _refine_request_request_scope()
    assert_buying_mode_consistent(req)


def test_wholesale_with_brief_rejected() -> None:
    from adcp.types import GetProductsRequest

    req = GetProductsRequest(buying_mode="wholesale", brief="leftover")
    with pytest.raises(AdcpError) as exc:
        assert_buying_mode_consistent(req)
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "brief"


def test_refine_with_brief_rejected() -> None:
    from adcp.types import GetProductsRequest

    req = GetProductsRequest(
        buying_mode="refine",
        brief="this should not be here",
        refine=[{"scope": "request", "ask": "more options"}],
    )
    with pytest.raises(AdcpError) as exc:
        assert_buying_mode_consistent(req)
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "brief"


def test_refine_without_refine_array_rejected() -> None:
    from adcp.types import GetProductsRequest

    req = GetProductsRequest(buying_mode="refine")
    with pytest.raises(AdcpError) as exc:
        assert_buying_mode_consistent(req)
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "refine"


# ---------------------------------------------------------------------------
# build_refinement_applied — position-matched echo
# ---------------------------------------------------------------------------


def test_refinement_applied_request_scope_echoes_scope() -> None:
    req = _refine_request_request_scope("more video")
    outcomes = [RefinementOutcome(status="applied", notes="added video products")]
    out = build_refinement_applied(req.refine or [], outcomes)
    assert len(out) == 1
    inner = out[0].root
    assert inner.scope == "request"
    assert inner.status.value == "applied"
    assert inner.notes == "added video products"


def test_refinement_applied_product_scope_echoes_product_id() -> None:
    req = _refine_request_product_scope("prod_42")
    outcomes = [RefinementOutcome(status="partial", notes="updated pricing only")]
    out = build_refinement_applied(req.refine or [], outcomes)
    inner = out[0].root
    assert inner.scope == "product"
    assert inner.product_id == "prod_42"
    assert inner.status.value == "partial"


def test_refinement_applied_proposal_scope_echoes_proposal_id() -> None:
    req = _refine_request_proposal_scope("prop_xyz")
    outcomes = [RefinementOutcome(status="unable", notes="not available in inventory")]
    out = build_refinement_applied(req.refine or [], outcomes)
    inner = out[0].root
    assert inner.scope == "proposal"
    assert inner.proposal_id == "prop_xyz"
    assert inner.status.value == "unable"


def test_refinement_applied_mixed_outcomes() -> None:
    from adcp.types import GetProductsRequest

    req = GetProductsRequest(
        buying_mode="refine",
        refine=[
            {"scope": "product", "product_id": "p1", "action": "omit"},
            {"scope": "product", "product_id": "p2", "action": "include"},
            {"scope": "proposal", "proposal_id": "pp1", "action": "include"},
        ],
    )
    outcomes = [
        RefinementOutcome(status="applied"),
        RefinementOutcome(status="applied"),
        RefinementOutcome(status="partial", notes="capacity reduced"),
    ]
    out = build_refinement_applied(req.refine or [], outcomes)
    assert [r.root.scope for r in out] == ["product", "product", "proposal"]
    assert out[0].root.product_id == "p1"
    assert out[1].root.product_id == "p2"
    assert out[2].root.proposal_id == "pp1"
    assert out[2].root.notes == "capacity reduced"


def test_mismatched_outcome_count_raises() -> None:
    req = _refine_request_request_scope()
    with pytest.raises(ValueError, match="counts must match"):
        build_refinement_applied(req.refine or [], outcomes=[])


# ---------------------------------------------------------------------------
# project_refine_response
# ---------------------------------------------------------------------------


def test_project_refine_response_attaches_products_and_outcomes() -> None:
    req = _refine_request_request_scope()
    result = RefineResult(
        products=[],
        proposals=None,
        per_refine_outcome=[RefinementOutcome(status="applied")],
    )
    response = project_refine_response(result, req.refine or [])
    assert response.products == []
    assert response.proposals is None
    assert response.refinement_applied is not None
    assert len(response.refinement_applied) == 1
    assert response.refinement_applied[0].root.scope == "request"


def test_project_refine_response_keeps_proposals_when_provided() -> None:
    req = _refine_request_proposal_scope("p_1")
    result = RefineResult(
        products=[],
        proposals=[],  # adopter explicitly returns empty list, not None
        per_refine_outcome=[RefinementOutcome(status="applied")],
    )
    response = project_refine_response(result, req.refine or [])
    assert response.proposals == []  # framework preserves empty-list intent


# ---------------------------------------------------------------------------
# has_refine_support
# ---------------------------------------------------------------------------


class _PlatformWithRefine(DecisioningPlatform):
    capabilities = DecisioningCapabilities()
    accounts = SingletonAccounts(account_id="acct")

    def get_products(self, req, ctx):
        from adcp.types import GetProductsResponse

        return GetProductsResponse(products=[])

    def refine_get_products(self, req, ctx):
        return RefineResult(
            products=[],
            proposals=None,
            per_refine_outcome=[RefinementOutcome(status="applied")],
        )


class _PlatformNoRefine(DecisioningPlatform):
    capabilities = DecisioningCapabilities()
    accounts = SingletonAccounts(account_id="acct")

    def get_products(self, req, ctx):
        from adcp.types import GetProductsResponse

        return GetProductsResponse(products=[])


def test_has_refine_support_true_when_method_present() -> None:
    assert has_refine_support(_PlatformWithRefine())


def test_has_refine_support_false_when_method_absent() -> None:
    assert not has_refine_support(_PlatformNoRefine())


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


def _handler(platform, executor):
    return PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())


@pytest.mark.asyncio
async def test_handler_dispatches_to_refine_get_products(executor) -> None:
    """When buying_mode='refine' and the method exists, framework dispatches there."""
    called: dict[str, Any] = {}

    class _P(_PlatformWithRefine):
        def refine_get_products(self, req, ctx):
            called["scope"] = req.refine[0].root.scope
            called["ask"] = req.refine[0].root.ask
            return RefineResult(
                products=[],
                proposals=None,
                per_refine_outcome=[RefinementOutcome(status="applied", notes="ok")],
            )

    handler = _handler(_P(), executor)
    req = _refine_request_request_scope("less display")
    resp = await handler.get_products(req, ToolContext())
    assert called == {"scope": "request", "ask": "less display"}
    assert resp.refinement_applied is not None
    assert resp.refinement_applied[0].root.notes == "ok"


@pytest.mark.asyncio
async def test_handler_rejects_refine_when_unsupported(executor) -> None:
    """Buyer sends buying_mode='refine' to a platform without refine_get_products."""
    handler = _handler(_PlatformNoRefine(), executor)
    req = _refine_request_request_scope()
    with pytest.raises(AdcpError) as exc:
        await handler.get_products(req, ToolContext())
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "buying_mode"


@pytest.mark.asyncio
async def test_handler_brief_mode_unaffected(executor) -> None:
    """Default buying_mode='brief' still calls plain get_products."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    class _P(_PlatformWithRefine):
        def get_products(self, req, ctx):
            return GetProductsResponse(products=[])

        def refine_get_products(self, req, ctx):  # not called
            raise AssertionError("refine_get_products should not be called for brief mode")

    handler = _handler(_P(), executor)
    req = GetProductsRequest(buying_mode="brief", brief="display only")
    resp = await handler.get_products(req, ToolContext())
    assert resp.products == []


@pytest.mark.asyncio
async def test_handler_refine_with_brief_rejected(executor) -> None:
    """Wire validation runs before account resolution."""
    from adcp.types import GetProductsRequest

    handler = _handler(_PlatformWithRefine(), executor)
    req = GetProductsRequest(
        buying_mode="refine",
        brief="invalid combo",
        refine=[{"scope": "request", "ask": "more video"}],
    )
    with pytest.raises(AdcpError) as exc:
        await handler.get_products(req, ToolContext())
    assert exc.value.field == "brief"


@pytest.mark.asyncio
async def test_handler_refine_validates_outcome_count(executor) -> None:
    """Adopter returns wrong number of outcomes — framework raises ValueError."""

    class _P(_PlatformWithRefine):
        def refine_get_products(self, req, ctx):
            return RefineResult(
                products=[],
                proposals=None,
                per_refine_outcome=[],  # WRONG — should be 1 to match req.refine
            )

    handler = _handler(_P(), executor)
    req = _refine_request_request_scope()
    with pytest.raises(ValueError, match="counts must match"):
        await handler.get_products(req, ToolContext())
