"""Stage 3a wire emission tests for the per-instance ``adcp_version`` pin.

Validates:

- Outbound request params get ``adcp_version`` injected from the
  client's per-instance pin (via ``ProtocolAdapter.envelope_enricher``).
- Caller-supplied values on the params dict win over the enricher.
- ``capabilities_response()`` emits ``supported_versions`` /
  ``build_version`` / top-level ``adcp_version`` when the server
  builder's pin is threaded through.
- ``ADCPServerBuilder``'s auto-generated capabilities handler passes
  the pin into ``capabilities_response()``.
"""

from __future__ import annotations

import asyncio

from adcp import ADCPClient
from adcp.server.builder import ADCPServerBuilder, adcp_server
from adcp.server.responses import capabilities_response
from adcp.types import AgentConfig, Protocol


def _agent_config() -> AgentConfig:
    return AgentConfig(
        id="test",
        agent_uri="https://test.example.com",
        protocol=Protocol.A2A,
    )


# ---------------------------------------------------------------------------
# envelope_enricher / outbound injection
# ---------------------------------------------------------------------------


def test_envelope_enricher_injects_pin() -> None:
    client = ADCPClient(_agent_config(), adcp_version="3.1")
    enriched = client.adapter._enrich_outgoing_params({"brief": "hi"})
    assert enriched == {"adcp_version": "3.1", "brief": "hi"}


def test_envelope_enricher_does_not_overwrite_caller_value() -> None:
    """Caller-supplied adcp_version on the params dict wins over the pin."""
    client = ADCPClient(_agent_config(), adcp_version="3.1")
    enriched = client.adapter._enrich_outgoing_params({"adcp_version": "3.0", "brief": "hi"})
    assert enriched == {"adcp_version": "3.0", "brief": "hi"}


def test_envelope_enricher_passes_through_non_dict() -> None:
    """Rare but possible: non-dict params (e.g. None or a list) pass through."""
    client = ADCPClient(_agent_config(), adcp_version="3.1")
    assert client.adapter._enrich_outgoing_params(None) is None
    assert client.adapter._enrich_outgoing_params([1, 2, 3]) == [1, 2, 3]


def test_envelope_enricher_uses_default_when_pin_omitted() -> None:
    """Default pin = packaged ADCP_VERSION."""
    from adcp import get_adcp_spec_version

    client = ADCPClient(_agent_config())
    enriched = client.adapter._enrich_outgoing_params({})
    assert enriched["adcp_version"] == get_adcp_spec_version()


# ---------------------------------------------------------------------------
# capabilities_response()
# ---------------------------------------------------------------------------


def test_capabilities_response_omits_new_fields_when_no_pin() -> None:
    """Backwards compatible: existing callers see no new fields."""
    resp = capabilities_response(["media_buy"])
    assert "adcp_version" not in resp
    assert "supported_versions" not in resp["adcp"]
    assert "build_version" not in resp["adcp"]
    assert resp["adcp"]["major_versions"] == [3]


def test_capabilities_response_emits_supported_versions_from_pin() -> None:
    """Single-pin server: supported_versions defaults to [pin]."""
    resp = capabilities_response(["media_buy"], adcp_version="3.1")
    assert resp["adcp_version"] == "3.1"
    assert resp["adcp"]["supported_versions"] == ["3.1"]
    # Legacy field still emitted for back-compat.
    assert resp["adcp"]["major_versions"] == [3]


def test_capabilities_response_explicit_supported_versions_override() -> None:
    """Multi-release server: caller passes explicit list."""
    resp = capabilities_response(
        ["media_buy"],
        adcp_version="3.1",
        supported_versions=["3.0", "3.1"],
    )
    assert resp["adcp_version"] == "3.1"
    assert resp["adcp"]["supported_versions"] == ["3.0", "3.1"]


def test_capabilities_response_emits_build_version() -> None:
    resp = capabilities_response(
        ["media_buy"],
        adcp_version="3.1",
        build_version="3.1.2",
    )
    assert resp["adcp"]["build_version"] == "3.1.2"


# ---------------------------------------------------------------------------
# ADCPServerBuilder auto-capabilities passes pin through
# ---------------------------------------------------------------------------


def test_server_builder_auto_capabilities_emits_pin() -> None:
    """The auto-generated get_adcp_capabilities handler threads the pin
    into capabilities_response()."""
    builder = adcp_server("my-seller", adcp_version="3.1")

    @builder.get_products
    async def _get_products(params, context=None):
        return {"products": []}

    handler = builder.build_handler()

    response = asyncio.run(handler.get_adcp_capabilities({}, None))
    assert response["adcp_version"] == "3.1"
    assert response["adcp"]["supported_versions"] == ["3.1"]


def test_server_builder_auto_capabilities_uses_default_pin() -> None:
    """No explicit pin → packaged ADCP_VERSION drives the response."""
    from adcp import get_adcp_spec_version

    builder = ADCPServerBuilder("my-seller")

    @builder.get_products
    async def _get_products(params, context=None):
        return {"products": []}

    handler = builder.build_handler()
    response = asyncio.run(handler.get_adcp_capabilities({}, None))
    assert response["adcp_version"] == get_adcp_spec_version()
