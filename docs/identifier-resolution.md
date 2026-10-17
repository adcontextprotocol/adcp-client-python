# Validating and resolving identifiers

Public helpers perform local syntax validation and return a canonical value
along with rewrite information:

```python
from adcp import normalize_identifier, validate_publisher_domain

result = normalize_identifier("domain", "EXAMPLE.com.")
assert result.value == "example.com"
assert result.rewritten

publisher = validate_publisher_domain("Example.com")
```

`normalize_domain`, `normalize_identifier`, and `validate_identifier` raise
`IdentifierValidationError` with `identifier_type` and `reason` on invalid
input. Hostnames canonicalize to lowercase IDNA2008 using non-transitional
UTS #46 processing and lose a trailing dot. For example, `faß.de` becomes
`xn--fa-hia.de`, preserving its distinction from `fass.de`.
Only domain identifiers allow a leading `*.`; publisher domains and subdomain
identifiers reject it. URLs, ports, IP addresses, embedded wildcards, and
invalid labels are rejected. Schemes and paths are never silently stripped.
Bundle/package IDs preserve case, numeric store IDs require digits, and Fire
TV ASINs canonicalize to uppercase. Other declared opaque ID types preserve
case and reject empty values or whitespace. Syntax validation establishes
neither ownership nor agent authorization.

Property save and publisher-domain lookup calls validate before making an
HTTP request. Use `normalize_identifier` when collecting a property list to
report errors at the point where data enters your application.

# Bulk catalog contribution

`RegistryClient.resolve_identifiers` calls `POST /api/registry/resolve`.
`mode="resolve"` is the bulk registration/contribution path: it creates missing
catalog entries, records provenance, and requires `auth_token`.
`mode="lookup"` performs read-only lookup without contributing entries or
activity. Both modes require a provenance envelope.

```python
from adcp import RegistryClient

async with RegistryClient(base_url=my_registry_url) as registry:
    result = await registry.resolve_identifiers(
        [
            {"type": "domain", "value": "example.com"},
            {"type": "android_package", "value": "com.example.app"},
            {"type": "roku_store_id", "value": "123"},
        ],
        mode="lookup",
        provenance={"type": "agency_allowlist", "context": "campaign-42"},
    )
```

Requests split automatically at 10,000 identifiers and `max_request_bytes`
(default 1 MiB of encoded JSON, including provenance). At most `concurrency`
requests run together (default four). Results preserve input order and summary
counts are summed. HTTP 429 and 503 retry up to `max_retries` (default three),
honoring `RegistryError.retry_after_seconds`; other errors propagate immediately.
Exhausted retries preserve the structured status and retry information.
Malformed JSON or incomplete resolve responses raise `RegistryError` without
including remote response values in the error message.
Resolve mappings are idempotent, but a repeated contribution can add activity.
A failed bulk call may already have completed other chunks.

`save_property` remains the per-publisher hosted configuration operation for
`authorized_agents` and property declarations. It is separate from bulk fact
contribution. A `property_rid` is a catalog join handle, not authorization.

Select a non-production `base_url` for integration environments or use lookup
mode when no contribution is intended. This SDK does not provision a registry
sandbox or assume that one is available.
