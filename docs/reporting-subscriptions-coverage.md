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

A buy stops at the earliest of `end_time`, the cancellation's delivery stop and `completed_at`. The delivery stop is `cancellation_effective_at` when the seller delivers through a notice period, else `cancellation.canceled_at` (3.3.0-beta.3). A period that starts at or after the stop is not owed (`after_stop`). The period containing the stop is owed in full. Flight values are read as of each period's end from the buy's `flight_history`, so a retroactive edit never makes an elapsed period owed.

### Re-grant gap

A re-grant inside `status_retention_days` continues the chain, so `authorized_at` is kept and `authorization.regranted_at` carries the re-grant. Unless the seller advertises `backfill_on_regrant`, the periods from the last boundary at or before `unauthorized_at` to the first boundary at or after `regranted_at` are a gap (`gap:unauthorized`). Withheld periods are due at `regranted_at` plus `delivery_sla`.

### Timezone cutover

A timezone change takes effect at its `effective_at`, and each zone owes only its own span. A previous-zone period starting at or after `effective_at`, or a new-zone period starting before it, is not owed (`cutover_stretch`).

### Billing stub

When a cutover hands a billing feed to a schedule with no boundary at E, the stretch from E to the first incoming boundary is owed as one stub period. Its obligation carries `stub: true` (`OwedSubscriptionPeriod.stub`, `ReportingObligationRecord.stub`). Pacing and analytics feeds leave that stretch unowed.

### Sticky resolution

A package resolves once, when its (buy, version) pair first does: at the later of the version's `accepted_at` and the buy's `confirmed_at`, among the offerings available then. An offering published later never re-resolves it. A resolved offering's `withdrawn_at` is the one change: the package re-resolves among the offerings available then, as a cutover.

### Reactivation

Coverage resumes at the first boundary at or after the later of the reactivation instant and the end of the last period owed before the deactivation, so a schedule change made while inactive never owes a period twice.

### DST

`P1D` periods follow the local calendar, so DST days are 23 or 25 hours long. Under a local alignment each local date's first instant is always a boundary: local midnight, or the first instant after a DST gap at midnight (Havana, Santiago, Cairo). Days therefore nest in every sub-daily schedule, and `PT24H` equals `P1D`. Under `utc`, `PT24H` periods are always 24 hours. A sub-daily `billing_cycle` is refused (`invalid_period_duration`).
