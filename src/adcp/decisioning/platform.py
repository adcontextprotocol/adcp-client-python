"""DecisioningPlatform base class + capabilities declaration.

:class:`DecisioningPlatform` is the adopter-facing base. Adopters subclass
it, attach an :class:`AccountStore`, declare :class:`DecisioningCapabilities`,
and implement specialism methods (``get_products``, ``create_media_buy``,
``sync_audiences``, etc.) directly on the class. The dispatch adapter
discovers methods via ``hasattr`` at server boot, validates against the
declared capabilities, and routes requests through the framework's
existing transport machinery.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from adcp.decisioning.account_mode import get_account_mode, get_mock_upstream_url
from adcp.decisioning.types import AdcpError
from adcp.decisioning.upstream import (
    NoAuth,
    UpstreamAuth,
    UpstreamHttpClient,
)
from adcp.types.capabilities import (
    Adcp,
    Brand,
    CapabilitiesAccount,
    CapabilitiesCreative,
    CapabilitiesMediaBuy,
    ComplianceTesting,
    Governance,
    Identity,
    RequestSigning,
    Signals,
    Specialism,
    SponsoredIntelligence,
    SupportedProtocol,
    WebhookSigning,
)

if TYPE_CHECKING:
    from adcp.decisioning.accounts import AccountStore
    from adcp.decisioning.context import RequestContext


@dataclass
class DecisioningCapabilities:
    """What a platform claims to support.

    Read by ``validate_platform`` at server boot to confirm each
    declared specialism has the methods it requires, and surfaced via
    the framework's auto-generated ``get_adcp_capabilities`` response
    so buyers can pre-flight without trial-and-error tool calls.

    Capability declaration shape mirrors the AdCP wire spec
    (``protocol/get-adcp-capabilities-response.json``). Adopters import
    the typed sub-models from :mod:`adcp.decisioning.capabilities` —
    that submodule re-exports under wire-spec names, so declarations
    read 1:1 against the spec::

        from adcp.decisioning import DecisioningCapabilities
        from adcp.decisioning.capabilities import (
            Account, MediaBuy, Targeting, GeoMetros,
            IdempotencySupported, Specialism,
        )

        capabilities = DecisioningCapabilities(
            specialisms=[Specialism.sales_non_guaranteed.value],
            adcp=Adcp(
                major_versions=[3],
                idempotency=IdempotencySupported(
                    supported=True, replay_ttl_seconds=86400,
                ),
            ),
            account=Account(supported_billing=["operator"]),
            media_buy=MediaBuy(
                supported_pricing_models=["cpm"],
                execution=Execution(
                    targeting=Targeting(geo_countries=True),
                ),
            ),
        )

    Wire capability blocks (one field per top-level wire field):

    :param adcp: Core protocol info — ``major_versions`` and
        ``idempotency``. Required on the wire; defaults to ``None``
        means the framework will project a non-conformant response
        (the boot-time validator catches this).
    :param account: Account-management capabilities (billing, OAuth,
        sandbox).
    :param media_buy: Media-buy protocol capabilities — pricing
        models, reporting delivery methods, execution targeting, etc.
        Expected when ``media_buy`` is in ``supported_protocols``.
    :param signals: Signals protocol capabilities. Only emit when
        ``signals`` is in ``supported_protocols``.
    :param governance: Governance protocol capabilities.
    :param sponsored_intelligence: SI protocol capabilities.
    :param brand: Brand protocol capabilities.
    :param creative: Creative protocol capabilities.
    :param request_signing: RFC 9421 inbound request signing posture.
    :param webhook_signing: Outbound webhook-signing posture.
    :param identity: Operator key-scoping / compromise-response
        identity posture (advisory in 3.x).
    :param compliance_testing: Deterministic-testing capability via
        ``comply_test_controller``. Omit entirely if unsupported.
    :param supported_protocols: Override for the ``supported_protocols``
        wire field. Default ``None`` = derive from
        :attr:`specialisms` via ``SPECIALISM_TO_PROTOCOLS``. Set
        explicitly when claiming a protocol whose specialisms aren't
        all listed (e.g. transitional state, generic seller passing the
        baseline storyboard without claiming a specific specialism).

    SDK-internal dispatch (not wire fields):

    :param specialisms: AdCP specialism slugs the platform claims —
        e.g. ``['sales-non-guaranteed', 'sales-broadcast-tv']``,
        ``['audience-sync']``, ``['signal-marketplace']``, or
        ``['signal-owned']``. Each maps to a ``Protocol`` class under
        :mod:`adcp.decisioning.specialisms`. Drives method-conformance
        validation at boot AND projects to the wire ``specialisms``
        field.
    :param creative_agents: Optional list of creative-agent endpoints
        the platform delegates creative review/generation to. Empty
        list means "no creative-agent integration; review is in-house."
    :param config: Free-form adopter-defined config exposed on
        capabilities. Use sparingly — strongly-typed fields above are
        preferred.
    :param governance_aware: Set ``True`` ONLY when the platform
        implements ``governance-*`` specialisms AND has wired a custom
        :class:`adcp.decisioning.state.StateReader` that returns real
        :data:`adcp.decisioning.state.GovernanceContextJWS` values.
        Defaults ``False`` — non-governance adopters never touch this
        flag.

        Stage 3 dispatch (foundation PR's ``validate_platform``) will
        fail-fast at server boot when a platform claims a
        ``governance-*`` specialism without setting this flag and
        wiring a real ``StateReader`` — silent governance-gate
        skipping is a security regression the framework refuses to
        ship. The flag itself is the contract that lands now; the
        enforcement lands in Stage 3. See
        ``docs/proposals/decisioning-platform-dispatch-design.md#d15``.

    Deprecated flat-declaration shortcuts (will be removed in v5):

    :param channels: Inventory channels the platform serves —
        ``'display'``, ``'video'``, etc. Not currently projected to any
        wire field (the spec's ``portfolio.primary_channels`` requires
        ``portfolio.publisher_domains`` alongside, which the flat
        ``channels`` field cannot supply). Use
        ``media_buy=MediaBuy(portfolio=Portfolio(...))`` instead.
        Deprecated; emits ``DeprecationWarning`` at projection.
    :param pricing_models: Pricing models — ``'cpm'``, ``'cpc'``, etc.
        Superseded by ``media_buy.supported_pricing_models``. The
        projection prefers the structured field when both are set;
        emits ``DeprecationWarning`` when ``pricing_models`` is set.
    :param supported_billing: Billing parties this seller invoices —
        any subset of ``{"operator", "agent", "advertiser"}``.
        Superseded by ``account.supported_billing``. The projection
        prefers the structured field when both are set; emits
        ``DeprecationWarning`` when ``supported_billing`` is set
        (alone or alongside ``account``).
    """

    # SDK-internal dispatch (not wire fields)
    specialisms: list[Specialism | str] = field(default_factory=list)
    creative_agents: list[Any] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    governance_aware: bool = False
    # When True, the framework calls get_products and slices the full result
    # set to the requested page. Only suitable for in-memory / small-catalog
    # adopters whose get_products returns the complete unfiltered product set.
    # Adopters with DB-backed catalogs at production scale MUST leave this
    # False and handle cursor logic natively — returning 100k products only
    # to discard 99 950 is a silent production latency and memory spike.
    auto_paginate: bool = False

    # Wire capability blocks (mirror ``GetAdcpCapabilitiesResponse``)
    adcp: Adcp | None = None
    account: CapabilitiesAccount | None = None
    media_buy: CapabilitiesMediaBuy | None = None
    signals: Signals | None = None
    governance: Governance | None = None
    sponsored_intelligence: SponsoredIntelligence | None = None
    brand: Brand | None = None
    creative: CapabilitiesCreative | None = None
    request_signing: RequestSigning | None = None
    webhook_signing: WebhookSigning | None = None
    identity: Identity | None = None
    compliance_testing: ComplianceTesting | None = None
    supported_protocols: list[SupportedProtocol] | None = None

    # Deprecated flat-declaration shortcuts (removed in v5)
    channels: list[str] = field(default_factory=list)
    pricing_models: list[str] = field(default_factory=list)
    supported_billing: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Normalize spec-known specialism strings to enum members.

        Accepts either ``Specialism`` enum members (the type-safe form
        adopters should prefer) or AdCP slug strings (back-compat with
        existing code, novel pre-spec slugs, and intentional-typo paths
        the validator wants to diagnose). Strings that match a known
        ``Specialism`` value are coerced; unknown strings pass through
        unchanged so :func:`adcp.decisioning.dispatch.validate_platform`
        can surface them with typo-detection or forward-compat warnings
        at server boot.

        Adopter code is encouraged to import ``Specialism`` from
        :mod:`adcp.decisioning.capabilities` and write
        ``specialisms=[Specialism.sales_non_guaranteed]`` for clean
        type checks. The string path stays available for config-driven
        declarations, downstream test code, and pre-spec experimental
        slugs.
        """
        coerced: list[Specialism | str] = []
        for entry in self.specialisms:
            if isinstance(entry, Specialism):
                coerced.append(entry)
                continue
            try:
                coerced.append(Specialism(entry))
            except ValueError:
                # Novel / typo / pre-spec slug — keep as string so the
                # validator's typo-vs-novel-vs-unenforced classification
                # at boot can surface the right diagnostic.
                coerced.append(entry)
        self.specialisms = coerced

        # Deprecation warnings for legacy flat fields. Fire at
        # construction so ``stacklevel=2`` points at the adopter's
        # ``DecisioningCapabilities(...)`` declaration site (where the
        # legacy field was set), not at the MCP dispatcher that later
        # called ``get_adcp_capabilities``. Python's warnings registry
        # deduplicates by ``(message, module, lineno)`` so each unique
        # declaration warns once per process.
        if self.supported_billing:
            warnings.warn(
                (
                    "DecisioningCapabilities.supported_billing is deprecated; "
                    "set ``account=Account(supported_billing=[...])`` instead. "
                    "Will be removed in v5."
                ),
                DeprecationWarning,
                stacklevel=2,
            )
        if self.pricing_models:
            warnings.warn(
                (
                    "DecisioningCapabilities.pricing_models is deprecated; "
                    "set ``media_buy=MediaBuy(supported_pricing_models=[...])`` "
                    "instead. Will be removed in v5."
                ),
                DeprecationWarning,
                stacklevel=2,
            )
        if self.channels:
            warnings.warn(
                (
                    "DecisioningCapabilities.channels is deprecated and no longer "
                    "projected to the wire (the spec's ``portfolio.primary_channels`` "
                    "requires ``portfolio.publisher_domains`` alongside, which the "
                    "flat ``channels`` field cannot supply). Set "
                    "``media_buy=MediaBuy(portfolio=Portfolio(...))`` instead. "
                    "Will be removed in v5."
                ),
                DeprecationWarning,
                stacklevel=2,
            )

        # ``supported_protocols`` semantically rolls UP FROM specialisms
        # per spec — it's the storyboard commitment, with specialisms as
        # the sub-claims that contribute to it. The framework's
        # auto-derivation (see ``handler.py:get_adcp_capabilities``) is
        # ergonomic but inverts the spec's data direction. Adopters
        # leaning on auto-derive get a one-shot UserWarning steering
        # them toward declaring ``supported_protocols`` explicitly. The
        # auto-derive path is supported indefinitely; the warning is a
        # gentle nudge toward the spec-aligned form, not a deprecation.
        if self.supported_protocols is None and self.specialisms:
            warnings.warn(
                (
                    "DecisioningCapabilities.supported_protocols was not declared; "
                    "the framework will auto-derive it from ``specialisms`` via "
                    "``SPECIALISM_TO_PROTOCOLS``. Per spec, ``supported_protocols`` is "
                    "the primary storyboard-commitment declaration — set it "
                    "explicitly via ``supported_protocols=[SupportedProtocol.media_buy, "
                    "...]`` so the spec's intent (specialisms roll up to protocols) "
                    "is preserved at the declaration site. Auto-derivation is not "
                    "deprecated; this warning fires once per declaration site."
                ),
                UserWarning,
                stacklevel=2,
            )


#: Specialisms that depend on framework-supplied
#: :data:`adcp.decisioning.state.GovernanceContextJWS` reads. Claiming
#: any of these without setting ``governance_aware=True`` (and wiring
#: a real :class:`StateReader`) trips the server-boot fail-fast in
#: :func:`adcp.decisioning.dispatch.validate_platform` — silent
#: governance-gate skipping is a security regression the framework
#: refuses to ship.
#:
#: Mirrors every ``governance-*`` slug in
#: ``schemas/cache/enums/specialism.json`` — including
#: ``governance-aware-seller``. A seller agent that composes with a
#: buyer's governance agent reads governance context per-request; the
#: gate must catch it claiming the specialism without wiring the
#: StateReader, just like the spend-authority and delivery-monitor
#: governance agents themselves.
GOVERNANCE_SPECIALISMS: frozenset[str] = frozenset(
    {
        "governance-aware-seller",
        "governance-delivery-monitor",
        "governance-spend-authority",
    }
)


class DecisioningPlatform:
    """Adopter-facing base class for the v6.0 framework.

    Subclasses set:

    * :attr:`capabilities` — what the platform claims to support
    * :attr:`accounts` — an :class:`AccountStore` instance defining
      how to resolve a wire reference + auth context to an
      :class:`Account`

    Then implement specialism methods directly on the subclass
    (``get_products``, ``create_media_buy``, ``sync_audiences``, etc.).
    Each method takes a typed Pydantic request model + a
    :class:`RequestContext[TMeta]` and returns a typed response (or
    raises :class:`AdcpError`).

    The dispatch adapter (:func:`adcp.decisioning.create_adcp_server_from_platform`)
    discovers methods via ``hasattr``, validates against
    ``capabilities.specialisms``, and routes requests through the
    framework's existing ``adcp.server.serve()`` infrastructure.

    Example::

        class HelloSeller(DecisioningPlatform):
            capabilities = DecisioningCapabilities(
                specialisms=["sales-non-guaranteed"],
                channels=["display"],
                pricing_models=["cpm"],
            )
            accounts = SingletonAccounts(account_id="hello")

            def get_products(self, req, ctx):
                return GetProductsResponse(products=[...])

            def create_media_buy(self, req, ctx):
                return CreateMediaBuySuccess(media_buy_id="mb_1", ...)

    Per-method signatures are documented in the per-specialism
    Protocol classes under :mod:`adcp.decisioning.specialisms` —
    those are the canonical contract reference. The base class
    itself is intentionally minimal so adopters can mix in
    cross-cutting helpers without inheritance constraints.
    """

    #: Required: the platform's capability declaration. Subclasses
    #: override.
    capabilities: DecisioningCapabilities = DecisioningCapabilities()

    #: Required: the platform's account-resolution strategy.
    #: Subclasses set to a :class:`SingletonAccounts`,
    #: :class:`ExplicitAccounts`, :class:`FromAuthAccounts`, or
    #: custom :class:`AccountStore` instance. Type erased to ``Any``
    #: at the base because the typed shape is platform-specific
    #: (different ``TMeta`` per adopter); ``validate_platform``
    #: confirms an :class:`AccountStore` instance is set.
    accounts: AccountStore[Any] = None  # type: ignore[assignment]

    #: Optional: the adopter's production upstream API URL. Adapters
    #: that talk to a real upstream (GAM, Kevel, FreeWheel, etc.) set
    #: this to the canonical production endpoint
    #: (``"https://googleads.googleapis.com"``,
    #: ``"https://api.kevel.co"``, etc.). The value is fixed per
    #: platform — credentials and per-tenant routing flow through
    #: ``ctx.auth_info`` and ``ctx.account.metadata``, not through
    #: this URL.
    #:
    #: Leave ``None`` for platforms that don't talk to an HTTP
    #: upstream (pure in-process, in-memory, or composing via
    #: framework-level resolvers only).
    #:
    #: When :attr:`upstream_url` is ``None``, :meth:`upstream_for`
    #: refuses to construct a client for ``mode='live'`` /
    #: ``mode='sandbox'`` accounts (raising ``CONFIGURATION_ERROR``).
    #: ``mode='mock'`` accounts always read from
    #: ``account.metadata['mock_upstream_url']`` and never consult
    #: this attribute.
    upstream_url: str | None = None

    def upstream_for(
        self,
        ctx: RequestContext[Any],
        *,
        auth: UpstreamAuth | None = None,
        default_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        treat_404_as_none: bool = True,
    ) -> UpstreamHttpClient:
        """Return an :class:`UpstreamHttpClient` pointed at the right URL
        for this request's resolved account.

        Routing rules:

        - ``mode='live'`` / ``mode='sandbox'``: client at
          :attr:`upstream_url`. The adopter's production upstream URL is
          fixed per platform; only credentials vary per tenant (and flow
          through ``auth`` / ``ctx.auth_info``).
        - ``mode='mock'``: client at
          ``ctx.account.metadata['mock_upstream_url']``. The adopter
          populates this on mock-mode accounts; the framework points
          the client at the per-tenant fixture URL. Adapter business
          logic runs unchanged.

        Clients are cached per-platform-instance keyed by
        ``(base_url, id(auth))`` so repeated requests pool connections
        through one ``httpx.AsyncClient``. Different auth strategies
        get distinct clients (the auth is injected at construction
        and can't be swapped per-request from a cached client).

        :param ctx: The current request context. Required for
            ``ctx.account.mode`` and ``ctx.account.metadata``.
        :param auth: Auth strategy for the upstream. Defaults to
            :class:`NoAuth` (no header injected). Adopters typically
            pass a :class:`StaticBearer`, :class:`DynamicBearer`, or
            :class:`ApiKey`. The same auth is used regardless of mode
            — mock-mode fixtures usually accept any token, but adopters
            may want their adapter to send identical headers in mock
            and live so the wire shape matches end-to-end.
        :param default_headers: Headers included on every request
            (e.g. ``X-API-Version``).
        :param timeout: Per-request timeout in seconds. Default 30.0.
        :param treat_404_as_none: When ``True`` (default), GET/DELETE
            404s return ``None`` rather than raising.

        :raises AdcpError: ``CONFIGURATION_ERROR`` when:

            - Account is ``mode='mock'`` but
              ``account.metadata['mock_upstream_url']`` is missing,
              empty, or non-string. Adopter must populate it on
              mock-mode accounts in their ``AccountStore.resolve``.
            - Account is ``mode='live'`` / ``mode='sandbox'`` but
              ``self.upstream_url`` is ``None``. Adopter must declare
              the production URL on their platform subclass.
        """
        account = ctx.account
        mode = get_account_mode(account)

        if mode == "mock":
            base_url = get_mock_upstream_url(account)
            if base_url is None:
                raise AdcpError(
                    "CONFIGURATION_ERROR",
                    message=(
                        "account is mode='mock' but no 'mock_upstream_url' "
                        "string in metadata; populate it in "
                        "AccountStore.resolve for mock-mode accounts. "
                        "See docs/handler-authoring.md#mock-mode-upstream-routing."
                    ),
                    recovery="terminal",
                    field="account.metadata.mock_upstream_url",
                )
        else:
            # mode in {'live', 'sandbox'} — point at the platform's
            # declared production URL. Sandbox is the adopter's own
            # test infra; the URL is the same as live (credentials
            # + tenant routing change, not the URL).
            if self.upstream_url is None:
                raise AdcpError(
                    "CONFIGURATION_ERROR",
                    message=(
                        f"platform {type(self).__name__!s} has no "
                        f"upstream_url declared but resolved account is "
                        f"mode={mode!r}. Set the class attribute "
                        "upstream_url to the production upstream API URL, "
                        "or mark the account mode='mock' and populate "
                        "metadata['mock_upstream_url']."
                    ),
                    recovery="terminal",
                )
            base_url = self.upstream_url

        return self._cached_upstream_client(
            base_url=base_url,
            auth=auth or NoAuth(),
            default_headers=default_headers,
            timeout=timeout,
            treat_404_as_none=treat_404_as_none,
        )

    def _cached_upstream_client(
        self,
        *,
        base_url: str,
        auth: UpstreamAuth,
        default_headers: dict[str, str] | None,
        timeout: float,
        treat_404_as_none: bool,
    ) -> UpstreamHttpClient:
        """Per-instance cached :class:`UpstreamHttpClient` factory.

        Cache key is ``(base_url, id(auth))``. Pooling correctness
        requires keying on the auth instance — different ``DynamicBearer``
        closures for different tenants need distinct clients so the
        token resolver doesn't get accidentally shared, and the
        ``UpstreamHttpClient`` itself owns the underlying
        ``httpx.AsyncClient`` connection pool.

        Cache lives on the platform instance (``__dict__`` lazy init);
        multi-platform processes don't cross-pollute. Adopter code
        does not mutate the cache; lifecycle is "create once, reuse
        for the platform instance's lifetime."
        """
        cache: dict[tuple[str, int], UpstreamHttpClient] | None
        cache = getattr(self, "_upstream_client_cache", None)
        if cache is None:
            cache = {}
            self._upstream_client_cache = cache

        key = (base_url, id(auth))
        existing = cache.get(key)
        if existing is not None:
            return existing

        client = UpstreamHttpClient(
            base_url=base_url,
            auth=auth,
            default_headers=default_headers,
            timeout=timeout,
            treat_404_as_none=treat_404_as_none,
        )
        cache[key] = client
        return client
