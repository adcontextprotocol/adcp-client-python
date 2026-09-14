"""Reliable Reporting: buyer-side reconciliation and seller-side production.

Two halves of the same ledger live under this package.

Buyer side (:mod:`adcp.reporting._reconcile`, re-exported here)
    :func:`reconcile_reporting_core` and friends read a seller's
    ``get_reporting_status`` ledger and decide whether reporting for a scope is
    definitive.  These names were previously importable from ``adcp.reporting``
    when it was a single module; that import path is unchanged.

Seller side (:mod:`adcp.reporting.source`, :mod:`adcp.reporting.conformance`)
    The transport-independent producer contract: an executor fetches one frozen
    slice from a reporting source and returns one immutable
    :class:`~adcp.reporting.source.SourceBatchManifestV1` whose bytes are
    ``canonical_json_utf8_v1``.  The conformance validators are the executable
    definition of "conforming"; run them in your own test suite.

    :mod:`adcp.reporting.inline_source` is the on-ramp: wrap the delivery fetch
    you already have and it produces conforming publications for you.

    :mod:`adcp.reporting.ledger` is the other half -- obligations, immutable
    revisions, the ``reporting.core`` health projection, and a
    framework-agnostic ``get_reporting_status`` handler.

Submodules are imported lazily (:pep:`562`) so ``import adcp.reporting`` stays
cheap for buyers who never touch the producer contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adcp.reporting._reconcile import (
    ConsumerLoopView,
    ConsumerStatusCheckpointStore,
    ConsumerStatusIntent,
    ConsumerStatusPlanError,
    ConsumerStatusPostError,
    ConsumerStatusPostResult,
    ExpectedReportingPeriod,
    InMemoryConsumerStatusCheckpoints,
    ObligationReconciliation,
    ReportingCheckpointStore,
    ReportingContentReading,
    ReportingFailureCode,
    ReportingInspectionContext,
    ReportingLedger,
    ReportingObservation,
    ReportingOperationsContactView,
    ReportingPinnedDefinition,
    ReportingReconciliationClient,
    ReportingReconciliationError,
    ReportingReconciliationResult,
    ReportingStatusClient,
    ReportingTier,
    build_reporting_receipt,
    classify_content_mismatch,
    consumer_status_chain_key,
    evaluate_reporting_ledger,
    load_consumer_loop_view,
    load_reporting_ledger,
    plan_consumer_statuses,
    post_consumer_statuses,
    reconcile_reporting,
    reconcile_reporting_core,
    reporting_tiers,
)

if TYPE_CHECKING:
    from adcp.reporting import canonical_json as canonical_json
    from adcp.reporting import conformance as conformance
    from adcp.reporting import fixtures as fixtures
    from adcp.reporting import inline_source as inline_source
    from adcp.reporting import ledger as ledger
    from adcp.reporting import source as source

_LAZY_SUBMODULES = frozenset(
    {"canonical_json", "conformance", "fixtures", "inline_source", "ledger", "source"}
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_SUBMODULES:
        import importlib

        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__) | _LAZY_SUBMODULES)


__all__ = [
    "ExpectedReportingPeriod",
    "ObligationReconciliation",
    "ReportingCheckpointStore",
    "ReportingInspectionContext",
    "ReportingLedger",
    "ReportingObservation",
    "ReportingReconciliationClient",
    "ReportingReconciliationError",
    "ReportingReconciliationResult",
    "ReportingStatusClient",
    "ReportingTier",
    "build_reporting_receipt",
    "evaluate_reporting_ledger",
    "load_reporting_ledger",
    "ConsumerLoopView",
    "ConsumerStatusCheckpointStore",
    "ConsumerStatusIntent",
    "ConsumerStatusPlanError",
    "ConsumerStatusPostError",
    "ConsumerStatusPostResult",
    "InMemoryConsumerStatusCheckpoints",
    "ReportingContentReading",
    "ReportingFailureCode",
    "ReportingOperationsContactView",
    "ReportingPinnedDefinition",
    "classify_content_mismatch",
    "consumer_status_chain_key",
    "load_consumer_loop_view",
    "plan_consumer_statuses",
    "post_consumer_statuses",
    "reconcile_reporting",
    "reconcile_reporting_core",
    "reporting_tiers",
]
