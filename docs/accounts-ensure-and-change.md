# Accounts: ensure and change

## Outcomes

`await client.accounts.ensure(key, ...)` returns an `AccountOutcome`. Pending, ambiguous and rejected are values, not errors:

| Outcome | Meaning | `next_step(outcome)` |
|---|---|---|
| `AccountReady(account_id, status, via)` | use `account_id` | `none` |
| `AccountPending(action, request_id)` | waiting on the seller, or on a buyer-side human | `wait_for_seller` or `send_human_to_url` |
| `AccountAmbiguous(candidates, request_id)` | several of your accounts match; `await outcome.choose(account_id)` | `pick_candidate` |
| `AccountRejected(errors)` | declined, or a buyer action expired | `contact_seller` |

`ensure` calls `request_account` when the seller advertises `account.requests`, and `sync_accounts` otherwise. It is idempotent: a repeat call never opens a duplicate request. Only a failure to process the call raises `AccountRequestError`. See [Account lifecycle](account-lifecycle.md) for storage and policies.

## Pending actions for humans and agents

`PendingAction(waiting_on, kind, message, url, expires_at)`:
- `url` is for a person to open. It is not a bearer link; never fetch it.
- `message` is untrusted. Render it with `format_pending_action(action)`.
- Agents switch on `next_step(outcome)`, never on `kind`, which is extensible.

Call `await client.accounts.refresh()` from a cron job or an `account_request.changed` webhook to learn what moved. `await client.accounts.pending()` lists what is still waiting.

## Separate accounts

```python
outcome = await client.accounts.change(
    "acc_nova", {"brand": {"domain": "nova-athletics.example", "countries": ["BE", "DE", "NL"]}},
    allow_separate_account=True)

match outcome:
    case AccountUpdated(account_id=aid, revision=rev): ...       # same account, new revision
    case AccountSeparate(account_id=new, previous_account_id=old) as separate:
        # Nothing moved. separate.not_carried_over and separate.subscriptions_not_covering
        # say what the new account lacks. New buys go to it only after:
        await client.accounts.use(separate.key, new)
    case AccountMatched(account_id=other): ...                   # an existing account matched
    case AccountPending(action=action): ...
    case AccountRejected() as rejected if rejected.requires_new_account: ...
```

- **Default.** `allow_separate_account` defaults to `False`, as in the spec. A seller that would accept the change only as a separate account rejects it, and `outcome.requires_new_account` is `True` (`ACCOUNT_CHANGE_REQUIRES_NEW_ACCOUNT`).
- **Pending changes.** A pending change is not kept in the registry. Keep its `request_id` and read it back with `await client.accounts.change_status(account_id, request_id)`; a new change request supersedes an open one.
- **No silent re-point.** `change()` never re-points the registry; `use()` records the account with its own status from `list_accounts`, so the spend gate still holds a `payment_required` or pending account. After `AccountSeparate` or `AccountMatched`, the natural key keeps naming the old account until you call `client.accounts.use(key, account_id)`. Which account carries new buys stays your explicit choice.
- **Not carried over.** `AccountSeparate.not_carried_over` lists the account-level configurations the new account lacks. `subscriptions_not_covering` lists the `account_ids`-scoped reporting subscriptions that will not see it.
- **Next step.** `next_step(outcome)` is `none` for `AccountUpdated`, `AccountSeparate` and `AccountMatched`.

## Money changes and revision

A change to `billing`, `billing_entity` or `payment_terms` needs the explicit account `revision` it was approved against: `await client.accounts.change(account_id, {"payment_terms": "net_60"}, revision=3)`.
- **Without `revision`** the call is refused before anything is sent: `AccountRequestError` with `dispatched=False` and code `ACCOUNT_REVISION_REQUIRED`. The SDK never reads the revision for you, because a human approved the change against a specific revision, and reading it would defeat the lost-update protection.
- **`effective_at`.** `AccountUpdated.effective_at` says when the new money terms take effect. Delivery before it is invoiced under the previous terms.
- **Other changes** (`brand` countries, `operator_unit`, `currency`, `timezone`) read the current `revision` from `list_accounts` when you omit it. On `CONFLICT` they retry once, but only when the fields being changed did not change concurrently.
- **Sellers without `account.requests`.** `change()` is refused (`ACCOUNT_REQUESTS_UNSUPPORTED`); change accounts through `sync_accounts` instead.

## Cold start

`list_accounts` with `include_requests` reads back your open requests and their pending actions. `refresh()` uses `account_request_ids`.

## Serving `request_account` (sellers)

On the decisioning platform, an `AccountStore` serves AdCP 3.3 `account.requests` with two optional hooks:

```python
from adcp.decisioning import AccountRequestsPage, DecisioningCapabilities
from adcp.decisioning.capabilities import Account as AccountCaps

class MyAccounts:
    def resolve(self, ref, auth_info=None): ...
    def list(self, filter=None, ctx=None): ...           # Account(..., revision=3) serves revision

    async def request_account(self, params, ctx=None):
        # Open, match or change; return the wire AccountRequest record.
        return await db.request(params, caller=ctx.agent)

    async def list_requests(self, params, ctx=None):
        # Only when the buyer sends include_requests, request_ids or requests_pagination.
        page = await db.requests_for(ctx.agent, ids=params.request_ids,
                                     cursor=params.requests_pagination and params.requests_pagination.cursor)
        return AccountRequestsPage(items=page.records, next_cursor=page.next)

capabilities = DecisioningCapabilities(
    specialisms=["sales-non-guaranteed"],
    account=AccountCaps(
        supported_billing=["operator"],
        # Optional, advisory: what change requests accept and which pending actions you issue.
        requests={"supported": True, "changeable_attributes": ["billing", "payment_terms", "timezone"],
                  "pending_action_kinds": ["review", "accept_terms"]},
    ),
)
```

- **Advertised for you.** With `request_account`, the handler serves the tool and advertises `account.requests` on `get_adcp_capabilities`: `supported: true`, `request_task: "request_account"`, `read_task: "list_accounts"`, plus your optional lists. It also adds `account.requests` to `experimental_features`. Callers on releases before 3.3 see neither, and an `experimental_features` claim without the hook is dropped. Buyers route `ensure` and `change` by this block.
- **`supported_billing` is yours to state.** The account block requires it, and the framework never invents one: a store with `request_account` and no `account.supported_billing` is refused at construction (`ConfigurationError`).
- **Return values.** `request_account` returns the request record, which is served as `{"status": "completed", "request": ...}`. `in_review`, `ambiguous` and `rejected` are normal outcomes. Raise `AdcpError` only for calls you cannot process. A change names `params.account_id`: check that the caller may change that account, and refuse a money change (`billing`, `billing_entity`, `payment_terms`) without `revision`.
- **Polling.** `list_requests` serves `account_requests[]` and `account_requests_pagination` on `list_accounts`. It runs only when the buyer asks, and only for 3.3 callers. Return only the caller's requests, newest `updated_at` first, at most 100 per page, and omit unknown or foreign `request_ids` silently. It needs `list`. With `request_account` but no `list_requests`, the handler warns at construction that buyers cannot poll a pending request.
- **`revision`.** `Account.revision` is served on `list_accounts` to 3.2+ callers. Buyers send it with a change.
