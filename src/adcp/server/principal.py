"""State primitives for implementing the AdCP 3.2 principal layer.

This module deliberately does not authenticate callers or prove control of
external resources.  The server resolves a stable transport identity first,
then passes it here; adopter hooks perform endpoint and provider proof before
transitioning a destination from ``validating`` to ``ready``.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel

from adcp.server.idempotency import canonical_json_sha256
from adcp.types import (
    AgentDeclarations,
    AgentNotificationConfig,
    AgentNotificationConfigState,
    AgentReportingDestination,
    AgentReportingDestinationState,
    GetPrincipalRequest,
    GetPrincipalResponse,
    PrincipalConfiguration,
    PrincipalDeclarationsState,
    PrincipalState,
    SyncPrincipalRequest,
    SyncPrincipalResponse,
)


@dataclass(frozen=True)
class PrincipalIdentity:
    """Seller-resolved stable identity; never derive this from request args."""

    subject: str
    kind: str


@dataclass(frozen=True)
class PrincipalChange:
    """State invalidation supplied to an adopter-owned webhook emitter."""

    principal_id: str
    subject: str
    changed_at: datetime
    reason: str
    destination_id: str | None = None


PrincipalChangeEmitter = Callable[[PrincipalChange], Awaitable[None]]
DestinationProofHook = Callable[
    [PrincipalIdentity, AgentReportingDestination, AgentReportingDestinationState | None],
    Awaitable[AgentReportingDestinationState],
]
NotificationProofHook = Callable[
    [PrincipalIdentity, AgentNotificationConfig], Awaitable[AgentNotificationConfigState]
]


@dataclass
class PrincipalRecord:
    subject: str
    principal_id: str
    principal_kind: str
    configuration_version: str | None = None
    configuration: PrincipalState | None = None
    idempotency: dict[str, tuple[str, SyncPrincipalResponse]] = field(default_factory=dict)
    # This is an internal revision, separate from the caller-visible configuration
    # version.  Idempotency writes and seller-driven state transitions need a CAS
    # fence even when they intentionally do not change configuration_version.
    store_revision: int = 0


class PrincipalRecordStore(Protocol):
    async def get(self, subject: str) -> PrincipalRecord | None:
        raise NotImplementedError

    async def compare_and_swap(
        self,
        subject: str,
        expected_store_revision: int | None,
        record: PrincipalRecord,
    ) -> bool:
        """Atomically replace one record when its internal revision still matches.

        ``None`` means the record must not yet exist. Implementations must make
        this operation atomic across all workers sharing the backing store.
        """
        raise NotImplementedError


class InMemoryPrincipalRecordStore:
    """Process-local reference store suitable for tests and single workers."""

    def __init__(self) -> None:
        self._records: dict[str, PrincipalRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, subject: str) -> PrincipalRecord | None:
        record = self._records.get(subject)
        return _copy_record(record) if record else None

    async def compare_and_swap(
        self,
        subject: str,
        expected_store_revision: int | None,
        record: PrincipalRecord,
    ) -> bool:
        async with self._lock:
            existing = self._records.get(subject)
            if expected_store_revision is None:
                if existing is not None:
                    return False
            elif existing is None or existing.store_revision != expected_store_revision:
                return False
            self._records[subject] = _copy_record(record)
            return True


def _copy_record(record: PrincipalRecord) -> PrincipalRecord:
    return PrincipalRecord(
        subject=record.subject,
        principal_id=record.principal_id,
        principal_kind=record.principal_kind,
        configuration_version=record.configuration_version,
        configuration=(
            record.configuration.model_copy(deep=True) if record.configuration else None
        ),
        idempotency={
            key: (digest, response.model_copy(deep=True))
            for key, (digest, response) in record.idempotency.items()
        },
        store_revision=record.store_revision,
    )


def _json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _failed(code: str, message: str, *, context: object | None = None) -> SyncPrincipalResponse:
    return SyncPrincipalResponse.model_validate(
        {
            "status": "rejected",
            "result": {"kind": "failed", "errors": [{"code": code, "message": message}]},
            "context": context,
        }
    )


def _notification_state(config: AgentNotificationConfig) -> AgentNotificationConfigState:
    value = config.model_dump(mode="json", exclude_none=True)
    authentication = value.get("authentication")
    if isinstance(authentication, dict):
        authentication.pop("credentials", None)
    return AgentNotificationConfigState.model_validate(value)


class _PrincipalValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_MEDIA_BUY_NOTIFICATION_TYPES = frozenset(
    {"scheduled", "final", "delayed", "adjusted", "window_update", "impairment"}
)
_ACCOUNT_NOTIFICATION_TYPES = frozenset(
    {
        "creative.status_changed",
        "creative.assignment_changed",
        "indicators.changed",
        "creative.purged",
        "account.status_changed",
        "account.change_recorded",
        "product.created",
        "product.updated",
        "product.priced",
        "product.removed",
        "signal.created",
        "signal.updated",
        "signal.priced",
        "signal.removed",
        "wholesale_feed.bulk_change",
        "reporting.delivery_ready",
        "reporting.status_changed",
    }
)
_SECRET_COORDINATE_RE = re.compile(
    r"(?:bearer\s+|private[ _-]?key|(?:api|access)[ _-]?key\s*[=:]|"
    r"(?:token|secret|password|signature|sig)\s*[=:])",
    re.IGNORECASE,
)


def _require_unique(values: Sequence[object], attribute: str, section: str) -> None:
    seen: set[str] = set()
    for index, value in enumerate(values):
        key = str(getattr(value, attribute))
        if key in seen:
            raise ValueError(f"{section}[{index}].{attribute} duplicates an earlier entry")
        seen.add(key)


def _validate_webhook_destination_url(url: str, *, field: str) -> Any:
    """Lazily invoke webhook validation without creating a facade import cycle."""
    from adcp.webhooks import validate_webhook_destination_url

    return validate_webhook_destination_url(url, field=field)


def _normalize_notification_config(
    config: AgentNotificationConfig, index: int
) -> AgentNotificationConfig:
    event_types = {str(item) for item in config.event_types}
    if event_types & _MEDIA_BUY_NOTIFICATION_TYPES:
        raise ValueError(
            f"notification_configs[{index}].event_types contains a media-buy-anchored event"
        )
    if event_types & _ACCOUNT_NOTIFICATION_TYPES and config.all_authorized_accounts is not True:
        raise ValueError(
            f"notification_configs[{index}].all_authorized_accounts must be true for account events"
        )
    if _SECRET_COORDINATE_RE.search(str(config.url)):
        raise ValueError(f"notification_configs[{index}].url must not embed a credential or secret")
    try:
        # This performs registration-time HTTPS, normalized-host, and
        # reserved-range checks for active *and* inactive registrations.
        validation = _validate_webhook_destination_url(
            str(config.url), field=f"notification_configs[{index}].url"
        )
    except Exception as error:
        raise ValueError(str(error)) from error
    parsed = urlsplit(validation.effective_url)
    port = parsed.port
    netloc = validation.hostname
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    normalized_url = urlunsplit(("https", netloc, parsed.path, parsed.query, ""))
    payload = config.model_dump(mode="json", exclude_none=True)
    payload["url"] = normalized_url
    return AgentNotificationConfig.model_validate(payload)


def _normalize_coordinate(value: str, *, field: str) -> str:
    """Reject secret-bearing locators and normalize URL-like coordinates."""
    normalized = unicodedata.normalize("NFC", value)
    if _SECRET_COORDINATE_RE.search(normalized):
        raise ValueError(f"{field} must not contain credentials or a secret")
    parsed = urlsplit(normalized)
    if not parsed.scheme:
        return normalized
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not include query or fragment components")
    # ``abfss://container@account/...`` is a legitimate provider locator; a
    # colon in the authority is what distinguishes credential-style userinfo.
    authority = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
    scheme = parsed.scheme.lower()
    if authority and (":" in authority or scheme not in {"abfs", "abfss"}):
        raise ValueError(f"{field} must not embed userinfo credentials")
    if parsed.hostname is None:
        raise ValueError(f"{field} must have a host when it is URL-like")
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{field} has an invalid port") from error
    default_port = {"http": 80, "https": 443}.get(scheme)
    netloc = host if port is None or port == default_port else f"{host}:{port}"
    if authority:
        netloc = f"{authority}@{netloc}"
    path = re.sub(r"/{2,}", "/", parsed.path)
    return urlunsplit((scheme, netloc, path, "", ""))


def _normalize_destination(destination: AgentReportingDestination) -> AgentReportingDestination:
    payload = destination.model_dump(mode="json", exclude_none=True)
    if "location" in payload:
        payload["location"] = _normalize_coordinate(
            payload["location"], field="reporting_destinations.location"
        )
    recipient = payload.get("recipient")
    if isinstance(recipient, dict):
        identity = recipient.get("identity")
        if isinstance(identity, str):
            recipient["identity"] = _normalize_coordinate(
                identity, field="reporting_destinations.recipient.identity"
            )
    return AgentReportingDestination.model_validate(payload)


class PrincipalService:
    """Implement atomic principal reads, guarded section sync, and proof state."""

    def __init__(
        self,
        store: PrincipalRecordStore | None = None,
        *,
        destination_proof: DestinationProofHook | None = None,
        notification_proof: NotificationProofHook | None = None,
        emit_change: PrincipalChangeEmitter | None = None,
        accepted_async_adcp_versions: tuple[str, ...] = ("3.2",),
        accepted_webhook_signing_algorithms: tuple[str, ...] = (),
        accepted_experimental_features: tuple[str, ...] = (),
    ) -> None:
        self._store = store or InMemoryPrincipalRecordStore()
        self._destination_proof = destination_proof
        self._notification_proof = notification_proof
        self._emit_change = emit_change
        self._accepted_declarations = {
            "async_adcp_versions": accepted_async_adcp_versions,
            "webhook_signing_algorithms": accepted_webhook_signing_algorithms,
            "experimental_features": accepted_experimental_features,
        }

    def set_declaration_support(
        self,
        *,
        async_adcp_versions: tuple[str, ...] | None = None,
        webhook_signing_algorithms: tuple[str, ...] | None = None,
        experimental_features: tuple[str, ...] | None = None,
    ) -> None:
        """Update objective seller support before refreshing affected records."""
        replacements = {
            "async_adcp_versions": async_adcp_versions,
            "webhook_signing_algorithms": webhook_signing_algorithms,
            "experimental_features": experimental_features,
        }
        for axis, values in replacements.items():
            if values is not None:
                self._accepted_declarations[axis] = values

    async def recognize(self, identity: PrincipalIdentity) -> str:
        """Materialize a durable recognized principal without configuration."""
        while True:
            record = await self._store.get(identity.subject)
            if record is not None:
                return record.principal_id
            record = PrincipalRecord(
                identity.subject, f"prin:{uuid4()}", identity.kind, store_revision=1
            )
            if await self._store.compare_and_swap(identity.subject, None, record):
                return record.principal_id

    async def get_principal(
        self,
        identity: PrincipalIdentity,
        request: GetPrincipalRequest | Mapping[str, Any] | None = None,
    ) -> GetPrincipalResponse:
        request = GetPrincipalRequest.model_validate(request or {})
        record = await self._store.get(identity.subject)
        if record is None:
            return GetPrincipalResponse.model_validate(
                {"result": {"kind": "unconfigured"}, "context": request.context}
            )
        if record.principal_kind != identity.kind:
            return GetPrincipalResponse.model_validate(
                {
                    "status": "rejected",
                    "result": {
                        "kind": "failed",
                        "errors": [
                            {
                                "code": "CONFLICT",
                                "message": "authenticated principal kind changed",
                            }
                        ],
                    },
                    "context": request.context,
                }
            )
        if record.configuration is None or record.configuration_version is None:
            return GetPrincipalResponse.model_validate(
                {
                    "result": {
                        "kind": "recognized",
                        "principal_id": record.principal_id,
                        "principal_kind": record.principal_kind,
                    },
                    "context": request.context,
                }
            )
        return self._current_response(record, context=request.context)

    async def sync_principal(
        self,
        identity: PrincipalIdentity,
        request: SyncPrincipalRequest | Mapping[str, Any],
    ) -> SyncPrincipalResponse:
        request = SyncPrincipalRequest.model_validate(request)
        if not request.configuration.model_fields_set:
            return _failed(
                "INVALID_REQUEST",
                "configuration must contain at least one section",
                context=request.context,
            )
        request_digest = canonical_json_sha256(request.model_dump(mode="json", exclude_none=True))

        while True:
            record = await self._store.get(identity.subject)
            if record is not None:
                replay = record.idempotency.get(request.idempotency_key)
                if replay:
                    digest, response = replay
                    if digest == request_digest:
                        return response.model_copy(update={"replayed": True}, deep=True)
                    return _failed(
                        "IDEMPOTENCY_CONFLICT",
                        "idempotency key was reused with a new request",
                        context=request.context,
                    )
            if request.expected_principal_kind is not None and (
                str(request.expected_principal_kind) != identity.kind
            ):
                return _failed(
                    "CONFLICT",
                    "expected_principal_kind is stale",
                    context=request.context,
                )
            if request.expected_configuration_version is not None and (
                record is None
                or request.expected_configuration_version != record.configuration_version
            ):
                return _failed(
                    "CONFLICT",
                    "expected_configuration_version is stale",
                    context=request.context,
                )

            previous = record.configuration if record else None
            try:
                next_state = await self._replace_sections(
                    identity,
                    previous,
                    request.configuration,
                    dry_run=bool(request.dry_run),
                )
            except _PrincipalValidationError as error:
                return _failed(error.code, str(error), context=request.context)
            except ValueError as error:
                return _failed("INVALID_REQUEST", str(error), context=request.context)
            changed = _json(previous) != _json(next_state)
            cleared = self._submitted_sections_are_empty(request.configuration)
            if request.dry_run:
                action = (
                    "would_clear"
                    if changed and cleared
                    else "would_update" if changed else "would_be_unchanged"
                )
                return SyncPrincipalResponse.model_validate(
                    {
                        "result": {"kind": "validated", "action": action, "dry_run": True},
                        "context": request.context,
                    }
                )

            if record is None:
                record = PrincipalRecord(identity.subject, f"prin:{uuid4()}", identity.kind)
                expected_store_revision: int | None = None
            else:
                expected_store_revision = record.store_revision
            record.configuration = next_state
            if changed or record.configuration_version is None:
                record.configuration_version = f"cfg:{uuid4()}"
            action = "cleared" if changed and cleared else "updated" if changed else "unchanged"
            response = SyncPrincipalResponse.model_validate(
                {
                    "result": {
                        "kind": "applied",
                        "action": action,
                        "dry_run": False,
                        "principal_id": record.principal_id,
                        "principal_kind": record.principal_kind,
                        "configuration_version": record.configuration_version,
                        "configuration": record.configuration,
                    },
                    "context": request.context,
                }
            )
            record.idempotency[request.idempotency_key] = (request_digest, response)
            record.store_revision = (expected_store_revision or 0) + 1
            if await self._store.compare_and_swap(
                identity.subject, expected_store_revision, record
            ):
                return response
            # A different process committed between our read and write.  Repeat
            # from the authoritative record so stale fences reject and an exact
            # same-key retry returns the winner's cached response.

    async def transition_destination(
        self,
        identity: PrincipalIdentity,
        destination_id: str,
        state: str,
        *,
        setup: Mapping[str, Any] | None = None,
        issues: list[Mapping[str, Any]] | None = None,
        reason: str = "destination_state_changed",
    ) -> AgentReportingDestinationState:
        """Record an adopter-verified provider transition without version churn."""
        while True:
            record = await self._store.get(identity.subject)
            if not record or not record.configuration:
                raise KeyError(destination_id)
            if record.principal_kind != identity.kind:
                raise ValueError("authenticated principal kind changed")
            destinations = list(record.configuration.reporting_destinations or [])
            index = next(
                (i for i, item in enumerate(destinations) if item.destination_id == destination_id),
                None,
            )
            if index is None:
                raise KeyError(destination_id)
            current = destinations[index]
            allowed = {
                "validating": {"ready", "action_required", "inactive", "rejected"},
                "action_required": {"validating", "ready", "inactive", "rejected"},
                "ready": {"action_required", "inactive", "rejected"},
                "inactive": {"validating"},
                "rejected": set(),
            }
            current_state = str(current.state)
            if state != current_state and state not in allowed[current_state]:
                raise ValueError(f"invalid destination transition {current_state!r} -> {state!r}")
            payload = current.model_dump(mode="json", exclude_none=True)
            payload.update(state=state)
            if setup is not None:
                payload["setup"] = dict(setup)
            elif state in {"ready", "inactive", "rejected"}:
                payload.pop("setup", None)
            if issues is not None:
                payload["issues"] = issues
            destinations[index] = AgentReportingDestinationState.model_validate(payload)
            state_payload = record.configuration.model_dump(mode="json", exclude_none=True)
            state_payload["reporting_destinations"] = destinations
            record.configuration = PrincipalState.model_validate(state_payload)
            expected_store_revision = record.store_revision
            record.store_revision += 1
            if await self._store.compare_and_swap(
                identity.subject, expected_store_revision, record
            ):
                break

        if self._emit_change:
            await self._emit_change(
                PrincipalChange(
                    record.principal_id,
                    identity.subject,
                    datetime.now(timezone.utc),
                    reason,
                    destination_id,
                )
            )
        return destinations[index]

    async def refresh_declarations(
        self, identity: PrincipalIdentity
    ) -> PrincipalDeclarationsState | None:
        """Recompute a persisted declaration intersection after seller changes.

        Like destination proof transitions, this seller-driven state change
        emits ``principal.changed`` but does not advance the caller-owned
        ``configuration_version``.
        """
        changed = False
        declarations: PrincipalDeclarationsState | None = None
        while True:
            record = await self._store.get(identity.subject)
            if not record or not record.configuration:
                return None
            current = record.configuration.declarations
            if current is None:
                return None
            declarations = self._negotiate_declarations(current.declared)
            changed = _json(current) != _json(declarations)
            if changed:
                payload = record.configuration.model_dump(mode="json", exclude_none=True)
                payload["declarations"] = declarations
                record.configuration = PrincipalState.model_validate(payload)
                expected_store_revision = record.store_revision
                record.store_revision += 1
                if not await self._store.compare_and_swap(
                    identity.subject, expected_store_revision, record
                ):
                    continue
            break
        if changed and self._emit_change:
            await self._emit_change(
                PrincipalChange(
                    record.principal_id,
                    identity.subject,
                    datetime.now(timezone.utc),
                    "declarations_intersection_changed",
                )
            )
        return declarations

    async def _replace_sections(
        self,
        identity: PrincipalIdentity,
        previous: PrincipalState | None,
        desired: PrincipalConfiguration,
        *,
        dry_run: bool,
    ) -> PrincipalState:
        payload = previous.model_dump(mode="json", exclude_none=True) if previous else {}
        fields = desired.model_fields_set
        if "notification_configs" in fields:
            configs = desired.notification_configs or []
            _require_unique(configs, "subscriber_id", "notification_configs")
            notification_states: list[AgentNotificationConfigState] = []
            for index, config in enumerate(configs):
                if config.active and self._notification_proof is None and not dry_run:
                    raise ValueError(
                        "active notification configs require a notification_proof hook"
                    )
                config = _normalize_notification_config(config, index)
                notification_state = (
                    await self._notification_proof(identity, config)
                    if self._notification_proof and not dry_run
                    else _notification_state(config)
                )
                if notification_state.subscriber_id != config.subscriber_id:
                    raise ValueError(
                        "notification_proof returned a state for a different subscriber"
                    )
                if _json(notification_state) != _json(_notification_state(config)):
                    raise ValueError(
                        "notification_proof returned a state for a different notification contract"
                    )
                notification_states.append(notification_state)
            payload["notification_configs"] = notification_states

        if "reporting_destinations" in fields:
            requested_destinations = [
                _normalize_destination(destination)
                for destination in desired.reporting_destinations or []
            ]
            _require_unique(requested_destinations, "destination_id", "reporting_destinations")
            prior = {
                item.destination_id: item
                for item in (previous.reporting_destinations if previous else None) or []
            }
            destination_states: list[AgentReportingDestinationState] = []
            for destination in requested_destinations:
                old = prior.pop(destination.destination_id, None)
                if old and self._same_destination_contract(old.configuration, destination):
                    if _json(old.configuration) == _json(destination):
                        destination_states.append(old)
                        continue
                    updated = old.model_dump(mode="json", exclude_none=True)
                    updated["configuration"] = destination
                    updated["state"] = "validating" if destination.active else "inactive"
                    destination_states.append(
                        AgentReportingDestinationState.model_validate(updated)
                    )
                    continue
                if self._destination_proof and not dry_run:
                    proved = await self._destination_proof(identity, destination, old)
                    if (
                        proved.destination_id != destination.destination_id
                        or _json(proved.configuration) != _json(destination)
                        or (not destination.active and str(proved.state) != "inactive")
                    ):
                        raise ValueError(
                            "destination_proof returned a state for a different "
                            "destination contract"
                        )
                    destination_states.append(proved)
                    continue
                refs = []
                if old:
                    refs = [old.destination_ref, *(old.prior_destination_refs or [])]
                destination_states.append(
                    AgentReportingDestinationState.model_validate(
                        {
                            "destination_id": destination.destination_id,
                            "destination_ref": f"dest:{uuid4()}",
                            "prior_destination_refs": refs or None,
                            "state": "validating" if destination.active else "inactive",
                            "configuration": destination,
                        }
                    )
                )
            retired: list[dict[str, Any]] = [
                item.model_dump(mode="json", exclude_none=True)
                for item in (previous.retired_destinations if previous else None) or []
            ]
            now = datetime.now(timezone.utc)
            for destination_id, old in prior.items():
                retired.append(
                    {
                        "destination_id": destination_id,
                        "destination_refs": [
                            old.destination_ref,
                            *(old.prior_destination_refs or []),
                        ],
                        "revoked_at": now,
                    }
                )
            payload["reporting_destinations"] = destination_states
            payload["retired_destinations"] = retired

        if "declarations" in fields:
            payload["declarations"] = self._negotiate_declarations(
                desired.declarations or AgentDeclarations()
            )
        state = PrincipalState.model_validate(payload)
        active_notifications = [
            config for config in state.notification_configs or [] if config.active is not False
        ]
        declarations = state.declarations
        accepted_algorithms = (
            declarations.accepted.webhook_signing_algorithms if declarations else None
        )
        if active_notifications and not accepted_algorithms:
            raise _PrincipalValidationError(
                "UNSUPPORTED_FEATURE",
                "active notification configs require an accepted webhook signing algorithm",
            )
        return state

    def _negotiate_declarations(self, declared: AgentDeclarations) -> PrincipalDeclarationsState:
        raw = declared.model_dump(mode="json", exclude_none=True)
        accepted: dict[str, list[str]] = {}
        exclusions: list[dict[str, str]] = []
        for axis, offered in raw.items():
            supported = set(self._accepted_declarations[axis])
            intersection = [value for value in offered if value in supported]
            # An omitted field represents an empty set: generated declaration
            # fields correctly reject a present empty array.
            if intersection:
                accepted[axis] = intersection
            exclusions.extend(
                {
                    "axis": axis,
                    "value": value,
                    "reason": "unsupported by this seller",
                }
                for value in offered
                if value not in supported
            )
        selected = next(iter(accepted.get("async_adcp_versions", [])), None)
        return PrincipalDeclarationsState.model_validate(
            {
                "declared": declared,
                "accepted": accepted,
                "selected_async_adcp_version": selected,
                "exclusions": exclusions or None,
            }
        )

    @staticmethod
    def _submitted_sections_are_empty(configuration: PrincipalConfiguration) -> bool:
        fields = configuration.model_fields_set
        if not fields:
            return False
        return (
            all(
                getattr(configuration, field) == []
                for field in fields
                if field in {"notification_configs", "reporting_destinations"}
            )
            and "declarations" not in fields
        )

    @staticmethod
    def _same_destination_contract(
        previous: AgentReportingDestination,
        desired: AgentReportingDestination,
    ) -> bool:
        old = previous.model_dump(mode="json", exclude_none=True)
        new = desired.model_dump(mode="json", exclude_none=True)
        old.pop("active", None)
        new.pop("active", None)
        return bool(old == new)

    @staticmethod
    def _current_response(
        record: PrincipalRecord, *, context: object | None = None
    ) -> GetPrincipalResponse:
        return GetPrincipalResponse.model_validate(
            {
                "result": {
                    "kind": "current",
                    "principal_id": record.principal_id,
                    "principal_kind": record.principal_kind,
                    "configuration_version": record.configuration_version,
                    "configuration": record.configuration,
                },
                "context": context,
            }
        )


__all__ = [
    "DestinationProofHook",
    "InMemoryPrincipalRecordStore",
    "NotificationProofHook",
    "PrincipalChange",
    "PrincipalChangeEmitter",
    "PrincipalIdentity",
    "PrincipalRecord",
    "PrincipalRecordStore",
    "PrincipalService",
]
