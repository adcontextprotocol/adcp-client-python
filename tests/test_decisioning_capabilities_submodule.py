"""Smoke + structural tests for the :mod:`adcp.decisioning.capabilities` submodule.

The submodule sits between the disambiguated forms in
:mod:`adcp.types.capabilities` (where ``Account`` / ``MediaBuy`` /
``Creative`` are aliased to ``Capabilities*`` to avoid colliding with
the unrelated wire types in :mod:`adcp.types`) and the adopter-facing
import path. Adopter code reads against the AdCP wire spec, so the
submodule re-aliases the disambiguated forms back to the wire-spec names.

These tests guard the alias mapping — if the disambiguation drifts (for
example, codegen renames a sub-model and the alias goes stale), the
import fails here before adopters hit it.
"""

from __future__ import annotations

import pytest


def test_wire_spec_names_resolve_in_submodule() -> None:
    """Adopter-facing names match the AdCP wire spec field types 1:1."""
    from adcp.decisioning.capabilities import (
        Account,
        Adcp,
        Creative,
        IdempotencySupported,
        IdempotencyUnsupported,
        MediaBuy,
        Targeting,
    )

    # Spot-check a few — full surface is covered by ``__all__``.
    assert Account.__name__ == "Account"
    assert MediaBuy.__name__ == "MediaBuy"
    assert Creative.__name__ == "Creative"
    assert Adcp.__name__ == "Adcp"
    assert Targeting.__name__ == "Targeting"
    assert IdempotencySupported.__name__ == "Idempotency"  # the supported variant
    assert IdempotencyUnsupported is not IdempotencySupported
    assert IdempotencyUnsupported.__name__.startswith("Idempotency")  # the unsupported variant


def test_preview_capability_models_construct_with_bundled_creative() -> None:
    """Preview helpers must be the exact bundled types Creative expects."""
    from adcp.decisioning.capabilities import (
        Creative,
        Preview,
        PreviewRenderingOrigin,
        PreviewRoute,
    )

    preview = Preview(
        routes=[
            PreviewRoute(
                capability_id="hosted-preview",
                rendering_origin=PreviewRenderingOrigin.platform_native,
            )
        ]
    )
    creative = Creative(preview=preview)

    assert creative.preview is preview
    assert creative.preview.routes[0].capability_id == "hosted-preview"


def test_capability_sub_models_construct() -> None:
    """Typical declarations produce well-formed Pydantic instances.

    Validates that the pieces an adopter would compose into a
    ``DecisioningCapabilities`` declaration work as Pydantic models —
    construction, field access, ``model_dump``.
    """
    from adcp.decisioning.capabilities import (
        Account,
        Execution,
        GeoMetros,
        IdempotencySupported,
        Specialism,
        Targeting,
    )

    idempotency = IdempotencySupported(supported=True, replay_ttl_seconds=86400)
    assert idempotency.replay_ttl_seconds == 86400

    geo_metros = GeoMetros(nielsen_dma=True, eurostat_nuts2=False)
    assert geo_metros.nielsen_dma is True

    targeting = Targeting(geo_countries=True, geo_metros=geo_metros)
    assert targeting.geo_countries is True
    assert targeting.geo_metros is not None
    assert targeting.geo_metros.nielsen_dma is True

    execution = Execution(targeting=targeting)
    dump = execution.model_dump(mode="json", exclude_none=True)
    assert dump == {
        "targeting": {
            "geo_countries": True,
            "geo_metros": {"nielsen_dma": True, "eurostat_nuts2": False},
        },
    }

    account = Account(supported_billing=["operator"])
    billing = [b.value if hasattr(b, "value") else b for b in account.supported_billing]
    assert billing == ["operator"]

    # Specialism is the wire enum; .value matches the AdCP slug form.
    assert Specialism.sales_non_guaranteed.value == "sales-non-guaranteed"


def test_wire_account_and_capabilities_account_are_distinct() -> None:
    """Guard against a future regression where the colliding names get conflated.

    The wire ``Account`` (from :mod:`adcp.types`) and the capabilities
    ``Account`` (from :mod:`adcp.decisioning.capabilities`) are different
    Pydantic classes describing different parts of AdCP. The alias
    plumbing in :mod:`adcp.types.capabilities` is the only thing keeping
    them apart in the public API; if it drifts, this fails.
    """
    from adcp.decisioning.capabilities import Account as CapabilitiesAccount
    from adcp.types import Account as WireAccount

    assert CapabilitiesAccount is not WireAccount
    assert CapabilitiesAccount.__module__.endswith("get_adcp_capabilities_response")


def test_idempotency_union_halves_round_trip_distinctly() -> None:
    """``IdempotencySupported`` and ``IdempotencyUnsupported`` are the two
    arms of the AdCP idempotency oneOf — adopters pick one at declaration
    time and the wire shape differs accordingly. The supported arm
    requires ``replay_ttl_seconds``; the unsupported arm forbids it.
    """
    from adcp.decisioning.capabilities import IdempotencySupported, IdempotencyUnsupported

    supported = IdempotencySupported(supported=True, replay_ttl_seconds=3600)
    assert supported.model_dump(mode="json")["supported"] is True
    assert supported.model_dump(mode="json")["replay_ttl_seconds"] == 3600

    unsupported = IdempotencyUnsupported(supported=False)
    dump = unsupported.model_dump(mode="json", exclude_none=True)
    assert dump == {"supported": False}
    # The schema's "not required: replay_ttl_seconds" invariant — the
    # unsupported arm should not even surface the field.
    assert "replay_ttl_seconds" not in dump

    # Wire validation: supported arm rejects missing replay_ttl_seconds.
    with pytest.raises(
        Exception
    ):  # noqa: PT011 — Pydantic ValidationError, broad to avoid coupling
        IdempotencySupported(supported=True)  # type: ignore[call-arg]


def test_submodule_all_matches_imports() -> None:
    """``__all__`` is the public contract — guard against drift from
    actual exports."""
    import adcp.decisioning.capabilities as caps

    for name in caps.__all__:
        assert hasattr(caps, name), f"__all__ lists {name!r} but it is not importable"


def test_legacy_field_warnings_fire_at_construction_not_projection() -> None:
    """Legacy-field DeprecationWarnings fire in
    ``DecisioningCapabilities.__post_init__`` so ``stacklevel=2`` lands
    on the adopter's declaration site (where the legacy field was set),
    not at the dispatcher that calls ``get_adcp_capabilities`` later.

    Construction-time emit means adopters see the migration message
    immediately when they instantiate the dataclass — not buried in a
    later transport layer. Multiple legacy fields on one declaration
    fire one warning each (Python's warnings registry deduplicates by
    ``(message, module, lineno)`` so the same line warns once per
    process).
    """
    import warnings

    from adcp.decisioning import DecisioningCapabilities

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            supported_billing=["operator"],
            pricing_models=["cpm"],
            channels=["display"],
        )

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    messages = " ".join(str(w.message) for w in deprecations)
    assert "supported_billing is deprecated" in messages
    assert "pricing_models is deprecated" in messages
    assert "channels is deprecated" in messages

    # Construction-time emit: the warning frame's filename is NOT the
    # handler module (where the projection runs). It's the test frame —
    # the adopter's declaration site.
    handler_filename_substr = "decisioning/handler.py"
    for w in deprecations:
        assert handler_filename_substr not in w.filename, (
            f"Deprecation fired from {w.filename} — should fire from adopter "
            "declaration, not handler projection."
        )


def test_auto_derive_supported_protocols_emits_warning_at_construction() -> None:
    """When ``supported_protocols`` is omitted and ``specialisms`` is set,
    ``DecisioningCapabilities`` auto-derives the wire field via
    ``SPECIALISM_TO_PROTOCOLS``. Per spec, ``supported_protocols`` is the
    primary storyboard-commitment declaration with specialisms as sub-claims;
    auto-derivation is ergonomic but inverts the spec's data direction.
    The dataclass emits a ``UserWarning`` at construction nudging adopters
    toward the explicit declaration form.

    The warning is NOT a deprecation — auto-derive stays supported. It's a
    one-shot nudge per declaration site (Python's warnings registry
    deduplicates by ``(message, module, lineno)``).
    """
    import warnings

    from adcp.decisioning import DecisioningCapabilities

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            # supported_protocols deliberately omitted — triggers auto-derive.
        )
    user_warnings = [
        w
        for w in caught
        if issubclass(w.category, UserWarning) and not issubclass(w.category, DeprecationWarning)
    ]
    messages = " ".join(str(w.message) for w in user_warnings)
    assert "auto-derive" in messages
    assert "supported_protocols" in messages


def test_explicit_supported_protocols_does_not_emit_auto_derive_warning() -> None:
    """When ``supported_protocols`` is set explicitly, no auto-derive
    warning fires — the adopter is on the spec-aligned path."""
    import warnings

    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import SupportedProtocol

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            supported_protocols=[SupportedProtocol.media_buy],
        )
    user_warnings = [
        w
        for w in caught
        if issubclass(w.category, UserWarning) and not issubclass(w.category, DeprecationWarning)
    ]
    messages = " ".join(str(w.message) for w in user_warnings)
    assert "auto-derive" not in messages


def test_compliance_testing_without_controller_warns_at_serve() -> None:
    """A platform that declares ``compliance_testing`` but doesn't wire
    a ``test_controller=`` to ``serve()`` advertises a capability it
    can't honor — buyers calling ``comply_test_controller`` will fail.
    The framework soft-warns at ``serve()`` time so the adopter sees
    the mismatch immediately, before the first buyer query.

    Soft-warn (not fail-fast) because adopters may legitimately stage
    the capability declaration ahead of the controller wiring (e.g.
    rolling out the change across two PRs).
    """
    import warnings

    from adcp.decisioning import (
        DecisioningCapabilities,
        DecisioningPlatform,
        SingletonAccounts,
    )
    from adcp.decisioning.capabilities import ComplianceTesting, SupportedProtocol
    from adcp.decisioning.serve import serve

    class _TestPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            supported_protocols=[SupportedProtocol.media_buy],
            supported_billing=["operator"],
            compliance_testing=ComplianceTesting(scenarios=["force_media_buy_status"]),
        )
        accounts = SingletonAccounts(account_id="test")

    # Stub out the actual MCP server boot so the test doesn't open a port.
    # We're checking the warning fires before _adcp_serve is invoked.
    import sys
    import unittest.mock as mock

    # ``adcp.server.serve`` is both a submodule and a re-exported function;
    # Python 3.10's import resolution differs from 3.11+. Reference the
    # module explicitly via ``sys.modules`` so the patch target lands on
    # the module's ``serve`` attribute regardless of Python version.
    server_serve_mod = sys.modules["adcp.server.serve"]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with mock.patch.object(server_serve_mod, "serve"):
            serve(_TestPlatform())

    user_warnings = [
        w
        for w in caught
        if issubclass(w.category, UserWarning) and not issubclass(w.category, DeprecationWarning)
    ]
    messages = " ".join(str(w.message) for w in user_warnings)
    assert "compliance_testing" in messages
    assert "test_controller" in messages


def test_compliance_testing_with_controller_does_not_warn() -> None:
    """When ``test_controller`` is wired alongside the
    ``compliance_testing`` declaration, the seller is consistent and no
    footgun warning fires."""
    import warnings

    from adcp.decisioning import (
        DecisioningCapabilities,
        DecisioningPlatform,
        SingletonAccounts,
    )
    from adcp.decisioning.capabilities import ComplianceTesting, SupportedProtocol
    from adcp.decisioning.serve import serve
    from adcp.server import TestControllerStore

    class _TestPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            supported_protocols=[SupportedProtocol.media_buy],
            supported_billing=["operator"],
            compliance_testing=ComplianceTesting(scenarios=["force_media_buy_status"]),
        )
        accounts = SingletonAccounts(account_id="test")

    import sys
    import unittest.mock as mock

    server_serve_mod = sys.modules["adcp.server.serve"]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with mock.patch.object(server_serve_mod, "serve"):
            serve(_TestPlatform(), test_controller=TestControllerStore())

    footgun = [
        w
        for w in caught
        if issubclass(w.category, UserWarning)
        and "compliance_testing" in str(w.message)
        and "test_controller" in str(w.message)
    ]
    assert not footgun, (
        f"Expected no compliance_testing footgun warning when controller is wired; "
        f"got: {[str(w.message) for w in footgun]}"
    )


def test_dual_transport_serve_registers_upstream_pool_shutdown() -> None:
    """The framework-owned lifespan drains platform upstream clients."""
    import sys
    import unittest.mock as mock

    from adcp.decisioning import (
        DecisioningCapabilities,
        DecisioningPlatform,
        SingletonAccounts,
    )
    from adcp.decisioning.capabilities import SupportedProtocol
    from adcp.decisioning.serve import serve

    class _TestPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            supported_protocols=[SupportedProtocol.media_buy],
            supported_billing=["operator"],
        )
        accounts = SingletonAccounts(account_id="test")

    platform = _TestPlatform()
    server_serve_mod = sys.modules["adcp.server.serve"]
    with mock.patch.object(server_serve_mod, "serve") as serve_mock:
        serve(platform, transport="both")

    shutdown_hooks = serve_mock.call_args.kwargs["on_shutdown"]
    assert len(shutdown_hooks) == 1
    assert shutdown_hooks[0].__self__ is platform
    assert shutdown_hooks[0].__func__ is platform.aclose_upstream_clients.__func__


def test_signals_features_and_content_standards_re_exported() -> None:
    """``SignalsFeatures`` (codegen ``Features2`` for ``Signals.features``)
    and ``ContentStandards`` (the ``MediaBuy.content_standards`` type, which
    collides with the unrelated wire ``adcp.types.ContentStandards``) are
    surfaced through :mod:`adcp.decisioning.capabilities` so adopters
    declaring deep Signals / MediaBuy blocks don't have to dig into
    ``generated_poc``.
    """
    from adcp.decisioning.capabilities import (
        ContentStandards,
        MediaBuy,
        Signals,
        SignalsFeatures,
    )
    from adcp.types import ContentStandards as WireContentStandards

    # Content-standards collision guard — same pattern as Account / MediaBuy / Creative.
    assert ContentStandards is not WireContentStandards
    assert ContentStandards.__name__ == "ContentStandards"

    # SignalsFeatures usable on a Signals declaration.
    sig = Signals(features=SignalsFeatures(catalog_signals=True))
    assert sig.features is not None
    assert sig.features.catalog_signals is True

    # ContentStandards usable on a MediaBuy declaration.
    mb = MediaBuy(content_standards=ContentStandards(supports_local_evaluation=True))
    assert mb.content_standards is not None
    assert mb.content_standards.supports_local_evaluation is True


def test_decisioning_capabilities_accepts_structured_fields() -> None:
    """``DecisioningCapabilities`` carries instances of the wire-spec
    capability sub-models. Validates the dataclass widening (commit 2 of
    the projection work) — every wire block has a corresponding field.

    No projection is exercised here; that lands in the projection-rewrite
    commit. The aim is to confirm adopters can declare every spec block
    and the dataclass holds the value typed.
    """
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import (
        Account,
        Adcp,
        Brand,
        ComplianceTesting,
        Creative,
        Execution,
        GeoMetros,
        Governance,
        IdempotencySupported,
        Identity,
        MediaBuy,
        RequestSigning,
        Signals,
        SupportedProtocol,
        Targeting,
        WebhookSigning,
    )

    # SponsoredIntelligence has required nested fields (endpoint, capabilities)
    # — constructing it requires more setup than this widening-smoke test
    # warrants. Adopters who claim sponsored_intelligence wire it explicitly;
    # platforms that don't claim it leave the field None.
    caps = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        adcp=Adcp(
            major_versions=[3],
            idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
        ),
        account=Account(supported_billing=["operator"]),
        media_buy=MediaBuy(
            supported_pricing_models=["cpm"],
            execution=Execution(
                targeting=Targeting(
                    geo_countries=True,
                    geo_metros=GeoMetros(nielsen_dma=True),
                ),
            ),
        ),
        signals=Signals(),
        governance=Governance(),
        brand=Brand(),
        creative=Creative(),
        request_signing=RequestSigning(supported=True),
        webhook_signing=WebhookSigning(supported=True, delivery_retry_horizon_seconds=86400),
        identity=Identity(),
        compliance_testing=ComplianceTesting(scenarios=["force_media_buy_status"]),
        supported_protocols=[SupportedProtocol.media_buy],
    )

    # Spot-check that the typed fields round-trip.
    assert caps.adcp is not None
    assert caps.adcp.major_versions[0].root == 3
    assert caps.account is not None
    assert caps.media_buy is not None
    assert caps.media_buy.execution is not None
    assert caps.media_buy.execution.targeting is not None
    assert caps.media_buy.execution.targeting.geo_countries is True
    assert caps.supported_protocols == [SupportedProtocol.media_buy]


def test_decisioning_capabilities_legacy_fields_still_default_empty() -> None:
    """Legacy flat fields (``pricing_models``, ``supported_billing``,
    ``channels``) still construct as empty lists by default — back-compat
    contract is preserved at the dataclass level. Deprecation warnings
    fire later at projection time, not at construction.
    """
    from adcp.decisioning import DecisioningCapabilities

    caps = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])

    assert caps.pricing_models == []
    assert caps.supported_billing == []
    assert caps.channels == []
    assert caps.creative_agents == []
    assert caps.config == {}
    assert caps.governance_aware is False
    # New structured fields default to None
    assert caps.adcp is None
    assert caps.account is None
    assert caps.media_buy is None
    assert caps.supported_protocols is None


# ----------------------------------------------------------------------
# Projection tests — handler.PlatformHandler.get_adcp_capabilities()
# emits every declared structured block, with legacy fields still
# functioning under DeprecationWarning.
# ----------------------------------------------------------------------


@pytest.fixture
def make_handler():
    """Build a minimal ``PlatformHandler`` over a stub ``DecisioningPlatform``.

    Lets each projection test parameterize the capabilities declaration
    without re-doing the executor / registry / accounts boilerplate.
    """
    from concurrent.futures import ThreadPoolExecutor

    from adcp.decisioning import (
        DecisioningCapabilities,
        DecisioningPlatform,
        InMemoryTaskRegistry,
        SingletonAccounts,
    )
    from adcp.decisioning.handler import PlatformHandler

    def _factory(capabilities: DecisioningCapabilities) -> PlatformHandler:
        class _StubPlatform(DecisioningPlatform):
            pass

        _StubPlatform.capabilities = capabilities
        _StubPlatform.accounts = SingletonAccounts(account_id="stub")
        return PlatformHandler(
            _StubPlatform(),
            executor=ThreadPoolExecutor(max_workers=1),
            registry=InMemoryTaskRegistry(),
        )

    return _factory


@pytest.mark.asyncio
async def test_projection_emits_structured_account_block(make_handler) -> None:
    """``account`` declared on capabilities — ``model_dump`` shape lands on the wire."""
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import Account

    handler = make_handler(
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            account=Account(supported_billing=["operator"], required_for_products=True),
        )
    )
    response = await handler.get_adcp_capabilities()
    assert response["account"]["supported_billing"] == ["operator"]
    assert response["account"]["required_for_products"] is True


@pytest.mark.asyncio
async def test_projection_emits_structured_media_buy_block(make_handler) -> None:
    """``media_buy`` declared on capabilities — full nested execution targeting projects."""
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import (
        Account,
        Execution,
        GeoMetros,
        MediaBuy,
        Targeting,
    )

    handler = make_handler(
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            account=Account(supported_billing=["operator"]),
            media_buy=MediaBuy(
                supported_pricing_models=["cpm"],
                execution=Execution(
                    targeting=Targeting(
                        geo_countries=True,
                        geo_metros=GeoMetros(nielsen_dma=True),
                    ),
                ),
            ),
        )
    )
    response = await handler.get_adcp_capabilities()
    assert response["media_buy"]["supported_pricing_models"] == ["cpm"]
    assert response["media_buy"]["execution"]["targeting"]["geo_countries"] is True
    assert response["media_buy"]["execution"]["targeting"]["geo_metros"]["nielsen_dma"] is True


@pytest.mark.asyncio
async def test_projection_emits_structured_idempotency_supported(make_handler) -> None:
    """``adcp.idempotency`` carries through with the supported-arm payload."""
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import Account, Adcp, IdempotencySupported

    handler = make_handler(
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            adcp=Adcp(
                major_versions=[3],
                idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
            ),
            account=Account(supported_billing=["operator"]),
        )
    )
    response = await handler.get_adcp_capabilities()
    assert response["adcp"]["idempotency"]["supported"] is True
    assert response["adcp"]["idempotency"]["replay_ttl_seconds"] == 86400


@pytest.mark.asyncio
async def test_projection_emits_wire_specialisms_field(make_handler) -> None:
    """Spec-known specialisms project to the wire ``specialisms`` field;
    novel slugs stay diagnostic-only and don't leak."""
    from adcp.decisioning import DecisioningCapabilities

    # Mix a spec-known and a novel slug — only the spec-known one should
    # land on the wire, since the wire ``specialisms`` field is enum-typed.
    # The ``__post_init__`` coerces spec-known slugs to enum members but
    # passes novel slugs through as strings; the projection filters on
    # ``hasattr(entry, "value")`` so only the enum-bearing ones reach
    # the wire. (The dispatch validator's UserWarning for novel slugs
    # fires at server boot, not at projection time — exercised in
    # tests/test_decisioning_dispatch.py.)
    handler = make_handler(
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed", "novel-experimental-slug"],
        )
    )
    response = await handler.get_adcp_capabilities()
    # Only spec-known slug emitted on the wire.
    assert response.get("specialisms") == ["sales-non-guaranteed"]


@pytest.mark.asyncio
async def test_projection_supported_protocols_override(make_handler) -> None:
    """When ``supported_protocols`` is set explicitly, it wins over the
    derived value (the 5% case for adopters claiming a protocol whose
    specialisms aren't enumerated)."""
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import SupportedProtocol

    handler = make_handler(
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],  # would derive to ["media_buy"]
            supported_protocols=[SupportedProtocol.media_buy, SupportedProtocol.signals],
        )
    )
    response = await handler.get_adcp_capabilities()
    assert sorted(response["supported_protocols"]) == ["media_buy", "signals"]


@pytest.mark.asyncio
async def test_projection_legacy_supported_billing_warns_and_projects(make_handler) -> None:
    """Legacy ``supported_billing`` still projects when ``account`` is None.

    The ``DeprecationWarning`` fires at construction (in
    ``DecisioningCapabilities.__post_init__``) so ``stacklevel=2`` points at
    the adopter's declaration site, not the dispatcher. Wrap the
    construction in ``pytest.warns``, not the projection.
    """
    from adcp.decisioning import DecisioningCapabilities

    with pytest.warns(DeprecationWarning, match="supported_billing"):
        caps = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            supported_billing=["operator"],
        )
    handler = make_handler(caps)
    response = await handler.get_adcp_capabilities()
    assert response["account"]["supported_billing"] == ["operator"]


@pytest.mark.asyncio
async def test_projection_legacy_pricing_models_warns_and_projects(make_handler) -> None:
    """Legacy ``pricing_models`` still projects when ``media_buy`` is None.
    Construction-time DeprecationWarning per the same pattern."""
    from adcp.decisioning import DecisioningCapabilities

    with pytest.warns(DeprecationWarning, match="pricing_models"):
        caps = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            supported_billing=["operator"],
            pricing_models=["cpm", "cpc"],
        )
    handler = make_handler(caps)
    response = await handler.get_adcp_capabilities()
    assert response["media_buy"]["supported_pricing_models"] == ["cpm", "cpc"]


@pytest.mark.asyncio
async def test_projection_structured_wins_over_legacy(make_handler) -> None:
    """When both structured and legacy forms are set, structured wins —
    legacy still emits its DeprecationWarning at construction."""
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import Account, MediaBuy

    with pytest.warns(DeprecationWarning):
        caps = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            account=Account(supported_billing=["agent"]),  # structured: agent
            supported_billing=["operator"],  # legacy: operator (loses)
            media_buy=MediaBuy(supported_pricing_models=["cpcv"]),  # structured: cpcv
            pricing_models=["cpm"],  # legacy: cpm (loses)
        )
    handler = make_handler(caps)
    response = await handler.get_adcp_capabilities()
    # Structured forms win.
    billing = response["account"]["supported_billing"]
    # Note: model_dump preserves enum-value form for SupportedBillingEnum.
    assert "agent" in billing
    assert "operator" not in billing
    assert response["media_buy"]["supported_pricing_models"] == ["cpcv"]


@pytest.mark.asyncio
async def test_projection_channels_warns_but_does_not_project(make_handler) -> None:
    """Legacy ``channels`` warns at construction but is no longer projected
    (the spec's ``portfolio.primary_channels`` requires
    ``portfolio.publisher_domains`` alongside, which the flat field can't
    supply)."""
    from adcp.decisioning import DecisioningCapabilities

    with pytest.warns(DeprecationWarning, match="channels"):
        caps = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            supported_billing=["operator"],
            channels=["display", "video"],
        )
    handler = make_handler(caps)
    response = await handler.get_adcp_capabilities()
    # No portfolio block emitted — channels alone can't satisfy the spec.
    assert "portfolio" not in response.get("media_buy", {})


@pytest.mark.asyncio
async def test_projection_emits_idempotency_unsupported_through_handler(make_handler) -> None:
    """``IdempotencyUnsupported`` arm projects with the discriminator and
    no ``replay_ttl_seconds`` — guards against the discriminated-union
    projection regressing to always-supported."""
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import Account, Adcp, IdempotencyUnsupported

    handler = make_handler(
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            adcp=Adcp(major_versions=[3], idempotency=IdempotencyUnsupported(supported=False)),
            account=Account(supported_billing=["operator"]),
        )
    )
    response = await handler.get_adcp_capabilities()
    assert response["adcp"]["idempotency"] == {"supported": False}
    # The ``replay_ttl_seconds`` field MUST NOT appear on the unsupported arm
    # (spec invariant — IdempotencyUnsupported's "not required" clause).
    assert "replay_ttl_seconds" not in response["adcp"]["idempotency"]


@pytest.mark.asyncio
async def test_projection_carries_major_versions_through_adcp_block(make_handler) -> None:
    """When the adopter declares ``Adcp(major_versions=[3, 4], ...)``,
    the projected response carries ``[3, 4]`` — not the helper's default
    of ``[3]``. Guards against silent override-loss when the structured
    Adcp block is set."""
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import Account, Adcp, IdempotencySupported

    handler = make_handler(
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            adcp=Adcp(
                major_versions=[3, 4],
                idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
            ),
            account=Account(supported_billing=["operator"]),
        )
    )
    response = await handler.get_adcp_capabilities()
    assert response["adcp"]["major_versions"] == [3, 4]


@pytest.mark.asyncio
async def test_projection_si_block_constructs_with_submodule_imports(make_handler) -> None:
    """Sponsored Intelligence requires nested ``Endpoint`` and
    ``SiCapabilities`` blocks. Adopters constructing SI declarations
    pull these from :mod:`adcp.decisioning.capabilities` — the
    re-export coverage test for the submodule's ``__all__`` is the
    only thing keeping deeply-nested SI declarations off ``generated_poc``
    direct imports.
    """
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import (
        Endpoint,
        SiCapabilities,
        SponsoredIntelligence,
        Transport,
    )

    # ``Endpoint.transports`` is required + minItems: 1; construct one
    # transport via the re-exported ``Transport`` type.
    si = SponsoredIntelligence(
        endpoint=Endpoint(transports=[Transport(type="mcp", url="https://si.example.com/mcp")]),
        capabilities=SiCapabilities(),
    )
    handler = make_handler(
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            sponsored_intelligence=si,
        )
    )
    response = await handler.get_adcp_capabilities()
    assert "sponsored_intelligence" in response


def test_bare_capabilities_emits_empty_supported_protocols_at_boot() -> None:
    """A platform with no specialisms and no ``supported_protocols``
    override emits an empty list at projection — the boot-time validator
    surfaces it as a configuration error (per spec ``minItems: 1``)
    rather than the SDK silently lying ``["media_buy"]``."""
    from concurrent.futures import ThreadPoolExecutor

    from adcp.decisioning import (
        DecisioningCapabilities,
        DecisioningPlatform,
        InMemoryTaskRegistry,
        SingletonAccounts,
    )
    from adcp.decisioning.handler import PlatformHandler
    from adcp.decisioning.types import AdcpError
    from adcp.decisioning.validate_capabilities import validate_capabilities_response_shape

    class _Bare(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="bare")

    handler = PlatformHandler(
        _Bare(),
        executor=ThreadPoolExecutor(max_workers=1),
        registry=InMemoryTaskRegistry(),
    )
    with pytest.raises(AdcpError) as excinfo:
        validate_capabilities_response_shape(handler)
    # Boot validator wraps the schema-validator's structured issues into
    # ``details``; one of them MUST name ``supported_protocols`` so the
    # operator can find the misconfiguration.
    err = excinfo.value
    issues = err.details.get("issues", []) if err.details else []
    issue_text = " ".join(f"{i.get('pointer', '')} {i.get('message', '')}" for i in issues)
    assert (
        "supported_protocols" in issue_text
    ), f"Expected validator to surface supported_protocols misconfiguration; got: {err}"


@pytest.mark.asyncio
async def test_projection_validates_against_response_schema(make_handler) -> None:
    """Round-trip a fully-populated declaration through the projection and
    confirm the response validates against
    ``protocol/get-adcp-capabilities-response.json``. This is the
    correctness invariant the rest of the test suite leans on — every
    declared field, every required field present, every optional field
    excluded when None.
    """
    from adcp.decisioning import DecisioningCapabilities
    from adcp.decisioning.capabilities import (
        Account,
        Adcp,
        Brand,
        Execution,
        GeoMetros,
        IdempotencySupported,
        MediaBuy,
        RequestSigning,
        Targeting,
        WebhookSigning,
    )
    from adcp.validation.schema_validator import validate_response

    handler = make_handler(
        DecisioningCapabilities(
            specialisms=["sales-non-guaranteed", "sales-guaranteed"],
            adcp=Adcp(
                major_versions=[3],
                idempotency=IdempotencySupported(supported=True, replay_ttl_seconds=86400),
            ),
            account=Account(
                supported_billing=["operator", "agent"],
                required_for_products=True,
                sandbox=True,
            ),
            media_buy=MediaBuy(
                supported_pricing_models=["cpm", "cpcv"],
                execution=Execution(
                    targeting=Targeting(
                        geo_countries=True,
                        geo_metros=GeoMetros(nielsen_dma=True),
                    ),
                ),
            ),
            brand=Brand(rights=False),
            request_signing=RequestSigning(supported=True),
            webhook_signing=WebhookSigning(supported=True, delivery_retry_horizon_seconds=86400),
        )
    )
    response = await handler.get_adcp_capabilities()
    outcome = validate_response("get_adcp_capabilities", response)
    assert outcome.valid, [f"{i.pointer}: {i.message}" for i in outcome.issues]
