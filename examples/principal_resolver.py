"""Coexistence of bearer tenants and identities verified by trusted middleware.

The outer middleware must verify the proxy connection or client certificate
before installing a Principal under state['verified_proxy_principal']. Reading
an arbitrary caller's identity header and copying it into state is insufficient.
Use with serve(handler, transport='both', auth=configure_auth(...),
context_factory=auth_context_factory). Import auth_context_factory from adcp.server.
"""

from adcp.server import (
    AuthRequest,
    BearerTokenAuth,
    Principal,
    PrincipalResolverError,
    TokenValidator,
)


async def resolve_principal(request: AuthRequest) -> Principal | None:
    if request.state.get("verified_proxy_forbidden") is True:
        raise PrincipalResolverError(403)
    principal = request.state.get("verified_proxy_principal")
    # An async DB/IdP lookup can also run here using verified metadata.
    return principal if isinstance(principal, Principal) else None


def configure_auth(validate_token: TokenValidator) -> BearerTokenAuth:
    return BearerTokenAuth(
        validate_token=validate_token,
        resolve_principal=resolve_principal,
    )
