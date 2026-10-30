# Coverage, explained

Both sides compute which periods a subscription owes with one function, `expected_subscription_periods`, over the same facts. Never compute periods yourself.

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

## Reason codes

The frozen vocabulary (`COVERAGE_REASON_CODES`, from the spec's `reason-codes.json`):

| Reason | Rule |
|---|---|
| `not_in_scope`, `excluded:excluded_by_buyer`, `buy_not_visible` (seller only) | scope |
| `not_authorized` | authorization |
| `before_coverage_start` | version |
| `subscription_inactive` | activity |
| `excluded:offering_unsupported` | offering |
| `outside_flight`, `after_stop` | flight |
| `before_confirmation` | confirmation |
| `excluded:capacity`, `excluded:overridden`, `excluded:action_required` | exception |
| `gap:unauthorized` | gap |
| `cutover_stretch`, `owed_under_other_version` | cutover |
| `period_not_ended`, `owed`, `owed:regrant_backfill`, `owed:withheld` | outcome |

## Worked traces

The `CoverageTrace` format and its template sentences arrive with `explain` in a later beta. The cases below are among the spec's canonical vectors, which this SDK runs in `tests/test_reporting_subscription_parity.py`.

### Confirmation day

A period that ends at or before the buy's `confirmed_at` is not owed (`before_confirmation`). The period in which the buy is confirmed ends after it, so it is owed in full.

### Cancellation mid-period

A buy stops at the earliest of `end_time`, `canceled_at` and `completed_at`. A period that starts at or after the stop is not owed (`after_stop`). The period containing the stop is owed in full.

### Re-grant gap

A re-grant inside `status_retention_days` continues the chain, so `authorized_at` is kept. Unless the seller advertises `backfill_on_regrant`, the periods from the last boundary at or before `unauthorized_at` to the first boundary at or after the re-grant are a gap (`gap:unauthorized`).

### Timezone cutover

A timezone change takes effect at its `effective_at`, and each zone owes only its own span. A previous-zone period starting at or after `effective_at`, or a new-zone period starting before it, is not owed (`cutover_stretch`).

### DST

`P1D` periods follow the local calendar, so DST days are 23 or 25 hours long. `PT24H` periods are always 24 hours.
