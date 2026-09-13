# Media-buy lifecycle coordinator

`MediaBuyLifecycleCoordinator` gives buyer applications one catalog-to-purchase
workflow across AdCP 3.0, 3.1, and 3.2. It negotiates the seller's served
contract, checks the advertised route, and chooses either:

- 3.2 compact: `list_products` then `buy_products`;
- 3.0/3.1 established: `get_products` then a continuation-fenced
  `create_media_buy`.

This first slice deliberately covers direct catalog purchase only. Proposal
discovery, proposal mutation and acceptance, media-buy controls, and readback
coordination remain on the native client methods. Buyer-side parity for those
operations is tracked in [Python issue #1154][buyer-parity].

The coordinator does not change the native `ADCPClient` methods or their
request signatures.

## Configure and negotiate

Compact purchases need only the client. Established purchases also need the
existing continuation coordinator, authenticated scope, and stable seller
session binding:

```python
from adcp.compat import (
    LegacyPurchaseCoordinator,
    MediaBuyLifecycleCoordinator,
    SqliteCompatibilityContinuationStore,
)

legacy = LegacyPurchaseCoordinator(
    store=SqliteCompatibilityContinuationStore("state/purchases.sqlite3"),
    executor=execute_create_media_buy_on_the_bound_client,
    reconciler=reconcile_create_by_application_transaction_identity,
    token_derivation_key=continuation_secret_from_secret_manager,
)

lifecycle = await MediaBuyLifecycleCoordinator.negotiate(
    client,
    legacy_purchase_coordinator=legacy,
    # Both values come from authenticated application state, not request data.
    principal_id=authenticated_principal_id,
    target_binding=stable_authenticated_seller_session_id,
    allowed_losses=[
        "feed_version_not_atomic",
        "pricing_version_not_atomic",
        # Required when capabilities do not positively declare mutation replay.
        "mutation_idempotency_not_guaranteed",
    ],
)
```

The coordinator prefers the compact pair on a served 3.2 contract. It requires
both `list_products` and `buy_products`; a partial pair fails before listing.
For 3.1 it requires `get_products`, `create_media_buy`, and advertised
`wholesale` buying mode. AdCP 3.0 predates that capability field, so wholesale
support is implicit for an otherwise complete 3.0 route.

Version negotiation is fail-closed. A served version must be compatible with
the client pin, use the same major, appear in `supported_versions` when that
list is present, and have an exact bundled validation schema. An older
capability document without served or supported release metadata uses the
protocol's 3.0 compatibility default.

## List and buy

Use the 3.2 listing vocabulary at the coordinator boundary:

```python
from adcp.compat import CoordinatorBuyProductsInput
from adcp.types import ListProductsRequest

listing = await lifecycle.list_products(
    ListProductsRequest.model_validate(
        {
            "idempotency_key": "catalog-read-2026-09-11-0001",
            "account": {"account_id": "account-acme"},
            "criteria": {"offer_filters": {"countries": ["US", "CA"]}},
        }
    ),
    # Stable retry identity for continuation issuance on the established route.
    issuance_idempotency_key="catalog-read-2026-09-11-0001",
)

purchase_request: CoordinatorBuyProductsInput = {
    "idempotency_key": "6eecf265-2445-4495-8273-3aeaa854d42c",
    "account": {"account_id": "account-acme"},
    "brand": {"domain": "acme.example"},
    "purchases": [
        {
            "product_id": listing.products[0]["product_id"],
            "pricing_option_id": listing.products[0]["pricing_options"][0][
                "pricing_option_id"
            ],
            "budget": 1_000,
        }
    ],
    "start_time": "2026-10-01T00:00:00Z",
    "end_time": "2026-11-01T00:00:00Z",
}
purchase = await lifecycle.buy_products(listing, purchase_request)

print(listing.compatibility.tools_used)
print(purchase.compatibility.losses)
print(purchase.success, purchase.data)
```

`CoordinatorBuyProductsInput` is the typed form of the second argument. It
matches the JSON-compatible buyer-owned `BuyProductsRequest` fields but
intentionally omits coordinator-owned protocol, feed, and pricing versions.
Pydantic-model inputs remain supported and are serialized before preflight.
Arbitrary mapping implementations still work at runtime for backward
compatibility, but annotate ordinary dictionaries with
`CoordinatorBuyProductsInput` to get required-field and nested-shape checking.

On compact routes, the coordinator removes caller-supplied feed or pricing
versions and injects only the seller evidence retained from the listing. It
does not invent a missing pricing version. Conditional listing reuse through
`if_feed_version` or `if_pricing_version` is rejected because an unchanged
response cannot safely provide fresh purchase evidence.

On established routes, country filters project to `filters.countries`. The SDK
validates requests and responses against the exact negotiated 3.0 or 3.1
schema, then issues an opaque `legacy_create` continuation. The continuation
binds the observed response, account, selectable products and pricing, source
schema, authenticated principal, seller session, expiry, and exact loss set.
Changed-account and out-of-catalog purchases fail before mutation.

## Recovery and process restarts

Exact retries with the same idempotency key replay the durable established
purchase result. If a mutation becomes ambiguous, fence the former executor,
look up the operation through `LegacyPurchaseCoordinator`, and call:

```python
purchase = await lifecycle.recover_legacy_purchase(operation)
```

Persist the opaque listing continuation when listing and purchase can span a
process restart. Recreate the coordinator with the same durable store,
authenticated principal, seller-session binding, served contract, and loss
policy, then redeem it without reconstructing a coordinator-owned catalog:

```python
purchase = await lifecycle.continue_legacy_purchase(
    persisted_continuation_token,
    purchase_request,
    accepted_losses=persisted_exact_loss_set,
)
```

The durable record, not caller-supplied catalog data, rechecks the account,
selected products, pricing terms, source schema, expiry, losses, and retry
identity.

## Guarantee boundaries

Established direct purchase cannot reproduce 3.2's atomic feed and pricing
fences. It always reports `feed_version_not_atomic` and
`pricing_version_not_atomic`. If capabilities do not positively advertise a
valid one-hour-to-seven-day idempotency replay window, it also reports
`mutation_idempotency_not_guaranteed`. Every reported loss must be allowed
before the seller mutation is sent.

The application still owns authenticated scope, seller credentials, durable
continuation storage, token-key management, and authoritative reconciliation
after an ambiguous purchase.

This is buyer-side compatibility only. Implementing compact seller handlers
does not expose established handlers to old buyers; an opt-in reverse facade is
tracked in [Python issue #1147][seller-facade]. Release-pinned cross-language
response certification is tracked in [AdCP issue #7439][conformance].

See [Durable legacy purchase continuations](legacy-purchase-continuations.md)
for store, retry, pending-task, and reconciliation details.

[buyer-parity]: https://github.com/adcontextprotocol/adcp-client-python/issues/1154
[seller-facade]: https://github.com/adcontextprotocol/adcp-client-python/issues/1147
[conformance]: https://github.com/adcontextprotocol/adcp/issues/7439
