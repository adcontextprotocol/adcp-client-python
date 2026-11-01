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
