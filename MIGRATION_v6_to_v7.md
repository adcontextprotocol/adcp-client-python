# Migrating from Python SDK 6 to 7

Python SDK 7 makes canonical creatives the default application contract. The
package release (`7.0.0-rc`) is distinct from the negotiated AdCP protocol
version (`3.0` or `3.1`). AdCP 3.2 is not advertised until the SDK ships its
validator bundle.

Use `Format`, `Product.format_options`, `format_kind`, and
`format_option_refs` in normal application code. `Format` now means a
canonical declaration. Products, packages, creatives, filters, delivery
reads, callbacks, generic task execution, multi-agent clients, server
handlers, response builders, and asset helpers enforce this boundary.

Legacy named-format identity is explicit:

```python
from adcp.types.legacy import LegacyGetProductsRequest

raw = await client.get_products_legacy(LegacyGetProductsRequest(...))
```

`FormatId` and `list_creative_formats` are no longer normal root surfaces.
Use `LegacyFormatId`, `adcp.types.legacy`, and methods ending in `_legacy`
only for migration or conformance tooling. These methods emit
`DeprecationWarning` and are scheduled for removal with AdCP 4.0.

For seller-owned named formats, configure `legacy_format_converter`. For
canonical selections persisted across a JSON/process boundary, configure a
separate `canonical_format_legacy_resolver`; the SDK never reverse-guesses a
legacy tuple. Catalog snapshots can build both:

```python
from adcp.canonical_formats import projection_adapters_from_catalog_snapshots

adapters = projection_adapters_from_catalog_snapshots(snapshots)
client = ADCPClient(
    agent,
    legacy_format_converter=adapters.legacy_format_converter,
    canonical_format_legacy_resolver=adapters.canonical_format_legacy_resolver,
)
```

AdCP 3.0 is upgraded on reads and downgraded on writes. AdCP 3.1 requires the
`media_buy.features.canonical_creatives` capability or unambiguous
request-local evidence. AdCP 3.2 will be canonical by contract once supported;
advertising `canonical_creatives: false` there will be an error.
