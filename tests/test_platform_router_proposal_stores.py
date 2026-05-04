"""Tests for PlatformRouter v1.5 proposal_stores= kwarg + cross-store
consistency check (D5).

Covers:

* proposal_stores keys must be a subset of platforms keys (orphan-key check).
* finalize=True ProposalManager without a wired ProposalStore for that
  tenant raises ValueError at construction with the exact remediation
  kwarg in the message.
* finalize=False ProposalManager (catalog mode) doesn't require a store
  — wiring a store is optional.
* proposal_store_for_tenant accessor returns the registered store, or
  None when none is wired.
* Mixed-version tenants — tenant A on v1 (no store), tenant B on v1.5
  (store wired) — supported.
"""

from __future__ import annotations

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryProposalStore,
    PlatformRouter,
    ProposalCapabilities,
)


class _CatalogManager:
    """ProposalManager with finalize=False (v1 catalog mode) — no store
    needed."""

    capabilities = ProposalCapabilities(
        sales_specialism="sales-non-guaranteed",
        finalize=False,
    )

    def get_products(self, req, ctx):  # type: ignore[no-untyped-def]
        return {}


class _FinalizableManager:
    """ProposalManager with finalize=True (v1.5) — REQUIRES a wired
    ProposalStore for the same tenant."""

    capabilities = ProposalCapabilities(
        sales_specialism="sales-non-guaranteed",
        refine=True,
        finalize=True,
    )

    def get_products(self, req, ctx):  # type: ignore[no-untyped-def]
        return {}

    def refine_products(self, req, ctx):  # type: ignore[no-untyped-def]
        return {}

    def finalize_proposal(self, req, ctx):  # type: ignore[no-untyped-def]
        return None


class _StubPlatform(DecisioningPlatform):
    """Stub DecisioningPlatform — only the bits the router needs."""

    capabilities = None  # type: ignore[assignment]
    accounts = None  # type: ignore[assignment]


class _StubAccounts:
    resolution = "explicit"

    def resolve(self, ref=None, auth_info=None):  # type: ignore[no-untyped-def]
        return None


# ---------------------------------------------------------------------------
# Cross-store consistency (D5)
# ---------------------------------------------------------------------------


def test_finalize_capable_manager_without_store_raises() -> None:
    """The hard-error posture per D5 — multi-worker deployments without
    a durable store lose proposals at the first worker that didn't see
    put_draft."""
    with pytest.raises(ValueError) as exc:
        PlatformRouter(
            accounts=_StubAccounts(),
            platforms={"default": _StubPlatform()},
            proposal_managers={"default": _FinalizableManager()},
            capabilities=DecisioningCapabilities(),
        )
    msg = str(exc.value)
    assert "finalize=True" in msg
    assert "proposal_stores" in msg
    # The remediation hint names the exact kwarg shape.
    assert "InMemoryProposalStore()" in msg


def test_finalize_capable_with_store_constructs_cleanly() -> None:
    router = PlatformRouter(
        accounts=_StubAccounts(),
        platforms={"default": _StubPlatform()},
        proposal_managers={"default": _FinalizableManager()},
        proposal_stores={"default": InMemoryProposalStore()},
        capabilities=DecisioningCapabilities(),
    )
    assert router.proposal_store_for_tenant("default") is not None


def test_catalog_only_manager_needs_no_store() -> None:
    """finalize=False managers (v1 catalog mode) wire no store — strictly
    additive, nothing breaks."""
    router = PlatformRouter(
        accounts=_StubAccounts(),
        platforms={"default": _StubPlatform()},
        proposal_managers={"default": _CatalogManager()},
        capabilities=DecisioningCapabilities(),
    )
    assert router.proposal_store_for_tenant("default") is None


# ---------------------------------------------------------------------------
# Orphan-key validation (mirrors v1 proposal_managers shape)
# ---------------------------------------------------------------------------


def test_proposal_stores_orphan_keys_raise() -> None:
    """proposal_stores key not present in platforms → orphan."""
    with pytest.raises(ValueError) as exc:
        PlatformRouter(
            accounts=_StubAccounts(),
            platforms={"default": _StubPlatform()},
            proposal_stores={"orphan_tenant": InMemoryProposalStore()},
            capabilities=DecisioningCapabilities(),
        )
    assert "orphan" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Mixed-version tenants
# ---------------------------------------------------------------------------


def test_mixed_version_tenants_supported() -> None:
    """Tenant A on v1 (catalog manager, no store), tenant B on v1.5
    (finalizable manager + store) — both coexist behind one router."""
    router = PlatformRouter(
        accounts=_StubAccounts(),
        platforms={
            "tenant_a": _StubPlatform(),
            "tenant_b": _StubPlatform(),
        },
        proposal_managers={
            "tenant_a": _CatalogManager(),
            "tenant_b": _FinalizableManager(),
        },
        proposal_stores={
            "tenant_b": InMemoryProposalStore(),
        },
        capabilities=DecisioningCapabilities(),
    )
    assert router.proposal_store_for_tenant("tenant_a") is None
    assert router.proposal_store_for_tenant("tenant_b") is not None


def test_proposal_stores_kwarg_is_optional() -> None:
    """v1 adopters don't pass proposal_stores= — strictly opt-in for v1.5."""
    router = PlatformRouter(
        accounts=_StubAccounts(),
        platforms={"default": _StubPlatform()},
        capabilities=DecisioningCapabilities(),
    )
    assert router.proposal_store_for_tenant("default") is None
