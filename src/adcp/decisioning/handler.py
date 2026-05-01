"""``PlatformHandler`` — wire-shape shims that route to a DecisioningPlatform.

This module is the codegen target — ``scripts/generate_decisioning_handler.py``
will (in a follow-up PR) emit this file by walking the per-specialism
Protocols. For v6.0 alpha foundation, the file is hand-written; the
codegen drift test ships in Stage 4.

Each shim:

1. Accepts the typed Pydantic request + framework :class:`ToolContext`.
2. Resolves the account via ``platform.accounts.resolve``.
3. Builds the typed :class:`RequestContext` via
   :func:`_build_request_context` (D2 + D9 + D15).
4. Calls :func:`_invoke_platform_method` to invoke the platform method,
   which projects ``TaskHandoff`` and wraps non-``AdcpError`` exceptions
   to the wire envelope.
5. Returns whatever the platform method returned — typed Pydantic
   response, plain dict matching the wire shape, or the ``Submitted``
   envelope dict from a TaskHandoff projection. The ``cast()`` on each
   shim is a static-typing hint for callers; it is NOT a runtime
   validation pass. The framework's transport layer
   (``adcp.server.serve``) handles wire serialization for both Pydantic
   and dict returns. Adopters relying on Pydantic round-trip validation
   can opt in via ``response_validator`` middleware.

The class-level ``advertised_tools: ClassVar[set[str]]`` declaration is
auto-registered with the framework's tool-discovery seam via
:meth:`adcp.server.base.ADCPHandler.__init_subclass__` (PR #318). Adopters
get a focused ``tools/list`` filter without manual registration.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar, cast

from adcp.decisioning.context import AuthInfo
from adcp.decisioning.dispatch import (
    _build_request_context,
    _invoke_platform_method,
)
from adcp.decisioning.webhook_emit import maybe_emit_sync_completion
from adcp.server.base import ADCPHandler, ToolContext

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from adcp.decisioning.platform import DecisioningPlatform
    from adcp.decisioning.resolve import ResourceResolver
    from adcp.decisioning.state import StateReader
    from adcp.decisioning.task_registry import TaskRegistry
    from adcp.decisioning.types import Account
    from adcp.types import (
        AccountReference,
        CreateMediaBuyRequest,
        CreateMediaBuySuccessResponse,
        GetMediaBuyDeliveryRequest,
        GetMediaBuyDeliveryResponse,
        GetMediaBuysRequest,
        GetMediaBuysResponse,
        GetProductsRequest,
        GetProductsResponse,
        ListCreativeFormatsRequest,
        ListCreativeFormatsResponse,
        ListCreativesRequest,
        ListCreativesResponse,
        ProvidePerformanceFeedbackRequest,
        ProvidePerformanceFeedbackResponse,
        SyncCreativesRequest,
        SyncCreativesSuccessResponse,
        UpdateMediaBuyRequest,
        UpdateMediaBuySuccessResponse,
    )
    from adcp.webhook_sender import WebhookSender


# ---------------------------------------------------------------------------
# Class-level advertised tool surface
# ---------------------------------------------------------------------------

#: All sales-* tools the v6.0 PlatformHandler shim covers. Auto-registered
#: with the framework's tool-discovery seam via ``__init_subclass__`` —
#: ``tools/list`` filters to this set unless the operator passes
#: ``advertise_all=True``. Adopters who only implement a subset of these
#: methods on their ``DecisioningPlatform`` subclass: the framework's
#: existing override-detection (``_is_method_overridden``) handles the
#: filter — methods inherited from the base ``DecisioningPlatform`` (which
#: doesn't define them) are NOT in the override set, so the framework
#: drops the tool from ``tools/list`` automatically.
_SALES_ADVERTISED_TOOLS: frozenset[str] = frozenset(
    {
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
        "get_media_buys",
        "provide_performance_feedback",
        "list_creative_formats",
        "list_creatives",
    }
)


class PlatformHandler(ADCPHandler[ToolContext]):
    """ADCPHandler subclass that routes wire requests to a
    :class:`DecisioningPlatform` via :func:`_invoke_platform_method`.

    Constructed by :func:`adcp.decisioning.serve.create_adcp_server_from_platform`
    — adopters never instantiate directly. The handler holds:

    * ``platform`` — the adopter's :class:`DecisioningPlatform` subclass
      instance. Method dispatches read/call this.
    * ``executor`` — the framework-allocated thread-pool for sync platform
      methods (D5).
    * ``registry`` — the :class:`TaskRegistry` for handoff lifecycle.
    * Optional ``state_reader`` / ``resource_resolver`` — Stage-3+ wiring
      for v6.1 backing-store impls; defaults to the v6.0 stubs.

    Per-method shims follow the same template:

    1. Extract ``account_ref`` from the typed request (when the tool
       carries ``account`` on the wire).
    2. Resolve via ``platform.accounts.resolve(ref, auth_info=...)``.
    3. Build :class:`RequestContext` via :func:`_build_request_context`.
    4. Invoke the platform method via :func:`_invoke_platform_method`.

    Adopters who don't override a given platform method get the framework's
    ``not_supported`` baseline (per ADCPHandler) on those tools — and the
    override-detection filter drops the tool from ``tools/list`` unless
    they pass ``advertise_all=True``.
    """

    advertised_tools: ClassVar[set[str]] = set(_SALES_ADVERTISED_TOOLS)

    _agent_type = "decisioning platform"

    def __init__(
        self,
        platform: DecisioningPlatform,
        *,
        executor: ThreadPoolExecutor,
        registry: TaskRegistry,
        state_reader: StateReader | None = None,
        resource_resolver: ResourceResolver | None = None,
        webhook_sender: WebhookSender | None = None,
        auto_emit_completion_webhooks: bool = True,
    ) -> None:
        super().__init__()
        self._platform = platform
        self._executor = executor
        self._registry = registry
        self._state_reader = state_reader
        self._resource_resolver = resource_resolver
        self._webhook_sender = webhook_sender
        self._auto_emit_completion_webhooks = auto_emit_completion_webhooks

    # ----- account resolution helper -----

    async def _resolve_account(
        self,
        ref: AccountReference | None,
        ctx: ToolContext,
    ) -> Account[Any]:
        """Resolve a wire :class:`AccountReference` to a typed
        :class:`Account` via the platform's :class:`AccountStore`.

        Pulls auth info from ``ctx.metadata['auth_info']`` when the
        operator's ``context_factory`` populates it; otherwise None.
        Adopter ``AccountStore`` impls handle missing-auth cases per
        their own resolution mode (``'derived'`` tolerates None;
        ``'implicit'`` raises ``AUTH_INVALID``; ``'explicit'`` resolves
        by ref).
        ``AccountStore.resolve`` takes a dict — convert the typed
        Pydantic ``AccountReference`` via ``model_dump()`` so adopter
        store impls see a normalized shape.
        """
        auth_info = self._extract_auth_info(ctx)
        # Handle both Pydantic AccountReference (typical wire path) and
        # raw dict (test fixtures using model_construct, custom dispatch
        # paths). Adopter stores implementing custom shapes are
        # responsible for whatever they accept.
        ref_dict: dict[str, Any] | None
        if ref is None:
            ref_dict = None
        elif hasattr(ref, "model_dump"):
            ref_dict = ref.model_dump()
        elif isinstance(ref, dict):
            ref_dict = ref
        else:
            ref_dict = cast("dict[str, Any]", ref)
        result = self._platform.accounts.resolve(ref_dict, auth_info=auth_info)
        if asyncio.iscoroutine(result):
            return cast("Account[Any]", await result)
        return cast("Account[Any]", result)

    @staticmethod
    def _extract_auth_info(ctx: ToolContext) -> AuthInfo | None:
        """Pull AuthInfo from ToolContext.metadata when present.

        The framework's existing auth integrations (BearerTokenAuthMiddleware,
        custom context_factory) populate ``ctx.metadata`` with
        principal/scope info. Adopter conventions vary; this helper checks
        for an ``adcp.auth_info`` key — Stage 3 ``serve()`` wiring sets
        this from the canonical principal. Returns None when no auth key
        is present (dev / ``'derived'`` fixtures).
        """
        raw = ctx.metadata.get("adcp.auth_info") if ctx.metadata else None
        if isinstance(raw, AuthInfo):
            return raw
        if isinstance(raw, dict):
            return AuthInfo(
                kind=raw.get("kind", "derived"),
                key_id=raw.get("key_id"),
                principal=raw.get("principal"),
                scopes=list(raw.get("scopes", [])),
            )
        return None

    def _maybe_auto_emit_sync_completion(
        self,
        method_name: str,
        params: Any,
        result: Any,
    ) -> None:
        """Fire the F12 sync-completion webhook if applicable.

        Skips TaskHandoff projections — those go through the registry
        completion path which emits its own webhook on terminal state.
        The auto-emit fires on the sync-success arm only, mirroring the
        JS-side ``routeIfHandoff`` logic at
        ``src/lib/server/decisioning/runtime/from-platform.ts``.

        TaskHandoff projection returns ``{"task_id": ..., "status":
        "submitted"}`` from ``_project_handoff``; sync success returns
        a Pydantic response or a dict matching the wire shape. We
        distinguish on the ``status == "submitted"`` shape.
        """
        if isinstance(result, dict) and result.get("status") == "submitted":
            # TaskHandoff projection — registry completion path emits
            # its own webhook on terminal state.
            return
        maybe_emit_sync_completion(
            sender=self._webhook_sender,
            enabled=self._auto_emit_completion_webhooks,
            method_name=method_name,
            params=params,
            result=result,
        )

    def _build_ctx(
        self,
        tool_ctx: ToolContext,
        account: Account[Any],
    ) -> Any:
        """Wrap :func:`_build_request_context` with the handler's
        wired StateReader / ResourceResolver overrides AND the
        platform's AccountStore (for D9 round-3 composite cache
        scope-key derivation)."""
        auth_info = self._extract_auth_info(tool_ctx)
        return _build_request_context(
            tool_ctx,
            account,
            auth_info,
            store=self._platform.accounts,
            state_reader=self._state_reader,
            resource_resolver=self._resource_resolver,
        )

    # ----- Sales tools -----

    async def get_products(  # type: ignore[override]
        self,
        params: GetProductsRequest,
        context: ToolContext | None = None,
    ) -> GetProductsResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetProductsResponse",
            await _invoke_platform_method(
                self._platform,
                "get_products",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def create_media_buy(  # type: ignore[override]
        self,
        params: CreateMediaBuyRequest,
        context: ToolContext | None = None,
    ) -> CreateMediaBuySuccessResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "create_media_buy",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("create_media_buy", params, result)
        return cast("CreateMediaBuySuccessResponse", result)

    async def update_media_buy(  # type: ignore[override]
        self,
        params: UpdateMediaBuyRequest,
        context: ToolContext | None = None,
    ) -> UpdateMediaBuySuccessResponse:
        """Wire shape carries ``media_buy_id`` + the patch fields at the
        same level on ``UpdateMediaBuyRequest``. The platform method
        signature is ``update_media_buy(media_buy_id, patch, ctx)`` —
        cleaner adopter ergonomics. Arg-projection per D1.
        """
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "update_media_buy",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
            arg_projector={"media_buy_id": params.media_buy_id, "patch": params},
        )
        self._maybe_auto_emit_sync_completion("update_media_buy", params, result)
        return cast("UpdateMediaBuySuccessResponse", result)

    async def sync_creatives(  # type: ignore[override]
        self,
        params: SyncCreativesRequest,
        context: ToolContext | None = None,
    ) -> SyncCreativesSuccessResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        result = await _invoke_platform_method(
            self._platform,
            "sync_creatives",
            params,
            ctx,
            executor=self._executor,
            registry=self._registry,
        )
        self._maybe_auto_emit_sync_completion("sync_creatives", params, result)
        return cast("SyncCreativesSuccessResponse", result)

    async def get_media_buy_delivery(  # type: ignore[override]
        self,
        params: GetMediaBuyDeliveryRequest,
        context: ToolContext | None = None,
    ) -> GetMediaBuyDeliveryResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetMediaBuyDeliveryResponse",
            await _invoke_platform_method(
                self._platform,
                "get_media_buy_delivery",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    # ----- Optional sales tools (gated by capabilities + override) -----

    async def get_media_buys(  # type: ignore[override]
        self,
        params: GetMediaBuysRequest,
        context: ToolContext | None = None,
    ) -> GetMediaBuysResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "GetMediaBuysResponse",
            await _invoke_platform_method(
                self._platform,
                "get_media_buys",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def provide_performance_feedback(  # type: ignore[override]
        self,
        params: ProvidePerformanceFeedbackRequest,
        context: ToolContext | None = None,
    ) -> ProvidePerformanceFeedbackResponse:
        """Wire request has no ``account`` field — resolve via auth only.
        Adopters in ``explicit`` resolution mode get an
        ``ACCOUNT_NOT_FOUND`` from their AccountStore unless they wire
        a derived/singleton path or extend ``AccountStore.resolve`` to
        handle the no-ref case (see python-port-v2 RFC TODO(rc.1))."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ProvidePerformanceFeedbackResponse",
            await _invoke_platform_method(
                self._platform,
                "provide_performance_feedback",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def list_creative_formats(  # type: ignore[override]
        self,
        params: ListCreativeFormatsRequest,
        context: ToolContext | None = None,
    ) -> ListCreativeFormatsResponse:
        """Wire request has no ``account`` field. See
        :meth:`provide_performance_feedback` for the no-ref account
        resolution caveat."""
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(None, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ListCreativeFormatsResponse",
            await _invoke_platform_method(
                self._platform,
                "list_creative_formats",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )

    async def list_creatives(  # type: ignore[override]
        self,
        params: ListCreativesRequest,
        context: ToolContext | None = None,
    ) -> ListCreativesResponse:
        tool_ctx = context or ToolContext()
        account = await self._resolve_account(params.account, tool_ctx)
        ctx = self._build_ctx(tool_ctx, account)
        return cast(
            "ListCreativesResponse",
            await _invoke_platform_method(
                self._platform,
                "list_creatives",
                params,
                ctx,
                executor=self._executor,
                registry=self._registry,
            ),
        )


__all__ = ["PlatformHandler"]
