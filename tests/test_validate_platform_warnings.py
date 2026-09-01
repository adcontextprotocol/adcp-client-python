"""Tests for the v6.0 rc.1 sales-* surface-broadening soft-warn path
(DX-423).

Covers ``validate_platform``'s ``RECOMMENDED_METHODS_PER_SPECIALISM``
walk: a sales-* platform that implements the five strict-required
methods but is missing one of the four v6.0 rc.1 recommended methods
(``get_media_buys``, ``provide_performance_feedback``,
``list_creative_formats_legacy``, ``list_creatives``) emits a
``UserWarning`` per missing method, deduped across overlapping
specialisms. Setting ``ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM=1``
flips the warning into an ``AdcpError("INVALID_REQUEST")``.

Regression guard: the v3 reference seller, post-broadening (PR #408),
implements all nine methods, so importing its platform and running
``validate_platform`` MUST emit zero warnings.
"""

from __future__ import annotations

import warnings

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
)
from adcp.decisioning.dispatch import (
    RECOMMENDED_METHODS_PER_SPECIALISM,
    validate_platform,
)

# ---------------------------------------------------------------------------
# Helpers — platforms that satisfy the strict required surface but vary
# in their recommended-method coverage.
# ---------------------------------------------------------------------------


def _add_strict_required(cls: type[DecisioningPlatform]) -> None:
    """Stamp the five strict-required sales-* methods onto a class so
    each test only has to declare its recommended-method coverage."""

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "mb_1"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"deliveries": []}

    cls.get_products = get_products  # type: ignore[attr-defined]
    cls.create_media_buy = create_media_buy  # type: ignore[attr-defined]
    cls.update_media_buy = update_media_buy  # type: ignore[attr-defined]
    cls.sync_creatives = sync_creatives  # type: ignore[attr-defined]
    cls.get_media_buy_delivery = get_media_buy_delivery  # type: ignore[attr-defined]


class _PartialSalesPlatform(DecisioningPlatform):
    """sales-non-guaranteed seller missing all four recommended methods."""

    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="acct-1")


_add_strict_required(_PartialSalesPlatform)


class _FullSalesPlatform(DecisioningPlatform):
    """sales-non-guaranteed seller implementing all 9 methods."""

    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="acct-1")

    def get_media_buys(self, req, ctx):
        return {"media_buys": []}

    def provide_performance_feedback(self, req, ctx):
        return {"acknowledged": True}

    def list_creative_formats_legacy(self, req, ctx):
        return {"formats": []}

    def list_creatives(self, req, ctx):
        return {"creatives": []}


_add_strict_required(_FullSalesPlatform)


class _DualSalesPlatform(DecisioningPlatform):
    """Platform claiming TWO sales-* specialisms with overlapping
    recommended sets — used to exercise the dedup path."""

    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed", "sales-guaranteed"])
    accounts = SingletonAccounts(account_id="acct-1")


_add_strict_required(_DualSalesPlatform)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_warns_when_sales_recommended_method_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sales-non-guaranteed platform missing get_media_buys (and the
    other three recommended methods) emits one UserWarning per missing
    method. Each warning identifies the specialism + missing method and
    points at the SalesPlatform Protocol docstring."""

    monkeypatch.delenv("ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_platform(_PartialSalesPlatform())

    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    # All four recommended methods missing.
    assert len(user_warnings) == 4
    methods_warned = set()
    for w in user_warnings:
        msg = str(w.message)
        assert "sales-non-guaranteed" in msg
        assert "src/adcp/decisioning/specialisms/sales.py:184-227" in msg
        assert "ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM" in msg
        # Pull the warned method name out of the message.
        for method in (
            "get_media_buys",
            "provide_performance_feedback",
            "list_creative_formats_legacy",
            "list_creatives",
        ):
            if f"'{method}'" in msg:
                methods_warned.add(method)
    assert methods_warned == {
        "get_media_buys",
        "provide_performance_feedback",
        "list_creative_formats_legacy",
        "list_creatives",
    }


def test_no_warning_when_all_recommended_methods_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sales-* platform implementing all 9 methods (five required +
    four recommended) emits zero warnings from validate_platform."""

    monkeypatch.delenv("ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_platform(_FullSalesPlatform())

    assert [w for w in caught if issubclass(w.category, UserWarning)] == []


def test_strict_mode_raises_instead_of_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM=1 flips recommended
    misses from UserWarning to AdcpError("INVALID_REQUEST")."""

    monkeypatch.setenv("ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM", "1")
    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialSalesPlatform())
    err = exc_info.value
    assert err.code == "INVALID_REQUEST"
    assert "Strict mode is enabled" in str(err)
    missing = err.details["missing_recommended"]
    methods = {entry["method"] for entry in missing}
    assert methods == {
        "get_media_buys",
        "provide_performance_feedback",
        "list_creative_formats_legacy",
        "list_creatives",
    }
    assert all(entry["specialism"] == "sales-non-guaranteed" for entry in missing)


def test_strict_mode_only_triggers_on_value_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env-var contract is exactly ``"1"``. Other truthy strings
    (``"true"``, ``"yes"``) keep soft-warn behavior. Documenting this
    pin via test so a future ``"true"``-tolerant rewrite has to update
    both sides intentionally."""

    monkeypatch.setenv("ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM", "true")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_platform(_PartialSalesPlatform())
    assert any(issubclass(w.category, UserWarning) for w in caught)


def test_warning_dedup_across_overlapping_specialisms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform claiming two sales-* specialisms with the same
    recommended set warns once per missing method, not once per
    (specialism, method) pair."""

    monkeypatch.delenv("ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_platform(_DualSalesPlatform())

    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    # Four recommended methods, two specialisms with overlapping sets:
    # eight warnings WITHOUT dedup, four WITH dedup.
    assert len(user_warnings) == 4


def test_recommended_map_covers_all_sales_specialisms() -> None:
    """Sanity guard: every sales-* slug in the spec enum that has a
    Protocol surface MUST appear in RECOMMENDED_METHODS_PER_SPECIALISM,
    so a future spec slug addition that forgets the wiring fails this
    test rather than shipping silently-unenforced."""

    expected_sales_slugs = {
        "sales-non-guaranteed",
        "sales-guaranteed",
        "sales-broadcast-tv",
        "sales-dooh",
        "sales-social",
        "sales-proposal-mode",
        "sales-catalog-driven",
    }
    assert expected_sales_slugs <= set(RECOMMENDED_METHODS_PER_SPECIALISM.keys())
    expected_recommended = frozenset(
        {
            "get_media_buys",
            "provide_performance_feedback",
            "list_creative_formats_legacy",
            "list_creatives",
        }
    )
    for slug in expected_sales_slugs:
        assert (
            RECOMMENDED_METHODS_PER_SPECIALISM[slug] == expected_recommended
        ), f"recommended-method drift on {slug}"


def test_v3_reference_seller_passes_with_no_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for PR #408: the v3 reference seller broadened
    its surface to include all four v6.0 rc.1 recommended methods, so
    validate_platform on a v3 ref seller instance MUST emit zero
    UserWarnings. If this test fails, PR #408 has regressed.

    The seller's ``__init__`` requires a SQLAlchemy ``async_sessionmaker``
    plus the upstream API key (Phase 3 of the lifecycle-state-and-
    sandbox-authority work — the platform builds its
    :class:`UpstreamHttpClient` lazily via :meth:`upstream_for`). We
    build the instance with a sentinel sessionmaker because
    ``validate_platform`` only walks attributes — it never executes
    the methods, so the sessionmaker is never touched.
    """

    v3_platform = pytest.importorskip("examples.v3_reference_seller.src.platform")
    seller_cls = v3_platform.V3ReferenceSeller

    # Sentinel sessionmaker — never invoked by validate_platform.
    class _StubSessionmaker:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("validate_platform must not open a session")

    seller = seller_cls(
        sessionmaker=_StubSessionmaker(),
        upstream_api_key="test-key",
    )

    monkeypatch.delenv("ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_platform(seller)

    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert user_warnings == [], (
        "v3 ref seller emitted unexpected UserWarning(s) — DX-423 regression: "
        f"{[str(w.message) for w in user_warnings]}"
    )
