"""Tests for the per-instance ``adcp_version`` constructor option (Stage 2).

Validates the per-instance pinning and wire-emission plumbing. These
tests assert that:

- Default resolution reads the packaged ``ADCP_VERSION`` file.
- Same-major pins are accepted only when they normalize to an exact
  advertised ``supported_versions`` entry.
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
    get_supported_adcp_versions,
    normalize_to_release_precision,
    parse_adcp_major_version,
    resolve_adcp_version,
)
from adcp.exceptions import ConfigurationError
from adcp.server.builder import ADCPServerBuilder, adcp_server
from adcp.types import AgentConfig, Protocol

_PACKAGED_ADCP_VERSION = normalize_to_release_precision(get_adcp_spec_version())

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


def test_resolve_default_returns_normalized_packaged_version() -> None:
    """Default pin is the packaged ADCP_VERSION, normalized to release-precision."""
    assert resolve_adcp_version(None) == normalize_to_release_precision(get_adcp_spec_version())


@pytest.mark.parametrize(
    "version,expected",
    [
        ("3.0", "3.0"),
        ("3.0.0", "3.0"),  # normalized — patch stripped
        ("3.0.1", "3.0"),  # normalized — patch stripped
        (_PACKAGED_ADCP_VERSION, _PACKAGED_ADCP_VERSION),
    ],
)
def test_resolve_same_major_normalized(version: str, expected: str) -> None:
    """Advertised same-major pins resolve to release-precision per the wire rule."""
    assert resolve_adcp_version(version) == expected


@pytest.mark.parametrize("version", ["3.1", "3.1-beta", "3.1.0-rc.1", "3.2"])
def test_resolve_unadvertised_same_major_rejected(version: str) -> None:
    if normalize_to_release_precision(version) in get_supported_adcp_versions():
        pytest.skip("Version is advertised by this SDK")
    with pytest.raises(ConfigurationError, match="Use one of those exact values"):
        resolve_adcp_version(version)


# ---------------------------------------------------------------------------
# normalize_to_release_precision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input,expected",
    [
        ("3.0", "3.0"),
        ("3.1", "3.1"),
        ("3.0.0", "3.0"),
        ("3.0.1", "3.0"),
        ("3.1-beta", "3.1-beta"),
        ("3.1.0-beta", "3.1-beta"),
        ("3.1.0-rc.1", "3.1-rc.1"),
        ("3.1.2-beta.5", "3.1-beta.5"),
        ("10.20.30", "10.20"),
        # Build metadata stripped (not part of contract).
        ("3.0.1+canary", "3.0"),
        ("3.0+exp.sha.5114f85", "3.0"),
        ("3.1.0-beta+sha.5", "3.1-beta"),
    ],
)
def test_normalize_strips_patch_keeps_prerelease(input: str, expected: str) -> None:
    assert normalize_to_release_precision(input) == expected


def test_normalize_rejects_garbage() -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError):
        normalize_to_release_precision("banana")


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


def test_supported_versions_include_packaged_spec_line() -> None:
    assert _PACKAGED_ADCP_VERSION in get_supported_adcp_versions()


def test_every_advertised_native_version_has_bundled_validators() -> None:
    """Never advertise a release whose schema validation fails open.

    Missing bundles make ``validate_request`` return the deliberately
    permissive ``variant='skipped'`` outcome used for custom tools.  That is
    safe only for versions the SDK does not claim to support: an advertised
    native release must have real request and response validators.
    """
    from adcp.validation import list_validator_keys

    for version in get_supported_adcp_versions():
        keys = list_validator_keys(version=version)
        assert any(key.endswith("::request") for key in keys), version
        assert any(key.endswith("::sync") for key in keys), version


# ---------------------------------------------------------------------------
# ADCPClient
# ---------------------------------------------------------------------------


def _agent_config() -> AgentConfig:
    return AgentConfig(
        id="test",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )


def test_adcp_client_default_uses_normalized_packaged_version() -> None:
    client = ADCPClient(_agent_config())
    assert client.get_adcp_version() == _PACKAGED_ADCP_VERSION


@pytest.mark.parametrize(
    "version,expected",
    [
        ("3.0", "3.0"),
        (_PACKAGED_ADCP_VERSION, _PACKAGED_ADCP_VERSION),
        ("3.0.0", "3.0"),  # patch input → release stored
        ("3.0.1", "3.0"),
    ],
)
def test_adcp_client_pin_normalized(version: str, expected: str) -> None:
    client = ADCPClient(_agent_config(), adcp_version=version)
    assert client.get_adcp_version() == expected


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
    assert multi.get_adcp_version() == _PACKAGED_ADCP_VERSION


def test_multi_agent_pin_forwards_to_per_agent() -> None:
    multi = ADCPMultiAgentClient(agents=[_agent_config()], adcp_version=_PACKAGED_ADCP_VERSION)
    assert multi.get_adcp_version() == _PACKAGED_ADCP_VERSION
    assert multi.agent("test").get_adcp_version() == _PACKAGED_ADCP_VERSION


def test_multi_agent_cross_major_rejected() -> None:
    with pytest.raises(ConfigurationError):
        ADCPMultiAgentClient(agents=[_agent_config()], adcp_version="4.0")


def _two_agents() -> list[AgentConfig]:
    return [
        AgentConfig(id="seller_a", agent_uri="https://a.example.com", protocol=Protocol.A2A),
        AgentConfig(id="seller_b", agent_uri="https://b.example.com", protocol=Protocol.A2A),
    ]


def test_multi_agent_per_agent_map_pins_each_agent_independently() -> None:
    """Per-agent dict form lets a holdco pin each seller separately."""
    multi = ADCPMultiAgentClient(
        agents=_two_agents(),
        adcp_version={"seller_a": "3.0", "seller_b": _PACKAGED_ADCP_VERSION},
    )
    assert multi.agent("seller_a").get_adcp_version() == "3.0"
    assert multi.agent("seller_b").get_adcp_version() == _PACKAGED_ADCP_VERSION


def test_multi_agent_per_agent_map_falls_back_to_default_for_missing_keys() -> None:
    multi = ADCPMultiAgentClient(
        agents=_two_agents(),
        adcp_version={"seller_a": _PACKAGED_ADCP_VERSION},
    )
    assert multi.agent("seller_a").get_adcp_version() == _PACKAGED_ADCP_VERSION
    # seller_b missing from map → SDK default.
    assert multi.agent("seller_b").get_adcp_version() == _PACKAGED_ADCP_VERSION


def test_multi_agent_get_version_raises_on_heterogeneous_pins() -> None:
    multi = ADCPMultiAgentClient(
        agents=_two_agents(),
        adcp_version={"seller_a": "3.0", "seller_b": _PACKAGED_ADCP_VERSION},
    )
    with pytest.raises(ValueError) as exc:
        multi.get_adcp_version()
    msg = str(exc.value)
    assert "heterogeneous" in msg
    assert "seller_a" in msg and "seller_b" in msg


def test_multi_agent_get_version_returns_uniform_when_map_agrees() -> None:
    """Dict form with all agents at the same pin still resolves uniformly."""
    multi = ADCPMultiAgentClient(
        agents=_two_agents(),
        adcp_version={
            "seller_a": _PACKAGED_ADCP_VERSION,
            "seller_b": _PACKAGED_ADCP_VERSION,
        },
    )
    assert multi.get_adcp_version() == _PACKAGED_ADCP_VERSION


def test_multi_agent_per_agent_map_cross_major_rejected() -> None:
    with pytest.raises(ConfigurationError):
        ADCPMultiAgentClient(
            agents=_two_agents(),
            adcp_version={"seller_a": "3.0", "seller_b": "4.0"},
        )


# ---------------------------------------------------------------------------
# ADCPServerBuilder + adcp_server() factory
# ---------------------------------------------------------------------------


def test_server_builder_default_uses_packaged_version() -> None:
    builder = ADCPServerBuilder("my-seller")
    assert builder.get_adcp_version() == _PACKAGED_ADCP_VERSION


@pytest.mark.parametrize("version", ["3.0", _PACKAGED_ADCP_VERSION])
def test_server_builder_explicit_pin_accepted(version: str) -> None:
    builder = ADCPServerBuilder("my-seller", adcp_version=version)
    assert builder.get_adcp_version() == version


def test_server_builder_cross_major_rejected() -> None:
    with pytest.raises(ConfigurationError):
        ADCPServerBuilder("my-seller", adcp_version="4.0")


def test_adcp_server_factory_passes_adcp_version_through() -> None:
    builder = adcp_server("my-seller", adcp_version=_PACKAGED_ADCP_VERSION)
    assert builder.get_adcp_version() == _PACKAGED_ADCP_VERSION


def test_adcp_server_factory_cross_major_rejected() -> None:
    with pytest.raises(ConfigurationError):
        adcp_server("my-seller", adcp_version="4.0")
