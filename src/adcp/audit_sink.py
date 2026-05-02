"""Audit-event observability seam paralleling :class:`~adcp.webhook_supervisor.DeliveryLogSink`.

:class:`~adcp.webhook_supervisor.DeliveryLogSink` records *delivery
attempts* (one per webhook send). This module records *skill dispatches*
(one per tool/A2A call) — the seller-facing observability trail that
activity feeds, internal dashboards, and SecOps alert channels read.

Two seams:

* :class:`AuditSink` — Protocol. Adopters implement this against their
  durable store of choice (``audit_log`` table, S3, Splunk HEC, etc.).

* :class:`LoggingAuditSink` and :class:`SlackAlertSink` — reference
  implementations sufficient for development, single-instance servers,
  or as templates for a custom adopter sink.

Wire one or more sinks into the :data:`~adcp.server.SkillMiddleware`
chain via :func:`make_audit_middleware`. Put audit middleware
**outermost** in the chain (see ``SkillMiddleware`` composition docs)
so rate-limited or feature-gated calls still appear in the trail.

Integrity contract — observability, NOT compliance-of-record
------------------------------------------------------------

Each sink call is bounded by ``sink_timeout_seconds`` and wrapped in a
log-and-swallow ``try``: a misbehaving sink (DB stall, Slack outage,
DNS failure) cannot wedge the dispatch hot path. This matches the
:class:`~adcp.webhook_supervisor.DeliveryLogSink` trade-off the design
explicitly parallels.

**This module is NOT sufficient for any of the following regimes** —
all of which require non-repudiable, write-ahead, durable records that
a best-effort, in-process, drop-on-failure sink fundamentally cannot
provide:

* **SOX** (financial controls on media buys) — requires tamper-evident
  records committed in the same transaction as the financial mutation.
* **GDPR Article 30** (records of processing activities) — requires
  durable records produce-able to a regulator on demand.
* **IAB TCF** (consent decisions for ad targeting) — requires consent
  decisions cryptographically tied to the consent string at storage.

Adopters who need any of the above implement :class:`AuditSink`
directly against a transactional store (write the audit row in the
**same DB transaction** as the mutation it audits) and skip
:func:`make_audit_middleware` for those code paths. The reference impls
in this module are for SecOps observability — the operator-facing "what
just happened" stream — not the system-of-record auditors will pull on.

Security notes
--------------

* :class:`SlackAlertSink` routes outbound HTTP through
  :func:`~adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport`
  — the same SSRF-hardened transport every other outbound call in this
  SDK uses. ``trust_env=False`` blocks ``HTTPS_PROXY`` exfiltration.

* :class:`SlackAlertSink` filters :attr:`AuditEvent.details` through an
  explicit ``allowed_fields`` allowlist — default ``frozenset()``,
  meaning **no** ``details`` content reaches Slack unless the adopter
  opts specific keys in. Prevents accidental egress of financial fields,
  PII, or buyer-supplied free text into a third-party chat surface.

* :class:`SlackAlertSink.__repr__` redacts the webhook URL (it's a
  bearer token); the URL never appears in tracebacks or
  ``logger.exception`` output.

* The middleware does NOT capture ``params`` or response bodies into
  :attr:`AuditEvent.details`. Adopters who need richer audit content
  build their own middleware that constructs :class:`AuditEvent` with
  whitelisted fields from ``params`` — this module deliberately keeps
  the default surface narrow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from adcp.server import SkillMiddleware, ToolContext

UTC = timezone.utc

logger = logging.getLogger(__name__)


# ----- Event record + sink Protocol -----


class AuditEvent(BaseModel):
    """One audit record per skill dispatch.

    Field naming aligns with :class:`~adcp.server.ToolContext` exactly
    (``caller_identity``, ``tenant_id``, ``request_id``) so the
    middleware constructing events copies fields without renaming. A
    distinct ``principal_id`` would force every adopter call site to
    translate.

    Frozen Pydantic model: every other event-shaped type in this SDK is
    Pydantic, so adopters get ``.model_dump(mode="json")`` for
    free-on-the-wire serialization, datetime validation at construction
    (no naive datetimes leaking into a sink), and consistency with the
    rest of the SDK's typed surface.

    :param operation: The skill name (``"create_media_buy"``,
        ``"acquire_rights"``) for middleware-emitted events. Adopters
        constructing events directly may use a coarser action label.
    :param success: Whether the operation completed without raising.
        Failure events carry ``error_type`` / ``error_message``.
    :param occurred_at: When the event was recorded. Timezone-aware UTC.
    :param caller_identity: From :attr:`ToolContext.caller_identity`.
    :param tenant_id: From :attr:`ToolContext.tenant_id`.
    :param request_id: From :attr:`ToolContext.request_id`.
    :param error_type: Exception class name on failure
        (``"IdempotencyConflictError"``). ``None`` on success.
    :param error_message: First 200 chars of ``str(exc)`` on failure.
        Truncated to bound log size; sinks that need full traces should
        capture them out-of-band (this field is meant for Slack-style
        summaries).
    :param details: Adopter-defined free-form fields. The middleware
        emits ``{}`` by default; adopters constructing events directly
        populate this with whatever their store needs. **Treat as
        potentially sensitive** — :class:`SlackAlertSink` filters
        through an explicit allowlist before egress.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str
    success: bool
    occurred_at: datetime
    caller_identity: str | None = None
    tenant_id: str | None = None
    request_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    details: Mapping[str, Any] = Field(default_factory=dict)


@runtime_checkable
class AuditSink(Protocol):
    """Persistence hook for :class:`AuditEvent` records.

    Called once per dispatch (success or failure) when wired via
    :func:`make_audit_middleware`. Implementations MUST be safe to call
    concurrently — the middleware composes with other middleware and
    sees concurrent skill calls under load.

    Failures inside ``record`` are bounded and swallowed by the
    middleware (see module docstring). Implementations should still
    avoid raising for transient conditions — log and return.
    """

    async def record(self, event: AuditEvent) -> None: ...


# ----- LoggingAuditSink -----


class LoggingAuditSink:
    """Writes :class:`AuditEvent` as structured JSON via stdlib :mod:`logging`.

    Sufficient for development and single-instance servers whose log
    aggregation pipeline already ingests ``logger.info`` output (Vector,
    Fluent Bit, journald → Loki, etc.). Adopters with a dedicated audit
    table implement :class:`AuditSink` directly.

    All :attr:`AuditEvent.details` keys are emitted verbatim — this sink
    runs in-process under the adopter's control and there's no third
    party to exfiltrate to. Contrast with :class:`SlackAuditSink` which
    requires explicit allowlisting.
    """

    def __init__(
        self,
        *,
        logger_: logging.Logger | None = None,
        level: int = logging.INFO,
    ) -> None:
        self._logger = logger_ or logging.getLogger("adcp.audit")
        self._level = level

    async def record(self, event: AuditEvent) -> None:
        self._logger.log(
            self._level,
            event.model_dump_json(),
        )


# ----- SlackAlertSink -----


class SlackAlertSink:
    """Posts :class:`AuditEvent` to a Slack incoming-webhook URL on a
    sensitive subset of events — operator alerting, NOT audit-of-record.

    Slack is universally used in ad-tech ops as an alerting channel for
    SecOps (failed-auth bursts, large media buys, contract-tier
    overages) — never as the system of record for compliance audit.
    This sink is named for that role: it implements the :class:`AuditSink`
    Protocol so it composes through :func:`make_audit_middleware`, but
    the intent is "tee a high-signal subset to a chat channel," not
    "satisfy the auditor."

    Pair with :class:`LoggingAuditSink` (or a custom sink writing to
    your audit table) for the durable trail; route only the sensitive
    subset through here via ``sensitive_operations``.

    :param webhook_url: Slack incoming-webhook URL. MUST be HTTPS. The
        URL is a bearer token; it appears redacted in :meth:`__repr__`.
    :param sensitive_operations: Optional whitelist of operations that
        trigger a Slack post. ``None`` means post every event (rarely
        what you want — Slack will rate-limit). Pass e.g.
        ``frozenset({"create_media_buy", "acquire_rights",
        "update_rights"})`` to post only mutations.
    :param allowed_fields: Allowlist of :attr:`AuditEvent.details` keys
        that may be included in the Slack message body. Default
        ``frozenset()`` — no ``details`` content is posted, only the
        operation/identity/error summary. Prevents accidental egress of
        financial fields (budgets, credit limits), PII (contact info),
        or buyer-supplied free text.
    :param timeout_seconds: Per-call HTTP timeout. The middleware also
        applies its own ``sink_timeout_seconds`` ceiling; the tighter of
        the two governs.
    :param allow_private_destinations: Pass-through to the IP-pinned
        transport. Default ``False`` (block private/loopback IPs).
        Override only for testing against a local stub.
    :param allowed_destination_ports: Pass-through to the IP-pinned
        transport. Default ``None`` (allow 443 only). Slack's webhook
        host serves on 443; overriding is rarely correct.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        sensitive_operations: frozenset[str] | None = None,
        allowed_fields: frozenset[str] = frozenset(),
        timeout_seconds: float = 5.0,
        allow_private_destinations: bool = False,
        allowed_destination_ports: frozenset[int] | None = None,
    ) -> None:
        if not webhook_url.startswith("https://"):
            raise ValueError(
                "SlackAlertSink: webhook_url must be HTTPS — Slack rejects "
                "plaintext webhooks and the URL is a bearer token."
            )
        self._webhook_url = webhook_url
        self._sensitive_operations = sensitive_operations
        self._allowed_fields = allowed_fields
        self._timeout = timeout_seconds
        self._allow_private = allow_private_destinations
        self._allowed_ports = allowed_destination_ports

    def __repr__(self) -> str:
        # Explicit repr so the webhook URL (a bearer token) never reaches
        # tracebacks, ``logger.exception`` output, or auto-rendered
        # ``__dict__`` dumps. Mirrors WebhookSender.__repr__.
        ops_repr = "all" if self._sensitive_operations is None else len(self._sensitive_operations)
        return f"SlackAlertSink(sensitive_operations={ops_repr})"

    async def record(self, event: AuditEvent) -> None:
        if (
            self._sensitive_operations is not None
            and event.operation not in self._sensitive_operations
        ):
            return

        # Lazy import: keep the audit_sink module importable in
        # environments that don't have httpx wired (e.g., type-only
        # consumers). The httpx + transport import only runs when an
        # adopter actually constructs a SlackAuditSink and emits.
        import httpx

        from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport

        transport = build_async_ip_pinned_transport(
            self._webhook_url,
            allow_private=self._allow_private,
            allowed_ports=self._allowed_ports,
        )

        payload = {"text": self._format(event)}
        async with httpx.AsyncClient(
            transport=transport,
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(self._webhook_url, json=payload)
            # Slack returns 200 with body ``"ok"`` on success. Non-2xx
            # raises so the middleware's swallow path logs a warning —
            # Slack rate-limit (429) and webhook-revoked (404) need to
            # surface in operator logs even if they can't fail dispatch.
            response.raise_for_status()

    def _format(self, event: AuditEvent) -> str:
        marker = "OK" if event.success else "FAIL"
        parts = [
            f"[{marker}] {event.operation}",
            f"caller={event.caller_identity or '?'}",
        ]
        if event.tenant_id:
            parts.append(f"tenant={event.tenant_id}")
        if event.request_id:
            parts.append(f"request_id={event.request_id}")
        if not event.success and event.error_type:
            parts.append(f"error={event.error_type}")
            if event.error_message:
                parts.append(f"msg={event.error_message}")
        if self._allowed_fields:
            filtered = {k: v for k, v in event.details.items() if k in self._allowed_fields}
            if filtered:
                parts.append(f"details={json.dumps(filtered, default=str, sort_keys=True)}")
        return " ".join(parts)


# ----- Middleware composition -----


def make_audit_middleware(
    sinks: Sequence[AuditSink],
    *,
    sink_timeout_seconds: float = 5.0,
) -> SkillMiddleware:
    """Compose one or more :class:`AuditSink` instances into a
    :data:`~adcp.server.SkillMiddleware`.

    The middleware records one :class:`AuditEvent` per dispatch:

    * On success: ``success=True``, no error fields.
    * On exception: ``success=False`` with ``error_type`` /
      ``error_message`` populated, then re-raises so the executor's
      normal error path runs unchanged.

    Each sink is bounded by ``sink_timeout_seconds`` and wrapped in
    log-and-swallow — a sink timeout or raise NEVER affects dispatch.

    Composition reminder (see :data:`~adcp.server.SkillMiddleware`
    docstring): put the audit middleware **outermost** in your
    middleware list. Middleware that short-circuits (rate limiter,
    feature-gate) never calls ``call_next()``; if audit sits inside the
    short-circuit, rejected calls disappear from the audit trail —
    often the most interesting events for security review.

    :param sinks: Sinks to dispatch every event to, in order. Empty
        sequence is allowed (audit middleware becomes a no-op
        passthrough — useful when the seller has audit toggled off but
        wants to keep the middleware list shape stable across configs).
    :param sink_timeout_seconds: Per-sink timeout ceiling. Default 5s
        matches :class:`~adcp.webhook_supervisor.RetryPolicy`.

    Example::

        from adcp.audit_sink import (
            LoggingAuditSink, SlackAlertSink, make_audit_middleware,
        )

        audit = make_audit_middleware([
            LoggingAuditSink(),
            SlackAlertSink(
                webhook_url=os.environ["SLACK_ALERT_WEBHOOK"],
                sensitive_operations=frozenset({
                    "create_media_buy", "acquire_rights", "update_rights",
                }),
            ),
        ])

        create_mcp_server(MyAgent(), middleware=[audit, rate_limit, metrics])
    """
    sinks_tuple = tuple(sinks)

    async def audit_middleware(
        skill_name: str,
        params: dict[str, Any],
        context: ToolContext,
        call_next: Callable[[], Awaitable[Any]],
    ) -> Any:
        try:
            result = await call_next()
        except Exception as exc:
            await _emit(
                sinks_tuple,
                AuditEvent(
                    operation=skill_name,
                    success=False,
                    occurred_at=datetime.now(UTC),
                    caller_identity=context.caller_identity,
                    tenant_id=context.tenant_id,
                    request_id=context.request_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:200],
                ),
                timeout_seconds=sink_timeout_seconds,
            )
            raise
        await _emit(
            sinks_tuple,
            AuditEvent(
                operation=skill_name,
                success=True,
                occurred_at=datetime.now(UTC),
                caller_identity=context.caller_identity,
                tenant_id=context.tenant_id,
                request_id=context.request_id,
            ),
            timeout_seconds=sink_timeout_seconds,
        )
        return result

    return audit_middleware


async def _emit(
    sinks: tuple[AuditSink, ...],
    event: AuditEvent,
    *,
    timeout_seconds: float,
) -> None:
    """Dispatch ``event`` to every sink with per-sink timeout + swallow.

    Sinks are invoked sequentially. A misbehaving sink only delays
    sibling sinks by ``timeout_seconds`` — never longer. Adopters with
    high-volume audit traffic and many sinks can build their own fan-out
    middleware that schedules sinks concurrently; this reference
    sequential path keeps ordering predictable and the failure mode
    obvious during incident review.
    """
    for sink in sinks:
        try:
            await asyncio.wait_for(sink.record(event), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "[adcp.audit_sink] %s.record timed out after %ss for %s — event dropped",
                type(sink).__name__,
                timeout_seconds,
                event.operation,
            )
        except Exception:
            logger.warning(
                "[adcp.audit_sink] %s.record raised for %s — event dropped",
                type(sink).__name__,
                event.operation,
                exc_info=True,
            )


__all__ = [
    "AuditEvent",
    "AuditSink",
    "LoggingAuditSink",
    "SlackAlertSink",
    "make_audit_middleware",
]
