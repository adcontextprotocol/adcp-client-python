"""AdCP compatibility surfaces for buyers on older spec versions.

The framework natively validates against the SDK's pinned major (3.x).
Buyers on pre-3 wire shapes are handled by the per-tool adapter registry
in :mod:`adcp.compat.legacy` — see that module's docstring for the
``AdapterPair`` pattern and the JS-SDK parity notes.

AdCP 3.2 products-only brief projections use the stateful
:class:`LegacyPurchaseCoordinator`. It lives outside the pure wire adapter
registry because redemption requires durable state and crash reconciliation.
"""

from adcp.compat.negotiated_catalog_purchase import (
    CompatibleCatalog,
    CompatiblePurchaseResult,
    CoordinatorBuyProductsInput,
    LegacyCatalogContinuation,
    MediaBuyCompatibility,
    MediaBuyCompatibilityReport,
    MediaBuyLifecycle,
    MediaBuyLifecycleCompatibilityError,
    MediaBuyLifecycleCoordinator,
    NegotiatedCatalogBuyer,
)
from adcp.compat.purchase_continuation import (
    CompatibilityContinuationError,
    CompatibilityContinuationErrorCode,
    CompatibilityContinuationStore,
    CompatibilityOperationState,
    CompatibilityPurchaseOperation,
    InMemoryCompatibilityContinuationStore,
    LegacyPurchaseContinuation,
    LegacyPurchaseCoordinator,
    LegacyPurchaseExecution,
    LegacyPurchaseExecutor,
    LegacyPurchasePendingPoller,
    LegacyPurchaseReconciler,
    LegacyPurchaseResult,
    PendingTaskResolution,
    ReconciliationResult,
    ReconciliationStatus,
    canonical_account_identity,
)
from adcp.compat.sqlite_continuation_store import SqliteCompatibilityContinuationStore

__all__ = [
    "CompatibleCatalog",
    "CompatiblePurchaseResult",
    "CoordinatorBuyProductsInput",
    "CompatibilityContinuationError",
    "CompatibilityContinuationErrorCode",
    "CompatibilityContinuationStore",
    "CompatibilityOperationState",
    "CompatibilityPurchaseOperation",
    "InMemoryCompatibilityContinuationStore",
    "LegacyPurchaseContinuation",
    "LegacyCatalogContinuation",
    "LegacyPurchaseCoordinator",
    "LegacyPurchaseExecution",
    "LegacyPurchaseExecutor",
    "LegacyPurchasePendingPoller",
    "LegacyPurchaseReconciler",
    "LegacyPurchaseResult",
    "MediaBuyCompatibility",
    "MediaBuyCompatibilityReport",
    "MediaBuyLifecycle",
    "MediaBuyLifecycleCompatibilityError",
    "MediaBuyLifecycleCoordinator",
    "NegotiatedCatalogBuyer",
    "PendingTaskResolution",
    "ReconciliationResult",
    "ReconciliationStatus",
    "SqliteCompatibilityContinuationStore",
    "canonical_account_identity",
]
