# Property-list filtering ownership

Declare `Features(property_list_filtering=True)` so buyers can discover that
your seller honors `get_products.property_list`.

The default `property_list_filter_mode="sdk"` requires a `PropertyListFetcher`.
The SDK resolves the buyer's list after your platform returns, filters products,
and sets `property_list_applied=True`. Dict and model responses are supported,
including dict products; other response fields are preserved, and the original
response is not mutated.

If your platform already resolves the list and filters before pagination,
select platform mode instead:

```python
from adcp.decisioning import create_adcp_server_from_platform

handler, executor, registry = create_adcp_server_from_platform(
    seller,
    property_list_filter_mode="platform",
)
```

The same keyword works with `adcp.decisioning.serve`. Keep the filtering
capability enabled. Platform mode requires no SDK fetcher and performs no SDK
fetch or duplicate post-filter, even if a fetcher is supplied. Your seller owns
list resolution, authorization, filtering and the `property_list_applied`
response flag. Return `True` only when the requested list was applied; the SDK
preserves your value, including `False` or an omitted flag.

Existing sellers retain SDK filtering by default. Sellers that previously
supplied a duplicate fetcher can remove it when selecting platform mode. Sellers
that disabled the capability to avoid duplicate filtering can re-enable it with
platform mode once their implementation honors the list.
