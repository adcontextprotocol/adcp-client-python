"""Opt-in WBA signing follows advertised profiles and never silently downgrades."""

from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from adcp import ADCPClient, AgentConfig
from adcp.signing import (
    SigningConfig,
    install_signing_event_hook,
    public_directory_key,
    signing_operation,
)
from adcp.types.experimental_identity import RequestSigning


@pytest.mark.parametrize("style", ["client", "httpx"])
@pytest.mark.parametrize(
    "profiles",
    [
        None,
        ["adcp/request-signing/v1"],
        ["web-bot-auth"],
        ["adcp/request-signing/v1", "web-bot-auth"],
    ],
)
async def test_wba_signer_requires_advertised_profile_before_network(style, profiles):
    private = ed25519.Ed25519PrivateKey.generate()
    signing = SigningConfig(
        private_key=private,
        key_id=public_directory_key(private)["kid"],
        agent_origin="https://buyer.com",
    )
    capability = RequestSigning(
        supported=True, required_for=["create_media_buy"], profiles=profiles
    )
    sent = []

    async def send(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(send), base_url="https://seller.com"
    ) as client:
        if style == "client":
            sdk = ADCPClient(
                AgentConfig(id="seller", agent_uri="https://seller.com", protocol="a2a"),
                signing=signing,
            )
            sdk._capabilities = SimpleNamespace(request_signing=capability)
            client.event_hooks["request"].append(sdk._sign_outgoing_request)
        else:
            install_signing_event_hook(client, signing=signing, seller_capability=capability)
        with signing_operation("create_media_buy"):
            if profiles is None or "web-bot-auth" not in profiles:
                with pytest.raises(ValueError, match="does not advertise"):
                    await client.post("/mcp", json={"budget": 100})
                assert not sent
            else:
                assert (await client.post("/mcp", json={"budget": 100})).status_code == 200
                assert len(sent) == 1
                assert 'tag="web-bot-auth"' in sent[0].headers["Signature-Input"]
                assert sent[0].headers["Signature-Agent"] == 'sig1="https://buyer.com"'
