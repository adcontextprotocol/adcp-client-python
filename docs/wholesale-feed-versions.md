# Wholesale feed versions (seller side)

A wholesale `get_products` (`buying_mode="wholesale"`) or `get_signals`
(`discovery_mode="wholesale"`) response carries an opaque
`wholesale_feed_version`. A buyer such as `adcp.FeedMirror` echoes it back as
`if_wholesale_feed_version` and expects `unchanged: true`, with the item list
omitted, when nothing it can see has changed. `adcp.server.feed_version`
provides the seller side:

| Helper | Job |
| --- | --- |
| `wholesale_feed_version(items, *, cache_scope, ignore=FRESHNESS_FIELDS, extra=None)` | Content token for the feed |
| `pricing_version(items, *, cache_scope)` | Token for the pricing layer, for sellers that version pricing separately |
| `check_feed_preconditions(request)` | Mode and pairing rules for `if_*` tokens. Raises `AdcpError("INVALID_REQUEST", field=...)` |
| `feed_unchanged(request, *, feed_version, pricing_version=None)` | Returns whether the buyer's tokens are current |

All four are exported from `adcp.server`.

```python
from adcp.server import feed_unchanged, products_response, wholesale_feed_version

async def get_products(params, context=None):
    scope = "account" if account_has_overrides(params.account) else "public"
    feed = await load_wholesale_feed(params, scope)   # the whole feed, not one page
    version = wholesale_feed_version(feed, cache_scope=scope)
    if feed_unchanged(params, feed_version=version):  # also runs check_feed_preconditions
        return products_response(
            unchanged=True, wholesale_feed_version=version, cache_scope=scope
        )
    page, pagination = paginate(feed, params.pagination)
    return products_response(
        page, wholesale_feed_version=version, cache_scope=scope, pagination=pagination
    )
```

`get_signals` follows the same pattern with `signals_response`.

## The token

The token is `"sha256:"` followed by 64 hex digits. The schema places no
format or length constraint on it, and buyers must treat it as opaque. Equal
buyer-visible content always produces the same token. Any buyer-visible change
produces a different one.

- **Wire form.** Each item, whether a dict or a Pydantic model, is serialized
  the way `products_response` / `signals_response` puts it on the wire. It is
  then RFC 8785 (JCS) canonicalized and hashed with SHA-256.
- **Order.** Only the order of top-level items is ignored, because a feed is a
  set keyed by `product_id` / `signal_agent_segment_id`. Arrays inside an item
  keep their order: some are ordered by contract (`forecast.points` ascend by
  budget), and the schema declares no set semantics for the rest. A seller
  that emits a stable order gets stable tokens. A reordering costs at most one spurious
  refetch, while a missed change would leave the buyer with stale data.
- **Freshness fields.** `ignore` takes dot paths from the item root, and
  arrays are traversed without a path segment. `FRESHNESS_FIELDS` is
  `forecast.generated_at` (products) and `coverage_forecast.generated_at`
  (signals): a regenerated forecast with the same numbers is not a change.
  `valid_until` is still hashed, because buyers read it to decide whether a
  forecast has expired. A bare key matches only at the item root, never by
  name anywhere in the item.
- **Scope.** `cache_scope` is part of the hash. The spec keys the comparator
  on `(cache_scope, wholesale_feed_version)`, so a public token never matches
  an account-scoped read, even when the content is identical.
- **Pages.** `pagination.cursor` is outside the version's scope. Hash the
  whole feed and put the same token on every page. `feed_unchanged` returns
  `False` on continuation pages. Hashing is linear in the feed size, so a
  seller with a large feed computes the token when the feed changes and
  stores it alongside the feed.
- **Response-level content.** Pass `extra` for content outside the item list
  that the buyer sees and that must move the version. AdCP 3.2 responses
  carry none; it exists for AdCP 3.3's `collections` (SDK 10). An empty value
  still produces a different token from no `extra`.

## Separate pricing version

`pricing_version` covers `PRICING_FIELDS`, which are top-level
`pricing_options` and `signal_targeting_options[].pricing_options`, the
fields `GetProductsResponse.pricing_version` names. It binds each price to its
item's ID. To separate pricing:

```python
feed_token = wholesale_feed_version(feed, cache_scope=scope,
                                    ignore=FRESHNESS_FIELDS | PRICING_FIELDS)
price_token = pricing_version(feed, cache_scope=scope)
if feed_unchanged(params, feed_version=feed_token, pricing_version=price_token):
    return products_response(unchanged=True, wholesale_feed_version=feed_token,
                             pricing_version=price_token, cache_scope=scope)
```

`feed_unchanged` follows the spec's evaluation order: a feed mismatch returns
the full payload, and so does a matching feed with a pricing mismatch. Only
when both match is the answer `unchanged`. If the seller passes
`pricing_version` and the buyer did not send `if_pricing_version`, the answer
is the full payload, because the feed token alone cannot show that prices are
current. Separating pricing is optional. A wholesale feed webhook carries
only `wholesale_feed_version`, and with separate pricing that token does not
move on a price-only change.

## Request rules

`check_feed_preconditions` rejects the following with `INVALID_REQUEST`:

- `if_pricing_version` without `if_wholesale_feed_version`, with
  `field="if_pricing_version"`.
- Either token on a read that is not wholesale, with
  `field="if_wholesale_feed_version"`. A `get_signals` request without
  `discovery_mode` is a brief read.

It accepts request models and wire dicts. It complements
`adcp.decisioning.refine.assert_buying_mode_consistent`, which checks `brief`
and `refine` but not these tokens.

## The unchanged answer

`products_response(unchanged=True, ...)` and `signals_response(unchanged=True, ...)`
omit the item list and require `wholesale_feed_version`. Pass `cache_scope`
too: the schema requires it, and the MCP dispatcher fills in `public` only
when the request has no account. Echo `pricing_version` when you use it.
Passing items with `unchanged=True`, or leaving out the version, raises
`ValueError`. `unchanged=False`
emits nothing, because the spec forbids `unchanged: false`: a response that
carries products simply has no `unchanged` field.

## Webhooks

After a change, pass the post-change token to the wholesale feed webhook:

```python
await sender.send_wholesale_feed(
    ...,
    wholesale_feed_version=wholesale_feed_version(feed_after, cache_scope="public"),
    previous_wholesale_feed_version=token_before,
    cache_scope="public",
)
```

A mirror that applies the webhook stores this version and presents it on its
next conditional read. Since the read computes the same token from the same
feed, the seller answers `unchanged: true`. A buyer that uses the version as a
gap detector therefore sees no mismatch.

`list_products` returns `feed_version` and accepts `if_feed_version`, the
same concept under a different name. The token functions work for it
unchanged. `feed_unchanged` and the response builders cover `get_products`
and `get_signals`.
