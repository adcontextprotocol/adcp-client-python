# Durable legacy purchase continuations

AdCP 3.2 can project an established 2.5, 3.0, or 3.1 products-only brief
response as `outcome: products_available`. A `legacy_create` continuation is a
lossy bridge back to that seller's `create_media_buy`; it is not a proposal,
seller feed fence, or reusable credential.

The Python coordinator validates the SDK-local beta.4
`CompatibilityPurchaseCoordinatorInput`, binds it to the complete discovery
transaction, atomically claims the token, and stores the result for exact
replay. The coordinator input must never be sent to an AdCP seller.

## Configure a durable coordinator

```python
from adcp.compat import (
    LegacyPurchaseCoordinator,
    ReconciliationResult,
    SqliteCompatibilityContinuationStore,
)

store = SqliteCompatibilityContinuationStore("state/adcp-continuations.sqlite3")

async def execute_legacy_purchase(execution):
    # Route using execution.target_binding to the same authenticated seller
    # connection/account session used for discovery. Authorize create_media_buy
    # here; authority from request_proposals does not transfer.
    return await legacy_client.call_tool(
        "create_media_buy",
        execution.legacy_create_request,
        adcp_version=execution.source_adcp_version,
    )

async def reconcile_legacy_purchase(execution, operation):
    result = await lookup_by_original_transaction_identity(execution)
    if result.found:
        return ReconciliationResult.applied(result.payload)
    if result.authoritatively_absent:
        return ReconciliationResult.not_applied()
    return ReconciliationResult.ambiguous()

coordinator = LegacyPurchaseCoordinator(
    store=store,
    executor=execute_legacy_purchase,
    reconciler=reconcile_legacy_purchase,
)
```

At projection time, persist every binding and return the generated opaque
token in `purchase_continuation`:

```python
token = await coordinator.issue_legacy_create_continuation(
    principal_id=authenticated_principal,
    account=account,
    source_adcp_version="3.1.15",  # exact negotiated patch release
    expires_at=expires_at,
    observed_request=complete_get_products_request,
    observed_response=complete_get_products_response,
    product_ids=[product["product_id"] for product in products],
    buyer_visible_products=projected_products_shown_to_buyer,
    losses=["feed_version_not_atomic", "pricing_version_not_atomic"],
    target_binding=stable_seller_session_id,
    # Set true only when the actual 3.0/3.1 peer guarantees mutation replay.
    mutation_idempotency_guaranteed=True,
)
```

Redeem only after the buyer explicitly accepts the exact loss set:

```python
result = await coordinator.continue_legacy_purchase(
    compatibility_input,
    principal_id=authenticated_principal,
    target_binding=stable_seller_session_id,
)
```

`InMemoryCompatibilityContinuationStore` is intentionally rejected by the
coordinator's production default. It can be enabled with
`allow_non_durable_store=True` for tests only. SQLite is safe across local
processes sharing one ordinary local filesystem. Distributed deployments
should implement `CompatibilityContinuationStore` on their transactional
database and preserve the same atomic state transitions.

The SQLite ledger is created with mode `0600`; its direct parent must be owned
by the current user and cannot be group/world writable. Existing database and
sidecar files with group or other access are rejected before every pathname
open. This is access control, not encryption; use an encrypted volume or an
application-owned encrypted store when payloads require encryption at rest.
`purge_resolved_before(cutoff)` removes only old succeeded/failed and
never-claimed continuations. It deliberately retains claimed, `in_flight`,
`pending`, and `ambiguous` operations regardless of age.

Ledgers created by the initial pre-release coordinator are migrated in place.
The old ledger did not retain the buyer-visible pricing subset, so unresolved
old rows are explicitly non-executable instead of exposing seller-only options.
The first exact retry may atomically adopt the sanitized execution snapshot
while incrementing the operation revision. A pre-release 3.0/3.1 row also did
not bind a verified replay guarantee, which the SDK cannot infer after the
fact. Already-terminal results still replay, and authoritative reconciliation
may still recover an applied result. Never start a fresh purchase until an
unresolved old operation has been reconciled.

For a migrated unresolved operation, call `continue_legacy_purchase` once with
the exact original input before using the recovery API. That retry adopts the
missing execution snapshot but still fails closed without executing; then look
up the new operation revision and perform fenced recovery.

## What the application owns

The SDK cannot infer security or commercial identity. The application must:

- derive `principal_id` from authenticated state, never a request-body claim,
  and make it globally unambiguous by binding issuer, tenant, and subject;
- preserve the original account and seller target/session, especially for 2.5,
  whose wire request has no account field;
- encrypt sensitive stored discovery payloads at rest, set a state-aware
  retention policy, and restrict ledger access. Never purge unresolved
  `in_flight` or `ambiguous` operations automatically;
- authorize the actual `create_media_buy` call and select its credentials;
- implement authoritative reconciliation using a seller transaction identity;
- keep the exact negotiated patch version and full observed product/pricing
  payload until expiry and reconciliation retention have elapsed.

The opaque token is generated with at least 128 bits of randomness and only its
SHA-256 hash is stored. A principal mismatch is reported as not found to avoid
cross-tenant token enumeration. Natural account comparison excludes mutable
display metadata such as `operator_unit.name` but includes the account's actual
natural key.

## Claim and crash behavior

The durable operation ledger moves through:

```text
claimed -> in_flight -> succeeded | failed | pending
                    \-> ambiguous -> claimed (only after authoritative absence)
```

The token is consumed when the first seller mutation is reserved. Exact
`(principal, idempotency_key, full logical payload)` retries replay the stored
result. Reusing the key with changed input conflicts, and another operation
cannot claim the token.

An exception, timeout, or cancellation observed by the coordinator after
`in_flight` is marked `ambiguous`. A hard process loss leaves the durable row
`in_flight`; recovery remains closed until the application fences the old
executor. Look up a revision-bearing snapshot and use the fenced recovery API:

```python
operation = await coordinator.get_legacy_purchase_operation(
    operation_id,
    principal_id=authenticated_principal,
)

# First revoke/fence the old worker's ability to reach the seller. The CAS
# revision fences stale ledger completion, but cannot revoke network access.
result = await coordinator.recover_legacy_purchase(
    operation,
    principal_id=authenticated_principal,
    target_binding=stable_seller_session_id,
)
```

Recovery atomically fences `in_flight` to `ambiguous` with a revision CAS. A
stale snapshot cannot recover or complete the operation. The SDK never reopens
the token by elapsed time and never blindly resends the legacy request. A
reconciler may then prove that the mutation was applied and supply its exact
source-version response, or prove it was not applied and allow the same durable
operation to resume. An inconclusive or absent reconciler raises
`CompatibilityContinuationError` with code `ambiguous_legacy_mutation` and an
`operation_id` for operators.

## Validation before mutation

Before the atomic claim, the coordinator rejects expiry, principal/account or
target rebinding, product substitution, package-set drift, duplicate selected
IDs, stale/partial/excess loss consent, and source-schema violations. The
nested request is validated against the exact source-version
`create-media-buy-request` schema and must use explicit packages. Pricing
selection and all projected pricing terms are checked against the token-bound
buyer-visible product projection and complete observed option, not merely the
seller's larger set of option IDs. AdCP 2.5 continuations, and
3.0/3.1 peers without a verified mutation replay guarantee, must declare
`mutation_idempotency_not_guaranteed`.

Executor and reconciliation results are validated against the exact legacy
`create_media_buy` response schema before persistence. Synchronous success,
terminal errors, and submitted task envelopes are stored and replayed in their
distinct durable states; arbitrary mappings are never promoted to success.
SDK `TaskResult` wrappers are unwrapped or projected only when they can produce
a valid source-version envelope. Synchronous executor/reconciler callbacks run
in a worker thread so they do not block the event loop.

`listed_purchase` is different: it is executable only with a real
account-scoped seller feed and unchanged seller-issued `feed_version` and
optional `pricing_version` fences. This legacy-create coordinator does not
fabricate those fences or redeem listed purchases.

## Reverse compatibility facade

A 3.2 seller may keep genuine legacy `get_products` and `create_media_buy`
facades for established buyers. If those facades are backed by compact tools,
the application must provide one private atomic transaction that preserves the
legacy product, price, and inline-creative contract. A `buy_products` followed
by creative synchronization is a non-atomic saga and must be rejected before
the facade is advertised. The SDK's pure legacy wire adapters do not expose a
reverse facade automatically.
