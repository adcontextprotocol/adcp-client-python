"""Security-focused OAuth 2.0 authorization-code helpers for buyer clients.

This module deliberately implements a small, explicit surface:

* RFC 8414 authorization-server discovery from a *trusted issuer URL*;
* pre-registered public clients (no dynamic registration or client secrets);
* authorization code with RFC 7636 S256 PKCE;
* one-time, atomically consumed pending flows; and
* bounded, DNS-pinned metadata and token HTTP requests.

It does not discover an authorization server from an MCP resource URL.  A
resource-to-issuer flow first needs RFC 9728 protected-resource metadata; pass
the resulting trusted issuer URL to :func:`discover_oauth_metadata`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Protocol, TypeVar, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from adcp.signing._bounded_http import ResponseTooLargeError, async_read_limited_bytes
from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport
from adcp.signing.jwks import SSRFValidationError

_MAX_HTTP_BODY_BYTES = 64 * 1024
_MAX_URL_LENGTH = 2048
_MAX_STATE_LENGTH = 512
_MAX_CODE_LENGTH = 8192
_MAX_CLIENT_ID_LENGTH = 512
_MAX_SCOPE_LENGTH = 2048
_MAX_RESOURCE_LENGTH = 2048
_MAX_TOKEN_LENGTH = 8192
_DEFAULT_FLOW_TTL_SECONDS = 600
_DEFAULT_STORE_CAPACITY = 1024
_SAFE_OAUTH_ERROR_CODES = frozenset(
    {
        "access_denied",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "invalid_target",
        "server_error",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_grant_type",
        "unsupported_response_type",
    }
)
_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_SCOPE_TOKEN_RE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]+$")
_SCOPE_RE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]+(?: [\x21\x23-\x5B\x5D-\x7E]+)*$")
_RESPONSE_TYPE_RE = _SCOPE_RE
_VSCHAR_RE = re.compile(r"^[\x20-\x7E]+$")
_TOKEN_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._~+:/-]{0,63}$")
_RESERVED_AUTHORIZATION_QUERY_KEYS = frozenset(
    {
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "redirect_uri",
        "resource",
        "response_type",
        "scope",
        "state",
    }
)


class OAuthClientError(Exception):
    """Base error whose message never contains remote response content."""

    def __init__(
        self,
        code: str,
        *,
        phase: str,
        status_code: int | None = None,
        oauth_error: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.code = code
        self.phase = phase
        self.status_code = status_code
        self.oauth_error = oauth_error
        self.retry_after_seconds = retry_after_seconds
        message = f"OAuth {phase} failed ({code})"
        if status_code is not None:
            message += f": HTTP {status_code}"
        super().__init__(message)


class OAuthDiscoveryError(OAuthClientError):
    """Authorization-server discovery failed safely."""


class OAuthAuthorizationError(OAuthClientError):
    """Authorization callback validation failed; start a new flow."""


class OAuthTokenExchangeError(OAuthClientError):
    """Token exchange failed; the consumed flow must not be retried."""


class OAuthFlowStoreError(OAuthClientError):
    """The pending-flow store rejected an insert or lookup."""


_T = TypeVar("_T")
_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, asyncio.TimeoutError)


class _BodyTimeoutError(Exception):
    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__()


class _OAuthDeadlineExpired(BaseException):
    """Raised only for the SDK-owned absolute OAuth network deadline."""


async def _run_with_deadline(factory: Callable[[], Awaitable[_T]], timeout: float) -> _T:
    """Apply one Python-3.10-compatible deadline without relabeling inner timeouts."""

    async def run() -> _T:
        try:
            return await factory()
        except _TIMEOUT_ERRORS as exc:
            raise _BodyTimeoutError(exc) from exc

    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except _BodyTimeoutError as exc:
        raise exc.cause from exc.cause.__cause__
    except _TIMEOUT_ERRORS as exc:
        raise _OAuthDeadlineExpired from exc


class OAuthIssuerBinding(str, Enum):
    """How a redirect URI is bound to one authorization-server issuer."""

    AUTHORIZATION_RESPONSE_ISS = "authorization_response_iss"
    DISTINCT_REDIRECT_URI = "distinct_redirect_uri"


BoundedUrl = Annotated[str, Field(min_length=1, max_length=_MAX_URL_LENGTH)]
BoundedToken = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[\x21-\x7E]+$")]
ResponseType = Annotated[
    str,
    Field(min_length=1, max_length=256, pattern=_RESPONSE_TYPE_RE.pattern),
]
ScopeToken = Annotated[
    str,
    Field(min_length=1, max_length=256, pattern=_SCOPE_TOKEN_RE.pattern),
]
BoundedState = Annotated[
    str,
    Field(min_length=43, max_length=_MAX_STATE_LENGTH, pattern=r"^[A-Za-z0-9_-]+$"),
]


def _validate_absolute_url(
    value: str,
    *,
    field: str,
    issuer: bool = False,
    allow_loopback_http: bool = False,
) -> str:
    if "\\" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{field} contains an unsafe character")
    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{field} must not contain user information")
    if not parts.hostname or parts.fragment:
        raise ValueError(f"{field} must be an absolute URL without a fragment")
    try:
        parts.port
    except ValueError:
        raise ValueError(f"{field} contains an invalid port") from None
    if issuer and parts.query:
        raise ValueError("issuer must not contain a query")
    if parts.scheme == "https":
        return value
    if parts.scheme == "http" and allow_loopback_http and _is_literal_loopback(parts.hostname):
        return value
    raise ValueError(f"{field} must use HTTPS")


def _is_literal_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OAuthAuthorizationServerMetadata(BaseModel):
    """Bounded subset of RFC 8414 metadata used by this helper."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    issuer: BoundedUrl
    authorization_endpoint: BoundedUrl
    token_endpoint: BoundedUrl
    response_types_supported: tuple[ResponseType, ...] = Field(min_length=1, max_length=32)
    grant_types_supported: tuple[BoundedToken, ...] | None = Field(
        default=None, min_length=1, max_length=32
    )
    token_endpoint_auth_methods_supported: tuple[BoundedToken, ...] = Field(
        min_length=1, max_length=32
    )
    code_challenge_methods_supported: tuple[BoundedToken, ...] = Field(min_length=1, max_length=16)
    scopes_supported: tuple[ScopeToken, ...] | None = Field(
        default=None, min_length=1, max_length=128
    )
    authorization_response_iss_parameter_supported: bool = False

    @field_validator("issuer")
    @classmethod
    def _issuer_url(cls, value: str) -> str:
        return _validate_absolute_url(
            value,
            field="issuer",
            issuer=True,
            allow_loopback_http=True,
        )

    @field_validator("authorization_endpoint", "token_endpoint")
    @classmethod
    def _endpoint_url(cls, value: str) -> str:
        return _validate_absolute_url(
            value,
            field="OAuth endpoint",
            allow_loopback_http=True,
        )

    @field_validator("authorization_endpoint")
    @classmethod
    def _reserved_authorization_query(cls, value: str) -> str:
        query_keys = [key for key, _ in parse_qsl(urlsplit(value).query, keep_blank_values=True)]
        if any(key in _RESERVED_AUTHORIZATION_QUERY_KEYS for key in query_keys):
            raise ValueError("authorization endpoint contains a reserved OAuth query parameter")
        if len(query_keys) != len(set(query_keys)):
            raise ValueError("authorization endpoint contains duplicate query parameters")
        return value


class OAuthAuthorizationRequest(BaseModel):
    """Browser-facing result. The PKCE verifier is intentionally absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_url: BoundedUrl
    state: BoundedState
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return value


class PendingOAuthAuthorization(BaseModel):
    """Authoritative server-side snapshot for one authorization attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: BoundedState
    code_verifier: SecretStr
    issuer: BoundedUrl
    authorization_endpoint: BoundedUrl
    token_endpoint: BoundedUrl
    client_id: Annotated[str, Field(min_length=1, max_length=_MAX_CLIENT_ID_LENGTH)]
    redirect_uri: BoundedUrl
    scope: Annotated[str | None, Field(max_length=_MAX_SCOPE_LENGTH)] = None
    resource: Annotated[str | None, Field(max_length=_MAX_RESOURCE_LENGTH)] = None
    issuer_binding: OAuthIssuerBinding
    allow_loopback_http: bool = False
    created_at: datetime
    expires_at: datetime

    @field_validator("code_verifier")
    @classmethod
    def _valid_verifier(cls, value: SecretStr) -> SecretStr:
        if not _PKCE_VERIFIER_RE.fullmatch(value.get_secret_value()):
            raise ValueError("invalid PKCE verifier")
        return value

    @model_validator(mode="after")
    def _valid_lifetime(self) -> PendingOAuthAuthorization:
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("OAuth flow timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("OAuth flow expiry must follow creation")
        return self


class OAuthTokenSet(BaseModel):
    """Closed, secret-safe token response returned after exchange."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    access_token: SecretStr
    token_type: Annotated[
        str,
        Field(min_length=1, max_length=64, pattern=_TOKEN_TYPE_RE.pattern),
    ]
    expires_in: Annotated[int | None, Field(default=None, ge=0, le=315_360_000)]
    refresh_token: SecretStr | None = None
    id_token: SecretStr | None = None
    scope: Annotated[str | None, Field(default=None, max_length=_MAX_SCOPE_LENGTH)]

    @field_validator("access_token", "refresh_token", "id_token")
    @classmethod
    def _bounded_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            secret = value.get_secret_value()
            if not 1 <= len(secret) <= _MAX_TOKEN_LENGTH or not _VSCHAR_RE.fullmatch(secret):
                raise ValueError("token must use bounded RFC 6749 VSCHAR syntax")
        return value

    @field_validator("scope")
    @classmethod
    def _valid_scope(cls, value: str | None) -> str | None:
        if value is not None and not _SCOPE_RE.fullmatch(value):
            raise ValueError("scope must contain space-delimited RFC 6749 scope tokens")
        return value


@runtime_checkable
class PendingOAuthFlowStore(Protocol):
    """Atomic one-time storage contract for pending OAuth flows."""

    async def insert_if_absent(self, pending: PendingOAuthAuthorization) -> bool:
        """Insert a live state without overwriting; return ``False`` on collision."""

    async def consume(self, state: str) -> PendingOAuthAuthorization | None:
        """Atomically remove and return a live state, or return ``None``."""


class InMemoryPendingOAuthFlowStore:
    """Lock-protected reference store for one process only.

    Multi-process and multi-replica applications must provide a shared store
    whose insert-if-absent and consume operations are atomic across replicas.
    """

    def __init__(
        self,
        *,
        capacity: int = _DEFAULT_STORE_CAPACITY,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(capacity, bool) or not 1 <= capacity <= 1_000_000:
            raise ValueError("capacity must be between 1 and 1000000")
        self._capacity = capacity
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[str, PendingOAuthAuthorization] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("OAuth store clock must return a timezone-aware datetime")
        return now

    def _prune_expired(self, now: datetime) -> None:
        expired = [state for state, pending in self._entries.items() if pending.expires_at <= now]
        for state in expired:
            del self._entries[state]

    async def insert_if_absent(self, pending: PendingOAuthAuthorization) -> bool:
        async with self._lock:
            now = self._now()
            self._prune_expired(now)
            if pending.expires_at <= now or pending.state in self._entries:
                return False
            if len(self._entries) >= self._capacity:
                raise OAuthFlowStoreError("capacity_exceeded", phase="store")
            self._entries[pending.state] = pending
            return True

    async def consume(self, state: str) -> PendingOAuthAuthorization | None:
        async with self._lock:
            now = self._now()
            self._prune_expired(now)
            return self._entries.pop(state, None)


def _metadata_url(issuer_url: str, *, allow_loopback_http: bool) -> str:
    if len(issuer_url) > _MAX_URL_LENGTH:
        raise OAuthDiscoveryError("invalid_issuer", phase="discovery")
    try:
        _validate_absolute_url(
            issuer_url,
            field="issuer",
            issuer=True,
            allow_loopback_http=allow_loopback_http,
        )
        parts = urlsplit(issuer_url)
        issuer_path = parts.path[:-1] if parts.path.endswith("/") else parts.path
        path = "/.well-known/oauth-authorization-server" + issuer_path
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except (ValueError, UnicodeError):
        raise OAuthDiscoveryError("invalid_issuer", phase="discovery") from None


async def _transport_for(url: str, *, allow_loopback_http: bool) -> httpx.AsyncBaseTransport:
    parts = urlsplit(url)
    if parts.scheme == "http":
        if (
            not allow_loopback_http
            or not parts.hostname
            or not _is_literal_loopback(parts.hostname)
        ):
            raise OAuthDiscoveryError("insecure_endpoint", phase="network")
        return await asyncio.to_thread(
            build_async_ip_pinned_transport,
            url,
            allow_private=True,
            allowed_ports=None,
            verify=True,
        )
    return await asyncio.to_thread(
        build_async_ip_pinned_transport,
        url,
        allow_private=False,
        allowed_ports=None,
        verify=True,
    )


async def _read_json_response(response: httpx.Response, *, phase: str) -> Mapping[str, Any]:
    try:
        raw = await async_read_limited_bytes(response, limit=_MAX_HTTP_BODY_BYTES)
        parsed = json.loads(raw)
    except (
        RecursionError,
        ResponseTooLargeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise OAuthClientError(
            "invalid_response",
            phase=phase,
            status_code=response.status_code,
        ) from None
    if not isinstance(parsed, dict):
        raise OAuthClientError("invalid_response", phase=phase, status_code=response.status_code)
    return parsed


def _validate_public_client_metadata(
    metadata: OAuthAuthorizationServerMetadata,
    *,
    allow_loopback_http: bool = False,
) -> None:
    try:
        _validate_absolute_url(
            metadata.issuer,
            field="issuer",
            issuer=True,
            allow_loopback_http=allow_loopback_http,
        )
        _validate_absolute_url(
            metadata.authorization_endpoint,
            field="authorization_endpoint",
            allow_loopback_http=allow_loopback_http,
        )
        _validate_absolute_url(
            metadata.token_endpoint,
            field="token_endpoint",
            allow_loopback_http=allow_loopback_http,
        )
    except ValueError:
        raise OAuthDiscoveryError("insecure_endpoint", phase="discovery") from None
    if "code" not in metadata.response_types_supported:
        raise OAuthDiscoveryError("authorization_code_unsupported", phase="discovery")
    if metadata.grant_types_supported is not None and (
        "authorization_code" not in metadata.grant_types_supported
    ):
        raise OAuthDiscoveryError("authorization_code_unsupported", phase="discovery")
    if "none" not in metadata.token_endpoint_auth_methods_supported:
        raise OAuthDiscoveryError("public_client_unsupported", phase="discovery")
    if "S256" not in metadata.code_challenge_methods_supported:
        raise OAuthDiscoveryError("s256_unsupported", phase="discovery")


async def discover_oauth_metadata(
    issuer_url: str,
    *,
    timeout: float = 5.0,
    allow_loopback_http: bool = False,
) -> OAuthAuthorizationServerMetadata:
    """Discover and validate RFC 8414 metadata for an exact issuer URL.

    ``issuer_url`` is an authorization-server issuer, not an MCP/HTTP resource
    URL. The returned ``issuer`` must match it exactly. Plain HTTP is available
    only for literal loopback IPs when ``allow_loopback_http=True``.
    """
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise ValueError("timeout must be greater than 0 and at most 60 seconds")
    discovery_url = _metadata_url(issuer_url, allow_loopback_http=allow_loopback_http)

    async def fetch() -> Mapping[str, Any]:
        transport = await _transport_for(
            discovery_url,
            allow_loopback_http=allow_loopback_http,
        )
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                discovery_url,
                headers={"accept": "application/json", "accept-encoding": "identity"},
            ) as response:
                if response.status_code != 200:
                    raise OAuthDiscoveryError(
                        "http_error", phase="discovery", status_code=response.status_code
                    )
                return await _read_json_response(response, phase="discovery")

    try:
        raw = await _run_with_deadline(fetch, timeout)
    except _OAuthDeadlineExpired:
        raise OAuthDiscoveryError("timeout", phase="discovery") from None
    except OAuthDiscoveryError:
        raise
    except OAuthClientError as exc:
        raise OAuthDiscoveryError(
            exc.code, phase="discovery", status_code=exc.status_code
        ) from None
    except (httpx.HTTPError, OSError, SSRFValidationError, ValueError):
        raise OAuthDiscoveryError("network_error", phase="discovery") from None

    try:
        metadata = OAuthAuthorizationServerMetadata.model_validate(raw)
    except ValidationError:
        raise OAuthDiscoveryError("invalid_metadata", phase="discovery") from None
    if metadata.issuer != issuer_url:
        raise OAuthDiscoveryError("issuer_mismatch", phase="discovery")
    _validate_public_client_metadata(metadata, allow_loopback_http=allow_loopback_http)
    return metadata


def _random_urlsafe(byte_count: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).rstrip(b"=").decode("ascii")


def _pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _validate_scope(scopes: Sequence[str]) -> str | None:
    if len(scopes) > 128:
        raise ValueError("at most 128 OAuth scopes are accepted")
    for scope in scopes:
        if len(scope) > 256 or not _SCOPE_TOKEN_RE.fullmatch(scope):
            raise ValueError("OAuth scopes must use RFC 6749 scope-token syntax")
    joined = " ".join(scopes) or None
    if joined is not None and len(joined) > _MAX_SCOPE_LENGTH:
        raise ValueError("combined OAuth scope is too long")
    return joined


def _validate_resource(resource: str | None) -> str | None:
    if resource is None:
        return None
    if not 1 <= len(resource) <= _MAX_RESOURCE_LENGTH:
        raise ValueError("OAuth resource is too long")
    if "\\" in resource or any(ord(char) < 0x20 or ord(char) == 0x7F for char in resource):
        raise ValueError("OAuth resource contains an unsafe character")
    parts = urlsplit(resource)
    if (
        not parts.scheme
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("OAuth resource must be an absolute URI without userinfo or fragment")
    if parts.scheme in {"http", "https"} and not parts.hostname:
        raise ValueError("HTTP OAuth resources must include a host")
    try:
        parts.port
    except ValueError:
        raise ValueError("OAuth resource contains an invalid port") from None
    return resource


async def start_oauth_authorization(
    metadata: OAuthAuthorizationServerMetadata,
    *,
    client_id: str,
    redirect_uri: str,
    store: PendingOAuthFlowStore,
    issuer_binding: OAuthIssuerBinding,
    scopes: Sequence[str] = (),
    resource: str | None = None,
    ttl_seconds: int = _DEFAULT_FLOW_TTL_SECONDS,
    clock: Callable[[], datetime] | None = None,
    allow_loopback_http: bool = False,
) -> OAuthAuthorizationRequest:
    """Create, persist, and return a browser authorization request.

    ``DISTINCT_REDIRECT_URI`` is an explicit assertion that this redirect URI
    is not shared with any other issuer. Prefer ``AUTHORIZATION_RESPONSE_ISS``
    when the server advertises RFC 9207 support.
    """
    _validate_public_client_metadata(metadata, allow_loopback_http=allow_loopback_http)
    if not 1 <= len(client_id) <= _MAX_CLIENT_ID_LENGTH or not _VSCHAR_RE.fullmatch(client_id):
        raise ValueError("client_id must use bounded RFC 6749 VSCHAR syntax")
    if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 3600:
        raise ValueError("ttl_seconds must be between 1 and 3600")
    if not isinstance(issuer_binding, OAuthIssuerBinding):
        raise ValueError("issuer_binding must be an OAuthIssuerBinding value")
    try:
        _validate_absolute_url(
            redirect_uri,
            field="redirect_uri",
            allow_loopback_http=allow_loopback_http,
        )
    except ValueError:
        policy = "HTTPS or a literal loopback HTTP address" if allow_loopback_http else "HTTPS"
        raise ValueError(f"redirect_uri must use {policy}") from None
    if (
        issuer_binding is OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS
        and not metadata.authorization_response_iss_parameter_supported
    ):
        raise ValueError("authorization server does not advertise RFC 9207 issuer responses")

    scope = _validate_scope(scopes)
    resource = _validate_resource(resource)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    expires_at = now + timedelta(seconds=ttl_seconds)
    state = _random_urlsafe(32)
    verifier = _random_urlsafe(64)
    challenge = _pkce_challenge(verifier)
    pending = PendingOAuthAuthorization(
        state=state,
        code_verifier=SecretStr(verifier),
        issuer=metadata.issuer,
        authorization_endpoint=metadata.authorization_endpoint,
        token_endpoint=metadata.token_endpoint,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        resource=resource,
        issuer_binding=issuer_binding,
        allow_loopback_http=allow_loopback_http,
        created_at=now,
        expires_at=expires_at,
    )
    if not await store.insert_if_absent(pending):
        raise OAuthFlowStoreError("state_collision", phase="store")

    parts = urlsplit(metadata.authorization_endpoint)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(
        [
            ("response_type", "code"),
            ("client_id", client_id),
            ("redirect_uri", redirect_uri),
            ("state", state),
            ("code_challenge", challenge),
            ("code_challenge_method", "S256"),
        ]
    )
    if scope is not None:
        query.append(("scope", scope))
    if resource is not None:
        query.append(("resource", resource))
    authorization_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    if len(authorization_url) > _MAX_URL_LENGTH:
        # The flow remains stored and must not be reused. Consume it best-effort.
        await store.consume(state)
        raise ValueError("authorization URL exceeds the accepted length")
    return OAuthAuthorizationRequest(
        authorization_url=authorization_url,
        state=state,
        expires_at=expires_at,
    )


def _safe_oauth_error(value: object) -> str | None:
    if isinstance(value, str) and value in _SAFE_OAUTH_ERROR_CODES:
        return value
    return None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None or not value.isascii() or not value.isdigit():
        return None
    seconds = int(value)
    return float(seconds) if 0 <= seconds <= 86_400 else None


async def _exchange_code(
    pending: PendingOAuthAuthorization,
    *,
    code: str,
    timeout: float,
) -> OAuthTokenSet:
    form = {
        "grant_type": "authorization_code",
        "client_id": pending.client_id,
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "code_verifier": pending.code_verifier.get_secret_value(),
    }
    if pending.resource is not None:
        form["resource"] = pending.resource

    async def exchange() -> Mapping[str, Any]:
        transport = await _transport_for(
            pending.token_endpoint,
            allow_loopback_http=pending.allow_loopback_http,
        )
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                pending.token_endpoint,
                data=form,
                headers={"accept": "application/json", "accept-encoding": "identity"},
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    try:
                        raw = await _read_json_response(response, phase="token_exchange")
                    except OAuthClientError:
                        raise OAuthTokenExchangeError(
                            "http_error",
                            phase="token_exchange",
                            status_code=response.status_code,
                            retry_after_seconds=_retry_after(response),
                        ) from None
                    raise OAuthTokenExchangeError(
                        "http_error",
                        phase="token_exchange",
                        status_code=response.status_code,
                        oauth_error=_safe_oauth_error(raw.get("error")),
                        retry_after_seconds=_retry_after(response),
                    )
                return await _read_json_response(response, phase="token_exchange")

    try:
        raw = await _run_with_deadline(exchange, timeout)
    except _OAuthDeadlineExpired:
        raise OAuthTokenExchangeError("timeout", phase="token_exchange") from None
    except OAuthTokenExchangeError:
        raise
    except OAuthClientError as exc:
        raise OAuthTokenExchangeError(
            exc.code,
            phase="token_exchange",
            status_code=exc.status_code,
        ) from None
    except (httpx.HTTPError, OSError, SSRFValidationError, ValueError):
        raise OAuthTokenExchangeError("network_error", phase="token_exchange") from None

    try:
        return OAuthTokenSet.model_validate(raw)
    except ValidationError:
        raise OAuthTokenExchangeError("invalid_token_response", phase="token_exchange") from None


async def complete_oauth_authorization(
    *,
    code: str | None,
    callback_state: str,
    expected_state: str,
    store: PendingOAuthFlowStore,
    callback_issuer: str | None = None,
    callback_error: str | None = None,
    timeout: float = 10.0,
    clock: Callable[[], datetime] | None = None,
) -> OAuthTokenSet:
    """Consume one callback and exchange its code for public-client tokens.

    ``expected_state`` must come from a separate browser-session binding, not
    from the callback query itself. Once the two states match, the pending flow
    is consumed before any issuer/error/code handling or network I/O. Any
    failure therefore requires starting a new authorization flow.
    """
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        raise ValueError("timeout must be greater than 0 and at most 60 seconds")
    if not (
        43 <= len(callback_state) <= _MAX_STATE_LENGTH
        and 43 <= len(expected_state) <= _MAX_STATE_LENGTH
        and callback_state.isascii()
        and expected_state.isascii()
        and re.fullmatch(r"[A-Za-z0-9_-]+", callback_state)
        and re.fullmatch(r"[A-Za-z0-9_-]+", expected_state)
    ) or not hmac.compare_digest(callback_state, expected_state):
        raise OAuthAuthorizationError("state_mismatch", phase="callback")

    pending = await store.consume(callback_state)
    if pending is None:
        raise OAuthAuthorizationError("flow_not_found", phase="callback")

    if not hmac.compare_digest(pending.state, callback_state):
        raise OAuthAuthorizationError("state_mismatch", phase="callback")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    if pending.expires_at <= now:
        raise OAuthAuthorizationError("flow_expired", phase="callback")
    if callback_issuer is not None and (
        len(callback_issuer) > _MAX_URL_LENGTH or callback_issuer != pending.issuer
    ):
        raise OAuthAuthorizationError("issuer_mismatch", phase="callback")
    if (
        pending.issuer_binding is OAuthIssuerBinding.AUTHORIZATION_RESPONSE_ISS
        and callback_issuer is None
    ):
        raise OAuthAuthorizationError("issuer_mismatch", phase="callback")
    if callback_error is not None:
        raise OAuthAuthorizationError(
            "authorization_failed",
            phase="callback",
            oauth_error=_safe_oauth_error(callback_error),
        )
    if code is None or not 1 <= len(code) <= _MAX_CODE_LENGTH or not _VSCHAR_RE.fullmatch(code):
        raise OAuthAuthorizationError("invalid_code", phase="callback")
    return await _exchange_code(
        pending,
        code=code,
        timeout=timeout,
    )


__all__ = [
    "InMemoryPendingOAuthFlowStore",
    "OAuthAuthorizationError",
    "OAuthAuthorizationRequest",
    "OAuthAuthorizationServerMetadata",
    "OAuthClientError",
    "OAuthDiscoveryError",
    "OAuthFlowStoreError",
    "OAuthIssuerBinding",
    "OAuthTokenExchangeError",
    "OAuthTokenSet",
    "PendingOAuthAuthorization",
    "PendingOAuthFlowStore",
    "complete_oauth_authorization",
    "discover_oauth_metadata",
    "start_oauth_authorization",
]
