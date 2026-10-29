# Application and wire versions

Client application requests and responses use the latest AdCP models packaged
with the SDK; a server's handlers run on its application version (its pin,
else the newest of its `supported_versions`, else the packaged release; see
below). The client discovers a seller's `adcp.supported_versions`
and chooses the highest common release for each call. Discovery follows the
existing `capabilities_ttl` cache; refreshing capabilities changes subsequent
calls, without changing requests already in flight. An advertisement containing
only `major_versions: [3]` selects the 3.0 compatibility contract.
For A2A, discovery uses a separate conversation and never changes a retained
business context or task ID. Refreshing capabilities while approval is pending
therefore preserves the original task for resumption.

## Handlers receive requests converted to the application version

A server runs its handlers on one release, its *application version*:

1. the server's `adcp_version` pin, when it has one (`adcp_version=` on a
   server factory, or the handler's `get_adcp_version()` / `_adcp_version`);
2. else the newest release in its `supported_versions`;
3. else the release packaged with the SDK (`3.3-beta.0` in SDK 10).

`adcp.server.version_policy.resolve_application_adcp_version` is the one
resolver every dispatch path uses: `create_mcp_server`, `create_a2a_server`,
`serve`, `MCPToolSet`, `ADCPAgentExecutor`, `create_tool_caller`, and the
Decisioning `PlatformHandler` / `create_adcp_server_from_platform`.

Server dispatch validates an older or newer buyer's request against the
buyer's own schema, converts it to the application version, and only then
applies the unknown-field policy, application-schema validation and typed
parsing. The handler answers in the application version; the SDK validates
that answer against the application schema (under the configured response
mode) and converts it to the buyer's version: up (a 3.2 server answering a
`3.3-beta.0` buyer), unchanged (a same-release buyer), or down (a 3.1 or 3.0
buyer). A conversion the target schema refuses fails as a structured
`VERSION_UNSUPPORTED` error rather than reaching the peer invalid.

So a production seller can keep its handler code on the stable 3.2 contract
while the SDK's default is a pre-release:

```python
from adcp.server import ADCPHandler, ToolContext, create_mcp_server


class Seller(ADCPHandler):
    def get_adcp_version(self) -> str:
        return "3.2"  # the server pin; ADCPServerBuilder(adcp_version="3.2") sets the same

    async def get_products(self, params: dict, context: ToolContext):
        assert params["adcp_version"] == "3.2"           # the application version
        assert context.application_adcp_version == "3.2"
        buyer_version = context.resolved_adcp_version    # "3.1", "3.2" or "3.3-beta.0"
        ...


mcp = create_mcp_server(Seller())                                    # pinned by the handler
mcp = create_mcp_server(OtherSeller(), supported_versions=["3.2"])   # newest listed release
```

Since SDK 9.0.0 the handler has not seen the buyer's own envelope; SDK 10.0
makes the version it does see follow the server's pin instead of always being
the packaged release. A handler that tailors a response per buyer must read
the buyer's version from the context, not from `params`:

```python
class Seller(ADCPHandler):
    async def get_products(self, params, context: ToolContext):
        # A 3.1 buyer on a 3.2 server: params["adcp_version"] == "3.2" after
        # the conversion, and resolve_requested_adcp_version(params) is "3.2".
        buyer_version = context.resolved_adcp_version  # "3.1"
        ...
```

`resolve_requested_adcp_version(params)` inside a handler returns the
application version for any explicit `adcp_version` claim, so branches keyed
on it silently stop matching older buyers. It still describes a raw wire
payload correctly, before dispatch. `ToolContext.resolved_adcp_version` is the
buyer's negotiated wire contract and `ToolContext.application_adcp_version`
is the handler's. A2A requests without any version envelope leave
`resolved_adcp_version` `None` and are served as the application version.
Decisioning platforms read the same two values from `RequestContext`.

Most handlers need no version logic: return the application-version shapes
and let the SDK drop the fields an older buyer cannot represent. Branch on
`context.resolved_adcp_version` only when the business answer itself differs,
for example when withholding a product that an older buyer could not book.

### What a pin does and does not change for typed handlers

Dict handlers, schema validation and the unknown-field policy are exact: on a
3.2 server they use the bundled 3.2 schemas and nothing else. Typed handler
parameters (`params: GetProductsRequest`) and typed responses still use the
generated classes of the packaged release. Those classes are additive
supersets of 3.2 (the only 3.2 property absent from `3.3-beta.0` is
`evaluator.evaluator_id` on `build_creative`), so a 3.2 payload parses into
them unchanged and dumps back as the same 3.2 payload. A field that 3.3 added
reads as unset, its model default, on a 3.2 server; it is never written to the
wire unless the handler sets it, and a response that sets a field the 3.2
schema refuses fails strict response validation before conversion. For a
handler that must only ever name 3.2 fields, subclass the pinned bases in
`adcp.types.versioned_bases.v32`.

`adcp_version` sets a negotiation ceiling. A request's explicit `adcp_version`
overrides the constructor ceiling for that call. `server_version` declares a
known peer version and bypasses automatic capability discovery. Application
models remain current with either option. Unsupported tasks fail before sending
a request; the SDK never retries a mutation using another protocol version.

The SDK chains consecutive minor conversions: 3.2 ↔ 3.1 ↔ 3.0. The bundled
schemas identify newly introduced fields, including fields in nested arrays,
account-reference unions, and maps. Extension and context bags remain opaque.
Existing creative-format adapters supply deterministic legacy format routes;
formats that cannot be represented still require a configured resolver.
Historical prerelease pins retain their existing explicit compatibility gates;
minor converters do not infer compatibility between prereleases.

Some valid historical responses lack information required by the current
contract. The SDK cannot infer an omitted media-buy `revision`, because it is
the seller's optimistic concurrency token. Such a response fails conversion
with an unsupported-feature diagnostic identifying the missing field; the SDK
never invents a token. The client returns a failed `TaskResult` with
`adcp_error.code="UNSUPPORTED_FEATURE"` and preserves the original wire response
in `metadata["adcp_conversion_wire_response"]`. This is a local representation
failure after the peer completed the operation; reconcile that response before
deciding whether any further action is needed, and never blindly retry a
mutation because its result could not be upgraded. Diagnostics identify the
field pointer, source and target releases, and reason. An absent `confirmed_at`
becomes `null` only when the current contract permits unconfirmed state. Active
buys and packages carrying committed metrics require the seller's actual
confirmation timestamp instead.
When converting a response down to a release that permits omission but rejects
`null`, the SDK omits that field and records the loss. Request-side explicit
null clears instead fail if the older contract cannot express the clear.
Unsupported request conversions, including a missing common version or a task
absent from the negotiated release, return a failed `TaskResult` with
`adcp_error.code="UNSUPPORTED_FEATURE"` before sending the business request.
Its error details set `wire_response_received=false`; any supplied idempotency
key is retained. Explicit `fetch_capabilities()` calls still raise `ADCPError`
when discovery fails, while automatic discovery retains the failed task result.
Historical product reporting metrics that have no verified newer equivalent,
including 3.0's `video_completions`, also fail with an exact field pointer.
The SDK does not manufacture a mapping to `completed_views`.

Older governance registrations can name several agents or restrict an agent to
specific `categories`. AdCP 3.1+ instead assigns one agent the full account
lifecycle. The SDK rejects those older scoped or multi-agent registrations with
a structured version-conversion error before handler dispatch; it never drops
scope restrictions or merges governance authorities automatically. A legacy
single-agent registration without categories upgrades normally.

Both MCP and A2A validate requests after converting to the wire version and
validate responses before upgrading them. Server dispatch validates a buyer's
request against its original schema, converts it to the application version
before typed handler parsing, validates the handler's response against the
application schema, and converts it before wire validation.
`ToolContext.resolved_adcp_version` continues to describe the buyer's wire
contract for transport-aware integrations; `ToolContext.application_adcp_version`
is the handler's.
Response envelope defaults and the 3.0 media-buy lifecycle status are adapted
at the boundary. Governance decision and acknowledgement statuses map to
`verdict` and `outcome_state`, including verdicts on audit check entries.
Frozen reporting feeds and receipt ingress retain their exact
version restrictions because they preserve signed/durable wire data.

Polling results use their `task_type` to convert the embedded `result`, including
results supplied on `list_tasks` entries. Loss pointers include `/result` or
`/tasks/<index>/result`. Unknown vendor task results remain opaque. Nested polling
wrappers remain opaque as well, bounding conversion to one wrapper level.

## Downgrades never emit a payload the older schema refuses

Dropping a newer field or enum member can leave its parent in a shape the
older release refuses: a notification config whose `event_types` were all
3.2-only becomes an empty list below 3.1's `minItems: 1`. When a node loses or
coarsens a direct child, the SDK checks the node's own constraints in the older
schema (`required`, `minItems`, `minProperties`, unions, `if`/`then`). A
refusal counts as caused by the drop only when the newer schema accepted the
original and the older schema accepts the node with the dropped children put
back. Such a node is resolved by one rule:

| Situation | Result | Loss `reason` |
| --- | --- | --- |
| Newer `boolean \| object` capability, older boolean only (3.2 `geo_regions`) | `true` when the structure declares any support, otherwise `false` | `coarsened_to_boolean` |
| Response: refused node is an optional field | Field omitted | `unrepresentable_in_target` |
| Response: refused node is an array item | Item omitted | `unrepresentable_in_target` |
| Request: any refused node | `ADCPVersionConversionError` | none |
| Response: only required fields lead to the root | `ADCPVersionConversionError` | none |

Requests are never repaired by omission, because omitting a request field
changes its meaning: a shortened sync list can read as removal under
`delete_missing`, and an absent filter matches everything. A request whose
`filters.required_metrics` were all 3.2-only therefore fails rather than
widening the search. Optional newer members are still dropped one at a time
(`enum_unavailable`) when others remain, as before. Losses already recorded
inside a dropped response node stay in the report, explaining why it went.

Every downgrade then validates the result against the older release's bundled
schema. A refusal the projection did not cause fails with
`ADCPVersionConversionError` instead of reaching the peer invalid. Examples are
a field the older release requires and the payload never had, such as 3.1
delivery `totals.spend`, and a required enum member the older release cannot
spell. That error's pointer locates the value in the converted payload. Its
reason never echoes payload values. A payload already invalid in its own
release is projected but neither repaired nor rejected; the caller's
validation policy decides. The server returns these errors as
`VERSION_UNSUPPORTED`, including after a handler has run. The client returns a
failed `TaskResult` with `adcp_error.code="UNSUPPORTED_FEATURE"`. Code that
reconstructs a peer's own evidence, rather than sending a payload, can pass
`require_valid_target=False` to `convert_payload` to skip the final check.

`tests/test_version_conversion_sweep.py` converts every bundled 3.2 schema
example and fixture down to 3.1 and 3.0 and requires each result to validate,
or to fail with one of the structured errors it lists.

## Conversion reports

Client results expose conversion details in
`TaskResult.metadata["adcp_version_conversion"]`:

```python
{
    "wire_version": "3.1",
    "losses": [
        {
            "pointer": "/accounts/0/brand/countries",
            "source_version": "3.2",
            "target_version": "3.1",
            "reason": "field_unavailable",
        }
    ],
}
```

Reports use JSON Pointers and contain no discarded values. They remain
available when a request fails validation or the peer returns a failed task.
Server reports use the same key in `ToolContext.metadata`; response losses are
appended after the handler returns. These diagnostics do not add unsupported
fields to older wire envelopes.

## Loss logging

Losses are logged on the `adcp.compat.versions` logger at a level that depends
on which side of the wire converts:

| Side | Per-loss record | Summary |
| --- | --- | --- |
| Client (buyer converting its request or a seller's response) | `WARNING` | none |
| Server dispatch (request and response conversion to and from the application version) | `DEBUG` | one `INFO` line per conversion with losses |

On the client, a drop means the buyer lost data it may care about. On the
server, dropping newer fields for an older buyer is the expected cost of serving
it, and `ToolContext.metadata` already records each loss. A 3.2 seller answering
a 3.1 `get_products` with 50 products logs one line instead of 150 warnings:

```text
INFO adcp.compat.versions: AdCP conversion of get_products 3.2 -> 3.1 dropped 150 field(s):
/products/*/audience_evidence, /products/*/included_signals/*/demographic_predicate,
/products/*/included_signals/*/restricted_attributes
```

The summary collapses array indexes to `*` and lists at most ten distinct
pointers. A request upgrade adds fields rather than drops them, so a server
serving older buyers logs at most one summary per response; a server pinned
below a buyer's release may also log one for the request it converts down. Enable `DEBUG` on `adcp.compat.versions` to
see every exact pointer, or read them from the metadata report.

Code calling `adcp.compat.versions.convert_payload` directly chooses the level
with `loss_log_level`; it defaults to `WARNING`.

## Account identity

The upstream account-key decision in
[adcp#8373](https://github.com/adcontextprotocol/adcp/issues/8373) is still open.
Until that decision is settled, this SDK **reduces and shares** when a new buyer
contacts an older seller: it omits `brand.countries`, `operator_unit`, `currency`,
and `timezone` wherever the old account schema cannot express them, and reports
each omission. It preserves the published brand ID and the remaining key fields.
Buyer accounts differing only in omitted fields therefore address the same
seller account. Applications can use the reports to surface billing or account
collisions. An older buyer reaching a newer server keeps its original key;
the SDK does not invent missing identity components.

The provisioning registry correlates reduced echoes only when they identify
one complete input key. It does not guess associations for ambiguous merged
batch results.

For a successfully recorded reduced identity, the registry also retains the
natural key used on the wire. If the seller later advertises 3.2, subsequent
calls keep addressing that existing account by its reduced key and expose
`account_identity_migration_pending` in the conversion report. This avoids
silently creating another account or losing access to its buys and history.
Complete any identity migration explicitly before replacing that registry
binding. Durable `AccountStorage` adapters must persist the optional
`AccountRecord.wire_key` and `wire_version` fields to preserve this behavior
across client restarts.

## Conformance

`tests/test_cross_version_conversion.py` exercises both client transports,
server dispatch, account reduction, nested references, and chained conversions
against the bundled schemas. Upstream has not published the shared cross-version
cases requested by adcp#8373 yet; those cases must be added to the conformance
suite when they are available.
