"""OAuth pass-through ``accounts.resolve`` factory ("Shape B").

Standardises the canonical Shape B account-resolution pattern:
an adapter wraps a vendor OAuth + ad-account API (Snap, Meta, TikTok,
LinkedIn, Reddit, Pinterest, etc.) and resolves the buyer's
:class:`AccountReference` by hitting the upstream's "list-my-accounts"
endpoint with the buyer's bearer.

Without this factory, every Shape B adapter rolls the same ~30 LOC:
extract bearer from ``ctx.auth_info``, GET ``/me/adaccounts``, match by
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
  returns just the resolve callable).
* **Publisher ops curates the roster** — your own
  :class:`AccountStore` impl backed by a database (Shape C).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from adcp.decisioning.accounts import ResolveContext
from adcp.decisioning.helpers import ref_account_id
from adcp.decisioning.types import Account
from adcp.decisioning.upstream import AuthContext, UpstreamHttpClient
from adcp.types import AccountReference

__all__ = ["create_oauth_passthrough_resolver"]


def _default_extract_rows(body: Any) -> list[Any] | None:
    """Default unwrap for the common ``/me/adaccounts``-shaped APIs.

    Accepts either a flat list (some plain-list APIs) or a
    ``{"data": [...]}`` envelope (Snap, Meta). Returns ``None`` when
    the body doesn't match either shape, signalling "no rows".
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
) -> Callable[
    [AccountReference | dict[str, Any] | None, ResolveContext | None],
    Awaitable[Account[Any] | None],
]:
    """Create an ``accounts.resolve`` callable backed by an upstream
    OAuth-protected listing endpoint.

    Returns just the resolve callable — adopters compose it into their
    own :class:`AccountStore` (typically alongside a no-op ``upsert``,
    since Shape B adapters don't manage account lifecycle on the
    seller side).

    :param http_client: Pre-configured upstream HTTP client (typically
        from :func:`create_upstream_http_client`). Should be configured
        with :class:`DynamicBearer` so the per-request auth context
        flows through to bearer selection.
    :param list_endpoint: Path on the upstream API that returns the
        buyer's accounts. Common shapes: ``/v1/adaccounts``,
        ``/me/adaccounts``, ``/customers``.
    :param to_account: Map an upstream row to a framework
        :class:`Account`. Sync or async — the framework awaits the
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
        that key (covers Snap / Meta / flat-list APIs). Provide a
        custom callback for deeper-nested shapes (e.g. TikTok's
        ``data.list``).
    :param get_auth_context: Extract the auth context to forward to the
        upstream's :meth:`DynamicBearer.get_token` resolver. The return
        value flows through as the per-call ``auth_context`` on
        :meth:`UpstreamHttpClient.get`. Defaults to forwarding
        ``ctx.auth_info`` verbatim — works when the http client's token
        resolver reads from :class:`AuthInfo` directly.

    Behavior:

    * The factory only handles the ``{account_id}`` discriminated-union
      arm of :class:`AccountReference`. Other arms (``{brand,
      operator}``) and ``None`` ref return ``None`` without calling
      upstream. Adopters needing natural-key fallback compose their own
      resolver around this one.
    * Upstream errors propagate verbatim — ``http_client`` already
      projects non-2xx to spec-conformant :class:`AdcpError` codes
      (``AUTH_REQUIRED``, ``SERVICE_UNAVAILABLE``, etc.). Adopters
      compose error mapping over the result if they want a different
      shape.
    * 404 from the upstream listing endpoint surfaces as ``None`` (the
      http client's ``treat_404_as_none`` default), which the factory
      treats as "no rows found".

    Example::

        from adcp.decisioning import (
            DynamicBearer,
            create_oauth_passthrough_resolver,
            create_upstream_http_client,
        )

        async def get_token(ctx):
            # ctx is the AuthInfo forwarded by default get_auth_context.
            return ctx.credential.token

        snap = create_upstream_http_client(
            "https://adsapi.snapchat.com",
            auth=DynamicBearer(get_token=get_token),
        )

        resolve = create_oauth_passthrough_resolver(
            http_client=snap,
            list_endpoint="/v1/me/adaccounts",
            to_account=lambda row, ctx: Account(
                id=row["id"],
                name=row["name"],
                status="active",
                metadata={"upstream_id": row["id"]},
            ),
        )
    """
    extract = extract_rows if extract_rows is not None else _default_extract_rows
    auth_ctx_fn = get_auth_context if get_auth_context is not None else _default_auth_context

    async def resolve(
        ref: AccountReference | dict[str, Any] | None,
        ctx: ResolveContext | None = None,
    ) -> Account[Any] | None:
        account_id = ref_account_id(ref)
        if account_id is None:
            return None

        auth_ctx = auth_ctx_fn(ctx)
        body = await http_client.get(
            list_endpoint,
            auth_context=auth_ctx,
        )
        rows = extract(body)
        if rows is None:
            return None

        for row in rows:
            row_id = row.get(id_field) if isinstance(row, dict) else getattr(row, id_field, None)
            if row_id == account_id:
                result = to_account(row, ctx)
                if inspect.isawaitable(result):
                    return await result
                return result
        return None

    return resolve


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
