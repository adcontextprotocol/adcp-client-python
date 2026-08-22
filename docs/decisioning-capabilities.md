# Declaring capabilities for a decisioning platform

A platform tells buyers what it can do via the `get_adcp_capabilities`
response. Buyers read it once at agent discovery and use it to choose
products, target safely, and decide which protocols to call.

The SDK projects your capability declaration into a spec-conformant
response automatically — you declare in Python, the framework emits the
wire shape. This guide covers what you can declare and how.

## The shape

```python
from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform
from adcp.decisioning.capabilities import (
    Account,
    Adcp,
    Execution,
    GeoMetros,
    IdempotencySupported,
    MediaBuy,
    Specialism,
    Targeting,
)


class MySeller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=[Specialism.sales_non_guaranteed],
        adcp=Adcp(
            major_versions=[3],
            idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
        ),
        account=Account(supported_billing=["operator"]),
        media_buy=MediaBuy(
            supported_pricing_models=["cpm"],
            execution=Execution(
                targeting=Targeting(
                    geo_countries=True,
                    geo_metros=GeoMetros(nielsen_dma=True),
                ),
            ),
        ),
    )

    accounts = ...  # AccountStore impl
```

The names mirror the AdCP wire spec 1:1 — every type in
`adcp.decisioning.capabilities` corresponds to a field type in
`protocol/get-adcp-capabilities-response.json`. Read your declaration
alongside the spec; they line up.

## What to declare

### Always

- **`specialisms`** — drives the dispatch validator (it checks that
  declared specialisms have the methods they require) and feeds the
  derived `supported_protocols`. Use `Specialism.*` enum members for
  type safety; spec slugs like `"sales-non-guaranteed"` work too and
  get coerced at construction.
- **`adcp`** — `major_versions` plus an idempotency declaration. The
  spec requires `adcp.idempotency`; if you skip this block, the
  framework emits a default `{"supported": False}` so the response stays
  spec-valid, but buyers reading it will mark you unsafe for retries.
  Declare it.
- **`account.supported_billing`** — required by the spec whenever
  `media_buy` is in `supported_protocols`. Pick a subset of `operator`,
  `agent`, `advertiser`.

### When you support media buying

- **`media_buy.supported_pricing_models`** — your full portfolio.
  Individual products may support a subset.
- **`media_buy.execution.targeting`** — every dimension you actually
  honor. Buyers read this before sending targeting payloads.
  Don't claim what you can't enforce — the spec is explicit that a
  declared capability is a commitment.
- **`media_buy.reporting_delivery_methods`** — push delivery formats
  beyond polling.
- **`media_buy.features`** — `inline_creative_management`,
  `property_list_filtering`, `catalog_management` flags that gate
  buyer-side flow.

### When you support other protocols

- **`signals`**, **`governance`**, **`sponsored_intelligence`**,
  **`brand`**, **`creative`** — declare the matching block when you
  claim that protocol.

### Cross-cutting posture

- **`request_signing`** — RFC 9421 inbound signature support. Adopters
  with signed-request infrastructure declare `supported=True` plus the
  `required_for` / `warn_for` operation lists.
- **`webhook_signing`** — outbound RFC 9421 webhook profile.
- **`identity`** — operator key-scoping / compromise-response posture.
  Advisory in 3.x; useful for buyer-side onboarding decisions.
- **`compliance_testing`** — declare when you support
  `comply_test_controller`-driven scenarios.

## Request-scoped capability blocks

Most capability declarations are static. Multi-tenant agents sometimes
need values that depend on the current request context: for example,
`media_buy.portfolio.publisher_domains` from the tenant's publisher
partner table, or `webhook_signing` only when that tenant has an active
locally usable signing credential.

Override `DecisioningPlatform.get_adcp_capabilities_for_request()` for
those cases. Return `None` to use the class-level declaration unchanged,
or return a complete `DecisioningCapabilities` instance for this
request. The SDK still owns the canonical `get_adcp_capabilities`
response shape.

The hook may enrich request-specific capability blocks, but it must not
change `specialisms` or the effective `supported_protocols`. Those fields
drive boot-time method validation and `tools/list`, so they stay static
for the handler instance.

```python
from dataclasses import replace

from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform
from adcp.decisioning.capabilities import (
    Account,
    MediaBuy,
    Portfolio,
    Specialism,
    SupportedProtocol,
    WebhookSigning,
)


class MultiTenantSeller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        # Static defaults that are valid without tenant context.
        specialisms=[Specialism.sales_non_guaranteed],
        supported_protocols=[SupportedProtocol.media_buy],
        account=Account(supported_billing=["operator"]),
        media_buy=MediaBuy(supported_pricing_models=["cpm"]),
    )

    def get_adcp_capabilities_for_request(self, params=None, context=None):
        if context is None or context.tenant_id is None:
            return None

        tenant = self.lookup_tenant(context.tenant_id)
        base = self.capabilities
        media_buy = base.media_buy.model_copy(
            update={
                "portfolio": Portfolio(
                    publisher_domains=tenant.publisher_domains,
                )
            }
        )
        webhook_signing = (
            WebhookSigning(
                supported=True,
                profile="adcp/webhook-signing/v1",
                algorithms=[tenant.signing_algorithm],
                delivery_retry_horizon_seconds=86_400,
            )
            if tenant.has_active_signing_credential
            else None
        )
        return replace(
            base,
            media_buy=media_buy,
            webhook_signing=webhook_signing,
            webhook_signing_managed_externally=tenant.has_active_signing_credential,
        )
```

Set `webhook_signing_managed_externally=True` only when an external durable
outbox signs and publishes outbound webhooks outside the SDK sender stack. Start
the handler with `auto_emit_task_webhooks=False` and do not wire an SDK sender or
supervisor. The external owner must retain immutable payload/key bindings and
retry state for the advertised horizon, and reconcile terminal state with its
outbox before reporting completion.

For a push-configured operation, TaskHandoff and WorkflowHandoff admission calls
this same request-scoped hook with the operation request and current
`ToolContext`. The effective capability set must prove that the selected tenant
has an external durable publisher; a static declaration cannot authorize push
for a tenant whose scoped capability set omits it. Keep the hook deterministic
for the same authenticated tenant so discovery and later operation admission
cannot disagree.

The hook may be synchronous or asynchronous. During discovery it receives the
typed `get_adcp_capabilities` request; during push admission it receives the
typed operation request (`create_media_buy`, `update_rights`, and so on). Custom
dispatch paths may pass a dict. Treat `params` as a request union—or branch on
its type—and derive tenant identity from the current `ToolContext`, such as
`context.tenant_id`, auth middleware metadata, or a custom context subclass.

## What you don't declare directly

The framework auto-derives a few things:

- **`supported_protocols`** — derived from the union of
  `SPECIALISM_TO_PROTOCOLS` over your declared specialisms. Override
  by setting `supported_protocols=[SupportedProtocol.media_buy, ...]`
  explicitly when claiming a protocol whose specialisms aren't all
  enumerated.

  **Spec note**: per the AdCP spec, `supported_protocols` is the
  primary storyboard-commitment declaration; specialisms are
  sub-claims that *roll up to* a protocol. The auto-derive direction
  inverts that data flow for ergonomics. It works fine in practice,
  but the spec-aligned form is to declare both — the SDK emits a
  one-shot `UserWarning` at construction when you omit
  `supported_protocols`. The warning is a nudge, not a deprecation
  (auto-derive stays supported indefinitely):

  ```python
  # Auto-derive — works, fires a one-shot UserWarning per declaration site.
  DecisioningCapabilities(specialisms=[Specialism.sales_non_guaranteed])

  # Spec-aligned — silent.
  DecisioningCapabilities(
      specialisms=[Specialism.sales_non_guaranteed],
      supported_protocols=[SupportedProtocol.media_buy],
  )
  ```

- **Wire-level `specialisms` field** — emitted from spec-known entries
  in your `specialisms` list. Novel/typo strings stay diagnostic-only at
  the dispatch validator and don't leak into the wire.

## Targeting capabilities — claim what you honor

The wire schema has fine-grained targeting-capability declarations:

```python
from adcp.decisioning.capabilities import (
    GeoMetros, GeoPostalAreas, Targeting,
)

targeting = Targeting(
    geo_countries=True,
    geo_regions=True,
    geo_metros=GeoMetros(nielsen_dma=True, eurostat_nuts2=True),
    geo_postal_areas=GeoPostalAreas(us_zip=True, gb_outward=True),
    language=True,
)
```

The dispatch validator walks each declared dimension at runtime when a
buyer sends a targeting payload — claiming `geo_countries=True` while
your adapter ignores the field is the kind of bug the validator
catches. Be honest in declarations.

## Common mistakes

### Declaring `account.required_for_products=True` without OAuth

If you require operator credentials before letting buyers list
products, also declare `authorization_endpoint` so the buyer's agent
can drive the operator through OAuth. Otherwise the buyer sees
`required_for_products=True` and has nowhere to send the operator.

### Mixing legacy and structured forms

```python
DecisioningCapabilities(
    pricing_models=["cpm"],                            # legacy
    media_buy=MediaBuy(supported_pricing_models=["cpcv"]),  # structured
)
```

When both are set, the structured form wins. The legacy field still
fires a `DeprecationWarning` at projection time, telling you to remove
it. Pick one.

### Claiming a protocol you don't fully implement

Each protocol claim commits you to passing the baseline storyboard at
`/compliance/{version}/protocols/{protocol}/`. If you can't run the
storyboard end-to-end, don't claim the protocol — buyers will hold you
to it.

## Migration from the flat shortcuts

The flat fields `pricing_models`, `supported_billing`, `channels` are
deprecated. The mapping is direct:

| Legacy | Structured equivalent |
|---|---|
| `pricing_models=["cpm"]` | `media_buy=MediaBuy(supported_pricing_models=["cpm"])` |
| `supported_billing=["operator"]` | `account=Account(supported_billing=["operator"])` |
| `channels=["display"]` | `media_buy=MediaBuy(portfolio=Portfolio(primary_channels=["display"], publisher_domains=[...]))` |

The `channels` migration path is special — the spec's
`portfolio.primary_channels` requires `publisher_domains` alongside,
which the flat field can't carry. The legacy field hasn't been emitted
to the wire since the projection rewrite. Adopters with channels
declarations should construct a full `Portfolio` block or remove the
declaration.

## Where capabilities run

- **Construction time** — `DecisioningCapabilities.__post_init__`
  coerces spec-known specialism strings to `Specialism` enum members.
  Novel slugs pass through unchanged so the dispatch validator can
  surface them.
- **Server boot** — `validate_capabilities_response_shape` runs the
  full projection synchronously and validates the output against
  `protocol/get-adcp-capabilities-response.json`. Boot-time errors here
  are the cleanest possible — fix the static declaration, the
  request-scoped capabilities hook, or a custom handler override.
- **Discovery time** — every `get_adcp_capabilities` call reads the
  declaration, gives `get_adcp_capabilities_for_request()` a chance to
  return a request-scoped override, then projects the result to the wire.

## Custom blocks

For vendor-specific capability fields the spec doesn't define, use
`config={"vendor_extension": ...}` — surfaced under `config` in the
projection. Don't reach into the structured blocks for vendor data;
their shapes are spec-bound and may break under regeneration when the
spec evolves.

## See also

- `examples/v3_reference_seller/src/platform.py` — the canonical
  motivating example. Declares `specialisms`, `account`, `media_buy`.
- `tests/test_decisioning_capabilities_submodule.py` — round-trips for
  every block including a fully-populated schema-validated response.
- AdCP spec — `protocol/get-adcp-capabilities-response.json` defines
  every field the structured blocks mirror. Read both in parallel.
