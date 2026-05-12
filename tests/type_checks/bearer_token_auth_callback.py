"""Adopter pattern: BearerTokenAuth with sync and async validate_token callbacks.

Verifies that both SyncTokenValidator and AsyncTokenValidator implementations
are accepted by mypy --strict without type: ignore.
"""
from __future__ import annotations

from adcp.server.auth import BearerTokenAuth, Principal


def sync_validator(token: str) -> Principal | None:
    if token == "secret":
        return Principal(caller_identity="agent.example.com", tenant_id="acme")
    return None


async def async_validator(token: str) -> Principal | None:
    if token == "secret":
        return Principal(caller_identity="agent.example.com", tenant_id="acme")
    return None


sync_auth = BearerTokenAuth(validate_token=sync_validator)
async_auth = BearerTokenAuth(validate_token=async_validator)
