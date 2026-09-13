# Media-buy lifecycle coordinator

`MediaBuyLifecycleCoordinator` gives buyer applications one catalog-to-purchase
workflow across AdCP 3.0, 3.1, and 3.2. It negotiates a served contract from
`get_adcp_capabilities`, confirms the advertised tools, and chooses either:

- 3.2 compact: `list_products` then `buy_products`;
- 3.0/3.1 established: `get_products` then a continuation-fenced
  `create_media_buy`.

The native `ADCPClient` task methods and their signatures do not change. The
coordinator covers ordinary wholesale listing, direct purchase, proposal
discovery, acceptance, refinement, decline, operational media-buy control, and
media-buy and delivery readback.

## Configure and negotiate

Established purchases use the existing durable continuation coordinator.
Established proposal mutations also need an
`EstablishedProposalEvidenceStore` that implements the atomic
`EstablishedProposalAcceptanceStore` and `EstablishedProposalMutationStore`
protocols. Submitted acceptance recovery additionally uses the optional
`EstablishedProposalAcceptanceRecoveryStore` protocol. Both stores must be shared and durable in production, and their
executor/client must be bound to the same authenticated seller session:

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
        # Required only when capabilities do not positively declare mutation
        # replay protection.
        "mutation_idempotency_not_guaranteed",
        # Established proposal acceptance cannot reproduce the 3.2 seller's
        # immutable, atomically digest-checked committed hold.
        "proposal_snapshot_not_immutable",
        "proposal_hold_not_verifiable",
        "proposal_terms_digest_not_enforced",
        "proposal_terms_digest_unavailable",
    ],
    proposal_evidence_store=durable_atomic_proposal_store,
)
```

The coordinator prefers compact tools on a 3.2 served contract. A 3.2 seller
that advertises neither a requested compact tool nor its established counterpart
is rejected before dispatch. An older capability document with no
release-precision version metadata is treated as 3.0; this compatibility default
is not evidence that an explicitly versioned response served some newer
contract. Stable releases downshift to the highest advertised minor no newer
than the client pin. Prerelease pins must match a real bundled prerelease
contract.

## List and buy

Use the 3.2 request vocabulary at the coordinator boundary. Country filters are
translated to the established `filters.countries` field when necessary:

```python
from adcp.types import ListProductsRequest

listing = await lifecycle.list_products(
    ListProductsRequest.model_validate(
        {
            "idempotency_key": "catalog-read-2026-09-11-0001",
            "account": {"account_id": "account-acme"},
            "criteria": {"offer_filters": {"countries": ["US", "CA"]}},
        }
    ),
    # Exact retries reuse this identity. Required on the established route;
    # ignored by the compact route.
    issuance_idempotency_key="catalog-read-2026-09-11-0001",
)

purchase = await lifecycle.buy_products(
    listing,
    {
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
    },
)

print(listing.compatibility.tools_used)
print(purchase.compatibility.losses)
print(purchase.success, purchase.data)
```

On compact routes, the coordinator copies the seller's real `feed_version` and
`pricing_version` from the listing into `buy_products`. On established routes,
those fields stay `None` when the seller did not return them. The SDK issues a
local `legacy_create` continuation instead; it never fabricates a feed token or
claims that a successful `create_media_buy` made the listing atomic.

The listing binds the complete observed response, account, selectable product
and pricing IDs, served schema, principal, seller session, expiry, and loss set.
Changed-account or out-of-catalog purchases fail before mutation. Exact retries
with the same idempotency key replay the durable result. To recover an ambiguous
attempt after fencing its former executor, look up the operation on the
`LegacyPurchaseCoordinator` and pass it to
`lifecycle.recover_legacy_purchase(operation)`.

Persist the opaque continuation token when listing and purchase may span a
process restart. Reconstruct the coordinator with the same durable continuation
store, authenticated principal, seller-session binding, served contract, and
loss policy, then redeem without reconstructing the coordinator-owned catalog
object:

```python
purchase = await lifecycle.continue_legacy_purchase(
    persisted_continuation_token,
    purchase_request,
    accepted_losses=persisted_exact_loss_set,
)
```

The durable record—not caller-supplied catalog data—rechecks account, selected
products, pricing terms, source schema, expiry, losses, and idempotency before
claiming the seller mutation.

## Request proposals

`request_proposals` uses the compact task on a compatible 3.2 seller and
projects to established `get_products(buying_mode="brief")` on 3.0/3.1. The
result keeps its task status and compatibility report together:

```python
from adcp.types import RequestProposalsRequest

result = await lifecycle.request_proposals(
    RequestProposalsRequest.model_validate(
        {
            "idempotency_key": "proposal-request-2026-09-11",
            "account": {"account_id": "account-acme"},
            "brand": {"domain": "acme.example"},
            "brief": "A premium display campaign for Acme.",
            "criteria": {"offer_filters": {"countries": ["US", "CA"]}},
        }
    )
)

if result.status.value in {"submitted", "working"}:
    result = await lifecycle.wait_for_proposals(result)

if result.success:
    print(result.data.outcome, result.data.proposals)
```

Completed outcomes are `proposed`, `products_available`, `rejected`, or
`legacy_unavailable`. The last outcome deliberately avoids converting an empty
or ambiguous established response into a seller rejection. An established
products-only response receives the same bound `legacy_create` continuation as
ordinary listing.

Established proposal payloads are retained as immutable observations in an
`EstablishedProposalEvidenceStore`, scoped by authenticated principal, seller
session, account, and exact source schema. The default in-memory store is for
single-process development only.

Executable snapshots are capped at 256 KiB and authenticated principal scopes
at 256 UTF-8 bytes, matching the TypeScript coordinator. Credential-bearing,
signed-URL, invalid-digest, or oversized seller proposals remain visible in the
read result but are not retained for later mutation. A same-ID unsafe
re-observation removes older available evidence first, so stale safe terms
cannot remain executable after the seller replaces them with an unsafe shape.

## Accept a proposal

On 3.2, `accept_proposal` validates and sends the native
`AcceptProposalRequest`. On 3.0/3.1 it consumes bound evidence from proposal
discovery and projects the mutation to `create_media_buy(proposal_id=...)`.
The three legacy fields are explicit because older create contracts require
them even though they are not part of native 3.2 acceptance:

```python
accepted = await lifecycle.accept_proposal(
    {
        "idempotency_key": "proposal-acceptance-2026-09-11-0001",
        "account": {"account_id": "account-acme"},
        "proposal_id": result.data.proposals[0]["proposal_id"],
        "proposal_terms_digest": result.data.proposals[0].get("terms_digest"),
        "total_budget": {"amount": 10_000, "currency": "USD"},
    },
    established_fallback={
        "brand": {"domain": "acme.example"},
        "start_time": "2026-10-01T00:00:00Z",
        "end_time": "2026-11-01T00:00:00Z",
    },
)
```

The fallback accepts only `brand`, `start_time`, and `end_time`. Verified
digest-bound proposal terms take precedence, and every missing legacy routing
field must be supplied by the fallback; the compact route rejects it. Before an
established mutation, the coordinator
checks account/principal/seller/version binding, local expiry and proposal
state, any retained digest, exact-version request validity, the exact accepted
loss set, and an atomic proposal/idempotency reservation. Exact retries replay
the buyer-side stored result without dispatching a second create. Concurrent,
changed, and ambiguous attempts are fenced.

An established seller may return a submitted `create_media_buy` task. Pass the
result to `wait_for_acceptance()` while the process is live. A recovery-capable
store binds that seller task to the acceptance reservation, so a new
coordinator with the same authenticated scope can finish it without a second
create:

```python
recovered = await lifecycle.recover_acceptance(
    seller_task_id,
    account={"account_id": "account-acme"},
)
```

Exact retries return the same submitted task. Missing task identity,
cross-account lookup, malformed terminal data, and non-authoritative task
states remain fenced.

If transport fails before any seller task identity is observed, an identical
request may be dispatched again only while the seller's advertised
`replay_ttl_seconds` window remains open. The atomic store computes that
deadline from its own clock. Changed requests, missing replay guarantees,
task-bound ambiguity, and expired windows stay fenced.

The established loss report always includes
`proposal_terms_digest_not_enforced`. Evidence without a verified digest also
requires `proposal_terms_digest_unavailable` and
`proposal_snapshot_not_immutable`; evidence without an expiry additionally
requires `proposal_hold_not_verifiable`. A seller without positive replay
capability also requires `mutation_idempotency_not_guaranteed`.

## Refine and decline proposals

`refine_proposals` and `decline_proposals` dispatch their native compact tasks
on 3.2. On 3.0/3.1 they use `get_products(buying_mode="refine")` with an atomic
multi-proposal reservation. Refinement supports legacy `ask`, product
`include`/`omit`, and proposal `finalize`. Structured constraints,
alternatives, criteria, amendment kinds, and unknown fields fail before
dispatch because older sellers cannot guarantee them.

Decline maps each proposal to legacy `action="omit"`. This cannot prove a
terminal decline or forward the compact reason/detail, so established calls
require both `proposal_decline_not_terminal` and
`proposal_decline_reason_not_forwarded`. Results report `unconfirmed` unless
the established seller explicitly returns an unable refinement row.

Both operations bind request order, proposal IDs, authenticated scope, exact
source schema, retained snapshots, and idempotency identity. Exact completed
retries replay locally; conflicts and non-retryable ambiguous outcomes remain fenced. For
submitted tasks, pass the result to
`wait_for_proposal_mutation()` so the durable reservation remains active until
the authoritative terminal result is validated and successor evidence is
stored.

A transport ambiguity before seller task binding permits the same narrow exact
retry as acceptance when a valid replay window was advertised. The original
store-authored deadline is retained across attempts and is never extended by a
retry.

Durable mutation stores also bind the seller task ID and retain the reduced
wire request. After a process restart, reconstruct the coordinator with the
same authenticated scope and reconcile without redispatching:

```python
recovered = await lifecycle.recover_proposal_mutation(
    seller_task_id,
    account={"account_id": "account-acme"},
)
```

Recovery requires an exact principal, seller-session, account, and source
version match. A missing, cross-scope, or durably ambiguous task fails closed.

The in-memory reference store defaults to 256 combined records, 4 MiB of
retained state, and a seven-day completion-proof window. Configure
`max_records` and `max_bytes` for development workloads. Its optional
`prune_completion_tombstones()` sweeper uses the store's own clock and will not
prune before seven days; successful source generations are consumed so pruning
cannot re-authorize them. Durable stores should implement
`EstablishedProposalTombstoneStore` or provide an equivalent database-owned
sweeper.

## Control a media buy

`control_media_buy` uses the native compact task on 3.2. On 3.0/3.1 it maps the
safe subset to revision-checked `update_media_buy`: media-buy pause/resume or
cancellation, reporting and notification configuration, and representable
package pause, cancellation, budget, impression, pacing, targeting, and keyword
changes. The response retains `media_buy_id`, the new `revision`, and the exact
route report.

```python
from adcp.types import ControlMediaBuyRequest

controlled = await lifecycle.control_media_buy(
    ControlMediaBuyRequest.model_validate(
        {
            "idempotency_key": "media-buy-control-2026-09-11-0001",
            "account": {"account_id": "account-acme"},
            "media_buy_id": "mb-1",
            "revision": 7,
            "paused": True,
        }
    )
)

if controlled.status.value in {"submitted", "working"}:
    controlled = await lifecycle.wait_for_control_media_buy(controlled)
```

Established contracts cannot preserve compact aggregate budget, allocation,
pacing, bidding, governance, extension, or name controls. They also cannot
preserve package bidding, daily caps, minimum-spend targets, canonical
optimization goals, promoted catalog IDs, or `budget=null`. These fields and
invalid cancellation combinations fail before transport; the coordinator never
silently drops them.

## Read media buys and delivery

`get_media_buys` and `get_media_buy_delivery` remain the same named seller
tools across 3.0, 3.1, and 3.2. The coordinator verifies that each shared tool
was advertised, pins the negotiated request schema, and returns its route report
with the result. Use `wait_for_media_buy_readback()` for a submitted read.

Fields introduced after the served contract fail before dispatch. This includes
3.1 webhook-activity selectors on 3.0, compact indicator filters on 3.0/3.1,
new delivery revision, pagination, and metric selectors, and newer reporting
dimensions or sort controls. Defaults from the current request model are not
leaked onto older wires.

## Guarantee boundaries

Established direct purchase always loses atomic feed and pricing fencing. When
the seller does not positively advertise idempotency replay with a valid
`replay_ttl_seconds` value in the protocol's one-hour-to-seven-day range, it
also loses a verified mutation-replay guarantee. An invalid advertised range
is rejected during coordinator construction; an omitted window is treated as
unsafe rather than assuming a default. Every required loss must appear in
`allowed_losses`; otherwise no mutation is sent.

The application still owns:

- authenticated `principal_id` and stable seller-session binding;
- credentials and authorization for the actual seller mutation;
- durable production continuation and atomic proposal stores, plus a stable
  token-derivation key;
- authoritative reconciliation after an ambiguous purchase or proposal-mutation outcome;
- authoritative reconciliation after an ambiguous proposal acceptance;
- retention and encryption policy for non-secret commercial payloads;
- application recovery policy for an ambiguously observed media-buy control.

See [Durable legacy purchase continuations](legacy-purchase-continuations.md)
for store, replay, pending-task, and reconciliation details.
