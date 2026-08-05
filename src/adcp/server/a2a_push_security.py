"""Shared A2A push-notification destination and identity policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from a2a.utils.errors import InvalidParamsError

from adcp.signing._idna_canonicalize import canonicalize_host
from adcp.webhooks import WebhookDestinationPolicy, WebhookDestinationValidationError
from adcp.webhooks import validate_webhook_destination_url as validate_destination

PushDestinationMode = Literal["disabled", "public_https", "allowlist"]


@dataclass(frozen=True)
class PushDestinationSettings:
    """Resolved operator policy for accepting A2A push destinations."""

    enabled: bool
    allowed_hosts: frozenset[str] | None


def normalize_allowed_push_hosts(hosts: Iterable[str]) -> frozenset[str]:
    """Canonicalize an operator-managed set of stable destination hosts."""
    try:
        return frozenset(canonicalize_host(host.strip()) for host in hosts if host.strip())
    except (UnicodeError, ValueError) as exc:
        raise WebhookDestinationValidationError(
            "allowed push-notification hostname is invalid",
            reason="invalid_allowed_hostname",
            field="allowed_destination_hosts",
        ) from exc


def resolve_push_destination_settings(
    mode: str,
    allowed_hosts: Iterable[str] = (),
) -> PushDestinationSettings:
    """Resolve explicit disabled/public/allowlist operator configuration.

    ``public_https`` retains the shared DNS and reserved-range SSRF checks; it
    disables only the additional hostname allowlist. ``disabled`` is expressed
    by omitting the push-config store entirely so the agent card does not claim
    push support.
    """
    normalized_mode = mode.strip().lower()
    normalized_hosts = normalize_allowed_push_hosts(allowed_hosts)
    if normalized_mode == "disabled":
        if normalized_hosts:
            raise ValueError(
                "A2A_PUSH_ALLOWED_HOSTS is set while A2A_PUSH_MODE=disabled; "
                "choose allowlist or remove the hosts"
            )
        return PushDestinationSettings(enabled=False, allowed_hosts=None)
    if normalized_mode == "public_https":
        if normalized_hosts:
            raise ValueError(
                "allowed push hosts require A2A_PUSH_MODE=allowlist; "
                "public_https accepts any SSRF-safe public HTTPS destination"
            )
        return PushDestinationSettings(enabled=True, allowed_hosts=None)
    if normalized_mode == "allowlist":
        if not normalized_hosts:
            raise ValueError(
                "A2A_PUSH_MODE=allowlist requires at least one allowed destination host"
            )
        return PushDestinationSettings(enabled=True, allowed_hosts=normalized_hosts)
    raise ValueError("A2A_PUSH_MODE must be one of: disabled, public_https, allowlist")


def scope_from_server_context(context: Any | None) -> str | None:
    """Return a verified authenticated principal name, or ``None``."""
    user = getattr(context, "user", None) if context is not None else None
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    user_name = getattr(user, "user_name", None)
    return user_name if isinstance(user_name, str) and user_name else None


def validate_push_notification_url(
    url: str,
    *,
    allowed_hosts: frozenset[str] | None = None,
    allowed_ports: frozenset[int] | None = None,
) -> None:
    """Validate a callback through the SDK's shared SSRF classifier."""
    canonical_allowed_hosts = (
        normalize_allowed_push_hosts(allowed_hosts) if allowed_hosts is not None else None
    )
    validation = validate_destination(
        url,
        policy=WebhookDestinationPolicy.production(
            allowed_destination_ports=allowed_ports,
        ),
        field="push_notification_config.url",
    )
    if canonical_allowed_hosts is not None and validation.hostname not in canonical_allowed_hosts:
        raise WebhookDestinationValidationError(
            "push notification URL hostname is not in allowed_destination_hosts",
            reason="hostname_not_allowed",
            field="push_notification_config.url",
            url=url,
            effective_url=validation.effective_url,
            policy=validation.policy,
        )


def validate_a2a_push_notification_url(
    url: str,
    *,
    allowed_hosts: frozenset[str] | None = None,
    allowed_ports: frozenset[int] | None = None,
) -> None:
    """Validate a callback and map policy failures to the A2A wire error."""
    try:
        validate_push_notification_url(
            url,
            allowed_hosts=allowed_hosts,
            allowed_ports=allowed_ports,
        )
    except WebhookDestinationValidationError as exc:
        # Do not reflect the rejected URL, its resolved address, or policy
        # internals onto the public JSON-RPC surface. In particular, the
        # shared SSRF classifier's diagnostic can contain a private IP.
        data = {"code": exc.code, "reason": exc.reason}
        if exc.field is not None:
            data["field"] = exc.field
        raise InvalidParamsError(
            message="push notification destination failed validation",
            data=data,
        ) from None


__all__ = [
    "PushDestinationMode",
    "PushDestinationSettings",
    "normalize_allowed_push_hosts",
    "resolve_push_destination_settings",
    "scope_from_server_context",
    "validate_a2a_push_notification_url",
    "validate_push_notification_url",
]
