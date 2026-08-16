"""Tests for the idempotency-wiring boot-time validator.

Catches the silent-lie configuration: a platform that advertises
``capabilities.adcp.idempotency.supported=True`` while never applying
``@IdempotencyStore.wrap`` to any handler method.
"""

from __future__ import annotations

import pytest

from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform, SingletonAccounts
from adcp.decisioning.capabilities import (
    Adcp,
    IdempotencySupported,
    IdempotencyUnsupported,
)
from adcp.decisioning.types import AdcpError
from adcp.decisioning.validate_idempotency import (
    idempotency_capability_supported,
    validate_idempotency_wiring,
)
from adcp.server.idempotency import IdempotencyStore, MemoryBackend, is_wrapped


def _store() -> IdempotencyStore:
    return IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)


def _caps_with_idempotency(*, supported: bool) -> DecisioningCapabilities:
    if supported:
        idempotency = IdempotencySupported(supported=True, replay_ttl_seconds=86400)
    else:
        idempotency = IdempotencyUnsupported(supported=False)
    return DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        supported_billing=("operator",),
        adcp=Adcp(major_versions=[3], idempotency=idempotency),
    )


# ---------------------------------------------------------------------------
# is_wrapped helper
# ---------------------------------------------------------------------------


class TestIsWrapped:
    def test_unwrapped_function_returns_false(self) -> None:
        async def plain_handler(self, params, context=None):
            return {}

        assert is_wrapped(plain_handler) is False

    def test_wrapped_function_returns_true(self) -> None:
        store = _store()

        @store.wrap
        async def handler(self, params, context=None):
            return {}

        assert is_wrapped(handler) is True

    def test_none_returns_false(self) -> None:
        assert is_wrapped(None) is False

    def test_attribute_spoofing_does_not_register(self) -> None:
        """Setting a sentinel attr on a plain function must NOT
        register it — defense-in-depth against the previous-design
        attr-based sentinel."""

        async def plain_handler(self, params, context=None):
            return {}

        plain_handler.__adcp_idempotency_wrapped__ = True  # type: ignore[attr-defined]
        assert is_wrapped(plain_handler) is False

    def test_bound_method_resolves(self) -> None:
        """A wrapped function used as a bound method still returns
        True — ``is_wrapped`` resolves ``__func__``."""
        store = _store()

        @store.wrap
        async def handler(self, params, context=None):
            return {}

        class Holder:
            create_media_buy = handler

        instance = Holder()
        assert is_wrapped(instance.create_media_buy) is True


# ---------------------------------------------------------------------------
# idempotency_capability_supported
# ---------------------------------------------------------------------------


class TestIdempotencyCapabilitySupported:
    def test_no_capabilities_attr_returns_false(self) -> None:
        class P:
            pass

        assert idempotency_capability_supported(P()) is False

    def test_capabilities_none_returns_false(self) -> None:
        class P:
            capabilities = None

        assert idempotency_capability_supported(P()) is False

    def test_no_adcp_arm_returns_false(self) -> None:
        class P:
            capabilities = DecisioningCapabilities(
                specialisms=["sales-non-guaranteed"],
                supported_billing=("operator",),
            )

        assert idempotency_capability_supported(P()) is False

    def test_unsupported_arm_returns_false(self) -> None:
        class P:
            capabilities = _caps_with_idempotency(supported=False)

        assert idempotency_capability_supported(P()) is False

    def test_supported_arm_returns_true(self) -> None:
        class P:
            capabilities = _caps_with_idempotency(supported=True)

        assert idempotency_capability_supported(P()) is True


# ---------------------------------------------------------------------------
# validate_idempotency_wiring — direct unit tests
# ---------------------------------------------------------------------------


class _MinimalPlatform:
    """Bare minimum platform shape — no DecisioningPlatform inheritance
    so unrelated boot validation doesn't run. The validator only needs
    ``capabilities`` and method introspection."""

    def __init__(self, capabilities, methods=None):
        self.capabilities = capabilities
        for name, fn in (methods or {}).items():
            setattr(self, name, fn)


async def _stub_create_media_buy(self, params, context=None):
    return {"media_buy_id": "x"}


async def _stub_update_media_buy(self, mid, p, context=None):
    return {"media_buy_id": mid}


class TestValidateIdempotencyWiring:
    def test_no_capability_passes(self) -> None:
        platform = _MinimalPlatform(
            capabilities=DecisioningCapabilities(
                specialisms=["sales-non-guaranteed"],
                supported_billing=("operator",),
            ),
        )
        validate_idempotency_wiring(platform)

    def test_unsupported_arm_passes(self) -> None:
        platform = _MinimalPlatform(
            capabilities=_caps_with_idempotency(supported=False),
        )
        validate_idempotency_wiring(platform)

    def test_supported_with_wrap_passes(self) -> None:
        store = _store()

        @store.wrap
        async def create_media_buy(self, params, context=None):
            return {"media_buy_id": "x"}

        platform = _MinimalPlatform(
            capabilities=_caps_with_idempotency(supported=True),
            methods={"create_media_buy": create_media_buy},
        )
        validate_idempotency_wiring(platform)

    def test_supported_with_async_but_unwrapped_methods_raises(self) -> None:
        """Real async handlers that lack the wrap registration → raise.
        Distinct from earlier lambda-based test which couldn't exercise
        the wrapped-vs-unwrapped discriminator."""
        platform = _MinimalPlatform(
            capabilities=_caps_with_idempotency(supported=True),
            methods={
                "create_media_buy": _stub_create_media_buy,
                "update_media_buy": _stub_update_media_buy,
            },
        )
        with pytest.raises(AdcpError) as exc_info:
            validate_idempotency_wiring(platform)

        err = exc_info.value
        assert err.recovery == "terminal"
        assert "@IdempotencyStore.wrap" in str(err)
        assert err.details["missing"] == "@IdempotencyStore.wrap"
        assert err.details["decorator_import"].startswith("from adcp.server.idempotency")
        assert "create_media_buy" in err.details["candidate_methods"]
        assert "update_media_buy" in err.details["candidate_methods"]
        assert err.details["external_opt_out"] == "_adcp_idempotency_external = True"

    def test_external_opt_out_passes_without_wrap(self) -> None:
        """``_adcp_idempotency_external = True`` on the platform class
        opts out of the wrap-coverage check. For adopters with
        gateway-tier dedup or a BYO decorator the SDK can't introspect."""

        class P:
            capabilities = _caps_with_idempotency(supported=True)
            _adcp_idempotency_external = True

            async def create_media_buy(self, params, context=None):
                return {}

        validate_idempotency_wiring(P())

    def test_property_with_boot_side_effect_does_not_blow_up(self) -> None:
        """Platforms with a ``@property`` that raises (e.g., DB lookup
        before connection is open) must not trip the validator's method
        scan. ``inspect.getmembers`` would fire every descriptor; the
        validator uses ``dir`` + try/except per name."""

        class P:
            capabilities = _caps_with_idempotency(supported=True)

            @property
            def db_session(self):
                raise RuntimeError("DB not connected at boot")

        # The platform has no wrapped method → expect the validator to
        # raise the wiring AdcpError (not the property's RuntimeError).
        with pytest.raises(AdcpError) as exc_info:
            validate_idempotency_wiring(P())
        assert exc_info.value.recovery == "terminal"

    def test_dynamic_bind_in_init(self) -> None:
        """Adopters may bind a wrapped function in ``__init__``. The
        introspection walks the instance, so this still passes."""
        store = _store()

        @store.wrap
        async def handler(self, params, context=None):
            return {}

        class P:
            capabilities = _caps_with_idempotency(supported=True)

            def __init__(self):
                self.create_media_buy = handler

        validate_idempotency_wiring(P())


# ---------------------------------------------------------------------------
# Boot-time integration: validator runs from create_adcp_server_from_platform
# ---------------------------------------------------------------------------


def _sales_methods():
    """Stubs for the SalesPlatform required surface — none wrapped."""

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "x", "status": "active"}

    def update_media_buy(self, mid, p, ctx):
        return {"media_buy_id": mid, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}

    def get_media_buys(self, req, ctx):
        return {"media_buys": []}

    def list_creative_formats_legacy(self, req, ctx):
        return {"creative_formats": []}

    def list_creatives(self, req, ctx):
        return {"creatives": []}

    def provide_performance_feedback(self, req, ctx):
        return {"acknowledged": True}

    return {
        "get_products": get_products,
        "create_media_buy": create_media_buy,
        "update_media_buy": update_media_buy,
        "sync_creatives": sync_creatives,
        "get_media_buy_delivery": get_media_buy_delivery,
        "get_media_buys": get_media_buys,
        "list_creative_formats": list_creative_formats_legacy,
        "list_creatives": list_creatives,
        "provide_performance_feedback": provide_performance_feedback,
    }


def test_boot_time_raises_when_idempotency_advertised_without_wrap() -> None:
    """End-to-end: ``create_adcp_server_from_platform`` invokes the
    validator and the boot fails with a structured AdcpError."""
    from adcp.testing import build_asgi_app

    methods = _sales_methods()

    class LyingPlatform(DecisioningPlatform):
        capabilities = _caps_with_idempotency(supported=True)
        accounts = SingletonAccounts(account_id="t")

    for name, fn in methods.items():
        setattr(LyingPlatform, name, fn)

    with pytest.raises(AdcpError) as exc_info:
        build_asgi_app(LyingPlatform())

    err = exc_info.value
    assert err.recovery == "terminal"
    assert "@IdempotencyStore.wrap" in str(err)


def test_boot_time_passes_when_idempotency_advertised_and_wrapped() -> None:
    """The same shape passes once at least one method is wrapped."""
    from adcp.testing import build_asgi_app

    store = _store()
    methods = _sales_methods()

    @store.wrap
    async def create_media_buy_wrapped(self, params, context=None):
        return {"media_buy_id": "x", "status": "active"}

    methods["create_media_buy"] = create_media_buy_wrapped

    class HonestPlatform(DecisioningPlatform):
        capabilities = _caps_with_idempotency(supported=True)
        accounts = SingletonAccounts(account_id="t")

    for name, fn in methods.items():
        setattr(HonestPlatform, name, fn)

    app = build_asgi_app(HonestPlatform())
    assert app is not None


def test_boot_time_passes_with_external_opt_out() -> None:
    """End-to-end: external opt-out lets a platform advertise
    idempotency support without the SDK seeing any wrapped method."""
    from adcp.testing import build_asgi_app

    methods = _sales_methods()

    class GatewayDedupPlatform(DecisioningPlatform):
        capabilities = _caps_with_idempotency(supported=True)
        accounts = SingletonAccounts(account_id="t")
        _adcp_idempotency_external = True

    for name, fn in methods.items():
        setattr(GatewayDedupPlatform, name, fn)

    app = build_asgi_app(GatewayDedupPlatform())
    assert app is not None
