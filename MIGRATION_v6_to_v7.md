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

## Synchronous completion webhooks

`auto_emit_completion_webhooks` now defaults to `False`. AdCP forbids a task
webhook when the initial response is already terminal: the result is available
inline and no registry task exists for a webhook `task_id`.

If an existing buyer depends on receiving both copies, temporarily pass
`auto_emit_completion_webhooks=True` to `serve()` or
`create_adcp_server_from_platform()`. This retains the former behavior as a
non-conformant compatibility extension with a synthetic, unpollable `sync-*`
task ID. Update the buyer to consume the inline result, then remove the opt-in.

This setting only controls synthetic synchronous-completion delivery. Terminal
webhooks for real `TaskHandoff` requests remain enabled when the request supplies
`push_notification_config` and a webhook sender or supervisor is configured. The
framework rejects a push-configured handoff before task creation when no transport
is available, rather than returning `submitted` and silently dropping the callback.
Adopters that deliver terminal task webhooks themselves can set the independent
`auto_emit_task_webhooks=False` ownership flag.
