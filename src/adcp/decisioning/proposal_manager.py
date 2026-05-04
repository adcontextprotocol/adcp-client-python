"""ProposalManager — the second platform shape.

The SDK's existing :class:`DecisioningPlatform` conflates two distinct
concerns: *assembling proposals from briefs* (``get_products``,
``refine``) vs. *executing media buys against an upstream*
(``create_media_buy``, ``update_media_buy``, ``get_delivery``). The
two-platform composition splits them: a separate :class:`ProposalManager`
handles the proposal side; the :class:`DecisioningPlatform` keeps the
execution side. Either platform can be mock-backed independently of
the other.

See ``docs/proposals/product-architecture.md`` § "The two-platform
composition" for the full design context.

v1 surface:

* :class:`ProposalManager` — the Protocol contract: ``get_products`` +
  optional ``refine_products``. Sync or async (detected via
  :func:`asyncio.iscoroutinefunction`, same convention as the
  ``SalesPlatform`` Protocol).
* :class:`ProposalCapabilities` — what the manager can do
  (``sales-guaranteed`` vs. ``sales-non-guaranteed``, refine support,
  dynamic products, rate-card pricing, availability reservations,
  multi-decisioning).
* :class:`MockProposalManager` — the v1 default forwarder. Symmetric
  with :meth:`DecisioningPlatform.upstream_for`'s mock-mode dispatch.
  Adopters who don't yet have proposal logic point this at a running
  ``bin/adcp.js mock-server <specialism>``; the mock fixtures provide
  a working catalog with stub recipes. Their first working seller
  agent runs against the mock fixtures with zero adopter code on the
  proposal side.

**Out of scope for v1** (deferred to subsequent PRs — flagged in the
design doc):

* Session cache for in-flight proposals
* ``finalize`` transition (``buying_mode='refine'`` +
  ``action='finalize'``)
* ``expires_at`` enforcement
* Capability-overlap declaration on Recipe + framework validation
* Recipe persistence through buy lifecycle (hydration in
  ``create_media_buy`` / ``update_media_buy`` / ``get_delivery``)

**Tenant binding (v1).** The ProposalManager is wired per-tenant on
:class:`PlatformRouter` via the ``proposal_managers={tenant_id:
ProposalManager}`` kwarg. Multi-tenant deployments (salesagent,
agentic-adapters social) need different proposal logic per tenant —
a GAM tenant has different products from a Kevel tenant; a Meta
tenant has different proposal assembly from a TikTok tenant. The
router dispatches ``get_products`` (and optionally ``refine``-mode
``get_products``) to the tenant's manager when one is wired; tenants
without a manager fall through to the tenant's
:meth:`DecisioningPlatform.get_products` — backward-compatible per
tenant. Single-tenant adopters use a one-entry router.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Protocol,
    runtime_checkable,
)

from adcp.decisioning.types import AdcpError
from adcp.decisioning.upstream import (
    NoAuth,
    UpstreamAuth,
    UpstreamHttpClient,
    create_upstream_http_client,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.recipe import Recipe
    from adcp.decisioning.types import MaybeAsync
    from adcp.types import GetProductsRequest, GetProductsResponse


#: Sales specialisms a ProposalManager can serve. Mirrors the
#: ``sales-*`` slugs in ``schemas/cache/enums/specialism.json``. v1
#: scopes to the two ``ProposalManager``-relevant flavours; the
#: broader sales catalogue (broadcast-tv, social, proposal-mode,
#: catalog-driven) lands in subsequent PRs as adopter signal grows.
SalesSpecialism = Literal["sales-guaranteed", "sales-non-guaranteed"]


@dataclass(frozen=True)
class ProposalCapabilities:
    """Capability declaration for a :class:`ProposalManager`.

    Sales-axis-scoped: proposal handling is a sales-specialism concern,
    not a generic platform-wide concept. The :attr:`sales_specialism`
    field declares which AdCP sales specialism this manager serves;
    capability flags declare which optional behaviours it supports.

    The framework reads this declaration at :func:`serve` time to
    decide which dispatch paths apply (e.g. ``refine_products`` is
    only invoked when :attr:`refine` is True).

    :param sales_specialism: Which AdCP sales specialism this manager
        serves. ``"sales-guaranteed"`` for guaranteed-direct flows
        with proposal lifecycle (``finalize`` → committed proposal →
        media buy); ``"sales-non-guaranteed"`` for catalog-style
        flows where ``get_products`` returns a static catalog and
        buyers reference products directly at ``create_media_buy``.
    :param refine: When True, the manager implements
        :meth:`ProposalManager.refine_products` and the framework
        routes ``get_products`` requests with ``buying_mode='refine'``
        to that method. When False, refine requests fall through to
        ``get_products`` (or surface ``UNSUPPORTED_FEATURE`` if the
        manager rejects them).
    :param dynamic_products: Signal-driven product assembly — the
        manager constructs products from buyer signals at request
        time rather than enumerating a static catalogue. The
        framework treats this as a hint today; future PRs may
        validate that ``InventoryStore`` / ``SignalStore`` primitives
        are wired when this flag is set.
    :param rate_card_pricing: The manager consults rate cards (per
        buyer relationship per product) when emitting prices.
        Informational in v1; future PRs may validate that a
        ``RateCardStore`` primitive is wired.
    :param availability_reservations: The manager reserves capacity
        at proposal time (typical for guaranteed). Informational in
        v1; the ``finalize`` transition that drives the actual hold
        lands in a subsequent PR.
    :param multi_decisioning: The manager emits products whose
        recipes route to >1 :class:`DecisioningPlatform` per request
        (the Prebid salesagent shape — GAM for guaranteed-direct,
        Kevel for non-guaranteed-remnant in the same proposal).
        Informational in v1; the per-recipe-kind routing lands in
        a subsequent PR alongside the typed-recipe registry.
    """

    sales_specialism: SalesSpecialism

    refine: bool = False
    finalize: bool = False
    expires_at_grace_seconds: int = 0
    dynamic_products: bool = False
    rate_card_pricing: bool = False
    availability_reservations: bool = False
    # ``multi_decisioning`` retained for v1 source-compat (adopters who
    # set it pass through harmlessly). Per v1.5 § D2 / Resolutions §6,
    # the framework no longer reads this field. Stops appearing on new
    # adopter declarations; v1.6+ removes it entirely.
    multi_decisioning: bool = False

    def __post_init__(self) -> None:
        # Spec only allows the two slugs at v1. Adopter passing a
        # typo or a different sales-* flavour gets a structured
        # error rather than a silent miss at dispatch time.
        valid = ("sales-guaranteed", "sales-non-guaranteed")
        if self.sales_specialism not in valid:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "ProposalCapabilities.sales_specialism must be one of "
                    f"{valid!r}. Got {self.sales_specialism!r}. v1 scopes "
                    "ProposalManager to the two core sales specialisms; "
                    "broader specialism support lands in subsequent PRs."
                ),
                recovery="terminal",
                field="sales_specialism",
            )
        # ``expires_at_grace_seconds`` must be non-negative; a negative
        # value would shrink the inventory hold rather than extend it,
        # which contradicts the design's intent.
        if self.expires_at_grace_seconds < 0:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "ProposalCapabilities.expires_at_grace_seconds must be "
                    f">= 0; got {self.expires_at_grace_seconds!r}. The "
                    "grace window extends the inventory hold past "
                    "expires_at; negative values would shrink it."
                ),
                recovery="terminal",
                field="expires_at_grace_seconds",
            )


@dataclass(frozen=True)
class FinalizeProposalRequest:
    """Framework-internal request shape passed to
    :meth:`ProposalManager.finalize_proposal`.

    Constructed by the dispatcher when a buyer's ``get_products``
    request with ``buying_mode='refine'`` carries a
    ``refine[i].action='finalize'`` entry. Adopter doesn't parse the
    wire envelope; the framework projects.

    :param proposal_id: The draft proposal the buyer is asking to
        finalize. Hydrated from the wire's ``refine[i].proposal_id``
        field.
    :param recipes: ``product_id -> Recipe`` mapping pulled from the
        :class:`adcp.decisioning.ProposalStore` draft. The adopter's
        finalize logic typically lock-prices these and emits the
        committed proposal.
    :param proposal_payload: The draft's wire ``Proposal`` shape
        (the same payload the adopter returned on the prior
        ``get_products`` / ``refine_products`` call). Adopter
        typically modifies this with locked pricing and returns it
        on :class:`FinalizeProposalSuccess`.
    :param ask: The buyer's per-entry refine ``ask`` text — what they
        want finalized. Free-form; adopter consumes.
    :param parent_request: The parent :class:`GetProductsRequest` so
        the adopter sees the full envelope (account, etc.) without
        the framework projecting fields one-by-one.
    """

    proposal_id: str
    recipes: dict[str, Recipe]
    proposal_payload: dict[str, Any]
    ask: str | None
    parent_request: GetProductsRequest


@dataclass(frozen=True)
class FinalizeProposalSuccess:
    """Adopter-returned shape from
    :meth:`ProposalManager.finalize_proposal` — inline commit.

    Framework calls
    :meth:`adcp.decisioning.ProposalStore.commit` with these fields
    before projecting the wire response. The buyer sees the committed
    :class:`~adcp.types.Proposal` with ``proposal_status='committed'``
    + ``expires_at`` populated on the next ``get_products`` response
    payload.

    :param proposal: The wire ``Proposal`` shape with locked pricing
        and ``proposal_status='committed'``. Adopter typically
        derives this from
        :attr:`FinalizeProposalRequest.proposal_payload` with
        modifications.
    :param expires_at: Inventory hold deadline. After this (plus the
        adopter's
        :attr:`ProposalCapabilities.expires_at_grace_seconds`
        window), the framework rejects ``create_media_buy`` calls
        referencing the proposal with ``PROPOSAL_EXPIRED``.
    :param recipes: Optional refreshed recipe mapping. ``None``
        (default) preserves the draft's recipes verbatim. Adopters
        whose finalize logic mutates recipe fields (e.g. locking a
        line-item template id) supply a fresh mapping.
    """

    proposal: dict[str, Any]
    expires_at: datetime
    recipes: dict[str, Recipe] | None = None


@runtime_checkable
class ProposalManager(Protocol):
    """Assembles proposals from buyer briefs.

    Reads inventory, signals, rate cards, availability. Produces
    proposals where each :class:`~adcp.types.Product` carries a typed
    ``implementation_config`` (a recipe; see :class:`Recipe`) the
    bound :class:`DecisioningPlatform` consumes at ``create_media_buy``.

    Methods may be sync or async; the dispatch adapter detects via
    :func:`asyncio.iscoroutinefunction` and runs sync methods on a
    thread pool. Same convention as the existing
    :class:`SalesPlatform` Protocol so a single thread pool serves
    both surfaces.

    v1 surface (this PR):

    * :meth:`get_products` — initial product discovery from a buyer
      brief. Required.
    * :meth:`refine_products` — refine-mode iteration (capability-
      gated by :attr:`ProposalCapabilities.refine`). Optional;
      adopters who don't implement refine return non-refine products
      via ``get_products`` and the framework surfaces
      ``UNSUPPORTED_FEATURE`` on refine requests when this method is
      absent.

    Future-state surfaces (deferred to subsequent PRs):

    * ``finalize`` transition handling (``buying_mode='refine'`` +
      ``action='finalize'`` → committed proposal with locked pricing
      + ``expires_at``)
    * Capability-overlap declaration on :class:`Recipe` + framework
      validation
    * Recipe lifecycle (session cache → persisted store → hydration
      at ``create_media_buy``)

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    rejection (``BUDGET_TOO_LOW``, ``POLICY_VIOLATION``,
    ``UNSUPPORTED_FEATURE``); the framework projects to the wire
    structured-error envelope.
    """

    capabilities: ClassVar[ProposalCapabilities]
    """What this ProposalManager can do — sales specialism + capability
    flags. Subclasses MUST override on the class body."""

    def get_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext[Any],
    ) -> MaybeAsync[GetProductsResponse]:
        """Initial product discovery from a buyer brief.

        Each returned :class:`~adcp.types.Product` SHOULD carry an
        ``implementation_config`` matching the bound
        :class:`DecisioningPlatform`'s recipe schema (see
        :class:`Recipe`). v1 treats ``implementation_config`` as
        opaque ``dict[str, Any]``; typed recipe validation lands in a
        subsequent PR.

        For non-guaranteed flows: typically a static catalogue,
        possibly filtered by buyer brief / signals.

        For guaranteed flows: typically a brief-driven assembly
        consulting rate cards + availability. v1 doesn't yet wire
        the ``finalize`` transition; adopters return draft proposals
        and rely on the buyer driving lifecycle via subsequent
        ``create_media_buy`` calls.
        """
        ...

    # NOTE: ``finalize_proposal`` is intentionally NOT a Protocol member.
    # Per Resolutions §7 of the v1.5 design doc, the framework detects
    # finalize support via ``hasattr(manager, "finalize_proposal")`` AND
    # ``manager.capabilities.finalize is True``. Putting the method on the
    # ``runtime_checkable`` Protocol body would break ``isinstance(...)``
    # for any v1 manager that doesn't declare finalize (every adopter who
    # ships catalog-mode without committing proposals). Mirrors v1's
    # ``refine_products`` posture — present on the Protocol surface only
    # because adopters declaring ``refine=True`` need a typed signature
    # to write against; absent from runtime conformance checks.
    #
    # Adopters declaring ``finalize=True`` who don't implement the method
    # get a clear error at ``serve()`` time; the boot-time validator walks
    # methods like ``_is_method_overridden`` from the dispatch design D3.

    # Optional refine surface — capability-gated.
    def refine_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext[Any],
    ) -> MaybeAsync[GetProductsResponse]:
        """Refine-mode iteration on a previous ``get_products`` response.

        Per the spec, refine is a ``buying_mode`` value on
        ``get_products`` — the wire envelope is the same. The framework
        routes refine requests to this method when:

        1. The wired ProposalManager declares
           :attr:`ProposalCapabilities.refine` = True, AND
        2. The request has ``buying_mode == 'refine'``, AND
        3. The manager subclass implements this method.

        Otherwise, refine requests fall through to :meth:`get_products`
        (the manager handles refinement itself) or surface
        ``UNSUPPORTED_FEATURE`` if neither path is wired.

        v1 does NOT handle the ``finalize`` action — that's a
        subsequent PR. Adopters implementing this method today should
        treat ``action='finalize'`` entries as
        ``UNSUPPORTED_FEATURE`` and return a structured error.
        """
        ...


class MockProposalManager:
    """v1 default forwarder. Dispatches ``get_products`` /
    ``refine_products`` to a running mock-server.

    Symmetric with :meth:`DecisioningPlatform.upstream_for`'s
    mock-mode dispatch (see Phase 2 — ``mock_upstream_url`` on
    ``Account.metadata``). Adopter declares a ``mock_upstream_url``
    pointing at ``bin/adcp.js mock-server <specialism>``; the
    framework forwards ``get_products`` requests verbatim and the
    mock-server returns wire-shaped products carrying stub recipes.

    The on-ramp story: adopters who don't yet have proposal logic of
    their own start with this class pointed at the appropriate
    mock-server specialism. Their first working seller agent runs
    against the mock fixtures with zero adopter code on the proposal
    side. They implement their own :class:`ProposalManager` subclass
    incrementally as they replace mock-served slices with real
    assembly logic.

    The mock-server lifecycle is **not** managed by the SDK. Adopters
    or CI start it as needed (``bin/adcp.js mock-server
    sales-non-guaranteed``) and pass the resulting URL to this
    class's constructor. Same posture as the
    :meth:`DecisioningPlatform.upstream_for` mock-mode dispatch.

    Example::

        manager = MockProposalManager(
            mock_upstream_url="http://localhost:4500",
        )
        router = PlatformRouter(
            accounts=...,
            platforms={"default": MyPlatform()},
            proposal_managers={"default": manager},
            capabilities=...,
        )
        serve(router)

    :param mock_upstream_url: URL of the running mock-server. The
        forwarder POSTs ``GetProductsRequest`` payloads to
        ``{mock_upstream_url}/get_products``.
    :param auth: Optional auth strategy applied to outbound mock-
        server calls. Defaults to :class:`NoAuth` — mock-servers
        typically run unauthenticated on localhost. Adopters running
        a shared mock-server behind a token proxy pass a
        :class:`StaticBearer` / :class:`ApiKey` here.
    :param sales_specialism: Which sales specialism this mock manager
        serves. Defaults to ``"sales-non-guaranteed"`` (the catalog-
        style mock-server fixture). Adopters wiring a guaranteed
        mock pass ``"sales-guaranteed"`` so the framework's
        capability projection matches the fixtures.
    :param default_headers: Headers forwarded on every mock-server
        request (e.g. ``X-Tenant-Id``).
    :param timeout: Per-request timeout in seconds. Default 30.0.
    """

    def __init__(
        self,
        *,
        mock_upstream_url: str,
        auth: UpstreamAuth | None = None,
        sales_specialism: SalesSpecialism = "sales-non-guaranteed",
        default_headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not mock_upstream_url or not isinstance(mock_upstream_url, str):
            raise AdcpError(
                "CONFIGURATION_ERROR",
                message=(
                    "MockProposalManager requires a non-empty "
                    "``mock_upstream_url`` pointing at a running "
                    "``bin/adcp.js mock-server <specialism>`` instance."
                ),
                recovery="terminal",
                field="mock_upstream_url",
            )

        # Per-instance capabilities — populated from the constructor
        # arg so a single class can serve guaranteed or non-guaranteed
        # mock fixtures without subclassing.
        self.capabilities: ProposalCapabilities = ProposalCapabilities(
            sales_specialism=sales_specialism,
        )
        self._mock_upstream_url = mock_upstream_url
        self._client = create_upstream_http_client(
            mock_upstream_url,
            auth=auth or NoAuth(),
            default_headers=default_headers,
            timeout=timeout,
            # Mock-server should never 404 on the canonical paths;
            # surface as a structured error if it does.
            treat_404_as_none=False,
        )

    @property
    def mock_upstream_url(self) -> str:
        """The configured mock-server URL — useful for diagnostics."""
        return self._mock_upstream_url

    @property
    def client(self) -> UpstreamHttpClient:
        """The underlying :class:`UpstreamHttpClient`. Adopter
        subclasses extending the forwarder may need direct access for
        custom paths; production users typically don't reach in here.
        """
        return self._client

    async def aclose(self) -> None:
        """Release the underlying connection pool. Idempotent."""
        await self._client.aclose()

    async def get_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Forward to ``POST {mock_upstream_url}/get_products``.

        Returns the parsed JSON dict verbatim — the framework's
        wire-projection layer handles serialization back to the
        buyer. Mock-server returns spec-shaped
        :class:`~adcp.types.GetProductsResponse` payloads, so the
        dict round-trips cleanly through the existing
        ``GetProductsResponse`` validation.
        """
        del ctx  # The mock-server is stateless; no per-request routing.
        payload = _request_to_dict(req)
        result: Any = await self._client.post("/get_products", json=payload)
        # Mock-server should always return a dict; defensive cast for
        # the type checker.
        if not isinstance(result, dict):
            raise AdcpError(
                "SERVICE_UNAVAILABLE",
                message=(
                    f"mock-server at {self._mock_upstream_url} returned "
                    f"a non-dict payload from /get_products: "
                    f"{type(result).__name__}"
                ),
                recovery="transient",
            )
        return result

    async def refine_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Forward refine-mode requests to the mock-server.

        Per the spec, refine rides on the same ``get_products``
        endpoint with ``buying_mode='refine'``. The forwarder POSTs
        to the same URL; the mock-server distinguishes by reading
        ``buying_mode`` on the payload.
        """
        # Same wire envelope, same upstream endpoint. The capability
        # flag governs WHETHER the framework dispatches refine
        # requests here vs. ``get_products``; the actual upstream
        # call is identical.
        return await self.get_products(req, ctx)


def _request_to_dict(req: Any) -> dict[str, Any]:
    """Serialize a Pydantic request to a JSON-safe dict.

    Pydantic models go through ``model_dump(mode='json',
    exclude_none=True)`` so the wire payload matches the spec shape.
    Plain dicts pass through. Anything else raises — the framework
    only ever hands typed Pydantic requests through this path.
    """
    if hasattr(req, "model_dump"):
        return dict(req.model_dump(mode="json", exclude_none=True))
    if isinstance(req, dict):
        return req
    raise AdcpError(
        "INTERNAL_ERROR",
        message=(
            f"MockProposalManager expected a Pydantic GetProductsRequest "
            f"or dict; got {type(req).__name__!r}. Framework dispatch "
            "contract drift."
        ),
        recovery="terminal",
    )


__all__ = [
    "FinalizeProposalRequest",
    "FinalizeProposalSuccess",
    "MockProposalManager",
    "ProposalCapabilities",
    "ProposalManager",
    "SalesSpecialism",
]
