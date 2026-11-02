# Subscribing to reports (buyer)

`client.reporting` registers AdCP 3.3 reporting subscriptions (experimental) safely. Every write is patch-shaped over the replace-shaped wire: other subscriptions are kept byte for byte, versions are chosen for you, and nothing is deactivated by omission.

## Subscribe

```python
result = await client.reporting.subscribe(
    {
        "subscription_id": "analytics-daily",
        "scope": {"all_authorized_accounts": True},
        "operator_domains": ["wppmedia.fr"],
        "selector": {"feed_purpose": "analytics", "period_duration": "P1D",
                     "required_finality": "official", "dimensions": ["package"]},
        "reconciliation_mode": "delivery_only",
        "destination": {"destination_id": "reporting-archive",
                        "path_template": "analytics/{account_id}/{period_start}"},
    },
    register_destination=archive_destination,
)
result.action, result.subscription_version
```

- **Versioning.** The version is bumped only when the selector, the scope kind, the destination or its `destination_overrides`, `reconciliation_mode` or `operator_domains` change. Adding or removing `account_ids`, and changing `exclude_account_ids`, apply in place (3.3.0-beta.3): the seller records each edit per account as an `excluded_by_buyer` interval, so periods that started before a removal stay owed and an added account is owed only from when it joined.
- **Pre-dispatch checks.** These fail with `dispatched=False`:
  - the scope must be explicit;
  - the destination must exist;
  - a multi-account path needs `{account_id}`;
  - the id must not clash with a `delivery_config_id`;
  - the operator scope must be safe;
  - the selector must match an offering.
- **Conflicts.** A conflicting write is re-read and retried.

## Unsubscribe

`await client.reporting.unsubscribe("analytics-daily")` is the only way to deactivate. Subscribing again reactivates the subscription at the same version.

## Why replace needs a flag

`replace_subscriptions(list, deactivate_omitted=True)` is the expert complete-set write. Sending `[]` needs `deactivate_all=True`. A raw `client.sync_principal(...)` carrying `reporting_subscriptions` or `reporting_destinations` is checked before dispatch. It is refused with `SUBSCRIPTION_WOULD_DEACTIVATE` or `DESTINATION_WOULD_REVOKE` unless you pass `allow_deactivation=True` or `allow_revocation=True`.

## Operator domains for agencies

An `all_authorized_accounts` subscription follows every grant, including accounts another operator grants later. When the principal spans operators, `subscribe` refuses an all-accounts scope without `operator_domains` (`SUBSCRIPTION_OPERATOR_SCOPE_REQUIRED`). Pass `acknowledge_cross_operator=True` to proceed anyway.

## Destinations

`register_destination=` upserts the named destination in the same write. Omitting a destination from a raw `sync_principal` revokes it for every account.

## Reconcile

```python
report = await client.reporting.reconcile(since="2026-11-01")
print(report.definitive, report.summary)
for line in report.periods:
    if line.responsible_party == "seller":
        print(line.account_id, line.period["start"], line.status, line.reason, line.message)
```

`reconcile()` reads everything from the wire on each run. Never compute periods yourself.
- **Roster:** `list_accounts` ∩ your subscriptions' scopes, plus each subscription's `accounts_unauthorized[]`. There is no account list to keep.
- **Grant history:** `authorization.authorized_at` and `regranted_at` from `list_accounts`, and `unauthorized_at` from `accounts_unauthorized[]`. You never record grants.
- **Facts:** buys from `get_media_buys`, offerings from capabilities, and `subscription_coverage[]` and the obligations from a complete `get_reporting_status` periods walk.

Each owed period that has ended becomes a `PeriodLine`, with wire-style keys and `to_dict()`:

| `status` | Meaning | `responsible_party` |
|---|---|---|
| `delivered` | a current revision meets the selector's `required_finality` | `none`; `buyer` when a billing receipt is owed (`receipt.status: owed_by_buyer`); `seller` once you rejected the revision (`receipt.status: rejected_by_buyer`: the seller corrects and re-delivers) |
| `pending` | not yet due: within `grace` of the period end, or of `expected_at` | `none` |
| `late` | an obligation exists, but its revision is more than `grace` past `expected_at` | `seller` |
| `missing_obligation` | no obligation `grace` after the period ended | `seller` |
| `withheld` | owed, and your authority is currently lost | `none` |
| `not_owed` | only with `include_not_owed=True`: every other period of each buy's flight, with its reason | `none` |

- `reason` is the spec reason code and `message` its sentence, the same as `explain`.
- `grace` defaults to one hour, never zero, so a fresh period is not flagged as missing.
- `report.definitive` is true only when the roster, every account's obligations walk and every owed set completed. Otherwise `report.issues` say what to retry; a partial roster is never used.
- An account whose authority you lost is reported from the last facts read for it, with its unpaid periods `withheld`.

From a shell: `python -m adcp reporting reconcile --agent <alias> [--since 2026-11-01] [--json]`.

## Explain

```python
traces = await client.reporting.explain(account_id="acc_a", subscription_id="analytics-daily",
                                        media_buy_id="mb_1", period="2026-11-03")
```

`explain()` returns the same `CoverageTrace` the seller's `explain_coverage` builds, with `side="buyer"`. Compare the two with `diff_coverage_traces` or `python -m adcp reporting diff buyer.json seller.json`. See [Coverage, explained](reporting-subscriptions-coverage.md#explaining-a-period). The CLI form is `python -m adcp reporting explain --agent <alias> --account <id> --period <date>`.

`ReportingSubscriptionTracker` is now an internal detail of these two calls. It stays importable for code that already uses it.

## Events

```python
# A long-running worker: each batch is acked once the loop body ran for all of it.
async for event in client.reporting.events(poll_every=timedelta(minutes=5)):
    match event.type:
        case "revision.available": await ingest(event.reporting_revision_id)
        case "period.late": await alert_seller(event)
        case "account.unauthorized": await pause_dashboards(event.account_id)

# Serverless: one batch per invocation.
batch = await client.reporting.poll()
await handle(batch.events)
await batch.ack()

# Webhook endpoint: a doorbell, never the event content.
outcome = await receiver.receive(...)               # verifies the signature
await client.reporting.handle_webhook(VerifiedWebhook.from_outcome(outcome))
```

- **Derived, not pushed.** Each poll runs `reconcile` and reads the subscription readback and coverage, then compares the result with the snapshot saved at the last `ack`. A crash before `ack` redelivers the same events with the same ids, so deliver at least once and deduplicate on `event.id`.
- **Deterministic ids.** `event.id` is `sha256(agent, type, natural key, version)`.
- **Durable state.** Pass `ReportingClient(client, event_store=...)` for a store that survives restarts. It implements `load(agent_id)` and a compare-and-set `save(...)`. The default `InMemoryReportingEventStore` is process-local. Two workers that ack the same snapshot get `ReportingEventConflictError` on the second ack.
- **Cold start.** `poll(baseline=True)` on a consumer with no snapshot records the present without emitting it.
- **Webhooks.** `handle_webhook` takes a `VerifiedWebhook` (or a verified, non-rejected `WebhookOutcome`) and refuses anything else. With an `events()` loop running it wakes the loop; otherwise it polls and returns the batch for you to handle and `ack`.
- **Partial reads.** An account that could not be read keeps its previous state, so it never produces a spurious "closed" or "gone" event.

| Event | When | Key fields |
|---|---|---|
| `revision.available` | a revision appears for an owed period | `reporting_revision_id`, `reporting_obligation_id`, `period`, `finality` |
| `revision.adjusted` | a new revision replaces one you saw | `superseded_reporting_revision_id` |
| `period.late`, `period.missing_obligation`, `period.withheld` | a period reaches that status | the period line |
| `coverage.exception_opened`, `coverage.exception_closed` | an `excluded[]` entry opens or closes | `reason`, `from`, `until`, `media_buy_id` |
| `account.covered` | an account enters a subscription | `subscription_id`, `account_id` |
| `account.unauthorized`, `account.regranted` | authority is lost, or returns | `unauthorized_at`, `regranted_at`, `chain` |
| `subscription.state_changed` | `active`, `inactive` or `action_required` | `state`, `previous_state`, `pending_action` |
| `timezone.cutover` | a `timezone_cutovers[]` entry appears | `effective_at`, `timezone`, `previous_timezone` |

## Receipts for billing feeds

A receipt states that you verified and committed the data, so the SDK never sends one on read. The policy is off by default.

```python
async def commit(revision: VerifiedRevision):
    job = await warehouse.load(revision.resource)    # your ingest
    return CommitResult(commit_ref=job.id)          # or "defer", or Reject(["ROW_COUNT_MISMATCH"])

report = await client.reporting.reconcile(
    receipts=ReceiptPolicy(mode="verified", commit=commit, resource_reader=reader))
report.receipts.submitted, report.receipts.deferred, report.receipts.rejected
```

With `mode="verified"`, the SDK handles each `consumer_receipt` obligation whose current revision is materialized and not yet receipted:
1. It verifies the materialization itself: the manifest through `resource_reader` (the built-in inspector), or your `inspect` adapter. This gives the observed row count, control totals and digest.
2. If verification fails, `commit` is not called:
   - A materialization the built-in inspector refuses (a digest, row-count or object mismatch), or cannot read, goes to `report.receipts.failed` with the code, and nothing is receipted for it.
   - An `inspect` adapter that returns a mismatched observation gets a `rejected` receipt with the SDK's codes.
3. Otherwise `commit` runs, and the `accepted` receipt carries your `consumer_commit_ref`. `commit` runs at least once per revision. A `commit` that raises is listed in `report.receipts.failed` as `COMMIT_FAILED`, and a receipt write that fails after it is retried, so the next reconcile calls `commit` again. Make it idempotent on `revision.resource`, for example by keying the load on the materialization id.

Other rules:
- **Mode off.** Billing periods read `receipt: {"status": "owed_by_buyer"}` and `responsible_party: "buyer"`, and the client logs a warning once.
- **Rejected.** A period whose current revision you rejected reads `receipt: {"status": "rejected_by_buyer"}` and `responsible_party: "seller"`: the rejection is final for that materialization, and the seller corrects and re-delivers.
- **Adjustments.** Each adjustment without an adjustment receipt is offered to `adjustments(adjustment)`, also on a revision already receipted, and deferred by default. Billing never auto-accepts. Accept or reject an adjustment with its raw evidence through `client.reporting.submit_receipts(account_id, adjustment_receipts=[...])`, the typed manual path over `sync_reporting_receipts`.
- **Once per materialization.** Receipt ids are stable per obligation, revision and materialization. A materialization already receipted, accepted or rejected, is not receipted again.
- **Ownership.** Receipts are submitted under the principal that owns the subscription: the client you call `reconcile` on.
