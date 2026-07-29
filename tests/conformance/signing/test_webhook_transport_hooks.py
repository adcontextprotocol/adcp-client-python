"""Tests for transport hooks (URL rewrite before SSRF).

The security boundary: hooks run before SSRF, but SSRF is authoritative
on the rewritten URL. ``DockerLocalhostRewrite`` requires
``allow_private_destinations=True`` at sender construction — the test
suite enforces this contract.
"""

from __future__ import annotations

import pytest

from adcp.signing import SSRFValidationError
from adcp.signing.canonical import canonicalize_authority
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


# ---------- rewrite_to validation ----------


def test_bare_ipv6_rewrite_to_is_bracketed() -> None:
    """A bare IPv6 ``rewrite_to`` yields an unbracketed authority that RFC 3986
    makes ambiguous with a port. Bracket it at construction — the operator's
    intent is unambiguous and the docstring encourages IP-literal values."""
    hook = DockerLocalhostRewrite(rewrite_to="::1")
    assert hook.rewrite_url("https://localhost:9000/hook") == "https://[::1]:9000/hook"


def test_bare_ipv6_rewrite_survives_apply_hooks_and_signing_canonicalization() -> None:
    """The bracketing must hold all the way through the framework's port guard
    and into signing canonicalization. Unbracketed, ``urlsplit(...).port`` inside
    ``apply_hooks`` raises an opaque stdlib ValueError about a port the operator
    never configured."""
    hook = DockerLocalhostRewrite(rewrite_to="2001:db8::1")
    out = apply_hooks("https://localhost:9000/hook", (hook,))
    assert out == "https://[2001:db8::1]:9000/hook"
    assert canonicalize_authority(out) == "[2001:db8::1]:9000"


@pytest.mark.parametrize(
    "bad",
    ["", "attacker.com/path", "user@evil.example", "host.docker.internal:1234", "ho st"],
)
def test_rewrite_to_rejects_non_host_values(bad: str) -> None:
    """``rewrite_to`` is interpolated into the netloc; anything that is not a
    hostname or IP literal changes the signed URL in ways apply_hooks' scheme/
    port guard does not catch (path injection, userinfo injection, raw spaces)."""
    with pytest.raises(ValueError, match="rewrite_to"):
        DockerLocalhostRewrite(rewrite_to=bad)


def test_rewrite_to_rejects_ipv6_zone_id() -> None:
    """``ipaddress`` accepts scoped addresses, but RFC 6874 requires the ``%``
    be percent-encoded as ``%25`` inside a URI. Rather than emit an authority
    that is invalid on the wire, reject the zone-ID form at construction."""
    with pytest.raises(ValueError, match="rewrite_to"):
        DockerLocalhostRewrite(rewrite_to="fe80::1%eth0")


def test_bracketed_ipv6_rewrite_to_accepted_unchanged() -> None:
    hook = DockerLocalhostRewrite(rewrite_to="[::1]")
    assert hook.rewrite_url("https://localhost:9000/hook") == "https://[::1]:9000/hook"


def test_rewrite_to_hostname_and_ipv4_still_accepted() -> None:
    assert (
        DockerLocalhostRewrite().rewrite_url("http://localhost:8080/webhook")
        == "http://host.docker.internal:8080/webhook"
    )
    assert (
        DockerLocalhostRewrite(rewrite_to="172.17.0.1").rewrite_url("http://localhost/x")
        == "http://172.17.0.1/x"
    )


@pytest.mark.parametrize("bad", ["[]", "[.]", ".", "..", "...", "a..b"])
def test_rewrite_to_rejects_values_that_normalize_to_no_host(bad: str) -> None:
    """An empty host is the outcome this guard exists to prevent.

    These reach the empty host by two different routes the earlier checks each
    miss: ``"[]"`` is non-empty until the brackets come off, and ``"."`` /
    ``".."`` are non-empty until the trailing root dot is stripped. Both used
    to assemble to ``https://:9000/hook`` — an authority with a port and no
    host, which is precisely the shape ``@target-uri`` canonicalization
    rejects, produced by the hook meant to keep the authority well-formed.

    ``"a..b"`` is here because the fix is stated as "no empty label" rather
    than "not empty", and an interior empty label is the same defect.
    """
    with pytest.raises(ValueError, match="rewrite_to"):
        DockerLocalhostRewrite(rewrite_to=bad)


@pytest.mark.parametrize(
    "name", ["my_service", "host_gateway", "docker_host.local", "_dns-sd._udp.local"]
)
def test_rewrite_to_accepts_docker_legal_service_names(name: str) -> None:
    """Underscored names must keep working -- this is a Docker helper.

    Docker Compose service names legally contain underscores and Docker's
    embedded DNS resolves them, but RFC 952/1123 and IDNA both reject them.
    Validating ``rewrite_to`` as a *hostname* rather than structurally would
    refuse the exact configuration this class exists to serve, and would do it
    at construction -- turning a working deployment into a startup crash on
    upgrade. The guard only has to prove the value cannot restructure the URL.
    """
    assert DockerLocalhostRewrite(rewrite_to=name).rewrite_to == name


def test_rewrite_to_is_normalized_at_construction() -> None:
    """Canonicalization is visible on the field: case-folded, trailing FQDN
    root dot dropped, IDN encoded to A-labels, IPv6 bracketed."""
    assert DockerLocalhostRewrite(rewrite_to="HOST.Docker.Internal.").rewrite_to == (
        "host.docker.internal"
    )
    assert DockerLocalhostRewrite(rewrite_to="bücher.example").rewrite_to == "xn--bcher-kva.example"
    assert DockerLocalhostRewrite(rewrite_to="[2001:DB8::1]").rewrite_to == "[2001:db8::1]"


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
