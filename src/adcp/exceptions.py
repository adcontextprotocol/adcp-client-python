"""Exception hierarchy for AdCP client."""

from __future__ import annotations

from typing import Any


class ADCPError(Exception):
    """Base exception for all AdCP client errors."""

    def __init__(
        self,
        message: str,
        agent_id: str | None = None,
        agent_uri: str | None = None,
        suggestion: str | None = None,
    ):
        """Initialize exception with context."""
        self.message = message
        self.agent_id = agent_id
        self.agent_uri = agent_uri
        self.suggestion = suggestion

        full_message = message
        if agent_id:
            full_message = f"[Agent: {agent_id}] {full_message}"
        if agent_uri:
            full_message = f"{full_message}\n  URI: {agent_uri}"
        if suggestion:
            full_message = f"{full_message}\n  Suggestion: {suggestion}"

        super().__init__(full_message)

    @property
    def is_retryable(self) -> bool:
        """Whether this error is safe to retry."""
        return False


class ADCPConnectionError(ADCPError):
    """Connection to agent failed."""

    def __init__(self, message: str, agent_id: str | None = None, agent_uri: str | None = None):
        """Initialize connection error."""
        suggestion = (
            "Check that the agent URI is correct and the agent is running.\n"
            "     Try testing with: python -m adcp test --config <agent-id>"
        )
        super().__init__(message, agent_id, agent_uri, suggestion)

    @property
    def is_retryable(self) -> bool:
        return True


class ADCPAuthenticationError(ADCPError):
    """Authentication failed (401, 403).

    `is_retryable` defaults to ``False`` (inherited). Per the AdCP 3.0.4 prose
    tightening, `AUTH_REQUIRED` covers two sub-cases: credentials missing
    (correctable — supply credentials and retry) and credentials presented but
    rejected (terminal — re-presenting creates SSO retry-storm patterns).
    Defaulting to non-retryable is the safe biased-toward-the-dangerous-case
    choice; callers handling the missing-credentials case should retry only
    after attaching credentials, not on a timer. The 3.1 line splits this
    into `AUTH_MISSING` and `AUTH_INVALID`.
    """

    def __init__(self, message: str, agent_id: str | None = None, agent_uri: str | None = None):
        """Initialize authentication error."""
        suggestion = (
            "Check that your auth_token is valid and not expired.\n"
            "     Verify auth_type ('bearer' vs 'token') and auth_header are correct.\n"
            "     Some agents (like Optable) require auth_type='bearer' and "
            "auth_header='Authorization'"
        )
        super().__init__(message, agent_id, agent_uri, suggestion)


class ADCPTimeoutError(ADCPError):
    """Request timed out."""

    def __init__(
        self,
        message: str,
        agent_id: str | None = None,
        agent_uri: str | None = None,
        timeout: float | None = None,
    ):
        """Initialize timeout error."""
        suggestion = (
            f"The request took longer than {timeout}s." if timeout else "The request timed out."
        )
        suggestion += "\n     Try increasing the timeout value or check if the agent is overloaded."
        super().__init__(message, agent_id, agent_uri, suggestion)

    @property
    def is_retryable(self) -> bool:
        return True


class ADCPProtocolError(ADCPError):
    """Protocol-level error (malformed response, unexpected format)."""

    def __init__(self, message: str, agent_id: str | None = None, protocol: str | None = None):
        """Initialize protocol error."""
        suggestion = (
            f"The agent returned an unexpected {protocol} response format."
            if protocol
            else "Unexpected response format."
        )
        suggestion += "\n     Enable debug mode to see the full request/response."
        super().__init__(message, agent_id, None, suggestion)


class ADCPToolNotFoundError(ADCPError):
    """Requested tool not found on agent."""

    def __init__(
        self, tool_name: str, agent_id: str | None = None, available_tools: list[str] | None = None
    ):
        """Initialize tool not found error."""
        message = f"Tool '{tool_name}' not found on agent"
        suggestion = "List available tools with: python -m adcp list-tools --config <agent-id>"
        if available_tools:
            tools_list = ", ".join(available_tools[:5])
            if len(available_tools) > 5:
                tools_list += f", ... ({len(available_tools)} total)"
            suggestion = f"Available tools: {tools_list}"
        super().__init__(message, agent_id, None, suggestion)


class ADCPWebhookError(ADCPError):
    """Webhook handling error."""


class ADCPWebhookSignatureError(ADCPWebhookError):
    """Webhook signature verification failed."""

    def __init__(self, message: str = "Invalid webhook signature", agent_id: str | None = None):
        """Initialize webhook signature error."""
        suggestion = (
            "Verify that the webhook_secret matches the secret configured on the agent.\n"
            "     Webhook signatures use HMAC-SHA256 for security."
        )
        super().__init__(message, agent_id, None, suggestion)


class ADCPSimpleAPIError(ADCPError):
    """Error from simplified API (.simple accessor).

    Raised when a simple API method fails. The underlying error details
    are available in the message. For more control over error handling,
    use the standard API (client.method()) instead of client.simple.method().
    """

    def __init__(
        self,
        operation: str,
        error_message: str | None = None,
        agent_id: str | None = None,
        errors: list[Any] | None = None,
    ):
        """Initialize simple API error.

        Args:
            operation: The operation that failed (e.g., "get_products")
            error_message: The underlying error message from TaskResult
            agent_id: Optional agent ID for context
            errors: Structured ADCP error objects from the response
        """
        self.operation = operation
        self.errors = errors or []

        message = f"{operation} failed"
        if error_message:
            message = f"{message}: {error_message}"

        suggestion = (
            f"For more control over error handling, use the standard API:\n"
            f"     result = await client.{operation}(request)\n"
            f"     if not result.success:\n"
            f"         # Handle error with full TaskResult context"
        )
        super().__init__(message, agent_id, None, suggestion)


class RegistryError(ADCPError):
    """Error from AdCP registry API operations (brand/property lookups)."""

    def __init__(self, message: str, status_code: int | None = None):
        """Initialize registry error."""
        self.status_code = status_code
        suggestion = "Check that the registry API is accessible and the domain is valid."
        super().__init__(message, suggestion=suggestion)


class ADCPFeatureUnsupportedError(ADCPError):
    """Seller does not support one or more required features."""

    def __init__(
        self,
        unsupported_features: list[str],
        declared_features: list[str] | None = None,
        agent_id: str | None = None,
        agent_uri: str | None = None,
    ):
        """Initialize feature unsupported error.

        Args:
            unsupported_features: Features that are not supported.
            declared_features: Features the seller does declare.
            agent_id: Optional agent ID for context.
            agent_uri: Optional agent URI for context.
        """
        self.unsupported_features = unsupported_features
        self.declared_features = declared_features or []

        missing = ", ".join(unsupported_features)
        message = f"Seller does not support: {missing}"

        suggestion = None
        if self.declared_features:
            declared = ", ".join(sorted(self.declared_features))
            suggestion = f"Declared features: {declared}"

        super().__init__(message, agent_id, agent_uri, suggestion)


class ADCPSigningRequiredError(ADCPError):
    """Raised when an operation in the seller's ``request_signing.required_for``
    is called without a ``SigningConfig`` on the client.

    Signing a ``required_for`` operation is mandatory — sending it unsigned
    would produce a ``request_signature_required`` rejection from the seller.
    Raising locally before the wire call saves a round-trip and gives the
    caller a clear, actionable error.
    """

    def __init__(
        self,
        operation: str,
        agent_id: str | None = None,
        agent_uri: str | None = None,
    ):
        self.operation = operation
        message = (
            f"Operation {operation!r} is in the seller's request_signing.required_for "
            f"list; signing is mandatory but no SigningConfig was provided"
        )
        suggestion = (
            "Pass signing=SigningConfig(private_key=..., key_id=...) when "
            "constructing ADCPClient. See adcp-keygen for key generation."
        )
        super().__init__(message, agent_id, agent_uri, suggestion)


class AdagentsValidationError(ADCPError):
    """Base error for adagents.json validation issues."""


class AdagentsNotFoundError(AdagentsValidationError):
    """adagents.json file not found (404)."""

    def __init__(self, publisher_domain: str):
        """Initialize not found error."""
        message = f"adagents.json not found for domain: {publisher_domain}"
        suggestion = (
            "Verify that the publisher has deployed adagents.json to:\n"
            f"     https://{publisher_domain}/.well-known/adagents.json"
        )
        super().__init__(message, None, None, suggestion)


class AdagentsTimeoutError(AdagentsValidationError):
    """Request for adagents.json timed out."""

    def __init__(self, publisher_domain: str, timeout: float):
        """Initialize timeout error."""
        message = f"Request to fetch adagents.json timed out after {timeout}s"
        suggestion = (
            "The publisher's server may be slow or unresponsive.\n"
            "     Try increasing the timeout value or check the domain is correct."
        )
        super().__init__(message, None, None, suggestion)


class AdagentsAccessBlockedError(AdagentsValidationError):
    """adagents.json fetch blocked by publisher-side bot management (403, cf-mitigated: challenge).

    Only surfaces in direct-fetch workflows (``fetch_adagents``). SDK callers that
    use ``fetch_agent_authorizations`` avoid this entirely — the AAO directory crawler
    handles publisher fetches and serves cached results without exposing the SDK to
    publisher-side bot management.

    If you need to catch this specifically without catching all
    ``AdagentsValidationError``s, use ``except AdagentsAccessBlockedError``.
    """

    def __init__(self, publisher_domain: str):
        """Initialize bot-management blocked error."""
        self.publisher_domain = publisher_domain
        message = (
            f"adagents.json blocked by bot management for {publisher_domain} "
            f"(HTTP 403, cf-mitigated: challenge)"
        )
        suggestion = (
            "The publisher's origin blocked this request with a Cloudflare bot management\n"
            "     challenge. This only affects direct adagents.json fetches (fetch_adagents).\n"
            "\n"
            "     To unblock local debugging:\n"
            "     - Retry with a browser-like User-Agent via the user_agent= parameter, e.g.\n"
            '       user_agent="Mozilla/5.0"\n'
            "     - Or call fetch_agent_authorizations() to query the AAO directory instead,\n"
            "       which bypasses publisher-side bot management entirely."
        )
        super().__init__(message, None, None, suggestion)


class ADCPTaskError(ADCPError):
    """A task returned an ADCP error response.

    Provides structured access to the error objects from the response,
    including error codes for programmatic handling.
    """

    def __init__(
        self,
        operation: str,
        errors: list[Any],
        agent_id: str | None = None,
    ):
        """Initialize task error.

        Args:
            operation: The task that failed (e.g., "create_media_buy")
            errors: List of ADCP Error objects from the response
            agent_id: Optional agent ID for context
        """
        self.operation = operation
        self.errors = errors
        self.error_codes = [e.code for e in errors if hasattr(e, "code") and e.code]

        message = f"{operation} failed"
        if errors:
            first_msg = getattr(errors[0], "message", str(errors[0]))
            message = f"{operation} failed: {first_msg}"
            if len(errors) > 1:
                message += f" (+{len(errors) - 1} more)"

        super().__init__(message, agent_id=agent_id)

    @property
    def is_retryable(self) -> bool:
        """True if any error code is transient (RATE_LIMITED, etc.)."""
        from adcp.server.helpers import TRANSIENT_CODES

        return bool(TRANSIENT_CODES & set(self.error_codes))


class IdempotencyConflictError(ADCPTaskError):
    """Server rejected a reused idempotency_key whose payload differs from the original.

    The request used the same idempotency_key as an earlier request but with a
    materially different (post-JCS-canonicalization) payload. Two valid recovery
    paths: (a) mint a fresh ``uuid.uuid4()`` key and resubmit, or (b) resend the
    exact original payload — whichever matches the caller's intent.

    By design the rendered message does NOT include the server's error text
    because non-compliant sellers may include payload hints that violate the
    ``IDEMPOTENCY_CONFLICT`` spec requirement. Raw errors remain on
    ``self.errors`` for callers that want to inspect.
    """

    def __init__(
        self,
        operation: str,
        errors: list[Any],
        agent_id: str | None = None,
    ):
        self.operation = operation
        self.errors = errors
        self.error_codes = [e.code for e in errors if hasattr(e, "code") and e.code] or [
            "IDEMPOTENCY_CONFLICT"
        ]
        message = f"{operation}: idempotency_key reused with a different payload"
        suggestion = (
            "The server already has a response for this idempotency_key with a "
            "different (JCS-canonicalized) payload. Either resend the exact original "
            "payload, or mint a fresh key with uuid.uuid4() and resubmit. Do NOT "
            "reuse this key with modified fields."
        )
        # Skip ADCPTaskError.__init__ to avoid leaking server-supplied text.
        ADCPError.__init__(self, message, agent_id=agent_id, suggestion=suggestion)


class IdempotencyExpiredError(ADCPTaskError):
    """Server's replay cache for this idempotency_key has expired.

    Per AdCP #2315 the seller MAY discard cached responses after
    ``replay_ttl_seconds``. Re-executing is unsafe because the seller can no
    longer distinguish "seen and evicted" from "never seen" — silently retrying
    risks duplicate execution. Recovery: reconcile state via a read (e.g.
    ``get_media_buys``) before resubmitting with a fresh key.
    """

    def __init__(
        self,
        operation: str,
        errors: list[Any],
        agent_id: str | None = None,
    ):
        self.operation = operation
        self.errors = errors
        self.error_codes = [e.code for e in errors if hasattr(e, "code") and e.code] or [
            "IDEMPOTENCY_EXPIRED"
        ]
        message = f"{operation}: idempotency replay window has expired"
        suggestion = (
            "The seller's replay_ttl_seconds window for this key has passed. "
            "Re-execution is unsafe — the seller can no longer guarantee "
            "at-most-once. Reconcile state with a read (e.g. get_media_buys) "
            "before resubmitting with a fresh uuid.uuid4() key."
        )
        ADCPError.__init__(self, message, agent_id=agent_id, suggestion=suggestion)


class IdempotencyUnsupportedError(ADCPError):
    """Seller does not support idempotency replay protection on mutating requests.

    Raised before the first mutating call when ``strict_idempotency=True`` and
    either the seller's capabilities response is missing ``adcp.idempotency``,
    declares ``supported=False``, or declares ``supported=True`` without a
    ``replay_ttl_seconds`` window. Per AdCP spec, clients MUST NOT assume a
    default — a seller that does not positively declare support cannot be
    safely retried.
    """

    def __init__(
        self,
        agent_id: str | None = None,
        agent_uri: str | None = None,
        reason: str | None = None,
    ):
        detail = reason or "seller did not declare adcp.idempotency support"
        message = f"{detail}; retry safety for mutating requests cannot be guaranteed."
        suggestion = (
            "Recommended: ask the seller to declare adcp.idempotency.supported=true "
            "with a replay_ttl_seconds window in get_adcp_capabilities. To proceed "
            "without this guarantee — retries may double-charge or duplicate — "
            "construct ADCPClient with strict_idempotency=False; the caller then "
            "owns reconciliation on retry."
        )
        super().__init__(message, agent_id, agent_uri, suggestion)


class ConfigurationError(ADCPError):
    """Invalid SDK configuration detected at construction time.

    Raised when a value passed to a client/server constructor cannot
    be reconciled with the SDK's compile-time pin — most commonly a
    cross-major ``adcp_version`` (e.g. ``adcp_version="4.0"`` against
    an SDK built for AdCP 3.x), or an unparseable version string.

    Recovery: install the SDK major that targets the wire version you
    want to speak. Cross-major pinning is not supported within a
    single SDK major.
    """


IDEMPOTENCY_ERROR_CODE_MAP: dict[str, type[ADCPTaskError]] = {
    "IDEMPOTENCY_CONFLICT": IdempotencyConflictError,
    "IDEMPOTENCY_EXPIRED": IdempotencyExpiredError,
}


def classify_task_error(
    operation: str,
    errors: list[Any],
    agent_id: str | None = None,
) -> ADCPTaskError:
    """Build the most specific ADCPTaskError subclass matching the response codes."""
    for err in errors:
        code = getattr(err, "code", None) or (err.get("code") if isinstance(err, dict) else None)
        if code and code in IDEMPOTENCY_ERROR_CODE_MAP:
            return IDEMPOTENCY_ERROR_CODE_MAP[code](operation, errors, agent_id=agent_id)
    return ADCPTaskError(operation, errors, agent_id=agent_id)
