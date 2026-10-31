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

`client.accounts.change(...)` with `allow_separate_account`, and `client.accounts.use(...)`, arrive in a later beta. A seller opening a separate account never re-points the registry on its own.

## Money changes and revision

A change to `billing`, `billing_entity` or `payment_terms` needs the explicit account `revision` it was approved against. Send it with `request_account` until `change()` lands.

## Cold start

`list_accounts` with `include_requests` reads back your open requests and their pending actions. `refresh()` uses `account_request_ids`.
