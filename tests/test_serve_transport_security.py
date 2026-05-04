"""``create_mcp_server`` plumbs DNS-rebinding-protection knobs through
to FastMCP's :class:`TransportSecuritySettings`.

FastMCP's default ``allowed_hosts`` accepts only loopback patterns
(``127.0.0.1:*``, ``localhost:*``, ``[::1]:*``). Adopters serving
multi-tenant subdomain hosts (``acme.example.com``,
``acme.localhost``) need to either extend the list or disable the
MCP-layer check entirely (when a tenant-aware ASGI middleware
already validates the Host header). Without these kwargs the
transport returns ``421 Misdirected Request`` and MCP discovery
fails — see PR #443 / storyboard CI ``v3_reference_seller``
job for the original symptom.

Pin the plumbing here so a future refactor doesn't silently drop
it.
"""

from __future__ import annotations

from typing import Any

from adcp.server.base import ADCPHandler
from adcp.server.serve import _synthesize_allowed_hosts, create_mcp_server
from adcp.server.tenant_router import (
    InMemorySubdomainTenantRouter,
    SubdomainTenantMiddleware,
    Tenant,
)


class _StubHandler(ADCPHandler[Any]):
    """Empty handler — only the FastMCP settings are under test."""


def test_default_transport_security_keeps_loopback_allowlist() -> None:
    """No kwargs → FastMCP defaults intact (loopback-only host list)."""
    mcp = create_mcp_server(_StubHandler(), name="t")
    ts = mcp.settings.transport_security
    assert ts is not None
    assert ts.enable_dns_rebinding_protection is True
    # FastMCP's loopback defaults — match exactly so a regression in
    # this list (e.g. an upstream rename) breaks here, not at runtime.
    assert "localhost:*" in ts.allowed_hosts
    assert "127.0.0.1:*" in ts.allowed_hosts


def test_allowed_hosts_extends_default_list() -> None:
    """``allowed_hosts=[...]`` extends rather than replaces the
    FastMCP default — loopback probes still work alongside the
    adopter's tenant hosts.
    """
    mcp = create_mcp_server(
        _StubHandler(),
        name="t",
        allowed_hosts=["acme.localhost:*", "beta.localhost:*"],
    )
    ts = mcp.settings.transport_security
    assert ts is not None
    assert "localhost:*" in ts.allowed_hosts  # default preserved
    assert "acme.localhost:*" in ts.allowed_hosts
    assert "beta.localhost:*" in ts.allowed_hosts


def test_allowed_origins_extends_default_list() -> None:
    """Symmetric to ``allowed_hosts`` — origins extend the default."""
    mcp = create_mcp_server(
        _StubHandler(),
        name="t",
        allowed_origins=["http://acme.localhost:*"],
    )
    ts = mcp.settings.transport_security
    assert ts is not None
    assert "http://localhost:*" in ts.allowed_origins  # default preserved
    assert "http://acme.localhost:*" in ts.allowed_origins


# ----- _synthesize_allowed_hosts -----------------------------------------


def test_synthesize_produces_bare_and_port_wildcard() -> None:
    """Each registered host gets both the bare form and ``:*`` so the
    FastMCP allowlist is port-agnostic, matching the router's
    ``_normalize_host`` port-stripping at lookup time."""
    router = InMemorySubdomainTenantRouter(
        tenants={
            "acme.localhost": Tenant(id="acme", display_name="Acme"),
            "beta.localhost": Tenant(id="beta", display_name="Beta"),
        }
    )
    result = _synthesize_allowed_hosts(
        [(SubdomainTenantMiddleware, {"router": router})],
        allowed_hosts=None,
    )
    assert result is not None
    assert "acme.localhost" in result
    assert "acme.localhost:*" in result
    assert "beta.localhost" in result
    assert "beta.localhost:*" in result


def test_synthesize_merges_with_explicit_allowed_hosts() -> None:
    """Explicit ``allowed_hosts`` are preserved; synthesized entries are
    appended without duplicates."""
    router = InMemorySubdomainTenantRouter(
        tenants={"acme.localhost": Tenant(id="acme", display_name="Acme")}
    )
    result = _synthesize_allowed_hosts(
        [(SubdomainTenantMiddleware, {"router": router})],
        allowed_hosts=["extra.example.com"],
    )
    assert result is not None
    assert "extra.example.com" in result
    assert "acme.localhost" in result
    assert "acme.localhost:*" in result
    # No duplicate bare host
    assert list(result).count("acme.localhost") == 1


def test_synthesize_noop_when_no_subdomain_middleware() -> None:
    """No SubdomainTenantMiddleware in the list → returned value is
    unchanged (None stays None, explicit list stays unchanged)."""
    assert _synthesize_allowed_hosts(None, None) is None
    assert _synthesize_allowed_hosts([], None) is None
    explicit: list[str] = ["host.example.com"]
    result = _synthesize_allowed_hosts([], explicit)
    assert result is explicit


def test_synthesize_noop_for_router_without_hosts_method() -> None:
    """Custom routers that don't expose ``hosts()`` are skipped —
    no AttributeError, no silent breakage."""

    class _CustomRouter:
        async def resolve(self, host: str) -> None:
            return None

    result = _synthesize_allowed_hosts(
        [(SubdomainTenantMiddleware, {"router": _CustomRouter()})],
        allowed_hosts=None,
    )
    assert result is None


def test_synthesize_skips_callable_factory_entries() -> None:
    """Callable-factory middleware entries (not tuples) are ignored."""
    router = InMemorySubdomainTenantRouter(tenants={})

    def _factory(app: Any) -> Any:
        return app

    result = _synthesize_allowed_hosts([_factory], allowed_hosts=None)
    assert result is None


def test_enable_dns_rebinding_protection_false_disables_check() -> None:
    """Adopters with their own tenant-aware host validation pass
    ``enable_dns_rebinding_protection=False`` so the MCP-layer check
    doesn't duplicate the upstream validation.
    """
    mcp = create_mcp_server(
        _StubHandler(),
        name="t",
        enable_dns_rebinding_protection=False,
    )
    ts = mcp.settings.transport_security
    assert ts is not None
    assert ts.enable_dns_rebinding_protection is False
