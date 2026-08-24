"""Adopter-facing type checks for the buyer OAuth helpers."""

from adcp import (
    InMemoryPendingOAuthFlowStore,
    OAuthAuthorizationRequest,
    OAuthIssuerBinding,
    OAuthTokenSet,
    complete_oauth_authorization,
    discover_oauth_metadata,
    start_oauth_authorization,
)


async def authorize() -> tuple[OAuthAuthorizationRequest, OAuthTokenSet]:
    store = InMemoryPendingOAuthFlowStore()
    metadata = await discover_oauth_metadata("https://login.example/tenant")
    request = await start_oauth_authorization(
        metadata,
        client_id="buyer-public",
        redirect_uri="https://buyer.example/oauth/callback",
        store=store,
        issuer_binding=OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS,
    )
    tokens = await complete_oauth_authorization(
        code="code",
        callback_state=request.state,
        expected_state=request.state,
        callback_issuer=metadata.issuer,
        store=store,
    )
    return request, tokens
