"""OAuth pass-through ``AccountStore`` factory ("Shape B").

Standardises the canonical Shape B account-resolution pattern: an
adapter wraps a vendor OAuth + ad-account API (e.g. social-platform ad
management APIs that expose ``/me/adaccounts``-shaped endpoints) and
resolves the buyer's :class:`AccountReference` by hitting the
upstream's "list-my-accounts" endpoint with the buyer's bearer.

Without this factory, every Shape B adapter rolls the same ~30 LOC:
extract bearer from ``auth_info``, GET ``/me/adaccounts``, match by
id, return the mapped :class:`Account`. This factory handles the
boilerplate; the adapter supplies the upstream specifics
(``list_endpoint``, ``to_account`` mapper) and the auth shape via
:func:`create_upstream_http_client`'s :class:`DynamicBearer.get_token`.

Mirrors the JS ``createOAuthPassthroughResolver`` from
``@adcp/sdk@6.7`` (``src/lib/adapters/oauth-passthrough-resolver.ts``).

Picking an :class:`AccountStore`? Three reference shapes by *who creates
the account*:

* **Buyer self-onboards via ``sync_accounts``** — implement
  :class:`AccountStoreUpsert` (Shape A).
* **Upstream OAuth API owns the roster** —
  :func:`create_oauth_passthrough_resolver` (this module, Shape B,
  returns an :class:`AccountStore`).
* **Publisher ops curates the roster** — your own
  :class:`AccountStore` impl backed by a database (Shape C).

**Pagination limitation.** The factory issues a single GET against
``list_endpoint`` and treats the parsed body as the full account
list. Upstreams that paginate (cursor / next-url envelopes) drop
accounts beyond page one silently. Adopters with paginated upstreams
must either aggregate pages inside ``extract_rows`` (synchronous
collection of all pages before returning the list) or compose their
own resolver. See the :class:`AccountStore` Protocol if you need
streaming pagination.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from adcp.decisioning.accounts import ResolveContext
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.helpers import ref_account_id
from adcp.decisioning.types import Account
from adcp.decisioning.upstream import AuthContext, UpstreamHttpClient
from adcp.types import AccountReference

__all__ = ["create_oauth_passthrough_resolver"]


def _default_extract_rows(body: Any) -> list[Any] | None:
    """Default unwrap for the common ``/me/adaccounts``-shaped APIs.

    Accepts either a flat list (some plain-list APIs) or a
    ``{"data": [...]}`` envelope. Returns ``None`` when the body
    doesn't match either shape, signalling "no rows".
    """
    if body is None:
        return None
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        rows = body.get("data")
        if isinstance(rows, list):
            return rows
    return None


def _default_auth_context(ctx: ResolveContext | None) -> AuthContext | None:
    """Default ``get_auth_context``: forward ``ctx.auth_info`` verbatim.

    Works when the http client's :class:`DynamicBearer.get_token`
    resolver reads the bearer off the :class:`AuthInfo` directly. The
    upstream client treats this as an opaque mapping; the factory
    doesn't interpret it.
    """
    if ctx is None:
        return None
    return ctx.auth_info  # type: ignore[return-value]


class _OAuthPassthroughAccountStore:
    """:class:`AccountStore` impl backing :func:`create_oauth_passthrough_resolver`.

    Public attributes match the :class:`AccountStore` Protocol so an
    instance plugs directly into :class:`DecisioningPlatform.accounts`.
    The resolve method takes ``(ref, auth_info=None)`` per the
    Protocol; the factory's ``to_account`` and ``get_auth_context``
    callbacks see a synthesised :class:`ResolveContext` so adopter
    callbacks have a uniform shape with the rest of the
    :class:`AccountStore` surface.
    """

    resolution: Literal["explicit"] = "explicit"

    def __init__(
        self,
        *,
        http_client: UpstreamHttpClient,
        list_endpoint: str,
        to_account: Callable[
            [Any, ResolveContext | None],
            Account[Any] | Awaitable[Account[Any]],
        ],
        id_field: str,
        extract_rows: Callable[[Any], list[Any] | None],
        get_auth_context: Callable[[ResolveContext | None], AuthContext | None],
    ) -> None:
        self._http_client = http_client
        self._list_endpoint = list_endpoint
        self._to_account = to_account
        self._id_field = id_field
        self._extract_rows = extract_rows
        self._get_auth_context = get_auth_context

    async def resolve(
        self,
        ref: AccountReference | dict[str, Any] | None,
        auth_info: AuthInfo | None = None,
    ) -> Account[Any] | None:
        account_id = ref_account_id(ref)
        if account_id is None:
            return None

        ctx = ResolveContext(auth_info=auth_info, tool_name="resolve")
        auth_ctx = self._get_auth_context(ctx)
        body = await self._http_client.get(
            self._list_endpoint,
            auth_context=auth_ctx,
        )
        rows = self._extract_rows(body)
        if rows is None:
            return None

        for row in rows:
            row_id = (
                row.get(self._id_field)
                if isinstance(row, dict)
                else getattr(row, self._id_field, None)
            )
            if row_id == account_id:
                result = self._to_account(row, ctx)
                if inspect.isawaitable(result):
                    return await result
                return result
        return None


def create_oauth_passthrough_resolver(
    *,
    http_client: UpstreamHttpClient,
    list_endpoint: str,
    to_account: Callable[
        [Any, ResolveContext | None],
        Account[Any] | Awaitable[Account[Any]],
    ],
    id_field: str = "id",
    extract_rows: Callable[[Any], list[Any] | None] | None = None,
    get_auth_context: Callable[[ResolveContext | None], AuthContext | None] | None = None,
) -> _OAuthPassthroughAccountStore:
    """Create an :class:`AccountStore` backed by an upstream
    OAuth-protected listing endpoint.

    The returned object satisfies the :class:`AccountStore` Protocol
    (``resolution = 'explicit'``, ``resolve(ref, auth_info=None)``).
    Adopters wire it directly into :class:`DecisioningPlatform`::

        class SnapSeller(DecisioningPlatform):
            accounts = create_oauth_passthrough_resolver(...)

    Shape B adapters typically don't manage account lifecycle on the
    seller side, so the returned store implements only ``resolve`` —
    not the optional :meth:`AccountStoreUpsert.upsert` /
    :meth:`AccountStoreList.list` surfaces. Add those by wrapping the
    returned store in a class that delegates ``resolve`` and adds the
    upsert/list methods.

    :param http_client: Pre-configured upstream HTTP client (typically
        from :func:`create_upstream_http_client`). Should be configured
        with :class:`DynamicBearer` so the per-request auth context
        flows through to bearer selection.
    :param list_endpoint: Path on the upstream API that returns the
        buyer's accounts. Common shapes: ``/v1/adaccounts``,
        ``/me/adaccounts``, ``/customers``.
    :param to_account: Map an upstream row to a framework
        :class:`Account`. Receives the row and a synthesised
        :class:`ResolveContext` (carrying the caller's
        ``auth_info``). Sync or async — the framework awaits the
        result either way.

        **Treat any embedded credential in ``Account.metadata`` as a
        secret.** The framework strips ``metadata`` from the wire
        response, but adopter code that throws an error containing
        ``json.dumps(account)`` or logs ``ctx.account`` at info level
        WILL leak it. Either don't embed the bearer (re-derive from
        ``ctx.auth_info`` on each downstream method), or audit your
        error projections.
    :param id_field: Field on each upstream row that matches
        ``AccountReference.account_id``. Defaults to ``"id"``. A typo
        here silently always returns ``None`` — verify against the
        upstream's documented response shape.
    :param extract_rows: Optional callback receiving the raw parsed
        upstream body and returning the row list. Defaults to: try the
        body if it's a list, else ``body["data"]`` if it's a dict with
        that key. Provide a custom callback for deeper-nested shapes
        (e.g. ``{"data": {"list": [...]}}``).
    :param get_auth_context: Extract the auth context to forward to the
        upstream's :meth:`DynamicBearer.get_token` resolver. The return
        value flows through as the per-call ``auth_context`` on
        :meth:`UpstreamHttpClient.get`. Defaults to forwarding
        ``ctx.auth_info`` verbatim — works when the http client's token
        resolver reads from :class:`AuthInfo` directly.

    Behavior:

    * The returned store only handles the ``{account_id}``
      discriminated-union arm of :class:`AccountReference`. Other arms
      (``{brand, operator}``) and ``None`` ref return ``None`` without
      calling upstream. Adopters needing natural-key fallback compose
      their own resolver around this one.
    * Upstream errors propagate verbatim — ``http_client`` already
      projects non-2xx to spec-conformant :class:`AdcpError` codes
      (``AUTH_REQUIRED``, ``SERVICE_UNAVAILABLE``, etc.). Adopters
      compose error mapping over the result if they want a different
      shape.
    * 404 from the upstream listing endpoint surfaces as ``None`` (the
      http client's ``treat_404_as_none`` default), which the store
      treats as "no rows found".
    * **Pagination is not handled.** A single GET fetches the full
      list; paginated upstreams drop accounts beyond page one. See the
      module docstring for adopter workarounds.

    Example::

        from adcp.decisioning import (
            DynamicBearer,
            create_oauth_passthrough_resolver,
            create_upstream_http_client,
        )

        async def get_token(ctx):
            # ctx is the AuthInfo forwarded by default get_auth_context.
            return ctx.credential.token

        upstream = create_upstream_http_client(
            "https://upstream.example.com",
            auth=DynamicBearer(get_token=get_token),
        )

        class UpstreamSeller(DecisioningPlatform):
            accounts = create_oauth_passthrough_resolver(
                http_client=upstream,
                list_endpoint="/v1/me/adaccounts",
                to_account=lambda row, ctx: Account(
                    id=row["id"],
                    name=row["name"],
                    status="active",
                    metadata={"upstream_id": row["id"]},
                ),
            )
    """
    return _OAuthPassthroughAccountStore(
        http_client=http_client,
        list_endpoint=list_endpoint,
        to_account=to_account,
        id_field=id_field,
        extract_rows=(extract_rows if extract_rows is not None else _default_extract_rows),
        get_auth_context=(
            get_auth_context if get_auth_context is not None else _default_auth_context
        ),
    )
