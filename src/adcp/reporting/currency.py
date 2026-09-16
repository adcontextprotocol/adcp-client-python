"""Currency checks shared by trusted obligation writers and source adapters.

These helpers never choose a currency from report rows. A row or a manifest
can corroborate the seller's frozen currency, but cannot establish it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

__all__ = ["ReportingCurrencyError", "require_single_currency", "validate_currency"]


class ReportingCurrencyError(ValueError):
    """Reporting money cannot be interpreted safely; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def validate_currency(value: object) -> str:
    """Require an ISO 4217-shaped code, without coercion or a registry lookup."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ReportingCurrencyError(
            "INVALID_CURRENCY", "reporting currency must be three uppercase ASCII letters"
        )
    return value


def require_single_currency(currencies: Iterable[str]) -> str:
    """Resolve trusted constituent currencies, rejecting an empty or mixed scope.

    Call this inside a currency resolver with historical account/media-buy
    values, before an obligation is committed. It does not convert money.
    """
    resolved = {validate_currency(value) for value in currencies}
    if not resolved:
        return require_frozen_currency(None)
    if len(resolved) != 1:
        raise ReportingCurrencyError(
            "MIXED_CURRENCY_SCOPE", "one reporting obligation cannot aggregate multiple currencies"
        )
    return next(iter(resolved))


def require_frozen_currency(currency: str | None) -> str:
    if currency is None:
        raise ReportingCurrencyError(
            "CURRENCY_UNRESOLVED",
            "the obligation has no proven currency; retain its history and use verified "
            "historical evidence for an explicit repair, never current defaults",
        )
    return validate_currency(currency)


def validate_currency_units(currency: str, units: Iterable[tuple[str, str]]) -> None:
    """Check trusted monetary unit declarations against the resolved currency."""
    pinned = {validate_currency(unit) for _, unit in units}
    if len(pinned) > 1:
        raise ReportingCurrencyError(
            "MIXED_CURRENCY_SCOPE", "the pinned definition declares multiple monetary currencies"
        )
    if pinned and pinned != {currency}:
        raise ReportingCurrencyError(
            "CURRENCY_MISMATCH", "the currency disagrees with the pinned monetary definition"
        )


def _carries(row: Mapping[str, Any], name: str) -> bool:
    """Whether a row actually reports ``name``.

    ``None`` is "not reported", not zero and not an empty label -- the same
    reading the inline adapter's control totals take, so the two cannot
    disagree about which cells exist.
    """
    return row.get(name) is not None


def validate_row_currencies(currency: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Reject mixed/mismatched row evidence before any aggregation takes place."""
    observed = {validate_currency(row["currency"]) for row in rows if _carries(row, "currency")}
    if len(observed) > 1:
        raise ReportingCurrencyError(
            "MIXED_CURRENCY_SCOPE", "source rows contain multiple currencies"
        )
    if observed and observed != {currency}:
        raise ReportingCurrencyError(
            "CURRENCY_MISMATCH", "source row currency disagrees with the frozen currency"
        )


def monetary_decimal(value: object) -> Decimal:
    """An exact, finite decimal representation of a normalized metric value."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ReportingCurrencyError("MONETARY_TOTAL_MISMATCH", "invalid monetary value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ReportingCurrencyError("MONETARY_TOTAL_MISMATCH", "non-finite monetary value")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ReportingCurrencyError("MONETARY_TOTAL_MISMATCH", "invalid monetary value") from error
    if not result.is_finite():
        raise ReportingCurrencyError("MONETARY_TOTAL_MISMATCH", "non-finite monetary value")
    return result


def validate_monetary_content(
    *,
    currency: str,
    rows: Sequence[Mapping[str, Any]],
    totals: Sequence[tuple[str, str]],
    metric_units: Sequence[tuple[str, str]] = (),
    total_units: Sequence[tuple[str, str]] = (),
) -> None:
    """Check flat monetary columns and their same-name additive totals.

    ``spend`` is the built-in money column for the existing single-currency
    API. Other monetary columns/totals must be declared by the trusted pinned
    definition. Unitless values inherit that declaration, never a source hint.

    A metric absent from *some* rows has no honest additive total -- the same
    rule :func:`adcp.reporting.inline_source._control_totals` applies when it
    declines to publish one -- so a sparse money column is checked value by
    value and left unsummed. A published total is still reconciled exactly, and
    a column every row carries still has to come with one. Rows are what create
    that demand, so a period with no rows owes no derived total; ``total_units``
    is the declaration that demands one unconditionally.
    """
    validate_currency_units(currency, (*metric_units, *total_units))
    validate_row_currencies(currency, rows)
    declared_metrics = dict(metric_units)
    declared_totals = dict(total_units)
    if "spend" in dict(totals) or any(_carries(row, "spend") for row in rows):
        declared_metrics.setdefault("spend", currency)
    total_values = dict(totals)
    if len(total_values) != len(totals):
        raise ReportingCurrencyError("MONETARY_TOTAL_MISMATCH", "duplicate control total names")
    for name in declared_totals:
        if name not in total_values:
            raise ReportingCurrencyError(
                "MONETARY_TOTAL_MISMATCH", f"missing pinned monetary control total {name!r}"
            )
        monetary_decimal(total_values[name])
    for name in declared_metrics:
        values = [monetary_decimal(row[name]) for row in rows if _carries(row, name)]
        complete = len(values) == len(rows)
        if name not in total_values:
            if complete and rows:
                raise ReportingCurrencyError(
                    "MONETARY_TOTAL_MISMATCH",
                    f"monetary metric {name!r} needs rows and a control total",
                )
            continue
        if not complete:
            raise ReportingCurrencyError(
                "MONETARY_TOTAL_MISMATCH",
                f"control total {name!r} cannot be reconciled against rows missing it",
            )
        # Do not let an adopter's Decimal context round an otherwise exact sum.
        # Decimal exponents are integers after the finite check above.
        precision = sum(len(value.as_tuple().digits) for value in values) + 1
        if values:
            precision += max(value.adjusted() for value in values) - min(
                int(value.as_tuple().exponent) for value in values
            )
        with localcontext() as context:
            context.prec = max(28, precision)
            observed = sum(values, Decimal(0))
        if observed != monetary_decimal(total_values[name]):
            raise ReportingCurrencyError(
                "MONETARY_TOTAL_MISMATCH", f"control total {name!r} disagrees with monetary rows"
            )
