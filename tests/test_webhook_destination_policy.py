from __future__ import annotations

import socket

import pytest

from adcp.webhooks import (
    WebhookDestinationPolicy,
    WebhookDestinationValidationError,
    validate_webhook_destination_url,
)


def test_production_accepts_https_public_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: object) -> list[tuple[object, ...]]:
        assert host == "example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("adcp.signing.jwks.socket.getaddrinfo", fake_getaddrinfo)

    result = validate_webhook_destination_url(
        "https://example.com/webhook",
        field="push_notification_config.url",
    )

    assert result.effective_url == "https://example.com/webhook"
    assert result.hostname == "example.com"
    assert result.resolved_ip == "93.184.216.34"
    assert result.port == 443


def test_production_rejects_http_before_dns() -> None:
    with pytest.raises(WebhookDestinationValidationError) as exc:
        validate_webhook_destination_url("http://example.com/webhook")

    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.reason == "https_required"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/webhook",
        "https://10.0.0.1/webhook",
        "https://169.254.169.254/webhook",
    ],
)
def test_production_rejects_private_loopback_and_metadata(url: str) -> None:
    with pytest.raises(WebhookDestinationValidationError) as exc:
        validate_webhook_destination_url(url, field="accounts[0].notification_configs[0].url")

    assert exc.value.reason == "ssrf_rejected"
    assert exc.value.field == "accounts[0].notification_configs[0].url"
    assert exc.value.to_error()["code"] == "INVALID_REQUEST"


def test_local_development_accepts_http_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: object) -> list[tuple[object, ...]]:
        assert host == "localhost"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr("adcp.signing.jwks.socket.getaddrinfo", fake_getaddrinfo)

    result = validate_webhook_destination_url(
        "http://localhost/webhook",
        policy=WebhookDestinationPolicy.local_development(),
    )

    assert result.effective_url == "http://localhost/webhook"
    assert result.resolved_ip == "127.0.0.1"
    assert result.policy.allow_private_destinations is True


def test_local_development_still_rejects_cloud_metadata() -> None:
    with pytest.raises(WebhookDestinationValidationError) as exc:
        validate_webhook_destination_url(
            "http://169.254.169.254/webhook",
            policy=WebhookDestinationPolicy.local_development(),
        )

    assert exc.value.reason == "ssrf_rejected"


def test_rejects_url_fragments_before_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_getaddrinfo(host: str, port: object) -> list[tuple[object, ...]]:
        raise AssertionError("fragment rejection should happen before DNS")

    monkeypatch.setattr("adcp.signing.jwks.socket.getaddrinfo", fail_getaddrinfo)

    with pytest.raises(WebhookDestinationValidationError) as exc:
        validate_webhook_destination_url("https://example.com/webhook#buyer-primary")

    assert exc.value.reason == "fragment_not_allowed"
