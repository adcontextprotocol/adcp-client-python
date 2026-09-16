"""Bounded, principal-scoped projection of reporting HTTP reservations.

No URL, body, header, response text, exception prose, or queue state is used as
an activity diagnostic. Pending means that peer I/O cannot yet be determined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import AnyUrl

from adcp.reporting.evidence import aware_utc
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.identity import resolve_reporting_consumer
from adcp.reporting.outbox.models import DeliveryBinding, DeliveryLease

if TYPE_CHECKING:
    from adcp.decisioning.accounts import ResolveContext

ActivityStatus: TypeAlias = Literal["pending", "success", "failed", "timeout", "connection_error"]
TerminalActivityStatus: TypeAlias = Literal["success", "failed", "timeout", "connection_error"]

# An allowlist intentionally also hides unfamiliar, non-secret route names.
# Entropy-only heuristics miss human-chosen passwords and encoded credentials.
_PUBLIC_PATH_SEGMENTS = frozenset(
    {
        "",
        "api",
        "adcp",
        "webhook",
        "webhooks",
        "reporting",
        "reports",
        "report",
        "callback",
        "callbacks",
        "notify",
        "notifications",
        "events",
        "delivery",
        "redacted",
        "hook",
        "hooks",
        "ingest",
        "endpoint",
    }
)


def sanitize_activity_url(value: str) -> str:
    """Keep the origin and public route words; redact every other path segment.

    This deterministic policy covers UUIDs, JWTs, long hex/base64, mixed tokens,
    percent encoding, and short human-chosen secrets without exposing hashes.
    Userinfo, query, and fragment are always removed. Failures contain no input.
    """
    try:
        if type(value) is not str or len(value) > 8192:
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        segments = []
        for part in parsed.path.split("/"):
            decoded = unquote(part)
            segments.append(
                decoded
                if decoded in _PUBLIC_PATH_SEGMENTS or re.fullmatch(r"v[0-9]{1,3}", decoded)
                else "redacted"
            )
        return str(AnyUrl(urlunsplit((parsed.scheme, host, "/".join(segments), "", ""))))
    except (ValueError, TypeError):
        pass
    # Raise outside the parser's except block so even __context__ cannot retain
    # a raw port/URL from urllib or a validation exception.
    raise ReportingNotificationError("invalid_activity_url")


def activity_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 200:
        raise ReportingNotificationError("activity_limit_must_be_1_to_200")
    return value


def retention_cutoff(now: datetime, retention_days: int) -> datetime:
    if type(retention_days) is not int or retention_days < 30:
        raise ReportingNotificationError("activity_retention_requires_30_days")
    return aware_utc(now) - timedelta(days=retention_days)


@dataclass(frozen=True)
class ActivityRequest:
    """Only safe, immutable HTTP request diagnostics cross the SQL boundary."""

    url: str
    payload_size_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", sanitize_activity_url(self.url))
        if type(self.payload_size_bytes) is not int or self.payload_size_bytes < 0:
            raise ReportingNotificationError("invalid_activity_request")


@dataclass(frozen=True)
class ActivityOutcome:
    status: TerminalActivityStatus
    http_status_code: int | None = None
    response_time_ms: int | None = None

    def __post_init__(self) -> None:
        valid = self.status in {"success", "failed", "timeout", "connection_error"}
        if self.status in {"success", "failed"}:
            valid = valid and (
                type(self.http_status_code) is int
                and 100 <= self.http_status_code <= 599
                and (self.status == "success") == (200 <= self.http_status_code < 300)
                and type(self.response_time_ms) is int
                and self.response_time_ms >= 0
            )
        else:
            valid = valid and self.http_status_code is None and self.response_time_ms is None
        if not valid:
            raise ReportingNotificationError("invalid_activity_outcome")


@dataclass(frozen=True)
class WebhookAttempt:
    binding: DeliveryBinding
    attempt: int
    lease_token: str = field(repr=False)
    reservation_token: str = field(repr=False)
    fired_at: datetime
    request: ActivityRequest
    outcome: ActivityOutcome | None = None
    completed_at: datetime | None = None

    def to_wire(self) -> dict[str, Any]:
        from adcp.validation.schema_loader import get_named_validator

        outcome = self.outcome
        status = outcome.status if outcome else "pending"
        row: dict[str, Any] = {
            "idempotency_key": self.binding.idempotency_key,
            "subscriber_id": self.binding.subscriber_id,
            "notification_type": self.binding.notification_type,
            "attempt": self.attempt,
            "fired_at": self.fired_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": status,
            "url": sanitize_activity_url(self.request.url),
            "http_status_code": outcome.http_status_code if outcome else None,
            "response_time_ms": outcome.response_time_ms if outcome else None,
            "payload_size_bytes": self.request.payload_size_bytes,
            "error_message": {
                "pending": None,
                "success": None,
                "failed": "HTTP non-success response",
                "timeout": "HTTP attempt timed out",
                "connection_error": "Connection failed",
            }[status],
        }
        # Reporting notifications normally have an ID; optional wire fields
        # must be absent, rather than null, when the source has none.
        if self.binding.notification_id:
            row["notification_id"] = self.binding.notification_id
        validator = get_named_validator("core/webhook-activity-record.json", version="3.2.0-rc.3")
        if validator is None or not validator.is_valid(row):
            raise ReportingNotificationError("invalid_activity_record")
        return row


class ReportingActivityReader(Protocol):
    """Optional read/purge projection, including the closed B+C activity union."""

    async def list_activity(
        self, *, account_id: str, consumer_id: str, limit: int = 50
    ) -> tuple[WebhookAttempt, ...]: ...

    async def purge_activity(
        self, *, account_id: str, consumer_id: str, now: datetime, retention_days: int = 30
    ) -> int: ...


class ReportingActivityStore(ReportingActivityReader, Protocol):
    async def reserve_attempt(
        self, lease: DeliveryLease, *, request: ActivityRequest, now: datetime
    ) -> WebhookAttempt | None: ...

    async def complete_attempt(
        self, attempt: WebhookAttempt, *, outcome: ActivityOutcome, now: datetime
    ) -> bool: ...

    async def list_activity(
        self, *, account_id: str, consumer_id: str, limit: int = 50
    ) -> tuple[WebhookAttempt, ...]: ...

    async def purge_activity(
        self, *, account_id: str, consumer_id: str, now: datetime, retention_days: int = 30
    ) -> int: ...


def omit_account_activity(envelope: Any) -> Any:
    """Unrequested/unsupported activity can never leak from adopter envelopes."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("accounts"), list):
        return envelope
    return {
        **envelope,
        "accounts": [
            (
                {key: value for key, value in account.items() if key != "webhook_activity"}
                if isinstance(account, dict)
                else account
            )
            for account in envelope["accounts"]
        ],
    }


class ReportingActivityProjector:
    """Optional decorator mounted *after* AccountStore's visibility/filtering.

    Pass this instance as ``account_activity=`` to the platform server factory.
    It never resolves additional accounts or accepts a consumer from the body.
    Memory stores support conformance but cannot justify durable capabilities.
    """

    def __init__(self, store: ReportingActivityReader) -> None:
        self.store = store

    async def for_account(
        self, *, account_id: str, context: ResolveContext, limit: int = 50
    ) -> list[dict[str, Any]]:
        consumer = resolve_reporting_consumer(auth_info=context.auth_info, agent=context.agent)
        attempts = await self.store.list_activity(
            account_id=account_id, consumer_id=consumer, limit=activity_limit(limit)
        )
        return [attempt.to_wire() for attempt in attempts]

    async def enrich(
        self, envelope: Any, *, context: ResolveContext, include: bool, limit: int = 50
    ) -> Any:
        # Preserve legacy envelopes, pagination, and account ordering. Only
        # already-returned accounts can ever reach the activity read boundary.
        if not isinstance(envelope, dict) or not isinstance(envelope.get("accounts"), list):
            return envelope
        if include:
            activity_limit(limit)
            resolve_reporting_consumer(auth_info=context.auth_info, agent=context.agent)
        accounts = []
        for original in envelope["accounts"]:
            if not isinstance(original, dict):
                accounts.append(original)
                continue
            account = dict(original)
            account.pop("webhook_activity", None)
            if include and isinstance(account.get("account_id"), str):
                account["webhook_activity"] = await self.for_account(
                    account_id=account["account_id"], context=context, limit=limit
                )
            accounts.append(account)
        return {**envelope, "accounts": accounts}
