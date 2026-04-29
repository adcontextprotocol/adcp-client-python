"""Tests for the per-instance ``adcp_version`` constructor option (Stage 2).

Validates the plumbing only — Stage 3 (per-instance schema/validator
selection, wire emission) is deferred. These tests assert that:

- Default resolution reads the packaged ``ADCP_VERSION`` file.
- Same-major release- and patch-precision pins are accepted as-is.
- Cross-major pins raise ``ConfigurationError`` at construction.
- Unparseable strings raise ``ConfigurationError``.
- All four constructor surfaces (``ADCPClient``,
  ``ADCPMultiAgentClient``, ``ADCPServerBuilder``, ``adcp_server``)
  honor the option and expose ``get_adcp_version()``.
"""

from __future__ import annotations

import pytest

from adcp import ADCPClient, ADCPMultiAgentClient, get_adcp_spec_version
from adcp._version import (
    ADCP_MAJOR_VERSION,
    COMPATIBLE_ADCP_VERSIONS,
    parse_adcp_major_version,
    resolve_adcp_version,
)
from adcp.exceptions import ConfigurationError
from adcp.server.builder import ADCPServerBuilder, adcp_server
from adcp.types import AgentConfig, Protocol

# ---------------------------------------------------------------------------
# parse_adcp_major_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,expected_major",
    [
        ("3.0", 3),
        ("3.1", 3),
        ("3.0.0", 3),
        ("3.0.1", 3),
        ("3.1-beta", 3),
        ("3.1.0-rc.1", 3),
        ("4.0", 4),
        ("10.20", 10),
    ],
)
def test_parse_adcp_major_version_extracts_major(version: str, expected_major: int) -> None:
    assert parse_adcp_major_version(version) == expected_major


@pytest.mark.parametrize(
    "bad_version",
    [
        "banana",
        "",
        "3",  # bare major, no release component
        "3.x",
        "v3.0",
        "3.0.0.0",
    ],
)
def test_parse_adcp_major_version_rejects_garbage(bad_version: str) -> None:
    with pytest.raises(ValueError):
        parse_adcp_major_version(bad_version)


# ---------------------------------------------------------------------------
# resolve_adcp_version
# ---------------------------------------------------------------------------


def test_resolve_default_returns_packaged_version() -> None:
    assert resolve_adcp_version(None) == get_adcp_spec_version()


@pytest.mark.parametrize("version", ["3.0", "3.1", "3.0.0", "3.0.1", "3.1-beta"])
def test_resolve_same_major_accepted(version: str) -> None:
    assert resolve_adcp_version(version) == version


@pytest.mark.parametrize("version", ["4.0", "2.0", "5.1", "1.0.0"])
def test_resolve_cross_major_rejected(version: str) -> None:
    with pytest.raises(ConfigurationError) as exc:
        resolve_adcp_version(version)
    assert "cross-major" in str(exc.value).lower() or "major" in str(exc.value).lower()


@pytest.mark.parametrize("bad", ["banana", "", "3", "v3.0"])
def test_resolve_unparseable_rejected(bad: str) -> None:
    with pytest.raises(ConfigurationError):
        resolve_adcp_version(bad)


def test_compatible_versions_constant_matches_major() -> None:
    """Every entry in COMPATIBLE_ADCP_VERSIONS must agree on major."""
    for v in COMPATIBLE_ADCP_VERSIONS:
        assert parse_adcp_major_version(v) == ADCP_MAJOR_VERSION


# ---------------------------------------------------------------------------
# ADCPClient
# ---------------------------------------------------------------------------


def _agent_config() -> AgentConfig:
    return AgentConfig(
        id="test",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )


def test_adcp_client_default_uses_packaged_version() -> None:
    client = ADCPClient(_agent_config())
    assert client.get_adcp_version() == get_adcp_spec_version()


@pytest.mark.parametrize("version", ["3.0", "3.1", "3.1-beta"])
def test_adcp_client_explicit_pin_accepted(version: str) -> None:
    client = ADCPClient(_agent_config(), adcp_version=version)
    assert client.get_adcp_version() == version


def test_adcp_client_cross_major_rejected() -> None:
    with pytest.raises(ConfigurationError):
        ADCPClient(_agent_config(), adcp_version="4.0")


def test_adcp_client_unparseable_rejected() -> None:
    with pytest.raises(ConfigurationError):
        ADCPClient(_agent_config(), adcp_version="banana")


# ---------------------------------------------------------------------------
# ADCPMultiAgentClient
# ---------------------------------------------------------------------------


def test_multi_agent_default_uses_packaged_version() -> None:
    multi = ADCPMultiAgentClient(agents=[_agent_config()])
    assert multi.get_adcp_version() == get_adcp_spec_version()


def test_multi_agent_pin_forwards_to_per_agent() -> None:
    multi = ADCPMultiAgentClient(agents=[_agent_config()], adcp_version="3.1")
    assert multi.get_adcp_version() == "3.1"
    assert multi.agent("test").get_adcp_version() == "3.1"


def test_multi_agent_cross_major_rejected() -> None:
    with pytest.raises(ConfigurationError):
        ADCPMultiAgentClient(agents=[_agent_config()], adcp_version="4.0")


# ---------------------------------------------------------------------------
# ADCPServerBuilder + adcp_server() factory
# ---------------------------------------------------------------------------


def test_server_builder_default_uses_packaged_version() -> None:
    builder = ADCPServerBuilder("my-seller")
    assert builder.get_adcp_version() == get_adcp_spec_version()


@pytest.mark.parametrize("version", ["3.0", "3.1"])
def test_server_builder_explicit_pin_accepted(version: str) -> None:
    builder = ADCPServerBuilder("my-seller", adcp_version=version)
    assert builder.get_adcp_version() == version


def test_server_builder_cross_major_rejected() -> None:
    with pytest.raises(ConfigurationError):
        ADCPServerBuilder("my-seller", adcp_version="4.0")


def test_adcp_server_factory_passes_adcp_version_through() -> None:
    builder = adcp_server("my-seller", adcp_version="3.1")
    assert builder.get_adcp_version() == "3.1"


def test_adcp_server_factory_cross_major_rejected() -> None:
    with pytest.raises(ConfigurationError):
        adcp_server("my-seller", adcp_version="4.0")
