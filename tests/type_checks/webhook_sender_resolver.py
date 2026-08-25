"""Adopter-facing type checks for tenant-scoped webhook sender resolution."""

from adcp import (
    ScopePermanentlyUnknown,
    ScopeTransientlyUnavailable,
    WebhookSender,
    WebhookSenderResolution,
    WebhookSenderResolver,
)
from adcp.decisioning import RequestContext, WebhookSigningScopeResolver


class TenantWebhookSenders:
    async def resolve(self, signing_scope_id: str) -> WebhookSenderResolution:
        if signing_scope_id == "decommissioned":
            raise ScopePermanentlyUnknown
        raise ScopeTransientlyUnavailable


resolver: WebhookSenderResolver = TenantWebhookSenders()


def resolve_signing_scope(context: RequestContext[object]) -> str:
    return str(context.tenant_id)


scope_resolver: WebhookSigningScopeResolver = resolve_signing_scope


def bind_sender(sender: WebhookSender) -> WebhookSenderResolution:
    return WebhookSenderResolution(
        sender=sender,
        advertised_algorithms=frozenset({"ed25519"}),
    )
