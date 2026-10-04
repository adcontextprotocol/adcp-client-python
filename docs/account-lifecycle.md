# Account lifecycle

## Defaults in 8.1 and what changes in the next major

8.1 ships the provisioning registry, account policies, typed account errors, and
scoped feed checks. The 8.0 defaults stay in place, so upgrading from 8.0 changes
nothing until you opt in. When a call relies on a default that will change, the
SDK emits a `DeprecationWarning`. Python hides these warnings outside `__main__`
by default. Run with `-W default::DeprecationWarning` or under pytest to see them.

| Setting | 8.1 default (8.0 behavior) | Opt in now | Next major default |
|---|---|---|---|
| `ADCPClient(account_policy=...)` | `"off"`: account references are sent unchanged and nothing is checked before transport. Warns once per client the first time an unset policy sends a natural-key reference. | `"auto"` or `"strict"` | `"auto"`. `"off"` remains available. |
| `ADCPClient(raise_account_errors=...)` | `False`: seller account errors come back as a failed `TaskResult`. Warns when this happens and the option is unset. | `True` | `True` |
| `FeedMirror(strict_scope=...)` | `False`: see the compatibility list below. Each lenient path warns. | `True` | `True` |
| `ExplicitAccounts(allow_public_discovery=...)` | `True`: an absent reference resolves to `None` for public discovery. This is a bug fix; see [Seller account stores](#seller-account-stores). | n/a | `True` |

To silence the warnings and keep the 8.0 behavior permanently, set
`account_policy="off"` and `raise_account_errors=False` explicitly. To adopt the
next-major behavior now, set `account_policy="auto"`, `raise_account_errors=True`,
and `FeedMirror(..., strict_scope=True)`.

`AccountNotFoundError`, `AccountSetupRequiredError`, and
`AccountPaymentRequiredError` subclass `ADCPTaskError`, so existing
`except ADCPTaskError` handlers catch them.

`ADCPMultiAgentClient` builds its per-agent clients with these defaults. To opt
in, set `multi.agents[agent_id].account_policy` and `.raise_account_errors`.

## Discovery and provisioning

Start with the seller's capabilities and discover public inventory before
choosing billing and payment terms:

```python
from adcp import ADCPClient
from adcp.types import GetProductsRequest

client = ADCPClient(agent_config, account_policy="auto", raise_account_errors=True)
capabilities = await client.fetch_capabilities()
key = {
    "brand": {"domain": "acme.example"},
    "operator": "agency.example",
    "currency": "USD",
    "sandbox": False,
}

# With account_policy="auto", an unprovisioned natural key is omitted on
# optional-account discovery. Its brand is sent as context.
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

`account_policy="off"` (the 8.1 default) skips every preflight described in this
section. Only the registry bookkeeping still runs, and it never fetches
capabilities or fails a call. `account_policy="strict"` rejects an unknown
natural key before transport. `auto` removes it only when the task permits public
discovery and rejects it elsewhere. Preflight rejections under `auto` and `strict`
always raise typed account errors, whatever `raise_account_errors` is set to. Required accounts,
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
error details. Exclude these errors from seller-health failures. With
`raise_account_errors=True`, the SDK raises them for seller responses carrying
`ACCOUNT_NOT_FOUND`, `ACCOUNT_REQUIRED`, `ACCOUNT_SETUP_REQUIRED`, or
`ACCOUNT_PAYMENT_REQUIRED`. Otherwise it returns the failed `TaskResult` as 8.0
did. Either way, an `ACCOUNT_NOT_FOUND` response invalidates the corresponding
registry record.

## Seller account stores

`ExplicitAccounts.resolve(None)` returns `None`. In 8.0 it raised
`ACCOUNT_NOT_FOUND`, which made spec-optional public discovery impossible against
explicit-account sellers. On optional discovery tasks (`get_products`,
`list_products`, `get_signals`, and proposal negotiation), the framework now
supplies a public `ctx.account` with an empty ID. It is not a persisted account
or an idempotency namespace. Discovery handlers that assumed a loaded account
must handle `ctx.account.id == ""`. Alternatively, pass
`ExplicitAccounts(loader, allow_public_discovery=False)` to keep rejecting absent
references as 8.0 did.

When the store cannot establish an account (it returns `None`), product discovery
with `required_for_products=True` returns `ACCOUNT_REQUIRED`. Stores that derive
the account from auth for an absent reference, such as `FromAuthAccounts` or
`SingletonAccounts`, keep serving discovery as in 8.0. A supplied reference that
cannot resolve always returns `ACCOUNT_NOT_FOUND`.

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

Shared public mirrors and their overlays are always strict about scope, whatever
`strict_scope` is set to. Without that, a response with a misclassified scope
could publish one account's prices to every overlay. `for_account` refuses a
mirror that already holds account-scoped state.

Public responses share one cache. Each account response is a complete snapshot
of inventory available to that account, with its own prices and membership.
A response explicitly marked `cache_scope="public"` updates the shared public
layer and drops the account view; account prices remain private to each overlay.
Missing or invalid scope,
scope changes during pagination, or failed refreshes preserve the last good
maps and version tokens. Refreshing both feeds commits only after both fetches
succeed. Strict mirrors require scope on wholesale responses and webhooks.

Standalone mirrors (not shared through `for_account`) default to
`strict_scope=False` in 8.1. That accepts three cases 8.0 accepted, each with a
`DeprecationWarning`:

- A wholesale response without `cache_scope` is treated as `public`.
- An unscoped mirror (no `account`) accepts account-scoped responses and webhooks.
- A natural-key mirror without an `account_id` binding accepts account webhooks.

Some checks apply in every mode because they only reject malformed or
misaddressed seller data:

- Unknown scope literals.
- A scope that changes during pagination.
- Envelope and payload scopes that disagree.
- `unchanged` responses that do not match the cached scope.
- Account webhooks naming a different `account_id` than the mirror's binding.

Authenticate webhook deliveries before applying them. The mirror also checks
that envelope and payload scopes agree and that account events name the bound
account. Natural-key mirrors need a trusted seller `account_id` from provisioning
or discovery to accept account webhooks; pass it when constructing the mirror.

## Preparing for the next major release

8.1 keeps the 8.0 defaults. Before the next major flips them, do the following:

- **Account errors.** Set `raise_account_errors=True` and catch
  `AccountNotFoundError`, `AccountSetupRequiredError`, or
  `AccountPaymentRequiredError` wherever code inspects a failed `TaskResult` for
  account codes. Treat them as buyer setup issues. `except ADCPTaskError` already
  catches them.
- **Account policy.** Set `account_policy="auto"`. Unprovisioned natural
  references then use public discovery where permitted and fail before transport
  elsewhere. Call `client.accounts.ensure` before account-scoped operations, or
  choose `strict` to reject unknown references during discovery as well. If you
  want 8.0 pass-through permanently, set `account_policy="off"`.
- **Explicit-account sellers.** Direct callers of `ExplicitAccounts.resolve(None)`
  must handle `None` (already the 8.1 behavior). Discovery handlers can receive
  a public `ctx.account` with an empty ID.
- **Wholesale feeds.** Set `FeedMirror(..., strict_scope=True)`. Make sure
  sellers and test doubles send an explicit `cache_scope`, construct account
  mirrors with `account=...`, and pass the seller-assigned `account_id` to
  natural-key mirrors that receive account webhooks.
- **Account overlays.** Account feed snapshots define their own complete
  inventory. Public-only products are not inherited by an account snapshot.
