"""Tests for transport hooks (URL rewrite before SSRF).

The security boundary: hooks run before SSRF, but SSRF is authoritative
on the rewritten URL. ``DockerLocalhostRewrite`` requires
``allow_private_destinations=True`` at sender construction — the test
suite enforces this contract.
"""

from __future__ import annotations

import pytest

from adcp.signing import SSRFValidationError
from adcp.webhook_sender import WebhookSender
from adcp.webhook_transport_hooks import (
    DockerLocalhostRewrite,
    apply_hooks,
)

# ---------- DockerLocalhostRewrite (unit) ----------


def test_rewrite_localhost_to_host_docker_internal() -> None:
    hook = DockerLocalhostRewrite()
    assert (
        hook.rewrite_url("http://localhost:8080/webhook")
        == "http://host.docker.internal:8080/webhook"
    )


def test_rewrite_127_0_0_1_to_host_docker_internal() -> None:
    hook = DockerLocalhostRewrite()
    assert (
        hook.rewrite_url("https://127.0.0.1:9000/path?x=1")
        == "https://host.docker.internal:9000/path?x=1"
    )


def test_rewrite_ipv6_loopback_to_host_docker_internal() -> None:
    hook = DockerLocalhostRewrite()
    assert (
        hook.rewrite_url("http://[::1]:8080/webhook") == "http://host.docker.internal:8080/webhook"
    )


def test_rewrite_passthrough_for_external_host() -> None:
    """Public hosts must pass through untouched — the hook is a localhost
    -only rewrite. Returning ``None`` signals no change."""
    hook = DockerLocalhostRewrite()
    assert hook.rewrite_url("https://buyer.example.com/webhook") is None


def test_rewrite_to_custom_target_for_linux_bridge() -> None:
    """Linux containers without ``--add-host=host.docker.internal:host-gateway``
    typically use the bridge gateway IP directly."""
    hook = DockerLocalhostRewrite(rewrite_to="172.17.0.1")
    assert hook.rewrite_url("http://localhost/x") == "http://172.17.0.1/x"


def test_rewrite_preserves_query_and_fragment() -> None:
    hook = DockerLocalhostRewrite()
    assert (
        hook.rewrite_url("http://localhost:8080/path?a=1&b=2#frag")
        == "http://host.docker.internal:8080/path?a=1&b=2#frag"
    )


# ---------- apply_hooks (framework) ----------


def test_apply_hooks_no_hooks_passthrough() -> None:
    assert apply_hooks("http://localhost/x", ()) == "http://localhost/x"


def test_apply_hooks_chains_in_order() -> None:
    """Multiple hooks pipeline — output of one feeds the next."""

    class _AppendQuery:
        def rewrite_url(self, url: str) -> str | None:
            return None  # don't change anything; test that ``None`` is no-op

    out = apply_hooks("http://localhost/x", (DockerLocalhostRewrite(), _AppendQuery()))
    assert out == "http://host.docker.internal/x"


def test_apply_hooks_rejects_scheme_change() -> None:
    """Hooks may rewrite hostname only — scheme changes widen the hook's
    authority and are an explicit error rather than a silent acceptance."""

    class _SchemeChange:
        def rewrite_url(self, url: str) -> str | None:
            return url.replace("http://", "https://")

    with pytest.raises(ValueError, match="scheme"):
        apply_hooks("http://localhost/x", (_SchemeChange(),))


def test_apply_hooks_rejects_port_change() -> None:
    class _PortChange:
        def rewrite_url(self, url: str) -> str | None:
            return "http://localhost:9999/x"

    with pytest.raises(ValueError, match="port"):
        apply_hooks("http://localhost:8080/x", (_PortChange(),))


# ---------- DockerLocalhostRewrite + WebhookSender wiring ----------


def test_docker_rewrite_requires_allow_private_destinations() -> None:
    """The construction-time guard the security review demanded:
    DockerLocalhostRewrite without allow_private_destinations is a
    misconfiguration, not a no-op."""
    with pytest.raises(ValueError, match="allow_private_destinations=True"):
        WebhookSender.from_bearer_token(
            "tok",
            transport_hooks=(DockerLocalhostRewrite(),),
            # allow_private_destinations defaults to False
        )


def test_docker_rewrite_accepted_with_allow_private_destinations() -> None:
    """The opt-in path — operator explicitly accepts private destinations."""
    sender = WebhookSender.from_bearer_token(
        "tok",
        transport_hooks=(DockerLocalhostRewrite(),),
        allow_private_destinations=True,
    )
    # No exception means the validation accepted the config.
    assert isinstance(sender, WebhookSender)


def test_docker_rewrite_construction_check_applies_to_jwk_path_too() -> None:
    """The validation runs on every constructor — JWK path included."""
    import copy
    import json
    from pathlib import Path

    vectors_dir = Path(__file__).parent.parent / "vectors" / "request-signing"
    keys = json.loads((vectors_dir / "keys.json").read_text())["keys"]
    request_ed25519 = next(k for k in keys if k["kid"] == "test-ed25519-2026")
    webhook_jwk = {
        **copy.deepcopy(request_ed25519),
        "kid": "test-webhook-ed25519-2026",
        "adcp_use": "webhook-signing",
    }
    private_jwk = {**webhook_jwk, "d": webhook_jwk["_private_d_for_test_only"]}

    with pytest.raises(ValueError, match="allow_private_destinations=True"):
        WebhookSender.from_jwk(
            private_jwk,
            transport_hooks=(DockerLocalhostRewrite(),),
        )


# ---------- SSRF stays authoritative ----------


@pytest.mark.asyncio
async def test_hook_cannot_punch_through_ssrf_when_private_disallowed() -> None:
    """Even if a custom hook rewrites a public hostname to a private IP,
    SSRF rejects the rewritten URL when ``allow_private_destinations``
    is False. This is the core invariant the security reviewer demanded."""

    class _MaliciousHook:
        def rewrite_url(self, url: str) -> str | None:
            return url.replace("buyer.example.com", "127.0.0.1")

    sender = WebhookSender.from_bearer_token(
        "tok",
        transport_hooks=(_MaliciousHook(),),
        # NB: no allow_private_destinations — _MaliciousHook is custom
        # and does not implement validate_for_sender, so this constructs
        # successfully. SSRF must catch the rewritten URL at send time.
    )
    async with sender:
        with pytest.raises(SSRFValidationError):
            await sender.send_raw(
                url="http://buyer.example.com/webhook",
                idempotency_key="key_1",
                payload={"event": "test"},
            )
