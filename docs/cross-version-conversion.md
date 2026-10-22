# Application and wire versions

Application requests, responses, and server handlers use the latest AdCP models
packaged with the SDK. The client discovers a seller's `adcp.supported_versions`
and chooses the highest common release for each call. Discovery follows the
existing `capabilities_ttl` cache; refreshing capabilities changes subsequent
calls, without changing requests already in flight. An advertisement containing
only `major_versions: [3]` selects the 3.0 compatibility contract.
For A2A, discovery uses a separate conversation and never changes a retained
business context or task ID. Refreshing capabilities while approval is pending
therefore preserves the original task for resumption.

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
validate responses before upgrading them. Server dispatch validates an older
request against its original schema, upgrades it before typed handler parsing,
and converts the response before wire validation. `ToolContext.resolved_adcp_version`
continues to describe the buyer's wire contract for transport-aware integrations.
Response envelope defaults and the 3.0 media-buy lifecycle status are adapted
at the boundary. Governance decision and acknowledgement statuses map to
`verdict` and `outcome_state`, including verdicts on audit check entries.
Frozen reporting feeds and receipt ingress retain their exact
version restrictions because they preserve signed/durable wire data.

Polling results use their `task_type` to convert the embedded `result`, including
results supplied on `list_tasks` entries. Loss pointers include `/result` or
`/tasks/<index>/result`. Unknown vendor task results remain opaque. Nested polling
wrappers remain opaque as well, bounding conversion to one wrapper level.

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

Every loss also produces a warning. Reports use JSON Pointers and contain no
discarded values. They remain available when a request fails validation or the
peer returns a failed task. Server reports use the same key in
`ToolContext.metadata`; response losses are appended after the handler returns
and are also logged. These diagnostics do not add unsupported fields to older
wire envelopes.

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
