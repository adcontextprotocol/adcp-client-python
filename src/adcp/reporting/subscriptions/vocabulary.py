"""Frozen reporting-subscription vocabularies from the spec (adcp#8616).

``reason-codes.json`` and ``error-codes.json`` in
``static/compliance/source/test-vectors/reporting-subscriptions/`` freeze why a
period is or is not owed and the error codes SDKs raise. Both SDKs use them
verbatim, so a buyer's and a seller's explanation of the same period, and
their errors, compare by code. Renaming or removing a code needs a new
``vocabulary_version``.
"""

from __future__ import annotations

from collections.abc import Mapping

#: The spec vocabulary these constants mirror.
COVERAGE_VOCABULARY_VERSION = 1

#: Rules in evaluation order: a period takes the reason of the first rule that rejects it.
COVERAGE_RULE_ORDER: tuple[str, ...] = (
    "scope",
    "authorization",
    "version",
    "activity",
    "offering",
    "flight",
    "confirmation",
    "exception",
    "gap",
    "cutover",
    "timezone",
)

#: Every coverage reason code and the rule that assigns it; ``None`` for the
#: outcome codes a period takes when no rule rejects it.
COVERAGE_REASON_CODES: Mapping[str, str | None] = {
    "not_in_scope": "scope",
    "excluded:excluded_by_buyer": "scope",
    "not_authorized": "authorization",
    "before_coverage_start": "version",
    "subscription_inactive": "activity",
    "excluded:offering_unsupported": "offering",
    "outside_flight": "flight",
    "after_stop": "flight",
    "before_confirmation": "confirmation",
    "excluded:capacity": "exception",
    "excluded:overridden": "exception",
    "excluded:action_required": "exception",
    "gap:unauthorized": "gap",
    "cutover_stretch": "cutover",
    "owed_under_other_version": "cutover",
    "period_not_ended": None,
    "owed": None,
    "owed:regrant_backfill": None,
    "owed:withheld": None,
    "buy_not_visible": "scope",
}

__all__ = ["COVERAGE_REASON_CODES", "COVERAGE_RULE_ORDER", "COVERAGE_VOCABULARY_VERSION"]
