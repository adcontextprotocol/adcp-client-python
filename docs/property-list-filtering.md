# Property-list resolution and filtering

Declare `Features(property_list_filtering=True)` to advertise seller filtering.
SDK mode uses `MCPPropertyListFetcher` by default; custom `PropertyListFetcher`
implementations returning publisher property IDs remain supported.

Both legacy `get_products.property_list` and AdCP 3.2
`criteria.offer_filters.property_list` are recognized. The compact location
wins if both are present. `PlatformHandler.list_products` and `request_proposals` apply the filter
before returning products. Explicit compact field selections are enriched
internally with inventory selectors and overlay support, then restored after
filtering so presentation fields do not prevent verification. Responses preserve their cursor, feed
version, and other fields without modifying the platform's original object.
Legacy discovery reports `property_list_applied=True`. Compact response schemas
forbid that top-level field, so `list_products` reports
`ext.property_list_applied=True`. Submitted, rejected, and unchanged envelopes pass through. Filtered
`products_available` responses update `listed_purchase.product_ids` while
preserving feed/pricing fences. If no products survive, or filtering would
invalidate immutable proposed terms or an opaque legacy continuation, the SDK
returns a schema-valid rejected outcome instead of rewriting bound terms.

The default resolver calls MCP `get_property_list` at the reference's
`agent_url`, presents `auth_token` as a bearer credential, and exhausts
`pagination.cursor` while `has_more` is true. It rejects repeated/missing
continuation cursors and mismatched list IDs. It retains identifier types,
including app and CTV IDs. Connections use public-destination validation,
IP-pinned delivery, disabled redirects, and disabled environment proxies.
There is no assumed `/property-lists/{id}` or `/lists/{id}` REST route.

Each resolver caches complete lists by URL, list ID, and credential digest.
The earliest `cache_valid_until` across pages controls expiration; a missing
or expired timestamp prevents caching. The default cache holds at most 128
lists, 250,000 identifiers, and a conservative 16 MiB of identifier data and reference keys. Each
resolution is limited to one million identifiers and 64 MiB, including source
and canonical strings and object overhead. Constructor options configure these
budgets; oversized resolutions fail without caching, and resolutions larger
than the cache budget are returned without being cached. Call `clear_cache()`
after a relevant change notification.

```python
from adcp import MCPPropertyListFetcher

resolver = MCPPropertyListFetcher()
resolved = await resolver.resolve(reference)
for identifier in resolved.identifiers:
    print(identifier.type, identifier.value)
```

SDK-owned seller filtering expands publisher selectors against public
`adagents.json` catalogs before matching typed identifiers. Strict products
require every property to match; products with canonical `overlay_support.property_list=True` (or legacy
`property_targeting_allowed=True`) need at least one matching property. Products without declared property selectors are excluded. Failed catalog
expansion for declared inventory returns a transient error instead of claiming
that filtering succeeded.

When a platform filters in its own database before pagination, select platform
mode. The seller then owns resolution, filtering, and acknowledgement; the SDK
preserves its response and performs no duplicate fetch or filtering:

```python
from adcp.decisioning import create_adcp_server_from_platform

handler, executor, registry = create_adcp_server_from_platform(
    seller, property_list_filter_mode="platform",
)
```

# Buyer checks

Client checks are opt-in and work across MCP and A2A:

```python
from adcp import ADCPClient, ProductPropertyPolicy

client = ADCPClient(
    seller_config,
    product_property_policy=ProductPropertyPolicy(mode="filter"),
)
```

`get_products` and `list_products`, including calls through `execute_task`,
resolve the requested list and expand seller selectors through the registry's
read-only product-identifier endpoint. Domain identifiers use AdCP domain
matching; other identifier types match canonical values of the same type.
Multiple identifiers for one property are alternatives, not separate properties.

`filter` removes proven mismatches. `flag` preserves every product. Both report
`match`, `mismatch`, or `unjudgeable` for each product in
`TaskResult.metadata["product_property_policy"]`. Unresolvable inventory and
list-fetch failures are unjudgeable and retained. This includes incomplete app
and CTV coverage. Seller cursors and totals are preserved; a filtered page can
be empty while later pages still contain matches. Checks concern discovered
inventory, not delivery targeting.

Supply `registry=` to select another registry, or `expand_identifiers=` with
an async `(seller_url, publisher_properties) -> {"identifiers": [...]}` callback
for a local catalog. Expansion rows should include `type`, `value`,
`publisher_domain`, and `property_id` so every selected property can be checked.

# Issuing shared references

`GovernanceHandler(property_list_token_verifier=...)` accepts a sync or async
`(list_id, context) -> bool` verifier. It runs after request validation and before
`handle_get_property_list` on every page. Only literal `True` permits access;
false results and verifier exceptions return `UNAUTHORIZED` without exposing
credentials. With no verifier, existing transport authentication applies.

Use a custom context factory to pass an authenticated list scope or a token to
the verifier. For example, on a dedicated shared-list MCP endpoint:

```python
from adcp.server import ToolContext, create_mcp_server

def context_factory(meta):
    if meta.tool_name != "get_property_list":
        raise ValueError("This endpoint only serves shared property lists")
    return ToolContext(metadata={
        "list_token": meta.request_context.headers.get("authorization"),
    })

# verify_list_access checks the credential against this exact list_id,
# including expiration/revocation, using the context's list_token.
issuer = MyGovernanceHandler(property_list_token_verifier=verify_list_access)
mcp = create_mcp_server(issuer, context_factory=context_factory)
```

On an endpoint with normal platform authentication, configure its authentication
layer to accept list credentials as an alternative and restrict that principal
to `get_property_list`. The handler hook does not bypass transport middleware.
Do not echo context metadata or bearer tokens into responses.

```python
from adcp import check_property_list_reference

# Resolves the issued reference through MCP, bypasses cache, follows every page.
resolved = await check_property_list_reference(reference)
assert resolved.pages >= 1
```

`WebhookSender.send_property_list_changed` supplies the required deprecated
payload marker `signature="rfc9421"` automatically. Actual RFC 9421 signatures
are computed in the HTTP headers by the configured JWK sender and cover the
complete payload. The old `signature=` argument remains accepted for source
compatibility; new payloads always use the marker.
