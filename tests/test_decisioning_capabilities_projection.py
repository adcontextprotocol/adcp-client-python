"""Capabilities projection — :meth:`PlatformHandler.get_adcp_capabilities`.

Validates that the framework auto-projects
:class:`DecisioningCapabilities` into a spec-conformant
``get_adcp_capabilities`` response. Pre-fix the v3 reference seller
inherited the base ADCPHandler's ``not_supported`` stub on this
discovery tool — buyers got ``NOT_SUPPORTED`` from the most-fundamental
handshake call, and ``account.supported_billing`` (required by spec
when ``media_buy`` is in ``supported_protocols``) wasn't surfaced at
all.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from adcp._version import get_supported_adcp_versions
from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import (
    Account,
    Adcp,
    IdempotencyUnsupported,
    MediaBuy,
    SupportedProtocol,
)
from adcp.decisioning.handler import SPECIALISM_TO_PROTOCOLS, PlatformHandler
from adcp.server.base import ToolContext
from adcp.validation.schema_validator import validate_response


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-caps-")
    yield pool
    pool.shutdown(wait=True)


def _build_handler(platform: DecisioningPlatform, executor: ThreadPoolExecutor) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )


class _SalesPlatform(DecisioningPlatform):
    """Minimal sales-non-guaranteed platform for projection tests."""

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        supported_protocols=[SupportedProtocol.media_buy],
        account=Account(supported_billing=["operator", "agent"]),
        media_buy=MediaBuy(supported_pricing_models=["cpm"]),
    )
    accounts = SingletonAccounts(account_id="test")


class _SignalsPlatform(DecisioningPlatform):
    """Minimal signal-marketplace platform — no media_buy claim."""

    capabilities = DecisioningCapabilities(
        specialisms=["signal-marketplace"],
        supported_protocols=[SupportedProtocol.signals],
        account=Account(supported_billing=["agent"]),
    )
    accounts = SingletonAccounts(account_id="test")


class _OwnedSignalsPlatform(DecisioningPlatform):
    """Minimal signal-owned platform — discovery-only signals claim."""

    capabilities = DecisioningCapabilities(
        specialisms=["signal-owned"],
        supported_protocols=[SupportedProtocol.signals],
        account=Account(supported_billing=["agent"]),
    )
    accounts = SingletonAccounts(account_id="test")


class _BarePlatform(DecisioningPlatform):
    """Platform with no specialisms claimed at all (pure meta)."""

    capabilities = DecisioningCapabilities()
    accounts = SingletonAccounts(account_id="test")


def test_specialism_to_protocols_covers_every_non_meta_slug() -> None:
    """Every non-meta specialism in
    :data:`SPECIALISM_TO_PROTOCOLS` resolves to a wire-protocol value
    inside the spec's ``supported_protocols`` enum.
    """
    spec_protocols = {
        "media_buy",
        "signals",
        "governance",
        "sponsored_intelligence",
        "creative",
        "brand",
    }
    for slug, protocols in SPECIALISM_TO_PROTOCOLS.items():
        assert protocols, f"{slug} mapped to empty protocol set"
        for p in protocols:
            assert p in spec_protocols, f"{slug} → {p!r} not in spec supported_protocols enum"


def test_sales_platform_projects_account_supported_billing(executor: ThreadPoolExecutor) -> None:
    """Spec invariant: ``account.supported_billing`` is required when
    ``media_buy`` is in ``supported_protocols`` (per
    ``protocol/get-adcp-capabilities-response.json``, lines 129-131).
    """
    handler = _build_handler(_SalesPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["supported_protocols"] == ["media_buy"]
    assert response["account"]["supported_billing"] == ["operator", "agent"]


def test_sales_platform_projects_pricing_models(executor: ThreadPoolExecutor) -> None:
    handler = _build_handler(_SalesPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["media_buy"]["supported_pricing_models"] == ["cpm"]


def test_sales_platform_response_is_spec_conformant(executor: ThreadPoolExecutor) -> None:
    """End-to-end: the projected response validates against the
    bundled ``get_adcp_capabilities`` JSON schema. Catches the original
    bug — ``account.supported_billing`` missing — at the wire level.
    """
    handler = _build_handler(_SalesPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    outcome = validate_response("get_adcp_capabilities", response)
    assert outcome.valid, f"validation failed: {outcome.issues}"


def test_signals_only_platform_emits_signals_protocol(executor: ThreadPoolExecutor) -> None:
    """A platform claiming only ``signal-marketplace`` projects to
    ``supported_protocols=['signals']`` — no ``media_buy`` block."""
    handler = _build_handler(_SignalsPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["supported_protocols"] == ["signals"]
    assert "media_buy" not in response
    # account block still present — supported_billing was declared.
    assert response["account"]["supported_billing"] == ["agent"]


def test_owned_signals_only_platform_emits_signals_protocol(
    executor: ThreadPoolExecutor,
) -> None:
    """A platform claiming only ``signal-owned`` projects to the
    signals protocol even though the method surface is discovery-only."""
    handler = _build_handler(_OwnedSignalsPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["supported_protocols"] == ["signals"]
    assert "media_buy" not in response
    assert response["account"]["supported_billing"] == ["agent"]


def test_bare_platform_emits_empty_supported_protocols(executor: ThreadPoolExecutor) -> None:
    """A platform with no specialisms emits an empty
    ``supported_protocols`` list — the projection refuses to silently
    default to ``["media_buy"]`` because that lies about a storyboard
    commitment the adopter never made.

    The boot-time validator
    (``validate_capabilities_response_shape``) catches the empty list
    and raises ``INVALID_REQUEST`` with a structured error pointing the
    operator at the configuration site. Adopters who claim a protocol
    without an enumerated specialism set ``supported_protocols``
    explicitly via ``DecisioningCapabilities(supported_protocols=[...])``.
    """
    handler = _build_handler(_BarePlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    # Empty list — no protocol claimed.
    assert response["supported_protocols"] == []
    # No supported_billing declared → no account block.
    assert "account" not in response


def test_idempotency_defaults_to_unsupported(executor: ThreadPoolExecutor) -> None:
    """Spec requires ``adcp.idempotency``. The base shim defaults to
    ``{supported: false}`` so adopters who haven't wired an
    IdempotencyStore still ship a valid capabilities response.
    """
    handler = _build_handler(_SalesPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["adcp"]["idempotency"] == {"supported": False}


def test_custom_adcp_block_preserves_supported_versions(executor: ThreadPoolExecutor) -> None:
    class _CustomAdcpSalesPlatform(_SalesPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            supported_protocols=[SupportedProtocol.media_buy],
            adcp=Adcp(
                major_versions=[3],
                idempotency=IdempotencyUnsupported(supported=False),
            ),
            account=Account(supported_billing=["operator", "agent"]),
            media_buy=MediaBuy(supported_pricing_models=["cpm"]),
        )

    handler = _build_handler(_CustomAdcpSalesPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["adcp"]["supported_versions"] == list(get_supported_adcp_versions())


def test_mixed_major_custom_adcp_block_does_not_invent_exact_versions(
    executor: ThreadPoolExecutor,
) -> None:
    class _MixedMajorSalesPlatform(_SalesPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            supported_protocols=[SupportedProtocol.media_buy],
            adcp=Adcp(
                major_versions=[2, 3],
                idempotency=IdempotencyUnsupported(supported=False),
            ),
            account=Account(supported_billing=["operator", "agent"]),
            media_buy=MediaBuy(supported_pricing_models=["cpm"]),
        )

    handler = _build_handler(_MixedMajorSalesPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["adcp"]["major_versions"] == [2, 3]
    assert "supported_versions" not in response["adcp"]


def test_capabilities_extra_hook_deep_merges_dynamic_portfolio(
    executor: ThreadPoolExecutor,
) -> None:
    class _DynamicPortfolioPlatform(_SalesPlatform):
        async def get_adcp_capabilities_extra(self, context: ToolContext) -> dict[str, object]:
            assert context.tenant_id == "tenant-a"
            return {
                "media_buy": {
                    "portfolio": {
                        "publisher_domains": ["example.com"],
                        "primary_channels": ["display"],
                    }
                }
            }

    handler = _build_handler(_DynamicPortfolioPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="tenant-a")))

    assert response["media_buy"]["supported_pricing_models"] == ["cpm"]
    assert response["media_buy"]["portfolio"]["publisher_domains"] == ["example.com"]


def test_capabilities_extra_hook_exception_fails_closed(
    executor: ThreadPoolExecutor,
) -> None:
    class _BrokenExtraPlatform(_SalesPlatform):
        async def get_adcp_capabilities_extra(self, context: ToolContext) -> dict[str, object]:
            raise RuntimeError("tenant lookup failed")

    handler = _build_handler(_BrokenExtraPlatform(), executor)

    with pytest.raises(AdcpError) as exc_info:
        asyncio.run(handler.get_adcp_capabilities())

    assert exc_info.value.code == "SERVICE_UNAVAILABLE"
    assert exc_info.value.recovery == "transient"


def test_capabilities_extra_hook_cannot_override_framework_keys(
    executor: ThreadPoolExecutor,
) -> None:
    class _ProtectedKeyPlatform(_SalesPlatform):
        def get_adcp_capabilities_extra(self, context: ToolContext) -> dict[str, object]:
            return {"status": "draft", "supported_protocols": ["signals"]}

    handler = _build_handler(_ProtectedKeyPlatform(), executor)

    with pytest.raises(AdcpError) as exc_info:
        asyncio.run(handler.get_adcp_capabilities())

    assert exc_info.value.code == "CONFIGURATION_ERROR"
    assert exc_info.value.details["protected_keys"] == ["status", "supported_protocols"]
