"""Multi-platform seller example — N tenants, N platforms, one process.

See the README for the wiring story; this package exposes:

* :class:`MockGuaranteedPlatform` — fixed-allocation, capacity-bounded
  inventory (the canonical guaranteed-buy shape).
* :class:`MockNonGuaranteedPlatform` — programmatic remnant, always
  accepts, delivery scales with budget.
* :class:`MultiTenantAccountStore` — resolves wire account refs to
  Account[TenantMeta] with ``metadata['tenant_id']`` populated.
* :func:`build_router` — assembles the :class:`PlatformRouter` from the
  per-tenant platforms.
"""

from __future__ import annotations

from .account_store import MultiTenantAccountStore
from .mock_guaranteed import MockGuaranteedPlatform
from .mock_non_guaranteed import MockNonGuaranteedPlatform

__all__ = [
    "MockGuaranteedPlatform",
    "MockNonGuaranteedPlatform",
    "MultiTenantAccountStore",
]
