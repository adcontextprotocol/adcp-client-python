"""Negotiated buyer lifecycle across AdCP 3.0, 3.1, and 3.2.

This is an SDK-local adoption helper.  It leaves the native task methods alone,
selects either ``list_products``/``buy_products`` or the established
``get_products``/``create_media_buy`` pair, and uses
:class:`LegacyPurchaseCoordinator` to fence established purchases.
Proposal discovery similarly selects ``request_proposals`` or established
``get_products`` while preserving task status and immutable buyer evidence.
Operational controls select ``control_media_buy`` or the exact representable
``update_media_buy`` subset.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import cmp_to_key
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeAlias, TypeVar, cast

import rfc8785
from pydantic import BaseModel

from adcp._version import normalize_to_release_precision
from adcp.compat.proposal_evidence import (
    ESTABLISHED_PROPOSAL_MAX_PRINCIPAL_BYTES,
    EstablishedProposalAcceptanceRecoveryStore,
    EstablishedProposalAcceptanceStore,
    EstablishedProposalEvidence,
    EstablishedProposalEvidenceStore,
    EstablishedProposalMutationStore,
    InMemoryEstablishedProposalEvidenceStore,
    ProposalAcceptanceRecord,
    ProposalAcceptanceState,
    ProposalEvidenceChangedError,
    ProposalEvidencePolicyError,
    ProposalMutationConflictError,
    ProposalMutationKind,
    ProposalMutationRecord,
    ProposalMutationReservation,
)
from adcp.compat.purchase_continuation import (
    CompatibilityContinuationError,
    CompatibilityPurchaseOperation,
    LegacyPurchaseCoordinator,
    _token_hash,
    _validate_persistable_payload,
    canonical_account_identity,
)
from adcp.compat.reporting_version import beta6_delivery_request_issue
from adcp.negotiation import verify_terms_digest
from adcp.types import (
    AcceptProposalRequest,
    BuyProductsRequest,
    ControlMediaBuyRequest,
    DeclineProposalsRequest,
    GetAdcpCapabilitiesResponse,
    GetMediaBuyDeliveryRequest,
    GetMediaBuysRequest,
    GetTaskStatusRequest,
    ListProductsRequest,
    RefineProposalsRequest,
    RequestProposalsRequest,
)
from adcp.types.core import TaskResult, TaskStatus
from adcp.types.legacy import (
    LegacyCreateMediaBuyRequest,
    LegacyGetProductsRequest,
    LegacyUpdateMediaBuyRequest,
)
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
LifecyclePreference: TypeAlias = Literal["auto", "compact", "established"]
ProposalOutcome: TypeAlias = Literal[
    "proposed", "products_available", "rejected", "legacy_unavailable"
]
CompatibilityErrorCode: TypeAlias = Literal["UNSUPPORTED_FEATURE", "PROPOSAL_DIGEST_MISMATCH"]
T = TypeVar("T")

_RELEASE_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<prerelease>[A-Za-z0-9.-]+))?(?:\+[A-Za-z0-9.-]+)?$"
)
_COMPACT_TO_ESTABLISHED = {
    "accept_proposal": "create_media_buy",
    "list_products": "get_products",
    "request_proposals": "get_products",
    "buy_products": "create_media_buy",
    "control_media_buy": "update_media_buy",
    "decline_proposals": "get_products",
    "refine_proposals": "get_products",
}
_PURCHASE_LOSSES = (
    "feed_version_not_atomic",
    "pricing_version_not_atomic",
)
_MUTATION_LOSS = "mutation_idempotency_not_guaranteed"
_PROPOSAL_SNAPSHOT_LOSS = "proposal_snapshot_not_immutable"
_PROPOSAL_DIGEST_NOT_ENFORCED_LOSS = "proposal_terms_digest_not_enforced"
_PROPOSAL_DIGEST_UNAVAILABLE_LOSS = "proposal_terms_digest_unavailable"
_PROPOSAL_HOLD_LOSS = "proposal_hold_not_verifiable"
_DECLINE_LOSSES = (
    "proposal_decline_not_terminal",
    "proposal_decline_reason_not_forwarded",
)
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


@dataclass(frozen=True)
class CompatibleProposalResponse:
    """Lifecycle-neutral completed proposal-discovery response."""

    operation: Literal["request"]
    outcome: ProposalOutcome
    proposals: tuple[JsonObject, ...]
    products: tuple[JsonObject, ...]
    raw: Any
    reason: str | None = None
    suggestions: tuple[Any, ...] = ()
    incomplete: tuple[JsonObject, ...] = ()
    context: JsonObject | None = None
    purchase_continuation: LegacyCatalogContinuation | JsonObject | None = None
    evidence: tuple[EstablishedProposalEvidence, ...] = ()


@dataclass(frozen=True)
class CompatibleRefineProposalsResponse:
    """Lifecycle-neutral proposal refinement completion."""

    operation: Literal["refine"]
    outcome: Literal["native_results", "legacy_projected", "legacy_unavailable"]
    proposals: tuple[JsonObject, ...]
    products: tuple[JsonObject, ...]
    results: tuple[JsonObject, ...]
    raw: JsonObject
    evidence: tuple[EstablishedProposalEvidence, ...] = ()


@dataclass(frozen=True)
class CompatibleDeclineProposalsResponse:
    """Lifecycle-neutral proposal decline completion."""

    operation: Literal["decline"]
    outcome: Literal["native_results", "legacy_unconfirmed"]
    results: tuple[JsonObject, ...]
    raw: JsonObject


@dataclass(frozen=True)
class CompatibleControlMediaBuyResponse:
    """Lifecycle-neutral completed media-buy control response."""

    operation: Literal["control"]
    media_buy_id: str
    revision: int
    raw: JsonObject


@dataclass(frozen=True)
class CompatibleMediaBuyReadbackResponse:
    """Lifecycle-neutral completed media-buy or delivery readback."""

    operation: Literal["get_media_buys", "get_media_buy_delivery"]
    raw: JsonObject


@dataclass(frozen=True)
class CompatibleTaskResult(Generic[T]):
    """Status-aware result that preserves compatibility provenance."""

    status: TaskStatus
    data: T | None
    compatibility: MediaBuyCompatibilityReport
    raw: TaskResult[Any]
    task_id: str | None = None
    _account: JsonObject | None = None
    _source_schema_version: str | None = None
    _issuance_idempotency_key: str | None = None
    _observed_request: JsonObject | None = None
    _mutation_operation: Literal["refine", "decline"] | None = None
    _mutation_record: ProposalMutationRecord | None = None
    _acceptance_record: ProposalAcceptanceRecord | None = None
    _proposal_ids: tuple[str, ...] = ()
    _readback_operation: Literal["get_media_buys", "get_media_buy_delivery"] | None = None

    @property
    def success(self) -> bool:
        """Whether this result is a completed successful compatibility result."""

        return self.status is TaskStatus.COMPLETED and self.data is not None and self.raw.success

    @property
    def terminal(self) -> bool:
        """Whether the operation has reached a terminal SDK state."""

        return self.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}


class MediaBuyLifecycleCoordinator:
    """Negotiate catalog, purchase, proposal, and media-buy control operations."""

    def __init__(
        self,
        client: ADCPClient,
        capabilities: GetAdcpCapabilitiesResponse,
        advertised_tools: Sequence[str],
        *,
        legacy_purchase_coordinator: LegacyPurchaseCoordinator | None = None,
        proposal_evidence_store: EstablishedProposalEvidenceStore | None = None,
        principal_id: str | None = None,
        target_binding: str | None = None,
        preferred_lifecycle: LifecyclePreference = "auto",
        allowed_losses: Sequence[str] = (),
        allow_non_durable_proposal_acceptance: bool = False,
        allow_non_durable_proposal_mutations: bool | None = None,
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
            if len(principal_id.encode("utf-8")) > ESTABLISHED_PROPOSAL_MAX_PRINCIPAL_BYTES:
                raise ValueError(
                    "principal_id must be at most "
                    f"{ESTABLISHED_PROPOSAL_MAX_PRINCIPAL_BYTES} UTF-8 bytes"
                )
        if target_binding is not None:
            if not isinstance(target_binding, str) or not target_binding.strip():
                raise TypeError("target_binding must be a non-empty string when provided")
            target_binding = target_binding.strip()
        if (legacy_purchase_coordinator is not None or proposal_evidence_store is not None) and (
            not principal_id or not target_binding
        ):
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
        self._allow_non_durable_proposal_acceptance = allow_non_durable_proposal_acceptance
        self._allow_non_durable_proposal_mutations = (
            allow_non_durable_proposal_acceptance
            if allow_non_durable_proposal_mutations is None
            else allow_non_durable_proposal_mutations
        )
        self._legacy = legacy_purchase_coordinator
        self.proposal_evidence_store = (
            proposal_evidence_store
            if proposal_evidence_store is not None
            else InMemoryEstablishedProposalEvidenceStore()
        )
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
        self._idempotency_replay_ttl = (
            timedelta(seconds=cast(int, replay_ttl_seconds))
            if self._mutation_idempotency_guaranteed
            else None
        )
        # This initial report is route discovery only. The actual list call
        # selects a complete list->buy pair so a partial compact surface cannot
        # strand the application after discovery.
        self.lifecycle = self._select_lifecycle("list_products", require_catalog_pair=False)

    @classmethod
    async def negotiate(
        cls,
        client: ADCPClient,
        *,
        capabilities: GetAdcpCapabilitiesResponse | None = None,
        advertised_tools: Sequence[str] | None = None,
        legacy_purchase_coordinator: LegacyPurchaseCoordinator | None = None,
        proposal_evidence_store: EstablishedProposalEvidenceStore | None = None,
        principal_id: str | None = None,
        target_binding: str | None = None,
        preferred_lifecycle: LifecyclePreference = "auto",
        allowed_losses: Sequence[str] = (),
        allow_non_durable_proposal_acceptance: bool = False,
        allow_non_durable_proposal_mutations: bool | None = None,
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
            proposal_evidence_store=proposal_evidence_store,
            principal_id=principal_id,
            target_binding=target_binding,
            preferred_lifecycle=preferred_lifecycle,
            allowed_losses=allowed_losses,
            allow_non_durable_proposal_acceptance=allow_non_durable_proposal_acceptance,
            allow_non_durable_proposal_mutations=allow_non_durable_proposal_mutations,
            continuation_ttl=continuation_ttl,
            clock=clock,
        )

    def report(self) -> MediaBuyCompatibilityReport:
        """Return the initial route report without claiming an operation was dispatched."""

        return self._report(self.lifecycle, ())

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

    async def request_proposals(
        self,
        request: RequestProposalsRequest,
    ) -> CompatibleTaskResult[CompatibleProposalResponse]:
        """Request proposals through compact or established discovery."""

        lifecycle = self._select_lifecycle("request_proposals")
        account = _json_object(request.account)
        if lifecycle is MediaBuyLifecycle.COMPACT:
            pinned = request.model_copy(update={"adcp_version": self.negotiated_version})
            compact_result = await self.client.request_proposals(pinned)
            return await self._adapt_proposal_result(
                compact_result,
                lifecycle=lifecycle,
                account=account,
                source_version=self.negotiated_version,
                issuance_idempotency_key=request.idempotency_key,
                observed_request=pinned.model_dump(mode="json", exclude_none=True),
            )

        if account is None:
            raise self._unsupported(
                "request_proposals",
                lifecycle,
                "account",
                "Established proposal coordination requires an account-bound request.",
            )
        if not self._principal_id or not self._target_binding:
            raise self._unsupported(
                "request_proposals",
                lifecycle,
                "authenticated_scope",
                "Established proposal coordination requires principal_id and target_binding.",
            )
        wire_request = self._legacy_proposal_request(request)
        source_version = self._exact_source_schema_version("request_proposals", lifecycle)
        try:
            _assert_declared_legacy_fields(
                wire_request,
                tool="get_products",
                source_version=source_version,
                nested=("filters",),
            )
        except ValueError as exc:
            raise self._unsupported(
                "request_proposals",
                lifecycle,
                "legacy_request_projection",
                str(exc),
            ) from exc
        validation = validate_request("get_products", wire_request, version=source_version)
        if not validation.valid or validation.variant == "skipped":
            raise self._unsupported(
                "request_proposals",
                lifecycle,
                "legacy_request_projection",
                "The proposal requirements cannot be represented by the negotiated "
                f"{self.negotiated_version} get_products contract.",
            )
        model = LegacyGetProductsRequest.model_validate(wire_request)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy_result = await self.client.get_products_legacy(model)
        return await self._adapt_proposal_result(
            legacy_result,
            lifecycle=lifecycle,
            account=account,
            source_version=source_version,
            issuance_idempotency_key=request.idempotency_key,
            observed_request=wire_request,
        )

    async def wait_for_proposals(
        self,
        initial: CompatibleTaskResult[CompatibleProposalResponse],
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> CompatibleTaskResult[CompatibleProposalResponse]:
        """Poll a submitted proposal request and apply the original projection."""

        if initial.terminal:
            return initial
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        if not initial.task_id:
            raise ValueError("submitted proposal result is missing task_id")
        if initial._source_schema_version is None or initial._observed_request is None:
            raise TypeError("initial result was not issued by this proposal coordinator")
        expected_tool = initial.compatibility.tools_used[0]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            polled = await self.client.get_task_status(
                GetTaskStatusRequest(
                    task_id=initial.task_id,
                    account=cast(Any, initial._account),
                    include_result=True,
                    adcp_version=self.negotiated_version,
                )
            )
            if not polled.success or polled.data is None:
                return CompatibleTaskResult(
                    status=TaskStatus.FAILED,
                    data=None,
                    compatibility=initial.compatibility,
                    raw=cast(TaskResult[Any], polled),
                    task_id=initial.task_id,
                    _account=initial._account,
                    _source_schema_version=initial._source_schema_version,
                    _issuance_idempotency_key=initial._issuance_idempotency_key,
                    _observed_request=initial._observed_request,
                )
            status_payload = polled.data.model_dump(mode="json", exclude_none=True)
            task_type = _root_value(status_payload.get("task_type"))
            if task_type != expected_tool:
                raise self._unsupported(
                    "wait_for_proposals",
                    initial.compatibility.lifecycle,
                    "task_type",
                    f"Task {initial.task_id} belongs to {task_type}, not {expected_tool}.",
                )
            seller_status = _root_value(status_payload.get("status"))
            if seller_status == "completed":
                terminal = status_payload.get("result")
                if not isinstance(terminal, Mapping):
                    raise self._unsupported(
                        "wait_for_proposals",
                        initial.compatibility.lifecycle,
                        "terminal_result",
                        "Completed proposal task omitted its canonical terminal result.",
                    )
                return await self._adapt_proposal_result(
                    TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=dict(terminal),
                        success=True,
                        metadata={"task_id": initial.task_id},
                    ),
                    lifecycle=initial.compatibility.lifecycle,
                    account=initial._account,
                    source_version=initial._source_schema_version,
                    issuance_idempotency_key=cast(str, initial._issuance_idempotency_key),
                    observed_request=initial._observed_request,
                )
            if seller_status in {"failed", "rejected", "canceled"}:
                return CompatibleTaskResult(
                    status=TaskStatus.FAILED,
                    data=None,
                    compatibility=initial.compatibility,
                    raw=cast(TaskResult[Any], polled),
                    task_id=initial.task_id,
                    _account=initial._account,
                    _source_schema_version=initial._source_schema_version,
                    _issuance_idempotency_key=initial._issuance_idempotency_key,
                    _observed_request=initial._observed_request,
                )
            if seller_status in {"input-required", "auth-required"}:
                return CompatibleTaskResult(
                    status=TaskStatus.NEEDS_INPUT,
                    data=None,
                    compatibility=initial.compatibility,
                    raw=cast(TaskResult[Any], polled),
                    task_id=initial.task_id,
                    _account=initial._account,
                    _source_schema_version=initial._source_schema_version,
                    _issuance_idempotency_key=initial._issuance_idempotency_key,
                    _observed_request=initial._observed_request,
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"proposal task {initial.task_id!r} did not complete in time")
            await asyncio.sleep(min(poll_interval, remaining))

    async def wait_for_proposal_mutation(
        self,
        initial: CompatibleTaskResult[Any],
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> CompatibleTaskResult[Any]:
        """Poll a submitted refinement or decline without releasing its fence."""

        if initial.terminal:
            return initial
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        if (
            initial._mutation_operation is None
            or not initial._proposal_ids
            or initial._source_schema_version is None
            or not initial.task_id
        ):
            raise TypeError("initial result was not issued by a proposal mutation coordinator")
        operation = initial._mutation_operation
        lifecycle = initial.compatibility.lifecycle
        expected_tool = (
            f"{operation}_proposals" if lifecycle is MediaBuyLifecycle.COMPACT else "get_products"
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            polled = await self.client.get_task_status(
                GetTaskStatusRequest(
                    task_id=initial.task_id,
                    account=cast(Any, initial._account),
                    include_result=True,
                    adcp_version=self.negotiated_version,
                )
            )
            if not polled.success or polled.data is None:
                ambiguous = await self._mark_recovered_mutation_ambiguous(initial)
                return replace(
                    initial,
                    status=TaskStatus.FAILED,
                    data=None,
                    raw=polled,
                    _mutation_record=ambiguous,
                )
            status_payload = polled.data.model_dump(mode="json", exclude_none=True)
            task_type = _root_value(status_payload.get("task_type"))
            if task_type != expected_tool:
                await self._mark_recovered_mutation_ambiguous(initial)
                raise self._unsupported(
                    "wait_for_proposal_mutation",
                    lifecycle,
                    "task_type",
                    f"Task {initial.task_id} belongs to {task_type}, not {expected_tool}.",
                )
            seller_status = _root_value(status_payload.get("status"))
            if seller_status == "completed":
                terminal = status_payload.get("result")
                if not isinstance(terminal, Mapping):
                    raise ValueError("completed proposal mutation omitted its terminal result")
                terminal_result = TaskResult[Any](
                    status=TaskStatus.COMPLETED,
                    data=dict(terminal),
                    success=True,
                    metadata={"task_id": initial.task_id},
                )
                projected: CompatibleTaskResult[Any]
                if operation == "refine":
                    projected = _project_refinement_task(
                        terminal_result,
                        lifecycle=lifecycle,
                        proposal_ids=initial._proposal_ids,
                        compatibility=initial.compatibility,
                        source_version=initial._source_schema_version,
                    )
                else:
                    projected = _project_decline_task(
                        terminal_result,
                        lifecycle=lifecycle,
                        proposal_ids=initial._proposal_ids,
                        compatibility=initial.compatibility,
                        source_version=initial._source_schema_version,
                    )
                if initial._mutation_record is not None:
                    store = self._require_proposal_mutation_store(
                        "wait_for_proposal_mutation", lifecycle
                    )
                    replacements: tuple[EstablishedProposalEvidence, ...] = ()
                    if operation == "refine" and projected.data is not None:
                        refine_projected = cast(
                            CompatibleTaskResult[CompatibleRefineProposalsResponse], projected
                        )
                        assert refine_projected.data is not None
                        source = await self.proposal_evidence_store.get(
                            initial._proposal_ids[0],
                            principal_id=initial._mutation_record.principal_id,
                            target_binding=initial._mutation_record.target_binding,
                            account_identity=initial._mutation_record.account_identity,
                            source_adcp_version=initial._mutation_record.source_adcp_version,
                        )
                        if source is None:
                            raise ProposalEvidenceChangedError(
                                "source proposal disappeared during task polling"
                            )
                        replacements = self._capture_mutation_replacements(
                            refine_projected.data.proposals,
                            source=source,
                            observed_request=cast(JsonObject, initial._observed_request),
                        )
                        projected = replace(
                            refine_projected,
                            data=replace(refine_projected.data, evidence=replacements),
                        )
                    await store.complete_mutation(
                        initial._mutation_record,
                        _persistable_task_result(terminal_result),
                        replacements=replacements,
                    )
                return replace(
                    projected,
                    _source_schema_version=initial._source_schema_version,
                    _mutation_operation=operation,
                    _mutation_record=initial._mutation_record,
                    _proposal_ids=initial._proposal_ids,
                )
            if seller_status in {"failed", "rejected", "canceled"}:
                failure = TaskResult[Any](
                    status=TaskStatus.FAILED,
                    success=False,
                    error=_optional_text(status_payload.get("message"))
                    or "Proposal mutation failed.",
                )
                if initial._mutation_record is not None:
                    store = self._require_proposal_mutation_store(
                        "wait_for_proposal_mutation", lifecycle
                    )
                    await store.complete_mutation(
                        initial._mutation_record, _persistable_task_result(failure)
                    )
                return replace(initial, status=TaskStatus.FAILED, data=None, raw=failure)
            if seller_status in {"input-required", "auth-required"}:
                ambiguous = await self._mark_recovered_mutation_ambiguous(initial)
                return replace(
                    initial,
                    status=TaskStatus.NEEDS_INPUT,
                    data=None,
                    raw=polled,
                    _mutation_record=ambiguous,
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"proposal mutation task {initial.task_id!r} did not complete in time"
                )
            await asyncio.sleep(min(poll_interval, remaining))

    async def recover_proposal_mutation(
        self,
        seller_task_id: str,
        *,
        account: Mapping[str, Any] | BaseModel,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> CompatibleTaskResult[Any]:
        """Recover and poll an established proposal mutation after restart."""

        if not isinstance(seller_task_id, str) or not seller_task_id:
            raise ValueError("seller_task_id must be non-empty")
        lifecycle = MediaBuyLifecycle.ESTABLISHED
        store = self._require_proposal_mutation_store("recover_proposal_mutation", lifecycle)
        source_version = self._exact_source_schema_version("recover_proposal_mutation", lifecycle)
        account_value = _json_object(account)
        assert account_value is not None
        account_identity = canonical_account_identity(account_value)
        record = await store.find_mutation_by_task(
            seller_task_id,
            principal_id=cast(str, self._principal_id),
            target_binding=cast(str, self._target_binding),
            account_identity=account_identity,
            source_adcp_version=source_version,
        )
        if record is None:
            raise self._unsupported(
                "recover_proposal_mutation",
                lifecycle,
                "submitted_task_not_found",
                "No submitted proposal mutation exists in the authenticated task scope.",
            )
        retained = record.request
        wire_request = (
            dict(retained["wire_request"])
            if isinstance(retained, Mapping) and isinstance(retained.get("wire_request"), Mapping)
            else None
        )
        if wire_request is None:
            raise self._unsupported(
                "recover_proposal_mutation",
                lifecycle,
                "recovery_evidence",
                "The durable mutation omitted its reduced wire-request evidence.",
            )
        operation: Literal["refine", "decline"] = (
            "refine" if record.operation is ProposalMutationKind.REFINE else "decline"
        )
        report = self._report(
            lifecycle,
            ("get_products",),
            warnings=("Recovered a durable submitted proposal mutation.",),
            losses=_DECLINE_LOSSES if operation == "decline" else (),
        )
        if record.state is ProposalAcceptanceState.COMPLETED and record.result is not None:
            terminal = TaskResult[Any].model_validate(record.result)
            projected = (
                _project_refinement_task(
                    terminal,
                    lifecycle=lifecycle,
                    proposal_ids=record.proposal_ids,
                    compatibility=report,
                    source_version=source_version,
                )
                if operation == "refine"
                else _project_decline_task(
                    terminal,
                    lifecycle=lifecycle,
                    proposal_ids=record.proposal_ids,
                    compatibility=report,
                    source_version=source_version,
                )
            )
            return replace(
                projected,
                _account=account_value,
                _source_schema_version=source_version,
                _observed_request=wire_request,
                _mutation_operation=operation,
                _mutation_record=record,
                _proposal_ids=record.proposal_ids,
            )
        if record.state is ProposalAcceptanceState.AMBIGUOUS:
            raise self._unsupported(
                "recover_proposal_mutation",
                lifecycle,
                "proposal_mutation_ambiguous",
                "The submitted proposal mutation is durably fenced as ambiguous.",
            )
        initial = CompatibleTaskResult[Any](
            status=TaskStatus.SUBMITTED,
            data=None,
            compatibility=report,
            raw=TaskResult[Any](
                status=TaskStatus.SUBMITTED,
                data={"status": "submitted", "task_id": seller_task_id},
                success=True,
                metadata={"task_id": seller_task_id},
            ),
            task_id=seller_task_id,
            _account=account_value,
            _source_schema_version=source_version,
            _observed_request=wire_request,
            _mutation_operation=operation,
            _mutation_record=record,
            _proposal_ids=record.proposal_ids,
        )
        return await self.wait_for_proposal_mutation(
            initial,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    async def wait_for_acceptance(
        self,
        initial: CompatibleTaskResult[Any],
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> CompatibleTaskResult[Any]:
        """Poll a submitted established acceptance without releasing its fence."""

        if initial.terminal:
            return initial
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        if (
            initial._acceptance_record is None
            or initial._source_schema_version is None
            or not initial.task_id
        ):
            raise TypeError("initial result was not issued by an acceptance coordinator")
        store = self._require_acceptance_recovery_store("wait_for_acceptance")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            polled = await self.client.get_task_status(
                GetTaskStatusRequest(
                    task_id=initial.task_id,
                    account=cast(Any, initial._account),
                    include_result=True,
                    adcp_version=self.negotiated_version,
                )
            )
            if not polled.success or polled.data is None:
                ambiguous = await self._mark_recovered_acceptance_ambiguous(initial, store)
                return replace(
                    initial,
                    status=TaskStatus.FAILED,
                    data=None,
                    raw=polled,
                    _acceptance_record=ambiguous,
                )
            status_payload = polled.data.model_dump(mode="json", exclude_none=True)
            task_type = _root_value(status_payload.get("task_type"))
            if task_type != "create_media_buy":
                await self._mark_recovered_acceptance_ambiguous(initial, store)
                raise self._unsupported(
                    "wait_for_acceptance",
                    MediaBuyLifecycle.ESTABLISHED,
                    "task_type",
                    f"Task {initial.task_id} belongs to {task_type}, not create_media_buy.",
                )
            seller_status = _root_value(status_payload.get("status"))
            if seller_status == "completed":
                terminal = status_payload.get("result")
                if not isinstance(terminal, Mapping):
                    await self._mark_recovered_acceptance_ambiguous(initial, store)
                    raise self._unsupported(
                        "wait_for_acceptance",
                        MediaBuyLifecycle.ESTABLISHED,
                        "terminal_result",
                        "Completed acceptance task omitted its canonical terminal result.",
                    )
                response_payload = dict(terminal)
                response_validation = validate_response(
                    "create_media_buy",
                    response_payload,
                    version=initial._source_schema_version,
                )
                if not response_validation.valid or response_validation.variant == "skipped":
                    await self._mark_recovered_acceptance_ambiguous(initial, store)
                    raise self._unsupported(
                        "wait_for_acceptance",
                        MediaBuyLifecycle.ESTABLISHED,
                        "terminal_result",
                        "Established create_media_buy returned an invalid exact-version response: "
                        f"{format_issues(response_validation.issues)}",
                    )
                terminal_result = TaskResult[Any](
                    status=TaskStatus.COMPLETED,
                    data=response_payload,
                    success=True,
                    metadata={"task_id": initial.task_id},
                )
                try:
                    completed = await store.complete_acceptance(
                        initial._acceptance_record,
                        _persistable_task_result(terminal_result),
                    )
                except BaseException:
                    await self._mark_recovered_acceptance_ambiguous(initial, store)
                    raise
                return replace(
                    _compatible_acceptance_result(
                        terminal_result,
                        compatibility=initial.compatibility,
                    ),
                    _account=initial._account,
                    _source_schema_version=initial._source_schema_version,
                    _acceptance_record=completed,
                )
            if seller_status in {"failed", "rejected", "canceled"}:
                failure = TaskResult[Any](
                    status=TaskStatus.FAILED,
                    success=False,
                    error=_optional_text(status_payload.get("message"))
                    or "Proposal acceptance failed.",
                )
                completed = await store.complete_acceptance(
                    initial._acceptance_record,
                    _persistable_task_result(failure),
                )
                return replace(
                    initial,
                    status=TaskStatus.FAILED,
                    data=None,
                    raw=failure,
                    _acceptance_record=completed,
                )
            if seller_status in {"input-required", "auth-required"}:
                ambiguous = await self._mark_recovered_acceptance_ambiguous(initial, store)
                return replace(
                    initial,
                    status=TaskStatus.NEEDS_INPUT,
                    data=None,
                    raw=polled,
                    _acceptance_record=ambiguous,
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"proposal acceptance task {initial.task_id!r} did not complete in time"
                )
            await asyncio.sleep(min(poll_interval, remaining))

    async def recover_acceptance(
        self,
        seller_task_id: str,
        *,
        account: Mapping[str, Any] | BaseModel,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> CompatibleTaskResult[Any]:
        """Recover and poll an established acceptance after coordinator restart."""

        if not isinstance(seller_task_id, str) or not seller_task_id:
            raise ValueError("seller_task_id must be non-empty")
        store = self._require_acceptance_recovery_store("recover_acceptance")
        source_version = self._exact_source_schema_version(
            "recover_acceptance", MediaBuyLifecycle.ESTABLISHED
        )
        account_value = _json_object(account)
        assert account_value is not None
        account_identity = canonical_account_identity(account_value)
        record = await store.find_acceptance_by_task(
            seller_task_id,
            principal_id=cast(str, self._principal_id),
            target_binding=cast(str, self._target_binding),
            account_identity=account_identity,
            source_adcp_version=source_version,
        )
        if record is None:
            raise self._unsupported(
                "recover_acceptance",
                MediaBuyLifecycle.ESTABLISHED,
                "submitted_task_not_found",
                "No submitted acceptance exists in the authenticated task scope.",
            )
        retained = record.request or {}
        losses_value = retained.get("accepted_losses")
        losses = tuple(str(loss) for loss in losses_value) if isinstance(losses_value, list) else ()
        report = self._report(
            MediaBuyLifecycle.ESTABLISHED,
            ("create_media_buy",),
            losses=losses,
            warnings=("Recovered a durable submitted proposal acceptance.",),
        )
        if record.state is ProposalAcceptanceState.COMPLETED and record.result is not None:
            terminal = TaskResult[Any].model_validate(record.result)
            return replace(
                _compatible_acceptance_result(terminal, compatibility=report),
                _account=account_value,
                _source_schema_version=source_version,
                _acceptance_record=record,
            )
        if record.state is ProposalAcceptanceState.AMBIGUOUS:
            raise self._unsupported(
                "recover_acceptance",
                MediaBuyLifecycle.ESTABLISHED,
                "acceptance_outcome_ambiguous",
                "The submitted acceptance is durably fenced as ambiguous.",
                losses=losses,
            )
        initial = CompatibleTaskResult[Any](
            status=TaskStatus.SUBMITTED,
            data=None,
            compatibility=report,
            raw=TaskResult[Any](
                status=TaskStatus.SUBMITTED,
                data={"status": "submitted", "task_id": seller_task_id},
                success=True,
                metadata={"task_id": seller_task_id},
            ),
            task_id=seller_task_id,
            _account=account_value,
            _source_schema_version=source_version,
            _acceptance_record=record,
        )
        return await self.wait_for_acceptance(
            initial,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    async def refine_proposals(
        self,
        request: RefineProposalsRequest,
    ) -> CompatibleTaskResult[CompatibleRefineProposalsResponse]:
        """Revise or finalize proposals through the negotiated lifecycle."""

        lifecycle = self._select_lifecycle("refine_proposals")
        payload = request.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        proposal_ids = _unique_proposal_ids(payload.get("refinements"), field="refinements")
        if lifecycle is MediaBuyLifecycle.COMPACT:
            pinned = request.model_copy(update={"adcp_version": self.negotiated_version})
            compact_result = await self.client.refine_proposals(pinned)
            projected = _project_refinement_task(
                compact_result,
                lifecycle=lifecycle,
                proposal_ids=proposal_ids,
                compatibility=self._report(lifecycle, ("refine_proposals",)),
                source_version=self.negotiated_version,
            )
            return replace(
                projected,
                _source_schema_version=self.negotiated_version,
                _mutation_operation="refine",
                _proposal_ids=proposal_ids,
            )

        store = self._require_proposal_mutation_store("refine_proposals", lifecycle)
        source_version = self._exact_source_schema_version("refine_proposals", lifecycle)
        evidence = await self._mutation_evidence(
            store, proposal_ids, source_version=source_version, operation="refine_proposals"
        )
        try:
            wire_request = _legacy_refine_request(payload, source_version=source_version)
        except ValueError as exc:
            raise self._unsupported(
                "refine_proposals",
                lifecycle,
                "legacy_request_projection",
                str(exc),
            ) from exc
        reservation_input = {
            "request": payload,
            "wire_request": wire_request,
            "retained_evidence": [row.proposal for row in evidence],
        }
        reservation = await self._reserve_proposal_mutation(
            store,
            evidence,
            operation=ProposalMutationKind.REFINE,
            idempotency_key=request.idempotency_key,
            reservation_input=reservation_input,
            lifecycle=lifecycle,
        )
        report = self._report(lifecycle, ("get_products",))
        if not reservation.created:
            replay = self._proposal_mutation_replay(
                reservation.record,
                proposal_ids=proposal_ids,
                idempotency_key=request.idempotency_key,
                reservation_input=reservation_input,
                operation="refine_proposals",
                lifecycle=lifecycle,
            )
            return _project_refinement_task(
                replay,
                lifecycle=lifecycle,
                proposal_ids=proposal_ids,
                compatibility=self._report(
                    lifecycle,
                    ("get_products",),
                    warnings=("Replayed the durable buyer-side refinement result.",),
                ),
                source_version=source_version,
            )
        record = reservation.record
        try:
            model = LegacyGetProductsRequest.model_validate(wire_request)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                legacy_result = await self.client.get_products_legacy(model)
            projected = _project_refinement_task(
                legacy_result,
                lifecycle=lifecycle,
                proposal_ids=proposal_ids,
                compatibility=report,
                source_version=source_version,
            )
            if not projected.terminal:
                if not projected.task_id:
                    raise ValueError("submitted refinement omitted its seller task ID")
                record = await store.record_mutation_task(record, projected.task_id)
                return replace(
                    projected,
                    _account=json.loads(evidence[0].account_identity),
                    _source_schema_version=source_version,
                    _observed_request=wire_request,
                    _mutation_operation="refine",
                    _mutation_record=record,
                    _proposal_ids=proposal_ids,
                )
            replacements = self._capture_mutation_replacements(
                projected.data.proposals if projected.data is not None else (),
                source=evidence[0],
                observed_request=wire_request,
            )
            if projected.data is not None:
                projected = replace(
                    projected,
                    data=replace(projected.data, evidence=replacements),
                )
            await store.complete_mutation(
                record,
                _persistable_task_result(legacy_result),
                replacements=replacements,
            )
            return replace(
                projected,
                _source_schema_version=source_version,
                _mutation_operation="refine",
                _mutation_record=record,
                _proposal_ids=proposal_ids,
            )
        except BaseException:
            try:
                await store.mark_mutation_ambiguous(record)
            except Exception:  # nosec B110 - preserve the original mutation failure
                pass
            raise

    async def decline_proposals(
        self,
        request: DeclineProposalsRequest,
        *,
        accepted_losses: Sequence[str] | None = None,
    ) -> CompatibleTaskResult[CompatibleDeclineProposalsResponse]:
        """Decline proposals, reporting weaker established omit semantics."""

        lifecycle = self._select_lifecycle("decline_proposals")
        payload = request.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        proposal_ids = _unique_proposal_ids(payload.get("declines"), field="declines")
        if lifecycle is MediaBuyLifecycle.COMPACT:
            pinned = request.model_copy(update={"adcp_version": self.negotiated_version})
            compact_result = await self.client.decline_proposals(pinned)
            projected = _project_decline_task(
                compact_result,
                lifecycle=lifecycle,
                proposal_ids=proposal_ids,
                compatibility=self._report(lifecycle, ("decline_proposals",)),
                source_version=self.negotiated_version,
            )
            return replace(
                projected,
                _source_schema_version=self.negotiated_version,
                _mutation_operation="decline",
                _proposal_ids=proposal_ids,
            )

        losses = tuple(_DECLINE_LOSSES)
        exact_losses = tuple(losses if accepted_losses is None else accepted_losses)
        if set(exact_losses) != set(losses) or len(exact_losses) != len(losses):
            raise self._unsupported(
                "decline_proposals",
                lifecycle,
                "accepted_losses",
                "accepted_losses must exactly match the established decline losses.",
                losses=losses,
            )
        refused = tuple(loss for loss in losses if loss not in self._allowed_losses)
        if refused:
            raise self._unsupported(
                "decline_proposals",
                lifecycle,
                "compatibility_losses",
                "No decline was sent because required compatibility losses were not allowed.",
                losses=refused,
            )
        store = self._require_proposal_mutation_store("decline_proposals", lifecycle)
        source_version = self._exact_source_schema_version("decline_proposals", lifecycle)
        evidence = await self._mutation_evidence(
            store, proposal_ids, source_version=source_version, operation="decline_proposals"
        )
        try:
            wire_request = _legacy_decline_request(payload, source_version=source_version)
        except ValueError as exc:
            raise self._unsupported(
                "decline_proposals",
                lifecycle,
                "legacy_request_projection",
                str(exc),
            ) from exc
        reservation_input = {
            "request": payload,
            "wire_request": wire_request,
            "retained_evidence": [row.proposal for row in evidence],
            "accepted_losses": list(exact_losses),
        }
        reservation = await self._reserve_proposal_mutation(
            store,
            evidence,
            operation=ProposalMutationKind.DECLINE,
            idempotency_key=request.idempotency_key,
            reservation_input=reservation_input,
            lifecycle=lifecycle,
        )
        report = self._report(
            lifecycle,
            ("get_products",),
            losses=losses,
            warnings=(
                "Legacy proposal omit is not a seller-confirmed terminal decline.",
                "Legacy proposal omit cannot forward decline reason or detail.",
            ),
        )
        if not reservation.created:
            replay = self._proposal_mutation_replay(
                reservation.record,
                proposal_ids=proposal_ids,
                idempotency_key=request.idempotency_key,
                reservation_input=reservation_input,
                operation="decline_proposals",
                lifecycle=lifecycle,
            )
            return _project_decline_task(
                replay,
                lifecycle=lifecycle,
                proposal_ids=proposal_ids,
                compatibility=report,
                source_version=source_version,
            )
        record = reservation.record
        try:
            model = LegacyGetProductsRequest.model_validate(wire_request)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                legacy_result = await self.client.get_products_legacy(model)
            projected = _project_decline_task(
                legacy_result,
                lifecycle=lifecycle,
                proposal_ids=proposal_ids,
                compatibility=report,
                source_version=source_version,
            )
            if not projected.terminal:
                if not projected.task_id:
                    raise ValueError("submitted decline omitted its seller task ID")
                record = await store.record_mutation_task(record, projected.task_id)
                return replace(
                    projected,
                    _account=json.loads(evidence[0].account_identity),
                    _source_schema_version=source_version,
                    _observed_request=wire_request,
                    _mutation_operation="decline",
                    _mutation_record=record,
                    _proposal_ids=proposal_ids,
                )
            await store.complete_mutation(record, _persistable_task_result(legacy_result))
            return replace(
                projected,
                _source_schema_version=source_version,
                _mutation_operation="decline",
                _mutation_record=record,
                _proposal_ids=proposal_ids,
            )
        except BaseException:
            try:
                await store.mark_mutation_ambiguous(record)
            except Exception:  # nosec B110 - preserve the original mutation failure
                pass
            raise

    async def control_media_buy(
        self,
        request: ControlMediaBuyRequest,
    ) -> CompatibleTaskResult[CompatibleControlMediaBuyResponse]:
        """Apply operational controls through the negotiated lifecycle."""

        lifecycle = self._select_lifecycle("control_media_buy")
        payload = request.model_dump(mode="json", exclude_unset=True)
        try:
            _validate_control_semantics(payload)
        except ValueError as exc:
            raise self._unsupported(
                "control_media_buy",
                lifecycle,
                "control_request",
                str(exc),
            ) from exc

        if lifecycle is MediaBuyLifecycle.COMPACT:
            pinned = request.model_copy(update={"adcp_version": self.negotiated_version})
            pinned_payload = pinned.model_dump(mode="json", exclude_unset=True)
            validation = validate_request(
                "control_media_buy", pinned_payload, version=self.negotiated_version
            )
            if not validation.valid or validation.variant == "skipped":
                raise self._unsupported(
                    "control_media_buy",
                    lifecycle,
                    "compact_request",
                    "The control does not satisfy the negotiated compact contract: "
                    f"{format_issues(validation.issues)}",
                )
            compact_result = await self.client.control_media_buy(pinned)
            projected = _project_control_task(
                compact_result,
                lifecycle=lifecycle,
                compatibility=self._report(lifecycle, ("control_media_buy",)),
                source_version=self.negotiated_version,
            )
            return replace(
                projected,
                _account=_json_object(request.account),
                _source_schema_version=self.negotiated_version,
                _observed_request=pinned_payload,
            )

        source_version = self._exact_source_schema_version("control_media_buy", lifecycle)
        try:
            wire_request = _legacy_control_request(
                payload,
                negotiated_version=self.negotiated_version,
                source_version=source_version,
            )
        except ValueError as exc:
            raise self._unsupported(
                "control_media_buy",
                lifecycle,
                "legacy_request_projection",
                str(exc),
            ) from exc
        model = LegacyUpdateMediaBuyRequest.model_validate(wire_request)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy_result = await self.client.update_media_buy_legacy(model)
        projected = _project_control_task(
            legacy_result,
            lifecycle=lifecycle,
            compatibility=self._report(lifecycle, ("update_media_buy",)),
            source_version=source_version,
        )
        return replace(
            projected,
            _account=_json_object(request.account),
            _source_schema_version=source_version,
            _observed_request=wire_request,
        )

    async def wait_for_control_media_buy(
        self,
        initial: CompatibleTaskResult[CompatibleControlMediaBuyResponse],
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> CompatibleTaskResult[CompatibleControlMediaBuyResponse]:
        """Poll a submitted control and preserve its compatibility report."""

        if initial.terminal:
            return initial
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        if not initial.task_id or initial._source_schema_version is None:
            raise TypeError("initial result was not issued by this control coordinator")
        expected_tool = initial.compatibility.tools_used[0]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            polled = await self.client.get_task_status(
                GetTaskStatusRequest(
                    task_id=initial.task_id,
                    account=cast(Any, initial._account),
                    include_result=True,
                    adcp_version=self.negotiated_version,
                )
            )
            if not polled.success or polled.data is None:
                return replace(initial, status=TaskStatus.FAILED, data=None, raw=polled)
            status_payload = polled.data.model_dump(mode="json", exclude_none=True)
            task_type = _root_value(status_payload.get("task_type"))
            if task_type != expected_tool:
                raise self._unsupported(
                    "wait_for_control_media_buy",
                    initial.compatibility.lifecycle,
                    "task_type",
                    f"Task {initial.task_id} belongs to {task_type}, not {expected_tool}.",
                )
            seller_status = _root_value(status_payload.get("status"))
            if seller_status == "completed":
                terminal = status_payload.get("result")
                if not isinstance(terminal, Mapping):
                    raise ValueError("completed media-buy control omitted its terminal result")
                projected = _project_control_task(
                    TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=dict(terminal),
                        success=True,
                        metadata={"task_id": initial.task_id},
                    ),
                    lifecycle=initial.compatibility.lifecycle,
                    compatibility=initial.compatibility,
                    source_version=initial._source_schema_version,
                )
                return replace(
                    projected,
                    _account=initial._account,
                    _source_schema_version=initial._source_schema_version,
                    _observed_request=initial._observed_request,
                )
            if seller_status in {"failed", "rejected", "canceled"}:
                return replace(initial, status=TaskStatus.FAILED, data=None, raw=polled)
            if seller_status in {"input-required", "auth-required"}:
                return replace(initial, status=TaskStatus.NEEDS_INPUT, data=None, raw=polled)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"control task {initial.task_id!r} did not complete in time")
            await asyncio.sleep(min(poll_interval, remaining))

    async def get_media_buys(
        self,
        request: GetMediaBuysRequest,
    ) -> CompatibleTaskResult[CompatibleMediaBuyReadbackResponse]:
        """Read media buys under the negotiated exact-version contract."""

        return await self._media_buy_readback("get_media_buys", request)

    async def get_media_buy_delivery(
        self,
        request: GetMediaBuyDeliveryRequest,
    ) -> CompatibleTaskResult[CompatibleMediaBuyReadbackResponse]:
        """Read delivery under the negotiated exact-version contract."""

        return await self._media_buy_readback("get_media_buy_delivery", request)

    async def wait_for_media_buy_readback(
        self,
        initial: CompatibleTaskResult[CompatibleMediaBuyReadbackResponse],
        *,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
    ) -> CompatibleTaskResult[CompatibleMediaBuyReadbackResponse]:
        """Poll a submitted media-buy readback with its original schema binding."""

        if initial.terminal:
            return initial
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("timeout and poll_interval must be positive")
        if (
            not initial.task_id
            or initial._source_schema_version is None
            or initial._readback_operation is None
        ):
            raise TypeError("initial result was not issued by this readback coordinator")
        operation = initial._readback_operation
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            polled = await self.client.get_task_status(
                GetTaskStatusRequest(
                    task_id=initial.task_id,
                    account=cast(Any, initial._account),
                    include_result=True,
                    adcp_version=self.negotiated_version,
                )
            )
            if not polled.success or polled.data is None:
                return replace(initial, status=TaskStatus.FAILED, data=None, raw=polled)
            status_payload = polled.data.model_dump(mode="json", exclude_none=True)
            task_type = _root_value(status_payload.get("task_type"))
            if task_type != operation:
                raise self._unsupported(
                    "wait_for_media_buy_readback",
                    initial.compatibility.lifecycle,
                    "task_type",
                    f"Task {initial.task_id} belongs to {task_type}, not {operation}.",
                )
            seller_status = _root_value(status_payload.get("status"))
            if seller_status == "completed":
                terminal = status_payload.get("result")
                if not isinstance(terminal, Mapping):
                    raise ValueError("completed media-buy readback omitted its terminal result")
                projected = _project_readback_task(
                    TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=dict(terminal),
                        success=True,
                        metadata={"task_id": initial.task_id},
                    ),
                    operation=operation,
                    compatibility=initial.compatibility,
                    source_version=initial._source_schema_version,
                )
                return replace(
                    projected,
                    _account=initial._account,
                    _source_schema_version=initial._source_schema_version,
                    _observed_request=initial._observed_request,
                    _readback_operation=operation,
                )
            if seller_status in {"failed", "rejected", "canceled"}:
                return replace(initial, status=TaskStatus.FAILED, data=None, raw=polled)
            if seller_status in {"input-required", "auth-required"}:
                return replace(initial, status=TaskStatus.NEEDS_INPUT, data=None, raw=polled)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"readback task {initial.task_id!r} did not complete in time")
            await asyncio.sleep(min(poll_interval, remaining))

    async def _media_buy_readback(
        self,
        operation: Literal["get_media_buys", "get_media_buy_delivery"],
        request: GetMediaBuysRequest | GetMediaBuyDeliveryRequest,
    ) -> CompatibleTaskResult[CompatibleMediaBuyReadbackResponse]:
        lifecycle = self._shared_tool_lifecycle(operation)
        source_version = self._exact_source_schema_version(operation, lifecycle)
        pinned = request.model_copy(
            update={"adcp_version": self.negotiated_version, "adcp_major_version": 3}
        )
        observed_request = pinned.model_dump(mode="json", exclude_unset=True)
        if lifecycle is MediaBuyLifecycle.ESTABLISHED:
            try:
                _assert_declared_legacy_fields(
                    observed_request,
                    tool=operation,
                    source_version=source_version,
                )
            except ValueError as exc:
                raise self._unsupported(
                    operation,
                    lifecycle,
                    "request_projection",
                    str(exc),
                ) from exc
            if operation == "get_media_buy_delivery":
                issue = beta6_delivery_request_issue(observed_request)
                if issue is not None:
                    raise self._unsupported(
                        operation,
                        lifecycle,
                        issue.field,
                        f"The negotiated {self.negotiated_version} contract cannot represent "
                        f"{issue.detail}.",
                    )
        validation = validate_request(operation, observed_request, version=source_version)
        if not validation.valid or validation.variant == "skipped":
            raise self._unsupported(
                operation,
                lifecycle,
                "request_projection",
                f"The negotiated {self.negotiated_version} contract cannot represent this "
                f"readback: {format_issues(validation.issues)}",
            )

        result: TaskResult[Any]
        if lifecycle is MediaBuyLifecycle.COMPACT:
            if operation == "get_media_buys":
                result = cast(
                    TaskResult[Any],
                    await self.client.get_media_buys(cast(GetMediaBuysRequest, pinned)),
                )
            else:
                result = cast(
                    TaskResult[Any],
                    await self.client.get_media_buy_delivery(
                        cast(GetMediaBuyDeliveryRequest, pinned)
                    ),
                )
        else:
            wire = _sanitize_established_readback_request(
                pinned,
                operation=operation,
                negotiated_version=self.negotiated_version,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                if operation == "get_media_buys":
                    result = cast(
                        TaskResult[Any],
                        await self.client.get_media_buys_legacy(cast(GetMediaBuysRequest, wire)),
                    )
                else:
                    result = cast(
                        TaskResult[Any],
                        await self.client.get_media_buy_delivery_legacy(
                            cast(GetMediaBuyDeliveryRequest, wire)
                        ),
                    )
        projected = _project_readback_task(
            result,
            operation=operation,
            compatibility=self._report(lifecycle, (operation,)),
            source_version=source_version,
        )
        return replace(
            projected,
            _account=_json_object(getattr(request, "account", None)),
            _source_schema_version=source_version,
            _observed_request=observed_request,
            _readback_operation=operation,
        )

    async def accept_proposal(
        self,
        request: Mapping[str, Any] | BaseModel,
        *,
        established_fallback: Mapping[str, Any] | BaseModel | None = None,
        accepted_losses: Sequence[str] | None = None,
    ) -> CompatibleTaskResult[Any]:
        """Accept a proposal through the native or established lifecycle.

        ``established_fallback`` supplies only the legacy routing fields
        ``brand``, ``start_time``, and ``end_time``. It is never silently
        ignored: compact acceptance rejects it so callers cannot accidentally
        believe fallback values affected the native proposal snapshot.
        """

        lifecycle = self._select_lifecycle("accept_proposal")
        payload = _json_object(request)
        assert payload is not None
        if lifecycle is MediaBuyLifecycle.COMPACT:
            if established_fallback is not None:
                raise self._unsupported(
                    "accept_proposal",
                    lifecycle,
                    "established_fallback",
                    "Native accept_proposal does not consume established fallback fields.",
                )
            compact_model = AcceptProposalRequest.model_validate(
                {**payload, "adcp_version": self.negotiated_version}
            )
            compact_result = await self.client.accept_proposal(compact_model)
            return _compatible_acceptance_result(
                compact_result,
                compatibility=self._report(lifecycle, ("accept_proposal",)),
            )

        account = _mapping(payload.get("account"))
        proposal_id = _optional_text(payload.get("proposal_id"))
        idempotency_key = _optional_text(payload.get("idempotency_key"))
        if not account or proposal_id is None or idempotency_key is None:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "request_binding",
                "Established acceptance requires account, proposal_id, and idempotency_key.",
            )
        if not self._principal_id or not self._target_binding:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "authenticated_scope",
                "Established acceptance requires principal_id and target_binding.",
            )
        store = self.proposal_evidence_store
        if not isinstance(store, EstablishedProposalAcceptanceStore):
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "atomic_acceptance_store",
                "The proposal evidence store does not implement atomic acceptance reservations.",
            )
        if not store.is_durable and not self._allow_non_durable_proposal_acceptance:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "durable_acceptance_store",
                "Established acceptance requires durable shared persistence. The in-memory "
                "store is only available with the explicit development opt-in.",
            )

        source_version = self._exact_source_schema_version("accept_proposal", lifecycle)
        account_identity = canonical_account_identity(account)
        evidence = await store.get(
            proposal_id,
            principal_id=self._principal_id,
            target_binding=self._target_binding,
            account_identity=account_identity,
            source_adcp_version=source_version,
        )
        if evidence is None:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "proposal_evidence",
                "No proposal evidence exists in this authenticated account and version scope; "
                "request proposals again before accepting.",
            )
        proposal = evidence.proposal
        try:
            _assert_proposal_observation_acceptable(proposal, now=_aware_utc(self._clock()))
        except ValueError as exc:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "proposal_state",
                str(exc),
            ) from exc

        observed_digest = (
            _optional_text(proposal.get("terms_digest"))
            if isinstance(proposal.get("commercial_terms"), Mapping)
            and verify_terms_digest(proposal)
            else None
        )
        supplied_digest = _optional_text(payload.get("proposal_terms_digest"))
        losses = [_PROPOSAL_DIGEST_NOT_ENFORCED_LOSS]
        if observed_digest is None:
            losses.extend((_PROPOSAL_DIGEST_UNAVAILABLE_LOSS, _PROPOSAL_SNAPSHOT_LOSS))
        else:
            if supplied_digest != observed_digest:
                raise self._unsupported(
                    "accept_proposal",
                    lifecycle,
                    "proposal_terms_digest",
                    "proposal_terms_digest does not match the retained proposal evidence.",
                    code="PROPOSAL_DIGEST_MISMATCH",
                )
        if proposal.get("expires_at") is None:
            losses.append(_PROPOSAL_HOLD_LOSS)
        if not self._mutation_idempotency_guaranteed:
            losses.append(_MUTATION_LOSS)
        exact_losses = tuple(losses if accepted_losses is None else accepted_losses)
        if set(exact_losses) != set(losses) or len(exact_losses) != len(losses):
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "accepted_losses",
                "accepted_losses must exactly match the established acceptance losses.",
                losses=losses,
            )
        refused = tuple(loss for loss in losses if loss not in self._allowed_losses)
        if refused:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "compatibility_losses",
                "No mutation was sent because required compatibility losses were not allowed.",
                losses=refused,
            )

        supplied_fallback = _json_object(established_fallback) or {}
        if set(supplied_fallback) - {"brand", "start_time", "end_time"}:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "established_fallback",
                "Established fallback accepts only brand, start_time, and end_time.",
            )
        try:
            resolved_payload, fallback = _resolve_established_acceptance(
                payload,
                proposal=proposal,
                digest_bound=observed_digest is not None,
                fallback=supplied_fallback,
            )
        except ValueError as exc:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "proposal_terms",
                str(exc),
                code=(
                    "PROPOSAL_DIGEST_MISMATCH"
                    if observed_digest is not None
                    else "UNSUPPORTED_FEATURE"
                ),
            ) from exc
        try:
            wire_request = _legacy_accept_proposal_request(
                resolved_payload,
                fallback=fallback,
                negotiated_version=self.negotiated_version,
                source_version=source_version,
            )
        except ValueError as exc:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "legacy_request_projection",
                str(exc),
            ) from exc

        reservation_request = {
            "accept_proposal": resolved_payload,
            "established_fallback": fallback,
            "accepted_losses": list(exact_losses),
            "retained_evidence": proposal,
            "wire_request": wire_request,
        }
        try:
            reservation = await store.reserve_acceptance(
                evidence,
                idempotency_key=idempotency_key,
                request=reservation_request,
                created_at=_aware_utc(self._clock()),
                retry_ttl=self._idempotency_replay_ttl,
            )
        except (ProposalEvidenceChangedError, ProposalMutationConflictError) as exc:
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                "proposal_evidence_changed",
                "Proposal evidence changed before the atomic mutation reservation; request "
                "proposals again.",
            ) from exc
        if not reservation.created:
            record = reservation.record
            fingerprint = _canonical_fingerprint(reservation_request)
            if (
                record.proposal_id != proposal_id
                or record.idempotency_key != idempotency_key
                or record.request_fingerprint != fingerprint
            ):
                raise self._unsupported(
                    "accept_proposal",
                    lifecycle,
                    "acceptance_conflict",
                    "The proposal or idempotency key is already reserved for a different "
                    "acceptance request.",
                )
            if record.state is ProposalAcceptanceState.COMPLETED and record.result is not None:
                replay = TaskResult[Any].model_validate(record.result)
                return _compatible_acceptance_result(
                    replay,
                    compatibility=self._report(
                        lifecycle,
                        ("create_media_buy",),
                        losses=losses,
                        warnings=("Replayed the durable buyer-side acceptance result.",),
                    ),
                )
            if (
                record.state is ProposalAcceptanceState.IN_FLIGHT
                and record.seller_task_id is not None
            ):
                return CompatibleTaskResult[Any](
                    status=TaskStatus.SUBMITTED,
                    data=None,
                    compatibility=self._report(
                        lifecycle,
                        ("create_media_buy",),
                        losses=losses,
                        warnings=("Recovered the durable submitted acceptance task.",),
                    ),
                    raw=TaskResult[Any](
                        status=TaskStatus.SUBMITTED,
                        data={"status": "submitted", "task_id": record.seller_task_id},
                        success=True,
                        metadata={"task_id": record.seller_task_id},
                    ),
                    task_id=record.seller_task_id,
                    _account=account,
                    _source_schema_version=source_version,
                    _acceptance_record=record,
                )
            feature = (
                "acceptance_outcome_ambiguous"
                if record.state is ProposalAcceptanceState.AMBIGUOUS
                else "acceptance_in_flight"
            )
            raise self._unsupported(
                "accept_proposal",
                lifecycle,
                feature,
                "The exact acceptance is fenced and will not be dispatched again. Reconcile "
                "the media buy with the seller before retrying.",
                losses=losses,
            )

        record = reservation.record
        try:
            legacy_model = LegacyCreateMediaBuyRequest.model_validate(wire_request)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                legacy_result = await self.client.create_media_buy_legacy(legacy_model)
            if legacy_result.success and legacy_result.status is TaskStatus.COMPLETED:
                response_payload = _json_object(legacy_result.data)
                if response_payload is None:
                    raise ValueError("completed create_media_buy response omitted result data")
                response_validation = validate_response(
                    "create_media_buy", response_payload, version=source_version
                )
                if not response_validation.valid or response_validation.variant == "skipped":
                    raise ValueError(
                        "established create_media_buy returned an invalid exact-version "
                        f"response: {format_issues(response_validation.issues)}"
                    )
            projected = _compatible_acceptance_result(
                legacy_result,
                compatibility=self._report(
                    lifecycle,
                    ("create_media_buy",),
                    losses=losses,
                    warnings=(
                        "Established create_media_buy cannot atomically enforce the retained "
                        "proposal snapshot and digest.",
                    ),
                ),
            )
            if not projected.terminal:
                if not projected.task_id:
                    raise ValueError("submitted acceptance omitted its seller task ID")
                recovery_store = self._require_acceptance_recovery_store("accept_proposal")
                record = await recovery_store.record_acceptance_task(record, projected.task_id)
                return replace(
                    projected,
                    _account=account,
                    _source_schema_version=source_version,
                    _acceptance_record=record,
                )
            completed = await store.complete_acceptance(
                record, _persistable_task_result(legacy_result)
            )
        except BaseException:
            try:
                await store.mark_acceptance_ambiguous(record)
            except Exception:  # nosec B110 - preserve the original mutation failure
                # Preserve the mutation failure. A durable store implementation
                # must reconcile an indeterminate local commit before reuse.
                pass
            raise
        assert completed.result is not None
        return replace(
            projected,
            _account=account,
            _source_schema_version=source_version,
            _acceptance_record=completed,
        )

    async def buy_products(
        self,
        listing: CompatibleCatalog,
        request: Mapping[str, Any] | BaseModel,
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
        request: Mapping[str, Any] | BaseModel,
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

    async def _adapt_proposal_result(
        self,
        result: TaskResult[Any],
        *,
        lifecycle: MediaBuyLifecycle,
        account: JsonObject | None,
        source_version: str,
        issuance_idempotency_key: str,
        observed_request: JsonObject,
    ) -> CompatibleTaskResult[CompatibleProposalResponse]:
        tool = "request_proposals" if lifecycle is MediaBuyLifecycle.COMPACT else "get_products"
        payload = _json_object(result.data)
        status = _effective_task_status(result, payload)
        task_id = _task_id(result, payload)
        if status is not TaskStatus.COMPLETED:
            return CompatibleTaskResult(
                status=status,
                data=None,
                compatibility=self._report(lifecycle, (tool,)),
                raw=result,
                task_id=task_id,
                _account=account,
                _source_schema_version=source_version,
                _issuance_idempotency_key=issuance_idempotency_key,
                _observed_request=observed_request,
            )
        if not result.success or payload is None:
            return CompatibleTaskResult(
                status=TaskStatus.FAILED,
                data=None,
                compatibility=self._report(lifecycle, (tool,)),
                raw=result,
                task_id=task_id,
                _account=account,
                _source_schema_version=source_version,
                _issuance_idempotency_key=issuance_idempotency_key,
                _observed_request=observed_request,
            )

        self._validate_served_version("request_proposals", payload, lifecycle)
        if lifecycle is MediaBuyLifecycle.COMPACT:
            for value in payload.get("proposals") or ():
                if (
                    isinstance(value, Mapping)
                    and isinstance(value.get("commercial_terms"), Mapping)
                    and isinstance(value.get("terms_digest"), str)
                    and not verify_terms_digest(value)
                ):
                    raise self._unsupported(
                        "request_proposals",
                        lifecycle,
                        "proposal_terms_digest",
                        "A compact proposal terms_digest did not match commercial_terms.",
                        code="PROPOSAL_DIGEST_MISMATCH",
                    )
        validation = validate_response(tool, payload, version=source_version)
        if not validation.valid or validation.variant == "skipped":
            raise self._unsupported(
                "request_proposals",
                lifecycle,
                "response_projection",
                f"The {tool} completion cannot be safely projected: "
                f"{format_issues(validation.issues)}",
            )

        try:
            proposals = _optional_rows(payload.get("proposals"), id_field="proposal_id")
            products = _optional_rows(payload.get("products"), id_field="product_id")
        except ValueError as exc:
            raise self._unsupported(
                "request_proposals",
                lifecycle,
                "response_projection",
                str(exc),
            ) from exc
        if len({proposal["proposal_id"] for proposal in proposals}) != len(proposals):
            raise self._unsupported(
                "request_proposals",
                lifecycle,
                "proposal_id",
                "Proposal discovery returned duplicate proposal_id values.",
            )

        warnings_out: list[str] = []
        evidence: list[EstablishedProposalEvidence] = []
        if lifecycle is MediaBuyLifecycle.ESTABLISHED and proposals:
            assert account is not None
            observed_at = _aware_utc(self._clock())
            account_identity = canonical_account_identity(account)
            for proposal in proposals:
                has_digest = "terms_digest" in proposal
                retainable = not has_digest or (
                    isinstance(proposal.get("commercial_terms"), Mapping)
                    and isinstance(proposal.get("terms_digest"), str)
                    and verify_terms_digest(proposal)
                )
                try:
                    _validate_persistable_payload(proposal, context="proposal evidence")
                    item = EstablishedProposalEvidence.capture(
                        proposal,
                        principal_id=cast(str, self._principal_id),
                        target_binding=cast(str, self._target_binding),
                        account_identity=account_identity,
                        source_adcp_version=source_version,
                        observed_request=observed_request,
                        observed_at=observed_at,
                    )
                except (CompatibilityContinuationError, ProposalEvidencePolicyError):
                    retainable = False
                if not retainable:
                    await self.proposal_evidence_store.delete(
                        proposal["proposal_id"],
                        principal_id=cast(str, self._principal_id),
                        target_binding=cast(str, self._target_binding),
                        account_identity=account_identity,
                        source_adcp_version=source_version,
                    )
                    warnings_out.append(
                        f"Proposal {proposal['proposal_id']} was returned but not retained because "
                        "its payload is unsafe for compatibility persistence."
                    )
                    continue
                await self.proposal_evidence_store.put(item)
                evidence.append(item)
            warnings_out.append(
                "Established proposal evidence is an observation, not an immutable seller hold."
            )

        continuation: LegacyCatalogContinuation | JsonObject | None = None
        if lifecycle is MediaBuyLifecycle.ESTABLISHED and not proposals and products:
            assert account is not None
            continuation = await self._issue_legacy_continuation(
                legacy=self._require_legacy("request_proposals", lifecycle),
                issuance_idempotency_key=issuance_idempotency_key,
                account=account,
                source_version=source_version,
                observed_request=observed_request,
                observed_response=payload,
                products=products,
            )
        elif lifecycle is MediaBuyLifecycle.COMPACT:
            continuation_value = payload.get("purchase_continuation")
            if isinstance(continuation_value, Mapping):
                continuation = dict(continuation_value)

        if lifecycle is MediaBuyLifecycle.COMPACT:
            declared = payload.get("outcome")
            if declared not in {"proposed", "products_available", "rejected"}:
                raise self._unsupported(
                    "request_proposals",
                    lifecycle,
                    "outcome",
                    "Compact proposal discovery returned an unknown completed outcome.",
                )
            outcome = cast(ProposalOutcome, declared)
        elif proposals:
            outcome = "proposed"
        elif products:
            outcome = "products_available"
        else:
            outcome = "legacy_unavailable"

        response = CompatibleProposalResponse(
            operation="request",
            outcome=outcome,
            proposals=proposals,
            products=products,
            reason=_optional_text(payload.get("reason")),
            suggestions=tuple(payload.get("suggestions") or ()),
            incomplete=tuple(
                dict(value)
                for value in (payload.get("incomplete") or ())
                if isinstance(value, Mapping)
            ),
            context=(
                dict(payload["context"]) if isinstance(payload.get("context"), Mapping) else None
            ),
            purchase_continuation=continuation,
            evidence=tuple(evidence),
            raw=payload,
        )
        return CompatibleTaskResult(
            status=TaskStatus.COMPLETED,
            data=response,
            compatibility=self._report(
                lifecycle,
                (tool,),
                warnings=warnings_out,
            ),
            raw=result,
            task_id=task_id,
            _account=account,
            _source_schema_version=source_version,
            _issuance_idempotency_key=issuance_idempotency_key,
            _observed_request=observed_request,
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

    def _select_lifecycle(
        self, compact_tool: str, *, require_catalog_pair: bool = True
    ) -> MediaBuyLifecycle:
        if require_catalog_pair and compact_tool in {"list_products", "buy_products"}:
            return self._select_catalog_purchase_lifecycle(compact_tool)
        established_tool = _COMPACT_TO_ESTABLISHED[compact_tool]
        compact_contract = _is_compact_release(self.negotiated_version)
        if self._preferred_lifecycle == "established":
            if compact_contract and established_tool not in self._tools:
                raise self._unsupported(
                    compact_tool,
                    MediaBuyLifecycle.COMPACT,
                    "established_lifecycle_not_advertised",
                    f"The seller provides no evidence that {established_tool} is callable.",
                )
            return MediaBuyLifecycle.ESTABLISHED
        if compact_contract and compact_tool in self._tools:
            return MediaBuyLifecycle.COMPACT
        if self._preferred_lifecycle == "compact":
            raise self._unsupported(
                compact_tool,
                MediaBuyLifecycle.COMPACT,
                compact_tool,
                f"The negotiated {self.negotiated_version} seller does not advertise "
                f"{compact_tool}.",
            )
        if compact_contract and established_tool not in self._tools:
            raise self._unsupported(
                compact_tool,
                MediaBuyLifecycle.COMPACT,
                "lifecycle_tool_not_advertised",
                f"The seller advertises neither {compact_tool} nor {established_tool}.",
            )
        return MediaBuyLifecycle.ESTABLISHED

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

    def _shared_tool_lifecycle(self, operation: str) -> MediaBuyLifecycle:
        lifecycle = (
            MediaBuyLifecycle.COMPACT
            if _is_compact_release(self.negotiated_version)
            else MediaBuyLifecycle.ESTABLISHED
        )
        if operation not in self._tools:
            raise self._unsupported(
                operation,
                lifecycle,
                "lifecycle_tool_not_advertised",
                f"The seller does not advertise {operation}.",
            )
        return lifecycle

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

    def _legacy_proposal_request(self, request: RequestProposalsRequest) -> JsonObject:
        value = request.model_dump(mode="json", exclude_none=True)
        criteria = _mapping(value.get("criteria"))
        unsupported_criteria = set(criteria) - {"offer_filters", "policy_ids"}
        if unsupported_criteria:
            raise self._unsupported(
                "request_proposals",
                MediaBuyLifecycle.ESTABLISHED,
                "criteria",
                "Established proposal discovery cannot preserve criteria fields: "
                + ", ".join(sorted(unsupported_criteria)),
            )
        unsupported_top = [
            field for field in ("governance_context", "context_id", "opportunity") if field in value
        ]
        if unsupported_top:
            raise self._unsupported(
                "request_proposals",
                MediaBuyLifecycle.ESTABLISHED,
                "/".join(unsupported_top),
                "Established proposal discovery cannot preserve fields: "
                + ", ".join(unsupported_top),
            )
        wire: JsonObject = {
            "adcp_version": self.negotiated_version,
            "adcp_major_version": 3,
            "buying_mode": "brief",
            "brief": value["brief"],
        }
        if _is_compact_release(self.negotiated_version):
            wire["idempotency_key"] = value["idempotency_key"]
        for field in ("account", "brand", "context", "push_notification_config"):
            if field in value:
                wire[field] = value[field]
        if "offer_filters" in criteria:
            wire["filters"] = criteria["offer_filters"]
        if "policy_ids" in criteria:
            wire["required_policies"] = criteria["policy_ids"]
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

    def _require_proposal_mutation_store(
        self, operation: str, lifecycle: MediaBuyLifecycle
    ) -> EstablishedProposalMutationStore:
        store = self.proposal_evidence_store
        if not isinstance(store, EstablishedProposalMutationStore):
            raise self._unsupported(
                operation,
                lifecycle,
                "atomic_proposal_store",
                "The proposal evidence store does not implement atomic batch mutations.",
            )
        if not store.is_durable and not self._allow_non_durable_proposal_mutations:
            raise self._unsupported(
                operation,
                lifecycle,
                "durable_proposal_store",
                "Established proposal mutations require durable shared persistence.",
            )
        if not self._principal_id or not self._target_binding:
            raise self._unsupported(
                operation,
                lifecycle,
                "authenticated_scope",
                "Established proposal mutations require principal_id and target_binding.",
            )
        return store

    def _require_acceptance_recovery_store(
        self, operation: str
    ) -> EstablishedProposalAcceptanceRecoveryStore:
        store = self.proposal_evidence_store
        if not isinstance(store, EstablishedProposalAcceptanceRecoveryStore):
            raise self._unsupported(
                operation,
                MediaBuyLifecycle.ESTABLISHED,
                "acceptance_recovery_store",
                "The proposal evidence store does not implement submitted acceptance recovery.",
            )
        if not store.is_durable and not self._allow_non_durable_proposal_acceptance:
            raise self._unsupported(
                operation,
                MediaBuyLifecycle.ESTABLISHED,
                "durable_acceptance_store",
                "Established acceptance recovery requires durable shared persistence.",
            )
        if not self._principal_id or not self._target_binding:
            raise self._unsupported(
                operation,
                MediaBuyLifecycle.ESTABLISHED,
                "authenticated_scope",
                "Established acceptance recovery requires principal_id and target_binding.",
            )
        return store

    async def _mark_recovered_acceptance_ambiguous(
        self,
        initial: CompatibleTaskResult[Any],
        store: EstablishedProposalAcceptanceRecoveryStore,
    ) -> ProposalAcceptanceRecord:
        record = initial._acceptance_record
        if record is None:
            raise TypeError("acceptance result omitted its durable reservation")
        return await store.mark_acceptance_ambiguous(record)

    async def _mutation_evidence(
        self,
        store: EstablishedProposalMutationStore,
        proposal_ids: Sequence[str],
        *,
        source_version: str,
        operation: str,
    ) -> tuple[EstablishedProposalEvidence, ...]:
        rows = tuple(
            await store.find(
                proposal_ids,
                principal_id=cast(str, self._principal_id),
                target_binding=cast(str, self._target_binding),
                source_adcp_version=source_version,
            )
        )
        by_id: dict[str, list[EstablishedProposalEvidence]] = {}
        for row in rows:
            by_id.setdefault(row.proposal_id, []).append(row)
        if any(len(by_id.get(proposal_id, ())) != 1 for proposal_id in proposal_ids):
            raise self._unsupported(
                operation,
                MediaBuyLifecycle.ESTABLISHED,
                "proposal_evidence",
                "Every proposal must resolve to exactly one retained authenticated account scope.",
            )
        ordered = tuple(by_id[proposal_id][0] for proposal_id in proposal_ids)
        if len({row.account_identity for row in ordered}) != 1:
            raise self._unsupported(
                operation,
                MediaBuyLifecycle.ESTABLISHED,
                "account_scope",
                "One proposal mutation batch cannot cross account scopes.",
            )
        return ordered

    async def _mark_recovered_mutation_ambiguous(
        self,
        initial: CompatibleTaskResult[Any],
    ) -> ProposalMutationRecord | None:
        record = initial._mutation_record
        if record is None:
            return None
        store = self._require_proposal_mutation_store(
            "wait_for_proposal_mutation", initial.compatibility.lifecycle
        )
        return await store.mark_mutation_ambiguous(record)

    async def _reserve_proposal_mutation(
        self,
        store: EstablishedProposalMutationStore,
        evidence: Sequence[EstablishedProposalEvidence],
        *,
        operation: ProposalMutationKind,
        idempotency_key: str,
        reservation_input: JsonObject,
        lifecycle: MediaBuyLifecycle,
    ) -> ProposalMutationReservation:
        try:
            return await store.reserve_mutation(
                evidence,
                operation=operation,
                idempotency_key=idempotency_key,
                request=reservation_input,
                created_at=_aware_utc(self._clock()),
                retry_ttl=self._idempotency_replay_ttl,
            )
        except (ProposalEvidenceChangedError, ProposalMutationConflictError) as exc:
            raise self._unsupported(
                f"{operation.value}_proposals",
                lifecycle,
                "proposal_mutation_conflict",
                "Proposal evidence changed or is fenced by another mutation.",
            ) from exc

    def _proposal_mutation_replay(
        self,
        record: ProposalMutationRecord,
        *,
        proposal_ids: Sequence[str],
        idempotency_key: str,
        reservation_input: JsonObject,
        operation: str,
        lifecycle: MediaBuyLifecycle,
    ) -> TaskResult[Any]:
        if (
            record.proposal_ids != tuple(proposal_ids)
            or record.idempotency_key != idempotency_key
            or record.request_fingerprint != _canonical_fingerprint(reservation_input)
        ):
            raise self._unsupported(
                operation,
                lifecycle,
                "proposal_mutation_conflict",
                "The idempotency key or proposal is reserved for a different mutation.",
            )
        if record.state is ProposalAcceptanceState.COMPLETED and record.result is not None:
            return TaskResult[Any].model_validate(record.result)
        feature = (
            "proposal_mutation_ambiguous"
            if record.state is ProposalAcceptanceState.AMBIGUOUS
            else "proposal_mutation_in_flight"
        )
        raise self._unsupported(
            operation,
            lifecycle,
            feature,
            "The exact proposal mutation is fenced and will not be dispatched again.",
        )

    def _capture_mutation_replacements(
        self,
        proposals: Sequence[JsonObject],
        *,
        source: EstablishedProposalEvidence,
        observed_request: JsonObject,
    ) -> tuple[EstablishedProposalEvidence, ...]:
        captured: list[EstablishedProposalEvidence] = []
        for proposal in proposals:
            try:
                _validate_persistable_payload(proposal, context="proposal refinement evidence")
            except CompatibilityContinuationError:
                continue
            if "terms_digest" in proposal and not (
                isinstance(proposal.get("commercial_terms"), Mapping)
                and isinstance(proposal.get("terms_digest"), str)
                and verify_terms_digest(proposal)
            ):
                raise ValueError("refined proposal terms_digest is not bound to commercial_terms")
            try:
                replacement = EstablishedProposalEvidence.capture(
                    proposal,
                    principal_id=source.principal_id,
                    target_binding=source.target_binding,
                    account_identity=source.account_identity,
                    source_adcp_version=source.source_adcp_version,
                    observed_request=observed_request,
                    observed_at=_aware_utc(self._clock()),
                )
            except ProposalEvidencePolicyError:
                continue
            captured.append(replacement)
        return tuple(captured)

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


def _effective_task_status(result: TaskResult[Any], payload: JsonObject | None) -> TaskStatus:
    if not result.success or result.status is TaskStatus.FAILED:
        return TaskStatus.FAILED
    status_source = payload.get("status") if payload is not None else None
    if status_source is None:
        status_source = (result.metadata or {}).get("status")
    wire_status = _root_value(status_source) if status_source is not None else ""
    return {
        "completed": TaskStatus.COMPLETED,
        "submitted": TaskStatus.SUBMITTED,
        "working": TaskStatus.WORKING,
        "input-required": TaskStatus.NEEDS_INPUT,
        "auth-required": TaskStatus.NEEDS_INPUT,
        "failed": TaskStatus.FAILED,
        "rejected": TaskStatus.FAILED,
        "canceled": TaskStatus.FAILED,
    }.get(wire_status, result.status)


def _task_id(result: TaskResult[Any], payload: JsonObject | None) -> str | None:
    if payload is not None:
        value = _optional_text(payload.get("task_id"))
        if value is not None:
            return value
    metadata = result.metadata or {}
    return _optional_text(metadata.get("task_id"))


_CONTROL_ROOT_CANCELLATION_CONFLICTS = frozenset(
    {
        "name",
        "paused",
        "total_budget",
        "daily_budget_cap",
        "budget_cap_timezone",
        "budget_allocation",
        "pacing",
        "bidding",
        "packages",
        "reporting_webhook",
    }
)
_CONTROL_PACKAGE_FIELDS = frozenset(
    {
        "package_id",
        "bidding",
        "budget",
        "canceled",
        "cancellation_reason",
        "daily_budget_cap",
        "impressions",
        "keyword_targets_add",
        "keyword_targets_remove",
        "min_spend_target",
        "negative_keywords_add",
        "negative_keywords_remove",
        "optimization_goals",
        "pacing",
        "paused",
        "targeting_overlay",
    }
)
_CONTROL_KEYWORD_DELTA_FIELDS = (
    "keyword_targets_add",
    "keyword_targets_remove",
    "negative_keywords_add",
    "negative_keywords_remove",
)


def _validate_control_semantics(payload: JsonObject) -> None:
    if "canceled" in payload:
        if payload["canceled"] is not True:
            raise ValueError("media-buy canceled may only be true when present")
        conflicts = sorted(_CONTROL_ROOT_CANCELLATION_CONFLICTS & payload.keys())
        if conflicts:
            raise ValueError(
                "media-buy cancellation cannot be combined with controls: " + ", ".join(conflicts)
            )
    if "cancellation_reason" in payload and "canceled" not in payload:
        raise ValueError("media-buy cancellation_reason requires canceled=true")

    packages = payload.get("packages")
    if packages is None:
        return
    if not isinstance(packages, list) or not packages:
        raise ValueError("package controls must be a non-empty array")
    package_ids: set[str] = set()
    for index, value in enumerate(packages):
        if not isinstance(value, Mapping):
            raise ValueError(f"packages[{index}] must be an object")
        package = dict(value)
        package_id = _optional_text(package.get("package_id"))
        if package_id is None:
            raise ValueError(f"packages[{index}].package_id must be non-empty")
        if package_id in package_ids:
            raise ValueError(f"packages contains duplicate package_id {package_id!r}")
        package_ids.add(package_id)
        if "canceled" in package:
            if package["canceled"] is not True:
                raise ValueError(f"packages[{index}].canceled may only be true")
            conflicts = sorted(
                {
                    "budget",
                    "daily_budget_cap",
                    "min_spend_target",
                    "impressions",
                    "pacing",
                    "bidding",
                    "paused",
                    "targeting_overlay",
                    *_CONTROL_KEYWORD_DELTA_FIELDS,
                    "optimization_goals",
                }
                & package.keys()
            )
            if conflicts:
                raise ValueError(
                    f"packages[{index}] cancellation cannot be combined with controls: "
                    + ", ".join(conflicts)
                )
        if "cancellation_reason" in package and "canceled" not in package:
            raise ValueError(f"packages[{index}].cancellation_reason requires canceled=true")
        if "targeting_overlay" in package and any(
            field in package for field in _CONTROL_KEYWORD_DELTA_FIELDS
        ):
            raise ValueError(
                f"packages[{index}].targeting_overlay cannot be combined with keyword deltas"
            )


def _legacy_control_request(
    payload: JsonObject,
    *,
    negotiated_version: str,
    source_version: str,
) -> JsonObject:
    accepted = {
        "account",
        "adcp_major_version",
        "adcp_version",
        "bidding",
        "budget_allocation",
        "budget_cap_timezone",
        "canceled",
        "cancellation_reason",
        "context",
        "daily_budget_cap",
        "ext",
        "governance_context",
        "idempotency_key",
        "media_buy_id",
        "name",
        "packages",
        "pacing",
        "paused",
        "push_notification_config",
        "reporting_webhook",
        "revision",
        "total_budget",
    }
    unknown = set(payload) - accepted
    if unknown:
        raise ValueError(
            "established control cannot preserve fields: " + ", ".join(sorted(unknown))
        )
    compact_only = {
        "bidding",
        "budget_allocation",
        "budget_cap_timezone",
        "daily_budget_cap",
        "ext",
        "governance_context",
        "name",
        "pacing",
        "total_budget",
    }
    unsupported = sorted(compact_only & payload.keys())
    if unsupported:
        raise ValueError(
            f"the negotiated {negotiated_version} update_media_buy cannot represent: "
            + ", ".join(unsupported)
        )

    packages_value = payload.get("packages")
    packages: list[JsonObject] | None = None
    if isinstance(packages_value, list):
        packages = []
        for index, value in enumerate(packages_value):
            package = dict(cast(Mapping[str, Any], value))
            unknown_package = set(package) - _CONTROL_PACKAGE_FIELDS
            if unknown_package:
                raise ValueError(
                    f"packages[{index}] has no established projection for: "
                    + ", ".join(sorted(unknown_package))
                )
            unsupported_package = {
                "bidding",
                "daily_budget_cap",
                "min_spend_target",
                "optimization_goals",
            } & package.keys()
            if unsupported_package:
                raise ValueError(
                    f"the negotiated {negotiated_version} packages[{index}] cannot represent: "
                    + ", ".join(sorted(unsupported_package))
                )
            if "budget" in package and package["budget"] is None:
                raise ValueError(
                    f"the negotiated {negotiated_version} packages[{index}].budget "
                    "cannot represent null"
                )
            for field in (
                "keyword_targets_remove",
                "negative_keywords_add",
                "negative_keywords_remove",
            ):
                for item_index, item in enumerate(package.get(field) or ()):
                    if isinstance(item, Mapping) and "bid_price" in item:
                        raise ValueError(
                            f"the negotiated {negotiated_version} packages[{index}]."
                            f"{field}[{item_index}] cannot represent bid_price"
                        )
            packages.append(package)

    wire: JsonObject = {
        "adcp_version": negotiated_version,
        "adcp_major_version": 3,
        "idempotency_key": payload["idempotency_key"],
        "account": payload["account"],
        "media_buy_id": payload["media_buy_id"],
        "revision": payload["revision"],
    }
    for field in (
        "paused",
        "canceled",
        "cancellation_reason",
        "reporting_webhook",
        "push_notification_config",
        "context",
    ):
        if field in payload:
            wire[field] = payload[field]
    if packages is not None:
        wire["packages"] = packages
    _assert_declared_legacy_fields(
        wire,
        tool="update_media_buy",
        source_version=source_version,
        nested=("packages",),
    )
    validation = validate_request("update_media_buy", wire, version=source_version)
    if not validation.valid or validation.variant == "skipped":
        raise ValueError(
            "control cannot be represented by exact established update_media_buy: "
            f"{format_issues(validation.issues)}"
        )
    return wire


def _project_control_task(
    result: TaskResult[Any],
    *,
    lifecycle: MediaBuyLifecycle,
    compatibility: MediaBuyCompatibilityReport,
    source_version: str,
) -> CompatibleTaskResult[CompatibleControlMediaBuyResponse]:
    payload = _json_object(result.data)
    status = _effective_task_status(result, payload)
    if status is not TaskStatus.COMPLETED or not result.success or payload is None:
        return CompatibleTaskResult(
            status=status,
            data=None,
            compatibility=compatibility,
            raw=result,
            task_id=_task_id(result, payload),
        )
    tool = "control_media_buy" if lifecycle is MediaBuyLifecycle.COMPACT else "update_media_buy"
    validation = validate_response(tool, payload, version=source_version)
    if not validation.valid or validation.variant == "skipped":
        raise ValueError(
            f"{tool} returned an invalid exact-version response: {format_issues(validation.issues)}"
        )
    media_buy_id = _optional_text(payload.get("media_buy_id"))
    revision = payload.get("revision")
    if (
        media_buy_id is None
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
    ):
        raise ValueError(f"{tool} completed without a valid media_buy_id and revision")
    return CompatibleTaskResult(
        status=TaskStatus.COMPLETED,
        data=CompatibleControlMediaBuyResponse(
            operation="control",
            media_buy_id=media_buy_id,
            revision=revision,
            raw=payload,
        ),
        compatibility=compatibility,
        raw=result,
        task_id=_task_id(result, payload),
    )


def _sanitize_established_readback_request(
    request: GetMediaBuysRequest | GetMediaBuyDeliveryRequest,
    *,
    operation: Literal["get_media_buys", "get_media_buy_delivery"],
    negotiated_version: str,
) -> GetMediaBuysRequest | GetMediaBuyDeliveryRequest:
    """Suppress current-model defaults absent from an older wire contract."""

    _, minor = _release_minor(negotiated_version)
    if operation == "get_media_buys":
        assert isinstance(request, GetMediaBuysRequest)
        updates: JsonObject = {"indicator_types": None}
        if minor < 1:
            updates.update(
                {
                    "include_webhook_activity": None,
                    "webhook_activity_limit": None,
                }
            )
        return request.model_copy(update=updates)
    assert isinstance(request, GetMediaBuyDeliveryRequest)
    updates = {
        "pagination": None,
        "reporting_revision_id": None,
        "requested_metrics": None,
    }
    if minor < 1:
        updates.update({"include_window_breakdown": None, "time_granularity": None})
    return request.model_copy(update=updates)


def _project_readback_task(
    result: TaskResult[Any],
    *,
    operation: Literal["get_media_buys", "get_media_buy_delivery"],
    compatibility: MediaBuyCompatibilityReport,
    source_version: str,
) -> CompatibleTaskResult[CompatibleMediaBuyReadbackResponse]:
    payload = _json_object(result.data)
    status = _effective_task_status(result, payload)
    if status is not TaskStatus.COMPLETED or not result.success or payload is None:
        return CompatibleTaskResult(
            status=status,
            data=None,
            compatibility=compatibility,
            raw=result,
            task_id=_task_id(result, payload),
        )
    validation = validate_response(operation, payload, version=source_version)
    if not validation.valid or validation.variant == "skipped":
        raise ValueError(
            f"{operation} returned an invalid exact-version response: "
            f"{format_issues(validation.issues)}"
        )
    return CompatibleTaskResult(
        status=TaskStatus.COMPLETED,
        data=CompatibleMediaBuyReadbackResponse(operation=operation, raw=payload),
        compatibility=compatibility,
        raw=result,
        task_id=_task_id(result, payload),
    )


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


def _unique_proposal_ids(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    proposal_ids: list[str] = []
    for row in value:
        if not isinstance(row, Mapping) or not _optional_text(row.get("proposal_id")):
            raise ValueError(f"every {field} entry must carry proposal_id")
        proposal_ids.append(cast(str, row["proposal_id"]))
    if len(set(proposal_ids)) != len(proposal_ids):
        raise ValueError(f"{field} contains duplicate proposal_id values")
    return tuple(proposal_ids)


def _legacy_refine_request(payload: JsonObject, *, source_version: str) -> JsonObject:
    unsupported_top = set(payload) - {
        "adcp_major_version",
        "adcp_version",
        "context",
        "governance_context",
        "context_id",
        "idempotency_key",
        "push_notification_config",
        "refinements",
    }
    if unsupported_top:
        raise ValueError(
            "established refinement cannot preserve fields: " + ", ".join(sorted(unsupported_top))
        )
    if "governance_context" in payload or "context_id" in payload:
        raise ValueError("established refinement cannot preserve governance_context or context_id")
    legacy_rows: list[JsonObject] = []
    refinements = payload.get("refinements")
    if not isinstance(refinements, list):
        raise ValueError("refinements must be an array")
    for refinement in refinements:
        if not isinstance(refinement, Mapping):
            raise ValueError("every refinement must be an object")
        row = dict(refinement)
        proposal_id = _optional_text(row.get("proposal_id"))
        action = _root_value(row.get("action", "revise"))
        if proposal_id is None:
            raise ValueError("every refinement requires proposal_id")
        if action == "finalize":
            unsupported = set(row) - {"proposal_id", "action"}
            if unsupported:
                raise ValueError(
                    "legacy finalize cannot preserve fields: " + ", ".join(sorted(unsupported))
                )
            legacy_rows.append(
                {"scope": "proposal", "proposal_id": proposal_id, "action": "finalize"}
            )
            continue
        if action != "revise":
            raise ValueError(f"unsupported proposal refinement action: {action}")
        unsupported = set(row) - {
            "proposal_id",
            "action",
            "ask",
            "product_changes",
            "constraints",
            "alternatives",
            "criteria",
            "change_kind",
        }
        if unsupported:
            raise ValueError(
                "legacy revision cannot preserve fields: " + ", ".join(sorted(unsupported))
            )
        structured = [
            field
            for field in ("constraints", "alternatives", "criteria", "change_kind")
            if field in row
        ]
        if structured:
            raise ValueError(
                "legacy revision cannot guarantee structured fields: " + ", ".join(structured)
            )
        proposal_row: JsonObject = {"scope": "proposal", "proposal_id": proposal_id}
        if "ask" in row:
            proposal_row["ask"] = row["ask"]
        legacy_rows.append(proposal_row)
        changes = row.get("product_changes") or {}
        if not isinstance(changes, Mapping):
            raise ValueError("product_changes must be an object")
        for product_id, change in changes.items():
            action_value = _root_value(change)
            if action_value not in {"include", "omit"}:
                raise ValueError(f"legacy product refinement cannot preserve action {action_value}")
            legacy_rows.append(
                {"scope": "product", "product_id": product_id, "action": action_value}
            )
    wire: JsonObject = {
        "adcp_major_version": 3,
        "buying_mode": "refine",
        "refine": legacy_rows,
    }
    for field in ("context", "push_notification_config"):
        if field in payload:
            wire[field] = payload[field]
    _assert_declared_legacy_fields(
        wire,
        tool="get_products",
        source_version=source_version,
        nested=("refine",),
    )
    validation = validate_request("get_products", wire, version=source_version)
    if not validation.valid or validation.variant == "skipped":
        raise ValueError(
            "refinement cannot be represented by exact established get_products: "
            f"{format_issues(validation.issues)}"
        )
    return wire


def _legacy_decline_request(payload: JsonObject, *, source_version: str) -> JsonObject:
    unsupported = set(payload) - {
        "adcp_major_version",
        "adcp_version",
        "context",
        "context_id",
        "declines",
        "governance_context",
        "idempotency_key",
        "opportunity",
        "push_notification_config",
    }
    if unsupported:
        raise ValueError(
            "established decline cannot preserve fields: " + ", ".join(sorted(unsupported))
        )
    if any(field in payload for field in ("context_id", "governance_context", "opportunity")):
        raise ValueError(
            "established decline cannot preserve context_id, governance_context, or opportunity"
        )
    declines = payload.get("declines")
    if not isinstance(declines, list):
        raise ValueError("declines must be an array")
    wire: JsonObject = {
        "adcp_major_version": 3,
        "buying_mode": "refine",
        "refine": [
            {
                "scope": "proposal",
                "proposal_id": decline["proposal_id"],
                "action": "omit",
            }
            for decline in declines
        ],
    }
    for field in ("context", "push_notification_config"):
        if field in payload:
            wire[field] = payload[field]
    _assert_declared_legacy_fields(
        wire,
        tool="get_products",
        source_version=source_version,
        nested=("refine",),
    )
    validation = validate_request("get_products", wire, version=source_version)
    if not validation.valid or validation.variant == "skipped":
        raise ValueError(
            "decline cannot be represented by exact established get_products: "
            f"{format_issues(validation.issues)}"
        )
    return wire


def _refinement_proposal_rows(
    payload: JsonObject, *, lifecycle: MediaBuyLifecycle
) -> tuple[JsonObject, ...]:
    if lifecycle is MediaBuyLifecycle.ESTABLISHED:
        return _optional_rows(payload.get("proposals"), id_field="proposal_id")
    values: list[Any] = []
    for result in payload.get("results") or ():
        if not isinstance(result, Mapping):
            continue
        child_rows = result.get("proposals")
        if isinstance(child_rows, list):
            values.extend(child_rows)
        if result.get("proposal") is not None:
            values.append(result["proposal"])
    proposals = _optional_rows(values, id_field="proposal_id")
    if len(proposals) > 250 or len({row["proposal_id"] for row in proposals}) != len(proposals):
        raise ValueError("refine_proposals returned excessive or duplicate successor proposals")
    for proposal in proposals:
        if not verify_terms_digest(proposal):
            raise ValueError("refine_proposals returned a successor with an invalid terms_digest")
    return proposals


def _project_refinement_task(
    result: TaskResult[Any],
    *,
    lifecycle: MediaBuyLifecycle,
    proposal_ids: Sequence[str],
    compatibility: MediaBuyCompatibilityReport,
    source_version: str,
) -> CompatibleTaskResult[CompatibleRefineProposalsResponse]:
    payload = _json_object(result.data)
    status = _effective_task_status(result, payload)
    if status is not TaskStatus.COMPLETED or not result.success or payload is None:
        return CompatibleTaskResult(
            status=status,
            data=None,
            compatibility=compatibility,
            raw=result,
            task_id=_task_id(result, payload),
        )
    tool = "refine_proposals" if lifecycle is MediaBuyLifecycle.COMPACT else "get_products"
    validation = validate_response(tool, payload, version=source_version)
    if not validation.valid or validation.variant == "skipped":
        raise ValueError(
            f"{tool} returned an invalid exact-version response: {format_issues(validation.issues)}"
        )
    proposals = _refinement_proposal_rows(payload, lifecycle=lifecycle)
    products = _optional_rows(payload.get("products"), id_field="product_id")
    results_value = payload.get("results")
    results = (
        tuple(dict(row) for row in results_value if isinstance(row, Mapping))
        if isinstance(results_value, list)
        else ()
    )
    if lifecycle is MediaBuyLifecycle.COMPACT:
        if len(results) != len(proposal_ids):
            raise ValueError("refine_proposals result count does not match the request")
        for index, (result_row, proposal_id) in enumerate(zip(results, proposal_ids, strict=True)):
            if result_row.get("source_proposal_id") != proposal_id:
                raise ValueError(
                    f"refine_proposals result {index} is not bound to its source proposal"
                )
            children = list(result_row.get("proposals") or ())
            if result_row.get("proposal") is not None:
                children.append(result_row["proposal"])
            if any(
                not isinstance(child, Mapping) or child.get("parent_proposal_id") != proposal_id
                for child in children
            ):
                raise ValueError(
                    f"refine_proposals result {index} returned invalid proposal lineage"
                )
    response = CompatibleRefineProposalsResponse(
        operation="refine",
        outcome=(
            "native_results"
            if lifecycle is MediaBuyLifecycle.COMPACT
            else "legacy_projected" if proposals else "legacy_unavailable"
        ),
        proposals=proposals,
        products=products,
        results=results,
        raw=payload,
    )
    return CompatibleTaskResult(
        status=TaskStatus.COMPLETED,
        data=response,
        compatibility=compatibility,
        raw=result,
        task_id=_task_id(result, payload),
    )


def _valid_compact_decline_completion(payload: JsonObject) -> bool:
    """Strict fallback for the draft-07 ``not`` validator's sync-arm bug."""

    if set(payload) - {"results", "context", "ext", "replayed"}:
        return False
    values = payload.get("results")
    if not isinstance(values, list) or not values:
        return False
    for value in values:
        if not isinstance(value, Mapping) or set(value) - {"proposal_id", "outcome", "reason"}:
            return False
        if not _optional_text(value.get("proposal_id")):
            return False
        outcome = value.get("outcome")
        if outcome == "declined" and "reason" not in value:
            continue
        if outcome == "unable" and _optional_text(value.get("reason")):
            continue
        return False
    return payload.get("replayed", True) is True


def _project_decline_task(
    result: TaskResult[Any],
    *,
    lifecycle: MediaBuyLifecycle,
    proposal_ids: Sequence[str],
    compatibility: MediaBuyCompatibilityReport,
    source_version: str,
) -> CompatibleTaskResult[CompatibleDeclineProposalsResponse]:
    payload = _json_object(result.data)
    status = _effective_task_status(result, payload)
    if status is not TaskStatus.COMPLETED or not result.success or payload is None:
        return CompatibleTaskResult(
            status=status,
            data=None,
            compatibility=compatibility,
            raw=result,
            task_id=_task_id(result, payload),
        )
    tool = "decline_proposals" if lifecycle is MediaBuyLifecycle.COMPACT else "get_products"
    validation = validate_response(tool, payload, version=source_version)
    compact_sync_valid = (
        lifecycle is MediaBuyLifecycle.COMPACT and _valid_compact_decline_completion(payload)
    )
    if (not validation.valid and not compact_sync_valid) or validation.variant == "skipped":
        raise ValueError(
            f"{tool} returned an invalid exact-version response: {format_issues(validation.issues)}"
        )
    if lifecycle is MediaBuyLifecycle.COMPACT:
        values = payload.get("results")
        if not isinstance(values, list) or len(values) != len(proposal_ids):
            raise ValueError("decline_proposals result count does not match the request")
        results = tuple(dict(value) for value in values if isinstance(value, Mapping))
        if len(results) != len(proposal_ids) or any(
            result_row.get("proposal_id") != proposal_id
            for result_row, proposal_id in zip(results, proposal_ids, strict=True)
        ):
            raise ValueError("decline_proposals result ordering or proposal binding is invalid")
        outcome: Literal["native_results", "legacy_unconfirmed"] = "native_results"
    else:
        applied = [
            dict(value)
            for value in payload.get("refinement_applied") or ()
            if isinstance(value, Mapping)
        ]
        results_list: list[JsonObject] = []
        for proposal_id in proposal_ids:
            unable = next(
                (
                    row
                    for row in applied
                    if row.get("scope") == "proposal"
                    and row.get("proposal_id") == proposal_id
                    and row.get("status") == "unable"
                ),
                None,
            )
            results_list.append(
                {
                    "proposal_id": proposal_id,
                    "outcome": "unable" if unable else "unconfirmed",
                    **(
                        {
                            "reason": _optional_text(unable.get("notes"))
                            or "The established seller could not apply the decline."
                        }
                        if unable
                        else {}
                    ),
                }
            )
        results = tuple(results_list)
        outcome = "legacy_unconfirmed"
    return CompatibleTaskResult(
        status=TaskStatus.COMPLETED,
        data=CompatibleDeclineProposalsResponse(
            operation="decline", outcome=outcome, results=results, raw=payload
        ),
        compatibility=compatibility,
        raw=result,
        task_id=_task_id(result, payload),
    )


def _legacy_accept_proposal_request(
    payload: JsonObject,
    *,
    fallback: JsonObject,
    negotiated_version: str,
    source_version: str,
) -> JsonObject:
    accepted_fields = {
        "account",
        "adcp_major_version",
        "adcp_version",
        "budget_cap_timezone",
        "context",
        "daily_budget_cap",
        "governance_context",
        "idempotency_key",
        "io_acceptance",
        "opportunity",
        "proposal_id",
        "proposal_terms_digest",
        "purchase_order_ref",
        "push_notification_config",
        "reporting_webhook",
        "total_budget",
    }
    unknown = set(payload) - accepted_fields
    if unknown:
        raise ValueError(
            "established proposal acceptance cannot preserve fields: " + ", ".join(sorted(unknown))
        )
    if "total_budget" not in payload:
        raise ValueError(
            "established create_media_buy requires total_budget when executing proposal_id"
        )
    wire: JsonObject = {
        "adcp_version": negotiated_version,
        "adcp_major_version": 3,
        "idempotency_key": payload["idempotency_key"],
        "account": payload["account"],
        "proposal_id": payload["proposal_id"],
        "total_budget": payload["total_budget"],
        **fallback,
    }
    passthrough = {
        "budget_cap_timezone",
        "context",
        "daily_budget_cap",
        "governance_context",
        "io_acceptance",
        "opportunity",
        "push_notification_config",
        "reporting_webhook",
    }
    for field in passthrough:
        if field in payload:
            wire[field] = payload[field]
    if "purchase_order_ref" in payload:
        wire["po_number"] = payload["purchase_order_ref"]
    _assert_declared_legacy_fields(
        wire,
        tool="create_media_buy",
        source_version=source_version,
    )
    validation = validate_request("create_media_buy", wire, version=source_version)
    if not validation.valid or validation.variant == "skipped":
        raise ValueError(
            "acceptance cannot be represented by the exact established create_media_buy "
            f"contract: {format_issues(validation.issues)}"
        )
    return wire


def _resolve_established_acceptance(
    payload: JsonObject,
    *,
    proposal: JsonObject,
    digest_bound: bool,
    fallback: JsonObject,
) -> tuple[JsonObject, JsonObject]:
    terms = _mapping(proposal.get("commercial_terms"))
    resolved = dict(payload)
    for field in (
        "total_budget",
        "daily_budget_cap",
        "budget_cap_timezone",
        "purchase_order_ref",
    ):
        term_value = terms.get(field)
        input_value = payload.get(field)
        if digest_bound and term_value is not None:
            if input_value is not None and _canonical_fingerprint(
                {"value": input_value}
            ) != _canonical_fingerprint({"value": term_value}):
                raise ValueError(f"{field} conflicts with the digest-bound seller terms")
            resolved[field] = term_value
    routing: JsonObject = {}
    for field in ("brand", "start_time", "end_time"):
        value = terms.get(field) if digest_bound else None
        if value is None:
            value = fallback.get(field)
        if value is None:
            raise ValueError(
                "proposal terms do not carry legacy routing fields; established_fallback "
                "must supply brand, start_time, and end_time"
            )
        routing[field] = value
    return resolved, routing


def _assert_proposal_observation_acceptable(proposal: Mapping[str, Any], *, now: datetime) -> None:
    status = _optional_text(proposal.get("proposal_status"))
    if status is not None and status != "committed":
        raise ValueError(f"retained proposal is not executable (proposal_status={status})")
    kind = _optional_text(proposal.get("proposal_kind"))
    if kind is not None and kind != "new_media_buy":
        raise ValueError(f"retained proposal kind cannot create a new media buy ({kind})")
    expires_at = _optional_text(proposal.get("expires_at"))
    if expires_at is None:
        return
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        expiry = _aware_utc(parsed)
    except ValueError as exc:
        raise ValueError("retained proposal has an invalid expires_at timestamp") from exc
    if expiry <= now:
        raise ValueError("retained proposal evidence has expired")


def _canonical_fingerprint(value: Mapping[str, Any]) -> str:
    try:
        canonical = rfc8785.dumps(dict(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("acceptance request must be JSON-canonicalizable") from exc
    return hashlib.sha256(canonical).hexdigest()


def _compatible_acceptance_result(
    result: TaskResult[Any],
    *,
    compatibility: MediaBuyCompatibilityReport,
) -> CompatibleTaskResult[Any]:
    return CompatibleTaskResult(
        status=result.status,
        data=result.data,
        compatibility=compatibility,
        raw=result,
        task_id=_task_id(result, _json_object(result.data)),
    )


def _persistable_task_result(result: TaskResult[Any]) -> JsonObject:
    dumped = result.model_dump(mode="json", exclude_none=True)
    safe_fields = {
        "adcp_error",
        "data",
        "error",
        "message",
        "needs_input",
        "replayed",
        "status",
        "submitted",
        "success",
    }
    persisted = {key: value for key, value in dumped.items() if key in safe_fields}
    _validate_persistable_payload(persisted, context="proposal acceptance result")
    return persisted


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
    "CompatibleControlMediaBuyResponse",
    "CompatibleDeclineProposalsResponse",
    "CompatibleMediaBuyReadbackResponse",
    "CompatibleProposalResponse",
    "CompatiblePurchaseResult",
    "CompatibleRefineProposalsResponse",
    "CompatibleTaskResult",
    "LegacyCatalogContinuation",
    "MediaBuyCompatibility",
    "MediaBuyCompatibilityReport",
    "MediaBuyLifecycle",
    "MediaBuyLifecycleCompatibilityError",
    "MediaBuyLifecycleCoordinator",
    "NegotiatedCatalogBuyer",
]
