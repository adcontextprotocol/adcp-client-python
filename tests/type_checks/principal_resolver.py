"""Adopter examples for metadata-only sync and async authentication resolution."""

from adcp.server import (
    AsyncPrincipalResolver,
    AuthRequest,
    BearerTokenAuth,
    BearerTokenAuthMiddleware,
    Principal,
    PrincipalResolver,
    PrincipalResolverError,
    SyncPrincipalResolver,
)


def resolve_sync(request: AuthRequest) -> Principal | None:
    principal = request.state.get("verified_proxy_principal")
    if request.state.get("verified_proxy_forbidden") is True:
        raise PrincipalResolverError(403)
    return principal if isinstance(principal, Principal) else None


async def resolve_async(request: AuthRequest) -> Principal | None:
    return resolve_sync(request)


class AsyncResolver:
    async def __call__(self, request: AuthRequest) -> Principal | None:
        return await resolve_async(request)


sync_resolver: SyncPrincipalResolver = resolve_sync
async_resolver: AsyncPrincipalResolver = resolve_async
resolver: PrincipalResolver = AsyncResolver()
config = BearerTokenAuth(validate_token=lambda token: None, resolve_principal=resolver)
BearerTokenAuth(validate_token=lambda token: None, resolve_principal=sync_resolver)
BearerTokenAuth(validate_token=lambda token: None, resolve_principal=async_resolver)

# Direct middleware users get exactly the same resolver API.
BearerTokenAuthMiddleware(
    app=None, validate_token=lambda token: None, resolve_principal=resolve_async
)

unauthorized = PrincipalResolverError(401)
forbidden = PrincipalResolverError(403)
