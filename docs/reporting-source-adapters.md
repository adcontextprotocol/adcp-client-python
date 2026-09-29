# Per-metric evidence for inline reporting sources

`InlineReportingSource` wraps a synchronous or asynchronous delivery fetch in a
sealed, replayable reporting publication. Return `InlineFetchResult` when metric
availability differs within a constituent. Its `cell_availability` map is keyed
by **request constituent ID, then metric name**. Constituent IDs are not
necessarily media buy IDs; read them from `request.coverage.constituents`.

```python
from adcp.reporting.inline_source import InlineFetchResult, MetricEvidence

return InlineFetchResult(
    rows=[{"constituent_id": constituent_id, "impressions": 120, "clicks": 0}],
    cell_availability={
        constituent_id: {
            "impressions": MetricEvidence.present(data_through=watermark),
            "clicks": MetricEvidence.explicit_zero(),
            "viewability": MetricEvidence.delayed(
                "measurement_pending", data_through=viewability_watermark
            ),
            "completed_views": MetricEvidence.unavailable("not_video_inventory"),
        },
    },
)
```

This publishes one `partial` constituent with four independent metric cells.
It can complete a request whose `coverage.expected` is `partial`. A `full`
request receives retryable `PARTIAL_RESULT` while `viewability` is delayed.
When every applicable cell is available, the constituent is `present` and can
satisfy full coverage even though `completed_views` remains `unsupported`.

Offering metrics with `support: partial` are requestable alongside `exact`
metrics in both the service and source conformance. Every requested cell must
still have manifest evidence with the offering's semantic contract. The inline
adapter builds that evidence from the defaults and `cell_availability`
overrides; use an override for each inapplicable or unready cell. An offering's
`unavailable` metrics cannot pass source conformance.

Only a metric declared with `support: partial` and a stable offering reason may
emit `unsupported` cells. Declaring `exact` support promises applicability;
use `missing` or `delayed` when that measurement has not arrived. The inline
adapter rejects contradictory evidence before staging, and both producer
admission and source conformance enforce the same rule for custom executors.
For example, an exact-support spend metric awaiting billing must use
`MetricEvidence.delayed("billing_pending")` and retain partial coverage.

If a conditional metric is `unsupported` in every constituent, the batch can
still have full coverage when every constituent has other applicable, complete
measurements. Every cell of the inapplicable metric retains its `unsupported`
evidence, including its reason and lack of watermark.

| Constructor | Meaning and required evidence |
| --- | --- |
| `MetricEvidence.present(data_through)` | Measured values through a timezone-aware watermark. Every matched row for the constituent must carry a non-null value for this metric. |
| `MetricEvidence.explicit_zero(data_through=...)` | An observed zero. The watermark defaults to the fetch watermark. Any supplied values must be finite numeric zeros. |
| `MetricEvidence.missing(reason)` | No answer for this metric. A stable reason is required; a watermark is forbidden. |
| `MetricEvidence.delayed(reason, data_through=...)` | Not ready yet. A stable reason is required; retain a known watermark when available. |
| `MetricEvidence.unavailable(reason)` | This metric is inapplicable to this constituent. Emits `unsupported`; a reason is required and a watermark is forbidden. Use `missing` or `delayed` for an applicable measurement that has not arrived. |

Evidence is immutable. Direct `MetricEvidence(...)` construction enforces the
same invariants. Reasons use the existing manifest format: bounded ASCII,
at most 512 characters, beginning and ending with an alphanumeric character.
Use redacted explanations such as `not_video_inventory`, not provider error
payloads. Available statuses do not accept reasons.

The adapter supplies availability, watermarks, and measurements. The SDK alone
copies semantic-contract ID, version, and digest from the **selected offering**.
`MetricEvidence` has no semantic-contract fields. `cell_availability` is an
adapter input; the sealed manifest continues to use its existing
`metric_availability` list and statuses.

## Defaults and freshness

Omit `cell_availability` (or pass `None` or `{}`) to retain the existing derived
behavior. Omitted cells inherit their constituent's status, reason, and
watermark. Explicit cells take precedence over `covered_constituent_ids` and
`unavailable_constituents`, including explicit measurements for an otherwise
missing constituent. Coverage is then reconciled from the resolved cells:
uniform statuses stay uniform. A mixture of `present`, `explicit_zero`, and
`unsupported` cells is `present` if at least one cell is available. Other
mixtures are `partial`; missing, delayed, and stale cells still prevent full
coverage.

An all-`unsupported` constituent has zero applicable metrics. It remains
`unsupported`, with a reason and no watermark, and cannot satisfy full
coverage. A manifest cannot label that constituent `partial` either. A result
containing only such constituents has coverage `none`, never an observed zero.
A partial-coverage request can retain that diagnostic result; a full-coverage
request returns `PARTIAL_RESULT` without publishing. Conformance rejects a
completed `none` result for a full-coverage request.

Explicit watermarks are bounded by period end, source read cutoff, and the
observation instant. A watermark before the period is rejected. An explicit
cell may advance the batch watermark while other cells keep their earlier
evidence. A fully available constituent uses its earliest cell watermark.
For an authoritative publication, an available cell whose bounded watermark
does not reach period end becomes `delayed`, with a reason and its watermark.
Cell evidence cannot bypass that freshness gate.

Rows can omit missing, unsupported, or delayed metrics; available measurements
for other metrics are retained. A control total is a checksum over the staged
rows -- a consumer recomputes it from the revision's rows -- so it covers every
staged row, including rows outside the requested coverage, which stay staged
with a warning. It is emitted when every staged row carries a valid finite
numeric value for the metric. Missing fields, nulls, booleans, and invalid
numbers prevent a total, and an omitted explicit-zero row value is not filled
in. Statuses the SDK derives on its own withdraw nothing: a result with no
`cell_availability` publishes exactly the totals it published before.

### The withdrawal invariant

A declared `missing`, `delayed`, or `unsupported` cell asserts that the source
produced **no measurement** for it. A control total is the exact sum of a metric
over every staged row, so the only question a withdrawal raises is whether it
can put a disclaimed value in that sum. Two shapes follow, and they are
deliberately not treated alike:

- **A withdrawal by a constituent that staged rows removes that metric's
  total.** Its own rows carry values the adapter has disclaimed, so summing
  them would contradict the evidence it supplied. The rows stay staged
  byte-for-byte -- an adapter may publish a provider payload unchanged and
  declare per cell which of its columns are measurements.
- **A withdrawal by a constituent that staged no rows removes nothing.** It
  reaches no sum, so the checksum over the rows that *were* measured is
  retained. A buy that delivered nothing and whose billing is pending is the
  answer per-metric evidence exists to give; withdrawing its neighbours'
  subtotal would destroy a checksum the consumer recomputes.

A batch with no rows at all is the one case where a withdrawal removes a total
on its own: a `0` there would publish unavailability as an observed zero.
Unavailability is otherwise carried by the cell's own evidence, never by a
missing total.

### Monetary columns have no third option

`spend`, plus every metric the trusted report definition froze through
`monetary_metric_units` / `monetary_control_total_units`, is reconciled by the
obligation ledger against the rows a revision retains. For those columns the
first shape above has nowhere to go: dropping the total makes the ledger refuse
the revision with `MONETARY_TOTAL_MISMATCH` on the immutable replay of every
retry, and keeping it would sum a value the adapter disclaims. So a withdrawn
monetary cell whose own constituent's rows report that metric raises
`ValueError` before anything is staged or sealed. Omit the metric from those
rows, or declare the cell measured.

The frozen slice request cannot carry those unit declarations --
`ReportingDefinitionBinding.to_wire()` keeps them off the wire so retained
contract hashes do not move -- so tell the adapter which columns they are:

```python
InlineReportingSource(
    capabilities=capabilities,
    fetch=fetch,
    monetary_metrics=[name for name, _ in obligation.definition.monetary_metric_units],
)
```

`spend` is always included. A custom money column that is **not** declared here
is treated as non-monetary, which can still wedge the obligation for that
column -- declare it whenever the obligation's definition does.

The existing zero-row wire rule is unchanged: an empty batch must be wholly
explicit-zero or wholly unavailable. It cannot mix available cells with
unavailable cells. To report a measured zero alongside an unavailable metric,
supply the source's normalized zero measurement row. The adapter does not
manufacture rows or silently convert unavailable metrics to zero.

## Common patterns

For complete source data, the existing row-list shorthand still works. To
declare its freshness explicitly:

```python
return InlineFetchResult.all_present(rows, data_through=watermark)
```

This uses the constituent defaults: constituents with rows are present and
covered constituents without rows are observed zeros. Bare `[]` still means
an observed zero; `None` still means not ready. The first six positional
`InlineFetchResult` arguments retain their meaning, and so do their control
totals -- only an explicitly declared cell withdraws one, under the
[withdrawal invariant](#the-withdrawal-invariant).

### Migrating positional result construction

`InlineFetchResult` accepts these six positional arguments, in order: `rows`,
`data_through`, `covered_constituent_ids`, `unavailable_constituents`,
`unavailable_status`, and `warnings`. The remaining fields -- `currency`,
`provisional_until`, and `cell_availability` -- are keyword-only.

If your adapter previously passed `currency` or `provisional_until` as the
seventh or eighth positional argument, move them to explicit keywords:

```python
return InlineFetchResult(
    rows,
    data_through,
    covered_constituent_ids,
    unavailable_constituents,
    unavailable_status,
    warnings,
    currency="USD",
    provisional_until=settles_at,
)
```

Passing either value positionally now raises `TypeError`. This constructor
change prevents a currency value from silently binding to settling evidence.
Existing keyword calls retain their behavior.

### Bulk availability helpers

Bulk helpers return maps to pass to `cell_availability`, leaving the result's
other options available:

```python
from adcp.reporting.inline_source import (
    metric_delayed_through,
    metric_unsupported_everywhere,
)

return InlineFetchResult(
    rows=rows,
    cell_availability=metric_unsupported_everywhere(
        request, "completed_views", "not_video_inventory"
    ),
)

# Or retain a delayed metric's watermark across every requested constituent:
return InlineFetchResult(
    rows=rows,
    cell_availability=metric_delayed_through(
        request, "viewability", watermark, reason="measurement_pending"
    ),
)
```

Only keys in the frozen requested matrix are accepted, even if the selected
offering declares additional metrics. Unknown keys, duplicate entries
exposed by a mapping, and non-`MetricEvidence` values raise `ValueError` before
staging or sealing. Evidence construction errors inside a fetch also propagate
as `ValueError`; they are not classified as transient provider failures.
Keys are not coerced or normalized. Ordinary Python dicts already discard
repeated keys; reject duplicates while parsing provider input if it can contain
them.

See the [sync and async type-check examples](../tests/type_checks/reporting_inline_source.py)
and the [conformance tests](../tests/conformance/reporting/test_inline_cell_availability.py).
Run `run_reporting_source_replay_conformance` against your adapter and staging
store to verify that the same execution key returns the original sealed
evidence even if source measurements or availability later change.
