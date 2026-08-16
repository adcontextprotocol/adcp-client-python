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
from dataclasses import replace

import pytest

from adcp._version import get_supported_adcp_versions
from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import (
    Account,
    Adcp,
    Execution,
    GeoPostalAreas,
    IdempotencySupported,
    IdempotencyUnsupported,
    Measurement,
    MediaBuy,
    Metric,
    Portfolio,
    SupportedProtocol,
    Targeting,
    WebhookSigning,
)
from adcp.decisioning.handler import SPECIALISM_TO_PROTOCOLS, PlatformHandler
from adcp.decisioning.types import AdcpError
from adcp.server.base import ToolContext
from adcp.types import project_geo_postal_areas
from adcp.validation.schema_validator import validate_response


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-caps-")
    yield pool
    pool.shutdown(wait=True)


class _WebhookSenderAuth:
    alg = "ed25519"


class _Rfc9421WebhookSender:
    signs_with_rfc9421 = True
    _auth = _WebhookSenderAuth()


def _build_handler(
    platform: DecisioningPlatform,
    executor: ThreadPoolExecutor,
    *,
    webhook_sender=None,
) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=webhook_sender,
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
        "measurement",
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


@pytest.mark.parametrize(
    ("version", "expected"),
    [("3.0", None), ("3.1", True)],
)
def test_framework_projects_canonical_creatives_for_negotiated_release(
    executor: ThreadPoolExecutor,
    version: str,
    expected: bool | None,
) -> None:
    handler = _build_handler(_SalesPlatform(), executor)
    context = ToolContext(resolved_adcp_version=version)

    response = asyncio.run(handler.get_adcp_capabilities(context=context))
    features = response["media_buy"].get("features", {})

    assert features.get("canonical_creatives") is expected


def _postal_platform(geo_postal_areas: GeoPostalAreas) -> DecisioningPlatform:
    class _PostalPlatform(DecisioningPlatform):
        accounts = SingletonAccounts(account_id="test")

    _PostalPlatform.capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        supported_protocols=[SupportedProtocol.media_buy],
        account=Account(supported_billing=["operator", "agent"]),
        media_buy=MediaBuy(
            supported_pricing_models=["cpm"],
            execution=Execution(
                targeting=Targeting(geo_postal_areas=geo_postal_areas),
            ),
        ),
    )
    return _PostalPlatform()


def _projected_geo_postal_areas(
    executor: ThreadPoolExecutor,
    geo_postal_areas: GeoPostalAreas,
    *,
    version: str | None,
) -> dict:
    handler = _build_handler(_postal_platform(geo_postal_areas), executor)
    context = ToolContext(resolved_adcp_version=version)
    response = asyncio.run(handler.get_adcp_capabilities(context=context))
    return response["media_buy"]["execution"]["targeting"]["geo_postal_areas"]


def test_native_postal_capabilities_remain_native_for_31_callers(
    executor: ThreadPoolExecutor,
) -> None:
    projected = _projected_geo_postal_areas(
        executor,
        GeoPostalAreas(US=["zip", "zip_plus_four"], BR=["cep"]),
        version="3.1",
    )

    assert projected == {"US": ["zip", "zip_plus_four"], "BR": ["cep"]}


def test_public_postal_projection_preserves_future_native_country_keys() -> None:
    projected = project_geo_postal_areas({"NL": ["postal_code"], "US": ["zip"]}, "3.1")

    assert projected == {"NL": ["postal_code"], "US": ["zip"]}


def test_native_postal_capabilities_project_to_legacy_for_30_callers(
    executor: ThreadPoolExecutor,
) -> None:
    projected = _projected_geo_postal_areas(
        executor,
        GeoPostalAreas(US=["zip", "zip_plus_four"], BR=["cep"]),
        version="3.0",
    )

    assert projected == {"us_zip": True, "us_zip_plus_four": True}


def test_native_postal_capabilities_without_legacy_alias_are_omitted_for_30_callers(
    executor: ThreadPoolExecutor,
) -> None:
    handler = _build_handler(_postal_platform(GeoPostalAreas(BR=["cep"])), executor)

    response = asyncio.run(
        handler.get_adcp_capabilities(context=ToolContext(resolved_adcp_version="3.0"))
    )

    assert "execution" not in response["media_buy"]


def test_postal_capabilities_30_projection_is_schema_valid(
    executor: ThreadPoolExecutor,
) -> None:
    handler = _build_handler(_postal_platform(GeoPostalAreas(US=["zip"])), executor)

    response = asyncio.run(
        handler.get_adcp_capabilities(context=ToolContext(resolved_adcp_version="3.0"))
    )

    outcome = validate_response("get_adcp_capabilities", response, version="3.0")
    assert outcome.valid, f"validation failed: {outcome.issues}"


def test_postal_capabilities_native_projection_is_schema_valid(
    executor: ThreadPoolExecutor,
) -> None:
    handler = _build_handler(_postal_platform(GeoPostalAreas(US=["zip"])), executor)

    response = asyncio.run(
        handler.get_adcp_capabilities(context=ToolContext(resolved_adcp_version="3.1"))
    )

    outcome = validate_response("get_adcp_capabilities", response)
    assert outcome.valid, f"validation failed: {outcome.issues}"


def test_legacy_postal_capabilities_remain_legacy_for_30_callers(
    executor: ThreadPoolExecutor,
) -> None:
    projected = _projected_geo_postal_areas(
        executor,
        GeoPostalAreas(us_zip=True, us_zip_plus_four=True),
        version="3.0",
    )

    assert projected == {"us_zip": True, "us_zip_plus_four": True}


def test_legacy_postal_capabilities_project_to_native_for_31_callers(
    executor: ThreadPoolExecutor,
) -> None:
    projected = _projected_geo_postal_areas(
        executor,
        GeoPostalAreas(us_zip=True, us_zip_plus_four=True),
        version="3.1",
    )

    assert projected == {"US": ["zip", "zip_plus_four"]}


def test_unversioned_postal_capabilities_fall_back_to_30_projection(
    executor: ThreadPoolExecutor,
) -> None:
    handler = _build_handler(_postal_platform(GeoPostalAreas(US=["zip"])), executor)

    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["media_buy"]["execution"]["targeting"]["geo_postal_areas"] == {"us_zip": True}


def test_absent_postal_capabilities_stay_absent_for_all_versions(
    executor: ThreadPoolExecutor,
) -> None:
    handler = _build_handler(_SalesPlatform(), executor)

    unversioned = asyncio.run(handler.get_adcp_capabilities())
    native = asyncio.run(
        handler.get_adcp_capabilities(context=ToolContext(resolved_adcp_version="3.1"))
    )

    assert "execution" not in unversioned["media_buy"]
    assert "execution" not in native["media_buy"]


def test_sales_platform_response_is_spec_conformant(executor: ThreadPoolExecutor) -> None:
    """End-to-end: the projected response validates against the
    bundled ``get_adcp_capabilities`` JSON schema. Catches the original
    bug — ``account.supported_billing`` missing — at the wire level.
    """
    handler = _build_handler(_SalesPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    outcome = validate_response("get_adcp_capabilities", response)
    assert outcome.valid, f"validation failed: {outcome.issues}"


def test_measurement_platform_projects_metric_catalog(executor: ThreadPoolExecutor) -> None:
    class _MeasurementPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            supported_protocols=[SupportedProtocol.measurement],
            measurement=Measurement(metrics=[Metric(metric_id="attention_units")]),
            experimental_features=["measurement.core"],
        )
        accounts = SingletonAccounts(account_id="test")

    handler = _build_handler(_MeasurementPlatform(), executor)
    response = asyncio.run(handler.get_adcp_capabilities())

    assert response["supported_protocols"] == ["measurement"]
    assert response["measurement"]["metrics"][0]["metric_id"] == "attention_units"
    assert response["experimental_features"] == ["measurement.core"]

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


def test_request_scoped_capabilities_hook_projects_tenant_blocks(
    executor: ThreadPoolExecutor,
) -> None:
    class _TenantCapabilitiesPlatform(_SalesPlatform):
        def get_adcp_capabilities_for_request(self, params=None, context=None):
            del params
            if context is None or context.tenant_id is None:
                return None

            base = self.capabilities
            assert base.media_buy is not None
            media_buy = base.media_buy.model_copy(
                update={
                    "portfolio": Portfolio(
                        publisher_domains=[f"{context.tenant_id}.example"],
                    )
                }
            )
            webhook_signing = (
                WebhookSigning(
                    supported=True,
                    profile="adcp/webhook-signing/v1",
                    algorithms=["ed25519"],
                )
                if context.tenant_id == "signed"
                else None
            )
            return replace(
                base,
                media_buy=media_buy,
                webhook_signing=webhook_signing,
            )

    handler = _build_handler(
        _TenantCapabilitiesPlatform(),
        executor,
        webhook_sender=_Rfc9421WebhookSender(),
    )

    unsigned = asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="tenant-a")))
    signed = asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="signed")))
    default = asyncio.run(handler.get_adcp_capabilities())

    assert unsigned["media_buy"]["portfolio"]["publisher_domains"] == ["tenant-a.example"]
    assert "webhook_signing" not in unsigned
    assert signed["media_buy"]["portfolio"]["publisher_domains"] == ["signed.example"]
    assert signed["webhook_signing"]["supported"] is True
    assert signed["webhook_signing"]["algorithms"] == ["ed25519"]
    assert "portfolio" not in default["media_buy"]


def test_request_scoped_webhook_signing_reuses_sender_invariant(
    executor: ThreadPoolExecutor,
) -> None:
    class _TenantCapabilitiesPlatform(_SalesPlatform):
        def get_adcp_capabilities_for_request(self, params=None, context=None):
            del params, context
            return replace(
                self.capabilities,
                webhook_signing=WebhookSigning(
                    supported=True,
                    profile="adcp/webhook-signing/v1",
                    algorithms=["ed25519"],
                ),
            )

    handler = _build_handler(_TenantCapabilitiesPlatform(), executor)

    with pytest.raises(AdcpError) as exc_info:
        asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="signed")))

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["missing"] == "webhook_sender_with_rfc9421_key"


def test_request_scoped_webhook_signing_can_be_adopter_managed(
    executor: ThreadPoolExecutor,
) -> None:
    class _TenantCapabilitiesPlatform(_SalesPlatform):
        def get_adcp_capabilities_for_request(self, params=None, context=None):
            del params, context
            return replace(
                self.capabilities,
                webhook_signing=WebhookSigning(
                    supported=True,
                    profile="adcp/webhook-signing/v1",
                    algorithms=["ed25519"],
                    legacy_hmac_fallback=True,
                ),
                webhook_signing_managed_externally=True,
            )

    handler = _build_handler(_TenantCapabilitiesPlatform(), executor)

    response = asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="signed")))

    assert response["webhook_signing"] == {
        "supported": True,
        "profile": "adcp/webhook-signing/v1",
        "algorithms": ["ed25519"],
        "legacy_hmac_fallback": True,
    }


def test_request_scoped_capabilities_are_schema_validated(
    executor: ThreadPoolExecutor,
) -> None:
    class _InvalidTenantCapabilitiesPlatform(_SalesPlatform):
        def get_adcp_capabilities_for_request(self, params=None, context=None):
            del params, context
            return replace(
                self.capabilities,
                account=None,
            )

    handler = _build_handler(_InvalidTenantCapabilitiesPlatform(), executor)

    with pytest.raises(AdcpError) as exc_info:
        asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="tenant-a")))

    assert exc_info.value.code == "INVALID_REQUEST"
    assert "account" in str(exc_info.value) or "supported_billing" in str(exc_info.value)


def test_request_scoped_capabilities_cannot_change_supported_protocols(
    executor: ThreadPoolExecutor,
) -> None:
    class _ProtocolChangingPlatform(_SignalsPlatform):
        def get_adcp_capabilities_for_request(self, params=None, context=None):
            del params, context
            return replace(
                self.capabilities,
                supported_protocols=[SupportedProtocol.media_buy],
            )

    handler = _build_handler(_ProtocolChangingPlatform(), executor)

    with pytest.raises(AdcpError) as exc_info:
        asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="tenant-a")))

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["field"] == "supported_protocols"


def test_request_scoped_capabilities_cannot_change_specialisms(
    executor: ThreadPoolExecutor,
) -> None:
    class _SpecialismChangingPlatform(_SignalsPlatform):
        def get_adcp_capabilities_for_request(self, params=None, context=None):
            del params, context
            return replace(
                self.capabilities,
                specialisms=["sales-non-guaranteed"],
            )

    handler = _build_handler(_SpecialismChangingPlatform(), executor)

    with pytest.raises(AdcpError) as exc_info:
        asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="tenant-a")))

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["field"] == "specialisms"


def test_request_scoped_idempotency_reuses_wiring_invariant(
    executor: ThreadPoolExecutor,
) -> None:
    class _TenantCapabilitiesPlatform(_SalesPlatform):
        def get_adcp_capabilities_for_request(self, params=None, context=None):
            del params, context
            return replace(
                self.capabilities,
                adcp=Adcp(
                    major_versions=[3],
                    idempotency=IdempotencySupported(
                        supported=True,
                        replay_ttl_seconds=86400,
                    ),
                ),
            )

    handler = _build_handler(_TenantCapabilitiesPlatform(), executor)

    with pytest.raises(AdcpError) as exc_info:
        asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="tenant-a")))

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.details["missing"] == "@IdempotencyStore.wrap"


def test_request_scoped_capabilities_hook_exceptions_are_structured(
    executor: ThreadPoolExecutor,
) -> None:
    class _FailingTenantCapabilitiesPlatform(_SalesPlatform):
        def get_adcp_capabilities_for_request(self, params=None, context=None):
            del params, context
            raise RuntimeError("tenant lookup failed")

    handler = _build_handler(_FailingTenantCapabilitiesPlatform(), executor)

    with pytest.raises(AdcpError) as exc_info:
        asyncio.run(handler.get_adcp_capabilities(context=ToolContext(tenant_id="tenant-a")))

    assert exc_info.value.code == "INTERNAL_ERROR"
    assert exc_info.value.details["caused_by"]["type"] == "RuntimeError"
    assert exc_info.value.details["caused_by"] == {"type": "RuntimeError"}
    assert "tenant lookup failed" not in str(exc_info.value)
    assert "tenant lookup failed" not in str(exc_info.value.details)


def test_request_scoped_capabilities_hook_may_be_async(
    executor: ThreadPoolExecutor,
) -> None:
    class _AsyncTenantCapabilitiesPlatform(_SalesPlatform):
        async def get_adcp_capabilities_for_request(self, params=None, context=None):
            del params
            if context is None or context.tenant_id is None:
                return None

            base = self.capabilities
            assert base.media_buy is not None
            return replace(
                base,
                media_buy=base.media_buy.model_copy(
                    update={
                        "portfolio": Portfolio(
                            publisher_domains=[f"{context.tenant_id}.example"],
                        )
                    }
                ),
            )

    handler = _build_handler(_AsyncTenantCapabilitiesPlatform(), executor)

    response = asyncio.run(
        handler.get_adcp_capabilities(context=ToolContext(tenant_id="async-tenant"))
    )

    assert response["media_buy"]["portfolio"]["publisher_domains"] == ["async-tenant.example"]
