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
from adcp.server.serve import create_mcp_server


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


# ---- Bare-host → ``host:*`` synthesis (issue #518) ----


def test_bare_host_synthesizes_port_wildcard_sibling() -> None:
    """``allowed_hosts=['acme.localhost']`` registers both
    ``acme.localhost`` and ``acme.localhost:*`` so requests on either
    bare or port-suffixed Host headers pass the transport check.

    Mirrors the port-stripping :class:`InMemorySubdomainTenantRouter`
    does at lookup time — registering once should cover both surfaces.
    """
    mcp = create_mcp_server(
        _StubHandler(),
        name="t",
        allowed_hosts=["acme.localhost"],
    )
    ts = mcp.settings.transport_security
    assert ts is not None
    assert "acme.localhost" in ts.allowed_hosts
    assert "acme.localhost:*" in ts.allowed_hosts


def test_explicit_port_wildcard_passes_through_unchanged() -> None:
    """When the adopter already passes ``acme.localhost:*``, no
    further synthesis happens — the bare ``acme.localhost`` is NOT
    auto-added because the input has an explicit ``:`` separator
    (the adopter signaled they're managing the form themselves)."""
    mcp = create_mcp_server(
        _StubHandler(),
        name="t",
        allowed_hosts=["acme.localhost:*"],
    )
    ts = mcp.settings.transport_security
    assert ts is not None
    assert "acme.localhost:*" in ts.allowed_hosts
    # Adopter passed the port-wildcard form; we don't second-guess.
    assert "acme.localhost" not in ts.allowed_hosts


def test_explicit_port_passes_through_unchanged() -> None:
    """A specific port (``acme.localhost:3001``) is left alone — the
    ``:`` discriminator covers any port-bearing form."""
    mcp = create_mcp_server(
        _StubHandler(),
        name="t",
        allowed_hosts=["acme.localhost:3001"],
    )
    ts = mcp.settings.transport_security
    assert ts is not None
    assert "acme.localhost:3001" in ts.allowed_hosts
    assert "acme.localhost" not in ts.allowed_hosts


def test_mixed_bare_and_explicit_hosts_each_get_correct_treatment() -> None:
    """Bare hosts get the wildcard sibling; explicit-port hosts pass
    through. A list with both forms produces the right combined set."""
    mcp = create_mcp_server(
        _StubHandler(),
        name="t",
        allowed_hosts=["acme.localhost", "beta.example.com:8443"],
    )
    ts = mcp.settings.transport_security
    assert ts is not None
    # Bare host gets both forms.
    assert "acme.localhost" in ts.allowed_hosts
    assert "acme.localhost:*" in ts.allowed_hosts
    # Explicit-port host stays as-is, no bare-host synthesis.
    assert "beta.example.com:8443" in ts.allowed_hosts
    assert "beta.example.com" not in ts.allowed_hosts


def test_idempotent_when_both_forms_passed() -> None:
    """Adopter who already passes both ``acme.localhost`` and
    ``acme.localhost:*`` doesn't end up with duplicates after
    expansion."""
    mcp = create_mcp_server(
        _StubHandler(),
        name="t",
        allowed_hosts=["acme.localhost", "acme.localhost:*"],
    )
    ts = mcp.settings.transport_security
    assert ts is not None
    bare_count = sum(1 for h in ts.allowed_hosts if h == "acme.localhost")
    wildcard_count = sum(1 for h in ts.allowed_hosts if h == "acme.localhost:*")
    assert bare_count == 1
    assert wildcard_count == 1
