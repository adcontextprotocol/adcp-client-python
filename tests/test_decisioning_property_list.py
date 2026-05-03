"""Tests for adcp.decisioning.property_list — resolver and filter helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp.decisioning.property_list import (
    PropertyListFetcher,
    filter_products_by_property_list,
    maybe_apply_property_list_filter,
    resolve_property_list,
    validate_property_list_config,
)
from adcp.decisioning.types import AdcpError


# ---------------------------------------------------------------------------
# Helpers — minimal wire-shape stubs
# ---------------------------------------------------------------------------


def _make_pp_all(publisher_domain: str = "example.com") -> MagicMock:
    """PublisherProperties(selection_type='all') stub."""
    pp = MagicMock()
    pp.selection_type = "all"
    pp.publisher_domain = publisher_domain
    return pp


def _make_pp_by_id(property_ids: list[str]) -> MagicMock:
    """PublisherProperties(selection_type='by_id') stub."""
    pp = MagicMock()
    pp.selection_type = "by_id"
    pp.property_ids = [_make_property_id(pid) for pid in property_ids]
    return pp


def _make_pp_by_tag(tags: list[str]) -> MagicMock:
    """PublisherProperties(selection_type='by_tag') stub."""
    pp = MagicMock()
    pp.selection_type = "by_tag"
    pp.property_tags = tags
    return pp


def _make_property_id(pid: str) -> MagicMock:
    """PropertyId(root=pid) stub."""
    obj = MagicMock()
    obj.root = pid
    return obj


def _make_pp_wrapper(pp: MagicMock) -> MagicMock:
    """PublisherProperties RootModel wrapper stub."""
    wrapper = MagicMock()
    wrapper.root = pp
    return wrapper


def _make_product(
    product_id: str,
    publisher_properties: list[Any],
    property_targeting_allowed: bool | None = False,
) -> MagicMock:
    product = MagicMock()
    product.product_id = product_id
    product.publisher_properties = [_make_pp_wrapper(pp) for pp in publisher_properties]
    product.property_targeting_allowed = property_targeting_allowed
    return product


def _make_response(products: list[Any]) -> MagicMock:
    resp = MagicMock()
    resp.products = products
    resp.property_list_applied = None

    def model_copy(update: dict[str, Any]) -> MagicMock:
        new_resp = MagicMock()
        new_resp.products = update.get("products", products)
        new_resp.property_list_applied = update.get("property_list_applied")
        return new_resp

    resp.model_copy = model_copy
    return resp


def _make_property_list_ref(
    agent_url: str = "https://agent.example.com",
    list_id: str = "list_1",
    auth_token: str | None = None,
) -> MagicMock:
    ref = MagicMock()
    ref.agent_url = agent_url
    ref.list_id = list_id
    ref.auth_token = auth_token
    return ref


# ---------------------------------------------------------------------------
# filter_products_by_property_list
# ---------------------------------------------------------------------------


class TestFilterProductsByPropertyList:
    def test_selection_type_all_always_included(self) -> None:
        """Products with selection_type='all' always pass the filter."""
        product = _make_product("p1", [_make_pp_all()])
        result = filter_products_by_property_list([product], allowed_property_ids=set())
        assert result == [product]

    def test_selection_type_all_ignores_allowed_set(self) -> None:
        """selection_type='all' includes the product regardless of allowed set."""
        product = _make_product("p1", [_make_pp_all()])
        result = filter_products_by_property_list(
            [product], allowed_property_ids={"completely_unrelated_id"}
        )
        assert result == [product]

    def test_by_id_strict_exact_match(self) -> None:
        """Strict (property_targeting_allowed=False): all product IDs must be in allowed set."""
        product = _make_product(
            "p1",
            [_make_pp_by_id(["home", "sports"])],
            property_targeting_allowed=False,
        )
        result = filter_products_by_property_list(
            [product], allowed_property_ids={"home", "sports", "news"}
        )
        assert result == [product]

    def test_by_id_strict_partial_match_excluded(self) -> None:
        """Strict mode: partial subset → product excluded."""
        product = _make_product(
            "p1",
            [_make_pp_by_id(["home", "sports"])],
            property_targeting_allowed=False,
        )
        result = filter_products_by_property_list(
            [product], allowed_property_ids={"home"}  # missing "sports"
        )
        assert result == []

    def test_by_id_strict_full_subset_required(self) -> None:
        """Strict: product with property_targeting_allowed=False requires all IDs in allowed."""
        product = _make_product(
            "p1",
            [_make_pp_by_id(["home"])],
            property_targeting_allowed=False,
        )
        assert filter_products_by_property_list([product], {"home", "sports"}) == [product]
        assert filter_products_by_property_list([product], {"sports"}) == []

    def test_by_id_permissive_any_intersection_sufficient(self) -> None:
        """Permissive (property_targeting_allowed=True): any intersection suffices."""
        product = _make_product(
            "p1",
            [_make_pp_by_id(["home", "sports"])],
            property_targeting_allowed=True,
        )
        result = filter_products_by_property_list(
            [product], allowed_property_ids={"sports"}
        )
        assert result == [product]

    def test_by_id_permissive_no_intersection_excluded(self) -> None:
        """Permissive: no intersection → excluded."""
        product = _make_product(
            "p1",
            [_make_pp_by_id(["home", "sports"])],
            property_targeting_allowed=True,
        )
        result = filter_products_by_property_list(
            [product], allowed_property_ids={"news", "entertainment"}
        )
        assert result == []

    def test_by_tag_always_excluded(self) -> None:
        """Products with only selection_type='by_tag' cannot be matched by ID → excluded."""
        product = _make_product("p1", [_make_pp_by_tag(["premium", "ctv"])])
        result = filter_products_by_property_list(
            [product], allowed_property_ids={"premium", "ctv", "any_id"}
        )
        assert result == []

    def test_mixed_all_and_by_tag_included_via_all(self) -> None:
        """If ANY publisher entry is 'all', product is included regardless of other entries."""
        product = _make_product(
            "p1",
            [_make_pp_by_tag(["ctv"]), _make_pp_all()],
        )
        result = filter_products_by_property_list([product], allowed_property_ids=set())
        assert result == [product]

    def test_mixed_by_id_and_by_tag_respects_by_id(self) -> None:
        """by_id entry can include product even when another entry is by_tag."""
        product = _make_product(
            "p1",
            [_make_pp_by_tag(["ctv"]), _make_pp_by_id(["home"])],
            property_targeting_allowed=True,
        )
        result = filter_products_by_property_list(
            [product], allowed_property_ids={"home"}
        )
        assert result == [product]

    def test_multiple_products_filtered_correctly(self) -> None:
        """Filter applied correctly across a list of products."""
        p1 = _make_product("p1", [_make_pp_all()])  # always included
        p2 = _make_product(
            "p2", [_make_pp_by_id(["a", "b"])], property_targeting_allowed=False
        )  # strict, "b" not in allowed → excluded
        p3 = _make_product(
            "p3", [_make_pp_by_id(["a"])], property_targeting_allowed=False
        )  # strict, "a" in allowed → included
        allowed = {"a"}
        result = filter_products_by_property_list([p1, p2, p3], allowed)
        assert result == [p1, p3]

    def test_empty_products_list(self) -> None:
        result = filter_products_by_property_list([], {"any"})
        assert result == []

    def test_empty_allowed_set_excludes_by_id(self) -> None:
        """Empty allowed set: by_id products excluded (nothing to intersect with)."""
        product = _make_product("p1", [_make_pp_by_id(["home"])])
        assert filter_products_by_property_list([product], set()) == []

    def test_property_targeting_allowed_none_treated_as_false(self) -> None:
        """property_targeting_allowed=None is treated as False (strict)."""
        product = _make_product(
            "p1",
            [_make_pp_by_id(["home", "sports"])],
            property_targeting_allowed=None,
        )
        # Strict: need all IDs in allowed
        assert filter_products_by_property_list([product], {"home"}) == []
        assert filter_products_by_property_list([product], {"home", "sports"}) == [product]


# ---------------------------------------------------------------------------
# resolve_property_list
# ---------------------------------------------------------------------------


class TestResolvePropertyList:
    @pytest.mark.asyncio
    async def test_returns_set_from_fetcher(self) -> None:
        fetcher = AsyncMock(spec=PropertyListFetcher)
        fetcher.fetch = AsyncMock(return_value=["home", "sports", "news"])
        ref = _make_property_list_ref(
            agent_url="https://agent.example.com", list_id="list_1"
        )

        result = await resolve_property_list(ref, fetcher=fetcher)

        assert result == {"home", "sports", "news"}

    @pytest.mark.asyncio
    async def test_auth_token_threaded_to_fetcher(self) -> None:
        """auth_token from the wire ref is threaded through to the fetcher."""
        fetcher = AsyncMock(spec=PropertyListFetcher)
        fetcher.fetch = AsyncMock(return_value=["a"])
        ref = _make_property_list_ref(auth_token="jwt_token_xyz")

        await resolve_property_list(ref, fetcher=fetcher)

        fetcher.fetch.assert_called_once_with(
            str(ref.agent_url),
            ref.list_id,
            auth_token="jwt_token_xyz",
        )

    @pytest.mark.asyncio
    async def test_fetch_failure_raises_adcp_error_transient(self) -> None:
        """Fetch failures are wrapped as AdcpError with recovery='transient'."""
        fetcher = AsyncMock(spec=PropertyListFetcher)
        fetcher.fetch = AsyncMock(side_effect=RuntimeError("connection refused"))
        ref = _make_property_list_ref(list_id="list_1")

        with pytest.raises(AdcpError) as exc_info:
            await resolve_property_list(ref, fetcher=fetcher)

        err = exc_info.value
        assert err.recovery == "transient"
        # auth_token must NOT appear in error details
        assert "auth_token" not in str(err.details)
        assert "jwt" not in str(err.details).lower()

    @pytest.mark.asyncio
    async def test_error_details_do_not_include_auth_token(self) -> None:
        """Credential-shaped values are not leaked into error details."""
        fetcher = AsyncMock(spec=PropertyListFetcher)
        fetcher.fetch = AsyncMock(side_effect=ValueError("403 Forbidden"))
        ref = _make_property_list_ref(auth_token="secret_bearer_token")

        with pytest.raises(AdcpError) as exc_info:
            await resolve_property_list(ref, fetcher=fetcher)

        err = exc_info.value
        # details should include list_id and agent_url but NOT auth_token
        assert err.details is not None
        assert "list_id" in err.details
        assert "agent_url" in err.details
        assert "auth_token" not in err.details
        assert "secret_bearer_token" not in str(err.details)


# ---------------------------------------------------------------------------
# validate_property_list_config
# ---------------------------------------------------------------------------


class TestValidatePropertyListConfig:
    def test_no_capability_no_fetcher_ok(self) -> None:
        """No capability declared → no validation needed."""
        validate_property_list_config(capability_enabled=False, fetcher=None)

    def test_capability_with_fetcher_ok(self) -> None:
        """Capability declared + fetcher wired → valid."""
        fetcher = MagicMock(spec=PropertyListFetcher)
        validate_property_list_config(capability_enabled=True, fetcher=fetcher)

    def test_capability_without_fetcher_raises(self) -> None:
        """Capability declared but no fetcher → AdcpError(recovery='terminal')."""
        with pytest.raises(AdcpError) as exc_info:
            validate_property_list_config(capability_enabled=True, fetcher=None)

        err = exc_info.value
        assert err.recovery == "terminal"
        assert "property_list_filtering" in str(err)
        assert "property_list_fetcher" in str(err)

    def test_no_capability_with_fetcher_ok(self) -> None:
        """Fetcher provided but capability disabled → no error (defensive wiring)."""
        fetcher = MagicMock(spec=PropertyListFetcher)
        validate_property_list_config(capability_enabled=False, fetcher=fetcher)


# ---------------------------------------------------------------------------
# maybe_apply_property_list_filter
# ---------------------------------------------------------------------------


class TestMaybeApplyPropertyListFilter:
    @pytest.mark.asyncio
    async def test_no_op_when_capability_disabled(self) -> None:
        """Gate is a no-op when capability_enabled=False."""
        params = MagicMock()
        params.property_list = _make_property_list_ref()
        response = _make_response([])

        result = await maybe_apply_property_list_filter(
            params=params,
            response=response,
            fetcher=None,
            capability_enabled=False,
        )
        assert result is response

    @pytest.mark.asyncio
    async def test_no_op_when_property_list_absent(self) -> None:
        """Gate is a no-op when params.property_list is None."""
        params = MagicMock()
        params.property_list = None
        response = _make_response([])

        result = await maybe_apply_property_list_filter(
            params=params,
            response=response,
            fetcher=MagicMock(),
            capability_enabled=True,
        )
        assert result is response

    @pytest.mark.asyncio
    async def test_filter_applied_and_flag_set(self) -> None:
        """When capability+property_list present: filter applied, property_list_applied=True."""
        p_pass = _make_product("pass", [_make_pp_all()])
        p_fail = _make_product("fail", [_make_pp_by_id(["x"])])
        response = _make_response([p_pass, p_fail])
        params = MagicMock()
        params.property_list = _make_property_list_ref()

        fetcher = AsyncMock(spec=PropertyListFetcher)
        fetcher.fetch = AsyncMock(return_value=["y"])  # "x" not in allowed → fail excluded

        result = await maybe_apply_property_list_filter(
            params=params,
            response=response,
            fetcher=fetcher,
            capability_enabled=True,
        )

        assert result.property_list_applied is True
        assert result.products == [p_pass]

    @pytest.mark.asyncio
    async def test_no_fetcher_returns_response_unmodified(self, caplog: Any) -> None:
        """When fetcher is None despite capability, log warning + return unmodified."""
        import logging

        params = MagicMock()
        params.property_list = _make_property_list_ref()
        response = _make_response([])

        with caplog.at_level(logging.WARNING, logger="adcp.decisioning.property_list"):
            result = await maybe_apply_property_list_filter(
                params=params,
                response=response,
                fetcher=None,
                capability_enabled=True,
            )

        assert result is response
        assert any("property_list_fetcher" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_fetch_failure_propagates_as_adcp_error(self) -> None:
        """Fetch failure from resolve_property_list propagates to caller."""
        params = MagicMock()
        params.property_list = _make_property_list_ref()
        response = _make_response([])

        fetcher = AsyncMock(spec=PropertyListFetcher)
        fetcher.fetch = AsyncMock(side_effect=ConnectionError("timeout"))

        with pytest.raises(AdcpError) as exc_info:
            await maybe_apply_property_list_filter(
                params=params,
                response=response,
                fetcher=fetcher,
                capability_enabled=True,
            )

        assert exc_info.value.recovery == "transient"

    @pytest.mark.asyncio
    async def test_model_copy_used_not_in_place_mutation(self) -> None:
        """Response is updated via model_copy, not in-place mutation."""
        p = _make_product("p1", [_make_pp_all()])
        original_response = _make_response([p])
        original_response.model_copy = MagicMock(
            side_effect=original_response.model_copy
        )
        params = MagicMock()
        params.property_list = _make_property_list_ref()

        fetcher = AsyncMock(spec=PropertyListFetcher)
        fetcher.fetch = AsyncMock(return_value=["a"])

        await maybe_apply_property_list_filter(
            params=params,
            response=original_response,
            fetcher=fetcher,
            capability_enabled=True,
        )

        original_response.model_copy.assert_called_once()
        call_kwargs = original_response.model_copy.call_args.kwargs
        assert "update" in call_kwargs
        assert call_kwargs["update"]["property_list_applied"] is True
