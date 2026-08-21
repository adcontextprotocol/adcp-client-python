# Migrating an integration from AdCP 3.1 to 3.2 beta

Python SDK 8 beta supports the AdCP `3.2.0-beta.4` schemas and the compact
product/media-buy lifecycle that becomes the foundation of AdCP 4.0. The SDK
continues to support AdCP 3.0 and 3.1, and the deprecated
`get_products`/`create_media_buy`/`update_media_buy` lifecycle remains available
throughout AdCP 3.x.

Protocol version and lifecycle shape are separate compatibility axes. A 3.2
seller may expose only the old lifecycle, a direct-buy subset of the compact
lifecycle, or the complete proposal lifecycle. Do not infer supported tools
from the version alone; read `media_buy.lifecycle_tools` or MCP `tools/list`.

## Pin the beta precisely

Use the release-precision prerelease identifier while 3.2 is in beta:

```python
client = ADCPClient(agent, adcp_version="3.2-beta.4")
server = adcp_server("seller", adcp_version="3.2-beta.4")
```

`"3.2"` intentionally does not alias to a prerelease. Exact prerelease pins
prevent a deployment from silently changing contracts when 3.2 stable ships.

## Beta.4 integration notes

AdCP 3.2.0-beta.4 retains beta.3 placement presentation and delegated preview
metadata and adds the signed products-only brief compatibility contract.
The SDK exports `PlacementPresentationDocument`,
`PlacementPresentationReference`, `PublisherDesignatedPreviewProvider`,
`PreviewRendererMetadata`, and `ReferenceRenderer` from `adcp` and
`adcp.types.creative`.

Treat every `preview_url` and `preview_html` as untrusted. URL previews belong
in a cross-origin iframe; HTML previews belong in iframe `srcdoc` with
`sandbox=""` and a caller-controlled restrictive CSP. Never inject preview HTML
into the host DOM. Provider `embedding` and `renderer` metadata is advisory and
does not grant authority or loosen those controls. `PreviewURLGenerator`
preserves this metadata and returns a mandatory `rendering_policy`; its cache is
bounded, size-limited, and expiry-aware.

Catalog-only `adagents.json` mirrors may now use `authorized_agents: []` when
at least one catalog collection is non-empty. This grants no sales authority.

Package requests may carry multiple selector routes only for 3.x compatibility.
Sellers must resolve each supplied route independently and reject different
selected product-option sets with `CONFLICTING_SELECTORS` before applying
precedence. This comparison is product-aware application logic; JSON Schema's
Draft 7 validator cannot enforce selector equivalence by itself.

When an older seller returns products without a proposal, a 3.2 compatibility
layer may project `outcome: products_available` with either a real seller-fenced
`listed_purchase` or an explicitly lossy `legacy_create` continuation. Use
`adcp.compat.LegacyPurchaseCoordinator` for the latter. It binds the principal,
account, exact source patch version, original seller session, full observed
product/pricing transaction, selected products, and accepted losses before an
atomic single-use claim. See
[Durable legacy purchase continuations](docs/legacy-purchase-continuations.md)
for storage, reconciliation, and migration requirements.

## Choose the lifecycle subset

| Workflow | Compact tools |
|---|---|
| Product feed/read | `list_products` |
| Direct buy | `list_products`, `buy_products`, `control_media_buy` |
| Proposal buy | `list_products`, `request_proposals`, `refine_proposals`, `decline_proposals`, `accept_proposal`, `control_media_buy` |
| Full compact | all proposal tools plus `buy_products` |

Proposal-only sellers do not need to advertise `buy_products`. Sellers can
combine the direct and proposal subsets when they support both paths. Declare
exactly what is implemented:

```python
from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform
from adcp.decisioning.capabilities import LifecycleTool, MediaBuy

class Seller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        media_buy=MediaBuy(
            lifecycle_tools=[
                LifecycleTool.list_products,
                LifecycleTool.buy_products,
                LifecycleTool.control_media_buy,
            ]
        ),
    )

    def list_products(self, req, ctx): ...
    def buy_products(self, req, ctx): ...
    def control_media_buy(self, req, ctx): ...

    # Keep the 3.x compatibility facades while older buyers migrate.
    def get_products(self, req, ctx): ...
    def create_media_buy(self, req, ctx): ...
    def update_media_buy(self, media_buy_id, patch, ctx): ...
```

Decisioning server startup fails if `lifecycle_tools` claims a method the
platform does not implement. Tools omitted from the declaration are not
advertised.

Class-based `ADCPHandler` servers and decorator servers use the same names:

```python
class Handler(ADCPHandler):
    async def list_products(self, params, context=None): ...
    async def request_proposals(self, params, context=None): ...
    async def refine_proposals(self, params, context=None): ...
    async def decline_proposals(self, params, context=None): ...
    async def buy_products(self, params, context=None): ...
    async def accept_proposal(self, params, context=None): ...
    async def control_media_buy(self, params, context=None): ...
```

## Update buyer calls

Request and response models are public from `adcp`, `adcp.types`,
`adcp.types.buyer`, and `adcp.types.media_buy`:

```python
from adcp import ListProductsRequest, BuyProductsRequest

products = await client.list_products(ListProductsRequest(...))
purchase = await client.buy_products(BuyProductsRequest(...))
```

The same methods are available on `ADCPMultiAgentClient`. Do not import from
`adcp.types._generated` or `adcp.types.generated_poc`.

Each stateful compact task has its own idempotency identity. Retry with the
same tool name and idempotency key; never retry `buy_products` as
`create_media_buy`, or `accept_proposal` as another operation. Compact buy and
control requests do not accept inline creatives—use the dedicated creative
lifecycle.

## Use version-scoped public models

The unqualified `adcp.types` namespace tracks the SDK's current 3.2 beta
surface. Applications that keep 3.0, 3.1, and 3.2 peers in the same process
can import schema-backed Pydantic models from the release namespace:

```python
from adcp.types.v31 import ListCreativesRequest as ListCreativesRequest31
from adcp.types.v32 import ListCreativesRequest as ListCreativesRequest32

legacy = ListCreativesRequest31(include_assignments=True)
current = ListCreativesRequest32(
    include_assignments=True,
    assignment_projection="matching",
)
```

These dict-shaped Pydantic models accept keyword construction, top-level
attribute access, `model_dump()`, and `model_json_schema()`, materialize
top-level schema defaults, and validate against the complete bundled
versioned JSON Schema. Each exported class is a `RootModel[dict[str, Any]]`:
it is intended as an exact-schema boundary validator and MCP schema source,
not as a drop-in, statically typed base class for adopter-defined models.
Static type checkers do not expose schema fields as typed model attributes,
and nested values remain plain dictionaries. Use the unqualified
current-version models when typed fields, typed nested objects, or model
subclassing are more important than multi-version isolation. Async variants
are available as `SubmittedResponse`, `WorkingResponse`, and
`InputRequiredResponse` suffixes.
`adcp.types.v30` provides the same surface for 3.0. A server
created with `adcp_server(..., adcp_version=...)` also uses that bundle for
MCP `tools/list`; tools absent from the pinned release are not advertised.
Class-based servers can pass `adcp_version=` to `create_mcp_tools()`.

## Select the request-signing profile

AdCP 3.2 tightens RFC 9421 handling: `Signature` Structured Fields binary
values use standard padded Base64, and every signed body-bearing request covers
`content-digest`. `ADCPClient` derives the signing profile from its trusted
`server_version` / `adcp_version` pin. An explicit profile is only needed to
override that negotiation, or when calling a low-level signer that has no
client pin:

```python
from adcp.signing import SigningConfig, VerifyOptions, sign_request

legacy_buyer = SigningConfig(
    private_key=key,
    key_id="buyer-key",
    signing_profile_version="3.1",
)

legacy_headers = sign_request(
    ...,
    signing_profile_version="3.1",
)

strict_3_2_verifier = VerifyOptions(
    ...,
    signing_profile_version="3.2",
)
```

For profile 3.2, low-level signers automatically cover `content-digest` on a
non-empty body when the coverage argument is omitted and reject an explicit
`cover_content_digest=False`. A 3.2 capability that advertises digest coverage
as forbidden is internally inconsistent and is rejected rather than producing
a non-conformant signature.

Choose the verifier profile from trusted endpoint configuration and negotiated
capabilities, never from an unsigned request-body field. The default verifier
profile remains 3.1-compatible so existing deployments do not begin rejecting
legacy signatures until they explicitly complete negotiation.

## Test the two-dimensional matrix

At minimum, exercise these rows independently:

| Wire version | Lifecycle variant | Expected surface |
|---|---|---|
| 3.0 | legacy | `get_products`, `create_media_buy`, `update_media_buy` |
| 3.1 | legacy | same legacy facade with canonical creative negotiation |
| 3.2 beta | legacy | compatibility facade still works |
| 3.2 beta | direct | list → buy → control |
| 3.2 beta | proposal | request → refine/decline → accept → control |

Keep legacy and compact tests against the same business implementation where
possible. This catches accidental divergence between compatibility facades and
the new task-specific contracts.
