"""One Host/Origin policy for protocol, discovery and operational HTTP paths."""

from __future__ import annotations

from collections.abc import Sequence

from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send


class HTTPTransportSecuritySettings(TransportSecuritySettings):
    """Retain explicit Origin enforcement when Host validation is disabled."""

    explicit_origins: bool = False


def transport_security_settings(
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
    enable_dns_rebinding_protection: bool | None = None,
) -> HTTPTransportSecuritySettings:
    # Preserve the SDK's additive loopback defaults and bare-host expansion.
    from adcp.server.serve import _expand_allowed_hosts

    return HTTPTransportSecuritySettings(
        enable_dns_rebinding_protection=(
            True if enable_dns_rebinding_protection is None else enable_dns_rebinding_protection
        ),
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            *_expand_allowed_hosts(allowed_hosts or ()),
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            *(allowed_origins or ()),
        ],
        explicit_origins=allowed_origins is not None,
    )


class HostOriginMiddleware:
    """Validate every HTTP Host and any supplied Origin before routing/auth.

    Missing Origin is valid for native clients, including AgentCard discovery.
    Matching follows MCP's exact-value and trailing ``:*`` port semantics; a
    bare configured Host also accepts any port. Domain wildcards are not globbed.
    Inner instances skip a policy already checked by the assembled outer app.
    MCP's own Host/Origin check is disabled, retaining its content-type check.
    """

    def __init__(self, app: ASGIApp, *, settings: TransportSecuritySettings | None) -> None:
        self.app = app
        self.settings = settings or TransportSecuritySettings(enable_dns_rebinding_protection=False)
        self.validator = TransportSecurityMiddleware(self.settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        check_host = self.settings.enable_dns_rebinding_protection
        check_origin = check_host or getattr(
            self.settings, "explicit_origins", bool(self.settings.allowed_origins)
        )
        policy = (
            check_host,
            check_origin,
            tuple(self.settings.allowed_hosts),
            tuple(self.settings.allowed_origins),
        )
        if scope.get("adcp.http_policy") != policy:
            request = Request(scope)
            # Reject repeated headers rather than allowing intermediaries and
            # protocol handlers to choose different effective values.
            hosts = request.headers.getlist("host")
            origins = request.headers.getlist("origin")
            if check_host and (len(hosts) != 1 or not self.validator._validate_host(hosts[0])):
                await Response("Invalid Host header", status_code=421)(scope, receive, send)
                return
            if check_origin and (
                len(origins) > 1
                or not self.validator._validate_origin(origins[0] if origins else None)
            ):
                await Response("Invalid Origin header", status_code=403)(scope, receive, send)
                return
            scope = dict(scope)
            scope["adcp.http_policy"] = policy
        await self.app(scope, receive, send)
