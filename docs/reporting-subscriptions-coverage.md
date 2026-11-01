# Coverage, explained

Both sides compute which periods a subscription owes with one function, `owed_subscription_periods`, over the same facts. Never compute periods yourself.

## The owed rules

A period of a resolved offering is owed for an account when all of these hold:
- the account is in the subscription's scope;
- the caller held a grant listing `get_reporting_status` for it;
- the period starts at or after the version's coverage start;
- the subscription was active;
- a confirmed buy's flight overlaps the period;
- no exception or gap removes it;
- the period has ended.

The rules are applied in a fixed order: `scope`, `authorization`, `version`, `activity`, `offering`, `flight`, `confirmation`, `exception`, `gap`, `cutover`, `timezone`.

## Explaining a period

`explain` answers "why is this period owed, or not?" with a `CoverageTrace`: the verdict, the reason code, a one-sentence explanation, every rule applied in order with the facts it read, and an `inputs_digest`.

```python
# Seller: from your service and storage.
traces = await reporting.explain_coverage(
    PrincipalRef("t1", "agency-x"), "acc_renault_fr",
    subscription_id="analytics-daily", media_buy_id="mb_123", period="2026-11-03")
for trace in traces:
    print(trace.verdict, trace.reason, trace.explanation)
```

```
python -m adcp reporting explain --app myapp.reporting:service \
    --principal agency-x --account acc_renault_fr --period 2026-11-03 [--json]
```

- `period` is a `YYYY-MM-DD` date read in each offering's zone (every period that starts on that local date), an instant (the period containing it), or `{"start", "end"}`, which must be a period of the offering's schedule. The trace echoes the period with its `timezone`.
- Omit `subscription_id`, `media_buy_id` or `offering_id` to explain every match. The result is a list, sorted by period start, buy and offering.
- The verdict is the owed rule's own: a period is `owed` exactly when `owed_subscription_periods` owes it. The reason is the first rule, in the fixed order, that rejects the period.
- `--app` names your `ReliableReportingService` as `module:attribute`, the way ASGI servers name an app.

### The trace

```json
{
  "trace_version": 1,
  "side": "seller",
  "subject": {
    "principal_id": "agency-x", "account_id": "acc_a",
    "subscription_id": "analytics-daily", "subscription_version": 1,
    "media_buy_id": "mb_123", "offering_id": "daily_pkg",
    "period": {"start": "2026-11-03T05:00:00Z", "end": "2026-11-04T05:00:00Z", "timezone": "America/New_York"}
  },
  "verdict": "not_owed",
  "reason": "before_confirmation",
  "explanation": "Not owed: the period ends 2026-11-04T05:00Z, before mb_123 was confirmed at 2026-11-04T10:00Z.",
  "steps": [
    {"rule": "scope", "result": "pass", "facts": {"scope": "all_authorized_accounts", "operator_domains": null, "operator": null}},
    {"rule": "authorization", "result": "pass", "facts": {"authorized_at": null, "chain": "not_advertised"}},
    {"rule": "version", "result": "pass", "facts": {"subscription_version": 1, "accepted_at": "2026-10-20T12:00:00Z", "coverage_start": "2026-10-21T04:00:00Z"}},
    {"rule": "activity", "result": "pass", "facts": {"inactive_intervals": []}},
    {"rule": "offering", "result": "pass", "facts": {"match": "exact", "candidates": ["daily_pkg"], "resolved": ["daily_pkg"], "selected": "daily_pkg"}},
    {"rule": "flight", "result": "pass", "facts": {"start_time": "2026-11-03T05:00:00Z", "stop_time": "2026-11-30T05:00:00Z", "as_of": "2026-11-04T05:00:00Z"}},
    {"rule": "confirmation", "result": "fail", "facts": {"confirmed_at": "2026-11-04T10:00:00Z"}}
  ],
  "inputs_digest": "sha256:…",
  "seller_only": {"buy_visible": true}
}
```

- Steps stop at the first failing rule. An owed period passes every rule; `timezone` is `not_applicable` unless the offering is `account_timezone` aligned.
- `seller_only` holds whether the caller can see the buy and, for an owed period, its `reporting_obligation_id`. It never leaves the seller: send `trace.for_buyer()`.
- `inputs_digest` hashes only wire-visible facts local to the period: the version in effect, the authorization as `list_accounts` and `accounts_unauthorized` show it, the zone in effect, the buy as `get_media_buys` returns it, the offering, the `excluded[]` entries covering the period start, and whether an authorization gap covers it. `now` is not part of it.

### Comparing a buyer trace with a seller trace

```
python -m adcp reporting diff buyer.json seller.json
```

`diff_coverage_traces(left, right)` names the first rule whose result or facts differ:
- **Equal digests and different verdicts:** both sides read the same facts, so one SDK applied a rule differently. Report it.
- **Different digests:** one side read stale or different state. Re-read and compare again.

## Reason codes

The frozen vocabulary comes from the spec's `reason-codes.json`, packaged with the SDK and read by `coverage_reasons()`. `COVERAGE_REASON_CODES` mirrors it. Each code renders one sentence from `EXPLAIN_TEMPLATES`, so both sides print identical text for identical facts.

| Reason | Verdict | Rule | Sentence |
|---|---|---|---|
| `not_in_scope` | not owed | scope | Not owed: version {v} of {subscription} does not select account {account}. |
| `excluded:excluded_by_buyer` | not owed | scope | Not owed: the buyer excluded account {account} from {subscription} from {from}. |
| `buy_not_visible` (seller only) | not owed | scope | Not owed: the caller cannot see {buy} through get_media_buys. |
| `not_authorized` | not owed | authorization | Not owed: the caller held no authority for {account} over the period {start} to {end}. |
| `before_coverage_start` | not owed | version | Not owed: the period starts {start}, before coverage starts at {coverage_start}. |
| `subscription_inactive` | not owed | activity | Not owed: {subscription} was inactive from {deactivated_at} when the period started at {start}. |
| `excluded:offering_unsupported` | not owed | offering | Not owed: {offering} does not report {buy} under the selector of version {v}. |
| `outside_flight` | not owed | flight | Not owed: the period ends {end}, at or before {buy} starts at {start_time}. |
| `after_stop` | not owed | flight | Not owed: the period starts {start}, at or after {buy} stopped at {stop_time}. |
| `before_confirmation` | not owed | confirmation | Not owed: the period ends {end}, before {buy} was confirmed at {confirmed_at}. |
| `excluded:capacity` | not owed | exception | Not owed: account {account} is beyond the seller's subscription capacity from {from}. |
| `excluded:overridden` | not owed | exception | Not owed: another of the caller's delivery configurations takes {account} for this feed from {from}. |
| `excluded:action_required` | not owed | exception | Not owed: the seller serves account {account} only after a pending action completes; excluded from {from}. |
| `gap:unauthorized` | not owed | gap | Not owed: the period falls in the authorization gap from {from} to {until}. |
| `cutover_stretch` | not owed | cutover or timezone | Not owed: the period {start} to {end} in {timezone} lies outside the span its side owes around the handoff at {handoff}. |
| `owed_under_other_version` | not owed | cutover | Not owed under version {v}: the side before the handoff at {handoff} owes this time. |
| `period_not_ended` | not owed | outcome | Not owed yet: the period {start} to {end} has not ended. |
| `owed` | owed | outcome | Owed: {buy} under {offering} for the period {start} to {end}, due at {expected_at}. |
| `owed:regrant_backfill` | owed | outcome | Owed as a re-grant backfill: the period {start} to {end} falls in the interruption from {unauthorized_at} to {regranted_at}, which the seller backfills; due at {expected_at}. |
| `owed:withheld` | owed | outcome | Owed but withheld: the caller lost authority for {account} at {unauthorized_at}, so the period is due again only after a re-grant. |

## Worked traces

Each case below is one of the spec's canonical vectors, which this SDK runs in `tests/test_reporting_subscription_parity.py`. The sentences are what `explain` prints for it.

### Confirmation day

A period that ends at or before the buy's `confirmed_at` is not owed (`before_confirmation`). The period in which the buy is confirmed ends after it, so it is owed in full.

> Not owed: the period ends 2026-11-04T05:00Z, before mb_123 was confirmed at 2026-11-04T10:00Z.
>
> Owed: mb_123 under daily_pkg for the period 2026-11-04T05:00Z to 2026-11-05T05:00Z, due at 2026-11-05T11:00Z.

### Cancellation mid-period

A buy stops at the earliest of `end_time`, the cancellation's delivery stop and `completed_at`. The delivery stop is `cancellation_effective_at` when the seller delivers through a notice period, else `cancellation.canceled_at` (3.3.0-beta.3). A period that starts at or after the stop is not owed (`after_stop`). The period containing the stop is owed in full. Flight values are read as of each period's end from the buy's `flight_history`, so a retroactive edit never makes an elapsed period owed.

> Owed: mb_1 under off_a_pkg for the period 2026-11-05T00:00Z to 2026-11-06T00:00Z, due at 2026-11-06T06:00Z.
>
> Not owed: the period starts 2026-11-06T00:00Z, at or after mb_1 stopped at 2026-11-05T06:00Z.

### Re-grant gap

A re-grant inside `status_retention_days` continues the chain, so `authorized_at` is kept and `authorization.regranted_at` carries the re-grant. Unless the seller advertises `backfill_on_regrant`, the periods from the last boundary at or before `unauthorized_at` to the first boundary at or after `regranted_at` are a gap (`gap:unauthorized`). Withheld periods are due at `regranted_at` plus `delivery_sla`.

> Not owed: the period falls in the authorization gap from 2026-11-05T06:00Z to 2026-11-06T18:00Z.

With `backfill_on_regrant`, the same period is `owed:regrant_backfill`, due at the re-grant plus `delivery_sla`.

### Timezone cutover

A timezone change takes effect at its `effective_at`, and each zone owes only its own span. A previous-zone period starting at or after `effective_at`, or a new-zone period starting before it, is not owed (`cutover_stretch`, rule `timezone`).

> Not owed: the period 2026-11-05T05:00Z to 2026-11-06T05:00Z in America/New_York lies outside the span its side owes around the handoff at 2026-11-05T05:00Z.
>
> Owed: mb_1 under off_e_pkg_acct_tz for the period 2026-11-05T08:00Z to 2026-11-06T08:00Z, due at 2026-11-06T14:00Z.

### Billing stub

When a cutover hands a billing feed to a schedule with no boundary at E, the stretch from E to the first incoming boundary is owed as one stub period. Its obligation carries `stub: true` (`OwedSubscriptionPeriod.stub`, `ReportingObligationRecord.stub`). Pacing and analytics feeds leave that stretch unowed.

### Sticky resolution

A package resolves once, when its (buy, version) pair first does: at the later of the version's `accepted_at` and the buy's `confirmed_at`, among the offerings available then. An offering published later never re-resolves it. A resolved offering's `withdrawn_at` is the one change: the package re-resolves among the offerings available then, as a cutover.

### Reactivation

Coverage resumes at the first boundary at or after the later of the reactivation instant and the end of the last period owed before the deactivation, so a schedule change made while inactive never owes a period twice.

### DST

> Owed: mb_1 under off_e_pkg_acct_tz for the period 2026-11-01T04:00Z to 2026-11-02T05:00Z, due at 2026-11-02T11:00Z.

That New York day is 25 hours long. `P1D` periods follow the local calendar, so DST days are 23 or 25 hours long. Under a local alignment each local date's first instant is always a boundary: local midnight, or the first instant after a DST gap at midnight (Havana, Santiago, Cairo). Days therefore nest in every sub-daily schedule, and `PT24H` equals `P1D`. Under `utc`, `PT24H` periods are always 24 hours. A sub-daily `billing_cycle` is refused (`invalid_period_duration`).
