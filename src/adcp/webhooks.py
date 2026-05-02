"""Webhook creation, signing, and reception for AdCP agents.

Single front door for both senders and receivers. Underlying modules in
``adcp.signing.webhook_*`` and ``adcp.webhook_receiver`` are implementation
details kept for internal organization — prefer the re-exports here for
stability.

**Which sender helper to use**

* :func:`deliver` — one-shot dispatch for legacy ``authentication`` (Bearer
  or HMAC-SHA256). Collapses the sender's 6-step boilerplate into one call
  and signs the exact bytes it POSTs. Deprecated with AdCP 4.0; emits a
  :class:`DeprecationWarning`.
* :class:`WebhookSender` — the AdCP 4.0 default. RFC 9421 signing, shared
  connection pool, byte-identical replay via :meth:`WebhookSender.resend`.
  Use this for any new integration.
* :func:`create_mcp_webhook_payload` / :func:`create_a2a_webhook_payload`
  plus :func:`get_adcp_signed_headers_for_webhook` — low-level path for
  callers who need full control over serialization, headers, or retry logic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
import warnings
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from a2a import types as pb
from a2a.types import (
    Task,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value

from adcp.server.idempotency.backends import MemoryBackend as MemoryBackend
from adcp.server.idempotency.webhook_dedup import WebhookDedupStore as WebhookDedupStore
from adcp.signing.webhook_hmac import (
    LegacyWebhookHmacError,
    LegacyWebhookHmacOptions,
    VerifiedLegacyWebhookSender,
    verify_webhook_hmac,
)
from adcp.signing.webhook_signer import sign_webhook
from adcp.signing.webhook_verifier import (
    VerifiedWebhookSender,
    WebhookVerifyOptions,
    verify_webhook_signature,
)
from adcp.types import GeneratedTaskStatus
from adcp.types.base import AdCPBaseModel
from adcp.types.generated_poc.core.async_response_data import AdcpAsyncResponseData
from adcp.webhook_receiver import (
    LegacyHmacFallback,
    VerifiedSignerLike,
    WebhookKind,
    WebhookOutcome,
    WebhookPayload,
    WebhookReceiver,
    WebhookReceiverConfig,
)


def generate_webhook_idempotency_key() -> str:
    """Generate a cryptographically random idempotency_key for a webhook event.

    Returns a UUID v4 prefixed with ``whk_`` — matches the example format in
    ``webhooks.mdx`` and stays within the spec's length + charset bounds
    (``^[A-Za-z0-9_.:-]{16,255}$``).

    Publishers MUST generate this once per distinct event and reuse the same
    value when retrying delivery. Do NOT call this function again on retry —
    it would mint a fresh UUID and defeat the dedup contract.
    """
    return f"whk_{uuid.uuid4()}"


def create_mcp_webhook_payload(
    task_id: str,
    status: GeneratedTaskStatus | str,
    result: AdcpAsyncResponseData | dict[str, Any] | None = None,
    timestamp: datetime | None = None,
    task_type: str | None = None,
    operation_id: str | None = None,
    message: str | None = None,
    context_id: str | None = None,
    domain: str | None = None,
    idempotency_key: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """
    Create MCP webhook payload dictionary.

    This function helps agent implementations construct properly formatted
    webhook payloads for sending to clients.

    Args:
        task_id: Unique identifier for the task
        status: Current task status
        task_type: Optionally type of AdCP operation (e.g., "get_products", "create_media_buy")
        timestamp: When the webhook was generated (defaults to current UTC time)
        result: Task-specific payload (AdCP response data)
        operation_id: Client-generated identifier the buyer embedded in the
            webhook URL when registering push-notification config. Publishers
            MUST echo this back in the payload so buyers correlate notifications
            without parsing URL paths (per ``mcp-webhook-payload.json``).
            Senders extracting the value from the URL path on emission populate
            this field; callers constructing payloads directly pass it through.
        message: Human-readable summary of task state
        context_id: Session/conversation identifier
        domain: AdCP domain this task belongs to
        idempotency_key: Sender-generated key stable across retries of the same
            event. Defaults to a freshly-generated UUID v4 — callers retrying
            delivery of the same event MUST pass the key from their first
            attempt; passing None twice mints two keys and defeats dedup.

    Returns:
        Dictionary matching McpWebhookPayload schema, ready to be sent as JSON

    Examples:
        Create a completed webhook with results:
        >>> from adcp.webhooks import create_mcp_webhook_payload
        >>> from adcp.types import GeneratedTaskStatus
        >>>
        >>> payload = create_mcp_webhook_payload(
        ...     task_id="task_123",
        ...     task_type="get_products",
        ...     status=GeneratedTaskStatus.completed,
        ...     result={"products": [...]},
        ...     message="Found 5 products"
        ... )

        Create a failed webhook with error:
        >>> payload = create_mcp_webhook_payload(
        ...     task_id="task_456",
        ...     task_type="create_media_buy",
        ...     status=GeneratedTaskStatus.failed,
        ...     result={"errors": [{"code": "INVALID_INPUT", "message": "..."}]},
        ...     message="Validation failed"
        ... )

        Create a working status update:
        >>> payload = create_mcp_webhook_payload(
        ...     task_id="task_789",
        ...     task_type="sync_creatives",
        ...     status=GeneratedTaskStatus.working,
        ...     message="Processing 3 of 10 creatives"
        ... )
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    if idempotency_key is None:
        idempotency_key = generate_webhook_idempotency_key()

    # Convert status enum to string value
    status_value = status.value if hasattr(status, "value") else str(status)

    # Build payload matching McpWebhookPayload schema
    payload: dict[str, Any] = {
        "idempotency_key": idempotency_key,
        "task_id": task_id,
        "task_type": task_type,
        "status": status_value,
        "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp,
    }

    # Add optional fields only if provided
    if result is not None:
        # Convert Pydantic model to dict if needed for JSON serialization
        if hasattr(result, "model_dump"):
            payload["result"] = result.model_dump(mode="json")
        else:
            payload["result"] = result

    if operation_id is not None:
        payload["operation_id"] = operation_id

    if message is not None:
        payload["message"] = message

    if context_id is not None:
        payload["context_id"] = context_id

    if domain is not None:
        payload["domain"] = domain

    if token is not None:
        # Buyer-supplied token from push_notification_config.token,
        # echoed back per push-notification-config.json spec text:
        # "Echoed back in webhook payload to validate request authenticity."
        # Cross-language wire-parity with the JS implementation
        # (``buildTaskWebhookPayload`` in ``from-platform.ts``) — buyers
        # validating against the spec read body.token, not headers.
        payload["token"] = token

    return payload


def get_adcp_signed_headers_for_webhook(
    headers: dict[str, Any],
    secret: str,
    timestamp: str | int | None,
    payload: dict[str, Any] | AdCPBaseModel,
) -> dict[str, Any]:
    """
    Generate AdCP-compliant signed headers for webhook delivery.

    This function creates a cryptographic signature that proves the webhook
    came from an authorized agent and protects against replay attacks by
    including a timestamp in the signed message.

    The function adds two headers to the provided headers dict:
    - X-AdCP-Signature: HMAC-SHA256 signature in format "sha256=<hex_digest>"
    - X-AdCP-Timestamp: Unix timestamp in seconds

    The signing algorithm:
    1. Constructs message as "{timestamp}.{json_payload}"
    2. JSON-serializes payload with default separators (matches wire format from json= kwarg)
    3. UTF-8 encodes the message
    4. HMAC-SHA256 signs with the shared secret
    5. Hex-encodes and prefixes with "sha256="

    Args:
        headers: Existing headers dictionary to add signature headers to
        secret: Shared secret key for HMAC signing
        timestamp: Unix timestamp in seconds (str or int). If None, uses current time.
        payload: Webhook payload (dict or Pydantic model - will be JSON-serialized)

    Returns:
        The modified headers dictionary with signature headers added

    Examples:
        Sign and send an MCP webhook:
        >>> import time
        >>> from adcp.webhooks import create_mcp_webhook_payload
        >>> from adcp.webhooks import get_adcp_signed_headers_for_webhook
        >>>
        >>> payload = create_mcp_webhook_payload(
        ...     task_id="task_123",
        ...     task_type="get_products",
        ...     status="completed",
        ...     result={"products": [...]}
        ... )
        >>> headers = {"Content-Type": "application/json"}
        >>> signed_headers = get_adcp_signed_headers_for_webhook(
        ...     headers, secret="my-webhook-secret", timestamp=str(int(time.time())),
        ...     payload=payload,
        ... )
        >>>
        >>> # Send webhook with signed headers
        >>> import httpx
        >>> response = await httpx.post(
        ...     webhook_url,
        ...     json=payload,
        ...     headers=signed_headers
        ... )

        Headers will contain:
        >>> print(signed_headers)
        {
            "Content-Type": "application/json",
            "X-AdCP-Signature": "sha256=a1b2c3...",
            "X-AdCP-Timestamp": "1773185740"
        }
    """
    signature_headers, _body_bytes = _compute_legacy_signature(
        secret=secret, timestamp=timestamp, payload=payload
    )
    headers.update(signature_headers)
    return headers


def _compute_legacy_signature(
    *,
    secret: str,
    timestamp: str | int | None,
    payload: dict[str, Any] | AdCPBaseModel,
) -> tuple[dict[str, str], bytes]:
    """Shared HMAC-SHA256 signing core for the legacy webhook surface.

    Returns ``(signature_headers, body_bytes)`` where ``body_bytes`` is the
    compact-separator JSON the HMAC was computed over. Callers that POST
    must transmit exactly these bytes via ``content=body_bytes`` — that's
    the whole point of exposing the bytes alongside the headers.
    """
    if timestamp is None:
        import time

        timestamp = str(int(time.time()))
    else:
        timestamp = str(timestamp)

    if hasattr(payload, "model_dump"):
        payload_dict = payload.model_dump(mode="json")
    else:
        payload_dict = payload

    # Compact separators per adcontextprotocol/adcp#2478 canonical form.
    payload_json = json.dumps(payload_dict, separators=(",", ":"))
    body_bytes = payload_json.encode("utf-8")

    signed_message = f"{timestamp}.{payload_json}"
    signature_hex = hmac.new(
        secret.encode("utf-8"), signed_message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return (
        {
            "X-AdCP-Signature": f"sha256={signature_hex}",
            "X-AdCP-Timestamp": timestamp,
        },
        body_bytes,
    )


def sign_legacy_webhook(
    secret: str,
    payload: dict[str, Any] | AdCPBaseModel,
    *,
    timestamp: str | int | None = None,
    headers: dict[str, Any] | None = None,
) -> tuple[dict[str, str], bytes]:
    """Return ``(signed_headers, body_bytes)`` for a legacy HMAC webhook.

    Byte-equality between signature input and HTTP body is guaranteed —
    callers POST ``content=body_bytes`` instead of ``json=payload``, so the
    separator-drift trap that caused silent 401s in every spaced-vs-compact
    interop is structurally impossible here.

    This is a lower-level companion to :func:`deliver` for callers who need
    to own the HTTP transport themselves (custom auth, pre-configured
    ``httpx.AsyncClient``, non-httpx clients). For the one-shot "send a
    webhook" path, prefer :func:`deliver`.

    The returned ``body_bytes`` use compact separators (``","``/``":"``)
    matching the canonical on-wire form pinned by adcontextprotocol/adcp#2478.

    Example:
        >>> signed, body = sign_legacy_webhook("shared-secret", payload)
        >>> headers = {**signed, "Content-Type": "application/json"}
        >>> await client.post(url, content=body, headers=headers)
    """
    signature_headers, body_bytes = _compute_legacy_signature(
        secret=secret, timestamp=timestamp, payload=payload
    )
    if headers is not None:
        merged = {str(k): str(v) for k, v in headers.items()}
        merged.update(signature_headers)
        return merged, body_bytes
    return signature_headers, body_bytes


def extract_webhook_result_data(webhook_payload: dict[str, Any]) -> AdcpAsyncResponseData | None:
    """
    Extract result data from webhook payload (MCP or A2A format).

    This utility function handles webhook payloads from both MCP and A2A protocols,
    extracting the result data regardless of the webhook format. Useful for quick
    inspection, logging, or custom webhook routing logic without requiring full
    client initialization.

    Protocol Detection:
    - A2A Task: Has "artifacts" field (terminated statuses: completed, failed)
    - A2A TaskStatusUpdateEvent: Has nested "status.message" structure (intermediate statuses)
    - MCP: Has "result" field directly

    Args:
        webhook_payload: Raw webhook dictionary from HTTP request (JSON-deserialized)

    Returns:
        AdcpAsyncResponseData union type containing the extracted AdCP response, or None
        if no result present. For A2A webhooks, unwraps data from artifacts/message parts
        structure. For MCP webhooks, returns the result field directly.

    Examples:
        Extract from MCP webhook:
        >>> mcp_payload = {
        ...     "task_id": "task_123",
        ...     "task_type": "create_media_buy",
        ...     "status": "completed",
        ...     "timestamp": "2025-01-15T10:00:00Z",
        ...     "result": {"media_buy_id": "mb_123", "buyer_ref": "ref_123", "packages": []}
        ... }
        >>> result = extract_webhook_result_data(mcp_payload)
        >>> print(result["media_buy_id"])
        mb_123

        Extract from A2A Task webhook:
        >>> a2a_task_payload = {
        ...     "id": "task_456",
        ...     "context_id": "ctx_456",
        ...     "status": {"state": "completed", "timestamp": "2025-01-15T10:00:00Z"},
        ...     "artifacts": [
        ...         {
        ...             "artifact_id": "artifact_456",
        ...             "parts": [
        ...                 {
        ...                     "data": {
        ...                         "media_buy_id": "mb_456",
        ...                         "buyer_ref": "ref_456",
        ...                         "packages": []
        ...                     }
        ...                 }
        ...             ]
        ...         }
        ...     ]
        ... }
        >>> result = extract_webhook_result_data(a2a_task_payload)
        >>> print(result["media_buy_id"])
        mb_456

        Extract from A2A TaskStatusUpdateEvent webhook:
        >>> a2a_event_payload = {
        ...     "task_id": "task_789",
        ...     "context_id": "ctx_789",
        ...     "status": {
        ...         "state": "working",
        ...         "timestamp": "2025-01-15T10:00:00Z",
        ...         "message": {
        ...             "message_id": "msg_789",
        ...             "role": "agent",
        ...             "parts": [
        ...                 {"data": {"current_step": "processing", "percentage": 50}}
        ...             ]
        ...         }
        ...     },
        ...     "final": False
        ... }
        >>> result = extract_webhook_result_data(a2a_event_payload)
        >>> print(result["percentage"])
        50

        Handle webhook with no result:
        >>> empty_payload = {"task_id": "task_000", "status": "working", "timestamp": "..."}
        >>> result = extract_webhook_result_data(empty_payload)
        >>> print(result)
        None
    """
    # Detect A2A Task format (has "artifacts" field)
    if "artifacts" in webhook_payload:
        # Extract from task.artifacts[].parts[]
        artifacts = webhook_payload.get("artifacts", [])
        if not artifacts:
            return None

        # Use last artifact (most recent)
        target_artifact = artifacts[-1]
        parts = target_artifact.get("parts", [])
        if not parts:
            return None

        # Find DataPart (skip TextPart)
        for part in parts:
            # Check if this part has "data" field (DataPart)
            if "data" in part:
                data = part["data"]
                # Unwrap {"response": {...}} wrapper if present (A2A convention)
                if isinstance(data, dict) and "response" in data and len(data) == 1:
                    return cast(AdcpAsyncResponseData, data["response"])
                return cast(AdcpAsyncResponseData, data)

        return None

    # Detect A2A TaskStatusUpdateEvent format (has nested "status.message")
    status = webhook_payload.get("status")
    if isinstance(status, dict):
        message = status.get("message")
        if isinstance(message, dict):
            # Extract from status.message.parts[]
            parts = message.get("parts", [])
            if not parts:
                return None

            # Find DataPart
            for part in parts:
                if "data" in part:
                    data = part["data"]
                    # Unwrap {"response": {...}} wrapper if present
                    if isinstance(data, dict) and "response" in data and len(data) == 1:
                        return cast(AdcpAsyncResponseData, data["response"])
                    return cast(AdcpAsyncResponseData, data)

            return None

    # MCP format: result field directly
    return cast(AdcpAsyncResponseData | None, webhook_payload.get("result"))


def create_a2a_webhook_payload(
    task_id: str,
    status: GeneratedTaskStatus,
    context_id: str,
    result: AdcpAsyncResponseData | dict[str, Any],
    timestamp: datetime | None = None,
) -> Task | TaskStatusUpdateEvent:
    """
    Create A2A webhook payload (Task or TaskStatusUpdateEvent).

    Per A2A specification:
    - Terminated statuses (completed, failed): Returns Task with artifacts[].parts[]
    - Intermediate statuses (working, input-required, submitted): Returns TaskStatusUpdateEvent
      with status.message.parts[]

    This function helps agent implementations construct properly formatted A2A webhook
    payloads for sending to clients.

    Args:
        task_id: Unique identifier for the task
        status: Current task status
        context_id: Session/conversation identifier (required by A2A protocol)
        timestamp: When the webhook was generated (defaults to current UTC time)
        result: Task-specific payload (AdCP response data)

    Returns:
        Task object for terminated statuses, TaskStatusUpdateEvent for intermediate statuses

    Examples:
        Create a completed Task webhook:
        >>> from adcp.webhooks import create_a2a_webhook_payload
        >>> from adcp.types import GeneratedTaskStatus
        >>>
        >>> task = create_a2a_webhook_payload(
        ...     task_id="task_123",
        ...     status=GeneratedTaskStatus.completed,
        ...     result={"products": [...]},
        ...     message="Found 5 products"
        ... )
        >>> # task is a Task object with artifacts containing the result

        Create a working status update:
        >>> event = create_a2a_webhook_payload(
        ...     task_id="task_456",
        ...     status=GeneratedTaskStatus.working,
        ...     message="Processing 3 of 10 items"
        ... )
        >>> # event is a TaskStatusUpdateEvent with status.message

        Send A2A webhook via HTTP POST:
        >>> import httpx
        >>> from a2a.types import Task
        >>>
        >>> payload = create_a2a_webhook_payload(...)
        >>> # Serialize to dict for JSON
        >>> if isinstance(payload, Task):
        ...     payload_dict = payload.model_dump(mode='json')
        ... else:
        ...     payload_dict = payload.model_dump(mode='json')
        >>>
        >>> response = await httpx.post(webhook_url, json=payload_dict)
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    # Convert datetime to ISO string for A2A protocol
    timestamp_str = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    timestamp_proto = _isoformat_to_proto_timestamp(timestamp_str) if timestamp_str else None

    # Map GeneratedTaskStatus to A2A TaskState enum value.
    status_value = status.value if hasattr(status, "value") else str(status)
    adcp_to_task_state: dict[str, int] = {
        "completed": pb.TaskState.TASK_STATE_COMPLETED,
        "failed": pb.TaskState.TASK_STATE_FAILED,
        "working": pb.TaskState.TASK_STATE_WORKING,
        "submitted": pb.TaskState.TASK_STATE_SUBMITTED,
        "input_required": pb.TaskState.TASK_STATE_INPUT_REQUIRED,
        # Tolerate the hyphenated form servers may echo back.
        "input-required": pb.TaskState.TASK_STATE_INPUT_REQUIRED,
    }
    task_state_enum = adcp_to_task_state.get(status_value, pb.TaskState.TASK_STATE_UNSPECIFIED)

    # Build parts for the message/artifact.
    parts: list[pb.Part] = []

    # Convert AdcpAsyncResponseData to dict if it's a Pydantic model
    if hasattr(result, "model_dump"):
        result_dict: dict[str, Any] = result.model_dump(mode="json")
    else:
        result_dict = result

    value = Value()
    ParseDict(result_dict, value)
    parts.append(pb.Part(data=value))

    # Determine if this is a terminated status (Task) or intermediate (TaskStatusUpdateEvent)
    is_terminated = status in [GeneratedTaskStatus.completed, GeneratedTaskStatus.failed]

    if is_terminated:
        status_kwargs: dict[str, Any] = {"state": task_state_enum}
        if timestamp_proto is not None:
            status_kwargs["timestamp"] = timestamp_proto
        task_status = pb.TaskStatus(**status_kwargs)

        artifacts = (
            [
                pb.Artifact(
                    artifact_id=f"{task_id}_result",
                    parts=parts,
                )
            ]
            if parts
            else []
        )

        return pb.Task(
            id=task_id,
            status=task_status,
            artifacts=artifacts,
            context_id=context_id,
        )

    # Intermediate status: build a Message carrying the parts and nest it
    # inside TaskStatus.message so the event mirrors the spec shape.
    message_obj = None
    if parts:
        message_obj = pb.Message(
            message_id=f"{task_id}_msg",
            role=pb.Role.ROLE_AGENT,
            parts=parts,
        )

    status_kwargs = {"state": task_state_enum}
    if timestamp_proto is not None:
        status_kwargs["timestamp"] = timestamp_proto
    if message_obj is not None:
        status_kwargs["message"] = message_obj
    task_status = pb.TaskStatus(**status_kwargs)

    return pb.TaskStatusUpdateEvent(
        task_id=task_id,
        status=task_status,
        context_id=context_id,
    )


def _isoformat_to_proto_timestamp(
    value: str | datetime,
) -> Any:
    """Convert an ISO-8601 string or datetime to a ``google.protobuf.Timestamp``.

    Returns ``None`` when the input is falsy. Any parse failure falls back
    to ``None`` rather than raising — webhook callers may pass pre-formatted
    strings from non-ISO sources, and losing the timestamp is better than
    raising mid-delivery.
    """
    from google.protobuf.timestamp_pb2 import Timestamp

    if not value:
        return None
    ts = Timestamp()
    try:
        if isinstance(value, datetime):
            ts.FromDatetime(value)
        else:
            ts.FromJsonString(value)
    except (ValueError, TypeError):
        return None
    return ts


_AUTH_DEPRECATION_WARNED = False
_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "content-digest",
        "content-length",
        "content-type",
        "host",
        "signature",
        "signature-input",
        "x-adcp-signature",
        "x-adcp-timestamp",
    }
)
_HEADER_FORBIDDEN_CHARS = ("\r", "\n", "\x00")
_MAX_HEADER_VALUE_BYTES = 8192
_DEFAULT_TIMEOUT_SECONDS = 10.0
# 10MB cap matches typical buyer-side reverse-proxy limits and is ~100×
# the realistic AdCP payload (biggest seen: get_products with long product
# lists, rarely over 100KB). Serialized bytes, not dict size — post-
# serialization check avoids a pre-cap on dict size being meaningless.
_MAX_BODY_BYTES = 10 * 1024 * 1024
# Cap extra_headers count so a caller that iterates a large container
# into the kwarg can't produce an unbounded header block.
_MAX_EXTRA_HEADERS = 64


def _warn_auth_deprecation_once() -> None:
    global _AUTH_DEPRECATION_WARNED
    if _AUTH_DEPRECATION_WARNED:
        return
    _AUTH_DEPRECATION_WARNED = True
    warnings.warn(
        "PushNotificationConfig.authentication (Bearer, HMAC-SHA256) is "
        "deprecated in AdCP 4.0. Migrate senders to adcp.webhooks.WebhookSender "
        "(RFC 9421 signing) and receivers to the 9421 webhook profile. This "
        "warning fires once per process.",
        DeprecationWarning,
        stacklevel=3,
    )


async def deliver(
    config: AdCPBaseModel | Mapping[str, Any],
    payload: AdCPBaseModel | Task | TaskStatusUpdateEvent | Mapping[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
    extra_headers: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
    token_field: str | None = None,
    allow_private: bool = False,
    allowed_ports: frozenset[int] | None = None,
) -> httpx.Response:
    """Dispatch one legacy-auth webhook in a single call.

    Collapses the sender's six-step boilerplate (build envelope, serialize,
    sign, merge headers, POST, echo token) into one call so the signer and
    the wire see the *same bytes*. The serialization-format drift that
    plagued the hand-rolled path — ``json=`` in httpx re-serializes the dict
    and breaks ``Content-Digest`` — is structurally impossible here: the
    helper JSON-serializes once, signs those bytes, and POSTs those bytes
    via ``content=``.

    This helper is for the **legacy** AdCP 3.x authentication schemes
    (``Bearer`` / ``HMAC-SHA256``) and emits a :class:`DeprecationWarning`
    on first use. For 4.0+ integrations use :class:`WebhookSender` (RFC 9421).

    Args:
        config: A :class:`PushNotificationConfig`, :class:`ReportingWebhook`,
            or equivalent dict. Must carry ``url`` (``https://`` only) and
            ``authentication.{schemes, credentials}``.
        payload: The webhook body. Accepts a Pydantic model (e.g. built via
            :func:`create_mcp_webhook_payload` / :func:`create_a2a_webhook_payload`),
            an a2a ``Task`` / ``TaskStatusUpdateEvent``, or a plain dict.
            Models are dumped with ``mode="json", exclude_none=True``.
        client: Optional shared ``httpx.AsyncClient``. When supplied, the
            caller owns SSRF guarantees — the helper trusts the operator's
            transport completely (typically a vetted egress proxy with
            mTLS, or an ASGI transport for testing). When omitted, the
            helper builds a per-request :class:`adcp.signing.IpPinnedTransport`
            so the URL is resolved, SSRF-validated, and pinned to the
            resolved IP — same defense applied to :class:`WebhookSender`.
        allow_private: Forwarded to the per-request pinned transport
            (owned-client path only). ``False`` (default) rejects URLs
            whose resolved IP is in a private / loopback / link-local
            range. Set ``True`` for dev/CI fixtures that post to internal
            endpoints; production should leave it ``False``.
        allowed_ports: Forwarded to the per-request pinned transport
            (owned-client path only). ``None`` (default) imposes no port
            filter — AdCP doesn't constrain webhook ports. Hardened
            deployments pass :data:`adcp.signing.DEFAULT_ALLOWED_PORTS`
            (`{443, 8443}`) or a custom set.
        extra_headers: Merged last. May not override any of
            ``Content-Type``, ``Content-Digest``, ``Content-Length``,
            ``Host``, ``Authorization``, ``Signature``, ``Signature-Input``,
            ``X-AdCP-Signature``, or ``X-AdCP-Timestamp``. Auth and
            signature-binding headers are sender-owned so the signer and
            the wire cannot disagree.
        timeout_seconds: Per-request timeout applied only when the helper
            creates its own client. Raises ``ValueError`` if set alongside
            ``client=`` — configure the timeout on the shared client instead.
        token_field: Opt-in field name for echoing ``config.token`` into
            the payload body (top-level for MCP dicts, under ``metadata``
            for ``Task`` / ``TaskStatusUpdateEvent``). Default ``None``
            disables echo; there is no spec-defined field name, so the
            caller must pick one the receiver agrees to read.

    Returns:
        The raw ``httpx.Response``. Caller is responsible for
        ``response.status_code`` inspection and retry scheduling. For retry,
        pass the *same, unmutated* payload again — serialization is
        deterministic so retries produce byte-identical bodies (spec-correct
        receiver dedup via ``idempotency_key``). Mutating the payload dict
        between attempts breaks byte-identity; callers who need byte-identical
        HTTP envelopes across retries (including headers) should use
        :class:`WebhookSender` and :meth:`WebhookSender.resend`. There is
        intentionally no ``resend()`` here — the retry contract is "call
        ``deliver`` again with the same inputs".

    Raises:
        ValueError: missing ``url``, non-HTTPS URL, control characters in
            header values, missing / unknown ``authentication`` (use
            :class:`WebhookSender` for RFC 9421), overriding a reserved
            header, or setting ``timeout_seconds`` alongside ``client``.
        DeprecationWarning (fires once): ``authentication`` is a 3.x fallback.

    Security notes:
        * ``config.url`` is buyer-controlled. The helper enforces HTTPS,
          rejects control characters, AND (on the owned-client path)
          builds a per-request IP-pinned transport that runs the full
          SSRF range check (loopback / RFC 1918 / link-local / CGNAT /
          IPv6 ULA / multicast / cloud metadata) and pins the connection
          to the validated IP. Operator-supplied clients skip the SSRF
          guard — they own egress policy on their transport.
        * ``config.token`` sits in the request body, so any receiver that
          logs bodies retains it indefinitely. Treat the token as a
          medium-sensitivity correlator, not a long-lived secret.
        * At ``httpx`` DEBUG log level, ``Authorization`` and
          ``X-AdCP-Signature`` appear in logs — gate DEBUG in production.
    """
    if client is not None and timeout_seconds is not None:
        raise ValueError(
            "timeout_seconds cannot be set when client= is provided; "
            "configure the timeout on your shared httpx.AsyncClient instead."
        )

    url, token, auth_scheme, credentials = _extract_config_fields(config)

    if auth_scheme is None:
        raise ValueError(
            "config.authentication is required for deliver(). "
            "For RFC 9421 signing (the AdCP 4.0 default), use "
            "adcp.webhooks.WebhookSender — no helper for unsigned webhooks "
            "is provided because the spec requires signing."
        )
    if auth_scheme not in ("Bearer", "HMAC-SHA256"):
        raise ValueError(
            f"unknown authentication scheme {auth_scheme!r}; "
            "supported legacy schemes are 'Bearer' and 'HMAC-SHA256'. "
            "For RFC 9421 use adcp.webhooks.WebhookSender."
        )

    _warn_auth_deprecation_once()

    # Build the pinned transport up-front (owned-client path). SSRF
    # validation runs synchronously inside ``build_async_ip_pinned_transport``
    # — a hostile URL raises ``SSRFValidationError`` before we serialize
    # the body or compute the HMAC, so a buyer-supplied 127.0.0.1 URL
    # does not produce an HMAC-over-buyer-body sitting in process memory
    # for fault-handlers / custom logging to capture on exception.
    # Mirrors the WebhookSender._send_bytes ordering.
    transport: Any = None
    if client is None:
        from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport

        transport = build_async_ip_pinned_transport(
            url,
            allow_private=allow_private,
            allowed_ports=allowed_ports,
        )

    body_dict = _payload_to_dict(payload)
    if token is not None and token_field is not None:
        _validate_header_value("config.token", token)
        _inject_push_token(body_dict, token, payload, token_field)

    # Compact separators so the signer and the wire see byte-identical
    # payloads, matching the canonical on-wire form pinned by
    # adcontextprotocol/adcp#2478. ``_compute_legacy_signature`` returns the
    # same compact body bytes below — we serialize here for the size check
    # and Bearer path, which both operate on the final transmitted bytes.
    body_bytes = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
    if len(body_bytes) > _MAX_BODY_BYTES:
        raise ValueError(
            f"serialized webhook body is {len(body_bytes):,} bytes, over the "
            f"{_MAX_BODY_BYTES:,}-byte cap. Split into smaller webhooks or use "
            "the batch-reporting endpoints — most receivers reject bodies over "
            "10MB at the reverse proxy anyway."
        )

    headers: dict[str, str] = {"Content-Type": "application/json"}

    if auth_scheme == "Bearer":
        if not credentials:
            raise ValueError(
                "config.authentication.schemes=['Bearer'] requires "
                "authentication.credentials (min 32 characters — token "
                "exchanged out-of-band with the receiver)."
            )
        _validate_header_value("authentication.credentials", credentials)
        headers["Authorization"] = f"Bearer {credentials}"
    else:  # HMAC-SHA256
        if not credentials:
            raise ValueError(
                "config.authentication.schemes=['HMAC-SHA256'] requires "
                "authentication.credentials (min 32 characters — shared "
                "secret exchanged out-of-band with the receiver)."
            )
        _validate_header_value("authentication.credentials", credentials)
        get_adcp_signed_headers_for_webhook(
            headers,
            secret=credentials,
            timestamp=str(int(time.time())),
            payload=body_dict,
        )

    if extra_headers:
        if len(extra_headers) > _MAX_EXTRA_HEADERS:
            raise ValueError(
                f"extra_headers has {len(extra_headers)} entries; "
                f"helper caps at {_MAX_EXTRA_HEADERS}. Pass only the custom "
                "headers you actually need (trace IDs, correlation IDs)."
            )
        for key in extra_headers:
            normalized = str(key).lower()
            if normalized in _RESERVED_HEADERS or normalized.startswith(":"):
                raise ValueError(_reserved_header_message(normalized, key))
        for key, value in extra_headers.items():
            _validate_header_value(f"extra_headers[{key!r}]", value)
            headers[key] = value

    effective_timeout = timeout_seconds if timeout_seconds is not None else _DEFAULT_TIMEOUT_SECONDS
    if client is None:
        # Owned-client path. ``transport`` was built up-front so SSRF
        # rejected before signing; here we just construct the per-request
        # client. ``follow_redirects=False`` closes rebinding-via-redirect;
        # ``trust_env=False`` blocks ``HTTPS_PROXY`` env-var bypass.
        # Same shape as ``WebhookSender._send_bytes``.
        async with httpx.AsyncClient(
            transport=transport,
            timeout=effective_timeout,
            follow_redirects=False,
            trust_env=False,
        ) as http_client:
            return await http_client.post(url, content=body_bytes, headers=headers)
    # Operator-supplied client: trust them completely; they own SSRF
    # guarantees on their transport (vetted egress proxy, ASGI test
    # transport, etc.).
    return await client.post(url, content=body_bytes, headers=headers)


def _extract_config_fields(
    config: AdCPBaseModel | Mapping[str, Any],
) -> tuple[str, str | None, str | None, str | None]:
    """Pull ``url``, ``token``, auth scheme, and credentials out of a webhook config.

    Accepts either a ``PushNotificationConfig`` / ``ReportingWebhook`` model
    or an equivalent dict — sellers often receive these as plain dicts from
    an incoming AdCP request and shouldn't have to round-trip through the
    Pydantic model just to dispatch a webhook.

    Validates the URL at the boundary: HTTPS only, no control characters.
    """
    if hasattr(config, "model_dump"):
        cfg = cast(AdCPBaseModel, config).model_dump(mode="json", exclude_none=True)
    else:
        cfg = dict(config)

    url_value = cfg.get("url")
    if not url_value:
        raise ValueError(
            "webhook config is missing required 'url' field. Pass a "
            "PushNotificationConfig, ReportingWebhook, or dict with an "
            "https:// 'url'."
        )
    url = str(url_value)
    if any(c in url for c in _HEADER_FORBIDDEN_CHARS):
        raise ValueError(
            "webhook config 'url' contains control characters "
            "(newline, carriage return, or NUL are not allowed in URLs)"
        )
    lower = url.lower()
    if not lower.startswith("https://"):
        scheme_end = lower.find("://")
        shown_scheme = lower[:scheme_end] if scheme_end >= 0 else "<no scheme>"
        raise ValueError(
            f"webhook config 'url' must use https:// (got scheme {shown_scheme!r}). "
            "HTTP and other schemes are rejected because they expose the "
            "webhook body, token, and Authorization header in transit."
        )
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "webhook config 'url' must not embed userinfo (user:pass@host). "
            "Pass credentials via config.authentication.credentials instead — "
            "URLs get logged by proxies, load balancers, and httpx DEBUG."
        )

    token = cfg.get("token")

    auth_raw = cfg.get("authentication")
    if auth_raw is not None and not isinstance(auth_raw, Mapping):
        raise ValueError(
            f"config.authentication must be an object with 'schemes' + "
            f"'credentials', got {type(auth_raw).__name__}"
        )
    auth: Mapping[str, Any] = auth_raw or {}
    schemes_raw = auth.get("schemes")
    if schemes_raw is not None and not isinstance(schemes_raw, (list, tuple)):
        raise ValueError(
            "config.authentication.schemes must be a list, got " f"{type(schemes_raw).__name__}"
        )
    schemes = list(schemes_raw or [])
    if len(schemes) > 1:
        raise ValueError(
            f"config.authentication.schemes has {len(schemes)} entries; "
            "the AdCP legacy auth schema allows exactly one scheme per config."
        )
    scheme = schemes[0] if schemes else None
    credentials = auth.get("credentials")

    return url, token, scheme, credentials


def _reserved_header_message(normalized: str, original_key: Any) -> str:
    """Build a fix-the-error message tailored to the reserved header class.

    The mistake category differs sharply by header: a caller passing
    ``Authorization`` usually doesn't know about ``config.authentication``;
    a caller passing ``Content-Type`` is probably debugging and reached for
    the override by reflex. Give each the right nudge."""
    if normalized == "authorization":
        return (
            f"extra_headers may not override {original_key!r} — set "
            "config.authentication.schemes=['Bearer'] + credentials instead. "
            "The helper derives Authorization from config so the signer and "
            "the wire cannot disagree."
        )
    if normalized in ("signature", "signature-input", "content-digest"):
        return (
            f"extra_headers may not override {original_key!r} — RFC 9421 "
            "signing headers are produced by adcp.webhooks.WebhookSender, "
            "not injected. Switch helpers if you need 9421."
        )
    if normalized in ("x-adcp-signature", "x-adcp-timestamp"):
        return (
            f"extra_headers may not override {original_key!r} — these are "
            "the HMAC-SHA256 signature headers the helper produces from "
            "config.authentication.credentials."
        )
    if normalized == "content-type":
        return (
            f"extra_headers may not override {original_key!r}; "
            "the helper always sends 'application/json'."
        )
    return (
        f"extra_headers may not override {original_key!r}; "
        "this header is sender-owned and managed by the helper."
    )


def _payload_to_dict(
    payload: AdCPBaseModel | Task | TaskStatusUpdateEvent | Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize a webhook payload to a JSON-ready dict.

    a2a-sdk ``Task`` / ``TaskStatusUpdateEvent`` are protobuf messages and
    serialize through ``MessageToDict`` with camelCase field names
    (``artifact_id`` → ``artifactId``) so external A2A receivers see the
    on-wire shape they expect. The protobuf default emits enum states as
    ``TASK_STATE_COMPLETED``; we post-process to the 0.3-compatible
    lowercase form (``completed``) so existing A2A buyer webhook
    receivers keep parsing. MCP-shape dicts / AdCP models are dumped
    with camelCase-off defaults.
    """
    if isinstance(payload, (Task, TaskStatusUpdateEvent)):
        data = MessageToDict(payload, preserving_proto_field_name=False)
        _normalize_a2a_task_state_to_v03(data)
        return data
    if hasattr(payload, "model_dump"):
        model = cast(AdCPBaseModel, payload)
        return model.model_dump(mode="json", exclude_none=True)
    return dict(payload)


def _normalize_a2a_task_state_to_v03(payload: dict[str, Any]) -> None:
    """Rewrite enum fields from 1.0 ``TASK_STATE_*`` / ``ROLE_*`` to 0.3 strings.

    Buyer webhook receivers that parse our A2A ``Task`` /
    ``TaskStatusUpdateEvent`` envelopes were built against the 0.3 wire
    shape (``"state": "completed"``, ``"role": "agent"``). The a2a-sdk
    1.0 protobuf JSON emitter produces ``"state": "TASK_STATE_COMPLETED"``
    and ``"role": "ROLE_AGENT"`` by default. This helper rewrites those
    enum-style values in-place to the 0.3 lowercase forms; non-matching
    values pass through unchanged.
    """
    status = payload.get("status")
    if isinstance(status, dict):
        state = status.get("state")
        if isinstance(state, str) and state.startswith("TASK_STATE_"):
            remainder = state[len("TASK_STATE_") :].lower()
            # Spec uses hyphens for multi-word states.
            status["state"] = remainder.replace("_", "-")
        message = status.get("message")
        if isinstance(message, dict):
            _normalize_message_role(message)

    # ``Task.history[]`` carries prior Messages each with a ``role`` that
    # serializes SCREAMING_SNAKE. ``create_a2a_webhook_payload`` does not
    # populate ``history`` today, but hand-built Task payloads or proxies
    # from other sources might — walk them so 0.3 receivers see the
    # spec-expected lowercase form.
    history = payload.get("history")
    if isinstance(history, list):
        for entry in history:
            if isinstance(entry, dict):
                _normalize_message_role(entry)

    # Task envelopes carry parts directly under artifacts[].parts[]; no
    # role field there. But a bare Message payload (edge case) could.
    if "role" in payload:
        _normalize_message_role(payload)


def _normalize_message_role(message: dict[str, Any]) -> None:
    role = message.get("role")
    if isinstance(role, str) and role.startswith("ROLE_"):
        message["role"] = role[len("ROLE_") :].lower()


def _inject_push_token(
    body: dict[str, Any],
    token: str,
    original_payload: AdCPBaseModel | Task | TaskStatusUpdateEvent | Mapping[str, Any],
    token_field: str,
) -> None:
    """Echo ``PushNotificationConfig.token`` into the body for buyer-side auth.

    AdCP 3.x says the token is "echoed back in webhook payload" but doesn't
    name the field. The caller picks ``token_field`` to match whatever the
    receiver is configured to read. A2A ``Task`` / ``TaskStatusUpdateEvent``
    carry a ``metadata`` object — the token lands there so the top-level
    shape stays a valid A2A entity. MCP-shape webhooks and plain dicts get
    the token at top-level (``additionalProperties`` is permitted by the
    MCP webhook payload schema).
    """
    is_a2a = isinstance(original_payload, (Task, TaskStatusUpdateEvent))
    if is_a2a:
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            body["metadata"] = metadata
        metadata.setdefault(token_field, token)
    else:
        body.setdefault(token_field, token)


def _validate_header_value(name: str, value: Any) -> None:
    """Reject control characters and oversize values at the helper boundary.

    httpx rejects bare CRLF at send time, but relying on that is
    defense-in-absentia — a later swap of the HTTP client, or a caller that
    logs the value before sending, would re-open header injection. Enforce
    here so the boundary contract is explicit.
    """
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, got {type(value).__name__}")
    if any(c in value for c in _HEADER_FORBIDDEN_CHARS):
        raise ValueError(f"{name} contains control characters")
    if len(value.encode("utf-8")) > _MAX_HEADER_VALUE_BYTES:
        raise ValueError(f"{name} exceeds {_MAX_HEADER_VALUE_BYTES}-byte limit")


# Sender import is at the bottom to resolve a circular dependency:
# WebhookSender uses create_mcp_webhook_payload / generate_webhook_idempotency_key
# which are defined above. Importing it at the top would try to resolve those
# names before they're bound. This is the canonical Python pattern for breaking
# such cycles without a third helper module.
from adcp.webhook_sender import (  # noqa: E402
    WebhookDeliveryResult,
    WebhookSender,
)
from adcp.webhook_supervisor_pg import (  # noqa: E402
    PgWebhookDeliverySupervisor,
)

__all__ = [
    # Sender — payload builders
    "create_a2a_webhook_payload",
    "create_mcp_webhook_payload",
    "generate_webhook_idempotency_key",
    "get_adcp_signed_headers_for_webhook",
    "sign_legacy_webhook",
    # Sender — 9421 signing (low-level)
    "sign_webhook",
    # Sender — one-call outbound helpers
    "deliver",
    "WebhookDeliveryResult",
    "WebhookSender",
    # Receiver — 9421 verification (low-level)
    "VerifiedWebhookSender",
    "WebhookVerifyOptions",
    "verify_webhook_signature",
    # Receiver — legacy HMAC verification (low-level, 3.x only)
    "LegacyWebhookHmacError",
    "LegacyWebhookHmacOptions",
    "VerifiedLegacyWebhookSender",
    "verify_webhook_hmac",
    # Receiver — one-call helper
    "LegacyHmacFallback",
    "VerifiedSignerLike",
    "WebhookKind",
    "WebhookOutcome",
    "WebhookPayload",
    "WebhookReceiver",
    "WebhookReceiverConfig",
    # Receiver — payload extraction (legacy helper)
    "extract_webhook_result_data",
    # Dedup / idempotency backends (re-exported so one import root suffices)
    "MemoryBackend",
    "WebhookDedupStore",
    # Pg-backed supervisor (requires adcp[pg] extra)
    "PgWebhookDeliverySupervisor",
]
