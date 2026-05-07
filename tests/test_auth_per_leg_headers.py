"""Per-leg header config + agent-card security envelope (issue #57).

Two upstream items from salesagent#57 land here:

1. ``BearerTokenAuth`` exposes per-leg ``mcp_*`` / ``a2a_*`` knobs so
   one config drives both legs without a translation shim. Defaults
   preserve the pre-#57 single-knob behavior — both legs default to
   ``Authorization`` + ``Bearer`` prefix. Adopters who want the
   AdCP-convention ``x-adcp-auth`` on MCP opt in via ``mcp_header_name``.
2. ``_build_agent_card`` publishes a matching security scheme +
   requirement when ``BearerTokenAuth`` is configured, so a2a-sdk's
   client auth interceptor attaches credentials instead of
   short-circuiting against an empty envelope. The scheme variant is
   chosen on ``bearer_prefix_required``: prefix-required publishes
   :class:`HTTPAuthSecurityScheme` (``scheme="bearer"``, id ``bearerAuth``);
   raw-token custom-header configs publish
   :class:`APIKeySecurityScheme` (id ``adcpAuth``).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from adcp.server import ADCPHandler
from adcp.server.auth import (
    A2ABearerAuthMiddleware,
    BearerTokenAuth,
    Principal,
    validator_from_token_map,
)


class _Handler(ADCPHandler):
    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}


def _validator() -> Any:
    return validator_from_token_map({"good-token": Principal(caller_identity="p", tenant_id="t")})


# ===========================================================================
# Resolved values
# ===========================================================================


class TestResolvedDefaults:
    """Back-compat: defaults match the pre-#57 single-knob behavior on
    both legs (``Authorization`` + ``Bearer`` prefix). Adopters who want
    a different carrier on one leg opt in via the per-leg knobs;
    adopters who want the same non-default carrier on both legs use the
    legacy single knob."""

    def test_mcp_default_is_authorization_with_prefix(self):
        """Back-compat: existing ``BearerTokenAuth(validate_token=...)``
        on MCP keeps reading ``Authorization: Bearer ...`` exactly as it
        did pre-#57."""
        cfg = BearerTokenAuth(validate_token=_validator())
        assert cfg.resolved_mcp_header_name() == "authorization"
        assert cfg.resolved_mcp_bearer_prefix_required() is True

    def test_a2a_default_is_authorization_with_prefix(self):
        cfg = BearerTokenAuth(validate_token=_validator())
        assert cfg.resolved_a2a_header_name() == "Authorization"
        assert cfg.resolved_a2a_bearer_prefix_required() is True

    def test_legacy_header_name_applies_to_both_legs(self):
        """Back-compat: pre-#57 adopters set ``header_name`` once and
        the same carrier wires both legs."""
        cfg = BearerTokenAuth(
            validate_token=_validator(),
            header_name="x-adcp-auth",
            bearer_prefix_required=False,
        )
        assert cfg.resolved_mcp_header_name() == "x-adcp-auth"
        assert cfg.resolved_a2a_header_name() == "x-adcp-auth"
        assert cfg.resolved_mcp_bearer_prefix_required() is False
        assert cfg.resolved_a2a_bearer_prefix_required() is False

    def test_per_leg_overrides_take_effect(self):
        """Opt-in per-leg config: AdCP-convention ``x-adcp-auth`` on
        MCP, RFC 6750 ``Authorization: Bearer ...`` on A2A."""
        cfg = BearerTokenAuth(
            validate_token=_validator(),
            mcp_header_name="x-adcp-auth",
            mcp_bearer_prefix_required=False,
            a2a_header_name="Authorization",
            a2a_bearer_prefix_required=True,
        )
        assert cfg.resolved_mcp_header_name() == "x-adcp-auth"
        assert cfg.resolved_a2a_header_name() == "Authorization"
        assert cfg.resolved_mcp_bearer_prefix_required() is False
        assert cfg.resolved_a2a_bearer_prefix_required() is True


class TestConflictingKnobsRejected:
    """Setting both legacy and per-leg knobs for the same axis is
    ambiguous — fail closed at construction so misconfigurations don't
    ship to production silently."""

    def test_legacy_and_mcp_header_conflict(self):
        with pytest.raises(ValueError, match="header_name"):
            BearerTokenAuth(
                validate_token=_validator(),
                header_name="authorization",
                mcp_header_name="x-adcp-auth",
            )

    def test_legacy_and_a2a_header_conflict(self):
        with pytest.raises(ValueError, match="header_name"):
            BearerTokenAuth(
                validate_token=_validator(),
                header_name="authorization",
                a2a_header_name="x-other",
            )

    def test_legacy_and_per_leg_prefix_conflict(self):
        with pytest.raises(ValueError, match="bearer_prefix_required"):
            BearerTokenAuth(
                validate_token=_validator(),
                bearer_prefix_required=True,
                a2a_bearer_prefix_required=False,
            )


class TestConstructionGuards:
    """``__post_init__`` rejects misconfigurations that would otherwise
    silently 401 every request or violate RFC 7235."""

    def test_empty_legacy_header_rejected(self):
        with pytest.raises(ValueError, match="header_name.*non-empty"):
            BearerTokenAuth(validate_token=_validator(), header_name="")

    def test_empty_mcp_header_rejected(self):
        with pytest.raises(ValueError, match="mcp_header_name.*non-empty"):
            BearerTokenAuth(validate_token=_validator(), mcp_header_name="   ")

    def test_authorization_without_bearer_prefix_rejected_legacy(self):
        """RFC 7235: ``Authorization`` carries ``<scheme> <credentials>``.
        Carrying a raw token in ``Authorization`` breaks RFC-compliant
        intermediaries and a2a-sdk's auth interceptor."""
        with pytest.raises(ValueError, match="RFC 7235"):
            BearerTokenAuth(
                validate_token=_validator(),
                header_name="Authorization",
                bearer_prefix_required=False,
            )

    def test_authorization_without_bearer_prefix_rejected_a2a(self):
        with pytest.raises(ValueError, match="RFC 7235"):
            BearerTokenAuth(
                validate_token=_validator(),
                a2a_header_name="authorization",
                a2a_bearer_prefix_required=False,
            )

    def test_authorization_with_bearer_prefix_accepted(self):
        """The legitimate combo (default) still constructs cleanly."""
        BearerTokenAuth(
            validate_token=_validator(),
            a2a_header_name="Authorization",
            a2a_bearer_prefix_required=True,
        )

    def test_custom_header_without_bearer_prefix_accepted(self):
        """The legitimate raw-token combo still constructs cleanly."""
        BearerTokenAuth(
            validate_token=_validator(),
            mcp_header_name="x-adcp-auth",
            mcp_bearer_prefix_required=False,
        )


# ===========================================================================
# A2A leg honors per-leg config at request time
# ===========================================================================


def _scope(path: str = "/", headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": list(headers or []),
    }


class TestA2AMiddlewareHonorsPerLegConfig:
    @pytest.mark.asyncio
    async def test_a2a_uses_a2a_header_name_when_set(self):
        """Per-leg ``a2a_header_name`` carries the credential on A2A
        even when the MCP leg keeps the AdCP-convention header."""
        cfg = BearerTokenAuth(
            validate_token=_validator(),
            a2a_header_name="x-fleet-auth",
            a2a_bearer_prefix_required=False,
        )
        inner_calls: list[dict] = []

        async def inner(scope: Any, _receive: Any, _send: Any) -> None:
            inner_calls.append(scope)

        mw = A2ABearerAuthMiddleware(inner, cfg)
        await mw(
            _scope(headers=[(b"x-fleet-auth", b"good-token")]),
            lambda: None,
            lambda _: None,
        )
        assert len(inner_calls) == 1
        assert inner_calls[0]["user"].display_name == "p"

    @pytest.mark.asyncio
    async def test_a2a_default_requires_bearer_prefix(self):
        """Default A2A behavior: raw token without ``Bearer `` prefix
        is rejected (RFC 6750)."""
        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        async def _unused_inner(*_args: Any) -> None:  # never reached on 401
            pass

        cfg = BearerTokenAuth(validate_token=_validator())
        mw = A2ABearerAuthMiddleware(_unused_inner, cfg)
        await mw(
            _scope(headers=[(b"authorization", b"good-token")]),  # no Bearer prefix
            lambda: None,
            send,
        )
        assert sent[0]["status"] == 401

    @pytest.mark.asyncio
    async def test_a2a_header_name_case_insensitive_match(self):
        """Adopters who set ``a2a_header_name="X-Adcp-Auth"`` (mixed
        case) still match wire headers in the lowercased form ASGI
        normalizes to. The middleware lowercases the configured name at
        construction so the case used in config is irrelevant."""
        cfg = BearerTokenAuth(
            validate_token=_validator(),
            a2a_header_name="X-Custom-Auth",
            a2a_bearer_prefix_required=False,
        )
        inner_calls: list[dict] = []

        async def inner(scope: Any, _receive: Any, _send: Any) -> None:
            inner_calls.append(scope)

        mw = A2ABearerAuthMiddleware(inner, cfg)
        await mw(
            _scope(headers=[(b"x-custom-auth", b"good-token")]),
            lambda: None,
            lambda _: None,
        )
        assert len(inner_calls) == 1


# ===========================================================================
# MCP leg honors per-leg config end-to-end
# ===========================================================================


@pytest.mark.asyncio
async def test_mcp_per_leg_header_routes_through_middleware() -> None:
    """End-to-end: ``mcp_header_name="x-adcp-auth"`` +
    ``mcp_bearer_prefix_required=False`` thread the resolved values
    into the MCP-side ``BearerTokenAuthMiddleware``. A request carrying
    the raw token in ``x-adcp-auth`` passes; a request carrying it in
    ``Authorization`` (the legacy default carrier) is rejected.
    """
    from adcp.server import create_mcp_server
    from adcp.server.serve import _wrap_mcp_with_auth

    cfg = BearerTokenAuth(
        validate_token=_validator(),
        mcp_header_name="x-adcp-auth",
        mcp_bearer_prefix_required=False,
    )
    mcp = create_mcp_server(_Handler(), name="t", validation=None)
    mcp_app = mcp.streamable_http_app()
    app = _wrap_mcp_with_auth(mcp_app, cfg)
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/call",
        "params": {"name": "get_products", "arguments": {}},
    }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=True,
        ) as client:
            resp_x_adcp = await client.post(
                "/mcp",
                json=body,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "x-adcp-auth": "good-token",
                },
            )
            resp_authz = await client.post(
                "/mcp",
                json=body,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "Authorization": "Bearer good-token",
                },
            )
    # x-adcp-auth carrier accepted — anything except 401 means auth
    # passed through to the inner handler.
    assert resp_x_adcp.status_code != 401, resp_x_adcp.text
    # Authorization carrier rejected — the per-leg config moved the
    # carrier off the legacy default.
    assert resp_authz.status_code == 401


# ===========================================================================
# Agent card auto-publishes bearerAuth scheme
# ===========================================================================


class TestAgentCardSecurityEnvelope:
    """Issue #57 #1 — when auth is configured, the agent card must
    publish a matching ``bearerAuth`` security scheme + requirement
    so a2a-sdk's client auth interceptor attaches credentials."""

    def test_no_auth_card_has_empty_security_envelope(self):
        from adcp.server.a2a_server import _build_agent_card

        card = _build_agent_card(_Handler(), name="test", port=3001, advertise_all=True)
        assert dict(card.security_schemes) == {}
        assert list(card.security_requirements) == []

    def test_default_auth_publishes_bearer_http_scheme(self):
        """Default A2A leg (Authorization + bearer prefix) publishes
        an HTTPAuthSecurityScheme with ``scheme="bearer"``."""
        from adcp.server.a2a_server import _build_agent_card

        cfg = BearerTokenAuth(validate_token=_validator())
        card = _build_agent_card(_Handler(), name="test", port=3001, advertise_all=True, auth=cfg)
        assert "bearerAuth" in card.security_schemes
        scheme = card.security_schemes["bearerAuth"]
        assert scheme.http_auth_security_scheme.scheme == "bearer"
        assert len(card.security_requirements) == 1
        assert "bearerAuth" in card.security_requirements[0].schemes

    def test_custom_header_publishes_api_key_scheme(self):
        """Custom A2A header without bearer prefix publishes an
        APIKeySecurityScheme under id ``adcpAuth`` — that's the
        OpenAPI/A2A way to describe a custom-header credential. The
        scheme id is distinct from ``bearerAuth`` so buyers reading the
        card can tell HTTP-bearer from raw-token at a glance."""
        from adcp.server.a2a_server import _build_agent_card

        cfg = BearerTokenAuth(
            validate_token=_validator(),
            a2a_header_name="x-adcp-auth",
            a2a_bearer_prefix_required=False,
        )
        card = _build_agent_card(_Handler(), name="test", port=3001, advertise_all=True, auth=cfg)
        assert "adcpAuth" in card.security_schemes
        assert "bearerAuth" not in card.security_schemes
        scheme = card.security_schemes["adcpAuth"]
        assert scheme.api_key_security_scheme.location == "header"
        assert scheme.api_key_security_scheme.name == "x-adcp-auth"
        assert "adcpAuth" in card.security_requirements[0].schemes

    def test_legacy_header_name_authorization_publishes_bearer_scheme(self):
        """Adopters using the legacy single-knob with the standard
        ``Authorization`` header still get the bearer HTTP scheme on
        their card."""
        from adcp.server.a2a_server import _build_agent_card

        cfg = BearerTokenAuth(
            validate_token=_validator(),
            header_name="Authorization",
            bearer_prefix_required=True,
        )
        card = _build_agent_card(_Handler(), name="test", port=3001, advertise_all=True, auth=cfg)
        scheme = card.security_schemes["bearerAuth"]
        assert scheme.http_auth_security_scheme.scheme == "bearer"


@pytest.mark.asyncio
async def test_agent_card_route_returns_security_envelope() -> None:
    """End-to-end: the published ``/.well-known/agent-card.json`` MUST
    serialize the ``bearerAuth`` scheme so a2a-sdk-based clients can
    parse it from the wire."""
    from adcp.server.a2a_server import create_a2a_server

    cfg = BearerTokenAuth(validate_token=_validator())
    inner = create_a2a_server(_Handler(), name="t", validation=None, auth=cfg)
    async with LifespanManager(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=inner), base_url="http://test"
        ) as client:
            response = await client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    body = response.json()
    schemes = body.get("securitySchemes") or body.get("security_schemes")
    assert schemes is not None and "bearerAuth" in schemes
    requirements = body.get("security") or body.get("security_requirements") or []
    assert requirements, "security_requirements missing from card"
