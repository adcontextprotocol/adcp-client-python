"""Unit tests for ``ProposalCapabilities.auto_commit_on_put_draft`` (#723).

When a manager declares ``auto_commit_on_put_draft=True``, the framework
calls :meth:`ProposalStore.commit` immediately after
:meth:`ProposalStore.put_draft` on every proposal returned from
``get_products`` / ``refine_products``. Promotes ``DRAFT → COMMITTED``
in a single dispatch so a subsequent ``create_media_buy(proposal_id=X)``
can call ``try_reserve_consumption`` without a separate buyer finalize
round-trip.

Pre-#723 the storyboard
``media_buy_seller/proposal_finalize/create_media_buy`` was unreachable
for managers that didn't declare ``finalize=True``: brief mode returned
proposals as DRAFT, the next call's ``try_reserve_consumption`` raised
``PROPOSAL_NOT_COMMITTED``. Adopters worked around it by writing
``state=COMMITTED`` directly in ``put_draft`` (salesagent PR #390); this
capability ships the framework-side equivalent so the Protocol surface
stays canonical.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from adcp.decisioning import AdcpError, ProposalCapabilities
from adcp.decisioning.context import RequestContext
from adcp.decisioning.proposal_dispatch import maybe_persist_draft_after_get_products
from adcp.decisioning.proposal_store import InMemoryProposalStore, ProposalState
from adcp.decisioning.types import Account


def _account_with_tenant(tenant_id: str = "t1") -> Account:
    return Account(id="acct-1", metadata={"tenant_id": tenant_id})


def _ctx(account: Account | None = None) -> RequestContext[Any]:
    return RequestContext(account=account or _account_with_tenant())


class _RouterLike:
    """Minimal router-shaped platform: exposes the two duck-typed
    accessors ``proposal_dispatch`` walks via ``hasattr``. Lets us
    exercise ``maybe_persist_draft_after_get_products`` without standing
    up a full ``LazyPlatformRouter``."""

    def __init__(
        self,
        *,
        tenant_id: str = "t1",
        manager: Any | None = None,
        store: InMemoryProposalStore | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._manager = manager
        self._store = store

    def proposal_manager_for_tenant(self, tenant_id: str) -> Any | None:
        return self._manager if tenant_id == self._tenant_id else None

    def proposal_store_for_tenant(self, tenant_id: str) -> InMemoryProposalStore | None:
        return self._store if tenant_id == self._tenant_id else None


class _AutoCommitManager:
    """Manager declaring auto_commit_on_put_draft=True. The body is
    irrelevant — only ``.capabilities`` is consulted by
    ``maybe_persist_draft_after_get_products``."""

    def __init__(self, ttl_seconds: int = 7 * 24 * 3600) -> None:
        self.capabilities = ProposalCapabilities(
            sales_specialism="sales-non-guaranteed",
            auto_commit_on_put_draft=True,
            auto_commit_ttl_seconds=ttl_seconds,
        )


class _DraftOnlyManager:
    """Default manager — leaves proposals DRAFT (the pre-#723 behavior)."""

    capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")


# ----- happy path -------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_commit_promotes_draft_to_committed() -> None:
    """The whole point of the capability: after
    ``maybe_persist_draft_after_get_products`` returns, the store's
    record is COMMITTED, not DRAFT. The buyer's subsequent
    ``create_media_buy(proposal_id=X)`` can call
    ``try_reserve_consumption`` without a finalize round-trip."""
    store = InMemoryProposalStore()
    platform = _RouterLike(manager=_AutoCommitManager(), store=store)

    response = {
        "products": [],
        "proposals": [{"proposal_id": "p1"}],
    }
    await maybe_persist_draft_after_get_products(platform, response, _ctx())

    record = await store.get("p1", expected_account_id="acct-1")
    assert record is not None
    assert record.state is ProposalState.COMMITTED


@pytest.mark.asyncio
async def test_auto_commit_off_leaves_record_in_draft() -> None:
    """Default capability (``auto_commit_on_put_draft=False``) preserves
    the pre-#723 behavior: proposals land as DRAFT and the buyer must
    drive the transition via finalize. Regression guard against the
    capability defaulting to True or the dispatch path firing
    unconditionally."""
    store = InMemoryProposalStore()
    platform = _RouterLike(manager=_DraftOnlyManager(), store=store)

    response = {"products": [], "proposals": [{"proposal_id": "p1"}]}
    await maybe_persist_draft_after_get_products(platform, response, _ctx())

    record = await store.get("p1", expected_account_id="acct-1")
    assert record is not None
    assert record.state is ProposalState.DRAFT


@pytest.mark.asyncio
async def test_auto_commit_expires_at_uses_capability_ttl() -> None:
    """``auto_commit_ttl_seconds`` controls the COMMITTED record's
    ``expires_at``. Default is 7 days; override via the capability
    field for spot-market (shorter) or long-running RFP (longer)
    scenarios."""
    store = InMemoryProposalStore()
    short_ttl = 3600  # 1 hour
    platform = _RouterLike(
        manager=_AutoCommitManager(ttl_seconds=short_ttl),
        store=store,
    )

    before = datetime.now(timezone.utc).timestamp()
    await maybe_persist_draft_after_get_products(
        platform,
        {"products": [], "proposals": [{"proposal_id": "p1"}]},
        _ctx(),
    )
    after = datetime.now(timezone.utc).timestamp()

    record = await store.get("p1", expected_account_id="acct-1")
    assert record is not None
    assert record.expires_at is not None
    # expires_at should be ~now + ttl, within the test's wall-clock window.
    assert before + short_ttl - 1 <= record.expires_at.timestamp() <= after + short_ttl + 1


@pytest.mark.asyncio
async def test_auto_commit_handles_multiple_proposals_in_one_response() -> None:
    """A ``get_products`` response can return multiple proposals; every
    one must be promoted independently. No short-circuit on the first."""
    store = InMemoryProposalStore()
    platform = _RouterLike(manager=_AutoCommitManager(), store=store)

    await maybe_persist_draft_after_get_products(
        platform,
        {
            "products": [],
            "proposals": [
                {"proposal_id": "p1"},
                {"proposal_id": "p2"},
                {"proposal_id": "p3"},
            ],
        },
        _ctx(),
    )

    for pid in ("p1", "p2", "p3"):
        record = await store.get(pid, expected_account_id="acct-1")
        assert record is not None, f"missing proposal {pid}"
        assert (
            record.state is ProposalState.COMMITTED
        ), f"proposal {pid} expected COMMITTED, got {record.state}"


# ----- construction guards (ProposalCapabilities __post_init__) ---------


def test_auto_commit_and_finalize_mutually_exclusive() -> None:
    """auto-commit and finalize are different lifecycle models. Both
    declared simultaneously would race: framework auto-commits, then
    buyer's finalize call rejects because the record is no longer
    DRAFT. Loud-fail at construction with a clear message."""
    with pytest.raises(AdcpError, match="mutually exclusive"):
        ProposalCapabilities(
            sales_specialism="sales-non-guaranteed",
            finalize=True,
            auto_commit_on_put_draft=True,
        )


def test_auto_commit_ttl_zero_rejected() -> None:
    """Zero or negative TTL would mark proposals expired on commit,
    making every consumption attempt fail with PROPOSAL_EXPIRED.
    Construction-time loud-fail prevents the misconfig from shipping."""
    with pytest.raises(AdcpError, match="auto_commit_ttl_seconds must be"):
        ProposalCapabilities(
            sales_specialism="sales-non-guaranteed",
            auto_commit_on_put_draft=True,
            auto_commit_ttl_seconds=0,
        )

    with pytest.raises(AdcpError, match="auto_commit_ttl_seconds must be"):
        ProposalCapabilities(
            sales_specialism="sales-non-guaranteed",
            auto_commit_on_put_draft=True,
            auto_commit_ttl_seconds=-1,
        )


def test_auto_commit_default_is_false() -> None:
    """Back-compat: existing managers (which don't set the field) keep
    the pre-#723 DRAFT-only behavior. Belt-and-suspenders regression
    guard so the default isn't accidentally flipped."""
    caps = ProposalCapabilities(sales_specialism="sales-non-guaranteed")
    assert caps.auto_commit_on_put_draft is False


def test_auto_commit_rejected_on_sales_guaranteed() -> None:
    """Product safety guard (raised by review): auto-commit on
    ``sales-guaranteed`` issues a silent inventory hold on every
    ``get_products`` call. GAM / ad-server proposal lifecycles
    require explicit buyer-driven reservation precisely because
    trafficking ops won't accept silent holds — a 7-day default TTL
    would burn inventory across thousands of catalog probes per day.
    Loud-fail with a clear migration path."""
    with pytest.raises(AdcpError, match="sales-guaranteed"):
        ProposalCapabilities(
            sales_specialism="sales-guaranteed",
            auto_commit_on_put_draft=True,
        )


def test_auto_commit_long_ttl_emits_soft_cap_warning() -> None:
    """A TTL longer than 30 days holds inventory for an entire month
    per catalog probe. The framework permits it (long-running RFPs
    can legitimately need it) but warns at boot so the choice is
    visible. Adopters silence per-process via the warnings filter."""
    import warnings

    with pytest.warns(UserWarning, match="soft cap of"):
        ProposalCapabilities(
            sales_specialism="sales-non-guaranteed",
            auto_commit_on_put_draft=True,
            auto_commit_ttl_seconds=45 * 24 * 3600,  # 45 days
        )

    # Boundary check: exactly 30 days = no warning (cap is "exceeds",
    # not "meets").
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        ProposalCapabilities(
            sales_specialism="sales-non-guaranteed",
            auto_commit_on_put_draft=True,
            auto_commit_ttl_seconds=30 * 24 * 3600,  # exactly 30 days
        )


@pytest.mark.asyncio
async def test_catalog_mode_store_wired_manager_unwired_no_auto_commit() -> None:
    """Catalog-mode adopter: ``ProposalStore`` wired but no
    ``ProposalManager`` (no proposal-lifecycle dispatch). The
    auto-commit branch must be off in this configuration regardless
    of what any other manager's capabilities say — we read the
    capability off the tenant's own manager, which here is ``None``.
    Explicit pin so future refactors that resolve the capability via
    a different path (e.g. a router-level default) don't accidentally
    enable auto-commit in catalog mode."""
    store = InMemoryProposalStore()
    platform = _RouterLike(manager=None, store=store)

    await maybe_persist_draft_after_get_products(
        platform,
        {"products": [], "proposals": [{"proposal_id": "p1"}]},
        _ctx(),
    )

    record = await store.get("p1", expected_account_id="acct-1")
    assert record is not None
    assert record.state is ProposalState.DRAFT
