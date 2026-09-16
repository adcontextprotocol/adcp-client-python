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
request receives retryable `PARTIAL_RESULT` and no publication until every
requested cell is available.

| Constructor | Meaning and required evidence |
| --- | --- |
| `MetricEvidence.present(data_through)` | Measured values through a timezone-aware watermark. Every matched row for the constituent must carry a non-null value for this metric. |
| `MetricEvidence.explicit_zero(data_through=...)` | An observed zero. The watermark defaults to the fetch watermark. Any supplied values must be finite numeric zeros. |
| `MetricEvidence.missing(reason)` | No answer for this metric. A stable reason is required; a watermark is forbidden. |
| `MetricEvidence.delayed(reason, data_through=...)` | Not ready yet. A stable reason is required; retain a known watermark when available. |
| `MetricEvidence.unavailable(reason)` | The source cannot measure this metric for this constituent. Emits the existing wire status `unsupported`; a reason is required and a watermark is forbidden. |

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
uniform statuses stay uniform, present plus explicit-zero is `present`, and
other mixtures are `partial`.

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
in. Declaring any cell of a metric `missing`, `delayed`, or `unsupported`
withdraws that metric's total even when rows still carry values, because the
adapter has said those values are not a measurement. Statuses the SDK derives
on its own withdraw nothing: a result with no `cell_availability` publishes
exactly the totals it published before.

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
an observed zero; `None` still means not ready. Existing positional
`InlineFetchResult` arguments retain their meaning, and so do their control
totals -- only an explicitly declared cell withdraws one.

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
