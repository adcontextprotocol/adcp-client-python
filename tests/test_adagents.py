from __future__ import annotations

"""Tests for adagents.json validation functionality."""

import json
import socket
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from adcp.adagents import (
    AuthorizationContext,
    _normalize_domain,
    _validate_publisher_domain,
    domain_matches,
    fetch_agent_authorizations,
    get_all_properties,
    get_all_tags,
    get_properties_by_agent,
    identifiers_match,
    resolve_properties_for_agent,
    verify_agent_authorization,
)
from adcp.exceptions import (
    AdagentsAccessBlockedError,
    AdagentsValidationError,
)


@pytest.fixture(autouse=True)
def _stub_getaddrinfo(monkeypatch):
    """Stub socket.getaddrinfo to a benign public IP for every test.

    The DNS pre-check in `_dns_validate_host` calls
    `socket.getaddrinfo` (via an executor) for every outbound URL whose
    host isn't a reserved RFC 2606 / 6761 name. Without this stub, tests
    that use arbitrary public-looking hostnames (e.g.
    `cdn.other-domain.com`) either hit live DNS or fail in CI
    environments without resolution.

    Tests that specifically want to exercise the DNS gate override this
    fixture or call `monkeypatch.setattr` on the same target.
    """

    def _fake_resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolve)


def _make_stream_response(
    *,
    status_code: int,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock streaming response with status, headers, aiter_bytes."""
    response = MagicMock()
    response.status_code = status_code
    response.headers = httpx.Headers(headers or {})

    body_bytes = body or b""

    async def aiter_bytes():
        if body_bytes:
            yield body_bytes

    response.aiter_bytes = aiter_bytes
    return response


def _stream_cm(response: MagicMock):
    """Wrap a response as an async context manager for client.stream(...)."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def make_text_url_client(url_to_text, called_urls=None):
    """Like ``make_url_dispatching_client`` but for plain-text ads.txt fetches.

    Used to mock the ads.txt MANAGERDOMAIN fallback path, which still
    uses ``client.get()``/``response.text`` rather than the streaming
    fetch. Maps URL → text body (200) or None (404).
    """

    async def _get(url, **kwargs):
        if called_urls is not None:
            called_urls.append(url)
        body = url_to_text.get(url)
        response = MagicMock()
        if body is None:
            response.status_code = 404
            response.text = ""
            response.content = b""
        else:
            response.status_code = 200
            response.text = body
            response.content = body.encode("utf-8")
        return response

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=_get)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


def make_url_dispatching_client(url_to_payload, called_urls=None, default_status=200):
    """Return a mock client whose .stream() dispatches by request URL.

    ``url_to_payload`` maps URL → either a JSON-serializable dict (sent
    with ``default_status``) or a tuple ``(dict, status_code, headers)``.
    For URLs not in the map, the mock returns 404. If ``called_urls``
    is provided, every request appends to it in order.
    """

    def _stream(method, url, **kwargs):
        if called_urls is not None:
            called_urls.append(url)
        entry = url_to_payload.get(url)
        if entry is None:
            response = _make_stream_response(status_code=404)
            return _stream_cm(response)
        if isinstance(entry, tuple):
            data, status, headers = entry
        else:
            data, status, headers = entry, default_status, {}
        body = json.dumps(data).encode("utf-8") if data is not None else b""
        response = _make_stream_response(status_code=status, body=body, headers=headers)
        return _stream_cm(response)

    mock_client = MagicMock()
    mock_client.stream = MagicMock(side_effect=_stream)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


def create_mock_httpx_client(mock_response):
    """Build a mock AsyncClient compatible with the streaming fetch path.

    Accepts a response built by tests with ``.status_code`` and either
    ``.json.return_value`` (legacy ergonomic) or ``.content`` /
    ``.text``. Translates that into a stream-capable mock by serializing
    the legacy JSON return value into the streamed body.
    """
    headers: dict[str, str] = {}
    if hasattr(mock_response, "headers") and mock_response.headers:
        try:
            headers = dict(mock_response.headers)
        except (TypeError, ValueError):
            headers = {}

    if hasattr(mock_response, "_mock_children") and "content" in mock_response._mock_children:
        body_bytes = mock_response.content
    elif (
        hasattr(mock_response, "json")
        and getattr(mock_response.json, "return_value", None) is not None
    ):
        body_bytes = json.dumps(mock_response.json.return_value).encode("utf-8")
    else:
        body_bytes = b""

    stream_response = _make_stream_response(
        status_code=mock_response.status_code,
        body=body_bytes,
        headers=headers,
    )

    mock_client_instance = MagicMock()
    mock_client_instance.stream = MagicMock(return_value=_stream_cm(stream_response))
    # Retain .get for tests that still assert against it; the production
    # code calls .stream(), so .get is effectively unused but kept callable
    # so legacy .get.assert_* assertions continue to operate on a real Mock.
    mock_client_instance.get = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__.return_value = mock_client_instance
    mock_client_instance.__aexit__.return_value = AsyncMock()
    return mock_client_instance


class TestDomainNormalization:
    """Test domain normalization function."""

    def test_normalize_basic(self):
        """Basic normalization should work."""
        assert _normalize_domain("Example.COM") == "example.com"
        assert _normalize_domain("  example.com  ") == "example.com"

    def test_normalize_trailing_slash(self):
        """Should remove trailing slashes."""
        assert _normalize_domain("example.com/") == "example.com"
        assert _normalize_domain("example.com///") == "example.com"

    def test_normalize_trailing_dot(self):
        """Should remove trailing dots."""
        assert _normalize_domain("example.com.") == "example.com"
        assert _normalize_domain("example.com...") == "example.com"

    def test_normalize_both(self):
        """Should remove both trailing slashes and dots."""
        assert _normalize_domain("example.com/.") == "example.com"

    def test_normalize_invalid_double_dots(self):
        """Double dots should raise error."""
        with pytest.raises(AdagentsValidationError, match="Invalid domain format"):
            _normalize_domain("example..com")

    def test_normalize_empty(self):
        """Empty string should raise error."""
        with pytest.raises(AdagentsValidationError, match="Invalid domain format"):
            _normalize_domain("")
        with pytest.raises(AdagentsValidationError, match="Invalid domain format"):
            _normalize_domain("   ")


class TestPublisherDomainValidation:
    """Test publisher domain validation for security."""

    def test_validate_basic(self):
        """Basic valid domains should pass."""
        assert _validate_publisher_domain("example.com") == "example.com"
        assert _validate_publisher_domain("sub.example.com") == "sub.example.com"

    def test_validate_removes_protocol(self):
        """Should strip protocol if present."""
        assert _validate_publisher_domain("https://example.com") == "example.com"
        assert _validate_publisher_domain("http://example.com") == "example.com"

    def test_validate_removes_path(self):
        """Should strip path if present."""
        assert _validate_publisher_domain("example.com/path") == "example.com"
        assert _validate_publisher_domain("https://example.com/path") == "example.com"

    def test_validate_case_insensitive(self):
        """Should normalize to lowercase."""
        assert _validate_publisher_domain("EXAMPLE.COM") == "example.com"

    def test_validate_empty(self):
        """Empty domain should raise error."""
        with pytest.raises(AdagentsValidationError, match="cannot be empty"):
            _validate_publisher_domain("")
        with pytest.raises(AdagentsValidationError, match="cannot be empty"):
            _validate_publisher_domain("   ")

    def test_validate_too_long(self):
        """Domain exceeding DNS max length should raise error."""
        long_domain = "a" * 254
        with pytest.raises(AdagentsValidationError, match="too long"):
            _validate_publisher_domain(long_domain)

    def test_validate_suspicious_chars(self):
        """Suspicious characters should raise error."""
        with pytest.raises(AdagentsValidationError, match="Invalid character"):
            _validate_publisher_domain("example.com\\malicious")
        with pytest.raises(AdagentsValidationError, match="Invalid character"):
            _validate_publisher_domain("user@example.com")
        with pytest.raises(AdagentsValidationError, match="Invalid character"):
            _validate_publisher_domain("example.com with spaces")
        with pytest.raises(AdagentsValidationError, match="Invalid character"):
            _validate_publisher_domain("example.com\n")

    def test_validate_no_dots(self):
        """Domain without dots should raise error."""
        with pytest.raises(AdagentsValidationError, match="must contain at least one dot"):
            _validate_publisher_domain("localhost")


class TestDomainMatching:
    """Test domain matching logic per AdCP spec."""

    def test_exact_match(self):
        """Exact domain match should succeed."""
        assert domain_matches("example.com", "example.com")
        assert domain_matches("sub.example.com", "sub.example.com")

    def test_case_insensitive(self):
        """Domain matching should be case-insensitive."""
        assert domain_matches("Example.com", "example.com")
        assert domain_matches("example.com", "EXAMPLE.COM")

    def test_bare_domain_matches_www(self):
        """Bare domain should match www subdomain."""
        assert domain_matches("www.example.com", "example.com")
        assert domain_matches("m.example.com", "example.com")

    def test_bare_domain_does_not_match_other_subdomains(self):
        """Bare domain should NOT match arbitrary subdomains."""
        assert not domain_matches("api.example.com", "example.com")
        assert not domain_matches("cdn.example.com", "example.com")

    def test_specific_subdomain_does_not_match_others(self):
        """Specific subdomain should only match itself."""
        assert not domain_matches("www.example.com", "api.example.com")
        assert domain_matches("api.example.com", "api.example.com")

    def test_wildcard_matches_all_subdomains(self):
        """Wildcard pattern should match all subdomains."""
        assert domain_matches("www.example.com", "*.example.com")
        assert domain_matches("api.example.com", "*.example.com")
        assert domain_matches("cdn.example.com", "*.example.com")
        assert domain_matches("sub.api.example.com", "*.example.com")

    def test_wildcard_does_not_match_base_domain(self):
        """Wildcard should not match the base domain without subdomain."""
        assert not domain_matches("example.com", "*.example.com")

    def test_no_match_different_domains(self):
        """Different domains should not match."""
        assert not domain_matches("example.com", "other.com")
        assert not domain_matches("www.example.com", "other.com")


class TestIdentifierMatching:
    """Test identifier matching logic."""

    def test_domain_identifier_uses_domain_matching(self):
        """Domain identifiers should use domain matching rules."""
        property_ids = [{"type": "domain", "value": "www.example.com"}]
        agent_ids = [{"type": "domain", "value": "example.com"}]
        assert identifiers_match(property_ids, agent_ids)

    def test_bundle_id_exact_match(self):
        """Bundle IDs require exact match."""
        property_ids = [{"type": "bundle_id", "value": "com.example.app"}]
        agent_ids = [{"type": "bundle_id", "value": "com.example.app"}]
        assert identifiers_match(property_ids, agent_ids)

    def test_bundle_id_no_partial_match(self):
        """Bundle IDs should not partially match."""
        property_ids = [{"type": "bundle_id", "value": "com.example.app"}]
        agent_ids = [{"type": "bundle_id", "value": "com.example"}]
        assert not identifiers_match(property_ids, agent_ids)

    def test_type_mismatch(self):
        """Different identifier types should not match."""
        property_ids = [{"type": "domain", "value": "example.com"}]
        agent_ids = [{"type": "bundle_id", "value": "example.com"}]
        assert not identifiers_match(property_ids, agent_ids)

    def test_multiple_identifiers_any_match(self):
        """Should match if ANY identifier matches."""
        property_ids = [
            {"type": "domain", "value": "example.com"},
            {"type": "bundle_id", "value": "com.example.app"},
        ]
        agent_ids = [{"type": "bundle_id", "value": "com.example.app"}]
        assert identifiers_match(property_ids, agent_ids)

    def test_no_match_empty_lists(self):
        """Empty lists should not match."""
        assert not identifiers_match([], [])
        assert not identifiers_match([{"type": "domain", "value": "example.com"}], [])


class TestVerifyAgentAuthorization:
    """Test agent authorization verification."""

    def test_agent_authorized_no_properties_restriction(self):
        """Agent with empty properties array is authorized for all properties."""
        adagents_data = {
            "authorized_agents": [{"url": "https://sales-agent.example.com", "properties": []}]
        }
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_agent_authorized_no_properties_field(self):
        """Agent without properties field is authorized for all properties."""
        adagents_data = {"authorized_agents": [{"url": "https://sales-agent.example.com"}]}
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_agent_url_protocol_agnostic(self):
        """Agent URL matching should ignore protocol."""
        adagents_data = {"authorized_agents": [{"url": "https://sales-agent.example.com"}]}
        assert verify_agent_authorization(
            adagents_data, "http://sales-agent.example.com", None, None
        )

    def test_agent_url_trailing_slash_ignored(self):
        """Agent URL matching should ignore trailing slash."""
        adagents_data = {"authorized_agents": [{"url": "https://sales-agent.example.com/"}]}
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_agent_authorized_specific_property(self):
        """Agent authorized for specific property type and identifiers."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://sales-agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Example Site",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ]
        }
        assert verify_agent_authorization(
            adagents_data,
            "https://sales-agent.example.com",
            "website",
            [{"type": "domain", "value": "www.example.com"}],
        )

    def test_agent_not_authorized_wrong_property_type(self):
        """Agent should not be authorized for wrong property type."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://sales-agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ]
        }
        assert not verify_agent_authorization(
            adagents_data,
            "https://sales-agent.example.com",
            "mobile_app",
            [{"type": "domain", "value": "example.com"}],
        )

    def test_agent_not_authorized_wrong_identifier(self):
        """Agent should not be authorized for wrong identifier."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://sales-agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ]
        }
        assert not verify_agent_authorization(
            adagents_data,
            "https://sales-agent.example.com",
            "website",
            [{"type": "domain", "value": "other.com"}],
        )

    def test_agent_not_in_list(self):
        """Agent not in authorized_agents list should not be authorized."""
        adagents_data = {
            "authorized_agents": [{"url": "https://other-agent.example.com", "properties": []}]
        }
        assert not verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_multiple_agents(self):
        """Should find correct agent in list."""
        adagents_data = {
            "authorized_agents": [
                {"url": "https://agent1.example.com", "properties": []},
                {"url": "https://agent2.example.com", "properties": []},
                {"url": "https://sales-agent.example.com", "properties": []},
            ]
        }
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", None, None
        )

    def test_invalid_adagents_data_not_dict(self):
        """Should raise error if adagents_data is not a dict."""
        with pytest.raises(AdagentsValidationError, match="must be a dictionary"):
            verify_agent_authorization([], "https://agent.example.com", None, None)

    def test_invalid_adagents_data_no_authorized_agents(self):
        """Should raise error if authorized_agents field is missing."""
        with pytest.raises(AdagentsValidationError, match="authorized_agents"):
            verify_agent_authorization({}, "https://agent.example.com", None, None)

    def test_invalid_authorized_agents_not_list(self):
        """Should raise error if authorized_agents is not a list."""
        with pytest.raises(AdagentsValidationError, match="authorized_agents"):
            verify_agent_authorization(
                {"authorized_agents": "not a list"}, "https://agent.example.com", None, None
            )

    def test_property_type_match_without_identifiers(self):
        """Should match property type even without identifier check."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://sales-agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ]
        }
        # When property_identifiers is None, just check property_type
        assert verify_agent_authorization(
            adagents_data, "https://sales-agent.example.com", "website", None
        )


class TestFetchAdagents:
    """Test fetching adagents.json from publisher domains."""

    @pytest.mark.asyncio
    async def test_fetch_success(self):
        """Should successfully fetch and parse adagents.json."""
        from adcp.adagents import fetch_adagents

        mock_adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All properties",
                    "authorization_type": "property_ids",
                    "property_ids": ["site1", "site2"],
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_adagents_data
        mock_response.raise_for_status = MagicMock()

        mock_client = create_mock_httpx_client(mock_response)

        result = await fetch_adagents("example.com", client=mock_client)

        assert result == mock_adagents_data
        mock_client.stream.assert_called_once()
        call_args = mock_client.stream.call_args
        assert "https://example.com/.well-known/adagents.json" in str(call_args)

    @pytest.mark.asyncio
    async def test_fetch_follows_authoritative_location(self):
        """Should follow authoritative_location redirect and return resolved data."""
        import adcp.adagents as adagents_module
        from adcp.adagents import fetch_adagents

        # Initial response has authoritative_location redirect
        redirect_response_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "https://cdn.example.com/adagents/v2/adagents.json",
            "last_updated": "2025-01-15T10:00:00Z",
        }

        # Final resolved data at the authoritative location
        resolved_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All properties",
                    "authorization_type": "property_tags",
                    "property_tags": ["all"],
                }
            ],
            "last_updated": "2025-01-15T10:00:00Z",
        }

        called_urls: list[str] = []
        mock_client = make_url_dispatching_client(
            {"https://example.com/.well-known/adagents.json": redirect_response_data},
            called_urls=called_urls,
        )

        # Redirect hop uses a fresh client — mock httpx.AsyncClient for that
        redirect_client = make_url_dispatching_client(
            {"https://cdn.example.com/adagents/v2/adagents.json": resolved_data},
            called_urls=called_urls,
        )

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: redirect_client
        ):
            result = await fetch_adagents("example.com", client=mock_client)

        assert result == resolved_data
        assert called_urls == [
            "https://example.com/.well-known/adagents.json",
            "https://cdn.example.com/adagents/v2/adagents.json",
        ]

    @pytest.mark.asyncio
    async def test_fetch_rejects_non_https_authoritative_location(self):
        """Should reject authoritative_location that uses HTTP instead of HTTPS."""
        from adcp.adagents import fetch_adagents

        redirect_response_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "http://cdn.example.com/adagents.json",  # HTTP not HTTPS
            "last_updated": "2025-01-15T10:00:00Z",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = redirect_response_data

        mock_client = create_mock_httpx_client(mock_response)

        with pytest.raises(AdagentsValidationError, match="HTTPS"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_fetch_prevents_redirect_loop(self):
        """Should detect and prevent circular redirect loops."""
        from adcp.adagents import fetch_adagents

        # Circular redirect: A -> B -> A
        redirect_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "https://example.com/.well-known/adagents.json",
            "last_updated": "2025-01-15T10:00:00Z",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = redirect_data

        mock_client = create_mock_httpx_client(mock_response)

        with pytest.raises(AdagentsValidationError, match="Circular redirect"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_fetch_enforces_max_redirect_depth(self):
        """Should enforce maximum redirect depth to prevent abuse."""
        import adcp.adagents as adagents_module
        from adcp.adagents import fetch_adagents

        # Create a long chain of redirects
        call_count = [0]

        def _stream(method, url, **kwargs):
            call_count[0] += 1
            data = {
                "$schema": "/schemas/2.6.0/adagents.json",
                "authoritative_location": f"https://cdn{call_count[0]}.example.com/adagents.json",
                "last_updated": "2025-01-15T10:00:00Z",
            }
            response = _make_stream_response(status_code=200, body=json.dumps(data).encode("utf-8"))
            return _stream_cm(response)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(side_effect=_stream)

        redirect_client = MagicMock()
        redirect_client.stream = MagicMock(side_effect=_stream)
        redirect_client.__aenter__ = AsyncMock(return_value=redirect_client)
        redirect_client.__aexit__ = AsyncMock(return_value=None)

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: redirect_client
        ):
            with pytest.raises(AdagentsValidationError, match="redirect|depth"):
                await fetch_adagents("example.com", client=mock_client)

        # Should stop after reasonable number of redirects (not go forever)
        assert call_count[0] <= 10

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cf_value", ["challenge", "Challenge"])
    async def test_fetch_403_cf_mitigated_raises_access_blocked(self, cf_value):
        """403 + cf-mitigated: challenge raises AdagentsAccessBlockedError (case-insensitive)."""
        from adcp.adagents import fetch_adagents

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.headers = httpx.Headers({"cf-mitigated": cf_value})
        mock_response.json.return_value = None

        mock_client = create_mock_httpx_client(mock_response)

        with pytest.raises(AdagentsAccessBlockedError, match="cf-mitigated: challenge"):
            await fetch_adagents("cafemedia.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_fetch_403_no_cf_header_raises_generic_validation_error(self):
        """Plain 403 without cf-mitigated header raises generic AdagentsValidationError."""
        from adcp.adagents import fetch_adagents

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.headers = httpx.Headers({})
        mock_response.json.return_value = None

        mock_client = create_mock_httpx_client(mock_response)

        with pytest.raises(AdagentsValidationError, match="HTTP 403") as exc_info:
            await fetch_adagents("example.com", client=mock_client)
        assert not isinstance(exc_info.value, AdagentsAccessBlockedError)

    def test_access_blocked_is_subclass_of_validation_error(self):
        """AdagentsAccessBlockedError is catchable as AdagentsValidationError."""
        err = AdagentsAccessBlockedError("cafemedia.com")
        assert isinstance(err, AdagentsValidationError)
        assert "cf-mitigated: challenge" in str(err)
        assert err.publisher_domain == "cafemedia.com"


class TestSSRFProtection:
    """Test SSRF protections on authoritative_location redirects."""

    @pytest.mark.asyncio
    async def test_rejects_localhost_redirect(self):
        """Should reject authoritative_location pointing to localhost."""
        from adcp.adagents import fetch_adagents

        redirect_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "https://localhost/adagents.json",
            "last_updated": "2025-01-15T10:00:00Z",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = redirect_data

        mock_client = create_mock_httpx_client(mock_response)

        with pytest.raises(AdagentsValidationError, match="localhost"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_rejects_private_ip_redirect(self):
        """Should reject authoritative_location pointing to private IP."""
        from adcp.adagents import fetch_adagents

        redirect_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "https://192.168.1.1/adagents.json",
            "last_updated": "2025-01-15T10:00:00Z",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = redirect_data

        mock_client = create_mock_httpx_client(mock_response)

        with pytest.raises(AdagentsValidationError, match="private/reserved"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_rejects_loopback_ip_redirect(self):
        """Should reject authoritative_location pointing to 127.0.0.1."""
        from adcp.adagents import fetch_adagents

        redirect_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "https://127.0.0.1/adagents.json",
            "last_updated": "2025-01-15T10:00:00Z",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = redirect_data

        mock_client = create_mock_httpx_client(mock_response)

        with pytest.raises(AdagentsValidationError, match="private/reserved"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_rejects_cloud_metadata_ip_redirect(self):
        """Should reject authoritative_location pointing to cloud metadata endpoint."""
        from adcp.adagents import fetch_adagents

        redirect_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "https://169.254.169.254/latest/meta-data/",
            "last_updated": "2025-01-15T10:00:00Z",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = redirect_data

        mock_client = create_mock_httpx_client(mock_response)

        with pytest.raises(AdagentsValidationError, match="private/reserved"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_rejects_dot_local_redirect(self):
        """Should reject authoritative_location pointing to .local domain."""
        from adcp.adagents import fetch_adagents

        redirect_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "https://internal-service.local/adagents.json",
            "last_updated": "2025-01-15T10:00:00Z",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = redirect_data

        mock_client = create_mock_httpx_client(mock_response)

        with pytest.raises(AdagentsValidationError, match="localhost"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_resolved_private_ip_rejected_before_connect(self, monkeypatch):
        # A public-looking hostname whose DNS points at a private address
        # must be rejected by the resolve-and-validate pre-check, not
        # silently connected to. Closes the string-level gap left by
        # _check_safe_host alone.
        from adcp.adagents import fetch_adagents

        def _resolve_to_private(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

        monkeypatch.setattr(socket, "getaddrinfo", _resolve_to_private)

        # Use a hostname that isn't in the RFC 2606 / 6761 reserved list
        # so the resolve pre-check actually runs.
        with pytest.raises(AdagentsValidationError, match="private/reserved"):
            await fetch_adagents("metadata.realhost.org")

    @pytest.mark.asyncio
    async def test_resolved_dns_failure_surfaces_as_validation_error(self, monkeypatch):
        # A DNS gaierror (NXDOMAIN, transient SERVFAIL) becomes a clear
        # AdagentsValidationError rather than bubbling raw socket errors
        # through the SDK boundary.
        from adcp.adagents import fetch_adagents

        def _resolve_fails(host, port, *args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _resolve_fails)

        with pytest.raises(AdagentsValidationError, match="DNS resolution failed"):
            await fetch_adagents("nonexistent.realhost.org")

    @pytest.mark.asyncio
    async def test_resolved_mixed_public_and_private_is_rejected(self, monkeypatch):
        # If a hostname resolves to a list of addresses where ANY one is
        # private, the SDK must reject — defending against split-horizon
        # DNS that returns both a public and a private address.
        from adcp.adagents import fetch_adagents

        def _resolve_mixed(host, port, *args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", _resolve_mixed)

        with pytest.raises(AdagentsValidationError, match="private/reserved"):
            await fetch_adagents("split-horizon.realhost.org")

    @pytest.mark.asyncio
    async def test_redirect_uses_fresh_client(self):
        """Redirect hops should not reuse the caller's client."""
        import adcp.adagents as adagents_module
        from adcp.adagents import fetch_adagents

        resolved_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ],
            "last_updated": "2025-01-15T10:00:00Z",
        }

        redirect_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "https://cdn.other-domain.com/adagents.json",
            "last_updated": "2025-01-15T10:00:00Z",
        }

        caller_urls: list[str] = []
        caller_client = make_url_dispatching_client(
            {"https://example.com/.well-known/adagents.json": redirect_data},
            called_urls=caller_urls,
        )

        fresh_client_urls: list[str] = []
        fresh_client = make_url_dispatching_client(
            {"https://cdn.other-domain.com/adagents.json": resolved_data},
            called_urls=fresh_client_urls,
        )

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: fresh_client
        ):
            result = await fetch_adagents("example.com", client=caller_client)

        # Initial fetch used the caller's client
        assert len(caller_urls) == 1
        assert "example.com" in caller_urls[0]
        # Redirect used a fresh client
        assert len(fresh_client_urls) == 1
        assert "cdn.other-domain.com" in fresh_client_urls[0]
        assert "authorized_agents" in result

    @pytest.mark.asyncio
    async def test_allows_public_domain_redirect(self):
        """Should allow redirects to legitimate public domains."""
        import adcp.adagents as adagents_module
        from adcp.adagents import fetch_adagents

        resolved_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ],
            "last_updated": "2025-01-15T10:00:00Z",
        }

        redirect_data = {
            "$schema": "/schemas/2.6.0/adagents.json",
            "authoritative_location": "https://cdn.example.com/adagents/v2/adagents.json",
            "last_updated": "2025-01-15T10:00:00Z",
        }

        mock_client = make_url_dispatching_client(
            {"https://example.com/.well-known/adagents.json": redirect_data},
        )
        redirect_client = make_url_dispatching_client(
            {"https://cdn.example.com/adagents/v2/adagents.json": resolved_data},
        )

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: redirect_client
        ):
            result = await fetch_adagents("example.com", client=mock_client)
        assert "authorized_agents" in result


class TestVerifyAgentForProperty:
    """Test convenience wrapper for fetching and verifying in one call."""

    @pytest.mark.asyncio
    async def test_verify_success(self):
        """Should fetch and verify authorization successfully."""
        from adcp.adagents import verify_agent_for_property

        mock_adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All properties",
                    "authorization_type": "property_ids",
                    "property_ids": ["site1", "site2"],
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_adagents_data
        mock_response.raise_for_status = MagicMock()

        mock_client = create_mock_httpx_client(mock_response)

        # Verify authorized agent
        result = await verify_agent_for_property(
            publisher_domain="example.com",
            agent_url="https://agent.example.com",
            property_identifiers=[{"type": "property_id", "value": "site1"}],
            client=mock_client,
        )

        assert result is True
        mock_client.stream.assert_called_once()


class TestGetAllProperties:
    """Test extracting all properties from adagents.json data."""

    def test_get_all_properties(self):
        """Should extract all properties from all agents."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        },
                        {
                            "property_type": "mobile_app",
                            "name": "App 1",
                            "identifiers": [{"type": "bundle_id", "value": "com.site1.app"}],
                        },
                    ],
                },
                {
                    "url": "https://agent2.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 2",
                            "identifiers": [{"type": "domain", "value": "site2.com"}],
                        }
                    ],
                },
            ]
        }

        properties = get_all_properties(adagents_data)
        assert len(properties) == 3
        assert properties[0]["name"] == "Site 1"
        assert properties[0]["agent_url"] == "https://agent1.example.com"
        assert properties[1]["name"] == "App 1"
        assert properties[1]["agent_url"] == "https://agent1.example.com"
        assert properties[2]["name"] == "Site 2"
        assert properties[2]["agent_url"] == "https://agent2.example.com"

    def test_get_all_properties_with_empty_properties(self):
        """Should handle agents with empty properties array."""
        adagents_data = {
            "authorized_agents": [
                {"url": "https://agent1.example.com", "properties": []},
                {
                    "url": "https://agent2.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site",
                            "identifiers": [{"type": "domain", "value": "site.com"}],
                        }
                    ],
                },
            ]
        }

        properties = get_all_properties(adagents_data)
        assert len(properties) == 1
        assert properties[0]["name"] == "Site"

    def test_get_all_properties_with_property_ids(self):
        """Should resolve property_ids against top-level properties."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "la_depeche",
                    "property_type": "website",
                    "name": "La Dépêche",
                    "identifiers": [{"type": "domain", "value": "ladepeche.fr"}],
                },
                {
                    "property_id": "midi_libre",
                    "property_type": "website",
                    "name": "Midi Libre",
                    "identifiers": [{"type": "domain", "value": "midilibre.fr"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "property_ids",
                    "property_ids": ["la_depeche"],
                },
                {
                    "url": "https://agent2.example.com",
                    "authorization_type": "property_ids",
                    "property_ids": ["midi_libre"],
                },
            ],
        }

        properties = get_all_properties(adagents_data)
        assert len(properties) == 2
        assert properties[0]["name"] == "La Dépêche"
        assert properties[0]["agent_url"] == "https://agent1.example.com"
        assert properties[1]["name"] == "Midi Libre"
        assert properties[1]["agent_url"] == "https://agent2.example.com"

    def test_get_all_properties_with_property_tags(self):
        """Should resolve property_tags against top-level properties."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site_a",
                    "property_type": "website",
                    "name": "Site A",
                    "identifiers": [{"type": "domain", "value": "a.com"}],
                    "tags": ["news", "premium"],
                },
                {
                    "property_id": "site_b",
                    "property_type": "website",
                    "name": "Site B",
                    "identifiers": [{"type": "domain", "value": "b.com"}],
                    "tags": ["sports"],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "property_tags",
                    "property_tags": ["news"],
                },
            ],
        }

        properties = get_all_properties(adagents_data)
        assert len(properties) == 1
        assert properties[0]["name"] == "Site A"
        assert properties[0]["agent_url"] == "https://agent1.example.com"

    def test_get_all_properties_mixed_authorization_types(self):
        """Should handle mix of inline, property_ids, and property_tags."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "ref_site",
                    "property_type": "website",
                    "name": "Referenced Site",
                    "identifiers": [{"type": "domain", "value": "ref.com"}],
                    "tags": ["premium"],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://inline-agent.example.com",
                    "authorization_type": "inline_properties",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Inline Site",
                            "identifiers": [{"type": "domain", "value": "inline.com"}],
                        }
                    ],
                },
                {
                    "url": "https://ids-agent.example.com",
                    "authorization_type": "property_ids",
                    "property_ids": ["ref_site"],
                },
                {
                    "url": "https://tags-agent.example.com",
                    "authorization_type": "property_tags",
                    "property_tags": ["premium"],
                },
            ],
        }

        properties = get_all_properties(adagents_data)
        assert len(properties) == 3
        # Check agent_url attribution
        by_agent = {p["agent_url"]: p["name"] for p in properties}
        assert by_agent["https://inline-agent.example.com"] == "Inline Site"
        assert by_agent["https://ids-agent.example.com"] == "Referenced Site"
        assert by_agent["https://tags-agent.example.com"] == "Referenced Site"

    def test_get_all_properties_deduplicates_not(self):
        """Properties referenced by multiple agents should appear once per agent."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "shared",
                    "property_type": "website",
                    "name": "Shared Site",
                    "identifiers": [{"type": "domain", "value": "shared.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "property_ids",
                    "property_ids": ["shared"],
                },
                {
                    "url": "https://agent2.example.com",
                    "authorization_type": "property_ids",
                    "property_ids": ["shared"],
                },
            ],
        }

        properties = get_all_properties(adagents_data)
        assert len(properties) == 2
        assert properties[0]["agent_url"] == "https://agent1.example.com"
        assert properties[1]["agent_url"] == "https://agent2.example.com"

    def test_get_all_properties_unknown_authorization_type(self):
        """Should return empty for agents with unrecognized authorization_type."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorization_type": "some_future_type",
                },
            ],
        }

        properties = get_all_properties(adagents_data)
        assert properties == []

    def test_get_all_properties_authorization_type_takes_precedence(self):
        """authorization_type should take precedence over stale properties key."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "correct",
                    "property_type": "website",
                    "name": "Correct Site",
                    "identifiers": [{"type": "domain", "value": "correct.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorization_type": "property_ids",
                    "property_ids": ["correct"],
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Stale Inline Site",
                            "identifiers": [{"type": "domain", "value": "stale.com"}],
                        }
                    ],
                },
            ],
        }

        properties = get_all_properties(adagents_data)
        assert len(properties) == 1
        assert properties[0]["name"] == "Correct Site"

    def test_get_all_properties_invalid_data(self):
        """Should raise error for invalid data."""
        with pytest.raises(AdagentsValidationError):
            get_all_properties([])


class TestGetAllTags:
    """Test extracting all unique tags from adagents.json data."""

    def test_get_all_tags(self):
        """Should extract all unique tags from properties."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                            "tags": ["premium", "news"],
                        },
                        {
                            "property_type": "mobile_app",
                            "name": "App 1",
                            "identifiers": [{"type": "bundle_id", "value": "com.site1.app"}],
                            "tags": ["mobile", "premium"],
                        },
                    ],
                },
                {
                    "url": "https://agent2.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 2",
                            "identifiers": [{"type": "domain", "value": "site2.com"}],
                            "tags": ["sports"],
                        }
                    ],
                },
            ]
        }

        tags = get_all_tags(adagents_data)
        assert tags == {"premium", "news", "mobile", "sports"}

    def test_get_all_tags_no_tags(self):
        """Should return empty set when no tags present."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        }
                    ],
                }
            ]
        }

        tags = get_all_tags(adagents_data)
        assert tags == set()

    def test_get_all_tags_with_property_ids(self):
        """Should extract tags from properties resolved via property_ids."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site_a",
                    "property_type": "website",
                    "name": "Site A",
                    "identifiers": [{"type": "domain", "value": "a.com"}],
                    "tags": ["premium", "news"],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorization_type": "property_ids",
                    "property_ids": ["site_a"],
                },
            ],
        }

        tags = get_all_tags(adagents_data)
        assert tags == {"premium", "news"}


class TestGetPropertiesByAgent:
    """Test getting properties for a specific agent."""

    def test_get_properties_by_agent_inline_properties(self):
        """Should return inline properties for agent with authorization_type=inline_properties."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test properties",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        },
                        {
                            "property_type": "mobile_app",
                            "name": "App 1",
                            "identifiers": [{"type": "bundle_id", "value": "com.site1.app"}],
                        },
                    ],
                },
            ]
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert len(properties) == 2
        assert properties[0]["name"] == "Site 1"
        assert properties[1]["name"] == "App 1"

    def test_get_properties_by_agent_legacy_properties(self):
        """Should return properties for agent without explicit authorization_type."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        },
                    ],
                },
            ]
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert len(properties) == 1
        assert properties[0]["name"] == "Site 1"

    def test_get_properties_by_agent_property_ids(self):
        """Should filter top-level properties by property_id for authorization_type=property_ids."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                },
                {
                    "property_id": "site2",
                    "property_type": "website",
                    "name": "Site 2",
                    "identifiers": [{"type": "domain", "value": "site2.com"}],
                },
                {
                    "property_id": "site3",
                    "property_type": "website",
                    "name": "Site 3",
                    "identifiers": [{"type": "domain", "value": "site3.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "property_ids",
                    "authorized_for": "Selected properties",
                    "property_ids": ["site1", "site3"],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert len(properties) == 2
        assert properties[0]["name"] == "Site 1"
        assert properties[1]["name"] == "Site 3"

    def test_get_properties_by_agent_property_tags(self):
        """Should filter top-level properties by tags for authorization_type=property_tags."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                    "tags": ["premium", "news"],
                },
                {
                    "property_id": "site2",
                    "property_type": "website",
                    "name": "Site 2",
                    "identifiers": [{"type": "domain", "value": "site2.com"}],
                    "tags": ["sports"],
                },
                {
                    "property_id": "site3",
                    "property_type": "website",
                    "name": "Site 3",
                    "identifiers": [{"type": "domain", "value": "site3.com"}],
                    "tags": ["premium", "entertainment"],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "property_tags",
                    "authorized_for": "Premium properties",
                    "property_tags": ["premium"],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert len(properties) == 2
        assert properties[0]["name"] == "Site 1"
        assert properties[1]["name"] == "Site 3"

    def test_get_properties_by_agent_property_tags_multiple(self):
        """Should match properties with any of the authorized tags."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                    "tags": ["news"],
                },
                {
                    "property_id": "site2",
                    "property_type": "website",
                    "name": "Site 2",
                    "identifiers": [{"type": "domain", "value": "site2.com"}],
                    "tags": ["sports"],
                },
                {
                    "property_id": "site3",
                    "property_type": "website",
                    "name": "Site 3",
                    "identifiers": [{"type": "domain", "value": "site3.com"}],
                    "tags": ["entertainment"],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "property_tags",
                    "authorized_for": "News and sports",
                    "property_tags": ["news", "sports"],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert len(properties) == 2
        assert properties[0]["name"] == "Site 1"
        assert properties[1]["name"] == "Site 2"

    def test_get_properties_by_agent_publisher_properties(self):
        """Should inline-resolve publisher_properties selectors against top-level properties."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "cnn-ctv-1",
                    "publisher_domain": "cnn.com",
                    "property_type": "ctv_app",
                    "name": "CNN CTV",
                    "identifiers": [{"type": "bundle_id", "value": "com.cnn.ctv"}],
                    "tags": ["ctv"],
                },
                {
                    "property_id": "cnn-web-1",
                    "publisher_domain": "cnn.com",
                    "property_type": "website",
                    "name": "CNN Web",
                    "identifiers": [{"type": "domain", "value": "cnn.com"}],
                    "tags": ["web"],
                },
                {
                    "property_id": "espn-1",
                    "publisher_domain": "espn.com",
                    "property_type": "website",
                    "name": "ESPN",
                    "identifiers": [{"type": "domain", "value": "espn.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "Cross-domain properties",
                    "publisher_properties": [
                        {
                            "publisher_domain": "cnn.com",
                            "selection_type": "by_tag",
                            "property_tags": ["ctv"],
                        },
                        {
                            "publisher_domain": "espn.com",
                            "selection_type": "all",
                        },
                    ],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert len(properties) == 2
        ids = {p["property_id"] for p in properties}
        assert ids == {"cnn-ctv-1", "espn-1"}

    def test_get_properties_by_agent_publisher_domains_fanout(self):
        """publisher_domains[] compact form expands and resolves per-domain inline."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "a-1",
                    "publisher_domain": "a.com",
                    "property_type": "website",
                    "name": "A",
                    "identifiers": [{"type": "domain", "value": "a.com"}],
                },
                {
                    "property_id": "b-1",
                    "publisher_domain": "b.com",
                    "property_type": "website",
                    "name": "B",
                    "identifiers": [{"type": "domain", "value": "b.com"}],
                },
                {
                    "property_id": "c-1",
                    "publisher_domain": "c.com",
                    "property_type": "website",
                    "name": "C",
                    "identifiers": [{"type": "domain", "value": "c.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "fanout",
                    "publisher_properties": [
                        {
                            "publisher_domains": ["a.com", "b.com", "c.com"],
                            "selection_type": "all",
                        },
                    ],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert {p["property_id"] for p in properties} == {"a-1", "b-1", "c-1"}

    def test_get_properties_by_agent_publisher_properties_by_id(self):
        """selection_type=by_id with property_ids returns only the named property."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "x",
                    "publisher_domain": "site.com",
                    "property_type": "website",
                    "name": "X",
                    "identifiers": [{"type": "domain", "value": "site.com/x"}],
                },
                {
                    "property_id": "y",
                    "publisher_domain": "site.com",
                    "property_type": "website",
                    "name": "Y",
                    "identifiers": [{"type": "domain", "value": "site.com/y"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "by_id",
                    "publisher_properties": [
                        {
                            "publisher_domain": "site.com",
                            "selection_type": "by_id",
                            "property_ids": ["x"],
                        },
                    ],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert [p["property_id"] for p in properties] == ["x"]

    def test_revocation_honored_on_publisher_domains_fanout(self):
        """Selector publisher_domains=[a,b,c] with parent revoking b → b's properties excluded."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "a-1",
                    "publisher_domain": "a.com",
                    "property_type": "website",
                    "name": "A",
                    "identifiers": [{"type": "domain", "value": "a.com"}],
                },
                {
                    "property_id": "b-1",
                    "publisher_domain": "b.com",
                    "property_type": "website",
                    "name": "B",
                    "identifiers": [{"type": "domain", "value": "b.com"}],
                },
                {
                    "property_id": "c-1",
                    "publisher_domain": "c.com",
                    "property_type": "website",
                    "name": "C",
                    "identifiers": [{"type": "domain", "value": "c.com"}],
                },
            ],
            "revoked_publisher_domains": [{"publisher_domain": "b.com"}],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "fanout",
                    "publisher_properties": [
                        {
                            "publisher_domains": ["a.com", "b.com", "c.com"],
                            "selection_type": "all",
                        },
                    ],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert {p["property_id"] for p in properties} == {"a-1", "c-1"}

    def test_unknown_selection_type_returns_empty(self):
        """Unknown selection_type fails closed (no fallback authorization)."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "x",
                    "publisher_domain": "site.com",
                    "property_type": "website",
                    "name": "X",
                    "identifiers": [{"type": "domain", "value": "site.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "unknown",
                    "publisher_properties": [
                        {
                            "publisher_domain": "site.com",
                            "selection_type": "by_category",
                        },
                    ],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert properties == []

    def test_by_tag_with_empty_property_tags_returns_empty(self):
        """selection_type=by_tag with empty property_tags resolves to [] (fail-closed)."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "x",
                    "publisher_domain": "site.com",
                    "property_type": "website",
                    "name": "X",
                    "identifiers": [{"type": "domain", "value": "site.com"}],
                    "tags": ["ctv"],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "empty tags",
                    "publisher_properties": [
                        {
                            "publisher_domain": "site.com",
                            "selection_type": "by_tag",
                            "property_tags": [],
                        },
                    ],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert properties == []

    def test_property_missing_property_id_raises(self):
        """Matching property without property_id raises AdagentsValidationError (fail-fast)."""
        adagents_data = {
            "properties": [
                {
                    # no property_id
                    "publisher_domain": "site.com",
                    "property_type": "website",
                    "name": "X",
                    "identifiers": [{"type": "domain", "value": "site.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "missing id",
                    "publisher_properties": [
                        {
                            "publisher_domain": "site.com",
                            "selection_type": "all",
                        },
                    ],
                },
            ],
        }

        with pytest.raises(AdagentsValidationError, match="missing required 'property_id'"):
            get_properties_by_agent(adagents_data, "https://agent1.example.com")

    def test_get_properties_by_agent_cafemedia_scale(self):
        """6,843 inline properties across 6,800 child domains under one publisher_domains[].

        Wall-clock-bounded to catch O(N×M) regressions in the resolver.
        At this scale a naive per-domain linear scan over the property list
        is roughly 46M comparisons; the indexed path is ~6,843 + 6,800 ops.
        """
        import time

        child_domains = [f"site{i}.example" for i in range(6800)]
        # 6,843 total properties across the 6,800 child domains: one per
        # domain, plus 43 extra properties on the first 43 domains
        # (mirrors a real publisher's mix where some child domains host
        # multiple inventory entries — e.g., site + ctv app).
        properties = [
            {
                "property_id": f"p-{i}",
                "publisher_domain": child_domains[i],
                "property_type": "website",
                "name": f"Site {i}",
                "identifiers": [{"type": "domain", "value": child_domains[i]}],
                "tags": ["raptive_managed"],
            }
            for i in range(6800)
        ] + [
            {
                "property_id": f"p-extra-{i}",
                "publisher_domain": child_domains[i],
                "property_type": "ctv_app",
                "name": f"Site {i} CTV",
                "identifiers": [{"type": "bundle_id", "value": f"com.site{i}.ctv"}],
                "tags": ["raptive_managed"],
            }
            for i in range(43)
        ]
        adagents_data = {
            "properties": properties,
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "scale",
                    "publisher_properties": [
                        {
                            "publisher_domains": child_domains,
                            "selection_type": "by_tag",
                            "property_tags": ["raptive_managed"],
                        },
                    ],
                },
            ],
        }

        start = time.perf_counter()
        result = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        elapsed = time.perf_counter() - start

        assert elapsed < 5.0, f"resolution took {elapsed:.2f}s (>= 5.0s budget)"
        assert len(result) == 6843
        assert {p["publisher_domain"] for p in result} == set(child_domains)

    def test_malformed_property_tags_value_resolves_empty(self):
        """publisher_properties selector with property_tags as a STRING resolves to [].

        Without the isinstance(list) guard, ``property_tags: "ctv"`` iterates
        char-by-char and matches properties tagged ``"c"``/``"t"``/``"v"``.
        The resolver must fail-closed on malformed input.
        """
        adagents_data = {
            "properties": [
                {
                    "property_id": "p1",
                    "publisher_domain": "site1.example",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.example"}],
                    "tags": ["c"],
                },
                {
                    "property_id": "p2",
                    "publisher_domain": "site1.example",
                    "property_type": "website",
                    "name": "Site 2",
                    "identifiers": [{"type": "domain", "value": "site2.example"}],
                    "tags": ["t"],
                },
                {
                    "property_id": "p3",
                    "publisher_domain": "site1.example",
                    "property_type": "website",
                    "name": "Site 3",
                    "identifiers": [{"type": "domain", "value": "site3.example"}],
                    "tags": ["v"],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "Test",
                    "publisher_properties": [
                        {
                            "publisher_domain": "site1.example",
                            "selection_type": "by_tag",
                            "property_tags": "ctv",  # malformed: string, not list
                        },
                    ],
                },
            ],
        }

        result = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert result == []

    def test_get_all_properties_builds_index_once(self):
        """_build_domain_index runs once per file, not once per agent.

        At N agents × M properties, rebuilding the index inside
        _resolve_agent_properties is O(agents × M). This test patches the
        helper with a counter and asserts a single invocation across a
        file with multiple publisher_properties agents.
        """
        from unittest.mock import patch

        from adcp import adagents as adagents_module

        adagents_data = {
            "properties": [
                {
                    "property_id": "p1",
                    "publisher_domain": "site1.example",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.example"}],
                    "tags": ["managed"],
                },
                {
                    "property_id": "p2",
                    "publisher_domain": "site2.example",
                    "property_type": "website",
                    "name": "Site 2",
                    "identifiers": [{"type": "domain", "value": "site2.example"}],
                    "tags": ["managed"],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "A",
                    "publisher_properties": [
                        {
                            "publisher_domain": "site1.example",
                            "selection_type": "all",
                        },
                    ],
                },
                {
                    "url": "https://agent2.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "B",
                    "publisher_properties": [
                        {
                            "publisher_domain": "site2.example",
                            "selection_type": "all",
                        },
                    ],
                },
                {
                    "url": "https://agent3.example.com",
                    "authorization_type": "publisher_properties",
                    "authorized_for": "C",
                    "publisher_properties": [
                        {
                            "publisher_domain": "site1.example",
                            "selection_type": "by_tag",
                            "property_tags": ["managed"],
                        },
                    ],
                },
            ],
        }

        original = adagents_module._build_domain_index
        with patch.object(
            adagents_module,
            "_build_domain_index",
            side_effect=original,
        ) as spy:
            result = get_all_properties(adagents_data)

        assert spy.call_count == 1, (
            f"_build_domain_index called {spy.call_count} times; "
            "expected exactly once per get_all_properties invocation"
        )
        # Sanity: index actually reused — all three agents resolved.
        agent_urls = {p["agent_url"] for p in result}
        assert agent_urls == {
            "https://agent1.example.com",
            "https://agent2.example.com",
            "https://agent3.example.com",
        }

    def test_get_properties_by_agent_protocol_agnostic(self):
        """Should match agent URL regardless of protocol."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        }
                    ],
                }
            ]
        }

        properties = get_properties_by_agent(adagents_data, "http://agent1.example.com")
        assert len(properties) == 1
        assert properties[0]["name"] == "Site 1"

    def test_get_properties_by_agent_not_found(self):
        """Should return empty list for unknown agent."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "site1.com"}],
                        }
                    ],
                }
            ]
        }

        properties = get_properties_by_agent(adagents_data, "https://unknown-agent.com")
        assert len(properties) == 0

    def test_get_properties_by_agent_no_top_level_properties(self):
        """Should return empty list when using property_ids/tags but no top-level props."""
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "property_ids",
                    "authorized_for": "Test",
                    "property_ids": ["site1"],
                },
            ],
        }

        properties = get_properties_by_agent(adagents_data, "https://agent1.example.com")
        assert len(properties) == 0

    def test_resolve_properties_for_agent_strict_keeps_bare_entries_empty(self):
        """Strict resolution preserves get_properties_by_agent behavior for bare entries."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorized_for": "All listed properties",
                },
            ],
        }

        assert get_properties_by_agent(adagents_data, "https://agent1.example.com") == []
        assert resolve_properties_for_agent(adagents_data, "https://agent1.example.com") == []

    def test_resolve_properties_for_agent_permissive_bare_entry_uses_top_level_properties(self):
        """Permissive mode treats a matching bare entry as authorizing top-level properties."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                },
                {
                    "property_id": "app1",
                    "property_type": "mobile_app",
                    "name": "App 1",
                    "identifiers": [{"type": "bundle_id", "value": "com.example.app"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorized_for": "All listed properties",
                },
            ],
        }

        properties = resolve_properties_for_agent(
            adagents_data,
            "https://agent1.example.com",
            mode="permissive",
        )
        assert [p["property_id"] for p in properties] == ["site1", "app1"]

    def test_resolve_properties_for_agent_permissive_still_requires_matching_agent(self):
        """Permissive mode does not expose properties when the agent is not listed."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorized_for": "All listed properties",
                },
            ],
        }

        properties = resolve_properties_for_agent(
            adagents_data,
            "https://other-agent.example.com",
            mode="permissive",
        )
        assert properties == []

    def test_resolve_properties_for_agent_permissive_does_not_override_selectors(self):
        """Permissive fallback applies only to bare entries, not broken explicit selectors."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorized_for": "Explicit but broken selector",
                    "property_ids": ["site1"],
                },
            ],
        }

        properties = resolve_properties_for_agent(
            adagents_data,
            "https://agent1.example.com",
            mode="permissive",
        )
        assert properties == []

    def test_resolve_properties_for_agent_permissive_rejects_missing_authorized_for(self):
        """Permissive fallback requires the exact legacy bare entry shape."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                },
            ],
        }

        properties = resolve_properties_for_agent(
            adagents_data,
            "https://agent1.example.com",
            mode="permissive",
        )
        assert properties == []

    def test_resolve_properties_for_agent_permissive_rejects_unknown_selector_fields(self):
        """Permissive fallback does not treat typoed selectors as all-property auth."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorized_for": "Typoed selector",
                    "property_idz": ["site1"],
                },
            ],
        }

        properties = resolve_properties_for_agent(
            adagents_data,
            "https://agent1.example.com",
            mode="permissive",
        )
        assert properties == []

    def test_resolve_properties_for_agent_permissive_prefers_explicit_duplicate_match(self):
        """A stale bare row cannot broaden a later explicit same-agent selector."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "property_type": "website",
                    "name": "Site 1",
                    "identifiers": [{"type": "domain", "value": "site1.com"}],
                },
                {
                    "property_id": "site2",
                    "property_type": "website",
                    "name": "Site 2",
                    "identifiers": [{"type": "domain", "value": "site2.com"}],
                },
            ],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorized_for": "Legacy broad row",
                },
                {
                    "url": "https://agent1.example.com",
                    "authorization_type": "property_ids",
                    "authorized_for": "Restricted row",
                    "property_ids": ["site1"],
                },
            ],
        }

        properties = resolve_properties_for_agent(
            adagents_data,
            "https://agent1.example.com",
            mode="permissive",
        )
        assert [p["property_id"] for p in properties] == ["site1"]

    def test_resolve_properties_for_agent_permissive_honors_revoked_domains(self):
        """Bare-entry fallback still excludes top-level properties revoked by the file."""
        adagents_data = {
            "properties": [
                {
                    "property_id": "site1",
                    "publisher_domain": "active.example",
                    "property_type": "website",
                    "name": "Active",
                    "identifiers": [{"type": "domain", "value": "active.example"}],
                },
                {
                    "property_id": "site2",
                    "publisher_domain": "revoked.example",
                    "property_type": "website",
                    "name": "Revoked",
                    "identifiers": [{"type": "domain", "value": "revoked.example"}],
                },
            ],
            "revoked_publisher_domains": [{"publisher_domain": "revoked.example"}],
            "authorized_agents": [
                {
                    "url": "https://agent1.example.com",
                    "authorized_for": "All listed properties",
                },
            ],
        }

        properties = resolve_properties_for_agent(
            adagents_data,
            "https://agent1.example.com",
            mode="permissive",
        )
        assert [p["property_id"] for p in properties] == ["site1"]

    def test_resolve_properties_for_agent_rejects_unknown_mode(self):
        """Unknown resolver modes fail loudly instead of silently broadening access."""
        with pytest.raises(ValueError, match="mode"):
            resolve_properties_for_agent(
                {"authorized_agents": []},
                "https://agent1.example.com",
                mode="loose",
            )


class TestAuthorizationContext:
    """Test AuthorizationContext class."""

    def test_extract_property_ids(self):
        """Should extract property IDs from properties using property_id field."""
        properties = [
            {
                "property_id": "prop1",
                "property_type": "website",
                "name": "Site 1",
                "identifiers": [{"type": "domain", "value": "site1.com"}],
            },
            {
                "property_id": "prop2",
                "property_type": "mobile_app",
                "name": "App 1",
                "identifiers": [{"type": "bundle_id", "value": "com.site1.app"}],
            },
        ]

        ctx = AuthorizationContext(properties)
        assert ctx.property_ids == ["prop1", "prop2"]

    def test_extract_property_tags(self):
        """Should extract unique tags from properties."""
        properties = [
            {
                "property_id": "prop1",
                "property_type": "website",
                "name": "Site 1",
                "tags": ["premium", "news"],
            },
            {
                "property_id": "prop2",
                "property_type": "website",
                "name": "Site 2",
                "tags": ["premium", "sports"],
            },
        ]

        ctx = AuthorizationContext(properties)
        assert set(ctx.property_tags) == {"premium", "news", "sports"}

    def test_deduplicate_tags(self):
        """Should deduplicate tags."""
        properties = [
            {
                "property_id": "prop1",
                "tags": ["premium", "news"],
            },
            {
                "property_id": "prop2",
                "tags": ["premium", "sports"],
            },
        ]

        ctx = AuthorizationContext(properties)
        # Each tag should appear only once
        assert ctx.property_tags.count("premium") == 1

    def test_handle_missing_fields(self):
        """Should handle properties without property_id or tags."""
        properties = [
            {
                "property_type": "website",
                "name": "Site 1",
            }
        ]

        ctx = AuthorizationContext(properties)
        assert ctx.property_ids == []
        assert ctx.property_tags == []

    def test_raw_properties_preserved(self):
        """Should preserve raw properties data."""
        properties = [
            {
                "property_id": "prop1",
                "property_type": "website",
                "name": "Site 1",
                "custom_field": "custom_value",
            }
        ]

        ctx = AuthorizationContext(properties)
        assert ctx.raw_properties == properties
        assert ctx.raw_properties[0]["custom_field"] == "custom_value"

    def test_repr(self):
        """Should have useful string representation."""
        properties = [
            {
                "property_id": "prop1",
                "tags": ["premium"],
            }
        ]

        ctx = AuthorizationContext(properties)
        repr_str = repr(ctx)
        assert "AuthorizationContext" in repr_str
        assert "property_ids" in repr_str
        assert "property_tags" in repr_str


@pytest.mark.asyncio
class TestFetchAgentAuthorizations:
    """Test fetch_agent_authorizations function."""

    async def test_single_publisher_authorized(self):
        """Should return authorization context for authorized publisher."""
        from unittest.mock import patch

        # Mock adagents.json data
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_id": "prop1",
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "nytimes.com"}],
                            "tags": ["premium", "news"],
                        }
                    ],
                }
            ]
        }

        # Mock fetch_adagents to return our test data
        with patch("adcp.adagents.fetch_adagents", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = adagents_data

            contexts = await fetch_agent_authorizations("https://our-agent.com", ["nytimes.com"])

            assert len(contexts) == 1
            assert "nytimes.com" in contexts
            ctx = contexts["nytimes.com"]
            assert ctx.property_ids == ["prop1"]
            assert "premium" in ctx.property_tags
            assert "news" in ctx.property_tags

    async def test_multiple_publishers(self):
        """Should fetch and return contexts for multiple publishers in parallel."""
        from unittest.mock import patch

        # Mock adagents.json data for different publishers
        nytimes_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_id": "nyt_prop1",
                            "property_type": "website",
                            "name": "NYT Site",
                            "identifiers": [{"type": "domain", "value": "nytimes.com"}],
                            "tags": ["news"],
                        }
                    ],
                }
            ]
        }

        wsj_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_id": "wsj_prop1",
                            "property_type": "website",
                            "name": "WSJ Site",
                            "identifiers": [{"type": "domain", "value": "wsj.com"}],
                            "tags": ["business"],
                        }
                    ],
                }
            ]
        }

        async def mock_fetch_adagents(domain, **kwargs):
            if domain == "nytimes.com":
                return nytimes_data
            elif domain == "wsj.com":
                return wsj_data
            else:
                raise Exception("Unexpected domain")

        with patch("adcp.adagents.fetch_adagents", side_effect=mock_fetch_adagents):
            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            assert len(contexts) == 2
            assert "nytimes.com" in contexts
            assert "wsj.com" in contexts
            assert contexts["nytimes.com"].property_ids == ["nyt_prop1"]
            assert contexts["wsj.com"].property_ids == ["wsj_prop1"]

    async def test_skip_unauthorized_publishers(self):
        """Should skip publishers where agent is not authorized."""
        from unittest.mock import patch

        # nytimes authorizes our agent
        nytimes_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_id": "prop1",
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "nytimes.com"}],
                        }
                    ],
                }
            ]
        }

        # wsj does NOT authorize our agent
        wsj_data = {
            "authorized_agents": [
                {
                    "url": "https://different-agent.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_id": "prop2",
                            "property_type": "website",
                            "name": "Site 2",
                            "identifiers": [{"type": "domain", "value": "wsj.com"}],
                        }
                    ],
                }
            ]
        }

        async def mock_fetch_adagents(domain, **kwargs):
            if domain == "nytimes.com":
                return nytimes_data
            elif domain == "wsj.com":
                return wsj_data
            else:
                raise Exception("Unexpected domain")

        with patch("adcp.adagents.fetch_adagents", side_effect=mock_fetch_adagents):
            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            # Should only include nytimes
            assert len(contexts) == 1
            assert "nytimes.com" in contexts
            assert "wsj.com" not in contexts

    async def test_skip_missing_adagents_json(self):
        """Should silently skip publishers with missing adagents.json."""
        from unittest.mock import patch

        from adcp.exceptions import AdagentsNotFoundError

        # nytimes has adagents.json
        nytimes_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_id": "prop1",
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "nytimes.com"}],
                        }
                    ],
                }
            ]
        }

        async def mock_fetch_adagents(domain, **kwargs):
            if domain == "nytimes.com":
                return nytimes_data
            elif domain == "wsj.com":
                # wsj doesn't have adagents.json (404)
                raise AdagentsNotFoundError("wsj.com")
            else:
                raise Exception("Unexpected domain")

        with patch("adcp.adagents.fetch_adagents", side_effect=mock_fetch_adagents):
            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            # Should only include nytimes
            assert len(contexts) == 1
            assert "nytimes.com" in contexts
            assert "wsj.com" not in contexts

    async def test_skip_invalid_adagents_json(self):
        """Should silently skip publishers with invalid adagents.json."""
        from unittest.mock import patch

        from adcp.exceptions import AdagentsValidationError

        nytimes_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_id": "prop1",
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "nytimes.com"}],
                        }
                    ],
                }
            ]
        }

        async def mock_fetch_adagents(domain, **kwargs):
            if domain == "nytimes.com":
                return nytimes_data
            elif domain == "wsj.com":
                # wsj has invalid adagents.json
                raise AdagentsValidationError("Invalid JSON")
            else:
                raise Exception("Unexpected domain")

        with patch("adcp.adagents.fetch_adagents", side_effect=mock_fetch_adagents):
            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            # Should only include nytimes
            assert len(contexts) == 1
            assert "nytimes.com" in contexts
            assert "wsj.com" not in contexts

    async def test_empty_result_when_no_authorizations(self):
        """Should return empty dict when no publishers authorize the agent."""
        from unittest.mock import patch

        # No publishers authorize our agent
        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://different-agent.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_id": "prop1",
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "example.com"}],
                        }
                    ],
                }
            ]
        }

        with patch("adcp.adagents.fetch_adagents", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = adagents_data

            contexts = await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com", "wsj.com"]
            )

            assert len(contexts) == 0
            assert contexts == {}

    async def test_uses_provided_http_client(self):
        """Should use provided HTTP client for connection pooling."""
        from unittest.mock import MagicMock, patch

        adagents_data = {
            "authorized_agents": [
                {
                    "url": "https://our-agent.com",
                    "authorization_type": "inline_properties",
                    "authorized_for": "Test",
                    "properties": [
                        {
                            "property_id": "prop1",
                            "property_type": "website",
                            "name": "Site 1",
                            "identifiers": [{"type": "domain", "value": "nytimes.com"}],
                        }
                    ],
                }
            ]
        }

        mock_client = MagicMock(spec=httpx.AsyncClient)

        with patch("adcp.adagents.fetch_adagents", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = adagents_data

            await fetch_agent_authorizations(
                "https://our-agent.com", ["nytimes.com"], client=mock_client
            )

            # Verify fetch_adagents was called with the provided client
            mock_fetch.assert_called_once()
            call_kwargs = mock_fetch.call_args[1]
            assert call_kwargs.get("client") == mock_client


class TestParseManagerdomains:
    """Test ads.txt MANAGERDOMAIN directive parsing."""

    def test_basic_directive(self):
        from adcp.adagents import _parse_managerdomains

        assert _parse_managerdomains("MANAGERDOMAIN=manager.example\n") == ["manager.example"]

    def test_case_insensitive_keyword(self):
        from adcp.adagents import _parse_managerdomains

        assert _parse_managerdomains("managerdomain=Manager.Example\n") == ["manager.example"]

    def test_pure_comment_line_rejected(self):
        from adcp.adagents import _parse_managerdomains

        assert _parse_managerdomains("# managerdomain=foo.example\n") == []
        assert _parse_managerdomains("#managerdomain=foo.example\n") == []

    def test_duplicates_preserved_in_order(self):
        from adcp.adagents import _parse_managerdomains

        assert _parse_managerdomains(
            "MANAGERDOMAIN=first.example\nMANAGERDOMAIN=second.example\n"
        ) == ["first.example", "second.example"]

    def test_inline_comment_after_directive(self):
        from adcp.adagents import _parse_managerdomains

        assert _parse_managerdomains("MANAGERDOMAIN=ok.example # comment\n") == ["ok.example"]

    def test_whitespace_around_equals(self):
        from adcp.adagents import _parse_managerdomains

        assert _parse_managerdomains("MANAGERDOMAIN  =  spaced.example\n") == ["spaced.example"]

    def test_non_managerdomain_lines_ignored(self):
        from adcp.adagents import _parse_managerdomains

        ads_txt = (
            "google.com, pub-1234, DIRECT, abc123\n"
            "MANAGERDOMAIN=manager.example\n"
            "appnexus.com, 5678, RESELLER\n"
        )
        assert _parse_managerdomains(ads_txt) == ["manager.example"]


class TestValidateAdagentsDomain:
    """Test validate_adagents_domain typed validator with discovery_method."""

    def _build_mock_client(self, url_handler):
        """Mock client backing both .stream() (adagents) and .get() (ads.txt).

        ``url_handler(url)`` returns a MagicMock built by ``_ok`` /
        ``_not_found`` / ``_text``. For adagents URLs we adapt that
        legacy-style response into a stream-capable mock; for ads.txt
        URLs the legacy ``.get()``/``.text`` path is preserved since the
        ads.txt fetch never went through the new streaming code.
        """

        def _stream(method, url, **kwargs):
            response = url_handler(url)
            body_data = response.json.return_value if response.status_code == 200 else None
            body = json.dumps(body_data).encode("utf-8") if body_data else b""
            stream_response = _make_stream_response(status_code=response.status_code, body=body)
            return _stream_cm(stream_response)

        async def mock_get(url, **kwargs):
            return url_handler(url)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(side_effect=_stream)
        mock_client.get = mock_get
        return mock_client

    def _ok(self, payload, status=200):
        response = MagicMock()
        response.status_code = status
        response.json.return_value = payload
        response.text = ""
        return response

    def _not_found(self):
        response = MagicMock()
        response.status_code = 404
        response.json.return_value = {}
        response.text = ""
        return response

    def _text(self, body, status=200):
        response = MagicMock()
        response.status_code = status
        response.text = body
        response.content = body.encode("utf-8") if isinstance(body, str) else body
        response.json.return_value = {}
        return response

    @pytest.mark.asyncio
    async def test_direct_discovery(self):
        from adcp.adagents import validate_adagents_domain

        adagents = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }

        def handler(url):
            if url.endswith("/.well-known/adagents.json"):
                return self._ok(adagents)
            raise AssertionError(f"unexpected url {url}")

        result = await validate_adagents_domain(
            "publisher.example", client=self._build_mock_client(handler)
        )

        assert result.valid is True
        assert result.discovery_method == "direct"
        assert result.manager_domain is None
        assert result.domain == "publisher.example"
        assert result.url == "https://publisher.example/.well-known/adagents.json"
        assert result.data == adagents

    @pytest.mark.asyncio
    async def test_authoritative_location_discovery(self):
        import adcp.adagents as adagents_module
        from adcp.adagents import validate_adagents_domain

        redirect = {
            "authoritative_location": "https://cdn.example.com/adagents.json",
        }
        resolved = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }

        # Initial fetch (publisher) returns the redirect stub.
        def handler(url):
            return self._ok(redirect)

        redirect_client = make_url_dispatching_client(
            {"https://cdn.example.com/adagents.json": resolved},
        )

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: redirect_client
        ):
            result = await validate_adagents_domain(
                "publisher.example", client=self._build_mock_client(handler)
            )

        assert result.valid is True
        assert result.discovery_method == "authoritative_location"
        assert result.manager_domain is None
        assert result.data == resolved

    @pytest.mark.asyncio
    async def test_ads_txt_managerdomain_fallback(self):
        import adcp.adagents as adagents_module
        from adcp.adagents import validate_adagents_domain

        manager_adagents = {
            "authorized_agents": [
                {
                    "url": "https://agent.example",
                    "authorized_for": "Managed inventory",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }

        # Initial client serves publisher endpoints (adagents 404, ads.txt 200).
        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._not_found()
            if url == "https://publisher.example/ads.txt":
                return self._text("MANAGERDOMAIN=manager.example\n")
            raise AssertionError(f"unexpected url {url}")

        manager_client = make_url_dispatching_client(
            {"https://manager.example/.well-known/adagents.json": manager_adagents},
        )

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: manager_client
        ):
            result = await validate_adagents_domain(
                "publisher.example", client=self._build_mock_client(handler)
            )

        assert result.valid is True
        assert result.discovery_method == "ads_txt_managerdomain"
        assert result.manager_domain == "manager.example"
        assert result.domain == "publisher.example"
        assert result.data == manager_adagents

    @pytest.mark.asyncio
    async def test_comment_form_managerdomain_not_followed(self):
        from adcp.adagents import validate_adagents_domain

        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._not_found()
            if url == "https://publisher.example/ads.txt":
                return self._text("# managerdomain=comment-only.example\n")
            raise AssertionError(f"unexpected url {url}")

        result = await validate_adagents_domain(
            "publisher.example", client=self._build_mock_client(handler)
        )

        assert result.valid is False
        # No fallback was attempted, so discovery_method stays at default 'direct'.
        assert result.discovery_method == "direct"
        assert result.manager_domain is None

    @pytest.mark.asyncio
    async def test_duplicate_managerdomain_last_wins(self):
        import adcp.adagents as adagents_module
        from adcp.adagents import validate_adagents_domain

        manager_adagents = {
            "authorized_agents": [
                {
                    "url": "https://agent.example",
                    "authorized_for": "All",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }

        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._not_found()
            if url == "https://publisher.example/ads.txt":
                return self._text(
                    "MANAGERDOMAIN=bad-manager.example\n" "MANAGERDOMAIN=good-manager.example\n"
                )
            raise AssertionError(f"unexpected url {url}")

        attempted_urls: list[str] = []
        manager_client = make_url_dispatching_client(
            {"https://good-manager.example/.well-known/adagents.json": manager_adagents},
            called_urls=attempted_urls,
        )

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: manager_client
        ):
            result = await validate_adagents_domain(
                "publisher.example", client=self._build_mock_client(handler)
            )

        assert result.valid is True
        assert result.discovery_method == "ads_txt_managerdomain"
        assert result.manager_domain == "good-manager.example"
        assert "https://good-manager.example/.well-known/adagents.json" in attempted_urls
        assert (
            "https://bad-manager.example/.well-known/adagents.json" not in attempted_urls
        ), "bad-manager.example must not be tried; last entry wins"

    @pytest.mark.asyncio
    async def test_manager_domain_404_is_terminal_failure(self):
        import adcp.adagents as adagents_module
        from adcp.adagents import validate_adagents_domain

        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._not_found()
            if url == "https://publisher.example/ads.txt":
                return self._text("MANAGERDOMAIN=manager.example\n")
            raise AssertionError(f"unexpected url {url}")

        manager_client = make_url_dispatching_client({})  # always 404

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: manager_client
        ):
            result = await validate_adagents_domain(
                "publisher.example", client=self._build_mock_client(handler)
            )

        assert result.valid is False
        # Provenance is preserved on failure so callers can diagnose.
        assert result.discovery_method == "ads_txt_managerdomain"
        assert result.manager_domain == "manager.example"
        assert result.data is None
        assert any("manager.example" in err for err in result.errors)

    @pytest.mark.asyncio
    async def test_managerdomain_pointing_at_private_ip_is_rejected(self):
        # A malicious publisher could declare MANAGERDOMAIN=169.254.169.254
        # (AWS IMDS) to force the SDK into an SSRF. The manager-domain
        # gate must reject private/reserved hosts before any fetch.
        from adcp.adagents import validate_adagents_domain

        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._not_found()
            if url == "https://publisher.example/ads.txt":
                return self._text("MANAGERDOMAIN=169.254.169.254\n")
            raise AssertionError(f"unexpected url {url}")

        result = await validate_adagents_domain(
            "publisher.example", client=self._build_mock_client(handler)
        )

        assert result.valid is False
        assert result.manager_domain is None
        assert any("private/reserved" in err for err in result.errors)

    @pytest.mark.asyncio
    async def test_managerdomain_pointing_at_loopback_is_rejected(self):
        from adcp.adagents import validate_adagents_domain

        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._not_found()
            if url == "https://publisher.example/ads.txt":
                return self._text("MANAGERDOMAIN=127.0.0.1\n")
            raise AssertionError(f"unexpected url {url}")

        result = await validate_adagents_domain(
            "publisher.example", client=self._build_mock_client(handler)
        )

        assert result.valid is False
        assert result.manager_domain is None

    @pytest.mark.asyncio
    async def test_oversized_ads_txt_is_discarded(self):
        # A hostile publisher serving a multi-MB ads.txt should not force
        # the SDK to buffer arbitrary data — the cap silently drops the body.
        from adcp.adagents import MAX_ADS_TXT_BYTES, validate_adagents_domain

        oversized = "MANAGERDOMAIN=manager.example\n" + ("# pad\n" * MAX_ADS_TXT_BYTES)

        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._not_found()
            if url == "https://publisher.example/ads.txt":
                response = MagicMock()
                response.status_code = 200
                response.text = oversized
                response.content = oversized.encode("utf-8")
                response.json.return_value = {}
                return response
            raise AssertionError(f"unexpected url {url}")

        result = await validate_adagents_domain(
            "publisher.example", client=self._build_mock_client(handler)
        )

        # Oversized body is discarded, so no MANAGERDOMAIN was parsed — the
        # result reflects the original direct 404 with no manager fallback.
        assert result.valid is False
        assert result.manager_domain is None

    @pytest.mark.asyncio
    async def test_ads_txt_30x_is_not_followed(self):
        # ads.txt fetch uses follow_redirects=False to match adagents.json;
        # a 30x response from the publisher therefore falls through to
        # "no MANAGERDOMAIN parsed" rather than transparently chasing the
        # Location header (which would bypass the SSRF gate).
        from adcp.adagents import validate_adagents_domain

        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._not_found()
            if url == "https://publisher.example/ads.txt":
                response = MagicMock()
                response.status_code = 302
                response.headers = httpx.Headers({"location": "https://127.0.0.1/ads.txt"})
                response.text = ""
                response.content = b""
                response.json.return_value = {}
                return response
            raise AssertionError(f"unexpected url {url}")

        result = await validate_adagents_domain(
            "publisher.example", client=self._build_mock_client(handler)
        )

        # A 30x ads.txt is treated as "no managerdomain", so the result
        # is the publisher's original 404 with no manager fallback.
        assert result.valid is False
        assert result.manager_domain is None

    @pytest.mark.asyncio
    async def test_redirect_target_404_does_not_trigger_managerdomain_fallback(self):
        # A 404 on a publisher-named authoritative_location target is a
        # broken redirect chain, not a missing publisher manifest, and
        # must not fall through to the publisher's ads.txt MANAGERDOMAIN
        # (which is a different trust path).
        import adcp.adagents as adagents_module
        from adcp.adagents import fetch_adagents
        from adcp.exceptions import AdagentsValidationError

        redirect = {"authoritative_location": "https://cdn.example.com/adagents.json"}

        ads_txt_consulted: list[str] = []

        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._ok(redirect)
            if url == "https://publisher.example/ads.txt":
                ads_txt_consulted.append(url)
                return self._text("MANAGERDOMAIN=manager.example\n")
            raise AssertionError(f"unexpected url {url}")

        redirect_client = make_url_dispatching_client({})  # always 404

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: redirect_client
        ):
            with pytest.raises(AdagentsValidationError, match="authoritative_location"):
                await fetch_adagents("publisher.example", client=self._build_mock_client(handler))

        # ads.txt should NEVER be consulted on a redirect-target 404.
        assert ads_txt_consulted == []

    @pytest.mark.asyncio
    async def test_managerdomain_cycle_to_source_publisher(self):
        from adcp.adagents import validate_adagents_domain

        def handler(url):
            if url == "https://publisher.example/.well-known/adagents.json":
                return self._not_found()
            if url == "https://publisher.example/ads.txt":
                return self._text("MANAGERDOMAIN=publisher.example\n")
            raise AssertionError(f"unexpected url {url}")

        result = await validate_adagents_domain(
            "publisher.example", client=self._build_mock_client(handler)
        )

        assert result.valid is False
        # No fallback hop is attempted, so discovery_method remains default.
        assert result.manager_domain is None
        assert any("points back" in err for err in result.errors)


class TestValidateAdagentsStructure:
    """Per-entry schema validation of pre-fetched adagents.json data.

    The key property under test: ``validate_adagents_structure``
    distinguishes a schema-invalid file (the wonderstruck-style bare
    entry case in issue #707) from a valid file where the caller's
    agent is simply not listed. ``get_properties_by_agent`` collapses
    both into ``[]`` — this validator does not.
    """

    def test_fully_valid_file_with_property_tags(self):
        from adcp.adagents import validate_adagents_structure

        data = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All tagged inventory",
                    "authorization_type": "property_tags",
                    "property_tags": ["premium"],
                }
            ],
            "properties": [
                {
                    "property_id": "main",
                    "property_type": "website",
                    "name": "Main",
                    "identifiers": [{"type": "domain", "value": "example.com"}],
                    "tags": ["premium"],
                }
            ],
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is True
        assert result.errors == []
        assert result.authorized_agents_count == 1
        assert result.properties_count == 1

    def test_valid_with_all_six_authorization_types(self):
        from adcp.adagents import validate_adagents_structure

        data = {
            "authorized_agents": [
                {
                    "url": "https://a.example.com",
                    "authorized_for": "by id",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                },
                {
                    "url": "https://b.example.com",
                    "authorized_for": "by tag",
                    "authorization_type": "property_tags",
                    "property_tags": ["t1"],
                },
                {
                    "url": "https://c.example.com",
                    "authorized_for": "inline",
                    "authorization_type": "inline_properties",
                    "properties": [{"property_id": "p2", "property_type": "website"}],
                },
                {
                    "url": "https://d.example.com",
                    "authorized_for": "cross-publisher",
                    "authorization_type": "publisher_properties",
                    "publisher_properties": [
                        {
                            "publisher_domain": "other.example.com",
                            "selection_type": "by_id",
                            "property_ids": ["p3"],
                        }
                    ],
                },
                {
                    "url": "https://e.example.com",
                    "authorized_for": "signals by id",
                    "authorization_type": "signal_ids",
                    "signal_ids": ["s1"],
                },
                {
                    "url": "https://f.example.com",
                    "authorized_for": "signals by tag",
                    "authorization_type": "signal_tags",
                    "signal_tags": ["t1"],
                },
            ]
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is True
        assert result.errors == []
        assert result.authorized_agents_count == 6

    def test_bare_entries_missing_authorization_type(self):
        """The wonderstruck.org case from issue #707 — entries with only
        url + authorized_for, no discriminator. The SDK currently treats
        these as "authorizes nothing" via ``get_properties_by_agent``;
        this validator must distinguish them from a valid-but-unlisted
        agent.
        """
        from adcp.adagents import validate_adagents_structure

        data = {
            "$schema": "https://adcontextprotocol.org/schemas/v1/adagents.json",
            "authorized_agents": [
                {
                    "url": "https://wonderstruck.sales-agent.scope3.com",
                    "authorized_for": "Authorized for display banners",
                },
                {
                    "url": "https://interchange.io",
                    "authorized_for": "Authorized for display banners",
                },
            ],
            "properties": [
                {
                    "property_id": "main_site",
                    "property_type": "website",
                    "name": "Main site",
                    "identifiers": [{"type": "domain", "value": "wonderstruck.org"}],
                    "tags": ["sites"],
                }
            ],
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is False
        assert len(result.errors) == 2
        assert all(err.kind == "missing_authorization_type" for err in result.errors)
        assert [err.index for err in result.errors] == [0, 1]
        assert result.errors[0].url == "https://wonderstruck.sales-agent.scope3.com"
        assert result.authorized_agents_count == 2
        assert result.properties_count == 1

    def test_unknown_authorization_type(self):
        from adcp.adagents import validate_adagents_structure

        data = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "x",
                    "authorization_type": "everything",
                    "property_ids": ["p1"],
                }
            ]
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is False
        assert len(result.errors) == 1
        assert result.errors[0].kind == "unknown_authorization_type"

    def test_missing_selector_for_type(self):
        from adcp.adagents import validate_adagents_structure

        data = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "x",
                    "authorization_type": "property_tags",
                    "property_ids": ["p1"],  # wrong selector for type
                }
            ]
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is False
        assert len(result.errors) == 1
        assert result.errors[0].kind == "missing_selector_for_type"
        assert "property_tags" in result.errors[0].message

    def test_empty_selector_array_is_invalid(self):
        from adcp.adagents import validate_adagents_structure

        data = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "x",
                    "authorization_type": "property_ids",
                    "property_ids": [],  # schema requires minItems: 1
                }
            ]
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is False
        assert result.errors[0].kind == "missing_selector_for_type"

    def test_missing_url_and_authorized_for(self):
        from adcp.adagents import validate_adagents_structure

        data = {
            "authorized_agents": [
                {
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is False
        kinds = {err.kind for err in result.errors}
        assert kinds == {"missing_url", "missing_authorized_for"}

    def test_non_object_entry(self):
        from adcp.adagents import validate_adagents_structure

        data = {"authorized_agents": ["not-an-object", None]}

        result = validate_adagents_structure(data)

        assert result.schema_valid is False
        assert [err.kind for err in result.errors] == ["not_an_object", "not_an_object"]

    def test_authoritative_location_variant_is_valid(self):
        """URL-reference form has no authorized_agents array — schema-valid
        but nothing to validate per-entry. Reports zero counts and
        ``is_reference=True`` so callers can distinguish it from an
        inline file with zero entries (which is invalid)."""
        from adcp.adagents import validate_adagents_structure

        data = {
            "$schema": "https://adcontextprotocol.org/schemas/v1/adagents.json",
            "authoritative_location": "https://cdn.example.com/adagents.json",
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is True
        assert result.errors == []
        assert result.authorized_agents_count == 0
        assert result.properties_count == 0
        assert result.is_reference is True

    def test_empty_authorized_agents_is_invalid(self):
        """Inline variant requires ``minItems: 1`` on ``authorized_agents``.
        A file with the array present but empty is structurally invalid;
        callers can distinguish this from the reference variant via
        ``is_reference``.
        """
        from adcp.adagents import validate_adagents_structure

        result = validate_adagents_structure({"authorized_agents": []})

        assert result.schema_valid is False
        assert result.is_reference is False
        assert len(result.errors) == 1
        assert result.errors[0].kind == "empty_authorized_agents"
        assert result.errors[0].index == -1

    def test_authorized_for_must_be_non_empty_string(self):
        """Schema requires ``authorized_for: {type: string, minLength: 1}``.
        Non-string truthy values (numbers, lists) must not silently pass.
        """
        from adcp.adagents import validate_adagents_structure

        data = {
            "authorized_agents": [
                {
                    "url": "https://a.example.com",
                    "authorized_for": 123,  # number, not string
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                },
                {
                    "url": "https://b.example.com",
                    "authorized_for": "",  # empty string
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                },
                {
                    "url": "https://c.example.com",
                    "authorized_for": ["x"],  # list, not string
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                },
            ]
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is False
        assert len(result.errors) == 3
        assert all(err.kind == "missing_authorized_for" for err in result.errors)

    def test_non_string_url_is_treated_as_missing(self):
        from adcp.adagents import validate_adagents_structure

        data = {
            "authorized_agents": [
                {
                    "url": 42,
                    "authorized_for": "x",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }

        result = validate_adagents_structure(data)

        assert result.schema_valid is False
        assert result.errors[0].kind == "missing_url"

    def test_non_dict_input_raises(self):
        from adcp.adagents import validate_adagents_structure

        with pytest.raises(AdagentsValidationError, match="must be a dictionary"):
            validate_adagents_structure([])  # type: ignore[arg-type]

    def test_non_list_authorized_agents_raises(self):
        from adcp.adagents import validate_adagents_structure

        with pytest.raises(AdagentsValidationError, match="must be an array"):
            validate_adagents_structure({"authorized_agents": "nope"})

    def test_distinguishes_invalid_file_from_unlisted_agent(self):
        """The headline use case from issue #707: a caller that previously
        could not tell ``get_properties_by_agent() == []`` apart from a
        broken file can now branch on ``schema_valid``.
        """
        from adcp.adagents import (
            get_properties_by_agent,
            validate_adagents_structure,
        )

        invalid = {
            "authorized_agents": [
                {"url": "https://other.example.com", "authorized_for": "x"},
            ]
        }
        valid_but_unlisted = {
            "authorized_agents": [
                {
                    "url": "https://other.example.com",
                    "authorized_for": "x",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }
        agent = "https://us.example.com"

        assert get_properties_by_agent(invalid, agent) == []
        assert get_properties_by_agent(valid_but_unlisted, agent) == []

        assert validate_adagents_structure(invalid).schema_valid is False
        assert validate_adagents_structure(valid_but_unlisted).schema_valid is True

    def test_report_dataclass_is_immutable(self):
        import dataclasses

        from adcp.adagents import (
            AdagentsEntryError,
            AdagentsValidationReport,
            validate_adagents_structure,
        )

        report = validate_adagents_structure(
            {
                "authorized_agents": [
                    {
                        "url": "https://a.example.com",
                        "authorized_for": "x",
                        "authorization_type": "property_ids",
                        "property_ids": ["p1"],
                    }
                ]
            }
        )
        assert isinstance(report, AdagentsValidationReport)

        err = AdagentsEntryError(index=0, kind="missing_url", message="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            err.index = 1  # type: ignore[misc]


class TestPublisherDomainsCompactForm:
    """adcp#4504: ``publisher_domains[]`` fan-out + XOR + ``by_id`` restriction."""

    def test_fanout_singular_passes_through(self):
        from adcp.adagents import _fanout_publisher_properties

        entry = {"selection_type": "all", "publisher_domain": "cnn.com"}
        assert _fanout_publisher_properties([entry]) == [entry]

    def test_fanout_expands_compact_form(self):
        from adcp.adagents import _fanout_publisher_properties

        out = _fanout_publisher_properties(
            [
                {
                    "selection_type": "by_tag",
                    "property_tags": ["ctv"],
                    "publisher_domains": ["site1.example", "site2.example", "site3.example"],
                }
            ]
        )
        assert [s["publisher_domain"] for s in out] == [
            "site1.example",
            "site2.example",
            "site3.example",
        ]
        # property_tags carried through to every expanded selector
        assert all(s.get("property_tags") == ["ctv"] for s in out)
        # publisher_domains stripped from each expanded selector
        assert all("publisher_domains" not in s for s in out)

    def test_fanout_preserves_mixed_compact_and_expanded_in_order(self):
        # adcp#4504 allows both forms in the same publisher_properties[]
        # array. Order must be preserved; compact entries fan out in-place.
        from adcp.adagents import _fanout_publisher_properties

        out = _fanout_publisher_properties(
            [
                {
                    "selection_type": "by_tag",
                    "property_tags": ["ctv"],
                    "publisher_domain": "first.example",
                },
                {
                    "selection_type": "by_tag",
                    "property_tags": ["ctv"],
                    "publisher_domains": ["b1.example", "b2.example"],
                },
                {
                    "selection_type": "by_tag",
                    "property_tags": ["ctv"],
                    "publisher_domain": "last.example",
                },
            ]
        )
        assert [s["publisher_domain"] for s in out] == [
            "first.example",
            "b1.example",
            "b2.example",
            "last.example",
        ]

    def test_fanout_skips_invalid_compact_entries(self):
        from adcp.adagents import _fanout_publisher_properties

        out = _fanout_publisher_properties(
            [
                {"selection_type": "by_tag", "publisher_domains": "not-a-list"},
                {"selection_type": "by_tag", "publisher_domains": []},
            ]
        )
        assert out == []

    def test_resolve_compact_form_via_get_properties_by_agent(self):
        adagents = {
            "properties": [
                {"property_id": "a-ctv", "publisher_domain": "a.example", "tags": ["ctv"]},
                {"property_id": "a-web", "publisher_domain": "a.example", "tags": ["web"]},
                {"property_id": "b-ctv", "publisher_domain": "b.example", "tags": ["ctv"]},
                {"property_id": "c-ctv", "publisher_domain": "c.example", "tags": ["ctv"]},
            ],
            "authorized_agents": [
                {
                    "url": "https://agent.example",
                    "authorized_for": "Managed network CTV",
                    "authorization_type": "publisher_properties",
                    "publisher_properties": [
                        {
                            "selection_type": "by_tag",
                            "property_tags": ["ctv"],
                            "publisher_domains": ["a.example", "b.example", "c.example"],
                        }
                    ],
                }
            ],
        }
        resolved = get_properties_by_agent(adagents, "https://agent.example")
        # Compact form fans out and inline-resolves against top-level properties[];
        # by_tag=["ctv"] picks the ctv-tagged property per domain (3 total).
        assert {p["property_id"] for p in resolved} == {"a-ctv", "b-ctv", "c-ctv"}
        assert all(
            p.get("publisher_domain") in {"a.example", "b.example", "c.example"} for p in resolved
        )

    def test_validate_accepts_pydantic_model_instance(self):
        # Upstream 3.0.12 dropped the `publisher_domains[]` compact form from
        # the generated `publisher-property-selector` schema, so the Pydantic
        # model now requires `publisher_domain`. The dict-layer helper still
        # implements the SDK-side compact-form / XOR contract (PR #750, #759)
        # for adopters consuming raw adagents.json bytes.
        from adcp.types.generated_poc.core.publisher_property_selector import (
            PublisherPropertySelector1,
        )
        from adcp.validation import (
            validate_publisher_properties_item,
        )

        # Valid Pydantic instance passes the dict-layer helper.
        good = PublisherPropertySelector1(selection_type="all", publisher_domain="cnn.com")
        validate_publisher_properties_item(good)

    def test_validate_rejects_non_dict_non_model(self):
        from adcp.validation import (
            ValidationError,
            validate_publisher_properties_item,
        )

        with pytest.raises(ValidationError, match="dict or a Pydantic model"):
            validate_publisher_properties_item("not-an-object")

    def test_xor_violation_both_publisher_fields(self):
        from adcp.validation import (
            ValidationError,
            validate_publisher_properties_item,
        )

        with pytest.raises(ValidationError, match="mutually exclusive"):
            validate_publisher_properties_item(
                {
                    "selection_type": "by_tag",
                    "property_tags": ["ctv"],
                    "publisher_domain": "cnn.com",
                    "publisher_domains": ["espn.com"],
                }
            )

    def test_xor_violation_neither_publisher_field(self):
        from adcp.validation import (
            ValidationError,
            validate_publisher_properties_item,
        )

        with pytest.raises(ValidationError, match="exactly one"):
            validate_publisher_properties_item(
                {"selection_type": "by_tag", "property_tags": ["ctv"]}
            )

    def test_by_id_rejects_publisher_domains(self):
        from adcp.validation import (
            ValidationError,
            validate_publisher_properties_item,
        )

        with pytest.raises(ValidationError, match="by_id"):
            validate_publisher_properties_item(
                {
                    "selection_type": "by_id",
                    "property_ids": ["p1"],
                    "publisher_domains": ["cnn.com", "espn.com"],
                }
            )

    def test_publisher_domains_must_be_unique(self):
        from adcp.validation import (
            ValidationError,
            validate_publisher_properties_item,
        )

        with pytest.raises(ValidationError, match="unique"):
            validate_publisher_properties_item(
                {
                    "selection_type": "all",
                    "publisher_domains": ["a.example", "a.example"],
                }
            )

    def test_compact_form_accepts_selection_type_all(self):
        from adcp.validation import validate_publisher_properties_item

        # Should not raise.
        validate_publisher_properties_item(
            {
                "selection_type": "all",
                "publisher_domains": ["a.example", "b.example"],
            }
        )


class TestRevokedPublisherDomains:
    """adcp#4504: ``revoked_publisher_domains[]`` filter takes precedence."""

    def test_revocation_reasons_are_well_known(self):
        # Upstream 3.0.12 dropped `revoked_publisher_domains` from the
        # generated `adagents` schema, so the `Reason` enum no longer
        # exists to cross-check against. The SDK-side validator continues
        # to enforce the four-value contract from PR #753 at the dict
        # layer; this test pins the canonical set so drift in the helper
        # is caught on its own.
        from adcp.validation.legacy import _REVOCATION_REASONS

        assert _REVOCATION_REASONS == frozenset(
            {"relationship_ended", "compliance_violation", "publisher_request", "other"}
        )

    def test_revocation_filters_compact_form_selectors(self):
        adagents = {
            "properties": [
                {"property_id": "a-ctv", "publisher_domain": "a.example", "tags": ["ctv"]},
                {"property_id": "b-ctv", "publisher_domain": "b.example", "tags": ["ctv"]},
                {"property_id": "c-ctv", "publisher_domain": "c.example", "tags": ["ctv"]},
            ],
            "revoked_publisher_domains": [
                {
                    "publisher_domain": "b.example",
                    "revoked_at": "2026-05-01T00:00:00Z",
                    "reason": "relationship_ended",
                }
            ],
            "authorized_agents": [
                {
                    "url": "https://agent.example",
                    "authorized_for": "Managed",
                    "authorization_type": "publisher_properties",
                    "publisher_properties": [
                        {
                            "selection_type": "by_tag",
                            "property_tags": ["ctv"],
                            "publisher_domains": ["a.example", "b.example", "c.example"],
                        }
                    ],
                }
            ],
        }
        resolved = get_properties_by_agent(adagents, "https://agent.example")
        # b.example is revoked — pre-filter strips its property from the index,
        # so inline resolution skips that domain transparently.
        assert {p["publisher_domain"] for p in resolved} == {"a.example", "c.example"}
        assert {p["property_id"] for p in resolved} == {"a-ctv", "c-ctv"}

    def test_revocation_filters_singular_selectors(self):
        adagents = {
            "properties": [
                {"property_id": "cnn-1", "publisher_domain": "cnn.com"},
                {"property_id": "espn-1", "publisher_domain": "espn.com"},
            ],
            "revoked_publisher_domains": [
                {"publisher_domain": "cnn.com", "revoked_at": "2026-05-01T00:00:00Z"}
            ],
            "authorized_agents": [
                {
                    "url": "https://agent.example",
                    "authorized_for": "x",
                    "authorization_type": "publisher_properties",
                    "publisher_properties": [
                        {"selection_type": "all", "publisher_domain": "cnn.com"},
                        {"selection_type": "all", "publisher_domain": "espn.com"},
                    ],
                }
            ],
        }
        resolved = get_properties_by_agent(adagents, "https://agent.example")
        # cnn.com revoked → its property is stripped from the index, so the
        # cnn selector resolves to nothing; only espn.com's property remains.
        assert [p["publisher_domain"] for p in resolved] == ["espn.com"]
        assert [p["property_id"] for p in resolved] == ["espn-1"]

    def test_revocation_filters_top_level_properties(self):
        adagents = {
            "revoked_publisher_domains": [
                {"publisher_domain": "revoked.example", "revoked_at": "2026-05-01T00:00:00Z"}
            ],
            "properties": [
                {"property_id": "p1", "publisher_domain": "kept.example"},
                {"property_id": "p2", "publisher_domain": "revoked.example"},
            ],
            "authorized_agents": [
                {
                    "url": "https://agent.example",
                    "authorized_for": "x",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1", "p2"],
                }
            ],
        }
        resolved = get_properties_by_agent(adagents, "https://agent.example")
        assert [p["property_id"] for p in resolved] == ["p1"]

    def test_revocation_validation_rejects_missing_revoked_at(self):
        from adcp.validation import (
            ValidationError,
            validate_revoked_publisher_domain_entry,
        )

        with pytest.raises(ValidationError, match="revoked_at"):
            validate_revoked_publisher_domain_entry({"publisher_domain": "x.example"})

    def test_revocation_validation_rejects_invalid_reason(self):
        from adcp.validation import (
            ValidationError,
            validate_revoked_publisher_domain_entry,
        )

        with pytest.raises(ValidationError, match="invalid reason"):
            validate_revoked_publisher_domain_entry(
                {
                    "publisher_domain": "x.example",
                    "revoked_at": "2026-05-01T00:00:00Z",
                    "reason": "made_up_reason",
                }
            )

    def test_revocation_validation_accepts_all_enum_reasons(self):
        from adcp.validation import validate_revoked_publisher_domain_entry

        for reason in (
            "relationship_ended",
            "compliance_violation",
            "publisher_request",
            "other",
        ):
            validate_revoked_publisher_domain_entry(
                {
                    "publisher_domain": "x.example",
                    "revoked_at": "2026-05-01T00:00:00Z",
                    "reason": reason,
                }
            )

    def test_validate_adagents_rejects_bad_revoked_array(self):
        from adcp.validation import ValidationError, validate_adagents

        with pytest.raises(ValidationError, match="revoked_publisher_domains"):
            validate_adagents({"revoked_publisher_domains": "not an array"})


class TestFetchWithCache:
    """adcp#4504: 304 conditional refresh + two-tier size cap."""

    @pytest.mark.asyncio
    async def test_304_returns_cached_body(self):
        from adcp.adagents import AdagentsCacheEntry, fetch_adagents_with_cache

        cached_body = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }
        cache_entry = AdagentsCacheEntry(
            body=cached_body, etag='"abc123"', last_modified="Mon, 01 Jan 2026 00:00:00 GMT"
        )

        mock_client = make_url_dispatching_client(
            {
                "https://example.com/.well-known/adagents.json": (
                    None,
                    304,
                    {"etag": '"abc123"'},
                )
            }
        )

        result = await fetch_adagents_with_cache(
            "example.com", cache_entry=cache_entry, client=mock_client
        )
        assert result.not_modified is True
        assert result.data == cached_body
        assert result.etag == '"abc123"'

        # The request must have carried the conditional headers.
        call_kwargs = mock_client.stream.call_args.kwargs
        sent_headers = call_kwargs["headers"]
        assert sent_headers.get("If-None-Match") == '"abc123"'
        assert sent_headers.get("If-Modified-Since") == "Mon, 01 Jan 2026 00:00:00 GMT"

    @pytest.mark.asyncio
    async def test_cache_entry_with_only_last_modified_sends_only_ims(self):
        # A cache entry can legitimately carry only Last-Modified (no
        # ETag) — origins differ. Verify we send If-Modified-Since alone
        # and the 304 path still serves the cached body.
        from adcp.adagents import AdagentsCacheEntry, fetch_adagents_with_cache

        cached_body = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }
        cache_entry = AdagentsCacheEntry(
            body=cached_body,
            etag=None,
            last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
        )
        mock_client = make_url_dispatching_client(
            {"https://example.com/.well-known/adagents.json": (None, 304, {})}
        )

        result = await fetch_adagents_with_cache(
            "example.com", cache_entry=cache_entry, client=mock_client
        )
        assert result.not_modified is True
        assert result.data == cached_body
        # The cache validators are echoed back when the 304 carries no
        # fresh ones — IMS persists, etag remains None.
        assert result.last_modified == "Mon, 01 Jan 2026 00:00:00 GMT"
        assert result.etag is None

        sent_headers = mock_client.stream.call_args.kwargs["headers"]
        assert sent_headers.get("If-Modified-Since") == "Mon, 01 Jan 2026 00:00:00 GMT"
        assert "If-None-Match" not in sent_headers

    @pytest.mark.asyncio
    async def test_200_returns_fresh_validators(self):
        from adcp.adagents import fetch_adagents_with_cache

        fresh_body = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }
        mock_client = make_url_dispatching_client(
            {
                "https://example.com/.well-known/adagents.json": (
                    fresh_body,
                    200,
                    {"etag": '"xyz"', "last-modified": "Mon, 19 May 2026 00:00:00 GMT"},
                )
            }
        )

        result = await fetch_adagents_with_cache("example.com", client=mock_client)
        assert result.not_modified is False
        assert result.data == fresh_body
        assert result.etag == '"xyz"'
        assert result.last_modified == "Mon, 19 May 2026 00:00:00 GMT"

    @pytest.mark.asyncio
    async def test_304_without_cache_entry_is_an_error(self):
        from adcp.adagents import fetch_adagents_with_cache
        from adcp.exceptions import AdagentsValidationError

        mock_client = make_url_dispatching_client(
            {"https://example.com/.well-known/adagents.json": (None, 304, {})}
        )

        with pytest.raises(AdagentsValidationError, match="304"):
            await fetch_adagents_with_cache("example.com", client=mock_client)


class TestSizeCaps:
    """adcp#4504: 5 MiB pointer cap + 20 MiB authoritative cap."""

    @pytest.mark.asyncio
    async def test_pointer_body_over_5mb_rejected(self):
        from adcp.adagents import MAX_POINTER_BYTES, fetch_adagents
        from adcp.exceptions import AdagentsValidationError

        oversized_body = b"x" * (MAX_POINTER_BYTES + 1)
        mock_client = MagicMock()

        def _stream(method, url, **kwargs):
            response = _make_stream_response(status_code=200, body=oversized_body)
            return _stream_cm(response)

        mock_client.stream = MagicMock(side_effect=_stream)

        with pytest.raises(AdagentsValidationError, match="size cap"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_streaming_cap_enforced_across_chunks_without_content_length(self):
        # When Content-Length is absent (chunked transfer-encoding), the
        # cap MUST come from the running-total guard inside the stream
        # loop. A single big body would exercise that too, but using many
        # small chunks demonstrates the loop guard is hit mid-stream, not
        # only on the final accumulator size.
        from adcp.adagents import MAX_POINTER_BYTES, fetch_adagents
        from adcp.exceptions import AdagentsValidationError

        chunk_size = 256 * 1024
        chunk_count = (MAX_POINTER_BYTES // chunk_size) + 2

        response = MagicMock()
        response.status_code = 200
        response.headers = httpx.Headers({})

        async def aiter_bytes():
            for _ in range(chunk_count):
                yield b"x" * chunk_size

        response.aiter_bytes = aiter_bytes

        mock_client = MagicMock()

        def _stream(method, url, **kwargs):
            return _stream_cm(response)

        mock_client.stream = MagicMock(side_effect=_stream)

        with pytest.raises(AdagentsValidationError, match="size cap"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_pointer_body_under_5mb_accepted(self):
        from adcp.adagents import fetch_adagents

        body = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "All",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                }
            ]
        }
        mock_client = make_url_dispatching_client(
            {"https://example.com/.well-known/adagents.json": body}
        )
        result = await fetch_adagents("example.com", client=mock_client)
        assert result == body

    @pytest.mark.asyncio
    async def test_pointer_content_length_over_cap_rejected_up_front(self):
        from adcp.adagents import MAX_POINTER_BYTES, fetch_adagents
        from adcp.exceptions import AdagentsValidationError

        mock_client = MagicMock()

        def _stream(method, url, **kwargs):
            # The cap is enforced from Content-Length before any bytes
            # are streamed, so the body itself doesn't need to be large.
            response = _make_stream_response(
                status_code=200,
                body=b"{}",
                headers={"content-length": str(MAX_POINTER_BYTES + 100)},
            )
            return _stream_cm(response)

        mock_client.stream = MagicMock(side_effect=_stream)

        with pytest.raises(AdagentsValidationError, match="Content-Length"):
            await fetch_adagents("example.com", client=mock_client)

    @pytest.mark.asyncio
    async def test_authoritative_hop_uses_20mb_cap(self):
        # A body that's larger than the pointer cap but under the
        # authoritative cap MUST be accepted when served as the second
        # hop. We simulate this with an in-band body that's 6 MB — over
        # the 5 MB pointer cap, under the 20 MB authoritative cap.
        import adcp.adagents as adagents_module
        from adcp.adagents import MAX_POINTER_BYTES, fetch_adagents

        pointer = {"authoritative_location": "https://cdn.example.com/adagents.json"}
        large_body = {
            "authorized_agents": [
                {
                    "url": "https://agent.example.com",
                    "authorized_for": "x",
                    "authorization_type": "property_ids",
                    "property_ids": ["p1"],
                    # Inject 6 MB of padding into a permissive (extra='allow') key.
                    "padding": "x" * (MAX_POINTER_BYTES + 1024 * 1024),
                }
            ]
        }
        mock_client = make_url_dispatching_client(
            {"https://example.com/.well-known/adagents.json": pointer}
        )
        redirect_client = make_url_dispatching_client(
            {"https://cdn.example.com/adagents.json": large_body}
        )

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: redirect_client
        ):
            result = await fetch_adagents("example.com", client=mock_client)

        assert result["authorized_agents"][0]["property_ids"] == ["p1"]

    @pytest.mark.asyncio
    async def test_authoritative_hop_rejects_over_20mb(self):
        import adcp.adagents as adagents_module
        from adcp.adagents import MAX_AUTHORITATIVE_BYTES, fetch_adagents
        from adcp.exceptions import AdagentsValidationError

        pointer = {"authoritative_location": "https://cdn.example.com/adagents.json"}
        oversized_body = b"x" * (MAX_AUTHORITATIVE_BYTES + 1)

        mock_client = make_url_dispatching_client(
            {"https://example.com/.well-known/adagents.json": pointer}
        )
        redirect_client = MagicMock()

        def _stream(method, url, **kwargs):
            response = _make_stream_response(status_code=200, body=oversized_body)
            return _stream_cm(response)

        redirect_client.stream = MagicMock(side_effect=_stream)
        redirect_client.__aenter__ = AsyncMock(return_value=redirect_client)
        redirect_client.__aexit__ = AsyncMock(return_value=None)

        with unittest.mock.patch.object(
            adagents_module.httpx, "AsyncClient", lambda *a, **kw: redirect_client
        ):
            with pytest.raises(AdagentsValidationError, match="size cap"):
                await fetch_adagents("example.com", client=mock_client)


# ---------------------------------------------------------------------------
# fetch_agent_authorizations_from_directory — HTTP-wire-level tests
#
# These tests exercise the AAO directory inverse-lookup path with a real
# httpx.MockTransport so the request URL, query string, and response body
# go through the same parser the SDK uses against a live directory. We
# parse the body with the real Pydantic model (no shape inference), and
# cover the 404 → empty path explicitly per the adcp#4828 contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFetchAgentAuthorizationsFromDirectory:
    @staticmethod
    def _client(handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_happy_path_parses_into_pydantic(self):
        """Real wire body round-trips through AgentAuthorizationsDirectoryResult."""
        from adcp.adagents import (
            AgentAuthorizationsDirectoryResult,
            DirectoryPublisherEntry,
            fetch_agent_authorizations_from_directory,
        )

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(
                200,
                json={
                    "agent_url": "https://agent.example.com/",
                    "directory_indexed_at": "2026-05-20T12:00:00Z",
                    "publishers": [
                        {
                            "publisher_domain": "nytimes.example",
                            "discovery_method": "direct",
                            "properties_authorized": 3,
                            "properties_total": 5,
                            "signing_keys_pinned": False,
                            "status": "authorized",
                            "last_verified_at": "2026-05-20T11:50:00Z",
                        },
                        {
                            "publisher_domain": "site1.example",
                            "discovery_method": "adagents_authoritative",
                            "manager_domain": "manager.example",
                            "properties_authorized": 1,
                            "properties_total": 1,
                            "status": "authorized",
                            "last_verified_at": "2026-05-20T11:55:00Z",
                        },
                    ],
                    "next_cursor": "opaque-cursor-1",
                },
            )

        async with self._client(handler) as client:
            result = await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                client=client,
            )

        assert captured["method"] == "GET"
        assert captured["url"] == (
            "https://aao.example.com/v1/agents/" "https%3A%2F%2Fagent.example.com%2F/publishers"
        )
        assert isinstance(result, AgentAuthorizationsDirectoryResult)
        assert result.agent_url == "https://agent.example.com/"
        assert result.next_cursor == "opaque-cursor-1"
        assert len(result.publishers) == 2
        assert all(isinstance(p, DirectoryPublisherEntry) for p in result.publishers)
        assert result.publishers[0].discovery_method == "direct"
        assert result.publishers[1].manager_domain == "manager.example"
        assert result.publishers[0].status == "authorized"

    async def test_404_returns_empty_publishers(self):
        """404 from the directory is the 'not indexed' answer — return empty."""
        from adcp.adagents import (
            AgentAuthorizationsDirectoryResult,
            fetch_agent_authorizations_from_directory,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="Not found")

        async with self._client(handler) as client:
            result = await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                client=client,
            )

        assert isinstance(result, AgentAuthorizationsDirectoryResult)
        assert result.publishers == []
        assert result.directory_indexed_at is None
        assert result.next_cursor is None
        assert result.agent_url == "https://agent.example.com/"

    async def test_since_passes_through_as_query_string(self):
        """`since` is forwarded verbatim as ?since=… for incremental sync."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["since"] = request.url.params.get("since") or ""
            captured["cursor"] = request.url.params.get("cursor") or ""
            return httpx.Response(
                200,
                json={
                    "agent_url": "https://agent.example.com/",
                    "directory_indexed_at": None,
                    "publishers": [],
                },
            )

        async with self._client(handler) as client:
            await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="https://aao.example.com/",
                since="2026-05-20T12:00:00Z",
                client=client,
            )

        assert captured["since"] == "2026-05-20T12:00:00Z"
        assert captured["cursor"] == ""
        assert "?since=2026-05-20T12%3A00%3A00Z" in captured["url"]

    async def test_cursor_passes_through_as_cursor_query_string(self):
        """Pagination cursors use ?cursor=…, distinct from timestamp `since`."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["since"] = request.url.params.get("since") or ""
            captured["cursor"] = request.url.params.get("cursor") or ""
            return httpx.Response(
                200,
                json={
                    "agent_url": "https://agent.example.com/",
                    "directory_indexed_at": None,
                    "publishers": [],
                },
            )

        async with self._client(handler) as client:
            await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="https://aao.example.com/",
                cursor="opaque-cursor-1",
                client=client,
            )

        assert captured["cursor"] == "opaque-cursor-1"
        assert captured["since"] == ""
        assert "?cursor=opaque-cursor-1" in captured["url"]
        assert "since=opaque-cursor-1" not in captured["url"]

    async def test_timeout_raises_adagents_timeout_error(self):
        """httpx timeouts surface as AdagentsTimeoutError (not generic Exception)."""
        from adcp.adagents import fetch_agent_authorizations_from_directory
        from adcp.exceptions import AdagentsTimeoutError

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated", request=request)

        async with self._client(handler) as client:
            with pytest.raises(AdagentsTimeoutError):
                await fetch_agent_authorizations_from_directory(
                    "https://agent.example.com/",
                    directory_url="https://aao.example.com",
                    client=client,
                )

    async def test_malformed_json_raises_validation_error(self):
        """A 200 with non-JSON body is the directory's bug — surface as ValidationError."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"not json at all",
                headers={"content-type": "application/json"},
            )

        async with self._client(handler) as client:
            with pytest.raises(AdagentsValidationError, match="Invalid JSON"):
                await fetch_agent_authorizations_from_directory(
                    "https://agent.example.com/",
                    directory_url="https://aao.example.com",
                    client=client,
                )

    async def test_schema_mismatch_raises_validation_error(self):
        """A 200 whose body doesn't match the schema fails Pydantic validation."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    # Missing required `directory_indexed_at`; `publishers`
                    # has an entry missing required `last_verified_at`.
                    "agent_url": "https://agent.example.com/",
                    "publishers": [
                        {
                            "publisher_domain": "site1.example",
                            "discovery_method": "direct",
                            "properties_authorized": 0,
                            "properties_total": 0,
                            "status": "authorized",
                        }
                    ],
                },
            )

        async with self._client(handler) as client:
            with pytest.raises(AdagentsValidationError, match="schema validation"):
                await fetch_agent_authorizations_from_directory(
                    "https://agent.example.com/",
                    directory_url="https://aao.example.com",
                    client=client,
                )

    async def test_non_https_directory_url_rejected(self):
        """SSRF gate: http:// is refused before any network I/O."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        with pytest.raises(AdagentsValidationError, match="HTTPS"):
            await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="http://aao.example.com",
            )

    async def test_non_200_non_404_raises_validation_error(self):
        """5xx is the directory's bug — surface as ValidationError, not 'empty'."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream unavailable")

        async with self._client(handler) as client:
            with pytest.raises(AdagentsValidationError, match="HTTP 503"):
                await fetch_agent_authorizations_from_directory(
                    "https://agent.example.com/",
                    directory_url="https://aao.example.com",
                    client=client,
                )

    async def test_include_properties_appears_as_query_param(self):
        """include=['properties'] emits ?include=properties (repeated-key form)."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["raw_url"] = str(request.url)
            captured["include_list"] = request.url.params.get_list("include")
            return httpx.Response(
                200,
                json={
                    "agent_url": "https://agent.example.com/",
                    "directory_indexed_at": None,
                    "publishers": [],
                },
            )

        async with self._client(handler) as client:
            await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                include=["properties"],
                client=client,
            )

        assert captured["include_list"] == ["properties"]
        assert "include=properties" in captured["raw_url"]  # type: ignore[operator]

    async def test_include_multiple_values_repeated_keys(self):
        """include=['properties','future'] emits TWO ?include= keys, not comma-joined."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["raw_url"] = str(request.url)
            captured["include_list"] = request.url.params.get_list("include")
            return httpx.Response(
                200,
                json={
                    "agent_url": "https://agent.example.com/",
                    "directory_indexed_at": None,
                    "publishers": [],
                },
            )

        async with self._client(handler) as client:
            await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                include=["properties", "future"],
                client=client,
            )

        assert captured["include_list"] == ["properties", "future"]
        # Comma-joined form would not produce two list items; assert wire form.
        raw = captured["raw_url"]
        assert isinstance(raw, str)
        assert raw.count("include=") == 2
        assert "include=properties%2Cfuture" not in raw

    async def test_property_ids_parsed_from_publisher_entry(self):
        """Directory row with property_ids round-trips into the Pydantic model."""
        from adcp.adagents import (
            DirectoryPublisherEntry,
            fetch_agent_authorizations_from_directory,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "agent_url": "https://agent.example.com/",
                    "directory_indexed_at": "2026-05-20T12:00:00Z",
                    "publishers": [
                        {
                            "publisher_domain": "nytimes.example",
                            "discovery_method": "direct",
                            "properties_authorized": 2,
                            "properties_total": 2,
                            "status": "authorized",
                            "last_verified_at": "2026-05-20T11:50:00Z",
                            "property_ids": ["p-1", "p-2"],
                        }
                    ],
                },
            )

        async with self._client(handler) as client:
            result = await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                include=["properties"],
                client=client,
            )

        entry = result.publishers[0]
        assert isinstance(entry, DirectoryPublisherEntry)
        assert entry.property_ids == ["p-1", "p-2"]

    async def test_property_ids_absent_is_none(self):
        """Directory row without property_ids parses with property_ids=None."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "agent_url": "https://agent.example.com/",
                    "directory_indexed_at": "2026-05-20T12:00:00Z",
                    "publishers": [
                        {
                            "publisher_domain": "nytimes.example",
                            "discovery_method": "direct",
                            "properties_authorized": 3,
                            "properties_total": 3,
                            "status": "authorized",
                            "last_verified_at": "2026-05-20T11:50:00Z",
                        }
                    ],
                },
            )

        async with self._client(handler) as client:
            result = await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                client=client,
            )

        assert result.publishers[0].property_ids is None

    async def test_include_combines_with_since_and_cursor(self):
        """`since`, pagination `cursor`, and `include` round-trip together."""
        from adcp.adagents import fetch_agent_authorizations_from_directory

        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["since"] = request.url.params.get("since")
            captured["cursor"] = request.url.params.get("cursor")
            captured["include_list"] = request.url.params.get_list("include")
            return httpx.Response(
                200,
                json={
                    "agent_url": "https://agent.example.com/",
                    "directory_indexed_at": None,
                    "publishers": [],
                },
            )

        async with self._client(handler) as client:
            await fetch_agent_authorizations_from_directory(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                since="2026-05-20T12:00:00Z",
                cursor="opaque-cursor-1",
                include=["properties"],
                client=client,
            )

        assert captured["since"] == "2026-05-20T12:00:00Z"
        assert captured["cursor"] == "opaque-cursor-1"
        assert captured["include_list"] == ["properties"]


class TestDetectPublisherPropertiesDivergence:
    """detect_publisher_properties_divergence: directory vs federated set-diff."""

    @staticmethod
    def _directory_handler(publishers, *, next_cursor=None):
        """Build a MockTransport handler that returns a fixed directory page."""

        def handler(request: httpx.Request) -> httpx.Response:
            body: dict[str, object] = {
                "agent_url": "https://agent.example.com/",
                "directory_indexed_at": "2026-05-20T12:00:00Z",
                "publishers": publishers,
            }
            if next_cursor is not None:
                body["next_cursor"] = next_cursor
            return httpx.Response(200, json=body)

        return handler

    @staticmethod
    def _entry(
        publisher_domain,
        *,
        properties_authorized=1,
        property_ids=None,
    ):
        entry: dict[str, object] = {
            "publisher_domain": publisher_domain,
            "discovery_method": "direct",
            "properties_authorized": properties_authorized,
            "properties_total": properties_authorized,
            "status": "authorized",
            "last_verified_at": "2026-05-20T11:50:00Z",
        }
        if property_ids is not None:
            entry["property_ids"] = property_ids
        return entry

    async def test_full_set_diff_when_property_ids_present(self, monkeypatch):
        """Directory says {p1,p2}; federated returns {p2,p3} → set-diff reported."""
        from adcp import adagents as adagents_mod
        from adcp.adagents import detect_publisher_properties_divergence

        publishers = [
            self._entry("nytimes.example", properties_authorized=2, property_ids=["p1", "p2"])
        ]
        handler = self._directory_handler(publishers)

        async def fake_fetch_adagents(domain, timeout=10.0, client=None):
            return {"_": "ignored — get_properties_by_agent is patched"}

        def fake_get_properties_by_agent(data, agent_url):
            return [{"property_id": "p2"}, {"property_id": "p3"}]

        monkeypatch.setattr(adagents_mod, "fetch_adagents", fake_fetch_adagents)
        monkeypatch.setattr(adagents_mod, "get_properties_by_agent", fake_get_properties_by_agent)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            report = await detect_publisher_properties_divergence(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                client=client,
            )

        assert len(report) == 1
        d = report[0]
        assert d.publisher_domain == "nytimes.example"
        assert d.missing_in_inline == ["p3"]
        assert d.missing_in_federated == ["p1"]
        assert d.federated_properties_found == 2
        assert d.directory_properties_authorized == 2
        assert d.child_fetch_error is None

    async def test_no_report_when_sets_match(self, monkeypatch):
        """Directory and federated agree on the ID set → empty report."""
        from adcp import adagents as adagents_mod
        from adcp.adagents import detect_publisher_properties_divergence

        publishers = [
            self._entry("nytimes.example", properties_authorized=2, property_ids=["p1", "p2"])
        ]
        handler = self._directory_handler(publishers)

        async def fake_fetch_adagents(domain, timeout=10.0, client=None):
            return {}

        def fake_get_properties_by_agent(data, agent_url):
            return [{"property_id": "p1"}, {"property_id": "p2"}]

        monkeypatch.setattr(adagents_mod, "fetch_adagents", fake_fetch_adagents)
        monkeypatch.setattr(adagents_mod, "get_properties_by_agent", fake_get_properties_by_agent)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            report = await detect_publisher_properties_divergence(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                client=client,
            )

        assert report == []

    async def test_count_fallback_when_property_ids_absent(self, monkeypatch):
        """No property_ids on the row → count mismatch yields divergence with None fields."""
        from adcp import adagents as adagents_mod
        from adcp.adagents import detect_publisher_properties_divergence

        publishers = [self._entry("nytimes.example", properties_authorized=5)]
        handler = self._directory_handler(publishers)

        async def fake_fetch_adagents(domain, timeout=10.0, client=None):
            return {}

        def fake_get_properties_by_agent(data, agent_url):
            # Only 3 IDs — directory said 5 → count mismatch.
            return [{"property_id": "a"}, {"property_id": "b"}, {"property_id": "c"}]

        monkeypatch.setattr(adagents_mod, "fetch_adagents", fake_fetch_adagents)
        monkeypatch.setattr(adagents_mod, "get_properties_by_agent", fake_get_properties_by_agent)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            report = await detect_publisher_properties_divergence(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                client=client,
            )

        assert len(report) == 1
        d = report[0]
        assert d.directory_properties_authorized == 5
        assert d.federated_properties_found == 3
        assert d.missing_in_inline is None
        assert d.missing_in_federated is None

    async def test_child_fetch_error_recorded(self, monkeypatch):
        """fetch_adagents raises → divergence record carries child_fetch_error."""
        from adcp import adagents as adagents_mod
        from adcp.adagents import detect_publisher_properties_divergence
        from adcp.exceptions import AdagentsNotFoundError

        publishers = [
            self._entry("nytimes.example", properties_authorized=2, property_ids=["p1", "p2"])
        ]
        handler = self._directory_handler(publishers)

        async def fake_fetch_adagents(domain, timeout=10.0, client=None):
            raise AdagentsNotFoundError(domain)

        monkeypatch.setattr(adagents_mod, "fetch_adagents", fake_fetch_adagents)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            report = await detect_publisher_properties_divergence(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                client=client,
            )

        assert len(report) == 1
        d = report[0]
        assert d.child_fetch_error is not None
        assert d.federated_properties_found == 0
        assert d.missing_in_inline is None
        assert d.missing_in_federated is None

    async def test_sample_size_caps_probes(self, monkeypatch):
        """sample_size=3 against a 10-row page → only 3 fetch_adagents calls."""
        from adcp import adagents as adagents_mod
        from adcp.adagents import detect_publisher_properties_divergence

        publishers = [
            self._entry(f"pub{i}.example", properties_authorized=1, property_ids=["p1"])
            for i in range(10)
        ]
        handler = self._directory_handler(publishers)

        call_count = 0

        async def fake_fetch_adagents(domain, timeout=10.0, client=None):
            nonlocal call_count
            call_count += 1
            return {}

        def fake_get_properties_by_agent(data, agent_url):
            return [{"property_id": "p1"}]

        monkeypatch.setattr(adagents_mod, "fetch_adagents", fake_fetch_adagents)
        monkeypatch.setattr(adagents_mod, "get_properties_by_agent", fake_get_properties_by_agent)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await detect_publisher_properties_divergence(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                sample_size=3,
                client=client,
            )

        assert call_count == 3

    async def test_max_concurrency_respected(self, monkeypatch):
        """Peak in-flight fetch_adagents calls never exceed max_concurrency."""
        import asyncio

        from adcp import adagents as adagents_mod
        from adcp.adagents import detect_publisher_properties_divergence

        publishers = [
            self._entry(f"pub{i}.example", properties_authorized=1, property_ids=["p1"])
            for i in range(20)
        ]
        handler = self._directory_handler(publishers)

        in_flight = 0
        peak = 0
        lock = asyncio.Lock()
        release_gate = asyncio.Event()

        async def fake_fetch_adagents(domain, timeout=10.0, client=None):
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                # Hold the slot long enough for the semaphore to actually
                # gate concurrent entrants; gate is set immediately by
                # the test event below so we don't slow the suite down.
                await release_gate.wait()
            finally:
                async with lock:
                    in_flight -= 1
            return {}

        def fake_get_properties_by_agent(data, agent_url):
            return [{"property_id": "p1"}]

        monkeypatch.setattr(adagents_mod, "fetch_adagents", fake_fetch_adagents)
        monkeypatch.setattr(adagents_mod, "get_properties_by_agent", fake_get_properties_by_agent)

        async def releaser():
            # Yield enough to let the semaphore admit its first batch
            # of waiters, observe peak, then release everyone.
            for _ in range(50):
                await asyncio.sleep(0)
            release_gate.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await asyncio.gather(
                detect_publisher_properties_divergence(
                    "https://agent.example.com/",
                    directory_url="https://aao.example.com",
                    sample_size=20,
                    max_concurrency=4,
                    client=client,
                ),
                releaser(),
            )

        assert peak <= 4
        assert peak >= 1  # sanity: probes actually ran

    async def test_divergence_dedupes_collected_by_publisher_domain(self, monkeypatch):
        """Hostile directory: 5 rows all publisher_domain=victim → 1 fetch."""
        from adcp import adagents as adagents_mod
        from adcp.adagents import detect_publisher_properties_divergence

        # 5 entries all pointing at the same victim host. A naive
        # implementation would fan out 5 concurrent fetches against
        # victim.example; the dedupe path must collapse to a single probe.
        publishers = [
            self._entry("victim.example", properties_authorized=1, property_ids=["p1"])
            for _ in range(5)
        ]
        handler = self._directory_handler(publishers)

        call_count = 0

        async def fake_fetch_adagents(domain, timeout=10.0, client=None):
            nonlocal call_count
            call_count += 1
            return {}

        def fake_get_properties_by_agent(data, agent_url):
            return [{"property_id": "p1"}]

        monkeypatch.setattr(adagents_mod, "fetch_adagents", fake_fetch_adagents)
        monkeypatch.setattr(adagents_mod, "get_properties_by_agent", fake_get_properties_by_agent)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await detect_publisher_properties_divergence(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                client=client,
            )

        assert call_count == 1

    async def test_divergence_uses_cursor_param_for_second_page(self):
        """Directory pagination sends next_cursor back as ?cursor=, not ?since=."""
        from adcp.adagents import detect_publisher_properties_divergence

        requests: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url)
            body: dict[str, object] = {
                "agent_url": "https://agent.example.com/",
                "directory_indexed_at": "2026-05-20T12:00:00Z",
                "publishers": [],
            }
            if len(requests) == 1:
                body["next_cursor"] = "page-2"
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await detect_publisher_properties_divergence(
                "https://agent.example.com/",
                directory_url="https://aao.example.com",
                sample_size=None,
                client=client,
            )

        assert len(requests) == 2
        assert requests[0].params.get("cursor") is None
        assert requests[0].params.get("since") is None
        assert requests[1].params.get("cursor") == "page-2"
        assert requests[1].params.get("since") is None

    async def test_divergence_aborts_on_repeated_cursor(self, monkeypatch):
        """Misbehaving directory returns the same next_cursor forever → raise."""
        from adcp.adagents import detect_publisher_properties_divergence

        # Each response includes a next_cursor that never advances. The
        # page-walk loop must detect the repeat and raise rather than
        # spin until OOM.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "agent_url": "https://agent.example.com/",
                    "directory_indexed_at": "2026-05-20T12:00:00Z",
                    "publishers": [],
                    "next_cursor": "stuck",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(AdagentsValidationError, match="cursor 'stuck' repeated"):
                await detect_publisher_properties_divergence(
                    "https://agent.example.com/",
                    directory_url="https://aao.example.com",
                    sample_size=None,  # full sweep, so we actually walk pages
                    client=client,
                )

    async def test_divergence_warns_on_count_only_mode(self, monkeypatch, caplog):
        """No entry has property_ids → one-shot warning fires."""
        import logging as _logging

        from adcp import adagents as adagents_mod
        from adcp.adagents import detect_publisher_properties_divergence

        # No property_ids on any row → directory is in count-only mode.
        publishers = [
            self._entry("a.example", properties_authorized=1),
            self._entry("b.example", properties_authorized=2),
        ]
        handler = self._directory_handler(publishers)

        async def fake_fetch_adagents(domain, timeout=10.0, client=None):
            return {}

        def fake_get_properties_by_agent(data, agent_url):
            return [{"property_id": "p1"}]

        monkeypatch.setattr(adagents_mod, "fetch_adagents", fake_fetch_adagents)
        monkeypatch.setattr(adagents_mod, "get_properties_by_agent", fake_get_properties_by_agent)

        with caplog.at_level(_logging.WARNING, logger="adcp.adagents"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await detect_publisher_properties_divergence(
                    "https://agent.example.com/",
                    directory_url="https://aao.example.com",
                    client=client,
                )

        count_only_warnings = [
            r for r in caplog.records if "count-only divergence detection" in r.getMessage()
        ]
        assert len(count_only_warnings) == 1
        assert "https://aao.example.com" in count_only_warnings[0].getMessage()
