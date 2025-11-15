from __future__ import annotations

"""Exception hierarchy for AdCP client."""


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
            full_message = f"{full_message}\n  💡 {suggestion}"

        super().__init__(full_message)


class ADCPConnectionError(ADCPError):
    """Connection to agent failed."""

    def __init__(self, message: str, agent_id: str | None = None, agent_uri: str | None = None):
        """Initialize connection error."""
        suggestion = (
            "Check that the agent URI is correct and the agent is running.\n"
            "     Try testing with: python -m adcp test --config <agent-id>"
        )
        super().__init__(message, agent_id, agent_uri, suggestion)


class ADCPAuthenticationError(ADCPError):
    """Authentication failed (401, 403)."""

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
    ):
        """Initialize simple API error.

        Args:
            operation: The operation that failed (e.g., "get_products")
            error_message: The underlying error message from TaskResult
            agent_id: Optional agent ID for context
        """
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
