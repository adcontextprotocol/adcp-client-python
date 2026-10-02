# Account lifecycle

Start with the seller's capabilities and discover public inventory before
choosing billing and payment terms:

```python
from adcp import ADCPClient
from adcp.types import GetProductsRequest

client = ADCPClient(agent_config)
capabilities = await client.fetch_capabilities()
key = {
    "brand": {"domain": "acme.example"},
    "operator": "agency.example",
    "currency": "USD",
    "sandbox": False,
}

# With the default account_policy="auto", an unprovisioned natural key is
# omitted on optional-account discovery. Its brand is sent as context.
public = await client.get_products(GetProductsRequest(buying_mode="wholesale", account=key))

# Provision only when you have selected the commercial terms.
account = await client.accounts.ensure(
    key, billing="agent", payment_terms="net_30", billing_entity=billing_entity,
)
print(account.status, account.account_id)

# The provisioned natural key is retained on subsequent requests.
priced = await client.get_products(
    GetProductsRequest(buying_mode="wholesale", account=key), account_policy="strict",
)
```

`ensure` calls `sync_accounts` once for each complete key at each seller and
returns an `AccountRecord`. Successful pending-approval or payment-required
accounts are provisioned; they still need buyer action before committing spend.
Only active and payment-required accounts can browse account-priced products;
other statuses use public discovery in `auto` mode when allowed, or raise in
`strict` mode.
Concurrent calls to `ensure` share the provisioning result. Use `sync_accounts`
explicitly to change settings later. Direct `sync_accounts` and `list_accounts`
calls also update the registry. Failed rows and dry runs never establish
provisioning. Refresh status through `list_accounts` after completing setup.

Pass `account_storage` to `ADCPClient` to persist records. The `AccountStorage`
adapter implements async `load(seller, key)`, `save(seller, key, record)`, and
`delete(seller, key)`. Keep the adapter private to one authenticated buyer, or
namespace your adapter by buyer identity. Seller URI and the complete natural
key scope each record. The key includes brand domain, brand ID, canonical country
set, operator, operator unit ID, currency, buyer-selected timezone, and sandbox.
Display metadata and brand overrides never change identity.

`account_policy="strict"` rejects an unknown natural key before transport.
`auto` removes it only when the task permits public discovery. Required accounts,
including `required_for_products` declared in cached capabilities, fail before
transport. Fetch capabilities before account-less discovery to enable this
preflight; without cached capabilities the seller enforces `ACCOUNT_REQUIRED`,
which the SDK classifies as `AccountSetupRequiredError`. Calls carrying natural
references fetch capabilities automatically. The input request remains unchanged, and account version tokens are
removed when discovery becomes public. Policies can be set on the client or
overridden on individual account-carrying calls and `execute_task`.

For sellers declaring `require_operator_auth: true`, discover an explicit
`account_id` with `list_accounts`, or use the seller's out-of-band onboarding.
Natural-key provisioning and strict natural references are rejected. Optional
discovery in `auto` mode still omits the account and carries its brand, allowing
public browsing. Explicit IDs pass through, including IDs
supplied out of band. Buyer-declared sellers may return an ID from provisioning;
the SDK continues sending the natural key because accepting that handle is
optional for those sellers.

Buyer-side `ensure` requires `sync_accounts`. Sellers that only provision
lazily through resource or spend commitments need explicit `sync_accounts`
onboarding support to use this registry workflow.

`AccountNotFoundError`, `AccountSetupRequiredError`, and
`AccountPaymentRequiredError` carry `fault="buyer_setup"` and retain structured
error details. Exclude these errors from seller-health failures. An
`ACCOUNT_NOT_FOUND` response invalidates the corresponding registry record.

## Seller account stores

`ExplicitAccounts.resolve(None)` returns `None`. On optional discovery, the
framework supplies a public `ctx.account` with an empty ID; it is not a persisted
account or an idempotency namespace. Product discovery with
`required_for_products=True` returns `ACCOUNT_REQUIRED`. A supplied reference
that cannot resolve always returns `ACCOUNT_NOT_FOUND`.

`NaturalKeyAccounts` bundles natural-key projection and resolution:

```python
from adcp.decisioning import NaturalKeyAccounts

store = NaturalKeyAccounts(
    loader=load_provisioned_account,
    upsert_request=sync_account_request,
)
```

The loader receives `(canonical_key_dict, auth_info)` and returns a framework
`Account`, or `None` on a miss. It must check caller access and search only
accounts that provisioning has persisted. The optional `upsert_request` callback
receives the full `SyncAccountsRequest` and `ctx=ResolveContext`; it handles
billing, dry runs, persistence, and returns `SyncAccountsResultRow` entries.
Discovery never calls the provisioning callback.

Custom stores may implement `AccountStoreResolveForTask.resolve_for_task(ref,
ctx)`. The framework prefers that hook and provides `ctx.tool_name`,
`ctx.auth_info`, `ctx.agent`, and `ctx.is_provisioning`. Lazy account creation
MUST be gated on `ctx.is_provisioning`: only spend commitments and account-owned
resource creation (`create_media_buy`, `buy_products`, `accept_proposal`,
`activate_signal`, and `sync_*`) provision. Discovery and negotiation never do.
Existing stores implementing only `resolve(ref, auth_info=None)` continue working.

## Shared public feeds and account overlays

```python
from adcp import FeedMirror
from adcp.types import GetProductsRequest

public_feed = FeedMirror(client)
await public_feed.bootstrap("product")
reference = GetProductsRequest(buying_mode="wholesale", account=key).account
assert reference is not None
account_feed = public_feed.for_account(reference, account_id=account.account_id)
await account_feed.bootstrap("product")
product = account_feed.get_product("product-id")
```

Public responses share one cache. Each account response is a complete snapshot
of inventory available to that account, with its own prices and membership.
A response explicitly marked `cache_scope="public"` updates the shared public
layer and drops the account view; account prices remain private to each overlay.
Missing or invalid scope,
scope changes during pagination, or failed refreshes preserve the last good
maps and version tokens. Refreshing both feeds commits only after both fetches
succeed. Scope is required on wholesale responses and webhooks.

Authenticate webhook deliveries before applying them. The mirror also checks
that envelope and payload scopes agree and that account events name the bound
account. Natural-key mirrors need a trusted seller `account_id` from provisioning
or discovery to accept account webhooks; pass it when constructing the mirror.

## Migrating existing integrations

This release changes the default account and wholesale-feed behavior:

- Account failures from client task calls now raise `AccountNotFoundError`,
  `AccountSetupRequiredError`, or `AccountPaymentRequiredError`. Catch these
  exceptions where code previously inspected a failed `TaskResult`, and treat
  them as buyer setup issues.
- Unprovisioned natural references use public discovery in `auto` mode where
  permitted. Call `client.accounts.ensure` before account-scoped operations,
  or choose `strict` to reject unknown references during discovery.
- Direct callers of `ExplicitAccounts.resolve(None)` must handle `None`.
  Server discovery handlers can receive a public `ctx.account` with an empty
  ID; product discovery that requires an account returns `ACCOUNT_REQUIRED`.
- Wholesale sellers and test doubles must provide an explicit `cache_scope`
  on responses and webhooks. Missing scope raises `FeedMirrorError`;
  `FeedState.cache_scope` starts as `None` until scope is validated.
- Account feed snapshots define their own complete inventory. Public-only
  products are not inherited by an account snapshot, and natural-key mirrors
  need a trusted seller ID binding before accepting account webhooks.
