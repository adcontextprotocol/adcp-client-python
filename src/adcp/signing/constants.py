"""Shared constants for the AdCP request-signing profile."""

from __future__ import annotations

DEFAULT_TAG = "adcp/request-signing/v1"
WEBHOOK_TAG = "adcp/webhook-signing/v1"
ADCP_USE_REQUEST = "request-signing"
#: Deprecated upstream (adcp#5555): new webhook signers publish ``request-signing``.
ADCP_USE_WEBHOOK = "webhook-signing"
#: Designated-task response JWS (``core/response-payload-jws-envelope.json``).
ADCP_USE_RESPONSE = "response-signing"
#: Governance attestation JWS (``check_governance`` ``governance_context``).
ADCP_USE_GOVERNANCE = "governance-signing"
#: Current request keys and deprecated webhook keys are both valid for webhooks.
WEBHOOK_ACCEPTED_ADCP_USES: frozenset[str] = frozenset({ADCP_USE_REQUEST, ADCP_USE_WEBHOOK})
MAX_WINDOW_SECONDS = 300
DEFAULT_EXPIRES_IN_SECONDS = 300
DEFAULT_SKEW_SECONDS = 60
NONCE_BYTES = 16
SIG_LABEL_DEFAULT = "sig1"

__all__ = [
    "ADCP_USE_GOVERNANCE",
    "ADCP_USE_REQUEST",
    "ADCP_USE_RESPONSE",
    "ADCP_USE_WEBHOOK",
    "DEFAULT_EXPIRES_IN_SECONDS",
    "DEFAULT_SKEW_SECONDS",
    "DEFAULT_TAG",
    "MAX_WINDOW_SECONDS",
    "NONCE_BYTES",
    "SIG_LABEL_DEFAULT",
    "WEBHOOK_ACCEPTED_ADCP_USES",
    "WEBHOOK_TAG",
]
