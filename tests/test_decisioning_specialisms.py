"""Per-specialism Protocol tests.

Covers ``SignalsPlatform`` (signal-marketplace, signal-owned) and
``AudiencePlatform`` (audience-sync). The ``SalesPlatform`` Protocol
is exercised end-to-end by the foundation tests
(``test_decisioning_handler.py``, ``test_hello_seller_integration.py``);
this file fills the breadth-sprint Batch 1 coverage for the two
specialisms shipped alongside it.

Three test surfaces per Protocol:

1. ``runtime_checkable`` conformance — a class implementing the
   methods passes ``isinstance`` against the Protocol.
2. ``validate_platform`` required-method enforcement — claiming the
   slug without the methods fails server boot.
3. Public exports — the Protocol is on ``adcp.decisioning.__all__``
   so adopters import from the canonical surface.
"""

from __future__ import annotations

import pytest

from adcp.decisioning import (
    AudiencePlatform,
    DecisioningCapabilities,
    DecisioningPlatform,
    SalesPlatform,
    SignalsPlatform,
    SingletonAccounts,
)
from adcp.decisioning.dispatch import (
    REQUIRED_METHODS_PER_SPECIALISM,
    validate_platform,
)
from adcp.decisioning.types import AdcpError

# ---- Public exports ----


def test_specialism_protocols_are_publicly_exported() -> None:
    """The three Protocol classes are on ``adcp.decisioning.__all__``
    so adopters import from the canonical public surface, not the
    internal ``adcp.decisioning.specialisms.*`` modules."""
    import adcp.decisioning as dx

    assert "SalesPlatform" in dx.__all__
    assert "SignalsPlatform" in dx.__all__
    assert "AudiencePlatform" in dx.__all__
    assert dx.SignalsPlatform is SignalsPlatform
    assert dx.AudiencePlatform is AudiencePlatform


# ---- SignalsPlatform ----


def test_signals_platform_runtime_checkable() -> None:
    """A class with ``get_signals`` + ``activate_signal`` passes
    ``isinstance`` against :class:`SignalsPlatform` thanks to the
    Protocol's ``@runtime_checkable`` decoration."""

    class _SignalsImpl:
        def get_signals(self, req, ctx):
            return {"signals": []}

        def activate_signal(self, req, ctx):
            return {"deployments": []}

    assert isinstance(_SignalsImpl(), SignalsPlatform)


def test_signals_platform_runtime_check_fails_when_methods_missing() -> None:
    """A class missing ``activate_signal`` does NOT pass the
    isinstance check. ``runtime_checkable`` matches by attribute name
    presence."""

    class _Partial:
        def get_signals(self, req, ctx):
            return {"signals": []}

        # Missing: activate_signal

    assert not isinstance(_Partial(), SignalsPlatform)


def test_validate_platform_enforces_signal_marketplace_methods() -> None:
    """A platform claiming ``signal-marketplace`` without implementing
    ``get_signals`` + ``activate_signal`` fails fast at server boot."""

    class _PartialSignalsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["signal-marketplace"])
        accounts = SingletonAccounts(account_id="hello")

        # Implements only get_signals; missing activate_signal.
        def get_signals(self, req, ctx):
            return {"signals": []}

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialSignalsPlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "activate_signal" in missing_methods


def test_validate_platform_enforces_signal_owned_methods() -> None:
    """``signal-owned`` shares the SignalsPlatform Protocol surface —
    same required-method enforcement."""

    class _PartialSignalOwnedPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["signal-owned"])
        accounts = SingletonAccounts(account_id="hello")
        # Implements neither method.

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialSignalOwnedPlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "get_signals" in missing_methods
    assert "activate_signal" in missing_methods


def test_validate_platform_passes_for_complete_signals_platform() -> None:
    """Happy path — fully-implemented signals platform passes."""

    class _CompleteSignalsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["signal-marketplace"])
        accounts = SingletonAccounts(account_id="hello")

        def get_signals(self, req, ctx):
            return {"signals": []}

        def activate_signal(self, req, ctx):
            return {"deployments": []}

    validate_platform(_CompleteSignalsPlatform())


def test_signal_marketplace_and_signal_owned_share_method_set() -> None:
    """Both signal specialisms gate on the same two methods. Drift in
    REQUIRED_METHODS_PER_SPECIALISM here surfaces as a visible test
    failure since they should track together."""
    expected = {"get_signals", "activate_signal"}
    assert REQUIRED_METHODS_PER_SPECIALISM["signal-marketplace"] == expected
    assert REQUIRED_METHODS_PER_SPECIALISM["signal-owned"] == expected


# ---- AudiencePlatform ----


def test_audience_platform_runtime_checkable() -> None:
    """A class with ``sync_audiences`` + ``poll_audience_statuses``
    passes ``isinstance`` against :class:`AudiencePlatform`."""

    class _AudienceImpl:
        def sync_audiences(self, audiences, ctx):
            return {"audiences": []}

        def poll_audience_statuses(self, audience_ids, ctx):
            return {}

    assert isinstance(_AudienceImpl(), AudiencePlatform)


def test_validate_platform_enforces_audience_sync_required_method() -> None:
    """A platform claiming ``audience-sync`` without implementing
    ``sync_audiences`` fails fast. ``poll_audience_statuses`` is
    NOT required (adopter-internal helper)."""

    class _PartialAudiencePlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["audience-sync"])
        accounts = SingletonAccounts(account_id="hello")
        # Missing sync_audiences entirely.

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialAudiencePlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "sync_audiences" in missing_methods


def test_validate_platform_passes_for_audience_sync_with_only_required_method() -> None:
    """``poll_audience_statuses`` is adopter-internal — not required
    for spec coverage. A platform implementing only ``sync_audiences``
    passes validation."""

    class _MinimalAudiencePlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["audience-sync"])
        accounts = SingletonAccounts(account_id="hello")

        def sync_audiences(self, audiences, ctx):
            return {"audiences": []}

    validate_platform(_MinimalAudiencePlatform())


def test_audience_sync_required_methods_pinned() -> None:
    """Contract test — the ``audience-sync`` required-method set is
    deliberately narrow (``sync_audiences`` only;
    ``poll_audience_statuses`` is adopter-internal).
    REQUIRED_METHODS_PER_SPECIALISM tracks the wire-required surface,
    not the full Protocol."""
    assert REQUIRED_METHODS_PER_SPECIALISM["audience-sync"] == {"sync_audiences"}


# ---- Cross-specialism: validate_platform doesn't conflate slugs ----


def test_signals_platform_can_compose_with_sales() -> None:
    """A platform claiming both ``sales-non-guaranteed`` and
    ``signal-marketplace`` must satisfy both Protocols' required
    methods. Cross-specialism composition is supported."""

    class _ComposedPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed", "signal-marketplace"]
        )
        accounts = SingletonAccounts(account_id="hello")

        # Sales-* methods
        def get_products(self, req, ctx):
            return {"products": []}

        def create_media_buy(self, req, ctx):
            return {}

        def update_media_buy(self, media_buy_id, patch, ctx):
            return {}

        def sync_creatives(self, req, ctx):
            return {}

        def get_media_buy_delivery(self, req, ctx):
            return {}

        # Signals methods
        def get_signals(self, req, ctx):
            return {"signals": []}

        def activate_signal(self, req, ctx):
            return {"deployments": []}

    validate_platform(_ComposedPlatform())


def test_sales_platform_protocol_still_runtime_checkable() -> None:
    """Round-trip: the existing ``SalesPlatform`` Protocol still works
    (Batch 1 didn't accidentally break the v6.0 baseline)."""

    class _SalesImpl:
        def get_products(self, req, ctx):
            return {"products": []}

        def create_media_buy(self, req, ctx):
            return {}

        def update_media_buy(self, media_buy_id, patch, ctx):
            return {}

        def sync_creatives(self, req, ctx):
            return {}

        def get_media_buy_delivery(self, req, ctx):
            return {}

        # Optional methods left unimplemented — runtime_checkable
        # checks attribute presence; methods on the Protocol that
        # aren't on the impl fail isinstance.

    # Required methods present, optional missing — runtime_checkable
    # matches by full attribute set so this is False (acceptable; the
    # base SalesPlatform declares 9 methods and runtime_checkable
    # requires all of them).
    # The validate_platform path uses a narrower required-set check,
    # which is what production servers actually rely on.
    assert REQUIRED_METHODS_PER_SPECIALISM["sales-non-guaranteed"] == {
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
    }

    # Smoke check that SalesPlatform symbol is still a runtime-checkable
    # Protocol (not redefined or shadowed). We verify by isinstance
    # against a minimal-but-complete impl rather than checking
    # ``_is_protocol`` (a private CPython typing internal — brittle
    # against typing-module changes).
    class _SalesShim:
        def get_products(self, req, ctx):
            return {"products": []}

        def create_media_buy(self, req, ctx):
            return {}

        def update_media_buy(self, media_buy_id, patch, ctx):
            return {}

        def sync_creatives(self, req, ctx):
            return {}

        def get_media_buy_delivery(self, req, ctx):
            return {}

        def get_media_buys(self, req, ctx):
            return {}

        def provide_performance_feedback(self, req, ctx):
            return {}

        def list_creative_formats(self, req, ctx):
            return {}

        def list_creatives(self, req, ctx):
            return {}

    assert isinstance(_SalesShim(), SalesPlatform)
