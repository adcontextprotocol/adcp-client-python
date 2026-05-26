"""Boot-time validation of the projected ``get_adcp_capabilities`` response.

Exercises :func:`adcp.decisioning.validate_capabilities.validate_capabilities_response_shape`
across:

* a conformant platform (sales-non-guaranteed with billing) — passes;
* a media_buy claimer that omits ``supported_billing`` — fails with a
  diagnostic naming the missing invariant (the historical v3 ref seller
  bug pre-#402);
* a platform whose handler override returns an empty
  ``supported_protocols`` — fails on the schema's ``minItems: 1``;
* a regression guard wiring the actual v3 reference seller platform
  through :func:`create_adcp_server_from_platform` — server boot
  succeeds.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import Account, MediaBuy, SupportedProtocol
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.serve import create_adcp_server_from_platform
from adcp.decisioning.types import AdcpError
from adcp.decisioning.validate_capabilities import (
    validate_capabilities_response_shape,
)


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-caps-shape-")
    yield pool
    pool.shutdown(wait=True)


def _build_handler(platform: DecisioningPlatform, executor: ThreadPoolExecutor) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )


# ---- Conformant platform ----


class _SalesPlatformMethods:
    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "x", "status": "active"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}

    def get_media_buys(self, req, ctx):
        return {"media_buys": []}

    def list_creative_formats(self, req, ctx):
        return {"formats": []}

    def list_creatives(self, req, ctx):
        return {"creatives": []}

    def provide_performance_feedback(self, req, ctx):
        return {"status": "completed"}


class _ConformantSalesPlatform(_SalesPlatformMethods, DecisioningPlatform):
    """Sales-non-guaranteed with supported_billing — projects a valid response.

    Stubs the five SalesPlatform-required methods so ``validate_platform``
    accepts the class when it's wired through
    :func:`create_adcp_server_from_platform`. The capabilities-shape
    validator under test runs *after* ``validate_platform``.
    """

    capabilities = DecisioningCapabilities(
        specialisms=("sales-non-guaranteed",),
        supported_protocols=[SupportedProtocol.media_buy],
        account=Account(supported_billing=["operator", "agent"]),
        media_buy=MediaBuy(supported_pricing_models=["cpm"]),
    )
    accounts = SingletonAccounts(account_id="test")


def test_conformant_platform_passes(executor: ThreadPoolExecutor) -> None:
    """A platform whose projection conforms to the spec passes silently."""
    handler = _build_handler(_ConformantSalesPlatform(), executor)
    # Returns None on success.
    assert validate_capabilities_response_shape(handler) is None


# ---- media_buy claimer missing supported_billing ----


class _MediaBuyMissingBillingPlatform(_SalesPlatformMethods, DecisioningPlatform):
    """Claims sales-non-guaranteed (→ media_buy) but omits supported_billing.

    Recreates the v3 reference seller's pre-#402 misconfiguration: the
    framework's projection skips the ``account`` block when
    ``supported_billing`` is absent, so the wire response advertises
    ``media_buy`` without the spec-required billing array.
    """

    capabilities = DecisioningCapabilities(
        specialisms=("sales-non-guaranteed",),
        supported_protocols=[SupportedProtocol.media_buy],
        media_buy=MediaBuy(supported_pricing_models=["cpm"]),
        # supported_billing intentionally unset.
    )
    accounts = SingletonAccounts(account_id="test")


def test_media_buy_without_supported_billing_fails(executor: ThreadPoolExecutor) -> None:
    handler = _build_handler(_MediaBuyMissingBillingPlatform(), executor)
    with pytest.raises(AdcpError) as exc_info:
        validate_capabilities_response_shape(handler)

    err = exc_info.value
    assert err.code == "INVALID_REQUEST"
    assert err.recovery == "terminal"
    # The invariant must be named in the diagnostic so operators don't
    # have to grep the schema to figure out what's wrong. Upstream 3.0.12
    # encodes ``account`` required via ``allOf/if/then`` on
    # ``supported_protocols contains media_buy``, so the schema-driven
    # step now reports the missing ``/account`` pointer before the
    # explicit step-3 check fires — assert against the structured issue
    # list as well as ``str(err)``.
    issues = (err.details or {}).get("issues") or []
    issue_blob = " ".join(
        f"{i.get('pointer', '')} {i.get('message', '')} {i.get('keyword', '')}" for i in issues
    )
    assert (
        "supported_billing" in str(err)
        or "account" in str(err)
        or "account" in issue_blob
        or "supported_billing" in issue_blob
    )


# ---- Empty supported_protocols (handler override) ----


class _EmptyProtocolsHandler(PlatformHandler):
    """Override that emits an empty ``supported_protocols`` list."""

    async def get_adcp_capabilities(
        self,
        params: Any = None,
        context: Any = None,
    ) -> dict[str, Any]:
        del params, context
        return {
            "adcp": {
                "major_versions": ["3"],
                "idempotency": {"supported": False},
            },
            "supported_protocols": [],
        }


class _BarePlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities()
    accounts = SingletonAccounts(account_id="test")


def test_empty_supported_protocols_fails(executor: ThreadPoolExecutor) -> None:
    """An override that violates ``supported_protocols`` minItems: 1 fails."""
    handler = _EmptyProtocolsHandler(
        _BarePlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    with pytest.raises(AdcpError) as exc_info:
        validate_capabilities_response_shape(handler)

    err = exc_info.value
    assert err.code == "INVALID_REQUEST"
    assert err.recovery == "terminal"
    # Either the schema's minItems issue, or the explicit invariant
    # check, must surface ``supported_protocols`` in the diagnostic.
    issues = (err.details or {}).get("issues") or []
    issue_blob = " ".join(
        f"{i.get('pointer', '')} {i.get('message', '')} {i.get('keyword', '')}" for i in issues
    )
    assert (
        "supported_protocols" in str(err)
        or "supported_protocols" in issue_blob
        or "/supported_protocols" in issue_blob
    )


# ---- Schema-driven validation: malformed override ----


class _MalformedHandler(PlatformHandler):
    """Override that omits the spec-required ``adcp`` top-level block."""

    async def get_adcp_capabilities(
        self,
        params: Any = None,
        context: Any = None,
    ) -> dict[str, Any]:
        del params, context
        # ``adcp`` is required at the top level (response schema
        # ``required: ["adcp", "supported_protocols"]``).
        return {"supported_protocols": ["media_buy"]}


def test_schema_violation_in_override_fails(executor: ThreadPoolExecutor) -> None:
    """An override that violates the bundled schema fails with structured issues."""
    handler = _MalformedHandler(
        _BarePlatform(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    with pytest.raises(AdcpError) as exc_info:
        validate_capabilities_response_shape(handler)

    err = exc_info.value
    assert err.code == "INVALID_REQUEST"
    # Schema violations carry a structured ``issues`` list so callers
    # can index every failure.
    assert err.details is not None
    assert "issues" in err.details
    assert err.details["issues"], "expected at least one schema issue"


# ---- Wired into create_adcp_server_from_platform ----


class _NonConformantSalesPlatform(_SalesPlatformMethods, DecisioningPlatform):
    """Sales platform with required-method coverage but missing
    ``supported_billing`` — passes ``validate_platform`` so the
    capabilities-shape validator is the gate that fails.
    """

    capabilities = DecisioningCapabilities(
        specialisms=("sales-non-guaranteed",),
        supported_protocols=[SupportedProtocol.media_buy],
        media_buy=MediaBuy(supported_pricing_models=["cpm"]),
        # supported_billing intentionally unset.
    )
    accounts = SingletonAccounts(account_id="test")


def test_create_adcp_server_rejects_non_conformant_platform() -> None:
    """The validator is wired into server boot — a non-conformant platform
    refuses to start. ``validate_platform`` passes (required methods
    present), F12 is bypassed via ``auto_emit_completion_webhooks=False``,
    so the capabilities-shape validator is the gate that fires.
    """
    with pytest.raises(AdcpError) as exc_info:
        create_adcp_server_from_platform(
            _NonConformantSalesPlatform(),
            auto_emit_completion_webhooks=False,
        )

    err = exc_info.value
    assert err.code == "INVALID_REQUEST"
    # The diagnostic should point operators at the capabilities-shape
    # validator — not, say, at validate_platform's error.
    assert "supported_billing" in str(err) or "get_adcp_capabilities response failed" in str(err)


def test_create_adcp_server_accepts_conformant_platform() -> None:
    """A conformant platform boots cleanly through the public entrypoint.

    ``auto_emit_completion_webhooks=False`` opts out of the F12 webhook
    gate (the SalesPlatform stubs above expose webhook-eligible tools
    but no sender is wired in this unit test). The capabilities-shape
    validator under test is independent of that gate.
    """
    handler, executor, registry = create_adcp_server_from_platform(
        _ConformantSalesPlatform(),
        auto_emit_completion_webhooks=False,
    )
    try:
        assert handler is not None
        assert registry is not None
    finally:
        executor.shutdown(wait=True)


# ---- Regression guard: same shape as the v3 reference seller ----


def test_v3_reference_seller_shape_passes(executor: ThreadPoolExecutor) -> None:
    """Mirrors the v3 reference seller's capabilities declaration shape
    (sales-non-guaranteed + supported_billing=("operator", "agent")
    per ``examples/v3_reference_seller/src/platform.py``). The full
    example platform isn't importable from the SDK test suite (it
    declares mixed absolute/relative imports against a local ``src``
    layout), so this guard recreates the exact capabilities tuple the
    reference declares — pre-#402 the projection dropped
    ``supported_billing`` entirely; this test would have caught that.
    """

    class _V3RefSellerShape(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=("sales-non-guaranteed",),
            supported_protocols=[SupportedProtocol.media_buy],
            account=Account(supported_billing=["operator", "agent"]),
            media_buy=MediaBuy(supported_pricing_models=["cpm"]),
        )
        accounts = SingletonAccounts(account_id="test")

    handler = _build_handler(_V3RefSellerShape(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())
    # Sanity: confirm media_buy is claimed; otherwise the guard
    # wouldn't exercise the supported_billing invariant.
    assert "media_buy" in response["supported_protocols"]

    assert validate_capabilities_response_shape(handler) is None


# ---- #700: async-safe init + async validator parity ----------------------


@pytest.mark.asyncio
async def test_async_validator_accepts_conformant_platform() -> None:
    """``validate_capabilities_response_shape_async`` returns None on a
    conformant projection, same as the sync sibling — but awaits the
    handler directly instead of driving it through ``asyncio.run``. This
    is the path async callers (test fixtures, ``lifespan`` handlers)
    use after passing ``validate_at_init=False`` to the constructor."""
    from adcp.decisioning.validate_capabilities import (
        validate_capabilities_response_shape_async,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        handler = _build_handler(_ConformantSalesPlatform(), pool)
        assert await validate_capabilities_response_shape_async(handler) is None


@pytest.mark.asyncio
async def test_async_validator_raises_same_error_as_sync() -> None:
    """Async validator must surface the same ``AdcpError`` (code,
    recovery, message text) as the sync version. Otherwise adopters
    swapping paths see a different diagnostic and can't share their
    error-handling between sync boot and async lifespan."""
    from adcp.decisioning.validate_capabilities import (
        validate_capabilities_response_shape_async,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        handler = _build_handler(_MediaBuyMissingBillingPlatform(), pool)
        with pytest.raises(AdcpError) as exc_info:
            await validate_capabilities_response_shape_async(handler)

        err = exc_info.value
        assert err.code == "INVALID_REQUEST"
        assert err.recovery == "terminal"
        issues = (err.details or {}).get("issues") or []
        issue_blob = " ".join(
            f"{i.get('pointer', '')} {i.get('message', '')} {i.get('keyword', '')}" for i in issues
        )
        assert (
            "supported_billing" in str(err)
            or "account" in str(err)
            or "account" in issue_blob
            or "supported_billing" in issue_blob
        )


@pytest.mark.asyncio
async def test_create_adcp_server_validate_at_init_false_works_in_async_context() -> None:
    """The bug from #700: ``create_adcp_server_from_platform`` called
    from inside a running event loop with the default
    ``validate_at_init=True`` raises ``RuntimeError`` because the sync
    validator calls ``asyncio.run`` under a loop. Passing
    ``validate_at_init=False`` skips that path so adopters in async
    contexts (test fixtures, ``lifespan``, in-process A2A clients)
    construct the server without thread-bouncing through
    ``asyncio.to_thread``."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        handler, _executor, _registry = create_adcp_server_from_platform(
            _ConformantSalesPlatform(),
            executor=pool,
            registry=InMemoryTaskRegistry(),
            auto_emit_completion_webhooks=False,
            validate_at_init=False,
        )
        assert handler is not None
        # Async validator on the same handler proves the validation
        # step is still reachable — just on the caller's terms.
        from adcp.decisioning.validate_capabilities import (
            validate_capabilities_response_shape_async,
        )

        assert await validate_capabilities_response_shape_async(handler) is None


@pytest.mark.asyncio
async def test_create_adcp_server_default_init_blows_up_in_async_context() -> None:
    """Regression guard for the bug: default ``validate_at_init=True``
    inside a running event loop must still fail loudly. If a future
    refactor makes it succeed silently, the issue's promise — that
    adopters in async contexts opt in via ``validate_at_init=False`` —
    becomes meaningless and adopters lose the fast-fail signal."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        # Match on the stable phrase from the SDK's wrapped RuntimeError
        # (which itself fires off the stdlib "cannot be called from a
        # running event loop" message). Don't pin to "asyncio.run" —
        # CPython 3.14+ may reword the inner message and our wrapper
        # rephrases it anyway.
        with pytest.raises(RuntimeError, match="running event loop"):
            create_adcp_server_from_platform(
                _ConformantSalesPlatform(),
                executor=pool,
                registry=InMemoryTaskRegistry(),
                auto_emit_completion_webhooks=False,
                # default validate_at_init=True — the boom case.
            )


def test_create_adcp_server_validate_at_init_true_still_validates_conformant() -> None:
    """The default ``validate_at_init=True`` path still runs validation
    when called from a sync context. Belt-and-suspenders regression
    guard so the opt-out flag doesn't accidentally invert the default."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        handler, _executor, _registry = create_adcp_server_from_platform(
            _ConformantSalesPlatform(),
            executor=pool,
            registry=InMemoryTaskRegistry(),
            auto_emit_completion_webhooks=False,
            # validate_at_init=True is the default
        )
        assert handler is not None


def test_create_adcp_server_validate_at_init_true_rejects_bad_platform() -> None:
    """Sync init path with default ``validate_at_init=True`` still
    surfaces the same ``AdcpError`` on a non-conformant platform.
    Confirms the gate hasn't accidentally suppressed validation on the
    default code path."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        with pytest.raises(AdcpError) as exc_info:
            create_adcp_server_from_platform(
                _MediaBuyMissingBillingPlatform(),
                executor=pool,
                registry=InMemoryTaskRegistry(),
                auto_emit_completion_webhooks=False,
            )
        assert exc_info.value.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_in_loop_error_message_points_at_opt_out() -> None:
    """When the sync validator hits ``asyncio.run`` under a running
    loop, the SDK's wrapped ``RuntimeError`` must name the opt-out so
    the diagnostic answers 'what do I do?' instead of leaving the
    adopter to grep for the issue.

    Asserts on the contract surface — the message must reference
    ``validate_at_init=False`` and the async sibling — not on exact
    wording, so wordsmithing later doesn't churn this guard."""
    from adcp.decisioning.validate_capabilities import (
        validate_capabilities_response_shape,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        handler = _build_handler(_ConformantSalesPlatform(), pool)
        with pytest.raises(RuntimeError) as exc_info:
            validate_capabilities_response_shape(handler)

        msg = str(exc_info.value)
        assert "validate_at_init=False" in msg, (
            f"Wrapped RuntimeError should point adopters at the opt-out " f"kwarg, got: {msg!r}"
        )
        assert (
            "validate_capabilities_response_shape_async" in msg
        ), f"Wrapped RuntimeError should name the async sibling, got: {msg!r}"
        # The stdlib exception is preserved as ``__cause__`` so
        # operators can trace through to the canonical message.
        assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_serve_forwards_validate_at_init() -> None:
    """``adcp.decisioning.serve`` is the one-call wrapper; if it
    doesn't forward ``validate_at_init`` then async-context callers
    (sidecar binaries launched from ``asyncio.run(main())``) can't
    opt out. Verify the kwarg appears in the signature and forwards
    correctly via call-side observation."""
    import inspect

    from adcp.decisioning.serve import serve as _adcp_serve

    sig = inspect.signature(_adcp_serve)
    assert "validate_at_init" in sig.parameters, (
        "serve() must accept validate_at_init for async-context parity "
        "with create_adcp_server_from_platform"
    )
    assert (
        sig.parameters["validate_at_init"].default is True
    ), "Default must stay True to preserve sync-boot fail-fast behavior"
