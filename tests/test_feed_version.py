"""Seller helper for wholesale feed versions (``adcp.server.feed_version``).

Tokens are exercised against wire dicts and real generated models; the
request rules and the ``unchanged: true`` answer are checked against the
bundled JSON schemas through the SDK's own validators, and the round trip
runs the buyer's real :class:`~adcp.feed_mirror.FeedMirror` against a seller
built from these helpers.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from adcp import FeedMirror
from adcp.decisioning import AdcpError
from adcp.server import (
    FRESHNESS_FIELDS,
    PRICING_FIELDS,
    check_feed_preconditions,
    feed_unchanged,
    pricing_version,
    products_response,
    signals_response,
    wholesale_feed_version,
)
from adcp.types import (
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
    Product,
    WholesaleFeedWebhook,
)
from adcp.types.core import TaskResult, TaskStatus
from adcp.validation.schema_validator import validate_request, validate_response
from adcp.webhooks import WebhookSender

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_product(product_id: str, *, cpm: float = 18.5, name: str | None = None) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "name": name or f"Product {product_id}",
        "description": f"Description for {product_id}",
        "publisher_properties": [{"selection_type": "all", "publisher_domain": "pub.example.com"}],
        "format_options": [
            {"format_kind": "image", "format_option_id": "image_default", "params": {}}
        ],
        "delivery_type": "guaranteed",
        "pricing_options": [
            {
                "pricing_option_id": "po_cpm",
                "pricing_model": "cpm",
                "currency": "USD",
                "fixed_cpm": cpm,
            }
        ],
        "reporting_capabilities": {
            "available_reporting_frequencies": ["daily"],
            "expected_delay_minutes": 60,
            "timezone": "UTC",
            "supports_webhooks": False,
            "available_metrics": ["impressions"],
            "date_range_support": "date_range",
        },
        "forecast": {
            "method": "modeled",
            "currency": "USD",
            "points": [{"metrics": {"impressions": {"mid": 100000}}}],
            "generated_at": "2026-10-01T00:00:00Z",
            "valid_until": "2026-10-02T00:00:00Z",
        },
    }


def make_signal(segment_id: str, *, cpm: float = 2.5) -> dict[str, Any]:
    return {
        "signal_ref": {"scope": "product", "signal_id": segment_id},
        "signal_agent_segment_id": segment_id,
        "name": f"Signal {segment_id}",
        "description": f"Description for {segment_id}",
        "signal_type": "marketplace",
        "data_provider": "Acme Data",
        "deployments": [{"type": "platform", "platform": "the-trade-desk", "is_live": True}],
        "pricing_options": [
            {"pricing_option_id": "po_cpm", "model": "cpm", "cpm": cpm, "currency": "USD"}
        ],
        "value_type": "binary",
        "coverage_forecast": {
            "points": [
                {
                    "dimensions": [
                        {"kind": "signal", "signal_id": segment_id, "presence": "present"}
                    ],
                    "metrics": {"coverage_rate": {"mid": 0.4}},
                }
            ],
            "forecast_range_unit": "availability",
            "method": "modeled",
            "scope": {"kind": "inventory", "label": "network inventory"},
            "bucket_semantics": "exclusive",
            "bucket_completeness": "partial",
            "generated_at": "2026-10-01T00:00:00Z",
        },
    }


FEED = [make_product("p1"), make_product("p2", cpm=12.0), make_product("p3")]


def version(items: list[Any], **kwargs: Any) -> str:
    kwargs.setdefault("cache_scope", "public")
    return wholesale_feed_version(items, **kwargs)


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def test_token_is_deterministic_and_opaque_hex() -> None:
    token = version(FEED)
    assert token == version(copy.deepcopy(FEED))
    assert token.startswith("sha256:") and len(token) == len("sha256:") + 64


def test_model_and_wire_dict_items_give_the_same_token() -> None:
    models = [Product.model_validate(item) for item in FEED]
    assert version(models) == version(FEED)


def test_top_level_order_does_not_matter() -> None:
    assert version(list(reversed(FEED))) == version(FEED)
    assert version([FEED[1], FEED[2], FEED[0]]) == version(FEED)


def test_key_order_does_not_matter() -> None:
    shuffled = [dict(reversed(list(item.items()))) for item in FEED]
    assert version(shuffled) == version(FEED)


def test_nested_array_order_is_content() -> None:
    # forecast.points ascend by budget, pricing_options are presented in
    # seller order: reordering inside an item is a (conservative) change.
    item = make_product("p1")
    item["pricing_options"].append(
        {"pricing_option_id": "po_cpc", "pricing_model": "cpc", "currency": "USD", "fixed_cpc": 1}
    )
    flipped = copy.deepcopy(item)
    flipped["pricing_options"].reverse()
    assert version([item]) != version([flipped])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda feed: feed[0].update(name="Renamed"),
        lambda feed: feed[0]["pricing_options"][0].update(fixed_cpm=19.0),
        lambda feed: feed[0]["forecast"]["points"][0]["metrics"]["impressions"].update(mid=1),
        lambda feed: feed[0]["forecast"].update(valid_until="2026-10-03T00:00:00Z"),
        lambda feed: feed.pop(),
        lambda feed: feed.append(make_product("p4")),
        lambda feed: feed.append(make_product("p1")),
    ],
    ids=["name", "price", "forecast-metric", "forecast-valid_until", "removed", "added", "dup"],
)
def test_any_buyer_visible_change_moves_the_token(mutate: Any) -> None:
    changed = copy.deepcopy(FEED)
    mutate(changed)
    assert version(changed) != version(FEED)


def test_forecast_generated_at_is_ignored() -> None:
    regenerated = copy.deepcopy(FEED)
    for item in regenerated:
        item["forecast"]["generated_at"] = "2026-10-29T09:30:00Z"
    assert version(regenerated) == version(FEED)
    # Opting out of the default makes it content again.
    assert version(regenerated, ignore=()) != version(FEED, ignore=())


def test_signal_coverage_forecast_generated_at_is_ignored() -> None:
    signals = [make_signal("s1"), make_signal("s2")]
    regenerated = copy.deepcopy(signals)
    regenerated[0]["coverage_forecast"]["generated_at"] = "2026-10-29T09:30:00Z"
    assert version(regenerated) == version(signals)
    regenerated[0]["coverage_forecast"]["method"] = "measured"
    assert version(regenerated) != version(signals)


def test_ignore_is_path_based_not_key_name_based() -> None:
    # A root-level generated_at is not forecast.generated_at.
    stamped = copy.deepcopy(FEED)
    stamped[0]["generated_at"] = "2026-10-29T00:00:00Z"
    assert version(stamped) != version(FEED)
    ignore = FRESHNESS_FIELDS | {"generated_at"}
    assert version(stamped, ignore=ignore) == version(FEED, ignore=ignore)


def test_ignore_paths_traverse_arrays() -> None:
    repriced = copy.deepcopy(FEED)
    repriced[0]["pricing_options"][0]["fixed_cpm"] = 99.0
    ignore = FRESHNESS_FIELDS | {"pricing_options.fixed_cpm"}
    assert version(repriced, ignore=ignore) == version(FEED, ignore=ignore)


@pytest.mark.parametrize("ignore", ["forecast.generated_at", ["forecast..generated_at"], [""]])
def test_malformed_ignore_paths_raise(ignore: Any) -> None:
    with pytest.raises(ValueError):
        version(FEED, ignore=ignore)


def test_cache_scope_keys_the_token() -> None:
    assert version(FEED, cache_scope="public") != version(FEED, cache_scope="account")
    with pytest.raises(ValueError, match="cache_scope"):
        version(FEED, cache_scope="global")


def test_extra_response_content_moves_the_token() -> None:
    # include_collections: the two settings get distinct tokens, even when
    # no collection is returned.
    base = version(FEED)
    with_none = version(FEED, extra={"collections": []})
    with_one = version(FEED, extra={"collections": [{"collection_id": "c1", "name": "News"}]})
    assert len({base, with_none, with_one}) == 3


def test_non_i_json_values_raise() -> None:
    item = make_product("p1")
    item["ext"] = {"big": 2**60}
    with pytest.raises(ValueError):
        version([item])


# ---------------------------------------------------------------------------
# Separate pricing version
# ---------------------------------------------------------------------------

STRUCTURE_ONLY = FRESHNESS_FIELDS | PRICING_FIELDS


def test_separated_pricing_moves_only_the_pricing_token() -> None:
    repriced = copy.deepcopy(FEED)
    repriced[0]["pricing_options"][0]["fixed_cpm"] = 25.0
    assert version(repriced, ignore=STRUCTURE_ONLY) == version(FEED, ignore=STRUCTURE_ONLY)
    assert pricing_version(repriced, cache_scope="public") != pricing_version(
        FEED, cache_scope="public"
    )


def test_structure_change_does_not_move_the_pricing_token() -> None:
    renamed = copy.deepcopy(FEED)
    renamed[0]["name"] = "Renamed"
    renamed[1]["forecast"]["generated_at"] = "2026-10-29T00:00:00Z"
    assert version(renamed, ignore=STRUCTURE_ONLY) != version(FEED, ignore=STRUCTURE_ONLY)
    assert pricing_version(renamed, cache_scope="public") == pricing_version(
        list(reversed(FEED)), cache_scope="public"
    )


def test_pricing_token_binds_prices_to_items() -> None:
    swapped = copy.deepcopy(FEED)
    swapped[0]["pricing_options"], swapped[1]["pricing_options"] = (
        swapped[1]["pricing_options"],
        swapped[0]["pricing_options"],
    )
    assert pricing_version(swapped, cache_scope="public") != pricing_version(
        FEED, cache_scope="public"
    )


def test_pricing_token_covers_nested_signal_targeting_options() -> None:
    def with_option(cpm: float) -> dict[str, Any]:
        item = make_product("p1")
        item["signal_targeting_options"] = [
            {
                "signal_ref": {"scope": "product", "signal_id": "auto"},
                "name": "Auto intenders",
                "pricing_options": [
                    {"pricing_option_id": "s", "model": "cpm", "cpm": cpm, "currency": "USD"}
                ],
            }
        ]
        return item

    cheap, dear = [with_option(1.0)], [with_option(2.0)]
    assert version(cheap, ignore=STRUCTURE_ONLY) == version(dear, ignore=STRUCTURE_ONLY)
    assert pricing_version(cheap, cache_scope="public") != pricing_version(
        dear, cache_scope="public"
    )


def test_pricing_token_covers_signals_and_needs_an_identity() -> None:
    signals = [make_signal("s1"), make_signal("s2")]
    repriced = [make_signal("s1", cpm=3.0), make_signal("s2")]
    assert pricing_version(signals, cache_scope="account") != pricing_version(
        repriced, cache_scope="account"
    )
    with pytest.raises(ValueError, match="product_id or signal_agent_segment_id"):
        pricing_version([{"name": "anonymous"}], cache_scope="public")


# ---------------------------------------------------------------------------
# Request preconditions
# ---------------------------------------------------------------------------


def _signals_brief(**extra: Any) -> dict[str, Any]:
    return {"signal_spec": "auto intenders", **extra}


INVALID_REQUESTS = [
    # (tool, request, offending field)
    (
        "get_products",
        {"buying_mode": "wholesale", "if_pricing_version": "p"},
        "if_pricing_version",
    ),
    (
        "get_products",
        {"buying_mode": "brief", "brief": "news", "if_wholesale_feed_version": "v"},
        "if_wholesale_feed_version",
    ),
    (
        "get_products",
        {
            "buying_mode": "refine",
            "refine": [{"scope": "request", "ask": "cheaper"}],
            "if_wholesale_feed_version": "v",
            "if_pricing_version": "p",
        },
        "if_wholesale_feed_version",
    ),
    (
        "get_signals",
        {"discovery_mode": "wholesale", "if_pricing_version": "p"},
        "if_pricing_version",
    ),
    ("get_signals", _signals_brief(if_wholesale_feed_version="v"), "if_wholesale_feed_version"),
]

VALID_REQUESTS = [
    ("get_products", {"buying_mode": "wholesale"}),
    ("get_products", {"buying_mode": "wholesale", "if_wholesale_feed_version": "v"}),
    (
        "get_products",
        {"buying_mode": "wholesale", "if_wholesale_feed_version": "v", "if_pricing_version": "p"},
    ),
    ("get_products", {"buying_mode": "brief", "brief": "news"}),
    ("get_signals", {"discovery_mode": "wholesale", "if_wholesale_feed_version": "v"}),
    ("get_signals", _signals_brief()),
]

MODELS = {"get_products": GetProductsRequest, "get_signals": GetSignalsRequest}


@pytest.mark.parametrize("as_model", [False, True], ids=["dict", "model"])
@pytest.mark.parametrize(("tool", "request_body", "field"), INVALID_REQUESTS)
def test_preconditions_reject_with_the_offending_field(
    tool: str, request_body: dict[str, Any], field: str, as_model: bool
) -> None:
    # The schema rejects the same request, so the helper and the wire agree.
    assert not validate_request(tool, request_body).valid
    request = MODELS[tool].model_validate(request_body) if as_model else request_body
    with pytest.raises(AdcpError) as raised:
        check_feed_preconditions(request)
    assert raised.value.code == "INVALID_REQUEST"
    assert raised.value.field == field
    with pytest.raises(AdcpError):
        feed_unchanged(request, feed_version="v")


@pytest.mark.parametrize("as_model", [False, True], ids=["dict", "model"])
@pytest.mark.parametrize(("tool", "request_body"), VALID_REQUESTS)
def test_preconditions_accept_valid_requests(
    tool: str, request_body: dict[str, Any], as_model: bool
) -> None:
    assert validate_request(tool, request_body).valid
    request = MODELS[tool].model_validate(request_body) if as_model else request_body
    check_feed_preconditions(request)


# ---------------------------------------------------------------------------
# Comparator
# ---------------------------------------------------------------------------


def _wholesale(**tokens: Any) -> GetProductsRequest:
    return GetProductsRequest(buying_mode="wholesale", **tokens)


def test_feed_unchanged_on_matching_token() -> None:
    assert feed_unchanged(_wholesale(if_wholesale_feed_version="v1"), feed_version="v1")
    assert not feed_unchanged(_wholesale(if_wholesale_feed_version="v0"), feed_version="v1")
    assert not feed_unchanged(_wholesale(), feed_version="v1")
    assert not feed_unchanged({"buying_mode": "brief", "brief": "news"}, feed_version="v1")


def test_feed_unchanged_is_false_on_continuation_pages() -> None:
    request = _wholesale(
        if_wholesale_feed_version="v1", pagination={"cursor": "page-2", "max_results": 10}
    )
    assert not feed_unchanged(request, feed_version="v1")
    first_page = _wholesale(if_wholesale_feed_version="v1", pagination={"max_results": 10})
    assert feed_unchanged(first_page, feed_version="v1")


def test_feed_unchanged_pricing_evaluation_order() -> None:
    both = _wholesale(if_wholesale_feed_version="v1", if_pricing_version="p1")
    assert feed_unchanged(both, feed_version="v1", pricing_version="p1")
    # (2) feed matches, pricing does not -> full payload.
    assert not feed_unchanged(both, feed_version="v1", pricing_version="p2")
    # (1) feed mismatch -> full payload regardless of pricing.
    assert not feed_unchanged(both, feed_version="v2", pricing_version="p1")
    # A seller that separates pricing cannot vouch for prices from the feed
    # token alone.
    feed_only = _wholesale(if_wholesale_feed_version="v1")
    assert not feed_unchanged(feed_only, feed_version="v1", pricing_version="p1")
    # A seller that does not track pricing ignores if_pricing_version.
    assert feed_unchanged(both, feed_version="v1")


# ---------------------------------------------------------------------------
# The unchanged answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "builder"), [("get_products", products_response), ("get_signals", signals_response)]
)
def test_unchanged_answer_validates_against_the_schema(tool: str, builder: Any) -> None:
    body = builder(
        unchanged=True,
        wholesale_feed_version=version(FEED),
        pricing_version=pricing_version(FEED, cache_scope="public"),
        cache_scope="public",
    )
    assert body["unchanged"] is True
    assert "products" not in body and "signals" not in body
    outcome = validate_response(tool, body)
    assert outcome.valid, outcome.issues


def test_unchanged_false_is_never_emitted() -> None:
    body = products_response(
        FEED, wholesale_feed_version=version(FEED), cache_scope="public", unchanged=False
    )
    assert "unchanged" not in body
    assert validate_response("get_products", body).valid
    signals = [make_signal("s1")]
    body = signals_response(
        signals, wholesale_feed_version=version(signals), cache_scope="public", unchanged=False
    )
    assert "unchanged" not in body
    assert validate_response("get_signals", body).valid


@pytest.mark.parametrize("builder", [products_response, signals_response])
def test_malformed_unchanged_answers_raise(builder: Any) -> None:
    with pytest.raises(ValueError, match="must omit"):
        builder([], unchanged=True, wholesale_feed_version="v", cache_scope="public")
    with pytest.raises(ValueError, match="must echo"):
        builder(unchanged=True, cache_scope="public")
    # cache_scope may be left to the dispatcher, which fills "public" for a
    # request without an account.
    assert builder(unchanged=True, wholesale_feed_version="v")["unchanged"] is True


# ---------------------------------------------------------------------------
# Round trip: buyer FeedMirror against a seller built from these helpers
# ---------------------------------------------------------------------------


class HelperSeller:
    """A wholesale seller that answers through the helpers and the schema."""

    def __init__(self, products: list[dict[str, Any]], *, separate_pricing: bool = False) -> None:
        self.products = products
        self.separate_pricing = separate_pricing
        self.responses: list[dict[str, Any]] = []

    def tokens(self) -> tuple[str, str | None]:
        if not self.separate_pricing:
            return version(self.products), None
        return (
            version(self.products, ignore=STRUCTURE_ONLY),
            pricing_version(self.products, cache_scope="public"),
        )

    async def get_products(self, request: GetProductsRequest) -> TaskResult[GetProductsResponse]:
        assert validate_request("get_products", request).valid
        feed_token, price_token = self.tokens()
        if feed_unchanged(request, feed_version=feed_token, pricing_version=price_token):
            body = products_response(
                unchanged=True,
                wholesale_feed_version=feed_token,
                pricing_version=price_token,
                cache_scope="public",
            )
        else:
            # Two-page walk: every page carries the whole feed's token.
            cursor = request.pagination.cursor if request.pagination else None
            start = int(cursor) if cursor else 0
            page = self.products[start : start + 2]
            more = start + 2 < len(self.products)
            body = products_response(
                page,
                wholesale_feed_version=feed_token,
                pricing_version=price_token,
                cache_scope="public",
                pagination={"has_more": more, **({"cursor": str(start + 2)} if more else {})},
            )
        outcome = validate_response("get_products", body)
        assert outcome.valid, outcome.issues
        self.responses.append(body)
        return TaskResult(
            status=TaskStatus.COMPLETED,
            success=True,
            data=GetProductsResponse.model_validate(json.loads(json.dumps(body))),
        )

    async def get_signals(self, request: GetSignalsRequest) -> TaskResult[GetSignalsResponse]:
        raise AssertionError("products only")


@pytest.mark.asyncio
@pytest.mark.parametrize("separate_pricing", [False, True], ids=["single", "separate-pricing"])
async def test_feed_mirror_round_trip_honours_unchanged(separate_pricing: bool) -> None:
    seller = HelperSeller(copy.deepcopy(FEED), separate_pricing=separate_pricing)
    mirror = FeedMirror(seller, strict_scope=True)

    await mirror.bootstrap("product")
    assert set(mirror.products) == {"p1", "p2", "p3"}
    assert len(seller.responses) == 2  # paginated full load

    # Same content, regenerated forecast: the mirror short-circuits.
    for product in seller.products:
        product["forecast"]["generated_at"] = "2026-10-29T09:30:00Z"
    result = await mirror.refresh("product")
    assert result.products_unchanged
    assert seller.responses[-1]["unchanged"] is True
    assert set(mirror.products) == {"p1", "p2", "p3"}

    # A re-price is a full payload, with or without a separate pricing token.
    seller.products[0]["pricing_options"][0]["fixed_cpm"] = 30.0
    result = await mirror.refresh("product")
    assert not result.products_unchanged
    assert "unchanged" not in seller.responses[-1]
    price = mirror.products["p1"].pricing_options[0].fixed_cpm
    assert price == 30.0

    result = await mirror.refresh("product")
    assert result.products_unchanged


@pytest.mark.asyncio
async def test_webhook_version_matches_the_conditional_read_token() -> None:
    seller = HelperSeller(copy.deepcopy(FEED))
    mirror = FeedMirror(seller, strict_scope=True)
    await mirror.bootstrap("product")
    previous = mirror.product_state.wholesale_feed_version

    # The seller removes p3 and announces it with the post-change token.
    seller.products = [p for p in seller.products if p["product_id"] != "p3"]
    current = version(seller.products)
    prepared = WebhookSender.prepare_wholesale_feed(
        url="https://buyer.example/webhooks/feed",
        subscriber_id="buyer-primary",
        account_id="acct_1",
        notification_type="product.removed",
        wholesale_feed_version=current,
        previous_wholesale_feed_version=previous,
        cache_scope="public",
        event={
            "event_id": "018f13f8-7b40-7000-8000-000000000123",
            "event_type": "product.removed",
            "entity_type": "product",
            "entity_id": "p3",
            "created_at": "2026-10-29T12:00:00Z",
            "payload": {"product_id": "p3", "applies_to": {"scope": "public"}},
        },
    )
    webhook = WholesaleFeedWebhook.model_validate_json(prepared.body)
    assert webhook.wholesale_feed_version == current
    await mirror.apply_webhook(webhook)

    # The buyer presents the webhook's version; the seller recognises it.
    result = await mirror.refresh("product")
    assert result.products_unchanged
    assert seller.responses[-1]["wholesale_feed_version"] == current
    assert set(mirror.products) == {"p1", "p2"}
