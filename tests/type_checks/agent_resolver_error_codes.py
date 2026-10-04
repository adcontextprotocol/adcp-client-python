"""Adopter-facing type checks for reporting a discovery failure's spec error code.

A verifier that drives discovery itself — calling ``async_resolve_agent`` rather than
the one-shot ``verify_from_agent_url`` — receives resolver-side codes and has to answer
the request with the ``request_signature_*`` code the discovery-chain rejection table
assigns. ``request_signature_code`` is that mapping, so an adopter reports the same
code the SDK's own verifier surfaces report instead of restating the table locally.
"""

from adcp.signing import (
    AgentResolution,
    AgentResolverError,
    AgentResolverErrorCode,
    async_resolve_agent,
    request_signature_code,
)


async def resolve_or_reject(agent_url: str) -> AgentResolution | str:
    """Resolve ``agent_url``, or return the error code to reject the request with."""
    try:
        return await async_resolve_agent(agent_url, agent_type="sales")
    except AgentResolverError as exc:
        return request_signature_code(exc)


def log_hop(exc: AgentResolverError) -> tuple[AgentResolverErrorCode, str]:
    """The resolver-side code stays available for logs alongside the wire code."""
    return exc.code, request_signature_code(exc)
