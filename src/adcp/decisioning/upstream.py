"""HTTP client primitives for AdCP translator adapters.

Every translator-pattern seller (Prebid, GAM-shaped publishers, the v3
reference seller) wires the same ~200 lines of httpx boilerplate: auth
header injection, JSON parsing, 404→None for resource lookups, error
projection to spec-conformant ``AdcpError`` codes. This module ships
those primitives so adopters write only the upstream-specific bits.

Mirrors the JS ``createUpstreamHttpClient`` helper from
``@adcp/sdk@6.7`` (``src/lib/server/upstream-helpers.ts``). Python
diverges from the JS shape in two places:

* Methods return parsed JSON (``dict``) directly rather than a
  ``{status, body}`` envelope. Within-2xx status distinction (201 vs
  200) hasn't been a real-world need; we can add a ``raw_*`` variant
  later if it does.
* Non-2xx responses raise :class:`adcp.decisioning.types.AdcpError`
  with a spec-conformant code (``AUTH_REQUIRED``, ``PERMISSION_DENIED``,
  ``MEDIA_BUY_NOT_FOUND``, ``RATE_LIMITED``, ``SERVICE_UNAVAILABLE``,
  ``INVALID_REQUEST``) rather than a generic ``Error``. Adopters can
  override the 404 code per call via ``not_found_code`` for endpoints
  whose missing-resource semantics aren't a media buy (e.g. creatives,
  forecasts).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from adcp.decisioning.types import AdcpError

#: Per-call routing context forwarded to :class:`DynamicBearer.get_token`.
#:
#: Adopters define their own shape. Common fields:
#:
#: * ``operator_id`` — resolved tenant id for per-operator credential lookup
#: * ``network_code`` — multi-tenant routing key (matches AdCP
#:   ``RequestContext.account.ext['network_code']``)
#: * ``principal`` — pass-through of the caller's bearer token
#:
#: The SDK doesn't interpret this; it forwards it verbatim so the same
#: client supports single-key tenant fan-out, per-operator credential
#: lookup, and pass-through of the caller's principal without
#: per-operator client construction.
AuthContext = Mapping[str, Any]


@dataclass(frozen=True)
class StaticBearer:
    """Fixed Bearer token injected into every request."""

    token: str
    kind: Literal["static_bearer"] = "static_bearer"


@dataclass(frozen=True)
class DynamicBearer:
    """Async token factory called per-request.

    ``get_token`` receives the optional ``auth_context`` passed to the
    method call, so the same resolver can return a master key for
    tenant fan-out, a per-operator key, or pass-through of the
    caller's principal. OAuth client-credentials refresh is the
    adopter's responsibility — typically a cached token with
    expiry-driven refresh inside the closure.
    """

    get_token: Callable[[AuthContext | None], Awaitable[str]]
    kind: Literal["dynamic_bearer"] = "dynamic_bearer"


@dataclass(frozen=True)
class ApiKey:
    """Fixed key injected into a named header (e.g. ``X-Api-Key``)."""

    header_name: str
    key: str
    kind: Literal["api_key"] = "api_key"


@dataclass(frozen=True)
class NoAuth:
    """No authentication header injected. For unauthenticated services."""

    kind: Literal["none"] = "none"


#: Discriminated union of authentication strategies.
UpstreamAuth = StaticBearer | DynamicBearer | ApiKey | NoAuth


# Spec-conformant default mapping from upstream HTTP status to AdcpError code.
# Per the AdCP error-code enum at schemas/cache/3.0.0/enums/error-code.json.
# 404 is configurable per-call via ``not_found_code`` because the right
# AdCP code depends on the resource being looked up.
_DEFAULT_NOT_FOUND_CODE = "MEDIA_BUY_NOT_FOUND"


def _project_status(
    status_code: int,
    *,
    not_found_code: str,
    method: str,
    path: str,
    body_text: str,
) -> AdcpError:
    """Project an upstream non-2xx status to a spec-conformant AdcpError."""
    snippet = body_text[:200] if body_text else ""
    suffix = f" — {snippet}" if snippet else ""
    base = f"upstream {method} {path} failed: {status_code}{suffix}"
    if status_code == 401:
        return AdcpError(
            "AUTH_REQUIRED",
            message=base,
            recovery="terminal",
        )
    if status_code == 403:
        return AdcpError(
            "PERMISSION_DENIED",
            message=base,
            recovery="terminal",
        )
    if status_code == 404:
        return AdcpError(
            not_found_code,
            message=base,
            recovery="terminal",
        )
    if status_code == 429:
        return AdcpError(
            "RATE_LIMITED",
            message=base,
            recovery="transient",
        )
    if status_code >= 500:
        return AdcpError(
            "SERVICE_UNAVAILABLE",
            message=base,
            recovery="transient",
        )
    # Any other 4xx → buyer-fixable.
    return AdcpError(
        "INVALID_REQUEST",
        message=base,
        recovery="retry_with_changes",
    )


async def _resolve_auth_header(
    auth: UpstreamAuth,
    auth_context: AuthContext | None,
) -> dict[str, str]:
    if isinstance(auth, StaticBearer):
        return {"Authorization": f"Bearer {auth.token}"}
    if isinstance(auth, DynamicBearer):
        token = await auth.get_token(auth_context)
        return {"Authorization": f"Bearer {token}"}
    if isinstance(auth, ApiKey):
        return {auth.header_name: auth.key}
    if isinstance(auth, NoAuth):
        return {}
    # Exhaustive — kept as a guard against future additions.
    raise TypeError(f"unknown UpstreamAuth variant: {type(auth).__name__}")


class UpstreamHttpClient:
    """Thin typed HTTP client over ``httpx.AsyncClient``.

    Construct via :func:`create_upstream_http_client`. Connection
    pooling is automatic (``max_keepalive_connections=10,
    max_connections=20``) and the client reuses a single
    ``httpx.AsyncClient`` across calls. Call :meth:`aclose` on shutdown
    to release the pool, or use ``async with`` if your wiring supports
    it.

    Method signatures:

    * :meth:`get` — returns parsed JSON ``dict``, or ``None`` on 404
      when ``treat_404_as_none=True``.
    * :meth:`post` / :meth:`put` — returns parsed JSON ``dict``.
    * :meth:`delete` — returns parsed JSON ``dict``, or ``None`` on
      404 when ``treat_404_as_none=True``.

    All non-2xx responses raise :class:`AdcpError` with a
    spec-conformant code. The 404 code is configurable per call via
    ``not_found_code`` for endpoints whose missing-resource semantics
    aren't a media buy.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth: UpstreamAuth,
        default_headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        treat_404_as_none: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._default_headers: dict[str, str] = dict(default_headers or {})
        self._timeout = timeout
        self._treat_404_as_none = treat_404_as_none
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        """Release the underlying ``httpx.AsyncClient`` connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> UpstreamHttpClient:
        await self._get_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json: Any,
        headers: Mapping[str, str] | None,
        auth_context: AuthContext | None,
        not_found_code: str,
    ) -> dict[str, Any] | None:
        auth_header = await _resolve_auth_header(self._auth, auth_context)
        merged: dict[str, str] = {**self._default_headers, **auth_header}
        if headers:
            merged.update(headers)
        if json is not None:
            # httpx adds Content-Type itself when ``json=`` is used, but
            # set it explicitly so adopter middleware sees it pre-flight.
            merged.setdefault("Content-Type", "application/json")

        # Drop None query params — caller-friendly: pass kwargs through
        # without filtering, matches the JS helper's behavior.
        cleaned_params: dict[str, Any] | None = None
        if params is not None:
            cleaned_params = {k: v for k, v in params.items() if v is not None}

        client = await self._get_client()
        response = await client.request(
            method,
            path,
            params=cleaned_params,
            json=json,
            headers=merged,
        )
        if response.status_code == 404 and self._treat_404_as_none:
            return None
        if response.status_code >= 400:
            try:
                body_text = response.text
            except Exception:  # pragma: no cover — defensive
                body_text = ""
            raise _project_status(
                response.status_code,
                not_found_code=not_found_code,
                method=method,
                path=path,
                body_text=body_text,
            )
        if response.status_code == 204 or not response.content:
            return {}
        parsed: dict[str, Any] = response.json()
        return parsed

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth_context: AuthContext | None = None,
        not_found_code: str = _DEFAULT_NOT_FOUND_CODE,
    ) -> dict[str, Any] | None:
        """``GET path``. Returns parsed JSON, or ``None`` on 404 when ``treat_404_as_none``."""
        return await self._request(
            "GET",
            path,
            params=params,
            json=None,
            headers=headers,
            auth_context=auth_context,
            not_found_code=not_found_code,
        )

    async def post(
        self,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth_context: AuthContext | None = None,
        not_found_code: str = _DEFAULT_NOT_FOUND_CODE,
    ) -> dict[str, Any]:
        """``POST path`` with JSON body. Returns parsed JSON."""
        result = await self._request(
            "POST",
            path,
            params=params,
            json=json,
            headers=headers,
            auth_context=auth_context,
            not_found_code=not_found_code,
        )
        # POST 404 is unusual; treat_404_as_none still applies but
        # callers don't expect None — surface as MEDIA_BUY_NOT_FOUND.
        if result is None:
            raise AdcpError(
                not_found_code,
                message=f"upstream POST {path} returned 404",
                recovery="terminal",
            )
        return result

    async def put(
        self,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth_context: AuthContext | None = None,
        not_found_code: str = _DEFAULT_NOT_FOUND_CODE,
    ) -> dict[str, Any]:
        """``PUT path`` with JSON body. Returns parsed JSON."""
        result = await self._request(
            "PUT",
            path,
            params=params,
            json=json,
            headers=headers,
            auth_context=auth_context,
            not_found_code=not_found_code,
        )
        if result is None:
            raise AdcpError(
                not_found_code,
                message=f"upstream PUT {path} returned 404",
                recovery="terminal",
            )
        return result

    async def delete(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth_context: AuthContext | None = None,
        not_found_code: str = _DEFAULT_NOT_FOUND_CODE,
    ) -> dict[str, Any] | None:
        """``DELETE path``. Returns parsed JSON, or ``None`` on 404 when ``treat_404_as_none``."""
        return await self._request(
            "DELETE",
            path,
            params=params,
            json=None,
            headers=headers,
            auth_context=auth_context,
            not_found_code=not_found_code,
        )


def create_upstream_http_client(
    base_url: str,
    *,
    auth: UpstreamAuth | None = None,
    default_headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    treat_404_as_none: bool = True,
) -> UpstreamHttpClient:
    """Create a thin typed HTTP client for an upstream platform API.

    Handles auth injection, JSON serialization, 404→None translation,
    and projection of non-2xx responses to spec-conformant
    :class:`AdcpError` codes so adapters focus on domain logic.

    :param base_url: Base URL of the upstream API. Trailing slashes
        are stripped.
    :param auth: Authentication strategy. Defaults to :class:`NoAuth`
        for unauthenticated internal services.
    :param default_headers: Headers included on every request
        (e.g. tenant id, API version). Per-request headers passed to
        individual method calls take precedence.
    :param timeout: Per-request timeout in seconds. Default 30.0.
    :param treat_404_as_none: When ``True`` (default), GET/DELETE 404
        responses return ``None`` rather than raising. Set ``False``
        to surface 404 as ``AdcpError(MEDIA_BUY_NOT_FOUND)``. POST and
        PUT always raise on 404 — they're not lookups.

    Example::

        client = create_upstream_http_client(
            "https://upstream.example.com",
            auth=DynamicBearer(get_token=resolve_tenant_token),
            default_headers={"X-API-Version": "2"},
        )
        order = await client.get(
            f"/v1/orders/{order_id}",
            auth_context={"network_code": ctx.account.ext["network_code"]},
        )
        if order is None:
            raise AdcpError("MEDIA_BUY_NOT_FOUND", message=f"order {order_id}")
    """
    return UpstreamHttpClient(
        base_url=base_url,
        auth=auth or NoAuth(),
        default_headers=default_headers,
        timeout=timeout,
        treat_404_as_none=treat_404_as_none,
    )


__all__ = [
    "ApiKey",
    "AuthContext",
    "DynamicBearer",
    "NoAuth",
    "StaticBearer",
    "UpstreamAuth",
    "UpstreamHttpClient",
    "create_upstream_http_client",
]
