"""Sanitizers for public error/account authorization metadata."""

from __future__ import annotations

from typing import Any

ACCOUNT_AUTHORIZATION_PUBLIC_KEYS = frozenset(
    {"allowed_tasks", "field_scopes", "scope_name", "read_only"}
)
AUTHORIZATION_REQUIRED_DETAIL_KEYS = frozenset(
    {
        "required_connections",
        "missing_connections",
        "authorization_url",
        "authorization_instructions",
    }
)
DOWNSTREAM_CONNECTION_PUBLIC_KEYS = frozenset(
    {
        "provider",
        "connection_type",
        "required_for",
        "scope",
        "status",
        "connection_id",
        "resource_ref",
        "authorization_url",
        "authorization_instructions",
        "expires_at",
    }
)
RESOURCE_REF_PUBLIC_KEYS = frozenset(
    {
        "platform_account_id",
        "identity_id",
        "handle",
        "profile_url",
        "post_id",
        "post_url",
    }
)


def _dump_public(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def sanitize_account_authorization(value: Any) -> dict[str, Any] | None:
    """Return caller-visible account authorization metadata only."""
    dumped = _dump_public(value)
    if not isinstance(dumped, dict):
        return None
    out = {
        key: dumped[key]
        for key in ACCOUNT_AUTHORIZATION_PUBLIC_KEYS
        if key in dumped and dumped[key] is not None
    }
    return out or None


def _sanitize_resource_ref(value: Any) -> dict[str, Any] | None:
    dumped = _dump_public(value)
    if not isinstance(dumped, dict):
        return None
    out = {
        key: dumped[key]
        for key in RESOURCE_REF_PUBLIC_KEYS
        if key in dumped and dumped[key] is not None
    }
    return out or None


def _sanitize_connection(value: Any) -> dict[str, Any] | None:
    dumped = _dump_public(value)
    if not isinstance(dumped, dict):
        return None
    out: dict[str, Any] = {}
    for key in DOWNSTREAM_CONNECTION_PUBLIC_KEYS:
        if key not in dumped or dumped[key] is None:
            continue
        if key == "resource_ref":
            resource_ref = _sanitize_resource_ref(dumped[key])
            if resource_ref:
                out[key] = resource_ref
        else:
            out[key] = dumped[key]
    return out or None


def _sanitize_connection_list(value: Any) -> list[dict[str, Any]] | None:
    dumped = _dump_public(value)
    if not isinstance(dumped, list):
        return None
    out = []
    for item in dumped:
        sanitized = _sanitize_connection(item)
        if sanitized:
            out.append(sanitized)
    return out or None


def sanitize_authorization_required_details(details: Any) -> dict[str, Any] | None:
    """Return public AUTHORIZATION_REQUIRED remediation details only."""
    dumped = _dump_public(details)
    if not isinstance(dumped, dict):
        return None
    out: dict[str, Any] = {}
    for key in AUTHORIZATION_REQUIRED_DETAIL_KEYS:
        if key not in dumped or dumped[key] is None:
            continue
        if key in {"required_connections", "missing_connections"}:
            connections = _sanitize_connection_list(dumped[key])
            if connections:
                out[key] = connections
        else:
            out[key] = dumped[key]
    return out or None


def sanitize_error_details(code: str, details: Any) -> dict[str, Any] | None:
    """Sanitize code-specific error details before emitting them."""
    if code == "AUTHORIZATION_REQUIRED":
        return sanitize_authorization_required_details(details)
    dumped = _dump_public(details)
    return dumped if isinstance(dumped, dict) else None


__all__ = [
    "sanitize_account_authorization",
    "sanitize_authorization_required_details",
    "sanitize_error_details",
]
