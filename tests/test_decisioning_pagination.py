"""Tests for adcp.decisioning.pagination: cursor codec + page-slice helper.

Covers all acceptance criteria from issue #493:
- encode/decode round-trip
- INVALID_REQUEST on hash mismatch and malformed cursor
- auto_paginate=False (default) leaves existing adopters untouched
- auto_paginate=True slices response and sets has_more correctly
- max_results clamped to [1, 100]
- 250-product / 5-page test
- cursor rejection when filters change between calls
- short-circuit when adopter already set response.pagination
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.pagination import (
    _decode_cursor,
    _encode_cursor,
    _query_hash,
    apply_framework_pagination,
)
from adcp.decisioning.task_registry import InMemoryTaskRegistry
from adcp.decisioning.types import AdcpError
from adcp.server.base import ToolContext
from adcp.types import (
    GetProductsRequest,
    GetProductsResponse,
    PaginationRequest,
    PaginationResponse,
    Product,
)


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-pag-")
    yield pool
    pool.shutdown(wait=True)


def _make_handler(platform: DecisioningPlatform, executor: ThreadPoolExecutor) -> PlatformHandler:
    return PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())

_SECRET = b"test-secret-key"


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


def test_encode_decode_roundtrip() -> None:
    cursor = _encode_cursor(50, "abc123", _SECRET)
    offset = _decode_cursor(cursor, "abc123", _SECRET)
    assert offset == 50


def test_encode_decode_offset_zero() -> None:
    cursor = _encode_cursor(0, "qh1", _SECRET)
    assert _decode_cursor(cursor, "qh1", _SECRET) == 0


def test_decode_raises_on_hash_mismatch() -> None:
    cursor = _encode_cursor(50, "hash-A", _SECRET)
    with pytest.raises(AdcpError) as exc_info:
        _decode_cursor(cursor, "hash-B", _SECRET)
    err = exc_info.value
    assert err.code == "INVALID_REQUEST"
    assert "stale" in (err.args[0] if err.args else "").lower()
    assert err.field == "pagination.cursor"
    assert err.recovery == "correctable"


def test_decode_raises_on_malformed_cursor() -> None:
    with pytest.raises(AdcpError) as exc_info:
        _decode_cursor("not-valid-base64!!!", "qh", _SECRET)
    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.field == "pagination.cursor"


def test_decode_raises_on_tampered_signature() -> None:
    cursor = _encode_cursor(0, "qh", _SECRET)
    # Flip the last character of the cursor.
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    with pytest.raises(AdcpError) as exc_info:
        _decode_cursor(tampered, "qh", _SECRET)
    assert exc_info.value.code == "INVALID_REQUEST"


def test_decode_raises_wrong_process_secret() -> None:
    cursor = _encode_cursor(0, "qh", b"secret-A")
    with pytest.raises(AdcpError):
        _decode_cursor(cursor, "qh", b"secret-B")


# ---------------------------------------------------------------------------
# apply_framework_pagination
# ---------------------------------------------------------------------------


def _make_response(n: int) -> GetProductsResponse:
    """Return a GetProductsResponse with n stub products (bypasses validation)."""
    products = [Product.model_construct(product_id=f"p{i}") for i in range(n)]
    return GetProductsResponse.model_construct(products=products)


def _make_pagination_request(max_results: int = 50, cursor: str | None = None) -> PaginationRequest:
    return PaginationRequest(max_results=max_results, cursor=cursor)


def test_first_page_has_more_true() -> None:
    resp = _make_response(100)
    pag = _make_pagination_request(max_results=50)
    result = apply_framework_pagination(resp, pag, "qh", _SECRET)
    assert len(result.products) == 50
    assert result.pagination is not None
    assert result.pagination.has_more is True
    assert result.pagination.cursor is not None


def test_last_page_has_more_false() -> None:
    resp = _make_response(30)
    pag = _make_pagination_request(max_results=50)
    result = apply_framework_pagination(resp, pag, "qh", _SECRET)
    assert len(result.products) == 30
    assert result.pagination is not None
    assert result.pagination.has_more is False
    assert result.pagination.cursor is None


def test_250_products_5_pages() -> None:
    """250 products, max_results=50 → exactly 5 pages, last has has_more=False."""
    qh = "fixed-qh"
    page_sizes: list[int] = []
    cursor: str | None = None

    for _ in range(10):  # cap iterations to prevent infinite loop in test
        resp = _make_response(250)
        pag = _make_pagination_request(max_results=50, cursor=cursor)
        result = apply_framework_pagination(resp, pag, qh, _SECRET)
        page_sizes.append(len(result.products))
        assert result.pagination is not None
        if not result.pagination.has_more:
            break
        cursor = result.pagination.cursor

    assert len(page_sizes) == 5
    assert all(s == 50 for s in page_sizes)
    assert result.pagination.has_more is False  # type: ignore[union-attr]


def test_max_results_clamped_above_100() -> None:
    resp = _make_response(200)
    # Bypass wire validation to test the framework clamp.
    pag = PaginationRequest.model_construct(max_results=9999, cursor=None)
    result = apply_framework_pagination(resp, pag, "qh", _SECRET)
    # Clamped to 100.
    assert len(result.products) == 100
    assert result.pagination is not None
    assert result.pagination.has_more is True


def test_max_results_clamped_below_1() -> None:
    resp = _make_response(10)
    # Bypass wire validation to test the framework clamp.
    pag = PaginationRequest.model_construct(max_results=0, cursor=None)
    result = apply_framework_pagination(resp, pag, "qh", _SECRET)
    # Clamped to 1.
    assert len(result.products) == 1


def test_short_circuit_when_pagination_already_set() -> None:
    resp = _make_response(10)
    existing = PaginationResponse(has_more=False, cursor=None)
    resp = resp.model_copy(update={"pagination": existing})
    pag = _make_pagination_request(max_results=5)
    result = apply_framework_pagination(resp, pag, "qh", _SECRET)
    # Must not overwrite the adopter's pagination.
    assert result is resp
    assert result.pagination is existing


def test_cursor_rejects_when_filters_change() -> None:
    """Cursor from request with qh='hash-A' must reject when qh='hash-B'."""
    resp = _make_response(100)
    pag_page1 = _make_pagination_request(max_results=50)
    result = apply_framework_pagination(resp, pag_page1, "hash-A", _SECRET)
    next_cursor = result.pagination.cursor  # type: ignore[union-attr]
    assert next_cursor is not None

    # Simulate filter change: different query hash.
    pag_page2 = _make_pagination_request(max_results=50, cursor=next_cursor)
    with pytest.raises(AdcpError) as exc_info:
        apply_framework_pagination(resp, pag_page2, "hash-B", _SECRET)
    assert exc_info.value.code == "INVALID_REQUEST"
    assert "stale" in (exc_info.value.args[0] if exc_info.value.args else "").lower()


# ---------------------------------------------------------------------------
# query_hash stability
# ---------------------------------------------------------------------------


def test_query_hash_excludes_pagination() -> None:
    req_no_pag = GetProductsRequest(buying_mode="brief", brief="test")
    req_with_pag = GetProductsRequest(
        buying_mode="brief",
        brief="test",
        pagination=PaginationRequest(max_results=50, cursor="xyz"),
    )
    assert _query_hash(req_no_pag) == _query_hash(req_with_pag)


def test_query_hash_changes_when_filters_change() -> None:
    req_a = GetProductsRequest(buying_mode="brief", brief="shoes")
    req_b = GetProductsRequest(buying_mode="brief", brief="bags")
    assert _query_hash(req_a) != _query_hash(req_b)


# ---------------------------------------------------------------------------
# Integration: auto_paginate flag through PlatformHandler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_paginate_false_default_passthrough(executor: ThreadPoolExecutor) -> None:
    """auto_paginate=False (default) — handler returns full list unchanged."""

    class TestPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acme")

        def get_products(self, req: GetProductsRequest, ctx: object) -> GetProductsResponse:
            return _make_response(10)

    handler = _make_handler(TestPlatform(), executor)
    req = GetProductsRequest(
        buying_mode="brief",
        brief="test",
        pagination=PaginationRequest(max_results=5),
    )
    result = await handler.get_products(req, ToolContext())
    # No slicing: all 10 products returned, no pagination set by framework.
    assert len(result.products) == 10
    assert result.pagination is None


@pytest.mark.asyncio
async def test_auto_paginate_true_slices_response(executor: ThreadPoolExecutor) -> None:
    """auto_paginate=True — handler slices and sets pagination."""

    class TestPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(auto_paginate=True)
        accounts = SingletonAccounts(account_id="acme")

        def get_products(self, req: GetProductsRequest, ctx: object) -> GetProductsResponse:
            return _make_response(10)

    handler = _make_handler(TestPlatform(), executor)
    req = GetProductsRequest(
        buying_mode="brief",
        brief="test",
        pagination=PaginationRequest(max_results=5),
    )
    result = await handler.get_products(req, ToolContext())
    assert len(result.products) == 5
    assert result.pagination is not None
    assert result.pagination.has_more is True


@pytest.mark.asyncio
async def test_auto_paginate_no_pagination_in_request_passthrough(
    executor: ThreadPoolExecutor,
) -> None:
    """auto_paginate=True but no pagination in request — full list returned."""

    class TestPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(auto_paginate=True)
        accounts = SingletonAccounts(account_id="acme")

        def get_products(self, req: GetProductsRequest, ctx: object) -> GetProductsResponse:
            return _make_response(10)

    handler = _make_handler(TestPlatform(), executor)
    req = GetProductsRequest(buying_mode="brief", brief="test")
    result = await handler.get_products(req, ToolContext())
    assert len(result.products) == 10
