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

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import SPECIALISM_TO_PROTOCOLS, PlatformHandler
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
        channels=["display", "ctv"],
        pricing_models=["cpm"],
        supported_billing=["operator", "agent"],
    )
    accounts = SingletonAccounts(account_id="test")


class _SignalsPlatform(DecisioningPlatform):
    """Minimal signal-marketplace platform — no media_buy claim."""

    capabilities = DecisioningCapabilities(
        specialisms=["signal-marketplace"],
        supported_billing=["agent"],
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


def test_bare_platform_falls_through_to_media_buy(executor: ThreadPoolExecutor) -> None:
    """A platform with no specialisms still produces a spec-valid
    response — fall through to ``media_buy`` so the response stays
    minItems-1 valid on ``supported_protocols``."""
    handler = _build_handler(_BarePlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["supported_protocols"] == ["media_buy"]
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
