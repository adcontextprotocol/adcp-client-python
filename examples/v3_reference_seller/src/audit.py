"""DB-backed :class:`adcp.audit_sink.AuditSink` for the v3 reference seller.

Every dispatched skill logs one row into ``audit_events`` — a
durable trail for compliance review, anomaly detection, and ops
queries ("who suspended buyer X yesterday?").

Adopters with Slack / PagerDuty alerting compose this sink with
:class:`adcp.audit_sink.SlackAlertSink` via a ``CompositeAuditSink``
so each sink fires on every event — Slack for the alert, this DB
sink for the durable record.

Failure semantics: ``record()`` is **best-effort** — it ``await`` s
the DB write inline, then swallows any exception so the audit step
never fails the request. The audit step is therefore in the latency
path of every skill call; adopters with high-volume audit traffic
either decouple via an ``asyncio.Queue`` worker or batch writes.
Adopters needing transactional audit (refusing the request if audit
fails) implement their own pre-dispatch middleware.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, BigInteger, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from adcp.audit_sink import AuditEvent, AuditSink

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import Base

logger = logging.getLogger(__name__)


class AuditEventRow(Base):
    """Audit-event row.

    Wide columns for the structured fields adopters query on
    (``tenant_id``, ``operation``, ``caller_identity``); free-form
    JSON for the adopter-defined ``details`` blob. Adopters with
    high audit volume partition by ``occurred_at`` (monthly).
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    caller_identity: Mapped[str | None] = mapped_column(String(512), nullable=True)

    success: Mapped[bool] = mapped_column(nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Adopter-defined free-form payload — buyer_agent_url, account_id,
    #: media_buy_id, decision flags, etc. Treat as potentially
    #: sensitive.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("audit_events_tenant_idx", "tenant_id", "occurred_at"),
        Index("audit_events_operation_idx", "operation", "occurred_at"),
        Index("audit_events_caller_idx", "caller_identity", "occurred_at"),
    )


class DbAuditSink:
    """Persistent :class:`AuditSink` writing one row per skill dispatch.

    Implements the framework's :class:`AuditSink` Protocol.
    Failures are swallowed by the framework's audit middleware
    (per the Protocol's documented contract).
    """

    def __init__(self, *, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def record(self, event: AuditEvent) -> None:
        try:
            row = AuditEventRow(
                occurred_at=event.occurred_at,
                tenant_id=event.tenant_id,
                request_id=event.request_id,
                operation=event.operation,
                caller_identity=event.caller_identity,
                success=event.success,
                error_type=event.error_type,
                error_message=event.error_message,
                details=dict(event.details) if event.details else None,
            )
            async with self._sessionmaker() as session, session.begin():
                session.add(row)
        except Exception:  # noqa: BLE001 — sink failures must not propagate
            logger.exception(
                "Audit-event write failed for operation=%s tenant=%s",
                event.operation,
                event.tenant_id,
            )


def make_sink(sessionmaker: async_sessionmaker) -> AuditSink:
    """Factory returning a Protocol-typed handle for the audit
    middleware."""
    return DbAuditSink(sessionmaker=sessionmaker)


__all__ = ["AuditEventRow", "DbAuditSink", "make_sink"]
