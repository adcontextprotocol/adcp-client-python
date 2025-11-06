from __future__ import annotations

"""Exception hierarchy for AdCP client."""


class ADCPError(Exception):
    """Base exception for all AdCP client errors."""


class ADCPConnectionError(ADCPError):
    """Connection to agent failed."""


class ADCPAuthenticationError(ADCPError):
    """Authentication failed (401, 403)."""


class ADCPTimeoutError(ADCPError):
    """Request timed out."""


class ADCPProtocolError(ADCPError):
    """Protocol-level error (malformed response, unexpected format)."""


class ADCPToolNotFoundError(ADCPError):
    """Requested tool not found on agent."""


class ADCPWebhookError(ADCPError):
    """Webhook handling error."""


class ADCPWebhookSignatureError(ADCPWebhookError):
    """Webhook signature verification failed."""
