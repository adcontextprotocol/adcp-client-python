"""One identity rule for authenticated activity reads and trusted registrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adcp.reporting.evidence import consumer_reference
from adcp.reporting.ledger.notification_models import ReportingNotificationError

if TYPE_CHECKING:
    from adcp.decisioning.context import AuthInfo
    from adcp.decisioning.registry import BuyerAgent


def canonical_consumer(principal: str | None) -> str:
    """Validate a principal restored from *trusted* subscription configuration.

    Registration must first call :func:`resolve_reporting_consumer`. This
    function restores that persisted result for asynchronous work; payloads,
    account references, and subscriber identifiers are never identity sources.
    """
    try:
        if not isinstance(principal, str) or principal.strip().lower() in {
            "",
            "anonymous",
            "anon",
            "unauthenticated",
            "none",
            "null",
        }:
            raise ValueError
        if principal != principal.strip() or not principal.isprintable() or len(principal) > 2048:
            raise ValueError
        consumer_reference(principal)
        return principal
    except (ValueError, TypeError):
        pass
    # Raise outside the parser exception scope: even __context__ is secret-free.
    raise ReportingNotificationError("activity_identity_required")


def resolve_reporting_consumer(
    *, auth_info: AuthInfo | None, agent: BuyerAgent | None = None
) -> str:
    """Resolve the authenticated consumer without choosing between disagreements.

    Inputs must come from verified middleware/the trusted registry. Every
    present flat/registry/signed identity must agree. API/OAuth credentials may
    resolve solely through the registry without a flat principal.
    Normalize aliases in the trusted authentication adapter before this boundary.
    Persist this result as ``ReportingNotificationSubscription.principal_id``.
    """
    from adcp.decisioning.registry import HttpSigCredential

    values = [agent.agent_url] if agent is not None else []
    if auth_info is not None:
        values.extend(
            value for value in (auth_info.principal, auth_info.agent_url) if value is not None
        )
        if isinstance(auth_info.credential, HttpSigCredential):
            values.append(auth_info.credential.agent_url)
    principals = {canonical_consumer(value) for value in values}
    if len(principals) > 1:
        raise ReportingNotificationError("activity_identity_conflict")
    if not principals:
        raise ReportingNotificationError("activity_identity_required")
    return principals.pop()
