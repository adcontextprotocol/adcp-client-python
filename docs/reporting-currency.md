# Currency on reporting obligations

One `ReportingProducer` can serve USD and EUR accounts concurrently. Currency
is resolved once, from trusted seller state, when the SDK creates an obligation.
It is stored before source acquisition and reused for retries, process restarts,
snapshot restatements, official publications and subsequent adjustments.

Pass a synchronous or asynchronous `CurrencyResolver` to the producer:

```python
import asyncio

from adcp.reporting.ledger import (
    ProducerOfferings,
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingProducer,
    require_single_currency,
)

# Illustrative historical seller records, keyed by account and media buy.
# In production, read their accepted value as of candidate.scope_resolved_at.
accepted_currencies = {
    ("us-account", "us-buy"): "USD",
    ("eu-account", "eu-buy"): "EUR",
}

def resolve_currency(
    configuration: ReportingConfiguration,
    candidate: ReportingObligationRecord,
) -> str:
    return require_single_currency(
        accepted_currencies[(candidate.account_id, buy_id)]
        for buy_id in candidate.media_buy_ids
    )

producer = ReportingProducer(
    store=ledger,
    source=source,
    object_reader=staging,
    offerings=ProducerOfferings(snapshot_offering_id="ACCOUNT_CURRENCY_SNAPSHOT"),
    currency_resolver=resolve_currency,
)
# Accepted us-account/daily@1 and eu-account/daily@1 configurations each
# contain their own media buy. The #1169 account-qualified keys isolate them.
await asyncio.gather(producer.run_worker(), producer.run_worker())
```

`ledger`, `source` and `staging` are the seller's existing reporting components;
the offering must support the accepted report definition and source scope.
The resolver receives SDK-owned configuration/obligation records, including the
account-qualified generation, period-end timestamp and frozen media-buy/package
scope. The candidate's currency is initially `None`. An async resolver can load
the same historical account context that a future `ReliableReportingService`
uses. A resolver must establish **one currency for the entire scope** and raise
on unknown or mixed currencies. `require_single_currency` validates each code
and rejects empty/mixed input without aggregating it.

Never derive this value from a buyer's request `context`, a live mutable default,
or an adapter response. A resolver should perform bounded, read-only historical
lookups. It can run more than once if workers race before commit; both workers
use the store's immutable winning obligation. An existing obligation never
calls the resolver again. Resolver failure occurs before obligation creation
and leaves source work untouched; it must be surfaced by the worker supervisor.

The existing `ProducerOfferings(currency="EUR")` option remains a convenient
single-currency configuration, implemented by `FixedCurrencyResolver`. Its
default remains USD. A custom resolver takes precedence. This convenience is
appropriate only when every scope it serves has that currency. Codes must match
three uppercase ASCII letters exactly: `EUR` is accepted; `eur`, whitespace,
non-ASCII letters, and non-string values fail with `INVALID_CURRENCY`. Validation
checks ISO 4217 **shape**, not membership in a periodically changing registry.

## Pinned monetary semantics

`ReportingDefinitionBinding` can retain immutable monetary unit declarations
projected by trusted seller code from its verified, content-addressed definition:

```python
from dataclasses import replace

binding = replace(
    verified_binding,
    monetary_metric_units=(("spend", "EUR"),),
    monetary_control_total_units=(("spend", "EUR"),),
)
```

These are tuples, copied into immutable pairs on construction and persisted
with the obligation. They are not an alternate definition download or evidence
obtained from the source. Verify the definition bytes against the retained
digest before projecting them. Do not supply a unit that its pinned definition
does not establish. If the definition leaves currency account-scoped, the
trusted resolver establishes it; unitless values inherit the frozen currency.

Conflicting pinned currencies fail at freeze time. Both ledger stores check
flat monetary columns and same-name additive totals using exact decimal values.
The existing `spend` column is treated as monetary even without extra declarations;
declare other monetary columns and totals explicitly. This convenience check
is for flat additive reporting, matching `InlineReportingSource`; it is not a
general evaluator for arbitrary definition expressions or nested row schemas.
Non-additive/custom metrics require an appropriate seller/source validator.
Nonmonetary control-total units are preserved; three capital letters alone do
not make a unit a currency.

Source slice requests use only the stored currency. A manifest's currency and
explicit monetary total units must match; its definition binding must also
match any retained pin before staging is read or a revision is committed. The
source cannot override the obligation. `InlineFetchResult(currency="EUR", rows=...)`
supplies optional corroboration. Any explicit row `currency` must match too.
When a fetch returns `GetMediaBuyDeliveryResponse`, response, media-buy and
package currency labels are checked before flattening. Mixed/mismatched rows
are rejected before inline aggregation or staging; no conversion to USD occurs.
The adapter copies rows before validation so a shared cache cannot change the
checked money during staging.
Absent unit labels inherit the obligation/definition, preserving existing
unitless low-level row and total formats. Inline `spend` totals explicitly carry
the frozen currency in their source manifests.

`ReportingCurrencyError.code` distinguishes `CURRENCY_UNRESOLVED`,
`CURRENCY_MISMATCH`, `MIXED_CURRENCY_SCOPE`, `INVALID_CURRENCY`, and
`MONETARY_TOTAL_MISMATCH`. An inline adapter returns currency failures as terminal
`INTEGRITY_FAILED` with that reason in `safe_message`.

## Low-level use and retained history

Low-level writers can still construct records and call `commit_obligation`,
`commit_revision` and `commit_adjustment` directly. Set `currency="EUR"` on each
new obligation, using trusted historical evidence. `currency=None` remains
representable for loading old records, but new writes without currency fail.
Public producer acquisition/manifest commit calls reload the stored obligation;
passing a modified copy cannot substitute its currency. Exact legacy record
replays remain idempotent. Adjustments inherit units through their official
revision's obligation and cannot request a different currency.

No new currency field is invented on the AdCP status wire schema. Status and
exact content reads retain the original definition binding, rows and digests;
audits can inspect the obligation's currency through the store. The consumer's
existing `currency_mismatch` classification remains available for contradictory
observed metric/control-total units.

Upgrading from beta.15 or #1169 preserves unknown legacy currencies as `NULL`,
blocks new acquisition/publication/adjustment for them, and projects
`HISTORY_UNAVAILABLE` / `action_required` while keeping their history readable.
See [the migration policy and deployment instructions](reporting-ledger-migration.md).

The shared memory/Postgres scenarios in
[`test_reporting_currency.py`](../tests/conformance/reporting/test_reporting_currency.py)
publish USD and EUR through one producer, change its resolver/default between
attempts, and reconcile the frozen results with the buyer-side SDK. The
[strict adopter fixture](../tests/type_checks/reporting_currency.py) demonstrates
both callback forms and low-level writes without typing suppressions.
`adcp.reporting.fixtures.redacted_multi_currency_requests()` supplies USD/EUR
slice requests for adopter replay-conformance tests.
