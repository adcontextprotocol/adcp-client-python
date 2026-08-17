# Migrating an integration from AdCP 3.1 to 3.2 beta

Python SDK 8 beta supports the AdCP `3.2.0-beta.0` schemas and the compact
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
client = ADCPClient(agent, adcp_version="3.2-beta.0")
server = adcp_server("seller", adcp_version="3.2-beta.0")
```

`"3.2"` intentionally does not alias to a prerelease. Exact prerelease pins
prevent a deployment from silently changing contracts when 3.2 stable ships.

## Choose the lifecycle subset

| Workflow | Compact tools |
|---|---|
| Product feed/read | `list_products` |
| Direct buy | `list_products`, `buy_products`, `control_media_buy` |
| Proposal buy | `request_proposals`, `refine_proposals`, `decline_proposals`, `accept_proposal` |

Sellers can combine these subsets. Declare exactly what is implemented:

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

## Select the request-signing profile

AdCP 3.2 tightens RFC 9421 handling: `Signature` Structured Fields binary
values use standard padded Base64, and every signed body-bearing request covers
`content-digest`. The SDK signer defaults to the 3.2 wire format. Select a
legacy profile only when negotiating with a 3.0/3.1 peer:

```python
from adcp.signing import SigningConfig, VerifyOptions

legacy_buyer = SigningConfig(
    private_key=key,
    key_id="buyer-key",
    signing_profile_version="3.1",
)

strict_3_2_verifier = VerifyOptions(
    ...,
    signing_profile_version="3.2",
)
```

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
