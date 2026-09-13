"""Negotiated catalog purchase across AdCP 3.0, 3.1, and 3.2.

This is an SDK-local adoption helper.  It leaves the native task methods alone,
selects either ``list_products``/``buy_products`` or the established
``get_products``/``create_media_buy`` pair, and uses
:class:`LegacyPurchaseCoordinator` to fence established purchases.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import cmp_to_key
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast

from pydantic import BaseModel
from typing_extensions import NotRequired, Required, TypedDict

from adcp._version import normalize_to_release_precision
from adcp.compat.purchase_continuation import (
    CompatibilityPurchaseOperation,
    LegacyPurchaseCoordinator,
    _token_hash,
    canonical_account_identity,
)
from adcp.types import (
    BuyProductsRequest,
    GetAdcpCapabilitiesResponse,
    ListProductsRequest,
)
from adcp.types.core import TaskResult, TaskStatus
from adcp.types.legacy import LegacyGetProductsRequest
from adcp.validation import (
    format_issues,
    get_bundle_adcp_version,
    get_schema,
    validate_request,
    validate_response,
)

if TYPE_CHECKING:
    from adcp.client import ADCPClient

JsonObject: TypeAlias = dict[str, Any]
JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonMapping: TypeAlias = Mapping[str, JsonValue]
LifecyclePreference: TypeAlias = Literal["auto", "compact", "established"]
CompatibilityErrorCode: TypeAlias = Literal["UNSUPPORTED_FEATURE"]


class CoordinatorBuyProductsInput(TypedDict, total=False):
    """Buyer-owned fields for :meth:`MediaBuyLifecycleCoordinator.buy_products`.

    This mirrors the buyer-owned JSON fields of ``BuyProductsRequest``. The
    coordinator owns the negotiated protocol version and injects feed/pricing
    evidence from the bound catalog result, so none of those fields are caller
    inputs.
    """

    idempotency_key: Required[str]
    account: Required[JsonMapping]
    brand: NotRequired[JsonMapping | None]
    advertiser_industry: NotRequired[str | None]
    purchases: Required[list[JsonMapping]]
    total_budget: NotRequired[JsonMapping | None]
    daily_budget_cap: NotRequired[float | None]
    budget_cap_timezone: NotRequired[str | None]
    budget_allocation: NotRequired[JsonMapping | None]
    start_time: Required[str | JsonMapping]
    end_time: Required[str]
    pacing: NotRequired[str | None]
    bidding: NotRequired[JsonMapping | None]
    paused: NotRequired[bool | None]
    purchase_order_ref: NotRequired[str | None]
    agency_estimate_number: NotRequired[str | None]
    invoice_recipient: NotRequired[JsonMapping | None]
    governance_context: NotRequired[str | None]
    push_notification_config: NotRequired[JsonMapping | None]
    reporting_webhook: NotRequired[JsonMapping | None]
    opportunity: NotRequired[JsonMapping | None]
    context: NotRequired[JsonMapping | None]
    ext: NotRequired[JsonMapping | None]


_RELEASE_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<prerelease>[A-Za-z0-9.-]+))?(?:\+[A-Za-z0-9.-]+)?$"
)
_COMPACT_TO_ESTABLISHED = {
    "list_products": "get_products",
    "buy_products": "create_media_buy",
}
_PURCHASE_LOSSES = (
    "feed_version_not_atomic",
    "pricing_version_not_atomic",
)
_MUTATION_LOSS = "mutation_idempotency_not_guaranteed"
_MAX_PRINCIPAL_BYTES = 256
_MIN_IDEMPOTENCY_REPLAY_TTL_SECONDS = 3600
_MAX_IDEMPOTENCY_REPLAY_TTL_SECONDS = 604800


class MediaBuyLifecycle(str, Enum):
    """Wire lifecycle selected for an operation."""

    COMPACT = "compact"
    ESTABLISHED = "established"


class MediaBuyCompatibility(str, Enum):
    """Strength of an operation projection."""

    NATIVE = "native"
    LOSSLESS_PROJECTION = "lossless_projection"
    LOSSY_PROJECTION = "lossy_projection"


@dataclass(frozen=True)
class MediaBuyCompatibilityReport:
    """Exact route and guarantee changes for one coordinator operation."""

    negotiated_version: str
    lifecycle: MediaBuyLifecycle
    tools_used: tuple[str, ...]
    compatibility: MediaBuyCompatibility
    warnings: tuple[str, ...] = ()
    losses: tuple[str, ...] = ()


class MediaBuyLifecycleCompatibilityError(Exception):
    """Structured preflight failure raised before an incompatible dispatch."""

    def __init__(
        self,
        *,
        operation: str,
        negotiated_version: str,
        lifecycle: MediaBuyLifecycle,
        feature: str,
        message: str,
        losses: Sequence[str] = (),
        recovery: str | None = None,
        code: CompatibilityErrorCode = "UNSUPPORTED_FEATURE",
    ) -> None:
        self.code = code
        self.operation = operation
        self.negotiated_version = negotiated_version
        self.lifecycle = lifecycle
        self.feature = feature
        self.losses = tuple(losses)
        self.recovery = recovery or (
            "Remove the unsupported requirement, select a compatible seller, or explicitly "
            "accept the named compatibility loss."
        )
        super().__init__(message)


@dataclass(frozen=True)
class LegacyCatalogContinuation:
    """SDK-local authorization for purchasing from an established catalog result."""

    kind: Literal["legacy_create"]
    token: str = dataclass_field(repr=False)
    losses: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class CompatibleCatalog:
    """Lifecycle-neutral completed catalog page."""

    products: tuple[JsonObject, ...]
    compatibility: MediaBuyCompatibilityReport
    raw: Any
    feed_version: str | None = None
    pricing_version: str | None = None
    next_cursor: str | None = None
    cache_scope: str | None = None
    purchase_continuation: LegacyCatalogContinuation | None = None
    _account: JsonObject | None = None
    _coordinator_identity: int = 0
    _source_schema_version: str | None = None


@dataclass(frozen=True)
class CompatiblePurchaseResult:
    """Lifecycle-neutral wrapper around a native or established purchase result."""

    result: TaskResult[Any] | JsonObject
    compatibility: MediaBuyCompatibilityReport

    @property
    def success(self) -> bool:
        """Whether the selected lifecycle reported a successful purchase."""

        if isinstance(self.result, TaskResult):
            return self.result.success
        return "errors" not in self.result and self.result.get("status") != "failed"

    @property
    def status(self) -> TaskStatus:
        """Normalized terminal status for ordinary direct purchases."""

        if isinstance(self.result, TaskResult):
            return self.result.status
        status = self.result.get("status")
        if not isinstance(status, str):
            return TaskStatus.COMPLETED if self.success else TaskStatus.FAILED
        return {
            "submitted": TaskStatus.SUBMITTED,
            "working": TaskStatus.WORKING,
            "input-required": TaskStatus.NEEDS_INPUT,
            "failed": TaskStatus.FAILED,
        }.get(status, TaskStatus.COMPLETED)

    @property
    def data(self) -> Any:
        """Lifecycle-neutral response data; ``result`` retains the SDK source."""

        if isinstance(self.result, TaskResult):
            return self.result.data
        return self.result


class MediaBuyLifecycleCoordinator:
    """Negotiate one catalog-to-purchase workflow across AdCP 3.x."""

    def __init__(
        self,
        client: ADCPClient,
        capabilities: GetAdcpCapabilitiesResponse,
        advertised_tools: Sequence[str],
        *,
        legacy_purchase_coordinator: LegacyPurchaseCoordinator | None = None,
        principal_id: str | None = None,
        target_binding: str | None = None,
        preferred_lifecycle: LifecyclePreference = "auto",
        allowed_losses: Sequence[str] = (),
        continuation_ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if preferred_lifecycle not in {"auto", "compact", "established"}:
            raise ValueError("preferred_lifecycle must be auto, compact, or established")
        if continuation_ttl <= timedelta(0):
            raise ValueError("continuation_ttl must be positive")
        if principal_id is not None:
            if not isinstance(principal_id, str) or not principal_id.strip():
                raise TypeError("principal_id must be a non-empty string when provided")
            principal_id = principal_id.strip()
            if re.search(r"[\x00-\x1f\x7f]", principal_id):
                raise ValueError("principal_id must not contain control characters")
            if len(principal_id.encode("utf-8")) > _MAX_PRINCIPAL_BYTES:
                raise ValueError(f"principal_id must be at most {_MAX_PRINCIPAL_BYTES} UTF-8 bytes")
        if target_binding is not None:
            if not isinstance(target_binding, str) or not target_binding.strip():
                raise TypeError("target_binding must be a non-empty string when provided")
            target_binding = target_binding.strip()
        if legacy_purchase_coordinator is not None and (not principal_id or not target_binding):
            raise ValueError(
                "compatibility persistence requires non-empty principal_id and target_binding"
            )

        self.client = client
        self.capabilities = capabilities
        self.negotiated_version = _negotiate_version(capabilities, client.get_adcp_version())
        self._tools = frozenset(str(tool) for tool in advertised_tools)
        media_buy = getattr(capabilities, "media_buy", None)
        self._tools = self._tools | frozenset(
            _root_value(tool) for tool in (getattr(media_buy, "lifecycle_tools", None) or ())
        )
        self._buying_modes = frozenset(
            _root_value(mode) for mode in (getattr(media_buy, "buying_modes", None) or ())
        )
        self._preferred_lifecycle = preferred_lifecycle
        self._allowed_losses = frozenset(str(loss) for loss in allowed_losses)
        self._legacy = legacy_purchase_coordinator
        self._principal_id = principal_id
        self._target_binding = target_binding
        self._continuation_ttl = continuation_ttl
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        idempotency = getattr(getattr(capabilities, "adcp", None), "idempotency", None)
        idempotency_supported = getattr(idempotency, "supported", False) is True
        replay_ttl_seconds = getattr(idempotency, "replay_ttl_seconds", None)
        if idempotency_supported and replay_ttl_seconds is not None:
            if (
                isinstance(replay_ttl_seconds, bool)
                or not isinstance(replay_ttl_seconds, int)
                or not _MIN_IDEMPOTENCY_REPLAY_TTL_SECONDS
                <= replay_ttl_seconds
                <= _MAX_IDEMPOTENCY_REPLAY_TTL_SECONDS
            ):
                raise ValueError(
                    "capabilities adcp.idempotency.replay_ttl_seconds must be an integer "
                    f"in [{_MIN_IDEMPOTENCY_REPLAY_TTL_SECONDS}, "
                    f"{_MAX_IDEMPOTENCY_REPLAY_TTL_SECONDS}]"
                )
        self._mutation_idempotency_guaranteed = (
            idempotency_supported and replay_ttl_seconds is not None
        )
        # ``lifecycle`` describes the negotiated protocol family. The complete
        # catalog-purchase route is preflighted when an operation is requested.
        self.lifecycle = (
            MediaBuyLifecycle.COMPACT
            if _is_compact_release(self.negotiated_version)
            else MediaBuyLifecycle.ESTABLISHED
        )

    @classmethod
    async def negotiate(
        cls,
        client: ADCPClient,
        *,
        capabilities: GetAdcpCapabilitiesResponse | None = None,
        advertised_tools: Sequence[str] | None = None,
        legacy_purchase_coordinator: LegacyPurchaseCoordinator | None = None,
        principal_id: str | None = None,
        target_binding: str | None = None,
        preferred_lifecycle: LifecyclePreference = "auto",
        allowed_losses: Sequence[str] = (),
        continuation_ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] | None = None,
    ) -> MediaBuyLifecycleCoordinator:
        """Fetch seller evidence and construct a stable negotiated coordinator."""

        resolved_capabilities = capabilities or await client.fetch_capabilities()
        resolved_tools = (
            tuple(advertised_tools)
            if advertised_tools is not None
            else tuple(await client.list_tools())
        )
        return cls(
            client,
            resolved_capabilities,
            resolved_tools,
            legacy_purchase_coordinator=legacy_purchase_coordinator,
            principal_id=principal_id,
            target_binding=target_binding,
            preferred_lifecycle=preferred_lifecycle,
            allowed_losses=allowed_losses,
            continuation_ttl=continuation_ttl,
            clock=clock,
        )

    def report(self, operation: str | None = None) -> MediaBuyCompatibilityReport:
        """Describe a viable operation route without claiming a dispatch.

        With no operation this reports only the negotiated protocol family.
        Passing ``list_products`` or ``buy_products`` performs the same route
        preflight as that operation while leaving ``tools_used`` empty.
        """

        lifecycle = self.lifecycle
        if operation is not None:
            if operation not in _COMPACT_TO_ESTABLISHED:
                raise ValueError("operation must be list_products or buy_products")
            lifecycle = self._select_lifecycle(operation)
        return self._report(lifecycle, ())

    async def list_products(
        self,
        request: ListProductsRequest,
        *,
        issuance_idempotency_key: str | None = None,
    ) -> CompatibleCatalog:
        """List a catalog through the negotiated lifecycle.

        Established results issue a short-lived SDK continuation.  They never
        synthesize ``feed_version`` or ``pricing_version`` values.
        """

        request_payload = request.model_dump(mode="json", exclude_none=True)
        conditional = tuple(
            field for field in ("if_feed_version", "if_pricing_version") if field in request_payload
        )
        if conditional:
            raise self._unsupported(
                "list_products",
                self.lifecycle,
                "conditional_catalog_reuse",
                "The coordinator cannot safely bind a purchase to an unchanged response; "
                "omit " + ", ".join(conditional) + " and request a complete catalog page.",
            )
        lifecycle = self._select_lifecycle("list_products")
        account = _json_object(request.account)
        if lifecycle is MediaBuyLifecycle.COMPACT:
            pinned = request.model_copy(update={"adcp_version": self.negotiated_version})
            result = await self.client.list_products(pinned)
            payload = self._completed_payload("list_products", result, lifecycle)
            self._validate_served_version("list_products", payload, lifecycle)
            products = _products(payload, operation="list_products", coordinator=self)
            feed_version = _optional_text(payload.get("feed_version"))
            if feed_version is None:
                raise self._unsupported(
                    "list_products",
                    lifecycle,
                    "feed_version",
                    "The compact seller did not return the required real feed_version.",
                )
            return CompatibleCatalog(
                products=products,
                feed_version=feed_version,
                pricing_version=_optional_text(payload.get("pricing_version")),
                next_cursor=_optional_text(payload.get("next_cursor")),
                cache_scope=_optional_text(payload.get("cache_scope")),
                compatibility=self._report(lifecycle, ("list_products",)),
                raw=result,
                _account=account,
                _coordinator_identity=id(self),
                _source_schema_version=self.negotiated_version,
            )

        legacy = self._require_legacy("list_products", lifecycle)
        if account is None:
            raise self._unsupported(
                "list_products",
                lifecycle,
                "account",
                "Established catalog purchase coordination requires an account-bound listing.",
            )
        issue_key = issuance_idempotency_key or request.idempotency_key
        if not issue_key:
            raise self._unsupported(
                "list_products",
                lifecycle,
                "issuance_idempotency_key",
                "Established listing requires a stable issuance_idempotency_key.",
            )
        wire_request = self._legacy_list_request(request)
        source_version = self._exact_source_schema_version("list_products", lifecycle)
        try:
            _assert_declared_legacy_fields(
                wire_request,
                tool="get_products",
                source_version=source_version,
                nested=("filters",),
            )
        except ValueError as exc:
            raise self._unsupported(
                "list_products",
                lifecycle,
                "legacy_request_projection",
                str(exc),
            ) from exc
        validation = validate_request("get_products", wire_request, version=source_version)
        if not validation.valid or validation.variant == "skipped":
            raise self._unsupported(
                "list_products",
                lifecycle,
                "legacy_request_projection",
                "The listing requirements cannot be represented by the negotiated "
                f"{self.negotiated_version} get_products contract.",
            )
        model = LegacyGetProductsRequest.model_validate(wire_request)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy_result = await self.client.get_products_legacy(model)
        payload = self._completed_payload("list_products", legacy_result, lifecycle)
        self._validate_served_version("list_products", payload, lifecycle)
        response_validation = validate_response("get_products", payload, version=source_version)
        if not response_validation.valid or response_validation.variant == "skipped":
            raise self._unsupported(
                "list_products",
                lifecycle,
                "response_projection",
                "Established get_products returned an invalid exact-version response: "
                f"{format_issues(response_validation.issues)}",
            )
        products = _products(payload, operation="list_products", coordinator=self)
        continuation = (
            await self._issue_legacy_continuation(
                legacy=legacy,
                issuance_idempotency_key=issue_key,
                account=account,
                source_version=source_version,
                observed_request=wire_request,
                observed_response=payload,
                products=products,
            )
            if products
            else None
        )
        return CompatibleCatalog(
            products=products,
            feed_version=_optional_text(payload.get("wholesale_feed_version")),
            pricing_version=_optional_text(payload.get("pricing_version")),
            next_cursor=_optional_text(_mapping(payload.get("pagination")).get("cursor")),
            cache_scope=_optional_text(payload.get("cache_scope")),
            purchase_continuation=continuation,
            compatibility=self._report(lifecycle, ("get_products",)),
            raw=legacy_result,
            _account=account,
            _coordinator_identity=id(self),
            _source_schema_version=source_version,
        )

    async def buy_products(
        self,
        listing: CompatibleCatalog,
        request: CoordinatorBuyProductsInput | BaseModel,
        *,
        accepted_losses: Sequence[str] | None = None,
    ) -> CompatiblePurchaseResult:
        """Purchase selected products without changing either native signature."""

        self._validate_listing(listing)
        lifecycle = self._select_lifecycle("buy_products")
        if lifecycle is not listing.compatibility.lifecycle:
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "lifecycle_changed",
                "The catalog route no longer matches the purchase route; restart discovery.",
            )
        payload = _json_object(request)
        assert payload is not None
        # Feed and pricing versions are seller-issued listing evidence. Never
        # accept caller substitutions, including when the listing omitted an
        # independent pricing version.
        payload.pop("feed_version", None)
        payload.pop("pricing_version", None)
        account = _mapping(payload.get("account"))
        if listing._account is not None and (
            not account
            or canonical_account_identity(account) != canonical_account_identity(listing._account)
        ):
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "account_binding",
                "The purchase account does not match the account used for listing.",
            )
        purchases = payload.get("purchases")
        try:
            selected_ids = _selected_product_ids(
                purchases, unique=lifecycle is MediaBuyLifecycle.ESTABLISHED
            )
        except ValueError as exc:
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "purchase_selection",
                str(exc),
            ) from exc
        listed_ids = {product["product_id"] for product in listing.products}
        if not set(selected_ids).issubset(listed_ids):
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "selection_binding",
                "The purchase selects a product that was not present in this catalog result.",
            )

        if lifecycle is MediaBuyLifecycle.COMPACT:
            if listing.feed_version is None:
                raise self._unsupported(
                    "buy_products",
                    lifecycle,
                    "feed_version",
                    "A compact purchase requires the seller's real listing feed_version.",
                )
            compact_payload = {
                **payload,
                "feed_version": listing.feed_version,
                "adcp_version": self.negotiated_version,
            }
            if listing.pricing_version is not None:
                compact_payload["pricing_version"] = listing.pricing_version
            model = BuyProductsRequest.model_validate(compact_payload)
            result = await self.client.buy_products(model)
            return CompatiblePurchaseResult(
                result=result,
                compatibility=self._report(lifecycle, ("buy_products",)),
            )

        legacy = self._require_legacy("buy_products", lifecycle)
        if not _optional_text(payload.get("idempotency_key")):
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "idempotency_key",
                "Established purchase requires a non-empty idempotency_key.",
            )
        continuation = listing.purchase_continuation
        if continuation is None:
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "purchase_continuation",
                "The established listing has no SDK purchase continuation.",
            )
        exact_losses = tuple(continuation.losses if accepted_losses is None else accepted_losses)
        if set(exact_losses) != set(continuation.losses) or len(exact_losses) != len(
            continuation.losses
        ):
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "accepted_losses",
                "accepted_losses must exactly match the listing continuation losses.",
                losses=continuation.losses,
            )
        refused = tuple(loss for loss in continuation.losses if loss not in self._allowed_losses)
        if refused:
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "compatibility_losses",
                "No purchase was sent because required compatibility losses were not allowed.",
                losses=refused,
            )
        source_version = listing._source_schema_version
        if source_version is None:
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "served_version_schema_unavailable",
                "The established listing did not retain its exact source schema version.",
            )
        try:
            legacy_create = _legacy_create_request(payload, self.negotiated_version, source_version)
        except ValueError as exc:
            raise self._unsupported(
                "buy_products",
                lifecycle,
                "legacy_request_projection",
                str(exc),
            ) from exc
        legacy_result = await legacy.continue_legacy_purchase(
            {
                "idempotency_key": payload.get("idempotency_key"),
                "continuation_token": continuation.token,
                "account": account,
                "selected_product_ids": selected_ids,
                "accepted_losses": list(exact_losses),
                "legacy_create_request": legacy_create,
            },
            principal_id=cast(str, self._principal_id),
            target_binding=cast(str, self._target_binding),
        )
        return CompatiblePurchaseResult(
            result=legacy_result,
            compatibility=self._report(
                lifecycle,
                ("create_media_buy",),
                losses=continuation.losses,
                warnings=(
                    "The established mutation cannot atomically fence the listed feed or pricing.",
                ),
            ),
        )

    async def recover_legacy_purchase(
        self, operation: CompatibilityPurchaseOperation
    ) -> CompatiblePurchaseResult:
        """Resume a fenced established purchase through the configured reconciler."""

        lifecycle = MediaBuyLifecycle.ESTABLISHED
        legacy = self._require_legacy("recover_legacy_purchase", lifecycle)
        record = await legacy.store.get_continuation(
            operation.token_hash,
            principal_id=cast(str, self._principal_id),
        )
        losses = (
            tuple(loss for loss in (*_PURCHASE_LOSSES, _MUTATION_LOSS) if loss in record.losses)
            if record is not None
            else ()
        )
        result = await legacy.recover_legacy_purchase(
            operation,
            principal_id=cast(str, self._principal_id),
            target_binding=cast(str, self._target_binding),
        )
        return CompatiblePurchaseResult(
            result=result,
            compatibility=self._report(
                lifecycle,
                ("create_media_buy",),
                losses=losses,
                warnings=(
                    "The recovered established mutation did not gain atomic feed or pricing "
                    "fencing.",
                ),
            ),
        )

    async def continue_legacy_purchase(
        self,
        continuation: LegacyCatalogContinuation | str,
        request: CoordinatorBuyProductsInput | BaseModel,
        *,
        accepted_losses: Sequence[str] | None = None,
    ) -> CompatiblePurchaseResult:
        """Redeem an established catalog continuation after coordinator restart."""

        lifecycle = self._select_lifecycle("buy_products")
        if lifecycle is not MediaBuyLifecycle.ESTABLISHED:
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "legacy_continuation",
                "A legacy catalog continuation can only be redeemed on the established lane.",
            )
        token = (
            continuation.token
            if isinstance(continuation, LegacyCatalogContinuation)
            else continuation
        )
        if not isinstance(token, str) or not token:
            raise ValueError("continuation token must be non-empty")
        payload = _json_object(request)
        assert payload is not None
        payload.pop("feed_version", None)
        payload.pop("pricing_version", None)
        account = _mapping(payload.get("account"))
        if not account:
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "account_binding",
                "Established continuation redemption requires an account.",
            )
        if not _optional_text(payload.get("idempotency_key")):
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "idempotency_key",
                "Established continuation redemption requires a non-empty idempotency_key.",
            )
        try:
            selected_ids = _selected_product_ids(payload.get("purchases"), unique=True)
        except ValueError as exc:
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "purchase_selection",
                str(exc),
            ) from exc
        legacy = self._require_legacy("continue_legacy_purchase", lifecycle)
        record = await legacy.store.get_continuation(
            _token_hash(token), principal_id=cast(str, self._principal_id)
        )
        if record is None:
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "purchase_continuation",
                "No continuation exists in this authenticated principal scope.",
            )
        source_version = self._exact_source_schema_version("continue_legacy_purchase", lifecycle)
        if record.source_adcp_version != source_version:
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "served_version_changed",
                "The continuation was issued under a different served AdCP contract; "
                "reconstruct the coordinator with the original seller binding.",
            )
        loss_order = (*_PURCHASE_LOSSES, _MUTATION_LOSS)
        losses = tuple(loss for loss in loss_order if loss in record.losses)
        if set(losses) != set(record.losses):
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "compatibility_losses",
                "The continuation contains an unsupported compatibility loss set.",
            )
        exact_losses = tuple(losses if accepted_losses is None else accepted_losses)
        if set(exact_losses) != set(losses) or len(exact_losses) != len(losses):
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "accepted_losses",
                "accepted_losses must exactly match the established continuation losses.",
                losses=losses,
            )
        refused = tuple(loss for loss in losses if loss not in self._allowed_losses)
        if refused:
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "compatibility_losses",
                "No purchase was sent because required compatibility losses were not allowed.",
                losses=refused,
            )
        try:
            legacy_create = _legacy_create_request(payload, self.negotiated_version, source_version)
        except ValueError as exc:
            raise self._unsupported(
                "continue_legacy_purchase",
                lifecycle,
                "legacy_request_projection",
                str(exc),
            ) from exc
        result = await legacy.continue_legacy_purchase(
            {
                "idempotency_key": payload["idempotency_key"],
                "continuation_token": token,
                "account": account,
                "selected_product_ids": selected_ids,
                "accepted_losses": list(exact_losses),
                "legacy_create_request": legacy_create,
            },
            principal_id=cast(str, self._principal_id),
            target_binding=cast(str, self._target_binding),
        )
        return CompatiblePurchaseResult(
            result=result,
            compatibility=self._report(
                lifecycle,
                ("create_media_buy",),
                losses=losses,
                warnings=(
                    "The established mutation cannot atomically fence the listed feed or pricing.",
                ),
            ),
        )

    async def _issue_legacy_continuation(
        self,
        *,
        legacy: LegacyPurchaseCoordinator,
        issuance_idempotency_key: str,
        account: JsonObject,
        source_version: str,
        observed_request: JsonObject,
        observed_response: JsonObject,
        products: Sequence[JsonObject],
    ) -> LegacyCatalogContinuation:
        losses = list(_PURCHASE_LOSSES)
        if not self._mutation_idempotency_guaranteed:
            losses.append(_MUTATION_LOSS)
        expires_at = _aware_utc(self._clock()) + self._continuation_ttl
        token = await legacy.issue_legacy_create_continuation(
            principal_id=cast(str, self._principal_id),
            issuance_idempotency_key=issuance_idempotency_key,
            account=account,
            source_adcp_version=source_version,
            expires_at=expires_at,
            observed_request=observed_request,
            observed_response=observed_response,
            product_ids=[product["product_id"] for product in products],
            buyer_visible_products=list(products),
            losses=losses,
            target_binding=cast(str, self._target_binding),
            mutation_idempotency_guaranteed=self._mutation_idempotency_guaranteed,
            listed_purchase_context={
                "negotiated_version": self.negotiated_version,
                "tools_used": ["get_products"],
            },
        )
        return LegacyCatalogContinuation(
            kind="legacy_create",
            token=token,
            losses=tuple(losses),
            expires_at=expires_at,
        )

    def _select_lifecycle(self, operation: str) -> MediaBuyLifecycle:
        return self._select_catalog_purchase_lifecycle(operation)

    def _select_catalog_purchase_lifecycle(self, operation: str) -> MediaBuyLifecycle:
        compact_contract = _is_compact_release(self.negotiated_version)
        _, release_minor = _release_minor(self.negotiated_version)
        # The 3.0 capabilities contract did not expose buying_modes. Wholesale
        # support is therefore discovered by the call on that exact legacy
        # contract; 3.1+ has an explicit capability and must advertise it.
        wholesale_supported = release_minor == 0 or "wholesale" in self._buying_modes
        compact_pair = compact_contract and {
            "list_products",
            "buy_products",
        }.issubset(self._tools)
        established_pair = (
            not compact_contract
            and {
                "get_products",
                "create_media_buy",
            }.issubset(self._tools)
            and wholesale_supported
        )

        if self._preferred_lifecycle == "compact":
            if compact_pair:
                return MediaBuyLifecycle.COMPACT
            raise self._unsupported(
                operation,
                MediaBuyLifecycle.COMPACT,
                "compact_catalog_purchase_not_advertised",
                "A compact catalog purchase requires both list_products and buy_products.",
            )
        if self._preferred_lifecycle == "established":
            if established_pair:
                return MediaBuyLifecycle.ESTABLISHED
            raise self._unsupported(
                operation,
                MediaBuyLifecycle.ESTABLISHED,
                "established_catalog_purchase_not_advertised",
                "An established catalog purchase requires get_products, create_media_buy, "
                "and the wholesale buying mode.",
            )
        if compact_pair:
            return MediaBuyLifecycle.COMPACT
        if established_pair:
            return MediaBuyLifecycle.ESTABLISHED
        raise self._unsupported(
            operation,
            MediaBuyLifecycle.COMPACT if compact_contract else MediaBuyLifecycle.ESTABLISHED,
            "catalog_purchase_route_not_advertised",
            "The seller advertises no complete compact or established catalog-purchase route.",
        )

    def _legacy_list_request(self, request: ListProductsRequest) -> JsonObject:
        value = request.model_dump(mode="json", exclude_none=True)
        criteria = _mapping(value.get("criteria"))
        unsupported = set(criteria) - {"offer_filters"}
        if unsupported:
            raise self._unsupported(
                "list_products",
                MediaBuyLifecycle.ESTABLISHED,
                "criteria",
                "Established listing cannot preserve criteria fields: "
                + ", ".join(sorted(unsupported)),
            )
        for field in ("governance_context", "context_id"):
            if field in value:
                raise self._unsupported(
                    "list_products",
                    MediaBuyLifecycle.ESTABLISHED,
                    field,
                    f"Established listing cannot preserve {field}.",
                )
        wire: JsonObject = {
            "adcp_version": self.negotiated_version,
            "adcp_major_version": 3,
            "buying_mode": "wholesale",
        }
        for field in ("account", "brand", "context", "push_notification_config"):
            if field in value:
                wire[field] = value[field]
        if "offer_filters" in criteria:
            wire["filters"] = criteria["offer_filters"]
        if "fields" in value:
            wire["fields"] = value["fields"]
        pagination = {key: value[key] for key in ("cursor", "max_results") if key in value}
        if pagination:
            wire["pagination"] = pagination
        if "if_feed_version" in value:
            wire["if_wholesale_feed_version"] = value["if_feed_version"]
        if "if_pricing_version" in value:
            wire["if_pricing_version"] = value["if_pricing_version"]
        return wire

    def _completed_payload(
        self,
        operation: str,
        result: TaskResult[Any],
        lifecycle: MediaBuyLifecycle,
    ) -> JsonObject:
        if not result.success or result.status is not TaskStatus.COMPLETED or result.data is None:
            raise self._unsupported(
                operation,
                lifecycle,
                "completed_catalog_required",
                f"{operation} did not return a completed successful response: "
                f"{result.error or result.message or result.status.value}",
            )
        return cast(
            JsonObject,
            result.data.model_dump(mode="json", exclude_none=True, exclude_unset=True),
        )

    def _validate_served_version(
        self, operation: str, payload: JsonObject, lifecycle: MediaBuyLifecycle
    ) -> None:
        served = payload.get("adcp_version")
        if served is None:
            # The capabilities response is the served-contract binding. Some
            # task response schemas do not repeat the envelope field.
            return
        if not isinstance(served, str) or _parse_release(served) is None:
            raise self._unsupported(
                operation,
                lifecycle,
                "served_version_invalid",
                "The seller returned an invalid response adcp_version.",
            )
        normalized = normalize_to_release_precision(served)
        if normalized != self.negotiated_version:
            raise self._unsupported(
                operation,
                lifecycle,
                "served_version_mismatch",
                f"The seller served AdCP {normalized}, but "
                f"{self.negotiated_version} was negotiated.",
            )

    def _exact_source_schema_version(self, operation: str, lifecycle: MediaBuyLifecycle) -> str:
        exact = get_bundle_adcp_version(version=self.negotiated_version)
        if exact is None:
            raise self._unsupported(
                operation,
                lifecycle,
                "served_version_schema_unavailable",
                f"No bundled schema can validate AdCP {self.negotiated_version}.",
            )
        return exact

    def _validate_listing(self, listing: CompatibleCatalog) -> None:
        if not isinstance(listing, CompatibleCatalog) or listing._coordinator_identity != id(self):
            raise TypeError("listing must be a CompatibleCatalog issued by this coordinator")

    def _require_legacy(
        self, operation: str, lifecycle: MediaBuyLifecycle
    ) -> LegacyPurchaseCoordinator:
        if self._legacy is None:
            raise self._unsupported(
                operation,
                lifecycle,
                "legacy_purchase_coordinator",
                "Established purchase coordination requires a LegacyPurchaseCoordinator.",
            )
        return self._legacy

    def _report(
        self,
        lifecycle: MediaBuyLifecycle,
        tools: Sequence[str],
        *,
        losses: Sequence[str] = (),
        warnings: Sequence[str] = (),
    ) -> MediaBuyCompatibilityReport:
        compatibility = (
            MediaBuyCompatibility.NATIVE
            if lifecycle is MediaBuyLifecycle.COMPACT
            else (
                MediaBuyCompatibility.LOSSY_PROJECTION
                if losses
                else MediaBuyCompatibility.LOSSLESS_PROJECTION
            )
        )
        return MediaBuyCompatibilityReport(
            negotiated_version=self.negotiated_version,
            lifecycle=lifecycle,
            tools_used=tuple(tools),
            compatibility=compatibility,
            warnings=tuple(warnings),
            losses=tuple(losses),
        )

    def _unsupported(
        self,
        operation: str,
        lifecycle: MediaBuyLifecycle,
        feature: str,
        message: str,
        *,
        losses: Sequence[str] = (),
        code: CompatibilityErrorCode = "UNSUPPORTED_FEATURE",
    ) -> MediaBuyLifecycleCompatibilityError:
        return MediaBuyLifecycleCompatibilityError(
            operation=operation,
            negotiated_version=self.negotiated_version,
            lifecycle=lifecycle,
            feature=feature,
            message=message,
            losses=losses,
            code=code,
        )


# The issue originally used this narrower name. Keep it as a public alias so
# adopters can describe the first slice without committing to a second API.
NegotiatedCatalogBuyer = MediaBuyLifecycleCoordinator


def _negotiate_version(capabilities: Any, client_version: str) -> str:
    client = normalize_to_release_precision(client_version)
    client_parsed = _parse_release(client)
    if client_parsed is None:
        raise ValueError(f"Client AdCP pin {client!r} is not semver-shaped.")
    adcp = getattr(capabilities, "adcp", None)
    raw_advertised = tuple(getattr(adcp, "supported_versions", None) or ())
    advertised = [
        normalize_to_release_precision(str(_root_value(value)))
        for value in raw_advertised
        if _parse_release(str(_root_value(value))) is not None
    ]
    served = getattr(capabilities, "adcp_version", None)
    if isinstance(served, str) and served:
        normalized = normalize_to_release_precision(served)
        served_parsed = _parse_release(normalized)
        if served_parsed is None or served_parsed[0] != client_parsed[0]:
            raise ValueError(
                f"Seller served cross-major AdCP {normalized} for client pin {client}."
            )
        if _compare_release(normalized, client) > 0:
            raise ValueError(
                f"Seller served AdCP {normalized}, newer than client pin {client}; "
                "the capabilities response cannot be interpreted safely."
            )
        if advertised and normalized not in advertised:
            raise ValueError(
                f"Seller served AdCP {normalized}, which is absent from its supported_versions."
            )
        if get_bundle_adcp_version(version=normalized) is None:
            raise ValueError(f"No bundled schema can validate served AdCP {normalized}.")
        return normalized

    candidates = [
        version
        for version in advertised
        if _release_minor(version)[0] == client_parsed[0]
        and _compare_release(version, client) <= 0
        and get_bundle_adcp_version(version=version) is not None
    ]
    if candidates:
        return sorted(candidates, key=cmp_to_key(_compare_release))[-1]
    if raw_advertised:
        raise ValueError(
            f"Seller advertises no bundled version compatible with client pin {client}."
        )
    # Release-precision discovery was added after 3.0. An otherwise valid
    # v3 capabilities response with no served/supported release stays on 3.0.
    if client_parsed[0] != 3 or get_bundle_adcp_version(version="3.0") is None:
        raise ValueError(
            f"No safe unversioned compatibility default exists for client pin {client}."
        )
    return "3.0"


def _parse_release(value: str) -> tuple[int, int, int, tuple[str, ...] | None] | None:
    match = _RELEASE_RE.fullmatch(value.strip())
    if match is None:
        return None
    prerelease = match.group("prerelease")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch") or 0),
        tuple(prerelease.split(".")) if prerelease is not None else None,
    )


def _compare_release(left: str, right: str) -> int:
    a = _parse_release(left)
    b = _parse_release(right)
    if a is None or b is None:
        raise ValueError("AdCP versions must be semver-shaped")
    if a[:3] != b[:3]:
        return (a[:3] > b[:3]) - (a[:3] < b[:3])
    return _compare_prerelease(a[3], b[3])


def _compare_prerelease(left: tuple[str, ...] | None, right: tuple[str, ...] | None) -> int:
    if left is None:
        return 0 if right is None else 1
    if right is None:
        return -1
    for a, b in zip(left, right):
        if a == b:
            continue
        a_numeric = a.isdigit()
        b_numeric = b.isdigit()
        if a_numeric and b_numeric:
            return (int(a) > int(b)) - (int(a) < int(b))
        if a_numeric != b_numeric:
            return -1 if a_numeric else 1
        return (a > b) - (a < b)
    return (len(left) > len(right)) - (len(left) < len(right))


def _release_minor(value: str) -> tuple[int, int]:
    parsed = _parse_release(value)
    if parsed is None:
        raise ValueError(f"invalid AdCP version {value!r}")
    return parsed[0], parsed[1]


def _is_compact_release(value: str) -> bool:
    major, minor = _release_minor(value)
    return major == 3 and minor >= 2


def _root_value(value: Any) -> str:
    return str(getattr(value, "root", getattr(value, "value", value)))


def _mapping(value: Any) -> JsonObject:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_object(value: Any) -> JsonObject | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        if not isinstance(dumped, dict):
            raise TypeError("request must serialize to an object")
        return dumped
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("request must be a mapping or Pydantic model")


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _products(
    payload: JsonObject,
    *,
    operation: str,
    coordinator: MediaBuyLifecycleCoordinator,
) -> tuple[JsonObject, ...]:
    values = payload.get("products")
    if not isinstance(values, list):
        raise coordinator._unsupported(
            operation,
            coordinator._select_lifecycle(operation),
            "products",
            "A completed catalog page must contain a products array.",
        )
    products: list[JsonObject] = []
    for value in values:
        if not isinstance(value, Mapping) or not _optional_text(value.get("product_id")):
            raise coordinator._unsupported(
                operation,
                coordinator._select_lifecycle(operation),
                "product_id",
                "Every catalog product must carry a non-empty product_id.",
            )
        products.append(dict(value))
    return tuple(products)


def _optional_rows(value: Any, *, id_field: str) -> tuple[JsonObject, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{id_field.removesuffix('_id')} collection must be an array")
    rows: list[JsonObject] = []
    for item in value:
        if not isinstance(item, Mapping) or not _optional_text(item.get(id_field)):
            raise ValueError(f"every row must carry a non-empty {id_field}")
        rows.append(dict(item))
    return tuple(rows)


def _selected_product_ids(value: Any, *, unique: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("purchases must be a non-empty array")
    selected: list[str] = []
    for purchase in value:
        if not isinstance(purchase, Mapping) or not _optional_text(purchase.get("product_id")):
            raise ValueError("every purchase must carry a non-empty product_id")
        if not _optional_text(purchase.get("pricing_option_id")):
            raise ValueError("every purchase must carry a non-empty pricing_option_id")
        selected.append(cast(str, purchase["product_id"]))
    if not unique:
        return tuple(selected)
    return tuple(dict.fromkeys(selected))


def _legacy_create_request(
    payload: JsonObject, negotiated_version: str, source_version: str
) -> JsonObject:
    unsupported = {
        field for field in ("feed_version", "pricing_version", "ext") if field in payload
    }
    if unsupported:
        raise ValueError("legacy purchase cannot project fields: " + ", ".join(sorted(unsupported)))
    fields = {
        "idempotency_key",
        "account",
        "brand",
        "start_time",
        "end_time",
        "total_budget",
        "daily_budget_cap",
        "budget_cap_timezone",
        "budget_allocation",
        "pacing",
        "bidding",
        "paused",
        "advertiser_industry",
        "agency_estimate_number",
        "invoice_recipient",
        "governance_context",
        "opportunity",
        "reporting_webhook",
        "push_notification_config",
        "context",
    }
    unknown = (
        set(payload)
        - fields
        - {
            "purchases",
            "purchase_order_ref",
            "feed_version",
            "pricing_version",
            "ext",
            "adcp_version",
            "adcp_major_version",
        }
    )
    if unknown:
        raise ValueError(
            "legacy purchase has no declared projection for fields: " + ", ".join(sorted(unknown))
        )
    request: JsonObject = {
        "adcp_version": negotiated_version,
        "adcp_major_version": 3,
        "packages": payload["purchases"],
    }
    for field in fields:
        if field in payload:
            request[field] = payload[field]
    if "purchase_order_ref" in payload:
        request["po_number"] = payload["purchase_order_ref"]
    _assert_declared_legacy_fields(
        request,
        tool="create_media_buy",
        source_version=source_version,
        nested=("packages",),
    )
    return request


def _assert_declared_legacy_fields(
    payload: JsonObject,
    *,
    tool: str,
    source_version: str,
    nested: Sequence[str] = (),
) -> None:
    schema = get_schema(tool, "request", version=source_version)
    if not isinstance(schema, dict):
        raise ValueError(f"No bundled {source_version} {tool} request schema is available.")
    properties = _mapping(schema.get("properties"))
    undeclared = (
        set(payload)
        - set(properties)
        - {
            "adcp_version",
            "adcp_major_version",
        }
    )
    if undeclared:
        raise ValueError(
            f"The {source_version} {tool} contract does not declare fields: "
            + ", ".join(sorted(undeclared))
        )
    for name in nested:
        value = payload.get(name)
        child_schema = _mapping(properties.get(name))
        if name == "packages":
            rows = value if isinstance(value, list) else []
            item_properties = _mapping(_mapping(child_schema.get("items")).get("properties"))
            for index, row in enumerate(rows):
                if not isinstance(row, Mapping):
                    continue
                child_undeclared = set(row) - set(item_properties)
                if child_undeclared:
                    raise ValueError(
                        f"The {source_version} {tool} contract does not declare "
                        f"{name}[{index}] fields: " + ", ".join(sorted(child_undeclared))
                    )
            continue
        if isinstance(value, Mapping):
            child_properties = _mapping(child_schema.get("properties"))
            child_undeclared = set(value) - set(child_properties)
            if child_undeclared:
                raise ValueError(
                    f"The {source_version} {tool} contract does not declare {name} fields: "
                    + ", ".join(sorted(child_undeclared))
                )


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


__all__ = [
    "CompatibleCatalog",
    "CompatiblePurchaseResult",
    "CoordinatorBuyProductsInput",
    "LegacyCatalogContinuation",
    "MediaBuyCompatibility",
    "MediaBuyCompatibilityReport",
    "MediaBuyLifecycle",
    "MediaBuyLifecycleCompatibilityError",
    "MediaBuyLifecycleCoordinator",
    "NegotiatedCatalogBuyer",
]
